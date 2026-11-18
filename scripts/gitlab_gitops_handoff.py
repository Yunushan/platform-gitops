#!/usr/bin/env python3
"""Explicit GitLab pipeline to reviewed GitOps branch handoff."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote, urlencode

from bounded_file import read_bounded_text
from strict_json import loads_strict_json
import ci_gitops_handoff as handoff
import tekton_forge_bridge as bridge
import verify_gitlab_pipeline as gitlab

INPUTS = Path("/run/platform/promoter")
RUNTIMES = {"rke2", "k3s", "k8s", "docker", "raw"}


def materialize(plan, pipeline_id=None, revision=None, *, local_only=False):
    """Bind a reviewed reusable source policy to one observed pipeline and commit."""
    if not isinstance(plan, dict):
        raise ValueError("invalid-plan")
    if type(plan.get("schema_version")) is int and plan["schema_version"] == 1:
        if pipeline_id is not None or revision is not None:
            raise ValueError("dynamic-identity-not-supported-for-fixed-plan")
        return plan
    if (type(plan.get("schema_version")) is not int or plan.get("schema_version") != 2
            or set(plan) != {"schema_version", "runtime", "pipeline_policy", "handoff"}):
        raise ValueError("invalid-plan")
    policy = plan["pipeline_policy"]
    required = {"schema_version", "api_url", "project_id", "project_path", "branch", "required_jobs"}
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError("invalid-pipeline-policy")
    deployment = plan["handoff"]
    if not isinstance(deployment, dict) or deployment.get("bridge_contract_sha256") != handoff.sha256(policy):
        raise ValueError("pipeline-policy-digest-mismatch")
    if local_only and pipeline_id is None and revision is None:
        pipeline_id, revision = 1, "1" * 40
    if pipeline_id is None or revision is None:
        raise ValueError("pipeline-id-and-revision-required")
    pipeline = {**copy.deepcopy(policy), "pipeline_id": pipeline_id, "revision": revision}
    gitlab.validate(pipeline)
    deployment = copy.deepcopy(deployment)
    deployment["bridge_contract_sha256"] = handoff.sha256(pipeline)
    result = {"schema_version": 1, "runtime": plan["runtime"], "pipeline": pipeline, "handoff": deployment}
    validate(result)
    return result


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "runtime", "pipeline", "handoff"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-plan")
    if plan["runtime"] not in RUNTIMES:
        raise ValueError("invalid-runtime")
    pipeline = gitlab.validate(plan["pipeline"])
    policy = plan["handoff"]
    # Git access and the API are deliberately bound to one HTTPS origin and
    # project path; a caller cannot supply an unrelated deployment repository.
    clone_url = pipeline["api_url"][:-len("/api/v4")] + "/" + pipeline["project_path"] + ".git"
    contract = {"runtime": plan["runtime"], "plan": {"source": {"clone_url": clone_url, "branch": pipeline["branch"]}}}
    handoff.validate_policy(policy, contract)
    if policy["bridge_contract_sha256"] != handoff.sha256(pipeline):
        raise ValueError("pipeline-plan-digest-mismatch")
    return contract


def discover_latest(policy, get):
    """Read the current protected head and GitLab's latest pipeline for it."""
    if not isinstance(policy, dict):
        raise ValueError("invalid-pipeline-policy")
    candidate = {**policy, "pipeline_id": 1, "revision": "1" * 40}
    gitlab.validate(candidate)
    base = "/projects/" + str(policy["project_id"])
    project, _ = get(base)
    if (not isinstance(project, dict) or type(project.get("id")) is not int
            or project.get("id") != policy["project_id"] or project.get("path_with_namespace") != policy["project_path"]):
        raise ValueError("project-identity-mismatch")
    branch, _ = get(base + "/repository/branches/" + quote(policy["branch"], safe=""))
    if (not isinstance(branch, dict) or branch.get("name") != policy["branch"] or branch.get("protected") is not True
            or not isinstance(branch.get("commit"), dict) or not isinstance(branch["commit"].get("id"), str)
            or not gitlab.SHA.fullmatch(branch["commit"]["id"])):
        raise ValueError("branch-not-protected-or-invalid")
    revision = branch["commit"]["id"]
    pipeline, _ = get(base + "/pipelines/latest?" + urlencode({"ref": policy["branch"]}))
    if (not isinstance(pipeline, dict) or type(pipeline.get("id")) is not int or pipeline["id"] <= 0
            or type(pipeline.get("project_id")) is not int or pipeline.get("project_id") != policy["project_id"]
            or pipeline.get("sha") != revision or pipeline.get("ref") != policy["branch"] or pipeline.get("source") != "push"):
        raise ValueError("latest-pipeline-identity-mismatch")
    return pipeline["id"], revision


