#!/usr/bin/env python3
"""Promote the image from a reviewed Zuul build to one Argo CD or Flux Deployment."""

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
from kubernetes_management import Client
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
import zuul_gitops_handoff as zuul
import zuul_static_image as publisher


def validate(plan, contract):
    handoff_fields = {"schema_version", "runtime", "buildset", "handoff"}
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (1, 2):
        raise common.HandoffError("invalid-zuul-artifact-promotion-plan")
    fields = handoff_fields | {"publisher_contract_sha256", "artifact_policy"}
    if plan["schema_version"] == 2:
        fields.add("signature_policy")
    if set(plan) != fields:
        raise common.HandoffError("invalid-zuul-artifact-promotion-plan")
    source = zuul.validate({**{name: plan[name] for name in handoff_fields}, "schema_version": 1})
    if (not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "programs_sha256"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1
            or contract["runtime"] != plan["runtime"] or not publisher.publisher.hex64(plan["publisher_contract_sha256"])
            or publisher.fingerprint(contract) != plan["publisher_contract_sha256"]):
        raise common.HandoffError("zuul-publisher-contract-does-not-match-reviewed-pin")
    programs = contract["programs_sha256"]
    if (not isinstance(programs, dict) or set(programs) != set(publisher.PROGRAMS)
            or not all(publisher.publisher.hex64(value) for value in programs.values())):
        raise common.HandoffError("zuul-publisher-contract-program-inventory-invalid")
    publication, buildset = contract["plan"], plan["buildset"]
    publisher.validate(publication)
    if (any(publication[name] != buildset[name] for name in ("tenant", "pipeline", "project", "branch"))
            or publication["job"] not in buildset["required_jobs"]
            or publication["source"]["clone_url"] != source["plan"]["source"]["clone_url"]):
        raise common.HandoffError("zuul-publisher-and-buildset-scope-mismatch")
    handoff = {**plan["handoff"], "artifact_policy": plan["artifact_policy"]}
    if handoff["allow_prune"] or handoff["allow_create_branch"]:
        raise common.HandoffError("zuul-artifact-promotion-requires-an-existing-branch-without-prune")
    promotion.validate_artifact_policy(handoff)
    if handoff["artifact_policy"]["expected_deployment_revision"] == "0" * 40:
        raise common.HandoffError("pin-expected-deployment-branch-revision")
    if plan["schema_version"] == 2:
        import verify_image_signature as signatures
        registry = publication["registry"]
        prefix = registry["origin"].removeprefix("https://") + "/" + registry["repository"]
        signatures.validate({"schema_version": 2, "image": prefix + "@sha256:" + "0" * 64,
                             "annotations": {"platform.gitops/validation": "pending"}, **promotion.signature_policy(plan)})
    return handoff, source


def verify(plan, contract, get):
    """Use the final build and its artifacts from the same Zuul response."""
    selection = plan["buildset"]
    job_name = contract["plan"]["job"]
    row = get(f"/api/tenant/{selection['tenant']}/buildset/{selection['uuid']}")
    proof = zuul.verify_buildset_row(selection, row)
    identifier = proof["jobs"][job_name]["uuid"]
    builds = [build for build in row["builds"] if build.get("uuid") == identifier]
    if len(builds) != 1:
        raise common.HandoffError("zuul-publisher-build-identity-ambiguous")
    event = {"build_uuid": identifier, "pipeline": selection["pipeline"], "project": selection["project"],
             "branch": selection["branch"], "revision": proof["revision"]}
    try:
        receipt = publisher.verify_artifact(contract, event, builds[0].get("artifacts"))
    except ValueError:
        raise common.HandoffError("zuul-publisher-receipt-unverified") from None
    return {"revision": proof["revision"], "zuul_build_uuid": identifier}, {
        "buildset": proof, "publisher_contract_sha256": plan["publisher_contract_sha256"], "artifact": receipt}


inspect_cd = promotion.artifact_cd_object


