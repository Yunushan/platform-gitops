#!/usr/bin/env python3
"""Validate the public-safe production threat model."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = ROOT / "docs/THREAT_MODEL.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    threat_model = read(THREAT_MODEL)

    for needle in (
        "# Threat Model",
        "public-safe",
        "private deployment",
        "## Scope",
        "## Assumptions",
        "## Assets",
        "## Trust Boundaries",
        "## Threat Scenarios",
        "## High-Risk Changes",
        "## Evidence",
        "## Review Cadence",
    ):
        require(threat_model, needle, "threat model")

    for asset in (
        "Kubernetes API and etcd",
        "GitOps repository",
        "Argo CD credentials and projects",
        "Forgejo repositories and admin users",
        "Woodpecker secrets and agents",
        "Harbor projects and robot accounts",
        "CloudNativePG data and backups",
        "Longhorn or alternate storage volumes",
        "Velero and object storage credentials",
        "cert-manager, trust-manager, and step-ca material",
        "SOPS age recipients and private keys",
        "Observability data",
    ):
        require(threat_model, asset, "threat model asset table")

    for scenario in (
        "Secret leakage",
        "Unauthorized production change",
        "Supply-chain compromise",
        "CI credential misuse",
        "Argo CD over-privilege",
        "Ingress or VIP exposure",
        "Storage or database loss",
        "Backup target compromise",
        "PKI or trust compromise",
        "Observability data leak",
        "Service-network failure",
    ):
        require(threat_model, scenario, "threat model scenarios")

    for control in (
        ".github/CODEOWNERS.example",
        "branch protection",
        "required reviews",
        "Renovate dashboard approval",
        "optional Cosign/Kyverno verification",
        "Argo CD projects",
        "restore drills",
        "credential rotation",
        "Health gates",
    ):
        require(threat_model, control, "threat model controls")

    for evidence in (
        "python scripts/run_validation.py",
        "make no-secrets",
        "platform-production-check",
        "docs/BACKUP_RESTORE.md",
        "docs/ALERTING.md",
    ):
        require(threat_model, evidence, "threat model evidence")

    for forbidden in (
        "172.",
        "192.168.",
        "10.",
        "AGE-SECRET-KEY-",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "hooks.slack.com",
    ):
        if forbidden in threat_model:
            fail(f"threat model must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/THREAT_MODEL.md", "README"),
        (ROOT / "docs/README.md", "THREAT_MODEL.md", "documentation index"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/THREAT_MODEL.md", "architecture"),
        (ROOT / "SECURITY.md", "docs/THREAT_MODEL.md", "security policy"),
    ):
        require(read(doc_path), needle, label)

    print("Threat model validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
