#!/usr/bin/env python3
"""Validate the production alerting and SLO runbook."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "ALERTING.md"
README = ROOT / "README.md"
DOC_INDEX = ROOT / "docs" / "README.md"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
PRIVATE_DEPLOYMENT = ROOT / "docs" / "PRIVATE_DEPLOYMENT.md"
RELEASE_GUIDE = ROOT / "docs" / "RELEASE_GUIDE.md"


PRIVATE_VALUE_PATTERNS = (
    re.compile(r"https://hooks\.slack\.com/", re.IGNORECASE),
    re.compile(r"pagerduty", re.IGNORECASE),
    re.compile(r"opsgenie", re.IGNORECASE),
    re.compile(r"xox[baprs]-", re.IGNORECASE),
)


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
        fail("alerting runbook must not contain angle-bracket placeholders")
    for pattern in PRIVATE_VALUE_PATTERNS:
        if pattern.search(runbook):
            fail(f"alerting runbook must not contain private receiver detail matching {pattern.pattern}")

    for heading in (
        "# Alerting and SLO Runbook",
        "## Alerting Principles",
        "## Severity Model",
        "## Required Receivers",
        "## Required Platform Signals",
        "## SLO and Error Budget Expectations",
        "## Alert Routing Tests",
        "## Silences and Maintenance",
        "## Alert Review",
        "## Production Evidence",
    ):
        require(runbook, heading, "alerting runbook")

    for proof in (
        "Alerts must be actionable",
        "Every paging alert must have an owner and a runbook",
        "critical",
        "warning",
        "info",
        "Immediate page",
        "Platform critical receiver",
        "Backup and restore receiver",
        "monitoring/alertmanager-main",
        "Kubernetes API",
        "CNI/service path",
        "Ingress/VIP",
        "GitOps",
        "Woodpecker server unavailable",
        "Harbor core/registry unavailable",
        "CloudNativePG primary unavailable",
        "Longhorn node not schedulable",
        "Velero BackupStorageLocation unavailable",
        "cert-manager Certificate not ready",
        "make platform-app-health",
        "Kubernetes API availability",
        "App ingress VIP/FQDN availability",
        "CI queue latency",
        "Backup freshness",
        "Restore drill freshness",
        "error budget",
        "Send a test alert for each receiver",
        "silences suppress only the intended labels",
        "Do not use broad namespace-wide or severity-wide silences",
        "Review alerts monthly",
        "Latest Alertmanager receiver test",
        "Do not commit private receiver details",
    ):
        require(runbook, proof, "alerting runbook")

    for path, needles in {
        README: ("Alerting", "docs/ALERTING.md"),
        DOC_INDEX: ("Alerting and SLOs", "ALERTING.md"),
        OPERATIONS: ("docs/ALERTING.md", "Alertmanager receiver"),
        ARCHITECTURE: ("docs/ALERTING.md", "SLO/error budget"),
        PRIVATE_DEPLOYMENT: ("alert routing and SLO evidence", "docs/ALERTING.md"),
        RELEASE_GUIDE: ("Alert routing", "docs/ALERTING.md"),
    }.items():
        text = read(path)
        for needle in needles:
            require(text, needle, str(path.relative_to(ROOT)))

    print("Alerting runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
