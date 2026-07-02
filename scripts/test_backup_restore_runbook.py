#!/usr/bin/env python3
"""Validate the production backup and restore drill runbook."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "BACKUP_RESTORE.md"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
QUICK_START = ROOT / "docs" / "QUICK_START.md"
PRIVATE_DEPLOYMENT = ROOT / "docs" / "PRIVATE_DEPLOYMENT.md"
RELEASE_GUIDE = ROOT / "docs" / "RELEASE_GUIDE.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing required text: {needle}")


def main() -> int:
    runbook = read(RUNBOOK)
    if re.search(r"<[^>\n]+>", runbook):
        fail("backup/restore runbook must not contain angle-bracket placeholders")

    for heading in (
        "# Backup and Restore",
        "## Required Backups",
        "## Off-Cluster Requirement",
        "## RPO/RTO Targets",
        "## Evidence Before Production",
        "## Restore Drill Scope",
        "## Drill Cadence",
        "## Acceptance Template",
        "## Failure Handling",
    ):
        require(runbook, heading, "backup/restore runbook")

    for proof in (
        "make platform-production-check",
        "make platform-app-health",
        "Etcd snapshots",
        "Velero BackupStorageLocation",
        "CloudNativePG backup plus WAL archive",
        "Longhorn backup target",
        "scratch PVC",
        "Forgejo",
        "git clone",
        "git fsck",
        "Harbor",
        "docker pull",
        "crane digest",
        "Argo CD",
        "SOPS age private key material",
        "RPO target",
        "RTO target",
        "DRILL_ID",
        "Quarterly",
        "Before each production upgrade",
        "restore evidence",
    ):
        require(runbook, proof, "backup/restore runbook")

    for path, needles in {
        README: ("Backup and Restore", "docs/BACKUP_RESTORE.md"),
        ARCHITECTURE: ("restore drill evidence", "docs/BACKUP_RESTORE.md"),
        QUICK_START: ("docs/BACKUP_RESTORE.md", "Run a restore drill"),
        PRIVATE_DEPLOYMENT: ("restore drill evidence", "docs/BACKUP_RESTORE.md"),
        RELEASE_GUIDE: ("Restore drill evidence captured", "docs/BACKUP_RESTORE.md"),
    }.items():
        text = read(path)
        for needle in needles:
            require(text, needle, str(path.relative_to(ROOT)))

    print("Backup and restore runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
