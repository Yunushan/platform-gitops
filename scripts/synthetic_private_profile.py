#!/usr/bin/env python3
"""Build a complete, non-secret premium profile for render/schema verification."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from atomic_file import atomic_write_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from subprocess_timeout import bounded_timeout_seconds


ROOT = Path(__file__).resolve().parents[1]
RENDER_TIMEOUT_SECONDS = 600
RENDERER_ENV_PREFIXES = (
    "PLATFORM_",
    "RKE2_",
    "FORGEJO_",
    "LONGHORN_",
    "WOODPECKER_",
    "HARBOR_",
    "MONITORING_",
    "PROMETHEUS_",
    "ALERTMANAGER_",
    "GRAFANA_",
    "OBJECT_STORAGE_",
    "LOKI_",
    "BACKUP_",
    "VELERO_",
    "CNPG_",
    "POSTGRES_",
    "MINIO_",
    "KEYCLOAK_",
    "STEP_CA_",
)
TEST_COSIGN_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6QsNef3SKYhJVYSVj+ZfbPwJd0pv
DLYNHXITZkhIzfE+apcxDjCCkDPcJ3A3zvhPATYOIsCxYPch7Q2JdJLsDQ==
-----END PUBLIC KEY-----
"""
TEST_INVENTORY = """[rke2_servers:vars]
platform_argocd_host=argocd.example.test
platform_git_host=git.example.test
platform_ci_host=ci.example.test
platform_registry_host=registry.example.test
platform_keycloak_host=sso.example.test
platform_grafana_host=grafana.example.test
platform_prometheus_host=prometheus.example.test
platform_loki_host=loki.example.test
platform_step_ca_host=ca.example.test
"""


