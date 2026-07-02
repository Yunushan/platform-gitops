#!/usr/bin/env python3
"""Validate the public-safe business continuity and disaster recovery runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/BUSINESS_CONTINUITY.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def require(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context} missing required text: {needle!r}")


def main() -> int:
    text = RUNBOOK.read_text(encoding="utf-8")

    for needle in (
        "# Business Continuity and Disaster Recovery",
        "public-safe business continuity and disaster recovery model",
        "## Continuity Principles",
        "## Scope",
        "## Minimum Viable Platform",
        "## Dependency Recovery Order",
        "## Scenario Matrix",
        "## RPO and RTO Model",
        "## Failover and Failback",
        "## Continuity Exercises",
        "## Continuity Evidence",
        "## Production Gate",
        "minimum viable platform",
        "RKE2 API, etcd quorum",
        "CNI, CoreDNS, and kube-proxy service path",
        "GitOps source of truth",
        "Velero BackupStorageLocation",
        "Longhorn or alternate storage",
        "CloudNativePG",
        "Traefik or alternate ingress",
        "Forgejo/Gitea/GitLab",
        "Harbor",
        "Woodpecker",
        "Prometheus, Grafana, Loki, Alertmanager",
        "cert-manager Certificate readiness",
        "trust-manager Bundle readiness",
        "step-ca health",
        "Single node loss",
        "Control-plane quorum risk",
        "Storage data loss",
        "GitOps source unavailable",
        "Registry unavailable",
        "Backup target unavailable",
        "Ingress/VIP failure",
        "PKI or trust failure",
        "Region or site loss",
        "Maximum accepted time",
        "Maximum accepted data loss",
        "rollback, forward recovery, or restore-from-backup",
        "Failover is allowed only",
        "Failback should not begin",
        "Quarterly for restore and minimum viable platform tabletop",
        "Open continuity exceptions",
        "accepting authority",
        "Do not commit private continuity records",
    ):
        require(text, needle, "business continuity runbook")

    for linked in (
        "docs/BACKUP_RESTORE.md",
        "docs/INCIDENT_RESPONSE.md",
        "docs/OPERATIONS.md",
        "docs/PRODUCTION_READINESS.md",
        "docs/RELEASE_PROMOTION.md",
        "docs/COMPLIANCE_AUDIT.md",
        "docs/ALERTING.md",
        "docs/CAPACITY_PLANNING.md",
        "docs/PLATFORM_SUPPORT.md",
    ):
        require(text, linked, "business continuity runbook")

    for path, needle, label in (
        (ROOT / "README.md", "docs/BUSINESS_CONTINUITY.md", "README"),
        (ROOT / "docs/README.md", "BUSINESS_CONTINUITY.md", "documentation index"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/BUSINESS_CONTINUITY.md", "architecture guide"),
        (ROOT / "docs/BACKUP_RESTORE.md", "docs/BUSINESS_CONTINUITY.md", "backup/restore runbook"),
        (ROOT / "docs/OPERATIONS.md", "docs/BUSINESS_CONTINUITY.md", "operations runbook"),
        (ROOT / "docs/PRODUCTION_READINESS.md", "docs/BUSINESS_CONTINUITY.md", "production readiness checklist"),
        (ROOT / "docs/COMPLIANCE_AUDIT.md", "docs/BUSINESS_CONTINUITY.md", "compliance and audit guide"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/BUSINESS_CONTINUITY.md", "release guide"),
        (ROOT / "docs/RELEASE_PROMOTION.md", "docs/BUSINESS_CONTINUITY.md", "release promotion runbook"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/BUSINESS_CONTINUITY.md", "private deployment guide"),
        (ROOT / "SECURITY.md", "docs/BUSINESS_CONTINUITY.md", "security policy"),
    ):
        require(path.read_text(encoding="utf-8"), needle, label)

    for forbidden in (
        "172.",
        "192.168.",
        "10.",
        "AGE-SECRET-KEY-",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "hooks.slack.com",
    ):
        if forbidden in text:
            fail(f"business continuity runbook must not contain private material marker {forbidden!r}")

    print("Business continuity runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
