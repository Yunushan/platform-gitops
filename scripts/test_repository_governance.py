#!/usr/bin/env python3
"""Validate repository review and issue governance templates."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
ISSUE_CONFIG = ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
BUG_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
FEATURE_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
EXPECTED_TEMPLATE_PATHS = (
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
)


PRIVATE_DETAIL_PATTERNS = (
    re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"AGE-SECRET-KEY-", re.IGNORECASE),
    re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
    re.compile(r"xox[baprs]-", re.IGNORECASE),
    re.compile(r"https://hooks\.slack\.com/", re.IGNORECASE),
)


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing required text: {needle}")


def reject_private_details(path: Path) -> None:
    text = read(path)
    for pattern in PRIVATE_DETAIL_PATTERNS:
        if pattern.search(text):
            fail(f"{path.relative_to(ROOT)} must not include private detail matching {pattern.pattern}")


def main() -> int:
    for expected_path in EXPECTED_TEMPLATE_PATHS:
        if not (ROOT / expected_path).exists():
            fail(f"missing governance template: {expected_path}")

    for path in (PR_TEMPLATE, ISSUE_CONFIG, BUG_TEMPLATE, FEATURE_TEMPLATE):
        if not path.exists():
            fail(f"missing governance template: {path.relative_to(ROOT)}")
        reject_private_details(path)

    pr_text = read(PR_TEMPLATE)
    for needle in (
        "## Summary",
        "## Change Type",
        "## Public-Safety Check",
        "## Validation",
        "## Production Impact",
        "## Rollback",
        "## Documentation",
        "No real domains, private IPs, customer names",
        "python scripts/run_validation.py",
        "make no-secrets",
        "Required maintenance window",
        "Required restore, alerting, security, or operations evidence update",
        "Data-loss risk",
        "SECURITY.md",
        "docs/OPERATIONS.md",
        "docs/ALERTING.md",
        "docs/BACKUP_RESTORE.md",
    ):
        require(pr_text, needle, "pull request template")

    config_text = read(ISSUE_CONFIG)
    for needle in (
        "blank_issues_enabled: false",
        "Security vulnerability or secret exposure",
        "SECURITY.md",
        "Do not open a public issue with private details",
        "Private deployment support",
    ):
        require(config_text, needle, "issue template config")

    bug_text = read(BUG_TEMPLATE)
    for needle in (
        "name: Bug report",
        "Do not include real domains, private IPs, credentials",
        "For vulnerabilities or secret exposure, follow SECURITY.md",
        "Reproduction steps",
        "Validation output",
        "python scripts/run_validation.py",
        "Safe environment details",
        "Operations, alerting, backup, or restore guidance",
    ):
        require(bug_text, needle, "bug issue template")

    feature_text = read(FEATURE_TEMPLATE)
    for needle in (
        "name: Feature request",
        "Keep the request public-safe",
        "Problem or gap",
        "Proposed solution",
        "Production impact",
        "Security, secrets, or supply chain",
        "Operations, alerting, backup, or restore",
        "Acceptance evidence",
    ):
        require(feature_text, needle, "feature issue template")

    contributing_text = read(CONTRIBUTING)
    for needle in (
        ".github/",
        "pull request and issue templates",
        "production impact",
        "rollback",
        "public-safety",
    ):
        require(contributing_text, needle, "CONTRIBUTING.md")

    print("Repository governance template validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
