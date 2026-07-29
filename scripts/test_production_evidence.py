#!/usr/bin/env python3
"""Self-test the private, commit-bound production evidence validator."""

from __future__ import annotations

import hashlib
import json
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
        "== platform production evidence ==\n"
        "source_branch=private-deploy\n"
        "source_expected_ref=seed/main\n"
        f"source_tree={'b' * 40}\n"
        "== platform-production-check ==\n"
        "platform-production-check\n"
        "== rendered-live-image-reconciliation ==\n"
        "Image inventory evidence accepted:\n",
        encoding="utf-8",
    )
    inventory = directory / "acceptance-image-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAt": (now - timedelta(minutes=15)).isoformat(),
                "profile": "premium-3node",
                "commit": "a" * 40,
                "expectedRegistry": "registry.example.test",
                "result": "passed",
                "inputs": {
                    "renderedSummarySha256": "d" * 64,
                    "liveInventorySha256": "e" * 64,
                    "signatureReportSha256": "f" * 64,
                    "exceptionsSha256": None,
                },
                "rendered": {"references": 1, "uniqueImages": 1, "unresolved": 0},
                "live": {
                    "containers": 1,
                    "uniqueImages": 1,
                    "unresolved": 0,
                    "clusterUid": "cluster-test",
                    "capturedAt": (now - timedelta(minutes=16)).isoformat(),
                },
                "images": [
                    {
                        "image": "registry.example.test/platform/app@sha256:" + "1" * 64,
                        "rendered": True,
                        "live": True,
                        "signatureVerified": True,
                        "admissionEnforced": True,
                        "exception": None,
                    }
                ],
                "summary": {
                    "images": 1,
                    "privateRegistryImages": 1,
                    "signatureVerifiedImages": 1,
                    "exceptions": 0,
                    "uncovered": 0,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "schemaVersion": 4,
        "releaseId": "release-test",
        "completedAt": (now - timedelta(minutes=15)).isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "commit": "a" * 40,
        "result": "passed",
        "logPath": "private/production-evidence/acceptance.log",
        "logSha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "imageInventory": {
            "path": "private/production-evidence/acceptance-image-inventory.json",
            "sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
        },
        "source": {
            "branch": "private-deploy",
            "expectedRef": "seed/main",
            "remote": "seed",
            "remoteUrlSha256": "c" * 64,
            "tree": "b" * 40,
            "clean": True,
        },
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
        if summary["expected_ref"] != "seed/main":
            fail("valid production evidence did not preserve source provenance")
        if summary["image_inventory_images"] != 1:
            fail("valid production evidence did not preserve image inventory proof")

        stale = dict(document)
        stale["completedAt"] = (now - timedelta(days=8)).isoformat()
        try:
            evidence.validate_evidence(stale, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("stale production evidence was accepted")

        dirty = dict(document)
        dirty["source"] = dict(document["source"], clean=False)
        try:
            evidence.validate_evidence(dirty, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a dirty source checkout")

        wrong_remote = dict(document)
        wrong_remote["source"] = dict(
            document["source"], expectedRef="origin/main"
        )
        try:
            evidence.validate_evidence(wrong_remote, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a mismatched remote-tracking ref")

        changed_inventory = root / "private" / "production-evidence" / "acceptance-image-inventory.json"
        original_inventory = changed_inventory.read_text(encoding="utf-8")
        changed_inventory.write_text("{}\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a changed image inventory artifact")
        changed_inventory.write_text(original_inventory, encoding="utf-8")

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
