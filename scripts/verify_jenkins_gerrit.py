#!/usr/bin/env python3
"""Read-only comparison of one Jenkins build and a merged Gerrit patch set."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit
from urllib.request import Request

from bounded_file import read_bootstrap_bytes, read_bounded_text
from http_transport import open_http_request, read_bounded_response
from strict_json import loads_strict_json
from tekton_forge_bridge import private_input
import tekton_gerrit_bridge as gerrit


def validate(plan):
    fields = {"schema_version", "jenkins_origin", "job", "build", "job_config_sha256", "gerrit_origin", "project", "branch", "change", "patchset", "revision"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-plan")
    for key in ("jenkins_origin", "gerrit_origin"):
        value = plan[key]
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) <= 32 or ord(c) > 126 for c in value):
            raise ValueError("invalid-origin")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path or parsed.netloc != parsed.netloc.lower()):
            raise ValueError("invalid-origin")
        try:
            if parsed.port is not None and not 1 <= parsed.port <= 65535: raise ValueError("invalid-origin")
        except ValueError:
            raise ValueError("invalid-origin") from None
    if not isinstance(plan["job"], list) or not 1 <= len(plan["job"]) <= 8 or any(
            not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", x) or x in {".", ".."} for x in plan["job"]):
        raise ValueError("invalid-job")
    for key in ("project", "branch"):
        value = plan[key]
        if (not isinstance(value, str) or len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
                or ".." in value or any(not p or p.startswith(".") or p.endswith((".", ".lock")) for p in value.split("/"))):
            raise ValueError("invalid-source")
    for key in ("build", "change", "patchset"):
        if type(plan[key]) is not int or not 1 <= plan[key] <= 9999999999: raise ValueError("invalid-identity")
    gerrit.revision(plan["revision"])
    if not isinstance(plan["job_config_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["job_config_sha256"]):
        raise ValueError("missing-reviewed-job-digest")
    return plan


def queue_record_from_journal(plan, path):
    validate(plan)
    identity = {k: plan[k] for k in ("jenkins_origin", "job", "gerrit_origin", "project", "branch", "change", "patchset", "revision")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = loads_strict_json(read_bootstrap_bytes(path, 4096, follow_symlinks=False))
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] not in {1, 2}
            or record.get("state") != "queued" or record.get("identity_sha256") != digest
            or type(record.get("queue_id")) is not int or not 1 <= record["queue_id"] <= 9999999999999999999):
        raise ValueError("invalid-queue-reservation")
    fields = {"schema_version", "state", "identity_sha256", "queue_id"}
    if record["schema_version"] == 2:
        fields.add("job_config_sha256")
        if record.get("job_config_sha256") != plan["job_config_sha256"]: raise ValueError("reserved-job-config-changed")
    if set(record) != fields: raise ValueError("invalid-queue-reservation")
    return {"queue_id": record["queue_id"], "job_config_bound": record["schema_version"] == 2}


def queue_id_from_journal(plan, path):
    return queue_record_from_journal(plan, path)["queue_id"]


def verify(plan, jenkins_get, gerrit_get, *, queue_id=None, journal=None, change_state="MERGED", expected_result="SUCCESS"):
    validate(plan)
    if queue_id is not None and journal is not None: raise ValueError("ambiguous-queue-source")
    record = queue_record_from_journal(plan, journal) if journal is not None else None
    if record is not None: queue_id = record["queue_id"]
    if change_state not in {"NEW", "MERGED"}: raise ValueError("invalid-change-state")
    if expected_result not in {"SUCCESS", "FAILURE"}: raise ValueError("invalid-expected-result")
    if expected_result == "FAILURE" and (change_state != "NEW" or not record or not record["job_config_bound"]):
        raise ValueError("failure-requires-config-bound-open-review")
    if change_state == "NEW" and queue_id is None: raise ValueError("open-change-requires-queue-identity")
    if queue_id is not None and (type(queue_id) is not int or not 1 <= queue_id <= 9999999999999999999):
        raise ValueError("invalid-queue-id")
    job = "".join("/job/" + quote(part, safe="") for part in plan["job"])
    build = job + "/" + str(plan["build"]) + "/api/json?tree=number,queueId,building,result,actions[parameters[name,value],lastBuiltRevision[SHA1],remoteUrls]"
    change_path = "/a/changes/" + str(plan["change"]) + "?o=CURRENT_REVISION"
    branch_path = "/a/projects/" + quote(plan["project"], safe="") + "/branches/" + quote(plan["branch"], safe="")
    source = {"source": {"project": plan["project"], "branch": plan["branch"]}}
    expected = {"change": plan["change"], "patchset": plan["patchset"], "revision": plan["revision"],
                "ref": gerrit.reference(plan["change"], plan["patchset"])}
    def read_state():
        config = jenkins_get(job + "/config.xml", False)
        if hashlib.sha256(config).hexdigest() != plan["job_config_sha256"]: raise ValueError("jenkins-job-config-changed")
        result = jenkins_get(build, True)
        if (not isinstance(result, dict) or type(result.get("number")) is not int or result.get("number") != plan["build"]
                or result.get("building") is not False or result.get("result") != expected_result or not isinstance(result.get("actions"), list)):
            raise ValueError("jenkins-build-result-mismatch")
        if queue_id is not None and (type(result.get("queueId")) is not int or result.get("queueId") != queue_id):
            raise ValueError("jenkins-queue-id-mismatch")
        params, git = [], []
        for action in result["actions"]:
            if not isinstance(action, dict): continue
            if "parameters" in action:
                if not isinstance(action["parameters"], list): raise ValueError("invalid-parameters")
                params.extend(action["parameters"])
            if "lastBuiltRevision" in action: git.append(action)
        values = {}
        for parameter in params:
            if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str): raise ValueError("invalid-parameter")
            if parameter["name"] in {"GERRIT_CHANGE_NUMBER", "GERRIT_PATCHSET_NUMBER", "GERRIT_PATCHSET_REVISION"}:
                if parameter["name"] in values: raise ValueError("duplicate-parameter")
                values[parameter["name"]] = parameter.get("value")
        if values != {"GERRIT_CHANGE_NUMBER": str(plan["change"]), "GERRIT_PATCHSET_NUMBER": str(plan["patchset"]), "GERRIT_PATCHSET_REVISION": plan["revision"]}:
            raise ValueError("jenkins-parameters-mismatch")
        clone = plan["gerrit_origin"] + "/a/" + plan["project"] + ".git"
        if ((expected_result == "SUCCESS" and len(git) != 1) or len(git) > 1
                or git and (not isinstance(git[0].get("lastBuiltRevision"), dict)
                or git[0]["lastBuiltRevision"].get("SHA1") != plan["revision"] or git[0].get("remoteUrls") != [clone])):
            raise ValueError("jenkins-scm-record-mismatch")
        if gerrit.current(source, gerrit_get(change_path), status=change_state) != expected: raise ValueError("gerrit-change-mismatch")
        branch = gerrit_get(branch_path)
        if not isinstance(branch, dict) or branch.get("ref") != "refs/heads/" + plan["branch"]:
            raise ValueError("gerrit-head-mismatch")
        head = gerrit.revision(branch.get("revision"))
        if change_state == "MERGED" and head != plan["revision"]: raise ValueError("gerrit-head-mismatch")
        return head
    if read_state() != read_state(): raise ValueError("gerrit-head-changed-during-check")
    return {"read_only": True, "jenkins_build_and_gerrit_match": True, "jenkins_result": expected_result, "change_state": change_state, "build": plan["build"], "revision": plan["revision"],
            "queue_identity_verified": queue_id is not None, "job_config_reservation_verified": bool(record and record["job_config_bound"]),
            "checkout_provenance_verified": False,
            "review_vote_authorized": False, "deployment_authorized": False, "full_stack_verified": False}


def requester(origin, username, token, *, prefix=False, tls_context=None):
    credentials = base64.b64encode(username.encode("ascii") + b":" + token).decode("ascii")
    def get(path, json_output=True, *, accept=None, max_bytes=1024 * 1024):
        request = Request(origin + path, headers={"Authorization": "Basic " + credentials, "Accept": accept or ("application/json" if json_output else "application/xml")})
        with open_http_request(request, timeout=15, use_environment_proxy=False, tls_context=tls_context) as response:
            raw = read_bounded_response(response, max_bytes=max_bytes)
        if not json_output: return raw
        if prefix:
            if not raw.startswith(b")]}'\n"): raise ValueError("invalid-gerrit-response")
            raw = raw[5:]
        return loads_strict_json(raw)
    return get


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--change-state", choices=("NEW", "MERGED"), default="MERGED", help="Check an open patch set or the merged branch head")
    queue = parser.add_mutually_exclusive_group()
    queue.add_argument("--queue-id", type=int, help="Compare the build against a recorded Jenkins queue ID")
    queue.add_argument("--journal", type=Path, help="Read the queued ID from the matching private trigger reservation")
    args = parser.parse_args()
    try:
        plan = validate(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
        users = [private_input(name, minimum=1, maximum=100, root=args.input_dir).decode("ascii") for name in ("jenkins-user", "gerrit-user")]
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", user) for user in users): raise ValueError("invalid-user")
        tokens = [private_input(name, minimum=16, maximum=512, root=args.input_dir) for name in ("jenkins-token", "gerrit-token")]
        if any(any(c < 33 or c > 126 for c in token) for token in tokens): raise ValueError("invalid-token")
        private_input("ca.crt", maximum=256 * 1024, root=args.input_dir)
        os.environ["SSL_CERT_FILE"] = str(args.input_dir / "ca.crt")
        jenkins_get = requester(plan["jenkins_origin"], users[0], tokens[0])
        gerrit_get = requester(plan["gerrit_origin"], users[1], tokens[1], prefix=True)
        result = verify(plan, jenkins_get, gerrit_get, queue_id=args.queue_id, journal=args.journal, change_state=args.change_state)
    except Exception:
        print("Jenkins/Gerrit verification failed; no deployment was attempted.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
