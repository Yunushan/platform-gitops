#!/usr/bin/env python3
"""Validate the public-safe compliance and audit evidence guide."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/COMPLIANCE_AUDIT.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    doc = read(RUNBOOK)

    for needle in (
        "# Compliance and Audit Evidence",
        "public-safe compliance and audit evidence model",
        "## Principles",
        "## Control Domains",
        "## Required Evidence Records",
        "## Audit Logging Expectations",
        "## Exceptions and Risk Acceptance",
        "## Review Cadence",
        "## Control Mapping Template",
        "## Production Evidence",
    ):
        require(doc, needle, "compliance and audit guide")

    for domain in (
        "Source control",
        "Change management",
        "Access control",
        "Secrets management",
        "CI/CD separation",
        "Supply chain",
        "Backup and recovery",
        "Incident response",
        "Observability",
        "Capacity management",
        "Data classification",
        "Vulnerability management",
        "Audit logging",
        "Disaster recovery",
        "PKI and trust",
    ):
        require(doc, domain, "control domains")

    for evidence in (
        "python scripts/run_validation.py",
        "make no-secrets",
        "python scripts/validate_no_secrets.py",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "make platform-app-health",
        "docs/BACKUP_RESTORE.md",
        "docs/OPERATIONS.md",
        "docs/INCIDENT_RESPONSE.md",
        "docs/ACCESS_CONTROL.md",
        "docs/CAPACITY_PLANNING.md",
        "docs/ALERTING.md",
        "docs/DATA_CLASSIFICATION.md",
        "docs/THREAT_MODEL.md",
        "SECURITY.md",
    ):
        require(doc, evidence, "required evidence records")

    for audit_item in (
        "Who changed production desired state",
        "Who approved the change",
        "Which validation checks passed",
        "Which Argo CD Application applied the change",
        "Which Kubernetes resources changed",
        "Which CI pipeline built and published an artifact",
        "Which registry identity pushed or pulled a release artifact",
        "Git hosting audit logs",
        "Argo CD Application history",
        "Kubernetes events and audit logs",
        "Harbor audit logs",
        "Woodpecker build history",
    ):
        require(doc, audit_item, "audit logging expectations")

    for exception_item in (
        "skipped health gate",
        "temporary admin access",
        "broad alert silence",
        "missing backup target",
        "unpinned dependency",
        "expired restore drill",
        "Owner",
        "Compensating control",
        "Expiration date",
        "Expired exceptions should block production release",
    ):
        require(doc, exception_item, "exception handling")

    for cadence in (
        "Per change",
        "Weekly",
        "Monthly",
        "Quarterly",
        "Before major release",
        "After incident",
    ):
        require(doc, cadence, "review cadence")

    for private_safety in (
        "It is not a legal compliance statement",
        "private deployment repository or governance system",
        "Do not commit private audit exports",
        "framework-specific control mappings",
    ):
        require(doc, private_safety, "public-safe audit guidance")

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
            fail(f"compliance and audit guide must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/COMPLIANCE_AUDIT.md", "README"),
        (ROOT / "docs/README.md", "COMPLIANCE_AUDIT.md", "documentation index"),
        (ROOT / "docs/OPERATIONS.md", "docs/COMPLIANCE_AUDIT.md", "operations runbook"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/COMPLIANCE_AUDIT.md", "architecture guide"),
        (ROOT / "docs/DATA_CLASSIFICATION.md", "docs/COMPLIANCE_AUDIT.md", "data classification"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/COMPLIANCE_AUDIT.md", "threat model"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/COMPLIANCE_AUDIT.md", "release guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/COMPLIANCE_AUDIT.md", "private deployment guide"),
        (ROOT / "docs/USER_GUIDE.md", "docs/COMPLIANCE_AUDIT.md", "user guide"),
        (ROOT / "SECURITY.md", "docs/COMPLIANCE_AUDIT.md", "security policy"),
    ):
        require(read(doc_path), needle, label)

    print("Compliance and audit evidence validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
