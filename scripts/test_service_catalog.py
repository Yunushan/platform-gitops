#!/usr/bin/env python3
"""Validate the public-safe service catalog and ownership model."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs/SERVICE_CATALOG.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def require(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context} missing required text: {needle!r}")


def main() -> int:
    text = CATALOG.read_text(encoding="utf-8")

    for needle in (
        "# Service Catalog and Ownership",
        "public-safe service catalog model",
        "## Catalog Principles",
        "## Required Fields",
        "## Platform Service Matrix",
        "## Dependency Map",
        "## Ownership Review",
        "## Production Acceptance",
        "## Evidence",
        "Service name",
        "Criticality",
        "Owner",
        "Backup owner",
        "Support tier",
        "Data classification",
        "SLO/SLA target",
        "RPO/RTO target",
        "Backup and restore",
        "Access model",
        "Observability",
        "Capacity signals",
        "Release model",
        "Continuity role",
        "RKE2 API and etcd",
        "Cilium, CoreDNS, and kube-proxy path",
        "kube-vip and MetalLB",
        "Traefik or alternate ingress",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI or selected runner",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Prometheus, Grafana, and Loki",
        "cert-manager and trust-manager",
        "step-ca",
        "Monthly for P0 and P1 services",
        "Catalog entry is complete and owner-approved",
        "Do not commit private service catalogs",
    ):
        require(text, needle, "service catalog")

    for linked in (
        "docs/OPERATIONS.md",
        "docs/PRODUCTION_READINESS.md",
        "docs/BUSINESS_CONTINUITY.md",
        "docs/ALERTING.md",
        "docs/ACCESS_CONTROL.md",
        "docs/DATA_CLASSIFICATION.md",
        "docs/CAPACITY_PLANNING.md",
        "docs/COMPLIANCE_AUDIT.md",
        "docs/RELEASE_PROMOTION.md",
    ):
        require(text, linked, "service catalog")

    for path, needle, label in (
        (ROOT / "README.md", "docs/SERVICE_CATALOG.md", "README"),
        (ROOT / "docs/README.md", "SERVICE_CATALOG.md", "documentation index"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/SERVICE_CATALOG.md", "architecture guide"),
        (ROOT / "docs/OPERATIONS.md", "docs/SERVICE_CATALOG.md", "operations runbook"),
        (ROOT / "docs/PRODUCTION_READINESS.md", "docs/SERVICE_CATALOG.md", "production readiness checklist"),
        (ROOT / "docs/BUSINESS_CONTINUITY.md", "docs/SERVICE_CATALOG.md", "business continuity runbook"),
        (ROOT / "docs/COMPLIANCE_AUDIT.md", "docs/SERVICE_CATALOG.md", "compliance and audit guide"),
        (ROOT / "docs/ALERTING.md", "docs/SERVICE_CATALOG.md", "alerting runbook"),
        (ROOT / "docs/ACCESS_CONTROL.md", "docs/SERVICE_CATALOG.md", "access control runbook"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/SERVICE_CATALOG.md", "private deployment guide"),
        (ROOT / "SECURITY.md", "docs/SERVICE_CATALOG.md", "security policy"),
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
            fail(f"service catalog must not contain private material marker {forbidden!r}")

    print("Service catalog validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
