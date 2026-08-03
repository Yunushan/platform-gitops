#!/usr/bin/env python3
"""Validate premium workload namespace isolation and required traffic paths."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PREMIUM = ROOT / "gitops/clusters/rke2-main/premium-3node"
COMPONENT = PREMIUM / "components/network-isolation"
VERIFY_PLAYBOOK = ROOT / "ansible/playbooks/verify-platform-network-isolation.yml"
MAKEFILE = ROOT / "Makefile"
PRODUCTION_CHECK = ROOT / "scripts/bootstrap/run-platform-production-check.sh"
PRODUCTION_READINESS = ROOT / "docs/PRODUCTION_READINESS.md"
TARGET_APPS = {
    "argocd-ha": "argocd",
    "forgejo": "forgejo",
    "harbor": "harbor",
    "keycloak": "keycloak",
    "loki": "logging",
    "minio": "object-storage",
    "monitoring": "monitoring",
    "openbao": "openbao",
    "platform-postgres": "platform-databases",
    "platform-valkey": "platform-cache",
    "step-ca": "step-ca",
    "woodpecker": "woodpecker",
}
CONTROLLER_NAMESPACES = {
    "cert-manager",
    "cnpg-system",
    "external-secrets",
    "kube-system",
    "kyverno",
    "longhorn-system",
    "velero",
}
REQUIRED_POLICY_NAMES = {
    "platform-default-deny",
    "platform-allow-same-namespace",
    "platform-allow-dns",
    "platform-allow-ingress",
    "platform-allow-egress",
}
POSTGRES_CLIENT_APPS = {"forgejo", "harbor", "keycloak", "monitoring", "woodpecker"}
POSTGRES_CLIENT_NAMESPACES = {"forgejo", "harbor", "keycloak", "monitoring", "woodpecker", "cnpg-system"}
VALKEY_CLIENT_APPS = {"forgejo", "harbor"}
REQUIRED_INTERNAL_PORTS = {22, 80, 443, 3000, 3100, 8200, 9000, 9090, 9187}
SENSITIVE_DATA_PORTS = {5432, 6379, 6380, 26379}
EGRESS_ROLES = {
    "external-web-egress": {
        "ports": {80, 443, 8443},
        "apps": {
            "argocd-ha", "forgejo", "harbor", "keycloak", "loki", "minio",
            "monitoring", "openbao", "platform-postgres", "step-ca", "woodpecker",
        },
    },
    "external-git-egress": {
        "ports": {22, 9418},
        "apps": {"argocd-ha", "forgejo", "woodpecker"},
    },
    "external-smtp-egress": {
        "ports": {25, 465, 587},
        "apps": {"forgejo", "harbor", "keycloak", "monitoring"},
    },
    "external-database-egress": {
        "ports": {3306, 5432},
        "apps": {"forgejo", "harbor", "keycloak", "monitoring", "woodpecker"},
    },
    "external-cache-egress": {
        "ports": {6379, 6380, 26379},
        "apps": set(),
    },
    "external-object-storage-egress": {
        "ports": {9000},
        "apps": {"harbor", "loki", "minio", "platform-postgres"},
    },
    "kubernetes-api-egress": {
        "ports": {6443},
        "apps": {"argocd-ha", "monitoring", "openbao", "platform-postgres", "woodpecker"},
    },
}


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required network isolation file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def main() -> int:
    component = read(COMPONENT / "kustomization.yaml")
    default_deny = read(COMPONENT / "default-deny.yaml")
    platform_traffic = read(COMPONENT / "allow-platform-traffic.yaml")
    postgres_client = read(PREMIUM / "components/postgres-client/allow-postgres-egress.yaml")
    valkey_client = read(PREMIUM / "components/valkey-client/allow-valkey-egress.yaml")
    postgres_server = read(PREMIUM / "apps/platform-postgres/allow-postgres-ingress.yaml")
    valkey_server = read(PREMIUM / "apps/platform-valkey/allow-valkey-ingress.yaml")
    verifier = read(VERIFY_PLAYBOOK)
    makefile = read(MAKEFILE)
    production_readiness = read(PRODUCTION_READINESS)
    combined = f"{default_deny}\n{platform_traffic}"

    require(component, "kind: Component", "network isolation component")
    require(component, "default-deny.yaml", "network isolation component")
    require(component, "allow-platform-traffic.yaml", "network isolation component")
    require(default_deny, "podSelector: {}", "default-deny policy")
    require(default_deny, "- Ingress", "default-deny policy")
    require(default_deny, "- Egress", "default-deny policy")
    require(platform_traffic, "k8s-app: kube-dns", "DNS egress policy")
    require(platform_traffic, "protocol: UDP", "DNS egress policy")
    require(platform_traffic, "protocol: TCP", "DNS egress policy")

    if combined.count("kind: NetworkPolicy") != len(REQUIRED_POLICY_NAMES):
        raise AssertionError("network isolation component must define exactly five NetworkPolicy resources")
    for policy_name in REQUIRED_POLICY_NAMES:
        require(combined, f"name: {policy_name}", "network isolation component")

    for app, namespace in TARGET_APPS.items():
        kustomization = read(PREMIUM / "apps" / app / "kustomization.yaml")
        require(kustomization, f"namespace: {namespace}", f"{app} kustomization")
        require(
            kustomization,
            "- ../../components/network-isolation",
            f"{app} kustomization",
        )
        if app in POSTGRES_CLIENT_APPS:
            require(
                kustomization,
                "- ../../components/postgres-client",
                f"{app} PostgreSQL client policy",
            )
        if app in VALKEY_CLIENT_APPS:
            require(
                kustomization,
                "- ../../components/valkey-client",
                f"{app} Valkey client policy",
            )
        for role, contract in EGRESS_ROLES.items():
            role_reference = f"- ../../components/{role}"
            role_apps = contract["apps"]
            assert isinstance(role_apps, set)
            if app in role_apps:
                require(kustomization, role_reference, f"{app} {role} policy")
            elif role_reference in kustomization:
                raise AssertionError(f"{app} must not receive the {role} policy")
        require(platform_traffic, f"- {namespace}", "platform namespace allowlist")

    for namespace in CONTROLLER_NAMESPACES:
        require(platform_traffic, f"- {namespace}", "controller namespace allowlist")
    for port in REQUIRED_INTERNAL_PORTS:
        require(platform_traffic, f"port: {port}", "network isolation port contract")
    if "\n    - ports:\n" in platform_traffic:
        raise AssertionError("shared network policy must not grant destination-unrestricted egress")
    for port in SENSITIVE_DATA_PORTS:
        if f"port: {port}" in platform_traffic:
            raise AssertionError(
                f"shared network policy must not grant sensitive data port {port}"
            )

    require(postgres_client, "name: platform-allow-postgres-egress", "PostgreSQL client policy")
    require(postgres_client, "kubernetes.io/metadata.name: platform-databases", "PostgreSQL client policy")
    require(postgres_client, "port: 5432", "PostgreSQL client policy")
    require(postgres_server, "name: platform-allow-postgres-clients", "PostgreSQL server policy")
    require(postgres_server, "cnpg.io/cluster: platform-postgres", "PostgreSQL server policy")
    for namespace in POSTGRES_CLIENT_NAMESPACES:
        require(postgres_server, f"- {namespace}", "PostgreSQL server client allowlist")

    require(valkey_client, "name: platform-allow-valkey-egress", "Valkey client policy")
    require(valkey_client, "kubernetes.io/metadata.name: platform-cache", "Valkey client policy")
    require(valkey_client, "port: 6379", "Valkey client policy")
    require(valkey_client, "port: 6380", "Valkey client policy")
    require(valkey_server, "name: platform-allow-valkey-clients", "Valkey server policy")
    for namespace in ("forgejo", "harbor", "monitoring", "platform-cache"):
        require(
            valkey_server,
            f"kubernetes.io/metadata.name: {namespace}",
            "Valkey server policy",
        )
    for port in (6379, 6380, 9121, 26379):
        require(valkey_server, f"port: {port}", "Valkey server policy")
    require(valkey_server, "- Egress", "Valkey server policy")

    for role, contract in EGRESS_ROLES.items():
        component_dir = PREMIUM / "components" / role
        policy_file = component_dir / f"allow-{role}.yaml"
        component_text = read(component_dir / "kustomization.yaml")
        policy_text = read(policy_file)
        require(component_text, "kind: Component", f"{role} component")
        require(component_text, policy_file.name, f"{role} component")
        require(policy_text, f"name: platform-allow-{role}", f"{role} policy")
        require(policy_text, "podSelector: {}", f"{role} policy")
        require(policy_text, "- Egress", f"{role} policy")
        actual_ports = {
            int(port)
            for port in re.findall(r"(?m)^\s+port:\s+(\d+)\s*$", policy_text)
        }
        expected_ports = contract["ports"]
        assert isinstance(expected_ports, set)
        if actual_ports != expected_ports:
            raise AssertionError(
                f"{role} policy ports are {sorted(actual_ports)}, "
                f"expected {sorted(expected_ports)}"
            )

    if "0.0.0.0/0" in combined or "::/0" in combined:
        raise AssertionError("network isolation must not grant address-wide ingress or egress with ipBlock")
    if "namespaceSelector: {}" in combined:
        raise AssertionError("network isolation must not grant traffic from every namespace")

    require(verifier, "platform-postgres-rw.platform-databases.svc.cluster.local", "live network verifier")
    require(verifier, "platform-valkey-primary.platform-cache.svc.cluster.local", "live network verifier")
    require(verifier, "create_probe woodpecker", "live network verifier")
    require(verifier, "create_probe harbor", "live network verifier")
    require(verifier, "create_probe argocd", "live network verifier")
    require(verifier, "result=denied", "live network verifier")
    require(verifier, "platform_network_required_role_policies", "live network verifier")
    require(verifier, "subelements('value')", "live network verifier")
    for role, contract in EGRESS_ROLES.items():
        role_apps = contract["apps"]
        assert isinstance(role_apps, set)
        if role_apps:
            require(verifier, f"platform-allow-{role}", "live network verifier")
    production_check = read(PRODUCTION_CHECK)
    require(makefile, "platform-network-isolation-verify:", "Makefile")
    require(
        production_check,
        '"${make_command}" platform-network-isolation-verify',
        "production readiness gate",
    )
    require(
        production_readiness,
        "make platform-network-isolation-verify",
        "production readiness documentation",
    )

    print(
        "Premium network isolation contract passed for "
        f"{len(TARGET_APPS)} namespaces, {len(REQUIRED_POLICY_NAMES)} baseline policies, "
        f"and {len(EGRESS_ROLES)} scoped egress roles."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
