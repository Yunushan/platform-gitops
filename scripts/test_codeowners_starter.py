#!/usr/bin/env python3
"""Validate the public-safe CODEOWNERS starter for private deployments."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS = ROOT / ".github/CODEOWNERS.example"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
OPERATIONS = ROOT / "docs/OPERATIONS.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    codeowners = read(CODEOWNERS)
    contributing = read(CONTRIBUTING)
    operations = read(OPERATIONS)

    for needle in (
        "Copy this file to .github/CODEOWNERS",
        "private deployment repository",
        "Replace @org/...",
        "branch protection",
        "required reviews",
        "Keep real owner names",
    ):
        require(codeowners, needle, "CODEOWNERS starter")

    for pattern in (
        "*",
        "/SECURITY.md",
        "/CONTRIBUTING.md",
        "/.github/",
        "/ansible/",
        "/scripts/",
        "/config/",
        "/inventory/",
        "/profiles/",
        "/gitops/",
        "/policies/",
        "/renovate.json",
        "/docs/BACKUP_RESTORE.md",
        "/docs/BUSINESS_CONTINUITY.md",
        "/docs/SERVICE_CATALOG.md",
        "/docs/ARCHITECTURE_DECISIONS.md",
        "/docs/adr/",
        "/docs/OPERATIONS.md",
        "/docs/PRODUCTION_READINESS.md",
        "/docs/PLATFORM_SUPPORT.md",
        "/docs/NODE_OS_SUPPORT.md",
        "/docs/ALERTING.md",
        "/docs/SECRETS_AND_PRIVACY.md",
        "/docs/PRIVATE_DEPLOYMENT.md",
    ):
        require(codeowners, pattern, "CODEOWNERS starter")

    for owner in (
        "@org/platform-maintainers",
        "@org/security-maintainers",
        "@org/platform-automation-maintainers",
        "@org/gitops-maintainers",
        "@org/platform-operations",
        "@org/backup-owners",
        "@org/observability-owners",
        "@org/supply-chain-maintainers",
    ):
        require(codeowners, owner, "CODEOWNERS starter")

    for forbidden in (
        "172.",
        "192.168.",
        "10.",
        "AGE-SECRET-KEY-",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "hooks.slack.com",
    ):
        if forbidden in codeowners:
            fail(f"CODEOWNERS starter must not contain private material marker {forbidden!r}")

    for doc_text, label in ((contributing, "CONTRIBUTING.md"), (operations, "docs/OPERATIONS.md")):
        for needle in (
            ".github/CODEOWNERS.example",
            ".github/CODEOWNERS",
            "branch protection",
        ):
            require(doc_text, needle, label)

    require(contributing, "required reviews", "CONTRIBUTING.md")
    require(operations, "required reviewers", "docs/OPERATIONS.md")

    print("CODEOWNERS starter validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
