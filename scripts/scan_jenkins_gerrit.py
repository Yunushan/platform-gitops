#!/usr/bin/env python3
"""Run one bounded Gerrit discovery cycle for a reviewed Jenkins job."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from contextlib import contextmanager

from bounded_file import read_bootstrap_bytes, read_bounded_text
from strict_json import loads_strict_json
from tekton_forge_bridge import private_input
import tekton_gerrit_bridge as gerrit
import trigger_jenkins_gerrit as trigger
import verify_jenkins_gerrit as verifier


def validate(policy):
    fields = {"schema_version", "jenkins_origin", "job", "job_config_sha256", "gerrit_origin", "project", "branch", "max_changes", "max_submissions"}
    if not isinstance(policy, dict) or set(policy) != fields or type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("invalid-scan-policy")
    for key in ("max_changes", "max_submissions"):
        if type(policy[key]) is not int or not 1 <= policy[key] <= 1000: raise ValueError("invalid-cycle-bound")
    if policy["max_submissions"] > policy["max_changes"]: raise ValueError("invalid-cycle-bound")
    fixed = {k: v for k, v in policy.items() if k not in {"max_changes", "max_submissions"}}
    trigger.validate({**fixed, "change": 1, "patchset": 1, "revision": "1" * 40})
    return policy


def canonical_digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@contextmanager
def cycle_lock(journal_dir, name, *, wait=False):
    if os.name != "posix" or name not in {".jenkins-trigger-poller.lock", ".jenkins-result-poller.lock", ".jenkins-queue-cycle.lock"}:
        raise ValueError("invalid-poller-lock")
    import fcntl
    import stat
    directory = trigger.private_journal_directory(journal_dir)
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        state = os.fstat(fd)
        if not stat.S_ISREG(state.st_mode) or state.st_mode & 0o077: raise ValueError("invalid-poller-lock")
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def journal_state(plan, directory):
    name = trigger.reservation_key(plan)
    path = directory / (name + ".json")
    if not path.exists() and not path.is_symlink(): return "absent"
    record = loads_strict_json(read_bootstrap_bytes(path, 4096, follow_symlinks=False))
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int or record.get("schema_version") not in {1, 2}
            or record.get("identity_sha256") != name): raise ValueError("invalid-existing-reservation")
    base = {"schema_version", "state", "identity_sha256"}
    if record["schema_version"] == 2:
        base.add("job_config_sha256")
        if record.get("job_config_sha256") != plan["job_config_sha256"]: raise ValueError("reserved-job-config-changed")
    if record.get("state") == "reserved" and set(record) == base:
        return "reserved"
    if (record.get("state") == "queued" and set(record) == base | {"queue_id"}
            and type(record["queue_id"]) is int and record["queue_id"] > 0):
        return "queued"
    raise ValueError("invalid-existing-reservation")


def cycle(policy, jenkins_get, gerrit_get, post, journal_dir, *, apply=False):
    validate(policy)
    if type(apply) is not bool: raise ValueError("invalid-mode")
    directory = trigger.private_journal_directory(journal_dir)
    class Source:
        def get(self, path): return gerrit_get(path)
    items = gerrit.discover(Source(), {"source": {"project": policy["project"], "branch": policy["branch"]}, "max_changes": policy["max_changes"]})
    fixed = {k: v for k, v in policy.items() if k not in {"max_changes", "max_submissions"}}
    plans = [{**fixed, "change": item["change"], "patchset": item["patchset"], "revision": item["revision"]} for item in items]
    pending, duplicate = [], 0
    for plan in plans:
        state = journal_state(plan, directory)
        if state == "reserved": raise ValueError("unresolved-queue-reservation")
        if state == "queued": duplicate += 1
        else: pending.append(plan)
    if len(pending) > policy["max_submissions"]: raise ValueError("cycle-submission-bound-exceeded")
    # Confirm every selected change and the reviewed job before the first write.
    for plan in pending: trigger.submit(plan, jenkins_get, gerrit_get, post, directory)
    queued = 0
    if apply:
        for plan in pending:
            try:
                trigger.submit(plan, jenkins_get, gerrit_get, post, directory, apply=True)
                queued += 1
            except FileExistsError:
                if journal_state(plan, directory) != "queued": raise ValueError("concurrent-or-uncertain-reservation") from None
                duplicate += 1
    return {"read_only": not apply, "changes_checked": len(items), "pending": len(pending), "queued": queued,
            "already_queued": duplicate, "build_verified": False, "review_vote_verified": False, "full_stack_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True); parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--input-dir", type=Path, required=True); parser.add_argument("--journal-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--watch", action="store_true", help="Repeat until interrupted or a cycle fails")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        policy = validate(loads_strict_json(read_bounded_text(args.policy, max_bytes=16384)))
        if not 10 <= args.interval_seconds <= 3600: raise ValueError("invalid-watch-interval")
        if not re.fullmatch(r"[0-9a-f]{64}", args.policy_sha256) or canonical_digest(policy) != args.policy_sha256:
            raise ValueError("reviewed-policy-digest-mismatch")
        users = [private_input(name, minimum=1, maximum=100, root=args.input_dir).decode("ascii") for name in ("jenkins-user", "gerrit-user")]
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", user) for user in users): raise ValueError("invalid-user")
        tokens = [private_input(name, minimum=16, maximum=512, root=args.input_dir) for name in ("jenkins-token", "gerrit-token")]
        if any(any(c < 33 or c > 126 for c in token) for token in tokens): raise ValueError("invalid-token")
        private_input("ca.crt", maximum=256 * 1024, root=args.input_dir)
        os.environ["SSL_CERT_FILE"] = str(args.input_dir / "ca.crt")
        jenkins_get = verifier.requester(policy["jenkins_origin"], users[0], tokens[0])
        gerrit_get = verifier.requester(policy["gerrit_origin"], users[1], tokens[1], prefix=True)
        post = trigger.post_requester(policy["jenkins_origin"], users[0], tokens[0])
        with cycle_lock(args.journal_dir, ".jenkins-trigger-poller.lock"):
            while True:
                with cycle_lock(args.journal_dir, ".jenkins-queue-cycle.lock", wait=True):
                    result = cycle(policy, jenkins_get, gerrit_get, post, args.journal_dir, apply=args.apply)
                print(json.dumps(result, sort_keys=True), flush=True)
                if not args.watch: break
                time.sleep(args.interval_seconds)
    except Exception:
        print("Jenkins discovery cycle stopped; inspect the private reservations and Jenkins queue before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
