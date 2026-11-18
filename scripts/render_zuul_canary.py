#!/usr/bin/env python3
"""Render a private Gerrit/Zuul worker canary; never publish or trigger a job."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
import render_nodepool_static as nodepool
import render_zuul_config as zuul
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = "platform-canary-check"
JOB = "platform-canary-worker"
SEMAPHORE = "platform-canary-slot"
PLAYBOOK = "platform-canary.yaml"
# No source-controlled script is executed on the worker. The sole dynamic
# argument is a Zuul build UUID, passed separately from this fixed Python code.
PROBE = """import json, os, sys, uuid
build = uuid.UUID(sys.argv[1]).hex
if build != sys.argv[1] or os.geteuid() == 0:
    raise SystemExit("Canary requires a canonical build UUID and an unprivileged worker")
print(json.dumps({"schema_version": 1, "component": "platform-zuul-canary",
                  "build": build, "unprivileged": True}, sort_keys=True))
"""


def validate(plan: object, zuul_plan: dict, nodepool_plan: dict) -> dict:
    nodepool.validate(nodepool_plan, zuul_plan)
    fields = {"schema_version", "tenant", "config_project", "canary_project", "canary_branch", "worker_label"}
    if not isinstance(plan, dict) or set(plan) != fields:
        raise ValueError("Missing or unsupported canary plan fields")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Unsupported canary plan schema")
    if any(not isinstance(plan[field], str) for field in fields - {"schema_version"}):
        raise ValueError("Canary selections must be explicit strings")
    tenants = {tenant["name"]: tenant for tenant in zuul_plan["tenants"]}
    if plan["tenant"] not in tenants:
        raise ValueError("Canary tenant is absent from the reviewed Zuul plan")
    tenant = tenants[plan["tenant"]]
    if (plan["config_project"] not in tenant["config_projects"]
            or plan["canary_project"] not in tenant["untrusted_projects"]):
        raise ValueError("Select an existing trusted config project and an existing untrusted canary project")
    # Loading these pipeline/base-job definitions in an unrelated tenant can
    # introduce references to projects that tenant does not even contain.
    if any(plan["config_project"] in other["config_projects"] + other["untrusted_projects"]
           for other in zuul_plan["tenants"] if other["name"] != plan["tenant"]):
        raise ValueError("Canary configuration project must be dedicated to the selected tenant")
    branch = plan["canary_branch"]
    if not zuul.PROJECT.fullmatch(branch) or ".." in branch or branch.endswith((".", ".lock")):
        raise ValueError("Declare an explicit simple canary branch")
    if not any(pool["tenant"] == plan["tenant"] and pool["label"] == plan["worker_label"]
               for pool in nodepool_plan["pools"]):
        raise ValueError("Canary worker label must be supplied for the selected tenant")
    if not zuul_plan.get("executor", {}).get("enabled", False):
        raise ValueError("Explicitly enable the reviewed Zuul executor package before preparing a canary")
    return plan


def job_configuration(plan: dict, zuul_plan: dict, nodepool_plan: dict) -> str:
    validate(plan, zuul_plan, nodepool_plan)
    canonical_project = zuul_plan["gerrit"]["host"] + "/" + plan["canary_project"]
    branch = "^" + re.escape(plan["canary_branch"]) + "$"
    reporter = {"gerrit": {"comment": True, "notify": "NONE"}}
    objects = [
        {"pipeline": {
            "name": PIPELINE, "manager": "independent", "precedence": "low", "post-review": False,
            "description": "Infrastructure canary only: worker allocation, remote Python and review comments.",
            "require": {"gerrit": {"open": True, "current-patchset": True}},
            "trigger": {"gerrit": [
                {"event": "patchset-created", "branch": branch},
                {"event": "change-restored", "branch": branch},
                {"event": "comment-added", "branch": branch, "comment": "(?m)^platform-canary recheck[ \\t]*$"},
            ]},
            "start-message": "Starting infrastructure canary; application code is not tested.",
            "success-message": "Infrastructure canary passed; application code is not tested. No review vote or submission.",
            "failure-message": "Infrastructure canary failed; application code is not tested. No review vote or submission.",
            # Keep every reporter independent to avoid YAML alias surprises.
            "start": reporter,
            "success": {"gerrit": dict(reporter["gerrit"])},
            "failure": {"gerrit": dict(reporter["gerrit"])},
        }},
        {"semaphore": {"name": SEMAPHORE, "max": 1}},
        {"job": {
            "name": JOB, "parent": None, "final": True, "protected": True,
            "description": "Infrastructure probe only; does not execute or validate application code.",
            "allowed-projects": [canonical_project], "branches": branch,
            "timeout": 180, "attempts": 1, "semaphores": [SEMAPHORE],
            "nodeset": {"nodes": [{"name": "canary", "label": plan["worker_label"]}]},
            "run": PLAYBOOK,
        }},
        {"project": {"name": canonical_project, PIPELINE: {"jobs": [JOB]}}},
    ]
    return yaml.safe_dump(objects, sort_keys=False)


def playbook() -> str:
    return yaml.safe_dump([{
        "name": "Exercise an allocated canary worker without running application code",
        "hosts": "canary", "gather_facts": False, "become": False,
        "tasks": [
            {"name": "Wait for the allocated worker connection", "ansible.builtin.wait_for_connection": {"timeout": 60}},
            {"name": "Run the fixed unprivileged infrastructure probe",
             "ansible.builtin.command": {"argv": ["/usr/bin/python3", "-c", PROBE, "{{ zuul.build }}"]},
             "changed_when": False, "register": "platform_canary_probe"},
            {"name": "Require the result to belong to this build",
             "ansible.builtin.assert": {"that": [
                 "(platform_canary_probe.stdout | from_json).schema_version == 1",
                 "(platform_canary_probe.stdout | from_json).component == 'platform-zuul-canary'",
                 "(platform_canary_probe.stdout | from_json).build == zuul.build",
                 "(platform_canary_probe.stdout | from_json).unprivileged == true",
             ]}},
        ],
    }], sort_keys=False)


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render(plan: dict, zuul_plan: dict, nodepool_plan: dict, runtime: str) -> dict[str, str]:
    validate(plan, zuul_plan, nodepool_plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown canary target runtime")
    tenant = next(tenant for tenant in zuul_plan["tenants"] if tenant["name"] == plan["tenant"])
    files = {"zuul.yaml": job_configuration(plan, zuul_plan, nodepool_plan), PLAYBOOK: playbook()}
    # Plan and content digests identify the candidate, not a successful run.
    status = {"schema_version": 1, "component": "zuul-worker-canary", "runtime": runtime,
        "rendered": True, "published": False, "triggered": False, "live_verified": False,
        "application_ci_verified": False, "ha_verified": False, "automatic_submission": False,
        "review_votes": False, "pipeline": PIPELINE, "job": JOB,
        "tenant": plan["tenant"], "config_project": plan["config_project"],
        "config_branch": tenant["config_branch"], "canary_project": plan["canary_project"],
        "canary_branch": plan["canary_branch"], "worker_label": plan["worker_label"],
        "plan_digests": {"canary": digest(plan), "zuul": digest(zuul_plan), "nodepool": digest(nodepool_plan)},
        "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()},
        "remaining": ["selected-release-configuration-validation", "reviewed-config-project-publication",
                      "real-event-worker-allocation-and-comment-correlation", "worker-log-retention-and-reset",
                      "application-ci-and-reviewed-gating-policy", "event-replay-node-loss-and-restore-acceptance"]}
    files["render-status.json"] = json.dumps(status, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--zuul-plan", type=Path, required=True)
    parser.add_argument("--nodepool-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plans = [loads_strict_json(read_bounded_text(path, encoding="utf-8"))
                 for path in (args.plan, args.zuul_plan, args.nodepool_plan)]
        files = render(*plans, args.runtime)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Canary render failed; inspect private selections, runtime prerequisites and new output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "published": False, "triggered": False, "live_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
