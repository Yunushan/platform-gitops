#!/usr/bin/env python3
"""Promote a release-signed image from a verified Forgejo or Gitea Tekton run."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from bounded_file import read_bounded_bytes
import ci_gitops_handoff as common
import gerrit_artifact_promotion as promotion
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
import verify_image_signature as signatures


def validate(plan, contract):
    signatures.exact(plan, "schema_version handoff artifact_policy signature_policy pipeline_run source_revision")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or contract["plan"]["schema_version"] != 2:
        raise common.HandoffError("require-reviewed-forge-artifact-run")
    run, revision = plan["pipeline_run"], plan["source_revision"]
    if not isinstance(run, str) or not bridge.NAME.fullmatch(run) or len(run) > 63:
        raise common.HandoffError("invalid-pipeline-run-name")
    if not isinstance(revision, str) or not common.SHA.fullmatch(revision) or revision == "0" * 40:
        raise common.HandoffError("pin-reviewed-source-revision")
    handoff = plan["handoff"]
    common.validate_policy(handoff, contract)
    deployment = {**handoff, "artifact_policy": plan["artifact_policy"]}
    promotion.validate_artifact_policy(deployment)
    if (handoff["cd"]["kind"] != "argocd" or handoff["allow_prune"] or handoff["allow_create_branch"]
            or plan["artifact_policy"]["expected_deployment_revision"] == "0" * 40):
        raise common.HandoffError("require-reviewed-existing-argocd-branch-without-prune")
    original = contract["plan"]
    if handoff["git_username"].casefold() == original["source"]["read_username"].casefold():
        raise common.HandoffError("use-independent-source-reader-and-promotion-writer")
    registry = original["publication"]["registry"]
    image = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@sha256:" + "0" * 64
    signatures.validate({"schema_version": 2, "image": image, "annotations": {"platform.gitops/validation": "pending"},
                         **promotion.signature_policy(plan)})
    return deployment


def verify(plan, contract, template, client, requester):
    handoff = plan["handoff"]
    data, evidence = common.verified_ci(client, contract, template, handoff["bridge_contract_sha256"], plan["pipeline_run"],
                                        requester=requester)
    if data["revision"] != plan["source_revision"] or "artifact" not in evidence:
        raise common.HandoffError("reviewed-tekton-artifact-evidence-missing-or-changed")
    selected = bridge.quote(handoff["deployment_branch"], safe="")
    original = contract["plan"]
    branch, _ = requester(original, "/repos/" + original["source"]["repository"] + "/branches/" + selected)
    if branch.get("name") != handoff["deployment_branch"] or branch.get("protected") is not True:
        raise common.HandoffError("artifact-deployment-branch-is-unprotected")
    return {**data, "task_uid": evidence["task_uid"]}, evidence


def release_request(plan, contract, data, evidence):
    original = contract["plan"]
    source = original["source"]
    identity = {**{key: source[key] for key in ("repository_id", "repository", "clone_url", "branch")},
                "provider": original["provider"]}
    scope = {key: value for key, value in plan.items() if key != "signature_policy"}
    image = evidence["artifact"]["image"]
    annotations = signatures.validate_annotations({
        "platform.gitops/source-identity-sha256": common.sha256(identity),
        "platform.gitops/source-revision": data["revision"],
        "platform.gitops/pipeline-run-uid": data["uid"],
        "platform.gitops/task-run-uid": data["task_uid"],
        "platform.gitops/ci-evidence-sha256": common.sha256(evidence),
        "platform.gitops/bridge-contract-sha256": plan["handoff"]["bridge_contract_sha256"],
        "platform.gitops/promotion-policy-sha256": common.sha256(scope),
        "platform.gitops/image-sha256": hashlib.sha256(image.encode()).hexdigest(),
    })
    return {"schema_version": 1, "image": image, "annotations": annotations}


def execute(plan, contract, template, ci, cd, requester, *, apply=False, record=None, inputs=common.INPUTS):
    deployment = validate(plan, contract)

    def signed(data, evidence):
        request = release_request(plan, contract, data, evidence)
        return promotion.verify_release_signature({**request, "schema_version": 2, **plan["signature_policy"]})

    return promotion.execute_verified(deployment, contract, cd, lambda: verify(plan, contract, template, ci, requester),
        apply=apply, record=record, git_inputs=inputs, inspect_cd=promotion.artifact_cd_object, signature_verifier=signed)


def operate(plan, bundle, *, mode=None, output=None, inputs=common.INPUTS):
    digest = plan.get("handoff", {}).get("bridge_contract_sha256", "")
    contract, template = bridge.load_bundle(bundle, digest)
    bridge.verify_running_sources(contract, Path(common.__file__))
    deployment = validate(plan, contract)
    if mode is None:
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or mode != "prepare_signature" and output is None:
        raise common.HandoffError("live-mode-requires-linux-and-new-private-proof-directory")
    original = contract["plan"]

    def requester(selected, path):
        return common.read_api(selected, path, inputs=inputs)

    with bridge.frozen_target(original, inputs=inputs, kubeconfig_name="ci-kubeconfig"):
        ci = bridge.Client(original["management_target"])
        bridge.verify_target(ci, original["management_target"])
        if mode == "prepare_signature":
            data, evidence = verify(plan, contract, template, ci, requester)
            return release_request(plan, contract, data, evidence)
        record, state = common.new_journal(output, {"schema_version": 1, "plan_sha256": common.sha256(plan),
            "bridge_contract_sha256": digest, "runtime": deployment["runtime"], "controller_kind": "argocd",
            "controller_verified": False, "deployment_rollout_verified": False, "full_stack_verified": False})
        try:
            target = deployment["cd"]["management_target"]
            with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
                return execute(plan, contract, template, ci, bridge.Client(target), requester,
                               apply=mode == "apply", record=record, inputs=inputs)
        except Exception as error:
            code = str(error) if isinstance(error, common.HandoffError) else ""
            record(phase="failed", failed_at=state["phase"],
                   error_code=code if re.fullmatch(r"[a-z0-9-]{1,100}", code) else "tekton-artifact-promotion-failed")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, default=common.INPUTS)
    parser.add_argument("--output", type=Path)
    actions = parser.add_mutually_exclusive_group()
    for action in ("prepare_signature", "check_server", "apply"):
        actions.add_argument("--" + action.replace("_", "-"), dest="mode", action="store_const", const=action)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        result = operate(plan, args.bundle_dir, mode=args.mode, output=args.output, inputs=args.input_dir)
    except Exception:
        print("Tekton artifact promotion stopped. Check the reviewed inputs and private evidence before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
