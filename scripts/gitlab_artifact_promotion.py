#!/usr/bin/env python3
"""Promote a signed image published by a successful, protected GitLab push job."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys

from bounded_file import read_bounded_bytes
import ci_gitops_handoff as common
import gerrit_artifact_promotion as promotion
import gitlab_gitops_handoff as source_handoff
import gitlab_static_image as publisher
from kubernetes_management import Client
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
import verify_gitlab_pipeline as gitlab
import verify_image_signature as signatures


def validate(plan, contract):
    publisher.harbor.exact(plan, "schema_version runtime pipeline handoff artifact_policy publisher_contract_sha256 signature_policy")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise common.HandoffError("invalid-gitlab-artifact-promotion-plan")
    publisher.validate(contract)
    if publisher.fingerprint(contract) != plan["publisher_contract_sha256"]:
        raise common.HandoffError("gitlab-publisher-contract-does-not-match-reviewed-pin")
    fixed = {name: plan[name] for name in ("schema_version", "runtime", "pipeline", "handoff")}
    source = source_handoff.validate(fixed)
    pipeline = plan["pipeline"]
    if (any(contract[name] != pipeline[name] for name in ("api_url", "project_id", "project_path", "branch"))
            or contract["job"] not in pipeline["required_jobs"] or pipeline["revision"] == "0" * 40):
        raise common.HandoffError("gitlab-publisher-and-pipeline-scope-mismatch")
    deployment = {**plan["handoff"], "artifact_policy": plan["artifact_policy"]}
    promotion.validate_artifact_policy(deployment)
    if deployment["allow_prune"] or deployment["allow_create_branch"] or plan["artifact_policy"]["expected_deployment_revision"] == "0" * 40:
        raise common.HandoffError("gitlab-artifact-promotion-requires-reviewed-existing-branch-without-prune")
    signatures.validate({"schema_version": 2, "image": publisher.image_reference(contract, "sha256:" + "0" * 64),
                         "annotations": {"platform.gitops/validation": "pending"}, **promotion.signature_policy(plan)})
    return deployment, source


def verify(plan, contract, get, get_artifact):
    pipeline = plan["pipeline"]
    identity = {name: pipeline[name] for name in ("project_id", "pipeline_id", "revision")}
    artifact = None

    def observe(jobs):
        nonlocal artifact
        identity["job_id"] = jobs[contract["job"]]
        path = f"/projects/{identity['project_id']}/jobs/{identity['job_id']}/artifacts/{publisher.ARTIFACT}"
        document, _ = get_artifact(path)
        artifact = publisher.verify_receipt(contract, identity, document)

    proof = gitlab.verify(pipeline, get, observe_jobs=observe)
    return {"revision": proof["revision"], "gitlab_pipeline_id": identity["pipeline_id"], "gitlab_job_id": identity["job_id"]}, {
        "pipeline": proof, "publisher_contract_sha256": plan["publisher_contract_sha256"], "artifact": artifact}


def signature_annotations(plan, data, evidence):
    pipeline = plan["pipeline"]
    source = {name: pipeline[name] for name in ("api_url", "project_id", "project_path", "branch")}
    scope = {name: value for name, value in plan.items() if name != "signature_policy"}
    return signatures.validate_annotations({
        "platform.gitops/source-identity-sha256": common.sha256(source),
        "platform.gitops/source-revision": data["revision"],
        "platform.gitops/gitlab-pipeline-id": str(data["gitlab_pipeline_id"]),
        "platform.gitops/gitlab-job-id": str(data["gitlab_job_id"]),
        "platform.gitops/publisher-contract-sha256": evidence["publisher_contract_sha256"],
        "platform.gitops/ci-evidence-sha256": common.sha256(evidence),
        "platform.gitops/promotion-policy-sha256": common.sha256(scope),
        "platform.gitops/image-sha256": hashlib.sha256(evidence["artifact"]["image"].encode()).hexdigest(),
    })


def release_request(plan, contract, get, get_artifact):
    validate(plan, contract)
    data, evidence = verify(plan, contract, get, get_artifact)
    return {"schema_version": 1, "image": evidence["artifact"]["image"], "annotations": signature_annotations(plan, data, evidence)}


def execute(plan, contract, client, get, get_artifact, *, apply=False, record=None, inputs=source_handoff.INPUTS):
    handoff, source = validate(plan, contract)

    def signature_verifier(data, evidence):
        selected = {"schema_version": 2, "image": evidence["artifact"]["image"],
                    "annotations": signature_annotations(plan, data, evidence), **plan["signature_policy"]}
        return promotion.verify_release_signature(selected)

    return promotion.execute_verified(handoff, source, client, lambda: verify(plan, contract, get, get_artifact),
        apply=apply, record=record, git_inputs=inputs, inspect_cd=promotion.artifact_cd_object, signature_verifier=signature_verifier)


def readers(plan, contract, inputs):
    token = bridge.private_input("api-token", minimum=16, maximum=512, root=inputs).decode("ascii")
    if any(not 33 <= ord(c) <= 126 for c in token):
        raise common.HandoffError("invalid-private-gitlab-api-token")
    ca = bridge.private_input("ca.crt", maximum=65536, root=inputs, trim_line_endings=False)
    if hashlib.sha256(ca).hexdigest() != contract["source"]["ca_sha256"]:
        raise common.HandoffError("gitlab-artifact-source-trust-changed")
    tls = ssl.create_default_context(cadata=ca.decode("ascii"))
    return (gitlab.requester(plan["pipeline"], token, tls_context=tls),
            gitlab.requester(plan["pipeline"], token, tls_context=tls, max_bytes=publisher.LIMIT))


def operate(plan, contract, *, apply=False, check_server=False, prepare_signature=False, output=None, inputs=source_handoff.INPUTS):
    handoff, _ = validate(plan, contract)
    if prepare_signature:
        return release_request(plan, contract, *readers(plan, contract, inputs))
    if not (apply or check_server):
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or output is None:
        raise common.HandoffError("server-mode-requires-linux-and-new-private-proof-directory")
    target = handoff["cd"]["management_target"]
    record, state = common.new_journal(output, {"schema_version": 1, "plan_sha256": common.sha256(plan),
        "runtime": plan["runtime"], "controller_kind": handoff["cd"]["kind"], "controller_verified": False,
        "deployment_rollout_verified": False, "full_stack_verified": False})
    try:
        get, get_artifact = readers(plan, contract, inputs)
        with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
            return execute(plan, contract, Client(target), get, get_artifact, apply=apply, record=record, inputs=inputs)
    except Exception as error:
        code = str(error) if isinstance(error, common.HandoffError) else ""
        record(phase="failed", failed_at=state["phase"],
               error_code=code if re.fullmatch(r"[a-z0-9-]{1,100}", code) else "gitlab-artifact-promotion-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--publisher-contract", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, default=source_handoff.INPUTS)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-signature", action="store_true")
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan, contract = [loads_strict_json(read_bounded_bytes(path, max_bytes=128 * 1024))
                          for path in (args.plan, args.publisher_contract)]
        result = operate(plan, contract, prepare_signature=args.prepare_signature, apply=args.apply, check_server=args.check_server,
                         output=args.output, inputs=args.input_dir)
    except Exception:
        print("GitLab artifact promotion stopped. Check the private proof and deployment branch before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
