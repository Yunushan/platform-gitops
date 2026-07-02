#!/usr/bin/env python3
"""Validate the public-safe architecture decision record process."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADR_DOC = ROOT / "docs/ARCHITECTURE_DECISIONS.md"
ADR_TEMPLATE = ROOT / "docs/adr/0000-template.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def require(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context} missing required text: {needle!r}")


def main() -> int:
    doc = ADR_DOC.read_text(encoding="utf-8")
    template = ADR_TEMPLATE.read_text(encoding="utf-8")

    for needle in (
        "# Architecture Decision Records",
        "public-safe architecture decision record process",
        "## Principles",
        "## When to Write an ADR",
        "## ADR Lifecycle",
        "## Required ADR Fields",
        "## Decision Review Gates",
        "## Public-Safe Guidance",
        "## Evidence",
        "Proposed",
        "Accepted",
        "Superseded",
        "Deprecated",
        "RKE2 topology",
        "CNI, kube-proxy, CoreDNS",
        "Argo CD source-of-truth",
        "Source control, CI, registry, storage, database, backup, observability, PKI",
        "Data classification, retention, backup, restore, failover, or failback",
        "Authentication, authorization, break-glass access",
        "Production readiness gates",
        "Title",
        "Status",
        "Owner",
        "Review date",
        "Context",
        "Decision drivers",
        "Options considered",
        "Consequences",
        "Validation",
        "Rollback or exit plan",
        "docs/adr/0000-template.md",
        "Do not commit private ADRs",
    ):
        require(doc, needle, "architecture decision process")

    for linked in (
        "docs/ARCHITECTURE.md",
        "docs/RELEASE_PROMOTION.md",
        "docs/PRODUCTION_READINESS.md",
        "docs/THREAT_MODEL.md",
        "docs/SERVICE_CATALOG.md",
        "docs/BUSINESS_CONTINUITY.md",
        "docs/COMPLIANCE_AUDIT.md",
    ):
        require(doc, linked, "architecture decision process")

    for needle in (
        "# ADR 0000: <Decision Title>",
        "Status: Proposed",
        "Owner: <PRIVATE_OWNER_OR_TEAM>",
        "## Context",
        "## Decision Drivers",
        "## Options Considered",
        "## Decision",
        "## Consequences",
        "## Validation",
        "## Rollback or Exit Plan",
        "## Related Records",
        "Supersedes:",
        "Superseded by:",
    ):
        require(template, needle, "ADR template")

    for path, needle, label in (
        (ROOT / "README.md", "docs/ARCHITECTURE_DECISIONS.md", "README"),
        (ROOT / "docs/README.md", "ARCHITECTURE_DECISIONS.md", "documentation index"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/ARCHITECTURE_DECISIONS.md", "architecture guide"),
        (ROOT / "docs/OPERATIONS.md", "docs/ARCHITECTURE_DECISIONS.md", "operations runbook"),
        (ROOT / "docs/PRODUCTION_READINESS.md", "docs/ARCHITECTURE_DECISIONS.md", "production readiness checklist"),
        (ROOT / "docs/RELEASE_PROMOTION.md", "docs/ARCHITECTURE_DECISIONS.md", "release promotion runbook"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/ARCHITECTURE_DECISIONS.md", "threat model"),
        (ROOT / "docs/COMPLIANCE_AUDIT.md", "docs/ARCHITECTURE_DECISIONS.md", "compliance and audit guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/ARCHITECTURE_DECISIONS.md", "private deployment guide"),
        (ROOT / "SECURITY.md", "docs/ARCHITECTURE_DECISIONS.md", "security policy"),
    ):
        require(path.read_text(encoding="utf-8"), needle, label)

    combined = doc + "\n" + template
    for forbidden in (
        "172.",
        "192.168.",
        "10.",
        "AGE-SECRET-KEY-",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "hooks.slack.com",
    ):
        if forbidden in combined:
            fail(f"ADR docs must not contain private material marker {forbidden!r}")

    print("Architecture decision record validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
