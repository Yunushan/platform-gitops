#!/usr/bin/env python3
"""Self-test the private, commit-bound production evidence validator."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_production_evidence as evidence  # noqa: E402


def fail(message: str) -> None:
    raise AssertionError(message)


def fixture(root: Path, now: datetime) -> dict[str, object]:
    directory = root / "private" / "production-evidence"
    directory.mkdir(parents=True)
    log = directory / "acceptance.log"
    log.write_text(
        "== platform production evidence ==\n== platform-production-check ==\nplatform-production-check\n",
        encoding="utf-8",
    )
    return {
        "schemaVersion": 1,
        "releaseId": "release-test",
        "completedAt": (now - timedelta(minutes=15)).isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "commit": "a" * 40,
        "result": "passed",
        "logPath": "private/production-evidence/acceptance.log",
        "logSha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "gates": {name: "passed" for name in evidence.REQUIRED_GATES},
    }


def main() -> int:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="platform-production-evidence-") as directory:
        root = Path(directory)
        document = fixture(root, now)
        summary = evidence.validate_evidence(
            document,
            root=root,
            now=now,
            max_age_days=7,
            expected_profile="premium-3node",
            expected_commit="a" * 40,
        )
        if summary["release_id"] != "release-test":
            fail("valid production evidence did not preserve release id")

        stale = dict(document)
        stale["completedAt"] = (now - timedelta(days=8)).isoformat()
        try:
            evidence.validate_evidence(stale, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("stale production evidence was accepted")

        changed_log = root / "private" / "production-evidence" / "acceptance.log"
        changed_log.write_text("changed\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence with a changed log was accepted")

    print("Production evidence validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
