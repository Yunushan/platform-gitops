#!/usr/bin/env python3
"""Self-test private platform value rendering."""
from __future__ import annotations

from contextlib import contextmanager
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = ROOT / "scripts/render_private_platform_values.py"
CHECKER_PATH = ROOT / "scripts/check_gitops_profile.py"
PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
sys.dont_write_bytecode = True
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


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_private_platform_values", RENDERER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {RENDERER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_checker():
    spec = importlib.util.spec_from_file_location("check_gitops_profile", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def patched_env(values: dict[str, str]):
    managed_keys = set(values)
    managed_keys.update(
        key
        for key in os.environ
        if key.startswith(RENDERER_ENV_PREFIXES)
    )
    previous = {key: os.environ.get(key) for key in managed_keys}
    try:
        for key in managed_keys:
            os.environ.pop(key, None)
        os.environ.update(values)
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def assert_no_placeholders(paths: list[Path]) -> None:
    findings: list[str] = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if PLACEHOLDER_RE.search(line):
                findings.append(f"{path}:{line_number}: {line.strip()}")
    if findings:
        raise AssertionError("rendered private values still contain placeholders:\n" + "\n".join(findings))


def assert_contains(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    missing = [needle for needle in needles if needle not in text]
    if missing:
        raise AssertionError(f"{path} is missing expected text: {', '.join(missing)}")


def assert_not_contains(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    present = [needle for needle in needles if needle in text]
    if present:
        raise AssertionError(f"{path} contains unexpected text: {', '.join(present)}")


def render_real_premium_profile(renderer, checker, env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="platform-real-premium-render-") as tmp:
        repo = Path(tmp)
        shutil.copytree(
            ROOT / "gitops/clusters/rke2-main",
            repo / "gitops/clusters/rke2-main",
        )
        inventory_path = write(
            repo / "inventory/hosts.local.ini",
            """[rke2_servers:vars]
platform_argocd_host=argocd.example.test
platform_git_host=git.example.test
platform_ci_host=ci.example.test
platform_registry_host=registry.example.test
platform_keycloak_host=sso.example.test
platform_grafana_host=grafana.example.test
platform_prometheus_host=prometheus.example.test
platform_loki_host=loki.example.test
platform_step_ca_host=ca.example.test
""",
        )
        inventory = renderer.read_inventory_vars(inventory_path)
        premium = repo / "gitops/clusters/rke2-main/premium-3node/apps"

        with patched_env(env):
            renderer.render_argocd(premium / "argocd-ha/values.yaml", inventory)
            renderer.render_forgejo(premium / "forgejo/values.yaml", inventory)
            renderer.render_longhorn(premium / "longhorn/values.yaml", os.environ["LONGHORN_BACKUP_TARGET"])
            renderer.render_woodpecker(premium / "woodpecker/values.yaml", inventory)
            renderer.render_harbor(premium / "harbor/values.yaml", inventory)
            renderer.render_monitoring(premium / "monitoring/values.yaml", inventory)
            renderer.render_loki(premium / "loki/values.yaml", inventory)
            renderer.render_velero(premium / "velero/values.yaml")
            renderer.render_platform_valkey(premium / "platform-valkey/values.yaml")
            renderer.render_minio(premium / "minio/values.yaml")
            renderer.render_keycloak(premium / "keycloak/values.yaml", inventory)
            renderer.render_step_ca(premium / "step-ca/values.yaml", inventory)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = checker.check_profile(repo, "premium-3node")
        if rc != 0:
            raise AssertionError(
                "rendered real premium profile did not pass profile check:\n"
                + stdout.getvalue()
                + stderr.getvalue()
            )


def main() -> int:
    renderer = load_renderer()
    checker = load_checker()

    with tempfile.TemporaryDirectory(prefix="platform-private-render-") as tmp:
        repo = Path(tmp)
        inventory_path = write(
            repo / "inventory/hosts.local.ini",
            """[rke2_servers:vars]
platform_argocd_host=argocd.example.test
platform_git_host=git.example.test
platform_ci_host=ci.example.test
platform_registry_host=registry.example.test
platform_keycloak_host=sso.example.test
platform_grafana_host=grafana.example.test
platform_prometheus_host=prometheus.example.test
platform_loki_host=loki.example.test
platform_step_ca_host=ca.example.test
""",
        )
        inventory = renderer.read_inventory_vars(inventory_path)

        paths = {
            "argocd": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/values.yaml",
                "server:\n  ingress:\n    hostname: argocd.<PLATFORM_DOMAIN>\n",
            ),
            "longhorn": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/values.yaml",
                'defaultSettings:\n  backupTarget: "<LONGHORN_BACKUP_TARGET>"\n',
            ),
            "forgejo": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml"),
            "woodpecker": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"),
            "harbor": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/harbor/values.yaml"),
            "monitoring": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/monitoring/values.yaml"),
            "loki": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/loki/values.yaml"),
            "velero": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/velero/values.yaml"),
            "cnpg": write(
                repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-postgres/postgres-cluster.yaml"
            ),
            "valkey": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/values.yaml"),
            "minio": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/minio/values.yaml"),
            "keycloak": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/keycloak/values.yaml"),
            "step_ca": write(repo / "gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml"),
        }

        env = {
            "FORGEJO_DATA_SIZE": "21Gi",
            "FORGEJO_STORAGE_CLASS": "longhorn-critical",
            "FORGEJO_IMAGE_TAG": "15.0.3-rootless",
            "LONGHORN_BACKUP_TARGET": "s3://platform-test-longhorn@eu-test-1/",
            "WOODPECKER_DATA_SIZE": "11Gi",
            "WOODPECKER_STORAGE_CLASS": "longhorn-standard",
            "WOODPECKER_ADMIN_USERS": "platform-admin",
            "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME": "woodpecker-oauth-test",
            "WOODPECKER_IMAGE_TAG": "3.16.0",
            "WOODPECKER_DATABASE_MODE": "postgres",
            "WOODPECKER_DATABASE_SECRET_NAME": "woodpecker-db-test",
            "WOODPECKER_SERVER_REPLICAS": "3",
            "WOODPECKER_AGENT_REPLICAS": "3",
            "HARBOR_STORAGE_CLASS": "longhorn-critical",
            "HARBOR_REGISTRY_SIZE": "55Gi",
            "HARBOR_JOBLOG_SIZE": "6Gi",
            "HARBOR_DATABASE_SIZE": "12Gi",
            "HARBOR_REDIS_SIZE": "7Gi",
            "HARBOR_TRIVY_SIZE": "13Gi",
            "HARBOR_ADMIN_SECRET_NAME": "harbor-admin-test",
            "HARBOR_SECRET_KEY_SECRET_NAME": "harbor-secret-key-test",
            "MONITORING_STORAGE_CLASS": "longhorn-standard",
            "PROMETHEUS_RETENTION_SIZE": "22GB",
            "PROMETHEUS_DATA_SIZE": "60Gi",
            "ALERTMANAGER_DATA_SIZE": "12Gi",
            "GRAFANA_DATA_SIZE": "14Gi",
            "GRAFANA_ADMIN_SECRET_NAME": "grafana-admin-test",
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
            "LOKI_STORAGE_CLASS": "longhorn-standard",
            "BACKUP_PROVIDER": "aws",
            "BACKUP_BUCKET": "platform-test-velero",
            "VELERO_CREDENTIALS_SECRET_NAME": "velero-cloud-test",
            "VELERO_DAILY_BACKUP_CRON": "15 2 * * *",
            "CNPG_OBJECT_STORE_SECRET_NAME": "cnpg-object-test",
            "CNPG_BACKUP_DESTINATION": "s3://platform-test-cnpg/platform-postgres",
            "CNPG_BACKUP_SCHEDULE": "20 2 * * *",
            "CNPG_BACKUP_ENABLED": "true",
            "CNPG_STORAGE_CLASS": "longhorn-critical",
            "POSTGRES_DATA_SIZE": "80Gi",
            "PLATFORM_VALKEY_AUTH_SECRET_NAME": "platform-valkey-test",
            "PLATFORM_VALKEY_PASSWORD_KEY": "valkey-password-test",
            "PLATFORM_VALKEY_REPLICA_COUNT": "3",
            "PLATFORM_VALKEY_DATA_SIZE": "9Gi",
            "PLATFORM_VALKEY_STORAGE_CLASS": "longhorn-critical",
            "MINIO_ROOT_SECRET_NAME": "minio-root-test",
            "MINIO_ROOT_USER_SECRET_KEY": "root-user-test",
            "MINIO_ROOT_PASSWORD_SECRET_KEY": "root-password-test",
            "MINIO_REPLICA_COUNT": "4",
            "MINIO_ZONES": "1",
            "MINIO_DRIVES_PER_NODE": "1",
            "MINIO_DATA_SIZE": "64Gi",
            "MINIO_STORAGE_CLASS": "longhorn-critical",
            "KEYCLOAK_ADMIN_SECRET_NAME": "keycloak-admin-test",
            "KEYCLOAK_DATABASE_SECRET_NAME": "keycloak-db-test",
            "KEYCLOAK_DATABASE_HOST": "platform-postgres-rw.platform-databases.svc.cluster.local",
            "KEYCLOAK_DATABASE_NAME": "keycloak",
            "KEYCLOAK_DATABASE_USER": "keycloak",
            "KEYCLOAK_REPLICAS": "2",
            "KEYCLOAK_STORAGE_CLASS": "longhorn-critical",
            "STEP_CA_MODE": "bootstrap",
            "STEP_CA_NAME": "Platform Test CA",
            "STEP_CA_DNS_NAMES": "ca.example.test,step-ca.step-ca.svc.cluster.local",
            "STEP_CA_URL": "https://ca.example.test",
            "STEP_CA_STORAGE_CLASS": "longhorn-critical",
            "STEP_CA_DB_SIZE": "9Gi",
        }

        with patched_env(env):
            renderer.render_argocd(paths["argocd"], inventory)
            renderer.render_forgejo(paths["forgejo"], inventory)
            renderer.render_longhorn(paths["longhorn"], os.environ["LONGHORN_BACKUP_TARGET"])
            renderer.render_woodpecker(paths["woodpecker"], inventory)
            renderer.render_harbor(paths["harbor"], inventory)
            renderer.render_monitoring(paths["monitoring"], inventory)
            renderer.render_loki(paths["loki"], inventory)
            renderer.render_velero(paths["velero"])
            renderer.render_cnpg_postgres_cluster(paths["cnpg"])
            renderer.render_platform_valkey(paths["valkey"])
            renderer.render_minio(paths["minio"])
            renderer.render_keycloak(paths["keycloak"], inventory)
            renderer.render_step_ca(paths["step_ca"], inventory)

        rendered_paths = list(paths.values())
        assert_no_placeholders(rendered_paths)
        assert_contains(paths["argocd"], "argocd.example.test")
        assert_contains(
            paths["forgejo"],
            "git.example.test",
            "21Gi",
            'tag: "15.0.3-rootless"',
            "DB_TYPE: postgres",
            'HOST: "platform-postgres-rw.platform-databases.svc.cluster.local:5432"',
            "GITEA__cache__HOST",
            'name: "forgejo-redis"',
            "ADAPTER: redis",
            "TYPE: redis",
        )

        sqlite_forgejo_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/sqlite-values.yaml")
        sqlite_forgejo_env = dict(env)
        sqlite_forgejo_env["FORGEJO_DATABASE_MODE"] = "sqlite"
        with patched_env(sqlite_forgejo_env):
            renderer.render_forgejo(sqlite_forgejo_path, inventory)
        assert_contains(sqlite_forgejo_path, "git.example.test", "sqlite3", 'tag: "15.0.3-rootless"')
        assert_not_contains(sqlite_forgejo_path, "additionalConfigFromEnvs:", "DB_TYPE: postgres")

        external_forgejo_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/external-values.yaml")
        external_forgejo_env = dict(env)
        external_forgejo_env.update(
            {
                "FORGEJO_DATABASE_MODE": "external",
                "FORGEJO_DATABASE_HOST": "forgejo-postgres.example.test:5432",
                "FORGEJO_DATABASE_NAME": "forgejo",
                "FORGEJO_DATABASE_USER": "forgejo",
                "FORGEJO_DATABASE_SECRET_NAME": "forgejo-db-test",
                "FORGEJO_DATABASE_SSL_MODE": "require",
                "FORGEJO_REDIS_MODE": "redis",
                "FORGEJO_REDIS_SECRET_NAME": "forgejo-redis-test",
            }
        )
        with patched_env(external_forgejo_env):
            renderer.render_forgejo(external_forgejo_path, inventory)
        assert_contains(
            external_forgejo_path,
            "additionalConfigFromEnvs:",
            "GITEA__database__PASSWD",
            'name: "forgejo-db-test"',
            "key: password",
            "GITEA__cache__HOST",
            "GITEA__queue__CONN_STR",
            'name: "forgejo-redis-test"',
            "key: uri",
            "DB_TYPE: postgres",
            'HOST: "forgejo-postgres.example.test:5432"',
            'SSL_MODE: "require"',
        )
        assert_not_contains(external_forgejo_path, "FORGEJO_REDIS_URL", "redis://")

        mysql_forgejo_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/mysql-values.yaml")
        mysql_forgejo_env = dict(env)
        mysql_forgejo_env.update(
            {
                "FORGEJO_DATABASE_MODE": "mariadb",
                "FORGEJO_DATABASE_HOST": "forgejo-mariadb.example.test:3306",
                "FORGEJO_DATABASE_NAME": "forgejo",
                "FORGEJO_DATABASE_USER": "forgejo",
                "FORGEJO_DATABASE_SECRET_NAME": "forgejo-mariadb-test",
            }
        )
        with patched_env(mysql_forgejo_env):
            renderer.render_forgejo(mysql_forgejo_path, inventory)
        assert_contains(
            mysql_forgejo_path,
            "DB_TYPE: mysql",
            'HOST: "forgejo-mariadb.example.test:3306"',
            'name: "forgejo-mariadb-test"',
        )
        assert_contains(paths["longhorn"], "s3://platform-test-longhorn@eu-test-1/")
        assert_contains(
            paths["cnpg"],
            'namespace: "platform-databases"',
            'size: "80Gi"',
            'storageClass: "longhorn-critical"',
            'database: "forgejo"',
            'owner: "forgejo"',
            'name: "forgejo-database"',
            'name: "woodpecker"',
            'name: "woodpecker-db-test"',
            'destinationPath: "s3://platform-test-cnpg/platform-postgres"',
            'endpointURL: "https://object.example.test"',
            'name: "cnpg-object-test"',
            "key: ACCESS_KEY_ID",
            "key: SECRET_ACCESS_KEY",
            'schedule: "20 2 * * *"',
        )
        assert_contains(
            paths["valkey"],
            'existingSecret: "platform-valkey-test"',
            'existingSecretPasswordKey: "valkey-password-test"',
            'storageClass: "longhorn-critical"',
            'size: "9Gi"',
            "replicaCount: 3",
            "sentinel:\n  enabled: true",
            "createPrimary: true",
            "serviceMonitor:\n    enabled: true",
        )
        assert_contains(
            paths["minio"],
            'existingSecret: "minio-root-test"',
            'rootUserSecretKey: "root-user-test"',
            'rootPasswordSecretKey: "root-password-test"',
            "mode: distributed",
            "replicaCount: 4",
            "zones: 1",
            "drivesPerNode: 1",
            'storageClass: "longhorn-critical"',
            'size: "64Gi"',
            "prometheusAuthType: public",
            "serviceMonitor:\n    enabled: true",
        )
        assert_contains(
            paths["keycloak"],
            "sso.example.test",
            'existingSecret: "keycloak-admin-test"',
            'passwordSecretKey: "admin-password"',
            "replicaCount: 2",
            'host: "platform-postgres-rw.platform-databases.svc.cluster.local"',
            'user: "keycloak"',
            'database: "keycloak"',
            'existingSecret: "keycloak-db-test"',
            'defaultStorageClass: "longhorn-critical"',
            "serviceMonitor:\n    enabled: true",
        )
        invalid_keycloak_env = dict(env, KEYCLOAK_REPLICAS="1")
        with patched_env(invalid_keycloak_env):
            try:
                renderer.render_keycloak(paths["keycloak"], inventory)
            except SystemExit as exc:
                if "KEYCLOAK_REPLICAS must be at least 2" not in str(exc):
                    raise AssertionError(f"unexpected Keycloak replica validation error: {exc}") from exc
            else:
                raise AssertionError("Keycloak renderer accepted a single premium replica")
        invalid_minio_env = dict(env, MINIO_REPLICA_COUNT="3")
        with patched_env(invalid_minio_env):
            try:
                renderer.render_minio(paths["minio"])
            except SystemExit as exc:
                if "distributed MinIO" not in str(exc):
                    raise AssertionError(f"unexpected MinIO replica validation error: {exc}") from exc
            else:
                raise AssertionError("MinIO renderer accepted a non-distributed replica count")
        assert_contains(paths["woodpecker"], "ci.example.test", "woodpecker-oauth-test")
        assert_contains(
            paths["woodpecker"],
            'WOODPECKER_HOST: "https://ci.example.test"',
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            'WOODPECKER_SERVER_ADDR: ":8000"',
            'WOODPECKER_GRPC_ADDR: ":9000"',
            'WOODPECKER_LOG_LEVEL: "debug"',
            "failureThreshold: 30",
            '"woodpecker-db-test"',
            "replicaCount: 3",
            "repository: woodpeckerci/woodpecker-server",
            "repository: woodpeckerci/woodpecker-agent",
            'tag: "v3.16.0"',
            'WOODPECKER_BACKEND_K8S_STORAGE_CLASS: "longhorn-standard"',
        )
        assert_contains(paths["harbor"], "registry.example.test", "harbor-admin-test", "55Gi")
        assert_contains(
            paths["harbor"],
            'externalURL: "https://registry.example.test"',
            'storageClass: "longhorn-critical"',
            'size: "55Gi"',
            "portal:\n  replicas: 1\n  resources:",
            "core:\n  replicas: 1\n  resources:",
            "jobservice:\n  replicas: 1\n  resources:",
            "registry:\n  replicas: 1\n  registry:\n    resources:",
            "trivy:\n  enabled: true\n  replicas: 1\n  resources:",
            "exporter:\n  resources:",
            "database:\n  type: internal\n  internal:\n    resources:",
            "redis:\n  type: external",
            'addr: "platform-valkey-primary.platform-cache.svc.cluster.local:6379"',
            'existingSecret: "harbor-redis"',
        )

        external_harbor_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/harbor/external-values.yaml")
        external_harbor_env = {
            "HARBOR_STORAGE_CLASS": "longhorn-critical",
            "HARBOR_TLS_CERT_SOURCE": "secret",
            "HARBOR_TLS_SECRET_NAME": "harbor-wildcard-test",
            "HARBOR_DATABASE_MODE": "external",
            "HARBOR_DATABASE_HOST": "harbor-postgres.example.test",
            "HARBOR_DATABASE_PORT": "5432",
            "HARBOR_DATABASE_NAME": "registry",
            "HARBOR_DATABASE_USER": "harbor",
            "HARBOR_DATABASE_SECRET_NAME": "harbor-db-test",
            "HARBOR_REDIS_MODE": "external",
            "HARBOR_REDIS_ADDR": "harbor-redis.example.test:6379",
            "HARBOR_REDIS_USERNAME": "harbor",
            "HARBOR_REDIS_SECRET_NAME": "harbor-redis-test",
            "HARBOR_STORAGE_MODE": "s3",
            "HARBOR_S3_BUCKET": "platform-test-harbor-registry",
            "HARBOR_S3_SECRET_NAME": "harbor-s3-test",
            "OBJECT_STORAGE_ENDPOINT": "https://object.example.test",
            "OBJECT_STORAGE_REGION": "eu-test-1",
        }
        with patched_env(external_harbor_env):
            renderer.render_harbor(external_harbor_path, inventory)
        assert_contains(
            external_harbor_path,
            "Uses external PostgreSQL, external Redis, and S3-compatible registry storage",
            'certSource: "secret"',
            'secretName: "harbor-wildcard-test"',
            "imageChartStorage:\n    disableredirect: true\n    type: s3",
            'bucket: "platform-test-harbor-registry"',
            'regionendpoint: "https://object.example.test"',
            'existingSecret: "harbor-s3-test"',
            "database:\n  type: external",
            'host: "harbor-postgres.example.test"',
            'existingSecret: "harbor-db-test"',
            "redis:\n  type: external",
            'addr: "harbor-redis.example.test:6379"',
            'username: "harbor"',
            'existingSecret: "harbor-redis-test"',
        )
        assert_not_contains(
            external_harbor_path,
            "database:\n  type: internal",
            "redis:\n  type: internal",
            "type: filesystem",
        )

        internal_harbor_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/harbor/internal-values.yaml")
        internal_harbor_env = {
            "HARBOR_DATABASE_MODE": "internal",
            "HARBOR_REDIS_MODE": "internal",
            "HARBOR_STORAGE_MODE": "filesystem",
        }
        with patched_env(internal_harbor_env):
            renderer.render_harbor(internal_harbor_path, inventory)
        assert_contains(
            internal_harbor_path,
            "Uses internal PostgreSQL, internal Redis, and filesystem registry storage",
            "database:\n  type: internal\n  internal:\n    resources:",
            "redis:\n  type: internal\n  internal:\n    resources:",
            "imageChartStorage:\n    type: filesystem",
        )

        assert_contains(
            paths["monitoring"],
            "crds:\n  enabled: true",
            "grafana.example.test",
            "prometheus.example.test",
            "60Gi",
            "prometheusSpec:\n    replicas: 2\n    retention: 15d",
            "    resources:\n      requests:\n        cpu: 500m\n        memory: 2Gi",
        )
        assert_contains(
            paths["monitoring"],
            'storageClassName: "longhorn-standard"',
            'storage: "60Gi"',
            'size: "14Gi"',
            "alertmanagerSpec:\n    replicas: 3\n    resources:",
            "grafana:\n  replicas: 1\n  admin:",
            'existingSecret: "grafana-admin-test"',
            "userKey: admin-user",
            "passwordKey: admin-password",
        )

        external_monitoring_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/monitoring/external-values.yaml")
        external_monitoring_env = {
            "MONITORING_STORAGE_CLASS": "longhorn-standard",
            "GRAFANA_ADMIN_SECRET_NAME": "grafana-admin-test",
            "GRAFANA_DATABASE_MODE": "postgres",
            "GRAFANA_DATABASE_HOST": "grafana-postgres.example.test",
            "GRAFANA_DATABASE_PORT": "5432",
            "GRAFANA_DATABASE_NAME": "grafana",
            "GRAFANA_DATABASE_USER": "grafana",
            "GRAFANA_DATABASE_SECRET_NAME": "grafana-db-test",
            "GRAFANA_DATABASE_SSL_MODE": "require",
        }
        with patched_env(external_monitoring_env):
            renderer.render_monitoring(external_monitoring_path, inventory)
        assert_contains(
            external_monitoring_path,
            "Uses external PostgreSQL for Grafana state",
            "envValueFrom:\n    GF_DATABASE_PASSWORD:",
            'name: "grafana-db-test"',
            "grafana.ini:\n    database:\n      type: postgres",
            'host: "grafana-postgres.example.test:5432"',
            'name: "grafana"',
            'user: "grafana"',
            'password: "$__env{GF_DATABASE_PASSWORD}"',
            'ssl_mode: "require"',
        )
        assert_contains(
            paths["loki"],
            "loki.example.test",
            "platform-test-loki-chunks",
            "loki-object-test",
            "${LOKI_S3_ACCESS_KEY_ID}",
            "${LOKI_S3_SECRET_ACCESS_KEY}",
        )
        assert_contains(
            paths["loki"],
            'endpoint: "https://object.example.test"',
            'chunks: "platform-test-loki-chunks"',
            'storageClass: "longhorn-standard"',
            "write:\n  replicas: 3\n  resources:",
            "read:\n  replicas: 3\n  resources:",
            "backend:\n  replicas: 3\n  resources:",
            "gateway:\n  enabled: true\n  resources:",
            "      cpu: 500m\n      memory: 1Gi",
        )
        assert_contains(
            paths["velero"],
            "platform-test-velero",
            "https://object.example.test",
            "velero-cloud-test",
            'schedule: "15 2 * * *"',
        )
        assert_contains(
            paths["velero"],
            'provider: "aws"',
            'bucket: "platform-test-velero"',
            'existingSecret: "velero-cloud-test"',
            "deployNodeAgent: true\n\nresources:",
            "nodeAgent:\n  resources:",
            "      cpu: 250m\n      memory: 256Mi",
        )
        assert_contains(
            paths["step_ca"],
            "Platform Test CA",
            "ca.example.test",
            'size: "9Gi"',
            "service:\n  type: ClusterIP\n  port: 443\n  targetPort: 9000",
            "address: :9000",
            "accessModes:\n      - ReadWriteOnce",
            "ssh:\n    enabled: false",
            "autocert:\n  enabled: false",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi",
        )

        render_real_premium_profile(renderer, checker, env)

        sqlite_woodpecker_path = write(repo / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/sqlite-values.yaml")
        sqlite_env = {
            "WOODPECKER_DATA_SIZE": "11Gi",
            "WOODPECKER_STORAGE_CLASS": "longhorn-standard",
            "WOODPECKER_ADMIN_USERS": "platform-admin",
            "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME": "woodpecker-oauth-test",
            "WOODPECKER_IMAGE_TAG": "3.16.0",
            "WOODPECKER_DATABASE_MODE": "sqlite",
            "WOODPECKER_AGENT_REPLICAS": "3",
        }
        with patched_env(sqlite_env):
            renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
        assert_contains(
            sqlite_woodpecker_path,
            'WOODPECKER_HOST: "https://ci.example.test"',
            'WOODPECKER_SERVER_ADDR: ":8000"',
            'WOODPECKER_GRPC_ADDR: ":9000"',
            "failureThreshold: 30",
            "replicaCount: 1",
            "repository: woodpeckerci/woodpecker-server",
            "repository: woodpeckerci/woodpecker-agent",
            'tag: "v3.16.0"',
            '"woodpecker-oauth-test"',
        )
        assert_not_contains(
            sqlite_woodpecker_path,
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            '"woodpecker-database"',
        )

        invalid_sqlite_env = dict(sqlite_env, WOODPECKER_DATABASE_MODE="sqlite", WOODPECKER_SERVER_REPLICAS="2")
        with patched_env(invalid_sqlite_env):
            try:
                renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
            except SystemExit as exc:
                if "WOODPECKER_SERVER_REPLICAS must be 1" not in str(exc):
                    raise AssertionError(f"unexpected sqlite replica validation error: {exc}") from exc
            else:
                raise AssertionError("SQLite-backed Woodpecker accepted multiple server replicas")

        invalid_image_env = dict(sqlite_env, WOODPECKER_IMAGE_TAG="next")
        with patched_env(invalid_image_env):
            try:
                renderer.render_woodpecker(sqlite_woodpecker_path, inventory)
            except SystemExit as exc:
                if "WOODPECKER_IMAGE_TAG must be a stable release tag" not in str(exc):
                    raise AssertionError(f"unexpected Woodpecker image tag validation error: {exc}") from exc
            else:
                raise AssertionError("Woodpecker renderer accepted a mutable image tag")

    print("Private platform values renderer self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
