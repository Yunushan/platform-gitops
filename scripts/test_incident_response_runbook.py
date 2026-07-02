#!/usr/bin/env python3
"""Validate the public-safe production incident response runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCIDENT_RESPONSE = ROOT / "docs/INCIDENT_RESPONSE.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    doc = read(INCIDENT_RESPONSE)

    for needle in (
        "# Incident Response Runbook",
        "public-safe incident response workflow",
        "private incident record",
        "## Principles",
        "## Severity Declaration",
        "## Roles",
        "## First 15 Minutes",
        "## Stabilization Actions",
        "## Component Triage Matrix",
        "## Communications",
        "## Evidence Collection",
        "## Recovery Validation",
        "## Post-Incident Review",
        "## Production Evidence",
    ):
        require(doc, needle, "incident response runbook")

    for role in (
        "Incident commander",
        "Operations lead",
        "Communications lead",
        "Scribe",
        "Security lead",
        "Service owner",
    ):
        require(doc, role, "incident roles")

    for severity in ("SEV1", "SEV2", "SEV3"):
        require(doc, severity, "incident severity")

    for action in (
        "Freeze nonessential deployments",
        "Preserve volatile evidence",
        "make platform-status",
        "make platform-app-health",
        "platform-production-check",
        "Pause Argo CD automated sync",
        "Treat break-glass access as temporary",
        "rotate affected credentials",
        "Confirm Argo CD is not hiding live drift",
    ):
        require(doc, action, "incident actions")

    for area in (
        "Kubernetes API and etcd",
        "CNI, CoreDNS, kube-proxy, and service path",
        "Ingress and VIP",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and backups",
        "Prometheus, Grafana, Loki",
        "cert-manager, trust-manager, step-ca",
    ):
        require(doc, area, "component triage matrix")

    for review_item in (
        "Root cause",
        "Contributing factors",
        "Data or secret exposure",
        "Manual changes made",
        "Monitoring gaps",
        "Runbook gaps",
        "Preventive actions",
        "Owners and due dates",
    ):
        require(doc, review_item, "post-incident review")

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
            fail(f"incident response runbook must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/INCIDENT_RESPONSE.md", "README"),
        (ROOT / "docs/README.md", "INCIDENT_RESPONSE.md", "documentation index"),
        (ROOT / "docs/OPERATIONS.md", "docs/INCIDENT_RESPONSE.md", "operations runbook"),
        (ROOT / "docs/ALERTING.md", "docs/INCIDENT_RESPONSE.md", "alerting runbook"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/INCIDENT_RESPONSE.md", "threat model"),
        (ROOT / "docs/DATA_CLASSIFICATION.md", "docs/INCIDENT_RESPONSE.md", "data classification"),
        (ROOT / "SECURITY.md", "docs/INCIDENT_RESPONSE.md", "security policy"),
        (ROOT / "docs/USER_GUIDE.md", "docs/INCIDENT_RESPONSE.md", "user guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/INCIDENT_RESPONSE.md", "private deployment guide"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/INCIDENT_RESPONSE.md", "release guide"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/INCIDENT_RESPONSE.md", "architecture guide"),
    ):
        require(read(doc_path), needle, label)

    print("Incident response runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
