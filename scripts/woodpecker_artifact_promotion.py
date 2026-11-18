#!/usr/bin/env python3
"""Promote a signed Harbor image from a successful Woodpecker publisher step."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time

from bounded_file import read_bounded_bytes
import ci_gitops_handoff as common
import gerrit_artifact_promotion as promotion
from kubernetes_management import Client, KubernetesOperationError
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
import verify_image_signature as signatures
import woodpecker_gitops_handoff as source
import woodpecker_static_image as publisher


def validate(plan, contract):
    publisher.harbor.exact(plan, "schema_version runtime pipeline handoff artifact_policy publisher_contract_sha256 signature_policy")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-woodpecker-artifact-promotion-plan")
    publisher.validate(contract)
    if publisher.fingerprint(contract) != plan["publisher_contract_sha256"]:
        raise ValueError("woodpecker-publisher-contract-changed")
    git = source.validate({key: plan[key] for key in ("schema_version", "runtime", "pipeline", "handoff")})
    ci = plan["pipeline"]
    if (any(ci[key] != contract[key] for key in publisher.SCOPE)
            or contract["step"] not in ci["required_steps"].get(contract["workflow"], ())):
        raise ValueError("woodpecker-publisher-and-pipeline-scope-mismatch")
    deployment = {**plan["handoff"], "artifact_policy": plan["artifact_policy"]}
    promotion.validate_artifact_policy(deployment)
    if (deployment["allow_prune"] or deployment["allow_create_branch"]
            or plan["artifact_policy"]["expected_deployment_revision"] == "0" * 40):
        raise ValueError("woodpecker-artifact-promotion-requires-reviewed-existing-branch-without-prune")
    if contract["source"]["read_username"].casefold() == deployment["git_username"].casefold():
        raise ValueError("woodpecker-publisher-and-promoter-must-use-separate-git-identities")
    registry = contract["registry"]
    image = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@sha256:" + "0" * 64
    signatures.validate({"schema_version": 2, "image": image, "annotations": {"platform.gitops/validation": "pending"},
                         **promotion.signature_policy(plan)})
    return deployment, git


def read_step_receipt(rows, step_id):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
        raise ValueError("invalid-woodpecker-publisher-log")
    lines = []
    for row in rows:
        if not isinstance(row, dict) or not source.match_fields(row, {"step_id": step_id}) or type(row.get("type")) is not int:
            raise ValueError("woodpecker-log-step-identity-mismatch")
        if row["type"] != 0:  # Only stdout can contain the publisher's receipt.
            continue
        data = publisher.read_base64(row.get("data"), 65536)
        for line in data.splitlines():
            if line.startswith(publisher.PREFIX):
                if len(line) > publisher.RECEIPT_LIMIT:
                    raise ValueError("woodpecker-publication-receipt-exceeds-bound")
                lines.append(line[len(publisher.PREFIX):])
    if len(lines) != 1:
        raise ValueError("woodpecker-publication-receipt-missing-or-ambiguous")
    return lines[0]


def verify(plan, contract, receipt_key, get_ci, get_forge):
    ci = plan["pipeline"]
    url = publisher.clone_url(contract)
    proof = source.verify_pipeline(ci, url, get_ci, get_forge)
    workflow = proof["workflows"][contract["workflow"]]
    step_id = workflow["steps"][contract["step"]]["id"]
    log_path = f"/repos/{ci['woodpecker_repository_id']}/logs/{ci['number']}/{step_id}"
    # Pipeline status can be visible before Woodpecker persists the step's log.
    deadline = time.monotonic() + 5
    while True:
        rows = get_ci(log_path)
        if rows is not None and rows != []:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("woodpecker-publisher-log-not-persisted")
        time.sleep(min(remaining, 0.5))
    raw = read_step_receipt(rows, step_id)
    job = publisher.identity(contract, ci["number"], ci["revision"])
    artifact = publisher.verify_receipt(contract, job, loads_strict_json(raw), receipt_key)
    if source.verify_pipeline(ci, url, get_ci, get_forge) != proof:
        raise ValueError("woodpecker-pipeline-changed-during-artifact-read")
    return {"revision": proof["revision"], "woodpecker_pipeline_id": proof["pipeline_id"],
            "woodpecker_workflow_id": workflow["id"], "woodpecker_step_id": step_id}, {
                "pipeline": proof, "authenticated_receipt_sha256": hashlib.sha256(raw).hexdigest(),
                "publisher_contract_sha256": plan["publisher_contract_sha256"], "artifact": artifact}


def signature_annotations(plan, data, evidence):
    identity = {key: plan["pipeline"][key] for key in publisher.SCOPE}
    scope = {key: value for key, value in plan.items() if key != "signature_policy"}
    return signatures.validate_annotations({
        "platform.gitops/source-identity-sha256": common.sha256(identity),
        "platform.gitops/source-revision": data["revision"],
        "platform.gitops/woodpecker-pipeline-id": str(data["woodpecker_pipeline_id"]),
        "platform.gitops/woodpecker-workflow-id": str(data["woodpecker_workflow_id"]),
        "platform.gitops/woodpecker-step-id": str(data["woodpecker_step_id"]),
        "platform.gitops/ci-evidence-sha256": common.sha256(evidence),
        "platform.gitops/promotion-policy-sha256": common.sha256(scope),
        "platform.gitops/image-sha256": hashlib.sha256(evidence["artifact"]["image"].encode()).hexdigest(),
    })


def release_request(plan, contract, receipt_key, get_ci, get_forge):
    validate(plan, contract)
    data, evidence = verify(plan, contract, receipt_key, get_ci, get_forge)
    return {"schema_version": 1, "image": evidence["artifact"]["image"], "annotations": signature_annotations(plan, data, evidence)}


def execute(plan, contract, receipt_key, client, get_ci, get_forge, *, apply=False, record=None, inputs=source.INPUTS):
    deployment, git = validate(plan, contract)
    if type(apply) is not bool:
        raise ValueError("invalid-woodpecker-artifact-promotion-mode")

    def signed(data, evidence):
        selected = {"schema_version": 2, "image": evidence["artifact"]["image"],
                    "annotations": signature_annotations(plan, data, evidence), **plan["signature_policy"]}
        return promotion.verify_release_signature(selected)

    return promotion.execute_verified(deployment, git, client, lambda: verify(plan, contract, receipt_key, get_ci, get_forge),
                                      apply=apply, record=record, git_inputs=inputs,
                                      inspect_cd=promotion.artifact_cd_object, signature_verifier=signed)


def readers(contract, inputs):
    def scoped_tls(name, expected):
        ca = bridge.private_input(name, maximum=65536, root=inputs, trim_line_endings=False)
        if hashlib.sha256(ca).hexdigest() != expected:
            raise ValueError("woodpecker-or-forge-api-ca-changed")
        return ssl.create_default_context(cadata=ca.decode("ascii"))

    def token(name):
        value = bridge.private_input(name, minimum=16, maximum=512, root=inputs).decode("ascii")
        if any(not 33 <= ord(char) <= 126 for char in value):
            raise ValueError("invalid-woodpecker-private-token")
        return value

    ci = source.requester(contract["woodpecker_origin"] + "/api", "Bearer " + token("woodpecker-token"),
                          tls_context=scoped_tls("woodpecker-ca.crt", contract["woodpecker_ca_sha256"]))
    forge = source.requester(contract["forge_origin"] + "/api/v1", "token " + token("api-token"),
                             tls_context=scoped_tls("ca.crt", contract["source"]["ca_sha256"]))
    return ci, forge


def operate(plan, contract, *, apply=False, check_server=False, prepare_signature=False, output=None, inputs=source.INPUTS):
    deployment, _ = validate(plan, contract)
    if not (apply or check_server or prepare_signature):
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or (not prepare_signature and output is None):
        raise ValueError("woodpecker-live-mode-requires-linux-and-new-private-proof-directory")
    key = bridge.private_input("receipt-key", minimum=32, maximum=32, root=inputs, trim_line_endings=False)
    publisher.check_receipt_key(contract, key)
    get_ci, get_forge = readers(contract, inputs)
    if prepare_signature:
        return release_request(plan, contract, key, get_ci, get_forge)
    target = deployment["cd"]["management_target"]
    record, state = common.new_journal(output, {"schema_version": 1, "plan_sha256": common.sha256(plan),
        "runtime": deployment["runtime"], "controller_kind": "argocd", "controller_verified": False,
        "deployment_rollout_verified": False, "full_stack_verified": False})
    try:
        with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
            return execute(plan, contract, key, Client(target), get_ci, get_forge,
                           apply=apply, record=record, inputs=inputs)
    except Exception as error:
        code = str(error) if isinstance(error, common.HandoffError) else ""
        kind = type(error).__name__
        diagnostic = {"exception_type": kind if kind in {"ValueError", "HandoffError", "FileNotFoundError", "PermissionError",
            "KubernetesOperationError", "TimeoutError", "HTTPError", "URLError", "SSLError", "SSLCertVerificationError"} else "other"}
        if isinstance(error, KubernetesOperationError):
            diagnostic["kubernetes"] = error.diagnostic
        record(phase="failed", failed_at=state["phase"], failure_diagnostic=diagnostic,
               error_code=code if re.fullmatch(r"[a-z0-9-]{1,100}", code) else "woodpecker-artifact-promotion-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--publisher-contract", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, default=source.INPUTS)
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
                         output=args.output, inputs=args.input_dir)
    except Exception:
        print("Woodpecker artifact promotion stopped. Check the private evidence and deployment branch before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
