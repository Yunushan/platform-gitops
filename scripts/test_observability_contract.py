#!/usr/bin/env python3
"""Validate authenticated, retained, and delivery-tested observability."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREMIUM = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required observability file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain {needle!r}")


def main() -> int:
    loki = read(PREMIUM / "loki/values.yaml")
    loki_kustomization = read(PREMIUM / "loki/kustomization.yaml")
    loki_chart = read(PREMIUM / "loki/charts/loki/Chart.yaml")
    alloy_chart = read(PREMIUM / "loki/charts/alloy/Chart.yaml")
    alloy = read(PREMIUM / "loki/alloy-values.yaml")
    monitoring = read(PREMIUM / "monitoring/values.yaml")
    secrets = read(ROOT / "ansible/playbooks/configure-platform-app-secrets.yml")
    verifier = read(ROOT / "ansible/playbooks/verify-platform-observability.yml")
    renderer = read(ROOT / "scripts/render_private_platform_values.py")
    makefile = read(ROOT / "Makefile")
    readiness = read(ROOT / "docs/PRODUCTION_READINESS.md")

    for needle in (
        "auth_enabled: true",
        "retention_period: 720h",
        "retention_enabled: true",
        "delete_request_store: s3",
        "existingSecret: loki-gateway-basic-auth",
        'locationSnippet: "proxy_set_header X-Scope-OrgID platform;"',
        "replicas: 3",
        "- -tenant-id=platform",
        "key: password",
    ):
        require(loki, needle, "Loki values")
    forbid(loki, "auth_enabled: false", "Loki values")

    for needle in (
        "helmGlobals:",
        "chartHome: charts",
        "name: loki",
        "name: alloy",
        "valuesFile: alloy-values.yaml",
    ):
        require(loki_kustomization, needle, "Loki kustomization")
    forbid(loki_kustomization, "repo:", "Loki kustomization")
    for chart, label, name, version in (
        (loki_chart, "Loki chart", "loki", "7.0.0"),
        (alloy_chart, "Alloy chart", "alloy", "1.11.0"),
    ):
        require(chart, f"name: {name}", label)
        require(chart, f"version: {version}", label)

    for needle in (
        'discovery.kubernetes "pods"',
        'loki.source.kubernetes "pod_logs"',
        'loki.write "platform"',
        'tenant_id = "platform"',
        'sys.env("LOKI_GATEWAY_PASSWORD")',
        "enabled: true",
        "type: deployment",
        "replicas: 2",
        "minAvailable: 1",
        "allowPrivilegeEscalation: false",
        "readOnlyRootFilesystem: true",
        "release: monitoring",
    ):
        require(alloy, needle, "Alloy values")
    for needle in ("hostPID: true", "privileged: true", "varlog: true", "dockercontainers: true"):
        forbid(alloy, needle, "Alloy values")

    for needle in (
        "useExistingSecret: true",
        "configSecret: alertmanager-platform-config",
        "name: platform-loki-client",
        "url: http://loki-gateway.logging.svc.cluster.local",
        "basicAuthUser: $__env{LOKI_GATEWAY_USERNAME}",
        "basicAuthPassword: $__env{LOKI_GATEWAY_PASSWORD}",
        "httpHeaderValue1: platform",
    ):
        require(monitoring, needle, "monitoring values")

    for needle in (
        "Generate or preserve Loki gateway and client credentials",
        "loki-gateway-basic-auth",
        "platform-loki-client",
        "Generate or preserve Alertmanager routing configuration",
        "ALERTMANAGER_WEBHOOK_URL",
        "ALERTMANAGER_CONFIG",
        "PLATFORM_ALERTMANAGER_REQUIRE_ROUTE",
        "alertmanager-platform-config",
    ):
        require(secrets, needle, "platform app secret automation")

    for needle in (
        "auth_enabled: true",
        "retention_period:",
        "existingSecret: loki-gateway-basic-auth",
        "configSecret: alertmanager-platform-config",
        "platform-loki-client",
        "LOKI_RETENTION_PERIOD",
    ):
        require(renderer, needle, "private values renderer")

    for needle in (
        "unauth_code",
        'test "$unauth_code" = "401"',
        "platform-observability-proof",
        'query={namespace="logging",app="alloy"}',
        "amtool check-config",
        "alertmanager_notifications_total",
        "alertmanager_notifications_failed_total",
        "platform_observability=verified",
    ):
        require(verifier, needle, "live observability verifier")

    require(makefile, "platform-observability-verify:", "Makefile")
    require(
        makefile,
        "PLATFORM_ALERT_DELIVERY_TEST=true $(MAKE) platform-observability-verify",
        "production readiness gate",
    )
    require(readiness, "make platform-observability-verify", "production readiness documentation")

    print("Production observability contract passed for Loki, Alloy, Grafana, and Alertmanager.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
