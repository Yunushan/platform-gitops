#!/usr/bin/env python3
"""Validate the public-safe platform support and lifecycle policy."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SUPPORT = ROOT / "docs/PLATFORM_SUPPORT.md"
NODE_OS_SUPPORT = ROOT / "docs/NODE_OS_SUPPORT.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def require(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context} missing required text: {needle!r}")


def main() -> int:
    platform = PLATFORM_SUPPORT.read_text(encoding="utf-8")
    node_os = NODE_OS_SUPPORT.read_text(encoding="utf-8")

    for needle in (
        "# Platform Support",
        "public-safe support and lifecycle policy",
        "## Support Scope",
        "## Support Tiers",
        "## Admin Workstations",
        "## Cluster Nodes",
        "## Component Support Matrix",
        "## Git and CI Compatibility",
        "## Version and Lifecycle Policy",
        "## Compatibility Gates",
        "## Upgrade and Deprecation Policy",
        "## Support Evidence",
        "## Out of Scope",
        "Enterprise validated",
        "Compatible / best effort",
        "Lab or workstation only",
        "Deprecated or unsupported",
        "RKE2",
        "Cilium, CoreDNS, kube-proxy",
        "kube-vip API VIP",
        "MetalLB app VIP",
        "Traefik",
        "Argo CD",
        "Forgejo",
        "Gitea",
        "GitLab CE",
        "Woodpecker",
        "Harbor",
        "CloudNativePG",
        "Longhorn",
        "Rook Ceph",
        "Velero",
        "Prometheus, Grafana, and Loki",
        "cert-manager, trust-manager, and optional step-ca",
        "python scripts/run_validation.py",
        "make no-secrets",
        "PLATFORM_PROFILE=<PROFILE> make platform-profile-check",
        "make rke2-verify",
        "make platform-status",
        "make platform-app-health",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "End-of-life operating systems",
        "owner, expiration, and compensating control",
        "rollback or roll-forward plan",
        "Do not commit private support inventories",
    ):
        require(platform, needle, "platform support policy")

    for needle in (
        "# Node OS Support",
        "Use this matrix with [Platform Support](PLATFORM_SUPPORT.md)",
        "## Support meaning",
        "Enterprise validated",
        "Compatible / best effort",
        "Workstation only",
        "## Required node capabilities",
        "Swap disabled",
        "## Production acceptance",
        "make rke2-verify",
        "make platform-production-check",
        "## Lifecycle review",
        "end-of-life OS",
        "owner, expiration date, compensating control",
        "## Validation sources",
    ):
        require(node_os, needle, "node OS support policy")

    for path, needle, label in (
        (ROOT / "README.md", "docs/PLATFORM_SUPPORT.md", "README"),
        (ROOT / "docs/README.md", "PLATFORM_SUPPORT.md", "documentation index"),
        (ROOT / "docs/INSTALLATION.md", "docs/PLATFORM_SUPPORT.md", "installation guide"),
        (ROOT / "docs/OPERATIONS.md", "docs/PLATFORM_SUPPORT.md", "operations runbook"),
        (ROOT / "docs/PRODUCTION_READINESS.md", "docs/PLATFORM_SUPPORT.md", "production readiness checklist"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/PLATFORM_SUPPORT.md", "release guide"),
        (ROOT / "docs/PLATFORM_SUPPORT.md", "NODE_OS_SUPPORT.md", "platform support policy"),
        (ROOT / "docs/NODE_OS_SUPPORT.md", "PLATFORM_SUPPORT.md", "node OS support policy"),
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
        if forbidden in platform or forbidden in node_os:
            fail(f"support docs must not contain private material marker {forbidden!r}")

    print("Platform support policy validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
