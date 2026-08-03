#!/usr/bin/env python3
"""Validate managed internal trust and encrypted OpenBao/PostgreSQL/Valkey paths."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREMIUM = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"
MAKEFILE = ROOT / "Makefile"
VERIFY_PLAYBOOK = ROOT / "ansible/playbooks/verify-platform-internal-tls.yml"
SECRET_PLAYBOOK = ROOT / "ansible/playbooks/configure-platform-app-secrets.yml"
PKI_DOC = ROOT / "docs/INTERNAL_PKI.md"
PRODUCTION_READINESS = ROOT / "docs/PRODUCTION_READINESS.md"


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required internal TLS file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain {needle!r}")


def main() -> int:
    cert_manager_kustomization = read(PREMIUM / "cert-manager/kustomization.yaml")
    internal_ca = read(PREMIUM / "cert-manager/internal-ca.yaml")
    trust_bundle = read(PREMIUM / "trust-manager/bundles.yaml")
    trust_values = read(PREMIUM / "trust-manager/values.yaml")
    openbao_kustomization = read(PREMIUM / "openbao/kustomization.yaml")
    openbao_certificate = read(PREMIUM / "openbao/server-certificate.yaml")
    injector_patch = read(PREMIUM / "openbao/injector-ca-patch.yaml")
    openbao_values = read(PREMIUM / "openbao/values.yaml")
    postgres = read(PREMIUM / "platform-postgres/postgres-cluster.yaml")
    valkey_kustomization = read(PREMIUM / "platform-valkey/kustomization.yaml")
    valkey_certificate = read(PREMIUM / "platform-valkey/server-certificate.yaml")
    valkey_values = read(PREMIUM / "platform-valkey/values.yaml")
    valkey_statefulset = read(
        PREMIUM
        / "platform-valkey/charts/valkey-0.10.0/valkey/templates/statefulset.yaml"
    )
    valkey_deployment = read(
        PREMIUM
        / "platform-valkey/charts/valkey-0.10.0/valkey/templates/deploy_valkey.yaml"
    )
    forgejo = read(PREMIUM / "forgejo/values.yaml")
    woodpecker = read(PREMIUM / "woodpecker/values.yaml")
    keycloak = read(PREMIUM / "keycloak/values.yaml")
    harbor = read(PREMIUM / "harbor/values.yaml")
    harbor_kustomization = read(PREMIUM / "harbor/kustomization.yaml")
    harbor_ca_patch = read(PREMIUM / "harbor/ca-bundle-configmap-patch.yaml")
    harbor_ca_statefulset_patch = read(
        PREMIUM / "harbor/ca-bundle-configmap-statefulset-patch.yaml"
    )
    monitoring = read(PREMIUM / "monitoring/values.yaml")
    verifier = read(VERIFY_PLAYBOOK)
    secret_playbook = read(SECRET_PLAYBOOK)
    makefile = read(MAKEFILE)
    pki_doc = read(PKI_DOC)
    readiness = read(PRODUCTION_READINESS)

    require(cert_manager_kustomization, "- internal-ca.yaml", "cert-manager kustomization")
    for needle in (
        "name: platform-internal-bootstrap",
        "name: platform-internal-root-ca",
        "name: platform-internal-ca",
        "selfSigned: {}",
        "rotationPolicy: Never",
        "secretName: platform-internal-root-ca",
    ):
        require(internal_ca, needle, "internal CA resources")

    for needle in (
        "name: platform-internal-roots",
        "name: platform-internal-root-ca",
        "key: tls.crt",
        "key: ca-certificates.crt",
    ):
        require(trust_bundle, needle, "trust-manager internal bundle")
    forbid(trust_bundle, "key: tls.key", "trust-manager internal bundle")
    forbid(trust_bundle, "key: ca.key", "trust-manager internal bundle")
    require(trust_values, "secretTargets:\n  enabled: false", "trust-manager values")

    require(openbao_kustomization, "- server-certificate.yaml", "OpenBao kustomization")
    require(openbao_kustomization, "- path: injector-ca-patch.yaml", "OpenBao kustomization")
    for needle in (
        "secretName: openbao-server-tls",
        "name: platform-internal-ca",
        "rotationPolicy: Always",
        "openbao.openbao.svc.cluster.local",
        '"*.openbao-internal.openbao.svc.cluster.local"',
    ):
        require(openbao_certificate, needle, "OpenBao Certificate")
    require(injector_patch, "AGENT_INJECT_VAULT_CACERT_BYTES", "OpenBao injector CA patch")
    require(injector_patch, "name: platform-internal-roots", "OpenBao injector CA patch")

    for needle in (
        "tlsDisable: false",
        "BAO_CACERT: /openbao/tls/ca.crt",
        "tls_cert_file = \"/openbao/tls/tls.crt\"",
        "tls_key_file = \"/openbao/tls/tls.key\"",
        "tls_min_version = \"tls12\"",
        "tls_max_version = \"tls13\"",
        "leader_api_addr = \"https://openbao-0.openbao-internal.openbao.svc.cluster.local:8200\"",
        "leader_api_addr = \"https://openbao-1.openbao-internal.openbao.svc.cluster.local:8200\"",
        "leader_api_addr = \"https://openbao-2.openbao-internal.openbao.svc.cluster.local:8200\"",
        "leader_tls_servername = \"openbao-0.openbao-internal.openbao.svc.cluster.local\"",
        "leader_tls_servername = \"openbao-1.openbao-internal.openbao.svc.cluster.local\"",
        "leader_tls_servername = \"openbao-2.openbao-internal.openbao.svc.cluster.local\"",
        "leader_ca_cert_file = \"/openbao/tls/ca.crt\"",
        "name: certificate-reloader",
        "kill -HUP",
        "serverName: openbao.openbao.svc.cluster.local",
    ):
        require(openbao_values, needle, "OpenBao TLS values")
    if openbao_values.count("retry_join {") != 3:
        raise AssertionError("OpenBao TLS values must declare one retry_join target per HA replica")
    forbid(openbao_values, "tls_disable = 1", "OpenBao TLS values")
    forbid(openbao_values, "insecureSkipVerify: true", "OpenBao TLS values")

    for needle in (
        "kind: Certificate",
        "name: platform-postgres-server",
        "secretName: platform-postgres-server-tls",
        "cnpg.io/reload",
        "rotationPolicy: Always",
        "platform-postgres-rw.platform-databases.svc.cluster.local",
        "serverCASecret: platform-postgres-server-tls",
        "serverTLSSecret: platform-postgres-server-tls",
    ):
        require(postgres, needle, "CloudNativePG TLS manifest")

    for needle in (
        "SSL_MODE: verify-full",
        "name: platform-internal-roots",
        "mountPath: /data/gitea/git/.postgresql",
        "name: SSL_CERT_FILE",
        "value: /etc/ssl/platform/ca-certificates.crt",
    ):
        require(forgejo, needle, "Forgejo PostgreSQL TLS values")
    for needle in (
        "name: platform-internal-roots",
        "mountPath: /etc/ssl/platform-postgres",
    ):
        require(woodpecker, needle, "Woodpecker PostgreSQL TLS values")
    for needle in (
        "sslmode=verify-full&sslrootcert=/etc/ssl/platform-postgres/ca-certificates.crt",
        "name: platform-internal-roots",
        "mountPath: /etc/ssl/platform-postgres",
    ):
        require(keycloak, needle, "Keycloak PostgreSQL TLS values")
    for needle in (
        "caBundleSecretName: platform-internal-roots",
        "sslmode: verify-full",
        "tlsOptions:\n      enable: true",
    ):
        require(harbor, needle, "Harbor PostgreSQL TLS values")
    require(harbor_kustomization, "ca-bundle-configmap-patch.yaml", "Harbor kustomization")
    require(harbor_kustomization, "harbor-(core|exporter|jobservice|registry)", "Harbor kustomization")
    require(harbor_kustomization, "ca-bundle-configmap-statefulset-patch.yaml", "Harbor kustomization")
    require(harbor_kustomization, "name: harbor-trivy", "Harbor kustomization")
    require(harbor_ca_patch, "configMap:\n            name: platform-internal-roots", "Harbor CA patch")
    forbid(harbor_ca_patch, "secretName: platform-internal-roots", "Harbor CA patch")
    require(
        harbor_ca_statefulset_patch,
        "configMap:\n            name: platform-internal-roots",
        "Harbor StatefulSet CA patch",
    )
    forbid(
        harbor_ca_statefulset_patch,
        "secretName: platform-internal-roots",
        "Harbor StatefulSet CA patch",
    )
    for needle in (
        "ssl_mode: verify-full",
        "ca_cert_path: /etc/ssl/platform-postgres/ca-certificates.crt",
        "configMap: platform-internal-roots",
    ):
        require(monitoring, needle, "Grafana PostgreSQL TLS values")

    for needle in (
        "- server-certificate.yaml",
    ):
        require(valkey_kustomization, needle, "Valkey kustomization")
    for needle in (
        "name: platform-valkey-server",
        "secretName: platform-valkey-tls",
        "rotationPolicy: Always",
        "platform-valkey-primary.platform-cache.svc.cluster.local",
        '"*.platform-valkey-headless.platform-cache.svc.cluster.local"',
        "name: platform-internal-ca",
    ):
        require(valkey_certificate, needle, "Valkey Certificate")
    for needle in (
        "tls:\n  enabled: true",
        "existingSecret: platform-valkey-tls",
        "requireClientCertificate: false",
        "tls-auto-reload-interval 300",
        "port 0",
        "tls-port 26379",
        "tls-replication yes",
        "check-ssl",
        "verify required",
        "ca-file /trust/ca-certificates.crt",
        "REDIS_ADDR: rediss://localhost:6379",
        "REDIS_EXPORTER_SKIP_TLS_VERIFICATION: \"false\"",
    ):
        require(valkey_values, needle, "Valkey TLS values")
    forbid(valkey_values, "REDIS_EXPORTER_SKIP_TLS_VERIFICATION: \"true\"", "Valkey TLS values")
    for chart_template in (valkey_statefulset, valkey_deployment):
        require(chart_template, "name: REDISCLI_AUTH", "Valkey workload template")
        require(
            chart_template,
            "--tls{{ if .Values.auth.enabled }} --user default --no-auth-warning{{ end }} ping",
            "Valkey workload template",
        )

    for needle in (
        "HARBOR_REDIS_TLS:-true",
        "FORGEJO_REDIS_TLS:-true",
        'scheme = "rediss"',
        "state=reconciled",
    ):
        require(secret_playbook, needle, "cache URI secret automation")

    for needle in (
        "openssl s_client",
        "-verify_hostname \"$OPENBAO_DNS\"",
        "-starttls postgres",
        "-verify_hostname \"$POSTGRES_DNS\"",
        "-verify_hostname \"$VALKEY_DNS\"",
        "certificate/platform-valkey-server",
        "valkey-cli --tls --cacert /tls/ca.crt",
        "reason=valkey-plaintext-listener-accepted-command",
        "valkey_tls=verified",
        "plaintext_disabled=true",
        "pg_stat_ssl",
        "database_clients=verified",
        "private-key-present-in-trust-bundle",
        "AGENT_INJECT_VAULT_CACERT_BYTES",
        "platform_internal_tls=verified",
    ):
        require(verifier, needle, "live internal TLS verifier")

    require(makefile, "platform-internal-tls-verify:", "Makefile")
    require(makefile, "@$(MAKE) platform-internal-tls-verify", "production readiness gate")
    require(readiness, "make platform-internal-tls-verify", "production readiness documentation")
    require(pki_doc, "platform-internal-root-ca", "internal PKI documentation")
    require(pki_doc, "SIGHUP", "internal PKI rotation documentation")

    print("Managed internal TLS contract passed for OpenBao, CloudNativePG, and Valkey.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
