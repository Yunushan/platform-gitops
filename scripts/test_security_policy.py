#!/usr/bin/env python3
"""Validate public security policy and governance references."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SECURITY = ROOT / "SECURITY.md"
README = ROOT / "README.md"
DOC_INDEX = ROOT / "docs" / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
RELEASE_GUIDE = ROOT / "docs" / "RELEASE_GUIDE.md"


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


def main() -> int:
    security = read(SECURITY)
    for pattern in PRIVATE_DETAIL_PATTERNS:
        if pattern.search(security):
            fail(f"SECURITY.md must not include private detail matching {pattern.pattern}")

    for heading in (
        "# Security Policy",
        "## Supported Scope",
        "## Supported Versions",
        "## Reporting a Vulnerability",
        "## Response Expectations",
        "## Secret Exposure Handling",
        "## Dependency and Supply-Chain Security",
        "## Secure Configuration Baseline",
        "## Disclosure and Safe Harbor",
    ):
        require(security, heading, "SECURITY.md")

    for needle in (
        "public-safe platform template",
        "must not contain live organization secrets",
        "latest-main security support model",
        "| `main` | Supported |",
        "Use a private security report",
        "Do not paste",
        "Target first response",
        "2 business days",
        "Rotate the exposed credential or key immediately",
        "make no-secrets",
        "python scripts/run_validation.py",
        "Rewrite Git history only in private repositories",
        "renovate.json",
        "CI workflows pinned to full commit SHAs",
        "verify-signed-images.example.yaml",
        "SBOMs and attestations",
        "make platform-production-check",
        "docs/BACKUP_RESTORE.md",
        "docs/OPERATIONS.md",
        "docs/ALERTING.md",
        "temporary seed Git URL",
        "Good-faith research",
        "Public disclosure should wait",
    ):
        require(security, needle, "SECURITY.md")

    for path, needles in {
        README: ("SECURITY.md", "Security"),
        DOC_INDEX: ("Security Policy", "../SECURITY.md"),
        CONTRIBUTING: ("SECURITY.md", "suspected vulnerabilities"),
        RELEASE_GUIDE: ("SECURITY.md", "supported-version policy"),
    }.items():
        text = read(path)
        for needle in needles:
            require(text, needle, str(path.relative_to(ROOT)))

    print("Security policy validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
