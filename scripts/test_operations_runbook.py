#!/usr/bin/env python3
"""Validate the production day-2 operations runbook."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "OPERATIONS.md"
README = ROOT / "README.md"
DOC_INDEX = ROOT / "docs" / "README.md"
USER_GUIDE = ROOT / "docs" / "USER_GUIDE.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
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
        fail("operations runbook must not contain angle-bracket placeholders")

    for heading in (
        "# Operations Runbook",
        "## Operating Principles",
        "## Ownership",
        "## Routine Checks",
        "## Change Management",
        "## Maintenance Windows",
        "## Upgrade Procedure",
        "## Access Control",
        "## Break-Glass Access",
        "## Incident Response",
        "## Drift Management",
        "## Credential Rotation",
        "## Capacity and Retention",
        "## Production Evidence",
    ):
        require(runbook, heading, "operations runbook")

    for proof in (
        "Git is the source of truth",
        "make platform-status",
        "make platform-app-health",
        "make platform-ci-health",
        "PLATFORM_PROFILE=premium-3node make platform-production-check",
        "pull request",
        "maintenance window",
        "RKE2 version upgrades",
        "one node at a time",
        "least privilege",
        "cluster-admin",
        "SOPS recipients",
        "Break-glass access",
        "incident commander",
        "SEV1",
        "SEV2",
        "SEV3",
        "manual cluster changes are temporary",
        "make platform-app-secrets",
        "SOPS age recipients and private keys",
        "Longhorn capacity",
        "Harbor registry storage",
        "Latest credential rotation",
    ):
        require(runbook, proof, "operations runbook")

    for path, needles in {
        README: ("Operations", "docs/OPERATIONS.md"),
        DOC_INDEX: ("Operations", "OPERATIONS.md"),
        USER_GUIDE: ("docs/OPERATIONS.md", "Day-2 operations"),
        ARCHITECTURE: ("Operations Model", "docs/OPERATIONS.md"),
        PRIVATE_DEPLOYMENT: ("operations evidence", "docs/OPERATIONS.md"),
        RELEASE_GUIDE: ("Operations owner", "docs/OPERATIONS.md"),
    }.items():
        text = read(path)
        for needle in needles:
            require(text, needle, str(path.relative_to(ROOT)))

    print("Operations runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
