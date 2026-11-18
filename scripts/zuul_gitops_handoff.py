#!/usr/bin/env python3
"""Promote a reviewed Gerrit branch revision after its exact Zuul buildset."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from bounded_file import read_bounded_text
import ci_gitops_handoff as handoff
from http_transport import open_http_request, read_bounded_response
from render_gerrit_router import RUNTIMES, dns_name
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge


INPUTS = Path("/run/platform/promoter")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
UUID = re.compile(r"[0-9a-f]{32}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")


def origin(value):
    if not isinstance(value, str) or len(value) > 300:
        raise handoff.HandoffError("invalid-https-origin")
    url = urlsplit(value)
    port = url.port
    if (url.scheme != "https" or not dns_name(url.hostname) or port == 0
            or value != f"https://{url.hostname}" + (f":{port}" if port is not None else "")):
        raise handoff.HandoffError("invalid-https-origin")
    return value


def validate(plan):
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "runtime", "buildset", "handoff"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or plan["runtime"] not in RUNTIMES):
        raise handoff.HandoffError("invalid-zuul-handoff-plan")
    ci = plan["buildset"]
    fields = {"zuul_origin", "zuul_ca_sha256", "gerrit_origin", "tenant", "pipeline", "project", "branch",
              "uuid", "revision", "required_jobs"}
    if not isinstance(ci, dict) or set(ci) != fields:
        raise handoff.HandoffError("invalid-zuul-buildset-plan")
    origin(ci["zuul_origin"])
    origin(ci["gerrit_origin"])
    if (not isinstance(ci["zuul_ca_sha256"], str) or not handoff.DIGEST.fullmatch(ci["zuul_ca_sha256"])
            or not isinstance(ci["project"], str) or not 1 <= len(ci["project"]) <= 200
            or not all(NAME.fullmatch(part) and ".." not in part for part in ci["project"].split("/"))
            or not handoff.git_branch(ci["branch"])
            or not all(isinstance(ci[k], str) and NAME.fullmatch(ci[k]) for k in ("tenant", "pipeline"))
            or not isinstance(ci["uuid"], str) or not UUID.fullmatch(ci["uuid"])
            or not isinstance(ci["revision"], str) or not handoff.SHA.fullmatch(ci["revision"])
            or ci["revision"] == "0" * 40):
        raise handoff.HandoffError("invalid-zuul-buildset-identity")
    jobs = ci["required_jobs"]
    if (not isinstance(jobs, list) or not 1 <= len(jobs) <= 32
            or not all(isinstance(job, str) and NAME.fullmatch(job) for job in jobs)
            or len(set(jobs)) != len(jobs)):
        raise handoff.HandoffError("invalid-required-zuul-jobs")
    contract = {"runtime": plan["runtime"], "plan": {"source": {
        "clone_url": ci["gerrit_origin"] + "/a/" + ci["project"] + ".git", "branch": ci["branch"],
    }}}
    handoff.validate_policy(plan["handoff"], contract)
    if plan["handoff"]["bridge_contract_sha256"] != handoff.sha256(ci):
        raise handoff.HandoffError("zuul-buildset-plan-digest-mismatch")
    return contract


def timestamp(value):
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise handoff.HandoffError("invalid-zuul-build-time")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise handoff.HandoffError("invalid-zuul-build-time") from None


def verify_buildset(ci, get):
    return verify_buildset_row(ci, get(f"/api/tenant/{ci['tenant']}/buildset/{ci['uuid']}"))


def verify_buildset_row(ci, row):
    ref = {"project": ci["project"], "branch": ci["branch"], "ref": "refs/heads/" + ci["branch"],
           "newrev": ci["revision"], "change": None, "patchset": None}

    def matches(candidate):
        return isinstance(candidate, dict) and all(k in candidate and candidate[k] == value for k, value in ref.items())

    if (not isinstance(row, dict) or row.get("uuid") != ci["uuid"] or row.get("pipeline") != ci["pipeline"]
            or row.get("result") != "SUCCESS" or "user_id" not in row or row["user_id"] is not None
            or not isinstance(row.get("refs"), list) or len(row["refs"]) != 1 or not matches(row["refs"][0])):
        raise handoff.HandoffError("zuul-buildset-ref-or-result-mismatch")
    started, ended = timestamp(row.get("first_build_start_time")), timestamp(row.get("last_build_end_time"))
    if started > ended:
        raise handoff.HandoffError("invalid-zuul-build-time")
    builds = row.get("builds")
    if not isinstance(builds, list) or not 1 <= len(builds) <= 256 or any(not isinstance(build, dict) for build in builds):
        raise handoff.HandoffError("invalid-zuul-build-list")
    jobs = {}
    for name in ci["required_jobs"]:
        matching = [build for build in builds if build.get("job_name") == name and build.get("final") is True]
        if len(matching) != 1:
            raise handoff.HandoffError("required-zuul-job-missing-or-ambiguous")
        build = matching[0]
        if (not isinstance(build.get("uuid"), str) or not UUID.fullmatch(build["uuid"])
                or build.get("result") != "SUCCESS" or build.get("voting") is not True or not matches(build.get("ref"))
                or not started <= timestamp(build.get("start_time")) <= timestamp(build.get("end_time")) <= ended):
            raise handoff.HandoffError("required-zuul-job-unverified")
        jobs[name] = {key: build[key] for key in ("uuid", "start_time", "end_time")}
    if len({job["uuid"] for job in jobs.values()}) != len(jobs):
        raise handoff.HandoffError("duplicate-zuul-build-identity")
    return {"buildset_uuid": ci["uuid"], "revision": ci["revision"], "ended": row["last_build_end_time"], "jobs": jobs}


def execute(plan, cd_client, get, *, apply=False, record=None, git_inputs=INPUTS):
    contract = validate(plan)
    ci = plan["buildset"]
    bridge.verify_target(cd_client, plan["handoff"]["cd"]["management_target"])

    def verify_ci(_client, _contract, _template, _digest, _run):
        proof = verify_buildset(ci, get)
        return {"revision": proof["revision"], "uid": proof["buildset_uuid"]}, proof

    return handoff.execute(plan["handoff"], contract, None, None, cd_client, ci["uuid"], apply=apply,
                           record=record, verify_ci=verify_ci, git_inputs=git_inputs)


def requester(ci, inputs):
    token = bridge.private_input("zuul-token", minimum=16, maximum=8192, root=inputs).decode("ascii")
    if any(not 33 <= ord(character) <= 126 for character in token):
        raise handoff.HandoffError("invalid-private-zuul-token")
    ca = bridge.private_input("zuul-ca.crt", maximum=256 * 1024, root=inputs, trim_line_endings=False)
    if hashlib.sha256(ca).hexdigest() != ci["zuul_ca_sha256"]:
        raise handoff.HandoffError("zuul-trust-anchor-changed")
    context = ssl.create_default_context(cadata=ca.decode("ascii"))

    def get(path):
        request = Request(ci["zuul_origin"] + path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            with open_http_request(request, timeout=15, use_environment_proxy=False, tls_context=context) as response:
                if response.status != 200:
                    raise handoff.HandoffError("zuul-buildset-read-failed")
                return loads_strict_json(read_bounded_response(response, max_bytes=2 * 1024 ** 2))
        except HTTPError as error:
            error.close()
            raise handoff.HandoffError("zuul-buildset-read-failed") from None

    return get


def operate(plan, *, apply=False, check_server=False, output=None, inputs=INPUTS):
    validate(plan)
    if not (apply or check_server):
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False}
    if os.name != "posix" or output is None:
        raise handoff.HandoffError("server-mode-requires-linux-and-new-private-proof-directory")
    target = plan["handoff"]["cd"]["management_target"]
    record, state = handoff.new_journal(output, {"schema_version": 1, "plan_sha256": handoff.sha256(plan),
        "runtime": plan["runtime"], "controller_kind": plan["handoff"]["cd"]["kind"],
        "controller_verified": False, "full_stack_verified": False})
    try:
        get = requester(plan["buildset"], inputs)
        with bridge.frozen_target({"management_target": target}, inputs=inputs, kubeconfig_name="cd-kubeconfig"):
            return execute(plan, bridge.Client(target), get, apply=apply, record=record, git_inputs=inputs)
    except Exception as error:
        code = str(error) if isinstance(error, handoff.HandoffError) and re.fullmatch(r"[a-z0-9-]{1,100}", str(error)) else "zuul-handoff-failed"
        record(phase="failed", failed_at=state["phase"], error_code=code)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, default=INPUTS)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024))
        result = operate(plan, apply=args.apply, check_server=args.check_server, output=args.output, inputs=args.input_dir)
    except Exception:
        print("Zuul handoff stopped. Check the private proof and deployment branch before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
