#!/usr/bin/env python3
"""Bounded, opt-in reporting of completed Jenkins builds for current changes."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

from bounded_file import read_bounded_text
from strict_json import loads_strict_json
from tekton_forge_bridge import private_input
import report_jenkins_gerrit as reporter
import resolve_jenkins_gerrit_build as resolver
import scan_jenkins_gerrit as scanner
import tekton_gerrit_bridge as bridge
import trigger_jenkins_gerrit as trigger
import verify_jenkins_gerrit as verifier


def validate(policy):
    fields = {"schema_version", "scan", "reviewer_username", "reviewer_account_id", "label_definition_sha256", "trust_jenkins_controller", "max_reports"}
    if not isinstance(policy, dict) or set(policy) not in (fields, fields | {"report_failures"}) or type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("invalid-result-scan-policy")
    if type(policy.get("report_failures", False)) is not bool: raise ValueError("invalid-failure-policy")
    scanner.validate(policy["scan"])
    if type(policy["max_reports"]) is not int or not 1 <= policy["max_reports"] <= policy["scan"]["max_changes"]:
        raise ValueError("invalid-report-bound")
    fixed = {k: v for k, v in policy["scan"].items() if k not in {"max_changes", "max_submissions"}}
    reporter.validate({"schema_version": 1, "trigger": {**fixed, "change": 1, "patchset": 1, "revision": "1" * 40},
                       **{k: policy[k] for k in ("reviewer_username", "reviewer_account_id", "label_definition_sha256", "trust_jenkins_controller")}})
    return policy


def cycle(policy, queue_dir, review_dir, jenkins_get, gerrit_get, api_factory, *, apply=False):
    validate(policy)
    if type(apply) is not bool: raise ValueError("invalid-mode")
    queue = trigger.private_journal_directory(queue_dir)
    trigger.private_journal_directory(review_dir)
    scan = policy["scan"]
    class Source:
        def get(self, path): return gerrit_get(path)
    items = bridge.discover(Source(), {"source": {"project": scan["project"], "branch": scan["branch"]}, "max_changes": scan["max_changes"]})
    fixed = {k: v for k, v in scan.items() if k not in {"max_changes", "max_submissions"}}
    ready = []; counts = {"unsubmitted": 0, "pending": 0, "non_success": 0, "reported": 0, "already_reported": 0}
    for item in items:
        plan = {**fixed, "change": item["change"], "patchset": item["patchset"], "revision": item["revision"]}
        state = scanner.journal_state(plan, queue)
        if state == "reserved": raise ValueError("unresolved-queue-reservation")
        if state == "absent": counts["unsubmitted"] += 1; continue
        journal = queue / (trigger.reservation_key(plan) + ".json")
        result = resolver.resolve(plan, journal, jenkins_get, gerrit_get)
        if result["status"] in {"running", "not-found-in-bounded-history"}: counts["pending"] += 1; continue
        failure = result["status"] == "terminal-non-success" and result.get("jenkins_result") == "FAILURE" and policy.get("report_failures", False)
        if result["status"] == "terminal-non-success" and not failure: counts["non_success"] += 1; continue
        if result["status"] != "matched-success" and not failure: raise ValueError("invalid-build-resolution")
        report = {"schema_version": 1, "trigger": plan, **{k: policy[k] for k in ("reviewer_username", "reviewer_account_id", "label_definition_sha256", "trust_jenkins_controller")}}
        state = reporter.report(report, journal, review_dir, jenkins_get, gerrit_get, api_factory(report), failure=failure)
        if state["status"] == "already-reported": counts["already_reported"] += 1
        elif state["status"] == "ready": ready.append((report, journal, failure))
        else: raise ValueError("invalid-report-state")
    if len(ready) > policy["max_reports"]: raise ValueError("cycle-report-bound-exceeded")
    if apply:
        for report, journal, failure in ready:
            state = reporter.report(report, journal, review_dir, jenkins_get, gerrit_get, api_factory(report), apply=True, failure=failure)
            if state["status"] == "reported": counts["reported"] += 1
            elif state["status"] == "already-reported": counts["already_reported"] += 1
            else: raise ValueError("invalid-report-state")
    return {"read_only": not apply, "changes_checked": len(items), "ready": len(ready), **counts,
            "checkout_provenance_verified": False, "deployment_authorized": False, "full_stack_verified": False}


def cycle_lock(queue_dir):
    return scanner.cycle_lock(queue_dir, ".jenkins-result-poller.lock")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True); parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--input-dir", type=Path, required=True); parser.add_argument("--queue-dir", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True); parser.add_argument("--apply", action="store_true")
    parser.add_argument("--watch", action="store_true", help="Repeat until interrupted or a cycle fails")
    parser.add_argument("--interval-seconds", type=int, default=60, help="Watch delay, between 10 and 3600 seconds")
    args = parser.parse_args()
    try:
        policy = validate(loads_strict_json(read_bounded_text(args.policy, max_bytes=16384)))
        if not 10 <= args.interval_seconds <= 3600: raise ValueError("invalid-watch-interval")
        if not re.fullmatch(r"[0-9a-f]{64}", args.policy_sha256) or scanner.canonical_digest(policy) != args.policy_sha256:
            raise ValueError("reviewed-policy-digest-mismatch")
        users = [private_input(n, minimum=1, maximum=100, root=args.input_dir).decode("ascii") for n in ("jenkins-user", "gerrit-user", "reviewer-user")]
        if (any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", u) for u in users)
                or users[1].casefold() == users[2].casefold() or users[2] != policy["reviewer_username"]): raise ValueError("separate-reviewer-required")
        tokens = [private_input(n, minimum=16, maximum=512, root=args.input_dir) for n in ("jenkins-token", "gerrit-token", "reviewer-token")]
        if any(any(c < 33 or c > 126 for c in token) for token in tokens): raise ValueError("invalid-token")
        private_input("ca.crt", maximum=256 * 1024, root=args.input_dir); os.environ["SSL_CERT_FILE"] = str(args.input_dir / "ca.crt")
        scan = policy["scan"]
        # ReviewAPI is restricted per change, so the cycle constructs one per item.
        def api_factory(report): return reporter.ReviewAPI(report, tokens[2])
        jenkins_get = verifier.requester(scan["jenkins_origin"], users[0], tokens[0])
        gerrit_get = verifier.requester(scan["gerrit_origin"], users[1], tokens[1], prefix=True)
        with cycle_lock(args.queue_dir):
            while True:
                with scanner.cycle_lock(args.queue_dir, ".jenkins-queue-cycle.lock", wait=True):
                    result = cycle(policy, args.queue_dir, args.review_dir, jenkins_get, gerrit_get, api_factory, apply=args.apply)
                print(json.dumps(result, sort_keys=True), flush=True)
                if not args.watch: break
                time.sleep(args.interval_seconds)
    except Exception:
        print("Jenkins result scan stopped; retain private reservations and inspect any partial votes.", file=sys.stderr); return 1
    return 0
if __name__ == "__main__": sys.exit(main())
