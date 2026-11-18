#!/usr/bin/env python3
"""Promote a signed Harbor digest from a specific reviewed Jenkins build."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import quote

from bounded_file import read_bounded_bytes
import ci_gitops_handoff as common
import gerrit_artifact_promotion as promotion
import jenkins_gerrit_gitops_handoff as source_handoff
import jenkins_static_image as publisher
from kubernetes_management import Client, KubernetesOperationError
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
import verify_image_signature as signatures


def validate(plan, contract):
    publisher.harbor.exact(plan, "schema_version handoff artifact_policy publisher_contract_sha256 signature_policy")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-jenkins-artifact-promotion-plan")
    publisher.validate(contract)
    if publisher.fingerprint(contract) != plan["publisher_contract_sha256"]:
        raise ValueError("jenkins-publisher-contract-changed")
    report, source = source_handoff.validate(plan["handoff"])
    if any(contract[key] != report["trigger"][key] for key in publisher.SCOPE):
        raise ValueError("jenkins-publisher-and-review-scope-mismatch")
    deployment = {**plan["handoff"]["promotion"], "artifact_policy": plan["artifact_policy"]}
    promotion.validate_artifact_policy(deployment)
    if (deployment["allow_prune"] or deployment["allow_create_branch"]
            or plan["artifact_policy"]["expected_deployment_revision"] == "0" * 40):
        raise ValueError("jenkins-artifact-promotion-requires-reviewed-existing-branch-without-prune")
    if contract["source"]["read_username"].casefold() in {deployment["git_username"].casefold(), report["reviewer_username"].casefold()}:
        raise ValueError("jenkins-publisher-must-use-separate-source-reader")
    signatures.validate({"schema_version": 2, "image": publisher.image_reference(contract, "sha256:" + "0" * 64),
                         "annotations": {"platform.gitops/validation": "pending"}, **promotion.signature_policy(plan)})
    return report, deployment, source


def verify(plan, contract, queue_journal, review_journal_dir, jenkins_get, gerrit_get, reviewer_api):
    report = plan["handoff"]["report"]
    trigger = report["trigger"]

    def merged():
        return source_handoff.reporter.verify_merged(report, queue_journal, review_journal_dir, jenkins_get, gerrit_get, reviewer_api)

    evidence = merged()
    if (evidence.get("review_vote_verified") is not True
            or any(evidence[key] != trigger[key] for key in ("change", "patchset", "revision"))
            or type(evidence["queue_id"]) is not int or evidence["queue_id"] < 1):
        raise ValueError("jenkins-artifact-requires-exact-merged-review")
    identity = {key: evidence[key] for key in publisher.IDENTITY}
    publisher.validate_identity(contract, identity)
    job = "".join("/job/" + quote(part, safe="") for part in trigger["job"])
    path = f"{job}/{identity['build']}/artifact/{publisher.ARTIFACT}"
    raw = jenkins_get(path, False, accept="application/json", max_bytes=publisher.LIMIT)
    artifact = publisher.verify_receipt(contract, identity, loads_strict_json(raw))
    if merged() != evidence:
        raise ValueError("jenkins-merged-evidence-changed-during-artifact-read")
    data = {"revision": evidence["revision"], "jenkins_build": evidence["build"], "jenkins_queue_id": evidence["queue_id"]}
    scope = {"review": evidence, "receipt_sha256": hashlib.sha256(raw).hexdigest(),
             "publisher_contract_sha256": plan["publisher_contract_sha256"], "artifact": artifact}
    return data, scope


def signature_annotations(plan, data, evidence):
    trigger = plan["handoff"]["report"]["trigger"]
    identity = {key: trigger[key] for key in publisher.SCOPE}
    scope = {key: value for key, value in plan.items() if key != "signature_policy"}
    return signatures.validate_annotations({
        "platform.gitops/source-identity-sha256": common.sha256(identity),
        "platform.gitops/source-revision": data["revision"],
        "platform.gitops/jenkins-build": str(data["jenkins_build"]),
        "platform.gitops/jenkins-queue-id": str(data["jenkins_queue_id"]),
        "platform.gitops/publisher-contract-sha256": evidence["publisher_contract_sha256"],
        "platform.gitops/ci-evidence-sha256": common.sha256(evidence),
        "platform.gitops/promotion-policy-sha256": common.sha256(scope),
        "platform.gitops/image-sha256": hashlib.sha256(evidence["artifact"]["image"].encode()).hexdigest(),
    })


def release_request(plan, contract, queue_journal, review_journal_dir, *readers):
    validate(plan, contract)
    data, evidence = verify(plan, contract, queue_journal, review_journal_dir, *readers)
    return {"schema_version": 1, "image": evidence["artifact"]["image"], "annotations": signature_annotations(plan, data, evidence)}


def execute(plan, contract, queue_journal, review_journal_dir, readers, client, *, git_inputs, apply=False, record=None):
    _, deployment, source = validate(plan, contract)
    if type(apply) is not bool:
        raise ValueError("invalid-jenkins-artifact-promotion-mode")

    def verified():
        return verify(plan, contract, queue_journal, review_journal_dir, *readers)

    def signed(data, evidence):
        selected = {"schema_version": 2, "image": evidence["artifact"]["image"],
                    "annotations": signature_annotations(plan, data, evidence), **plan["signature_policy"]}
        return promotion.verify_release_signature(selected)

    return promotion.execute_verified(deployment, source, client, verified, apply=apply, record=record, git_inputs=git_inputs,
                                      inspect_cd=promotion.artifact_cd_object, signature_verifier=signed)


def operate(plan, contract, *, apply=False, check_server=False, prepare_signature=False, inputs=None, git_inputs=None,
            queue_journal=None, review_journal_dir=None, output=None):
    report, deployment, _ = validate(plan, contract)
    if not (apply or check_server or prepare_signature):
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or None in (inputs, queue_journal, review_journal_dir):
        raise ValueError("live-jenkins-evidence-requires-linux-and-private-inputs")
    if not prepare_signature and (git_inputs is None or output is None):
        raise ValueError("jenkins-server-mode-requires-git-inputs-and-new-proof-directory")
    jenkins_ca = bridge.private_input("jenkins-ca.crt", maximum=65536, root=inputs, trim_line_endings=False)
    gerrit_ca = bridge.private_input("ca.crt", maximum=65536, root=inputs, trim_line_endings=False)
    if (hashlib.sha256(jenkins_ca).hexdigest() != contract["jenkins_ca_sha256"]
            or hashlib.sha256(gerrit_ca).hexdigest() != contract["source"]["ca_sha256"]):
        raise ValueError("jenkins-or-gerrit-artifact-ca-changed")
    readers = source_handoff.private_readers(report, deployment["git_username"], inputs,
        jenkins_tls=ssl.create_default_context(cadata=jenkins_ca.decode("ascii")),
        gerrit_tls=ssl.create_default_context(cadata=gerrit_ca.decode("ascii")))
    if prepare_signature:
        return release_request(plan, contract, queue_journal, review_journal_dir, *readers)
    target = deployment["cd"]["management_target"]
    record, state = common.new_journal(output, {"schema_version": 1, "plan_sha256": common.sha256(plan),
        "runtime": deployment["runtime"], "controller_kind": "argocd", "controller_verified": False,
        "deployment_rollout_verified": False, "full_stack_verified": False})
    try:
        with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
            return execute(plan, contract, queue_journal, review_journal_dir, readers, Client(target),
                           git_inputs=git_inputs, apply=apply, record=record)
    except Exception as error:
        code = str(error) if isinstance(error, common.HandoffError) else ""
        error_type = type(error).__name__
        diagnostic = {"exception_type": error_type if error_type in {"ValueError", "HandoffError", "FileNotFoundError",
            "PermissionError", "KubernetesOperationError", "TimeoutError", "HTTPError", "URLError", "SSLError", "SSLCertVerificationError"} else "other"}
        if isinstance(error, KubernetesOperationError):
            diagnostic["kubernetes"] = error.diagnostic
        record(phase="failed", failed_at=state["phase"],
               error_code=code if re.fullmatch(r"[a-z0-9-]{1,100}", code) else "jenkins-artifact-promotion-failed",
               failure_diagnostic=diagnostic)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--publisher-contract", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--git-input-dir", type=Path)
    parser.add_argument("--queue-journal", type=Path)
    parser.add_argument("--review-journal-dir", type=Path)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-signature", action="store_true")
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan, contract = [loads_strict_json(read_bounded_bytes(path, max_bytes=128 * 1024))
                          for path in (args.plan, args.publisher_contract)]
        result = operate(plan, contract, apply=args.apply, check_server=args.check_server, prepare_signature=args.prepare_signature,
                         inputs=args.input_dir, git_inputs=args.git_input_dir, queue_journal=args.queue_journal,
                         review_journal_dir=args.review_journal_dir, output=args.output)
    except Exception:
        print("Jenkins artifact promotion stopped. Check the private evidence and deployment branch before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
