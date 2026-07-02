#!/usr/bin/env python3
"""Validate the public-safe release and environment promotion runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/RELEASE_PROMOTION.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    doc = read(RUNBOOK)

    for needle in (
        "# Release and Environment Promotion",
        "public-safe release and environment promotion model",
        "## Principles",
        "## Environment Model",
        "## Source and Artifact Flow",
        "## Promotion Gates",
        "## Change Windows and Freezes",
        "## Rollback and Roll-Forward",
        "## Hotfix Flow",
        "## Versioning and Tags",
        "## Argo CD Promotion Modes",
        "## Production Evidence",
    ):
        require(doc, needle, "release promotion runbook")

    for environment in (
        "Development",
        "Staging",
        "Production",
        "gitops/apps-dev",
        "gitops/apps-stage",
        "gitops/apps-prod",
    ):
        require(doc, environment, "environment model")

    for gate in (
        "pull request review",
        "CI test, scan, sign, and publish",
        "immutable image tag or digest",
        "GitOps pull request updates desired state",
        "python scripts/run_validation.py",
        "make no-secrets",
        "PLATFORM_PROFILE=<PROFILE> make platform-profile-check",
        "make platform-status",
        "make platform-app-health",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "docs/COMPLIANCE_AUDIT.md",
        "docs/CAPACITY_PLANNING.md",
    ):
        require(doc, gate, "promotion gates")

    for risk_control in (
        "maintenance window",
        "change freeze",
        "incident is active",
        "error budget is exhausted",
        "freeze override",
        "Previous known-good Git revision",
        "Previous known-good image tag or digest",
        "Database and storage rollback constraints",
        "Argo CD sync action or revert commit",
        "roll-forward fix",
    ):
        require(doc, risk_control, "release risk controls")

    for hotfix_item in (
        "Declare the incident or urgent change owner",
        "Freeze unrelated promotions",
        "Create the smallest safe Git change",
        "Run focused validation",
        "Promote through staging when time allows",
        "post-hotfix review",
    ):
        require(doc, hotfix_item, "hotfix flow")

    for versioning_item in (
        "Git commit SHA",
        "Image digest or stable release tag",
        "Pinned Helm chart version",
        "Pinned CI Action commit SHA",
        "Do not use mutable tags",
        "latest",
        "next",
        "nightly",
    ):
        require(doc, versioning_item, "versioning and tags")

    for pattern in (
        "Directory promotion",
        "Branch promotion",
        "Repository promotion",
        "ApplicationSet promotion",
        "temporary seed Git",
        "insecure repository URLs",
    ):
        require(doc, pattern, "Argo CD promotion modes")

    for private_safety in (
        "private deployment repository or release system",
        "Do not commit private release records",
        "customer impact notes",
        "internal environment names",
    ):
        require(doc, private_safety, "public-safe promotion guidance")

    for forbidden in (
        "172.",
        "192.168.",
        "10.",
        "AGE-SECRET-KEY-",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "hooks.slack.com",
    ):
        if forbidden in doc:
            fail(f"release promotion runbook must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/RELEASE_PROMOTION.md", "README"),
        (ROOT / "docs/README.md", "RELEASE_PROMOTION.md", "documentation index"),
        (ROOT / "docs/OPERATIONS.md", "docs/RELEASE_PROMOTION.md", "operations runbook"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/RELEASE_PROMOTION.md", "release guide"),
        (ROOT / "docs/USER_GUIDE.md", "docs/RELEASE_PROMOTION.md", "user guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/RELEASE_PROMOTION.md", "private deployment guide"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/RELEASE_PROMOTION.md", "architecture guide"),
        (ROOT / "docs/COMPLIANCE_AUDIT.md", "docs/RELEASE_PROMOTION.md", "compliance and audit guide"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/RELEASE_PROMOTION.md", "threat model"),
        (ROOT / "SECURITY.md", "docs/RELEASE_PROMOTION.md", "security policy"),
    ):
        require(read(doc_path), needle, label)

    print("Release promotion runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
