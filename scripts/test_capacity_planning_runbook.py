#!/usr/bin/env python3
"""Validate the public-safe production capacity planning runbook."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/CAPACITY_PLANNING.md"


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
        "# Capacity Planning Runbook",
        "public-safe production capacity planning model",
        "## Principles",
        "## Capacity Domains",
        "## Baseline Inventory",
        "## Saturation Signals",
        "## Load and Scale Tests",
        "## Scaling Decisions",
        "## Component Planning",
        "## Review Cadence",
        "## Production Evidence",
    ):
        require(doc, needle, "capacity planning runbook")

    for domain in (
        "Kubernetes nodes and API",
        "CNI, CoreDNS, kube-proxy, and service path",
        "Ingress and VIP",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Prometheus, Grafana, and Loki",
        "cert-manager, trust-manager, and step-ca",
        "MetalLB, kube-vip, and ingress",
    ):
        require(doc, domain, "capacity domains")

    for signal in (
        "CPU",
        "memory",
        "disk",
        "inode",
        "Pod scheduling failures",
        "Kubernetes API latency",
        "etcd health",
        "CoreDNS",
        "ClusterIP",
        "VIP reachability",
        "PVC usage",
        "WAL growth",
        "replication lag",
        "CI queue depth",
        "Registry storage usage",
        "Prometheus retention size",
        "Loki ingestion rate",
        "Velero backup age",
        "Certificate renewal failures",
    ):
        require(doc, signal, "saturation signals")

    for test_item in (
        "Git clone, push, pull request, and webhook traffic",
        "representative Woodpecker pipelines",
        "Push and pull representative images through Harbor",
        "Argo CD reconciliation load",
        "Prometheus and Loki ingestion",
        "PostgreSQL data and WAL volume",
        "Fill and expand test PVCs",
        "Velero backup and restore drills",
        "Restart or drain one node",
    ):
        require(doc, test_item, "load and scale tests")

    for command in (
        "make platform-status",
        "make platform-app-health",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "PLATFORM_CAPACITY_STORAGE_PATH=/mnt/longhorn",
    ):
        require(doc, command, "capacity evidence commands")

    if "PLATFORM_CAPACITY_STORAGE_PATH=/var/lib/longhorn" in doc:
        fail("capacity planning runbook must not recommend a root-backed Longhorn path")

    for private_safety in (
        "private deployment repository or operations system",
        "Real baselines belong",
        "Do not commit private capacity reports",
        "docs/DATA_CLASSIFICATION.md",
        "docs/ACCESS_CONTROL.md",
    ):
        require(doc, private_safety, "public-safe capacity guidance")

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
            fail(f"capacity planning runbook must not contain private material marker {forbidden!r}")

    for doc_path, needle, label in (
        (ROOT / "README.md", "docs/CAPACITY_PLANNING.md", "README"),
        (ROOT / "docs/README.md", "CAPACITY_PLANNING.md", "documentation index"),
        (ROOT / "docs/OPERATIONS.md", "docs/CAPACITY_PLANNING.md", "operations runbook"),
        (ROOT / "docs/ARCHITECTURE.md", "docs/CAPACITY_PLANNING.md", "architecture guide"),
        (ROOT / "docs/DATA_CLASSIFICATION.md", "docs/CAPACITY_PLANNING.md", "data classification"),
        (ROOT / "docs/THREAT_MODEL.md", "docs/CAPACITY_PLANNING.md", "threat model"),
        (ROOT / "docs/RELEASE_GUIDE.md", "docs/CAPACITY_PLANNING.md", "release guide"),
        (ROOT / "docs/PRIVATE_DEPLOYMENT.md", "docs/CAPACITY_PLANNING.md", "private deployment guide"),
        (ROOT / "docs/USER_GUIDE.md", "docs/CAPACITY_PLANNING.md", "user guide"),
    ):
        require(read(doc_path), needle, label)

    print("Capacity planning runbook validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
