#!/usr/bin/env python3
"""Validate premium namespace Pod Security Admission and privileged exceptions."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"
PROJECT = ROOT / "gitops/clusters/rke2-main/projects/platform-project.yaml"
NAMESPACE_POLICIES = {
    "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/namespace.yaml": ("argocd", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/cert-manager/namespace.yaml": ("cert-manager", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/cloudnativepg/namespace.yaml": ("cnpg-system", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/external-secrets/namespace.yaml": ("external-secrets", "restricted", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/forgejo/namespace.yaml": ("forgejo", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/harbor/namespace.yaml": ("harbor", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/keycloak/namespace.yaml": ("keycloak", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/kyverno/namespace.yaml": ("kyverno", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/loki/namespace.yaml": ("logging", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/longhorn/namespace.yaml": ("longhorn-system", "privileged", "privileged", "privileged"),
    "gitops/clusters/rke2-main/premium-3node/apps/minio/namespace.yaml": ("object-storage", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/monitoring/namespace.yaml": ("monitoring", "privileged", "privileged", "privileged"),
    "gitops/clusters/rke2-main/premium-3node/apps/openbao/namespace.yaml": ("openbao", "restricted", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/platform-postgres/namespace.yaml": ("platform-databases", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/namespace.yaml": ("platform-cache", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/step-ca/namespace.yaml": ("step-ca", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/tetragon/namespace.yaml": ("tetragon", "privileged", "privileged", "privileged"),
    "gitops/clusters/rke2-main/premium-3node/apps/traefik/namespace.yaml": ("traefik", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/premium-3node/apps/velero/namespace.yaml": ("velero", "privileged", "privileged", "privileged"),
    "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/namespace.yaml": ("woodpecker", "baseline", "restricted", "restricted"),
    "gitops/clusters/rke2-main/apps/metallb/namespace.yaml": ("metallb-system", "privileged", "privileged", "privileged"),
}
PRIVILEGED_NAMESPACES = {
    "kube-system",
    "longhorn-system",
    "metallb-system",
    "monitoring",
    "tetragon",
    "velero",
}


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required pod-security file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def main() -> int:
    managed_namespaces: set[str] = set()
    for relative, (namespace, enforce, audit, warn) in NAMESPACE_POLICIES.items():
        path = ROOT / relative
        text = read(path)
        label = str(path.relative_to(ROOT))
        require(text, f"name: {namespace}", label)
        require(text, f"pod-security.kubernetes.io/enforce: {enforce}", label)
        require(text, f"pod-security.kubernetes.io/audit: {audit}", label)
        require(text, f"pod-security.kubernetes.io/warn: {warn}", label)
        require(read(path.parent / "kustomization.yaml"), "- namespace.yaml", f"{label} owner Kustomization")
        if namespace in managed_namespaces:
            raise AssertionError(f"namespace is classified more than once: {namespace}")
        managed_namespaces.add(namespace)

    project_destinations = {
        line.split(":", 1)[1].strip()
        for line in read(PROJECT).splitlines()
        if line.strip().startswith("- namespace:")
    }
    expected_destinations = managed_namespaces | {"kube-system"}
    if project_destinations != expected_destinations:
        missing = sorted(project_destinations - expected_destinations)
        stale = sorted(expected_destinations - project_destinations)
        raise AssertionError(
            f"AppProject/PSA namespace coverage mismatch; missing={missing or 'none'} stale={stale or 'none'}"
        )

    policy = read(APPS / "platform-policies/require-pod-security-baseline.yaml")
    require(policy, "apiVersion: policies.kyverno.io/v1", "pod-security policy")
    require(policy, "kind: ValidatingPolicy", "pod-security policy")
    require(policy, "namespaceSelector:", "pod-security policy")
    for namespace in PRIVILEGED_NAMESPACES:
        if policy.count(f"            - {namespace}") != 1:
            raise AssertionError(
                f"the pod-security ValidatingPolicy must exclude privileged namespace {namespace} exactly once"
            )
    for namespace in managed_namespaces - PRIVILEGED_NAMESPACES:
        if f"            - {namespace}" in policy:
            raise AssertionError(f"ordinary application namespace must not bypass Kyverno Pod rules: {namespace}")

    workload_policy = read(APPS / "platform-policies/require-workload-baseline.yaml")
    if "namespaceSelector:" in workload_policy or "excludeResourceRules:" in workload_policy:
        raise AssertionError("workload resource-request policy must cover privileged namespaces too")

    documentation = read(ROOT / "docs/PREMIUM_3NODE.md")
    require(documentation, "Pod Security Admission", "premium profile documentation")
    require(documentation, "Explicit privileged namespaces", "premium profile documentation")

    print(f"Premium Pod Security Admission contract passed for {len(managed_namespaces)} managed namespaces.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
