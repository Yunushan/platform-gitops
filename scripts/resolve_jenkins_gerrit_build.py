#!/usr/bin/env python3
"""Find a queued Jenkins build and compare successful evidence read-only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from bounded_file import read_bounded_text
from strict_json import loads_strict_json
from tekton_forge_bridge import private_input
import trigger_jenkins_gerrit as trigger
import verify_jenkins_gerrit as verifier


def resolve(plan, journal, jenkins_get, gerrit_get, *, change_state="NEW"):
    trigger.validate(plan)
    if change_state not in {"NEW", "MERGED"}: raise ValueError("invalid-change-state")
    probe = {**plan, "build": 1}
    record = verifier.queue_record_from_journal(probe, journal)
    job = "".join("/job/" + quote(part, safe="") for part in plan["job"])
    config = jenkins_get(job + "/config.xml", False)
    if hashlib.sha256(config).hexdigest() != plan["job_config_sha256"]: raise ValueError("jenkins-job-config-changed")
    rows = jenkins_get(job + "/api/json?tree=builds[number,queueId,building,result]{0,1000}", True)
    if not isinstance(rows, dict) or not isinstance(rows.get("builds"), list) or len(rows["builds"]) > 1000:
        raise ValueError("invalid-build-inventory")
    selected = []
    for row in rows["builds"]:
        if not isinstance(row, dict): raise ValueError("invalid-build-record")
        if type(row.get("queueId")) is int and row["queueId"] == record["queue_id"]:
            if type(row.get("number")) is not int or not 1 <= row["number"] <= 9999999999:
                raise ValueError("invalid-build-identity")
            selected.append(row)
    if len(selected) > 1: raise ValueError("ambiguous-queue-build")
    base = {"read_only": True, "queue_id": record["queue_id"], "build_verified": False,
            "review_vote_authorized": False, "deployment_authorized": False, "full_stack_verified": False}
    if not selected:
        return {**base, "status": "not-found-in-bounded-history"}
    item = selected[0]
    if item.get("building") is True and item.get("result") is None:
        return {**base, "status": "running", "build": item["number"]}
    if item.get("building") is not False or not isinstance(item.get("result"), str):
        raise ValueError("invalid-build-status")
    if item["result"] != "SUCCESS":
        if item["result"] not in {"FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT"}: raise ValueError("unknown-build-status")
        return {**base, "status": "terminal-non-success", "build": item["number"], "jenkins_result": item["result"]}
    evidence = verifier.verify({**plan, "build": item["number"]}, jenkins_get, gerrit_get, journal=journal, change_state=change_state)
    return {**base, "status": "matched-success", "build": item["number"], "build_verified": True, "evidence": evidence}


def resolve_failure(plan, journal, jenkins_get, gerrit_get):
    """Compare a definite FAILURE to its config-bound open patch-set queue."""
    outcome = resolve(plan, journal, jenkins_get, gerrit_get)
    if outcome.get("status") != "terminal-non-success" or outcome.get("jenkins_result") != "FAILURE":
        raise ValueError("terminal-failure-required")
    evidence = verifier.verify({**plan, "build": outcome["build"]}, jenkins_get, gerrit_get,
                               journal=journal, change_state="NEW", expected_result="FAILURE")
    return {**outcome, "status": "matched-failure", "build_verified": True, "evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True); parser.add_argument("--change-state", choices=("NEW", "MERGED"), default="NEW")
    args = parser.parse_args()
    try:
        plan = trigger.validate(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
        users = [private_input(name, minimum=1, maximum=100, root=args.input_dir).decode("ascii") for name in ("jenkins-user", "gerrit-user")]
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", user) for user in users): raise ValueError("invalid-user")
        tokens = [private_input(name, minimum=16, maximum=512, root=args.input_dir) for name in ("jenkins-token", "gerrit-token")]
        if any(any(c < 33 or c > 126 for c in token) for token in tokens): raise ValueError("invalid-token")
        private_input("ca.crt", maximum=256 * 1024, root=args.input_dir)
        os.environ["SSL_CERT_FILE"] = str(args.input_dir / "ca.crt")
        result = resolve(plan, args.journal, verifier.requester(plan["jenkins_origin"], users[0], tokens[0]),
                         verifier.requester(plan["gerrit_origin"], users[1], tokens[1], prefix=True), change_state=args.change_state)
    except Exception:
        print("Jenkins build resolution failed; no vote or deployment was attempted.", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
