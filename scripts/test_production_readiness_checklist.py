#!/usr/bin/env python3
"""Validate the public-safe production readiness checklist."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs/PRODUCTION_READINESS.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    doc = read(CHECKLIST)

    for needle in (
        "# Production Readiness Checklist",
        "public-safe go/no-go model",
        "## Principles",
        "## Readiness Scope",
        "## Go/No-Go Checklist",
        "## Required Live Gates",
        "## 100-Point Production Gate",
        "## Component Acceptance Matrix",
        "## Exceptions and Deferrals",
        "## Launch Decision",
        "## Post-Launch Validation",
        "## Production Evidence",
    ):
        require(doc, needle, "production readiness checklist")

    for scope in (
        "Repository safety",
        "RKE2 cluster",
        "GitOps source",
        "Ingress",
        "Stateful data",
        "Backup and recovery",
        "Platform apps",
        "Access control",
        "Security and supply chain",
        "Operations",
    ):
        require(doc, scope, "readiness scope")

    for gate in (
        "python scripts/run_validation.py",
        "make validate",
        "make no-secrets",
        "python scripts/validate_no_secrets.py",
        "python scripts/test_strict_yaml_contract.py",
        "PLATFORM_PROFILE=<PROFILE> make platform-profile-check",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "make platform-production-score",
        "Final production score passed",
        "GITHUB_GOVERNANCE_EVIDENCE_FILE",
        "GITHUB_RELEASE_EVIDENCE_FILE",
        "GITHUB_RELEASE_APPROVAL_EVIDENCE_FILE",
        "GITHUB_RELEASE_CHECKSUMS_FILE",
        "GITHUB_RELEASE_CHECKSUM_BUNDLE_FILE",
        "PLATFORM_EXPECTED_COMMIT",
        "make platform-app-health",
        "make rke2-verify",
        "make platform-status",
        "PLATFORM_IMAGE_INTEGRITY_MODE=Enforce",
        "PLATFORM_IMAGE_INTEGRITY_REQUIRED=true",
        "PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE",
        "make platform-image-inventory-verify",
        "PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE",
        "PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-production-check",
    ):
        require(doc, gate, "required live gates")

    for linked_runbook in (
        "docs/BACKUP_RESTORE.md",
        "docs/ACCESS_CONTROL.md",
        "docs/ALERTING.md",
        "docs/CAPACITY_PLANNING.md",
        "docs/COMPLIANCE_AUDIT.md",
        "docs/RELEASE_PROMOTION.md",
        "docs/THREAT_MODEL.md",
        "docs/DATA_CLASSIFICATION.md",
        "docs/INCIDENT_RESPONSE.md",
        "SECURITY.md",
    ):
        require(doc, linked_runbook, "readiness evidence links")

    for component in (
        "RKE2 and etcd",
        "Cilium, CoreDNS, and kube-proxy path",
        "MetalLB, kube-vip, and ingress",
        "Traefik or alternate ingress",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Prometheus, Grafana, and Loki",
        "cert-manager and trust-manager",
        "Kyverno admission",
        "step-ca",
    ):
        require(doc, component, "component acceptance matrix")

    for exception_item in (
        "A skipped gate is an exception",
        "Expired exceptions block launch",
        "Owner",
        "Compensating control",
        "Expiration date",
        "Approval authority",
        "Launch should stop",
    ):
        require(doc, exception_item, "exception handling")

    for decision_item in (
        "Decision:",
        "Deployment/profile:",
        "Approver:",
        "Evidence package location:",
        "Rollback or roll-forward plan:",
        "Post-launch monitoring window:",
        "Result:",
    ):
        require(doc, decision_item, "launch decision record")

    for private_safety in (
        "private deployment repository or release system",
        "Do not commit private readiness packets",
        "internal hostnames",
        "launch approvals",
    ):
        require(doc, private_safety, "public-safe readiness guidance")

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
            fail(f"production readiness checklist must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/PRODUCTION_READINESS.md", "README"),
        (ROOT / "docs/README.md", "PRODUCTION_READINESS.md", "documentation index"),
        (ROOT / "docs/QUICK_START.md", "docs/PRODUCTION_READINESS.md", "quick start"),
        (ROOT / "docs/INSTALLATION.md", "docs/PRODUCTION_READINESS.md", "installation guide"),
        (ROOT / "docs/PREMIUM_3NODE.md", "docs/PRODUCTION_READINESS.md", "premium profile"),
        (ROOT / "docs/OPERATIONS.md", "docs/PRODUCTION_READINESS.md", "operations runbook"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/PRODUCTION_READINESS.md", "release guide"),
        (ROOT / "docs/RELEASE_PROMOTION.md", "docs/PRODUCTION_READINESS.md", "release promotion runbook"),
        (ROOT / "docs/COMPLIANCE_AUDIT.md", "docs/PRODUCTION_READINESS.md", "compliance and audit guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/PRODUCTION_READINESS.md", "private deployment guide"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/PRODUCTION_READINESS.md", "architecture guide"),
        (ROOT / "SECURITY.md", "docs/PRODUCTION_READINESS.md", "security policy"),
    ):
        require(read(doc_path), needle, label)

    print("Production readiness checklist validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
