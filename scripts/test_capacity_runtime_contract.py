#!/usr/bin/env python3
"""Validate the fail-closed production capacity runtime contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required capacity file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain {needle!r}")


def main() -> int:
    verifier = read(ROOT / "ansible/playbooks/verify-platform-capacity.yml")
    node_prepare = read(ROOT / "ansible/playbooks/prepare-nodes.yml")
    makefile = read(ROOT / "Makefile")
    production_check = read(ROOT / "scripts/bootstrap/run-platform-production-check.sh")
    planning = read(ROOT / "docs/CAPACITY_PLANNING.md")
    readiness = read(ROOT / "docs/PRODUCTION_READINESS.md")

    for needle in (
        "hosts: rke2_servers",
        "hosts: rke2_servers[0]",
        "PLATFORM_CAPACITY_ROOT_FREE_PERCENT",
        "PLATFORM_CAPACITY_STORAGE_FREE_PERCENT",
        "PLATFORM_CAPACITY_MAX_CPU_PERCENT",
        "PLATFORM_CAPACITY_MAX_MEMORY_PERCENT",
        "PLATFORM_CAPACITY_MAX_PODS_PERCENT",
        "PLATFORM_CAPACITY_LONGHORN_FREE_PERCENT",
        "PLATFORM_STORAGE_ENCRYPTION_REQUIRED",
        "LONGHORN_ENCRYPTION_SECRET_NAME",
        "df -Pk",
        'get nodes -o json',
        'get pods -A -o json',
        "nodes.longhorn.io",
        "unsupported Kubernetes quantity",
        "DiskPressure",
        "MemoryPressure",
        "PIDPressure",
        "regular_cpu",
        "init_cpu",
        "longhorn_schedulable_nodes",
        "filesystem-headroom-below-threshold",
        "ready-node-count-below-threshold",
        "cpu-requests-above-threshold",
        "memory-requests-above-threshold",
        "pod-capacity-above-threshold",
        "longhorn-free-capacity-below-threshold",
        "platform-capacity-headroom-verified",
        "longhorn-standard-encrypted",
        "longhorn-critical-encrypted",
        "longhorn-cache-encrypted",
        "CRYPTO_KEY_VALUE",
        'get pvc -A -o json',
        'get pv -o json',
        "nodeExpandSecretRef",
        "bound-longhorn-pvc-not-encrypted",
        "longhorn-storage-encryption-verified",
    ):
        require(verifier, needle, "capacity verifier")

    for forbidden in (
        "kubectl patch",
        "kubectl delete",
        "kubectl apply",
        "allowScheduling=true",
    ):
        forbid(verifier, forbidden, "read-only capacity verifier")

    for needle in (
        "cryptsetup",
        "dm_crypt",
        "Verify encrypted Longhorn volume prerequisites",
    ):
        require(node_prepare, needle, "RKE2 node preparation")

    require(makefile, "platform-capacity-verify:", "Makefile")
    require(
        production_check,
        '"${make_command}" platform-capacity-verify',
        "production readiness gate",
    )
    require(planning, "make platform-capacity-verify", "capacity planning runbook")
    require(readiness, "make platform-capacity-verify", "production readiness runbook")

    print("Production capacity runtime contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