def signature_annotations(plan, contract, data, evidence):
    """Bind the release key's approval to the publisher, CI result and deployment policy."""
    import verify_image_signature as signatures

    publication = contract["plan"]
    source = {"project": publication["project"], "branch": publication["branch"],
              "clone_url": publication["source"]["clone_url"]}
    scope = {name: value for name, value in plan.items() if name != "signature_policy"}
    return signatures.validate_annotations({
        "platform.gitops/source-identity-sha256": common.sha256(source),
        "platform.gitops/source-revision": data["revision"],
        "platform.gitops/zuul-buildset-uuid": plan["buildset"]["uuid"],
        "platform.gitops/zuul-build-uuid": data["zuul_build_uuid"],
        "platform.gitops/publisher-contract-sha256": evidence["publisher_contract_sha256"],
        "platform.gitops/ci-evidence-sha256": common.sha256(evidence),
        "platform.gitops/promotion-policy-sha256": common.sha256(scope),
        "platform.gitops/image-sha256": hashlib.sha256(evidence["artifact"]["image"].encode()).hexdigest(),
    })


def release_request(plan, contract, *, inputs=zuul.INPUTS, get=None):
    validate(plan, contract)
    if plan["schema_version"] != 2:
        raise common.HandoffError("zuul-release-request-requires-signed-promotion-plan")
    if get is None:
        get = zuul.requester(plan["buildset"], inputs)
    data, evidence = verify(plan, contract, get)
    return {"schema_version": 1, "image": evidence["artifact"]["image"],
            "annotations": signature_annotations(plan, contract, data, evidence)}


def execute(plan, contract, client, get, *, apply=False, record=None, inputs=zuul.INPUTS):
    handoff, source = validate(plan, contract)
    signature_verifier = None
    if plan["schema_version"] == 2:
        def signature_verifier(data, evidence):
            selected = {"schema_version": 2, "image": evidence["artifact"]["image"],
                        "annotations": signature_annotations(plan, contract, data, evidence), **plan["signature_policy"]}
            return promotion.verify_release_signature(selected)
    return promotion.execute_verified(handoff, source, client, lambda: verify(plan, contract, get),
                                      apply=apply, record=record, git_inputs=inputs, inspect_cd=inspect_cd,
                                      signature_verifier=signature_verifier)


def operate(plan, contract, *, apply=False, check_server=False, output=None, inputs=zuul.INPUTS):
    handoff, _ = validate(plan, contract)
    if not (apply or check_server):
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False}
    if os.name != "posix" or output is None:
        raise common.HandoffError("server-mode-requires-linux-and-new-private-proof-directory")
    target = handoff["cd"]["management_target"]
    record, state = common.new_journal(output, {"schema_version": 1, "plan_sha256": common.sha256(plan),
        "runtime": plan["runtime"], "controller_kind": handoff["cd"]["kind"],
        "controller_verified": False, "deployment_rollout_verified": False, "full_stack_verified": False})
    try:
        get = zuul.requester(plan["buildset"], inputs)
        with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
            return execute(plan, contract, Client(target), get, apply=apply, record=record, inputs=inputs)
    except Exception as error:
        code = str(error) if isinstance(error, common.HandoffError) else ""
        record(phase="failed", failed_at=state["phase"],
               error_code=code if re.fullmatch(r"[a-z0-9-]{1,100}", code) else "zuul-artifact-promotion-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--publisher-contract", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, default=zuul.INPUTS)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--prepare-signature", action="store_true", help="Read Zuul and print the release signing request")
    args = parser.parse_args(argv)
    try:
        plan, contract = [loads_strict_json(read_bounded_bytes(path, max_bytes=128 * 1024))
                          for path in (args.plan, args.publisher_contract)]
        if args.prepare_signature:
            result = release_request(plan, contract, inputs=args.input_dir)
        else:
            result = operate(plan, contract, apply=args.apply, check_server=args.check_server,
                             output=args.output, inputs=args.input_dir)
    except Exception:
        print("Zuul artifact promotion stopped. Check the private proof and deployment branch before retrying.",
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
