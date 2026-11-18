#!/usr/bin/env python3
"""Prepare a trusted, final post-review static-image job without publishing config."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
import render_zuul_config as zuul
from strict_json import loads_strict_json
import zuul_static_image as worker


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = "playbooks/platform-static-publish.yaml"
PACKAGE = "playbooks/platform-static-publisher"


def validate(plan, ci):
    worker.validate(plan)
    zuul.validate(ci)
    tenants = [tenant for tenant in ci["tenants"] if tenant["name"] == plan["tenant"]]
    if len(tenants) != 1 or not ci.get("executor", {}).get("enabled"):
        raise ValueError("Select the reviewed tenant and explicitly enabled executor")
    tenant = tenants[0]
    if plan["config_project"] not in tenant["config_projects"] or plan["project"] not in tenant["untrusted_projects"]:
        raise ValueError("Separate the existing trusted config and application projects")
    if any(plan["config_project"] in other["config_projects"] + other["untrusted_projects"]
           for other in ci["tenants"] if other["name"] != plan["tenant"]):
        raise ValueError("Keep publisher configuration dedicated to one tenant")
    url = urlsplit(plan["source"]["clone_url"])
    if url.hostname != ci["gerrit"]["host"] or (url.port or 443) != ci["gerrit"].get("https_port", 443):
        raise ValueError("The publisher source must use the reviewed Gerrit connection")
    return tenant


def playbook(contract_digest):
    event = "{{ {'build_uuid': zuul.build, 'project': zuul.project.name, 'pipeline': zuul.pipeline, 'branch': zuul.branch, 'revision': zuul.newrev} | to_json }}"
    payload = "{{ {'source_ca': platform_publication.source_ca, 'source_token': platform_publication.source_token, 'registry_ca': platform_publication.registry_ca, 'registry_credential': platform_publication.registry_credential} | to_json }}"
    arguments = [
        "/usr/bin/python3", "-E", "-s", "-B", "{{ playbook_dir }}/platform-static-publisher/programs/zuul_static_image.py",
        "--contract", "{{ playbook_dir }}/platform-static-publisher/contract.json", "--contract-sha256", contract_digest]
    scoped = [*arguments, "--event-json", event, "--receipt-work-root", "{{ zuul.executor.work_root }}"]
    codes = {value: key for key, value in worker.EXIT_STAGE.items()}
    failure = "Trusted publisher failed at {{ " + repr(codes) + ".get(platform_publication_result.rc | default(1), 'command-or-template') }}; private details suppressed."
    tasks = [{"name": "Verify trusted programs before reading publication inputs", "ansible.builtin.command": {
        "argv": [*arguments, "--check-contract"]}, "changed_when": False},
        {"name": "Build and publish only the reviewed static source", "ansible.builtin.command": {"argv": [
        *scoped, "--input-stdin"], "stdin": payload, "stdin_add_newline": False},
        "register": "platform_publication_result", "failed_when": False, "no_log": True},
        {"name": "Stop if trusted publication failed", "ansible.builtin.fail": {
            "msg": failure},
            "when": "platform_publication_result.rc | default(1) != 0"},
        {"name": "Read only the verified public receipt", "ansible.builtin.command": {"argv": [*scoped, "--read-receipt"]},
            "register": "platform_publication_receipt", "changed_when": False},
        {"name": "Return only the public digest receipt to Zuul", "zuul_return": {"data": {"zuul": {
            "artifacts": ["{{ platform_publication_receipt.stdout | from_json }}"]}}}, "no_log": True}]
    return yaml.safe_dump([{"name": "Publish a static artifact in the trusted executor context", "hosts": "localhost",
        "connection": "local", "gather_facts": False, "become": False, "tasks": tasks}], sort_keys=False)


def render(plan, ci, runtime):
    tenant = validate(plan, ci)
    if runtime not in RUNTIMES:
        raise ValueError("Select the actual Zuul hosting runtime")
    programs = {name: read_bounded_text(ROOT / "scripts" / name, max_bytes=256 * 1024) for name in worker.PROGRAMS}
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan,
                "programs_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in programs.items()}}
    digest = worker.fingerprint(contract)
    canonical = ci["gerrit"]["host"] + "/" + plan["project"]
    branch = "^" + re.escape(plan["branch"]) + "$"
    objects = [
        {"pipeline": {"name": plan["pipeline"], "manager": "independent", "post-review": True,
            "description": "Trusted reviewed-branch static image publication; no review votes or submission.",
            "trigger": {"gerrit": [{"event": "ref-updated", "ref": "^refs/heads/" + re.escape(plan["branch"]) + "$"}]}}},
        {"job": {"name": plan["job"], "parent": None, "final": True, "protected": True, "post-review": True,
            "allowed-projects": [canonical], "branches": branch, "timeout": 600, "attempts": 1, "nodeset": {"nodes": []},
            "secrets": [{"name": "platform_publication", "secret": plan["secret"], "pass-to-parent": False}],
            "run": PLAYBOOK}},
        {"project": {"name": canonical, plan["pipeline"]: {"jobs": [plan["job"]]}}},
    ]
    files = {"zuul.yaml": yaml.safe_dump(objects, sort_keys=False), PLAYBOOK: playbook(digest),
             PACKAGE + "/contract.json": worker.publisher.canonical(contract).decode() + "\n"}
    files.update({PACKAGE + "/programs/" + name: value for name, value in programs.items()})
    files["render-status.json"] = json.dumps({"schema_version": 1, "runtime": runtime, "rendered": True,
        "contract_sha256": digest, "trusted_config_branch": tenant["config_branch"],
        "files_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()},
        "published": False, "artifact_publication_verified": False, "application_ci_verified": False,
        "deployment_verified": False, "ha_verified": False, "full_stack_verified": False}, sort_keys=True) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--zuul-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan, ci = [loads_strict_json(read_bounded_text(path, max_bytes=256 * 1024)) for path in (args.plan, args.zuul_plan)]
        write_bundle(render(plan, ci, args.runtime), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, "published": False, "artifact_publication_verified": False}))
        return 0
    except Exception:
        print("Trusted static publication rendering stopped; private selections suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