def synthetic_environment(cosign_public_key: Path) -> dict[str, str]:
    """Return deterministic, non-secret values that resolve every premium placeholder."""

    return {
        "FORGEJO_DATA_SIZE": "21Gi",
        "FORGEJO_STORAGE_CLASS": "longhorn-critical-encrypted",
        "FORGEJO_IMAGE_TAG": "15.0.3-rootless",
        "LONGHORN_BACKUP_TARGET": "s3://platform-test-longhorn@eu-test-1/",
        "LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME": "longhorn-backup-test",
        "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE": "275",
        "WOODPECKER_DATA_SIZE": "11Gi",
        "WOODPECKER_STORAGE_CLASS": "longhorn-standard-encrypted",
        "WOODPECKER_ADMIN_USERS": "platform-admin",
        "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME": "woodpecker-oauth-test",
        "WOODPECKER_AGENT_SECRET_NAME": "woodpecker-agent-test",
        "WOODPECKER_IMAGE_TAG": "3.16.0",
        "WOODPECKER_DATABASE_MODE": "postgres",
        "WOODPECKER_DATABASE_SECRET_NAME": "woodpecker-db-test",
        "WOODPECKER_SERVER_REPLICAS": "3",
        "WOODPECKER_AGENT_REPLICAS": "3",
        "HARBOR_STORAGE_CLASS": "longhorn-critical-encrypted",
        "HARBOR_REGISTRY_SIZE": "55Gi",
        "HARBOR_JOBLOG_SIZE": "6Gi",
        "HARBOR_DATABASE_SIZE": "12Gi",
        "HARBOR_REDIS_SIZE": "7Gi",
        "HARBOR_TRIVY_SIZE": "13Gi",
        "HARBOR_ADMIN_SECRET_NAME": "harbor-admin-test",
        "HARBOR_SECRET_KEY_SECRET_NAME": "harbor-secret-key-test",
        "HARBOR_REPLICAS": "2",
        "HARBOR_DATABASE_SECRET_NAME": "harbor-db-test",
        "HARBOR_S3_SECRET_NAME": "harbor-s3-test",
        "MONITORING_STORAGE_CLASS": "longhorn-standard-encrypted",
        "PROMETHEUS_RETENTION_SIZE": "22GB",
        "PROMETHEUS_DATA_SIZE": "60Gi",
        "ALERTMANAGER_DATA_SIZE": "12Gi",
        "GRAFANA_DATA_SIZE": "14Gi",
        "GRAFANA_ADMIN_SECRET_NAME": "grafana-admin-test",
        "GRAFANA_REPLICAS": "2",
        "GRAFANA_DATABASE_SECRET_NAME": "grafana-db-test",
        "OBJECT_STORAGE_ENDPOINT": "https://object.example.test",
        "OBJECT_STORAGE_REGION": "eu-test-1",
        "OBJECT_STORAGE_BUCKET_PREFIX": "platform-test",
        "OBJECT_STORAGE_FORCE_PATH_STYLE": "true",
        "OBJECT_STORAGE_INSECURE": "false",
        "LOKI_OBJECT_STORAGE_SECRET_NAME": "loki-object-test",
        "LOKI_CHUNKS_BUCKET": "platform-test-loki-chunks",
        "LOKI_RULER_BUCKET": "platform-test-loki-ruler",
        "LOKI_ADMIN_BUCKET": "platform-test-loki-admin",
        "LOKI_WRITE_CACHE_SIZE": "24Gi",
        "LOKI_BACKEND_CACHE_SIZE": "26Gi",
        "LOKI_STORAGE_CLASS": "longhorn-standard-encrypted",
        "BACKUP_PROVIDER": "aws",
        "BACKUP_BUCKET": "platform-test-velero",
        "VELERO_CREDENTIALS_SECRET_NAME": "velero-cloud-test",
        "VELERO_DAILY_BACKUP_CRON": "15 2 * * *",
        "CNPG_OBJECT_STORE_SECRET_NAME": "cnpg-object-test",
        "CNPG_BACKUP_DESTINATION": "s3://platform-test-cnpg/platform-postgres",
        "CNPG_BACKUP_SCHEDULE": "20 2 * * *",
        "CNPG_BACKUP_ENABLED": "true",
        "CNPG_STORAGE_CLASS": "longhorn-critical-encrypted",
        "POSTGRES_DATA_SIZE": "80Gi",
        "PLATFORM_VALKEY_AUTH_SECRET_NAME": "platform-valkey-test",
        "PLATFORM_VALKEY_PASSWORD_KEY": "valkey-password-test",
        "PLATFORM_VALKEY_REPLICA_COUNT": "3",
        "PLATFORM_VALKEY_DATA_SIZE": "9Gi",
        "PLATFORM_VALKEY_STORAGE_CLASS": "longhorn-critical-encrypted",
        "MINIO_ROOT_SECRET_NAME": "minio-root-test",
        "MINIO_ROOT_USER_SECRET_KEY": "root-user-test",
        "MINIO_ROOT_PASSWORD_SECRET_KEY": "root-password-test",
        "MINIO_REPLICA_COUNT": "4",
        "MINIO_ZONES": "1",
        "MINIO_DRIVES_PER_NODE": "1",
        "MINIO_DATA_SIZE": "64Gi",
        "MINIO_STORAGE_CLASS": "longhorn-critical-encrypted",
        "KEYCLOAK_ADMIN_SECRET_NAME": "keycloak-admin-test",
        "KEYCLOAK_DATABASE_SECRET_NAME": "keycloak-db-test",
        "KEYCLOAK_DATABASE_HOST": "platform-postgres-rw.platform-databases.svc.cluster.local",
        "KEYCLOAK_DATABASE_NAME": "keycloak",
        "KEYCLOAK_DATABASE_USER": "keycloak",
        "KEYCLOAK_REPLICAS": "2",
        "KEYCLOAK_STORAGE_CLASS": "longhorn-critical-encrypted",
        "KEYCLOAK_IMAGE_REGISTRY": "quay.io",
        "KEYCLOAK_IMAGE_REPOSITORY": "keycloak/keycloak",
        "KEYCLOAK_IMAGE_TAG": "26.7.0",
        "KEYCLOAK_CONFIG_CLI_IMAGE_REGISTRY": "quay.io",
        "KEYCLOAK_CONFIG_CLI_IMAGE_REPOSITORY": "adorsys/keycloak-config-cli",
        "KEYCLOAK_CONFIG_CLI_IMAGE_TAG": "6.5.1",
        "PLATFORM_SSO_BOOTSTRAP_ADMIN_USERNAME": "platform-bootstrap-test",
        "STEP_CA_MODE": "bootstrap",
        "STEP_CA_NAME": "Platform Test CA",
        "STEP_CA_DNS_NAMES": "ca.example.test,step-ca.step-ca.svc.cluster.local",
        "STEP_CA_URL": "https://ca.example.test",
        "STEP_CA_STORAGE_CLASS": "longhorn-critical-encrypted",
        "STEP_CA_DB_SIZE": "9Gi",
        "PLATFORM_IMAGE_INTEGRITY_MODE": "Audit",
        "PLATFORM_COSIGN_PUBLIC_KEY_FILE": str(cosign_public_key),
        "PLATFORM_COSIGN_REKOR_URL": "https://rekor.example.test",
    }


def sanitized_environment(values: dict[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(RENDERER_ENV_PREFIXES)
    }
    environment.update(values)
    return environment


def prepare_synthetic_private_profile(
    destination: Path,
    *,
    source_root: Path = ROOT,
    environment_overrides: dict[str, str] | None = None,
) -> str:
    """Copy and render the real premium profile using deterministic safe values."""

    destination = destination.resolve()
    source_root = source_root.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source_root / "gitops", destination / "gitops")

    inventory = destination / "inventory/hosts.local.ini"
    inventory.parent.mkdir(parents=True)
    atomic_write_text(inventory, TEST_INVENTORY)
    public_key = destination / "private/cosign.pub"
    public_key.parent.mkdir(parents=True)
    atomic_write_text(public_key, TEST_COSIGN_PUBLIC_KEY)

    values = synthetic_environment(public_key)
    if environment_overrides:
        values.update(environment_overrides)
    values["PLATFORM_COSIGN_PUBLIC_KEY_FILE"] = str(public_key)
    try:
        timeout = bounded_timeout_seconds(
            RENDER_TIMEOUT_SECONDS,
            "PLATFORM_RENDER_COMMAND_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    try:
        result = run_bounded(
            [sys.executable, str(source_root / "scripts/render_private_platform_values.py")],
            cwd=destination,
            env=sanitized_environment(values),
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"synthetic premium profile rendering timed out after {timeout:g} seconds"
        ) from None
    except (BoundedSubprocessError, ValueError) as exc:
        raise RuntimeError(f"synthetic premium profile output rejected: {exc}") from None
    if result.returncode != 0:
        raise RuntimeError(
            "synthetic premium profile rendering failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout
