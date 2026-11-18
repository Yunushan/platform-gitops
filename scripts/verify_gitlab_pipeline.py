#!/usr/bin/env python3
"""Read-only GitLab exact-head pipeline verification; never promotes or deploys."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request

from bounded_file import read_bounded_text
from http_transport import open_http_request, read_bounded_response
from render_gerrit_router import dns_name
from strict_json import loads_strict_json
from tekton_forge_bridge import private_input

SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,199}")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)+")


def validate(plan):
    fields = {"schema_version", "api_url", "project_id", "project_path", "branch", "pipeline_id", "revision", "required_jobs"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-plan")
    url = plan["api_url"]
    if not isinstance(url, str) or any(ch.isspace() or ord(ch) < 32 for ch in url): raise ValueError("invalid-api-url")
    parsed = urlsplit(url)
    try: port = parsed.port
    except ValueError: raise ValueError("invalid-api-url") from None
    if (parsed.scheme != "https" or not dns_name(parsed.hostname) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path != "/api/v4" or port is not None and not 1 <= port <= 65535):
        raise ValueError("invalid-api-url")
    for name in ("project_id", "pipeline_id"):
        if type(plan[name]) is not int or not 1 <= plan[name] <= 2**63 - 1: raise ValueError("invalid-identity")
    branch = plan["branch"]
    if (not isinstance(branch, str) or not BRANCH.fullmatch(branch) or ".." in branch
            or any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in branch.split("/"))):
        raise ValueError("invalid-branch")
    if (not isinstance(plan["project_path"], str) or not PROJECT.fullmatch(plan["project_path"])
            or not isinstance(plan["revision"], str) or not SHA.fullmatch(plan["revision"])):
        raise ValueError("invalid-source")
    jobs = plan["required_jobs"]
    if (not isinstance(jobs, list) or not 1 <= len(jobs) <= 100
            or any(not isinstance(job, str) or not JOB.fullmatch(job) for job in jobs)
            or len(set(jobs)) != len(jobs)):
        raise ValueError("invalid-required-jobs")
    return plan


def requester(plan, token, *, tls_context=None, max_bytes=1024 * 1024):
    def get(path):
        request = Request(plan["api_url"] + path, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
        for attempt in range(3):
            try:
                with open_http_request(request, timeout=15, use_environment_proxy=False, tls_context=tls_context) as response:
                    data = loads_strict_json(read_bounded_response(response, max_bytes=max_bytes))
                    return data, {key.casefold(): value for key, value in response.headers.items()}
            except HTTPError as error:
                error.close()
                if error.code not in {500, 502, 503, 504} or attempt == 2:
                    raise
                time.sleep(1)
    return get


def verify(plan, get, *, observe_jobs=None):
    validate(plan)
    project_url = "/projects/" + str(plan["project_id"])
    pipeline_url = project_url + "/pipelines/" + str(plan["pipeline_id"])
    branch_url = project_url + "/repository/branches/" + quote(plan["branch"], safe="")
    project, _ = get(project_url)
    if not isinstance(project, dict) or type(project.get("id")) is not int or project.get("id") != plan["project_id"] or project.get("path_with_namespace") != plan["project_path"]:
        raise ValueError("project-identity-mismatch")

    def source_state():
        branch, _ = get(branch_url)
        pipeline, _ = get(pipeline_url)
        if (not isinstance(branch, dict) or branch.get("name") != plan["branch"] or branch.get("protected") is not True
                or not isinstance(branch.get("commit"), dict) or branch["commit"].get("id") != plan["revision"]):
            raise ValueError("branch-not-protected-or-head-changed")
        if (not isinstance(pipeline, dict) or type(pipeline.get("id")) is not int or pipeline.get("id") != plan["pipeline_id"]
                or type(pipeline.get("project_id")) is not int or pipeline.get("project_id") != plan["project_id"] or pipeline.get("sha") != plan["revision"]
                or pipeline.get("ref") != plan["branch"] or pipeline.get("status") != "success" or pipeline.get("source") != "push"):
            raise ValueError("pipeline-identity-or-status-mismatch")
        return pipeline.get("updated_at")

    first_update = source_state()
    if not isinstance(first_update, str) or not first_update: raise ValueError("missing-pipeline-update-time")
    def selected_jobs():
        rows = []; page = 1
        while True:
            batch, headers = get(pipeline_url + f"/jobs?per_page=100&page={page}&include_retried=false")
            if not isinstance(batch, list) or len(batch) > 100 or "x-next-page" not in headers: raise ValueError("incomplete-jobs-pagination")
            rows.extend(batch)
            if len(rows) > 2000: raise ValueError("too-many-jobs")
            next_page = headers["x-next-page"]
            if next_page == "": break
            if next_page != str(page + 1) or page >= 20 or len(batch) != 100: raise ValueError("invalid-jobs-pagination")
            page += 1
        selected = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 or not isinstance(row.get("name"), str):
                raise ValueError("invalid-job-record")
            if row["name"] in plan["required_jobs"]:
                if row["name"] in selected or row.get("status") != "success" or row.get("allow_failure") is not False:
                    raise ValueError("required-job-not-successful")
                selected[row["name"]] = row["id"]
        if set(selected) != set(plan["required_jobs"]) or len(set(selected.values())) != len(selected):
            raise ValueError("required-job-missing-or-ambiguous")
        return selected

    selected = selected_jobs()
    for name, job_id in selected.items():
        job, _ = get(project_url + "/jobs/" + str(job_id))
        if (not isinstance(job, dict) or type(job.get("id")) is not int or job.get("id") != job_id or job.get("name") != name
                or job.get("status") != "success" or job.get("allow_failure") is not False
                or not isinstance(job.get("pipeline"), dict) or type(job["pipeline"].get("id")) is not int or job["pipeline"].get("id") != plan["pipeline_id"]
                or not isinstance(job.get("commit"), dict) or job["commit"].get("id") != plan["revision"]):
            raise ValueError("required-job-changed")
    if observe_jobs is not None:
        observe_jobs(selected)
    if selected_jobs() != selected: raise ValueError("jobs-changed-during-verification")
    if source_state() != first_update: raise ValueError("pipeline-changed-during-verification")
    project_again, _ = get(project_url)
    if not isinstance(project_again, dict) or type(project_again.get("id")) is not int or project_again.get("id") != plan["project_id"] or project_again.get("path_with_namespace") != plan["project_path"]:
        raise ValueError("project-changed-during-verification")
    return {"verified": True, "read_only": True, "project_id": plan["project_id"], "pipeline_id": plan["pipeline_id"],
            "revision": plan["revision"], "required_job_ids": selected, "deployment_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True, help="Private directory containing api-token")
    args = parser.parse_args()
    try:
        plan = validate(loads_strict_json(read_bounded_text(args.plan, encoding="utf-8")))
        token = private_input("api-token", minimum=16, maximum=512, root=args.input_dir).decode("ascii")
        if any(ord(c) < 33 or ord(c) > 126 for c in token): raise ValueError("invalid-token")
        result = verify(plan, requester(plan, token))
    except Exception:
        print("GitLab pipeline verification failed; no deployment was attempted.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
