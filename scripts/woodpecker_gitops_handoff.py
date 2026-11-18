#!/usr/bin/env python3
"""Promote an exact, verified Woodpecker push pipeline to Argo CD."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote
from urllib.request import Request

from bounded_file import read_bounded_text
import ci_gitops_handoff as handoff
from http_transport import open_http_request, read_bounded_response
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
from woodpecker_static_image import REPOSITORY, STEP, match_fields, origin, positive_id


INPUTS = Path("/run/platform/promoter")


def validate(plan):
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "runtime", "pipeline", "handoff"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or plan["runtime"] not in ("rke2", "k3s", "k8s", "docker", "raw")):
        raise ValueError("invalid-woodpecker-handoff-plan")
    ci = plan["pipeline"]
    fields = {"woodpecker_origin", "forge_origin", "provider", "forge_id", "woodpecker_repository_id",
              "forge_repository_id", "repository", "branch", "number", "revision", "required_steps"}
    if not isinstance(ci, dict) or set(ci) != fields:
        raise ValueError("invalid-woodpecker-pipeline-plan")
    origin(ci["woodpecker_origin"])
    origin(ci["forge_origin"])
    if (ci["provider"] not in ("gitea", "forgejo")
            or not all(positive_id(ci[key]) for key in ("forge_id", "woodpecker_repository_id", "forge_repository_id", "number"))
            or not isinstance(ci["repository"], str) or not REPOSITORY.fullmatch(ci["repository"])
            or not handoff.git_branch(ci["branch"])
            or not isinstance(ci["revision"], str) or not handoff.SHA.fullmatch(ci["revision"])
            or ci["revision"] == "0" * 40):
        raise ValueError("invalid-woodpecker-pipeline-identity")
    required = ci["required_steps"]
    if (not isinstance(required, dict) or not 1 <= len(required) <= 32
            or any(not isinstance(name, str) or not STEP.fullmatch(name)
                   or not isinstance(steps, list) or not 1 <= len(steps) <= 32
                   or any(not isinstance(step, str) or not STEP.fullmatch(step) for step in steps)
                   or len(set(steps)) != len(steps) for name, steps in required.items())):
        raise ValueError("invalid-required-workflow-steps")
    contract = {"runtime": plan["runtime"], "plan": {"source": {
        "clone_url": ci["forge_origin"] + "/" + ci["repository"] + ".git", "branch": ci["branch"],
    }}}
    policy = plan["handoff"]
    handoff.validate_policy(policy, contract)
    if policy["cd"]["kind"] != "argocd" or policy["bridge_contract_sha256"] != handoff.sha256(ci):
        raise ValueError("woodpecker-plan-digest-or-controller-mismatch")
    return contract


def requester(base, authorization, *, tls_context=None):
    def get(path):
        request = Request(base + path, headers={"Authorization": authorization, "Accept": "application/json"})
        with open_http_request(request, timeout=15, use_environment_proxy=False, tls_context=tls_context) as response:
            return loads_strict_json(read_bounded_response(response, max_bytes=1024 * 1024))
    return get


def select_named(rows, name):
    if not isinstance(rows, list) or len(rows) > 256 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid-woodpecker-workflow-or-step-list")
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) != 1:
        raise ValueError("woodpecker-workflow-or-step-missing-or-ambiguous")
    return matches[0]


def verify_pipeline(ci, clone_url, get_ci, get_forge):
    source = "/repos/" + ci["repository"]
    repository = get_forge(source)
    if not match_fields(repository, {"id": ci["forge_repository_id"], "full_name": ci["repository"], "clone_url": clone_url}):
        raise ValueError("forge-repository-identity-changed")
    branch = get_forge(source + "/branches/" + quote(ci["branch"], safe=""))
    if (not match_fields(branch, {"name": ci["branch"], "protected": True})
            or not match_fields(branch.get("commit"), {"id": ci["revision"]})):
        raise ValueError("source-branch-is-unprotected-or-head-changed")
    repo_path = "/repos/" + str(ci["woodpecker_repository_id"])
    repo = get_ci(repo_path)
    if not match_fields(repo, {"id": ci["woodpecker_repository_id"], "forge_id": ci["forge_id"],
                               "forge_remote_id": str(ci["forge_repository_id"]), "full_name": ci["repository"],
                               "clone_url": clone_url, "active": True}):
        raise ValueError("woodpecker-repository-identity-changed")
    forge = get_ci("/forges/" + str(ci["forge_id"]))
    if (not match_fields(forge, {"id": ci["forge_id"], "type": ci["provider"], "url": ci["forge_origin"]})
            or forge.get("skip_verify", False) is not False):
        raise ValueError("woodpecker-forge-identity-changed")
    pipeline = get_ci(repo_path + "/pipelines/" + str(ci["number"]))
    if (not match_fields(pipeline, {"number": ci["number"], "commit": ci["revision"], "branch": ci["branch"],
                                    "ref": "refs/heads/" + ci["branch"], "status": "success", "event": "push",
                                    "rerun_count": 0})
            or pipeline.get("from_fork", False) is not False
            or not all(positive_id(pipeline.get(field)) for field in ("id", "finished"))):
        raise ValueError("woodpecker-pipeline-identity-or-status-mismatch")
    errors = pipeline.get("errors")
    if errors is not None and (
        not isinstance(errors, list) or len(errors) > 256
        or any(not isinstance(error, dict) or error.get("is_warning") is not True for error in errors)
    ):
        raise ValueError("woodpecker-pipeline-reported-errors")
    verified = {}
    for name, names in ci["required_steps"].items():
        workflow = select_named(pipeline.get("workflows"), name)
        if workflow.get("state") != "success" or not all(positive_id(workflow.get(key)) for key in ("id", "finished")):
            raise ValueError("required-woodpecker-workflow-not-successful")
        steps = {}
        for step_name in names:
            step = select_named(workflow.get("children"), step_name)
            if (not match_fields(step, {"state": "success", "exit_code": 0})
                    or not all(positive_id(step.get(key)) for key in ("id", "finished")) or step.get("error")):
                raise ValueError("required-woodpecker-step-not-successful")
            steps[step_name] = {"id": step["id"], "finished": step["finished"]}
        verified[name] = {"id": workflow["id"], "finished": workflow["finished"], "steps": steps}
    return {"pipeline_id": pipeline["id"], "revision": ci["revision"], "finished": pipeline["finished"], "workflows": verified}


def execute(plan, client, get_ci, get_forge, *, apply=False, record=None, git_inputs=None):
    contract = validate(plan)
    ci = plan["pipeline"]

    def verify_ci(_client, _contract, _template, _digest, _run):
        proof = verify_pipeline(ci, contract["plan"]["source"]["clone_url"], get_ci, get_forge)
        return {"revision": proof["revision"], "uid": str(proof["pipeline_id"])}, proof

    return handoff.execute(plan["handoff"], contract, None, None, client, str(ci["number"]),
                           apply=apply, record=record, verify_ci=verify_ci, git_inputs=git_inputs)


def operate(plan_path, *, apply=False, check_server=False, output=None, inputs=INPUTS):
    plan = loads_strict_json(read_bounded_text(plan_path, max_bytes=128 * 1024))
    validate(plan)
    if not (apply or check_server):
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False}
    if os.name != "posix" or output is None:
        raise ValueError("server-mode-requires-linux-and-new-private-proof-directory")
    record, state = handoff.new_journal(output, {"schema_version": 1, "plan_sha256": handoff.sha256(plan),
        "runtime": plan["runtime"], "controller_kind": "argocd", "controller_verified": False, "full_stack_verified": False})
    try:
        tokens = {}
        for name in ("woodpecker-token", "api-token", "token"):
            value = bridge.private_input(name, minimum=16, maximum=512, root=inputs).decode("ascii")
            if any(not 33 <= ord(char) <= 126 for char in value):
                raise ValueError("invalid-private-token")
            tokens[name] = value
        target = plan["handoff"]["cd"]["management_target"]
        ci = plan["pipeline"]
        with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
            client = bridge.Client(target)
            bridge.verify_target(client, target)
            return execute(plan, client, requester(ci["woodpecker_origin"] + "/api", "Bearer " + tokens["woodpecker-token"]),
                           requester(ci["forge_origin"] + "/api/v1", "token " + tokens["api-token"]),
                           apply=apply, record=record, git_inputs=inputs)
    except Exception as error:
        code = str(error) if isinstance(error, handoff.HandoffError) else "woodpecker-handoff-validation-or-transport-failed"
        record(phase="failed", failed_at=state["phase"], error_code=code)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--input-dir", type=Path, default=INPUTS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = operate(args.plan, apply=args.apply, check_server=args.check_server, output=args.output_dir, inputs=args.input_dir)
    except Exception:
        print("Woodpecker GitOps handoff failed. Check the private proof and current branch before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
