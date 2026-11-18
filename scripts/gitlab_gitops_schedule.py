#!/usr/bin/env python3
"""Run a reviewed latest-pipeline handoff once for an external scheduler."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid

from bounded_file import read_bounded_text
from strict_json import loads_strict_json
import ci_gitops_handoff as handoff
import gitlab_gitops_handoff as adapter


def run(plan_path, expected_sha256, *, apply=False, wait_seconds=0, root=handoff.ROOT):
    if os.name != "posix":
        raise ValueError("scheduler-requires-posix")
    import fcntl
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("reviewed-plan-digest-required")
    if type(apply) is not bool or type(wait_seconds) is not int or not 0 <= wait_seconds <= 3600:
        raise ValueError("invalid-scheduler-policy")
    plan = loads_strict_json(read_bounded_text(plan_path, max_bytes=128 * 1024))
    if not isinstance(plan, dict) or plan.get("schema_version") != 2 or handoff.sha256(plan) != expected_sha256:
        raise ValueError("reviewed-plan-mismatch")
    adapter.validate(adapter.materialize(plan, local_only=True))
    private = root / "private"
    if not private.is_dir() or private.is_symlink():
        raise ValueError("private-directory-required")
    lock_path = private / ("gitlab-gitops-" + expected_sha256 + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("invalid-lock-file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"started": False, "reason": "already-running", "controller_verified": False}
        # Freeze the exact reviewed plan: the caller's source file may rotate
        # while the scheduler is running, but this invocation cannot change.
        frozen = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=private, prefix=".gitlab-gitops-plan-", delete=False) as staged:
                frozen = Path(staged.name)
                json.dump(plan, staged, sort_keys=True)
                staged.flush()
                os.fsync(staged.fileno())
            proof = private / ("gitlab-gitops-proof-" + uuid.uuid4().hex)
            result = adapter.operate(frozen, apply=apply, check_server=not apply, output=proof,
                                     latest=True, wait_seconds=wait_seconds)
            return {"started": True, "proof_directory": str(proof), **result}
        finally:
            if frozen is not None:
                frozen.unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    try:
        result = run(args.plan, args.plan_sha256, apply=args.apply, wait_seconds=args.wait_seconds)
    except Exception:
        print("Scheduled GitLab handoff failed; review the private proof and live state.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
