#!/usr/bin/env python3
"""Validate the public-safe data classification and retention runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_CLASSIFICATION = ROOT / "docs/DATA_CLASSIFICATION.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    doc = read(DATA_CLASSIFICATION)

    for needle in (
        "# Data Classification and Retention",
        "public template",
        "private deployment repository",
        "## Classification Levels",
        "## Component Data Map",
        "## Retention Baseline",
        "## Handling Rules",
        "## Disposal and Erasure",
        "## Evidence",
    ):
        require(doc, needle, "data classification runbook")

    for data_class in (
        "Public template data",
        "Internal deployment metadata",
        "Confidential operational data",
        "Restricted secrets and access material",
        "Regulated or customer data",
    ):
        require(doc, data_class, "classification levels")

    for component in (
        "Forgejo, Gitea, or GitLab",
        "Argo CD",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG PostgreSQL",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Loki",
        "Prometheus",
        "Grafana",
        "cert-manager and trust-manager",
        "step-ca",
        "RKE2 and etcd",
        "CI runners and operator workstations",
    ):
        require(doc, component, "component data map")

    for retention_area in (
        "Git repositories and pull requests",
        "CI logs and artifacts",
        "Registry artifacts",
        "Vulnerability scan results",
        "Metrics",
        "Logs",
        "Database backups and WAL",
        "Volume snapshots and backups",
        "Etcd snapshots",
        "Velero backups",
        "Audit and access review evidence",
        "Incident records",
        "Secrets and credentials",
    ):
        require(doc, retention_area, "retention baseline")

    for rule in (
        "Encrypt restricted secrets",
        "highest-class source data",
        "Redact logs",
        "synthetic data for restore",
        "Confirming Argo CD does not recreate deleted resources from Git",
        "Do not promise customer or user erasure",
    ):
        require(doc, rule, "handling and disposal rules")

    for evidence in (
        "Current data owner",
        "Current retention period",
        "Latest access review",
        "docs/BACKUP_RESTORE.md",
        "docs/ALERTING.md",
        "docs/THREAT_MODEL.md",
        "SECURITY.md",
    ):
        require(doc, evidence, "data evidence")

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
            fail(f"data classification runbook must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/DATA_CLASSIFICATION.md", "README"),
        (ROOT / "docs/README.md", "DATA_CLASSIFICATION.md", "documentation index"),
        (ROOT / "docs/SECRETS_AND_PRIVACY.md", "docs/DATA_CLASSIFICATION.md", "secrets/privacy docs"),
        (ROOT / "docs/OPERATIONS.md", "docs/DATA_CLASSIFICATION.md", "operations runbook"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/DATA_CLASSIFICATION.md", "threat model"),
        (ROOT / "SECURITY.md", "docs/DATA_CLASSIFICATION.md", "security policy"),
    ):
        require(read(doc_path), needle, label)

    print("Data classification validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
