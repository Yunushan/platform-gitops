#!/usr/bin/env python3
"""Validate the public-safe production access control runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCESS_CONTROL = ROOT / "docs/ACCESS_CONTROL.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"{label} is missing {needle!r}")


def main() -> int:
    doc = read(ACCESS_CONTROL)

    for needle in (
        "# Access Control Runbook",
        "public-safe access control model",
        "## Principles",
        "## Access Domains",
        "## Human Access",
        "## Kubernetes RBAC",
        "## Argo CD Access",
        "## Git and Branch Protection",
        "## CI and Robot Accounts",
        "## Break-Glass Access",
        "## Access Review",
        "## Removal and Rotation",
        "## Production Evidence",
    ):
        require(doc, needle, "access control runbook")

    for domain in (
        "Git hosting",
        "CI",
        "CD",
        "Kubernetes",
        "Registry",
        "Database",
        "Storage and backup",
        "Observability",
        "PKI and trust",
        "Operator workstations",
    ):
        require(doc, domain, "access domains")

    for role in (
        "Platform operators",
        "Security operators",
        "Source-control administrators",
        "CI administrators",
        "Registry administrators",
        "Database administrators",
        "Storage and backup administrators",
        "Observability administrators",
        "Read-only auditors",
        "Emergency break-glass users",
    ):
        require(doc, role, "human access roles")

    for control in (
        "least privilege",
        "MFA",
        "cluster-admin",
        "ClusterRoleBindings",
        "ServiceAccount token automounting",
        "Argo CD projects",
        "Repository credentials",
        "Protected main or production branches",
        "Required reviews",
        "Required validation checks",
        ".github/CODEOWNERS.example",
        "Robot accounts",
        "short-lived or scoped credentials",
        "docs/INCIDENT_RESPONSE.md",
    ):
        require(doc, control, "access controls")

    for review_item in (
        "Before production launch",
        "Monthly for high-value admin and robot access",
        "Quarterly for all platform roles",
        "SOPS age recipients",
        "external secret-store policies",
        "Remove Git hosting and repository permissions",
        "Confirm Argo CD does not reapply stale credentials from Git",
    ):
        require(doc, review_item, "access review and removal")

    for evidence in (
        "Current role-to-system access matrix",
        "Current Argo CD project and admin review",
        "Current Kubernetes RBAC review",
        "Current Git branch protection and CODEOWNERS review",
        "Current robot account and CI secret review",
        "Latest break-glass use",
        "Latest credential rotation",
    ):
        require(doc, evidence, "access evidence")

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
            fail(f"access control runbook must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/ACCESS_CONTROL.md", "README"),
        (ROOT / "docs/README.md", "ACCESS_CONTROL.md", "documentation index"),
        (ROOT / "docs/OPERATIONS.md", "docs/ACCESS_CONTROL.md", "operations runbook"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/ACCESS_CONTROL.md", "threat model"),
        (ROOT / "docs/DATA_CLASSIFICATION.md", "docs/ACCESS_CONTROL.md", "data classification"),
        (ROOT / "SECURITY.md", "docs/ACCESS_CONTROL.md", "security policy"),
        (ROOT / "docs/USER_GUIDE.md", "docs/ACCESS_CONTROL.md", "user guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/ACCESS_CONTROL.md", "private deployment guide"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/ACCESS_CONTROL.md", "release guide"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/ACCESS_CONTROL.md", "architecture guide"),
    ):
        require(read(doc_path), needle, label)

    print("Access control runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