def execute(plan, cd_client, get, *, apply=False, record=None, git_inputs=None, require_latest=False):
    contract = validate(plan)
    def verify_ci(_client, _contract, _template, _digest, _run):
        if require_latest:
            policy = {k: v for k, v in plan["pipeline"].items() if k not in {"pipeline_id", "revision"}}
            if discover_latest(policy, get) != (plan["pipeline"]["pipeline_id"], plan["pipeline"]["revision"]):
                raise ValueError("latest-pipeline-changed")
        evidence = gitlab.verify(plan["pipeline"], get)
        return {"revision": evidence["revision"], "uid": str(evidence["pipeline_id"])}, evidence
    return handoff.execute(plan["handoff"], contract, None, None, cd_client, str(plan["pipeline"]["pipeline_id"]),
                           apply=apply, record=record, verify_ci=verify_ci, git_inputs=git_inputs)


def wait_pipeline(pipeline, get, timeout, *, clock=time.monotonic, sleep=time.sleep):
    """Wait for this exact push pipeline; never treat an unknown state as success."""
    gitlab.validate(pipeline)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("invalid-pipeline-wait")
    path = "/projects/" + str(pipeline["project_id"]) + "/pipelines/" + str(pipeline["pipeline_id"])
    deadline = clock() + timeout
    while True:
        if clock() >= deadline:
            raise ValueError("pipeline-wait-timeout")
        item, _ = get(path)
        if (not isinstance(item, dict) or type(item.get("id")) is not int or item.get("id") != pipeline["pipeline_id"]
                or type(item.get("project_id")) is not int or item.get("project_id") != pipeline["project_id"]
                or item.get("sha") != pipeline["revision"] or item.get("ref") != pipeline["branch"]
                or item.get("source") != "push"):
            raise ValueError("pipeline-identity-mismatch")
        if clock() >= deadline:
            raise ValueError("pipeline-wait-timeout")
        status = item.get("status")
        if status == "success":
            return
        if status not in {"created", "waiting_for_resource", "waiting_for_callback", "preparing", "pending", "running", "scheduled"}:
            raise ValueError("pipeline-not-successful")
        sleep(min(5, max(0, deadline - clock())))


def operate(plan_path, *, apply=False, check_server=False, output=None, pipeline_id=None, revision=None, wait_seconds=0, latest=False):
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 3600:
        raise ValueError("invalid-pipeline-wait")
    if wait_seconds and not (apply or check_server):
        raise ValueError("pipeline-wait-requires-server-mode")
    reviewed_plan = loads_strict_json(read_bounded_text(plan_path, max_bytes=128 * 1024))
    if latest and (not (apply or check_server) or pipeline_id is not None or revision is not None or reviewed_plan.get("schema_version") != 2):
        raise ValueError("latest-requires-server-mode-and-reusable-policy")
    plan = materialize(reviewed_plan, pipeline_id, revision, local_only=latest or not apply and not check_server)
    validate(plan)
    if not apply and not check_server:
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or output is None:
        raise ValueError("server-mode-requires-linux-and-new-private-proof-directory")
    record, state = handoff.new_journal(output, {"schema_version": 1, "plan_sha256": handoff.sha256(reviewed_plan),
        "bound_plan_sha256": None if latest else handoff.sha256(plan),
        "runtime": plan["runtime"], "controller_kind": plan["handoff"]["cd"]["kind"],
        "controller_verified": False, "full_stack_verified": False})
    try:
        token = bridge.private_input("api-token", minimum=16, maximum=512, root=INPUTS).decode("ascii")
        if any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("invalid-token")
        bridge.private_input("token", minimum=16, maximum=512, root=INPUTS)
        target = plan["handoff"]["cd"]["management_target"]
        with bridge.frozen_target({"management_target": target}, inputs=INPUTS, kubeconfig_name="cd-kubeconfig"):
            client = bridge.Client(target)
            bridge.verify_target(client, target)
            get = gitlab.requester(plan["pipeline"], token)
            if latest:
                selected_id, selected_revision = discover_latest(reviewed_plan["pipeline_policy"], get)
                plan = materialize(reviewed_plan, selected_id, selected_revision)
                record(phase="pipeline-discovered", pipeline_id=selected_id, source_revision=selected_revision,
                       bound_plan_sha256=handoff.sha256(plan))
            if wait_seconds:
                record(phase="waiting-for-pipeline", pipeline_id=plan["pipeline"]["pipeline_id"])
                wait_pipeline(plan["pipeline"], get, wait_seconds)
            return execute(plan, client, get, apply=apply, record=record, git_inputs=INPUTS, require_latest=latest)
    except Exception:
        record(phase="failed", failed_at=state["phase"], error_code="gitlab-handoff-failed")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pipeline-id", type=int, help="Pipeline ID for a reusable version 2 policy")
    parser.add_argument("--revision", help="Exact 40-character source commit for a reusable version 2 policy")
    parser.add_argument("--wait-seconds", type=int, default=0, help="Wait up to 3600 seconds for the selected pipeline to succeed")
    parser.add_argument("--latest", action="store_true", help="Discover the current protected-branch pipeline using a reusable policy")
    args = parser.parse_args()
    try:
        result = operate(args.plan, apply=args.apply, check_server=args.check_server, output=args.output,
                         pipeline_id=args.pipeline_id, revision=args.revision, wait_seconds=args.wait_seconds, latest=args.latest)
    except Exception:
        print("GitLab GitOps handoff failed; check the private proof and live state before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
