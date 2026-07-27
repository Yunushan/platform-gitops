#!/usr/bin/env python3
"""Validate app secret names/keys stay aligned across values and automation."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = ROOT / "scripts" / "render_private_platform_values.py"
APP_SECRETS_PLAYBOOK = ROOT / "ansible" / "playbooks" / "configure-platform-app-secrets.yml"
PREMIUM_APPS = ROOT / "gitops" / "clusters" / "rke2-main" / "premium-3node" / "apps"
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


CONTRACTS = [
    {
        "label": "Platform Valkey auth",
        "env": "PLATFORM_VALKEY_AUTH_SECRET_NAME",
        "default": "platform-valkey-auth",
        "namespace": "platform-cache",
        "keys": ["valkey-password"],
        "playbook_key_needles": ['--from-literal="${PASSWORD_KEY}=${password}"'],
        "static_file": PREMIUM_APPS / "platform-valkey" / "values.yaml",
        "static_needles": [
            "usersExistingSecret: platform-valkey-auth",
            "passwordKey: valkey-password",
        ],
        "rendered_app": "valkey",
        "custom_secret": "platform-valkey-custom",
        "rendered_needles": [
            'usersExistingSecret: "platform-valkey-custom"',
            'passwordKey: "valkey-password-custom"',
        ],
    },
    {
        "label": "Longhorn backup target credentials",
        "env": "LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME",
        "default": "longhorn-backup-target",
        "namespace": "longhorn-system",
        "keys": [
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ENDPOINTS",
            "VIRTUAL_HOSTED_STYLE",
        ],
        "static_file": PREMIUM_APPS / "longhorn" / "values.yaml",
        "static_needles": [
            "backupTargetCredentialSecret: <LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME>",
        ],
        "rendered_app": "longhorn",
        "custom_secret": "longhorn-backup-custom",
        "rendered_needles": [
            "backupTargetCredentialSecret: longhorn-backup-custom",
        ],
    },
    {
        "label": "Harbor admin password",
        "env": "HARBOR_ADMIN_SECRET_NAME",
        "default": "harbor-admin",
        "namespace": "harbor",
        "keys": ["HARBOR_ADMIN_PASSWORD"],
        "static_file": PREMIUM_APPS / "harbor" / "values.yaml",
        "static_needles": [
            "existingSecretAdminPassword: harbor-admin",
            "existingSecretAdminPasswordKey: HARBOR_ADMIN_PASSWORD",
        ],
        "rendered_app": "harbor",
        "custom_secret": "harbor-admin-custom",
        "rendered_needles": [
            'existingSecretAdminPassword: "harbor-admin-custom"',
            "existingSecretAdminPasswordKey: HARBOR_ADMIN_PASSWORD",
        ],
    },
    {
        "label": "Harbor secret key",
        "env": "HARBOR_SECRET_KEY_SECRET_NAME",
        "default": "harbor-secret-key",
        "namespace": "harbor",
        "keys": ["secretKey"],
        "static_file": PREMIUM_APPS / "harbor" / "values.yaml",
        "static_needles": ["existingSecretSecretKey: harbor-secret-key"],
        "rendered_app": "harbor",
        "custom_secret": "harbor-key-custom",
        "rendered_needles": ['existingSecretSecretKey: "harbor-key-custom"'],
    },
    {
        "label": "Harbor external database password",
        "env": "HARBOR_DATABASE_SECRET_NAME",
        "default": "harbor-database",
        "namespace": "harbor",
        "secondary_namespaces": ["platform-databases"],
        "keys": ["username", "password"],
        "static_file": PREMIUM_APPS / "harbor" / "values.yaml",
        "static_needles": [
            "database:\n  type: external",
            "existingSecret: harbor-database",
        ],
        "static_related": [
            (
                PREMIUM_APPS / "platform-postgres" / "postgres-cluster.yaml",
                ["- name: harbor", "name: harbor-database"],
            ),
            (
                PREMIUM_APPS / "platform-postgres" / "harbor-database.yaml",
                ["name: registry", "owner: harbor", "databaseReclaimPolicy: retain"],
            ),
        ],
        "rendered_app": "harbor",
        "custom_secret": "harbor-db-custom",
        "rendered_needles": [
            "database:\n  type: external",
            'existingSecret: "harbor-db-custom"',
        ],
        "secondary_rendered_app": "cnpg",
        "secondary_rendered_needles": ['name: "harbor"', 'name: "harbor-db-custom"'],
    },
    {
        "label": "Harbor external Redis password",
        "env": "HARBOR_REDIS_SECRET_NAME",
        "default": "harbor-redis",
        "namespace": "harbor",
        "keys": ["REDIS_PASSWORD"],
        "static_file": None,
        "static_needles": [],
        "rendered_app": "harbor",
        "custom_secret": "harbor-redis-custom",
        "rendered_needles": [
            "redis:\n  type: external",
            'existingSecret: "harbor-redis-custom"',
            'name: "harbor-redis-url"',
        ],
    },
    {
        "label": "Harbor registry S3 credentials",
        "env": "HARBOR_S3_SECRET_NAME",
        "default": "harbor-registry-s3",
        "namespace": "harbor",
        "keys": ["REGISTRY_STORAGE_S3_ACCESSKEY", "REGISTRY_STORAGE_S3_SECRETKEY"],
        "static_file": None,
        "static_needles": [],
        "rendered_app": "harbor",
        "custom_secret": "harbor-s3-custom",
        "rendered_needles": [
            "imageChartStorage:\n    disableredirect: true\n    type: s3",
            'existingSecret: "harbor-s3-custom"',
        ],
    },
    {
        "label": "Forgejo external database password",
        "env": "FORGEJO_DATABASE_SECRET_NAME",
        "default": "forgejo-database",
        "namespace": "forgejo",
        "keys": ["username", "password"],
        "static_file": None,
        "static_needles": [],
        "rendered_app": "forgejo",
        "custom_secret": "forgejo-db-custom",
        "rendered_needles": [
            "additionalConfigFromEnvs:",
            "GITEA__database__PASSWD",
            'name: "forgejo-db-custom"',
            "key: password",
        ],
    },
    {
        "label": "Forgejo Redis URI",
        "env": "FORGEJO_REDIS_SECRET_NAME",
        "default": "forgejo-redis",
        "namespace": "forgejo",
        "keys": ["uri"],
        "static_file": None,
        "static_needles": [],
        "rendered_app": "forgejo",
        "custom_secret": "forgejo-redis-custom",
        "rendered_needles": [
            "GITEA__cache__HOST",
            "GITEA__queue__CONN_STR",
            'name: "forgejo-redis-custom"',
            "key: uri",
        ],
    },
    {
        "label": "Woodpecker Forgejo OAuth",
        "env": "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME",
        "default": "woodpecker-forgejo-oauth",
        "namespace": "woodpecker",
        "keys": ["WOODPECKER_FORGEJO_CLIENT", "WOODPECKER_FORGEJO_SECRET"],
        "static_file": PREMIUM_APPS / "woodpecker" / "values.yaml",
        "static_needles": [
            "extraSecretNamesForEnvFrom:",
            "- woodpecker-forgejo-oauth",
        ],
        "rendered_app": "woodpecker",
        "custom_secret": "woodpecker-oauth-custom",
        "rendered_needles": ['- "woodpecker-oauth-custom"'],
    },
    {
        "label": "Woodpecker shared agent token",
        "env": "WOODPECKER_AGENT_SECRET_NAME",
        "default": "woodpecker-agent-secret",
        "namespace": "woodpecker",
        "keys": ["WOODPECKER_AGENT_SECRET"],
        "static_file": PREMIUM_APPS / "woodpecker" / "values.yaml",
        "static_needles": [
            "- woodpecker-agent-secret",
            "createAgentSecret: false",
            "mapAgentSecret: false",
        ],
        "rendered_app": "woodpecker",
        "custom_secret": "woodpecker-agent-custom",
        "rendered_needles": [
            '- "woodpecker-agent-custom"',
            "createAgentSecret: false",
            "mapAgentSecret: false",
        ],
    },
    {
        "label": "Woodpecker database datasource",
        "env": "WOODPECKER_DATABASE_SECRET_NAME",
        "default": "woodpecker-database",
        "namespace": "woodpecker",
        "keys": ["WOODPECKER_DATABASE_DATASOURCE"],
        "static_file": PREMIUM_APPS / "woodpecker" / "values.yaml",
        "static_needles": [
            "- woodpecker-database",
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
        ],
        "static_when_any": [
            "- woodpecker-database",
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            "WOODPECKER_DATABASE_DRIVER: postgres",
        ],
        "rendered_app": "woodpecker",
        "custom_secret": "woodpecker-db-custom",
        "rendered_needles": [
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            '- "woodpecker-db-custom"',
        ],
    },
    {
        "label": "Keycloak admin credentials",
        "env": "KEYCLOAK_ADMIN_SECRET_NAME",
        "default": "keycloak-admin",
        "namespace": "keycloak",
        "keys": ["admin-user", "admin-password"],
        "static_file": PREMIUM_APPS / "keycloak" / "values.yaml",
        "static_needles": [
            "existingSecret: keycloak-admin",
            "passwordSecretKey: admin-password",
        ],
        "rendered_app": "keycloak",
        "custom_secret": "keycloak-admin-custom",
        "rendered_needles": [
            'existingSecret: "keycloak-admin-custom"',
            'passwordSecretKey: "admin-password"',
        ],
    },
    {
        "label": "Keycloak external database password",
        "env": "KEYCLOAK_DATABASE_SECRET_NAME",
        "default": "keycloak-database",
        "namespace": "keycloak",
        "keys": ["username", "password"],
        "static_file": PREMIUM_APPS / "keycloak" / "values.yaml",
        "static_needles": [
            "existingSecret: keycloak-database",
            "existingSecretUserKey: username",
            "existingSecretPasswordKey: password",
        ],
        "rendered_app": "keycloak",
        "custom_secret": "keycloak-db-custom",
        "rendered_needles": [
            'existingSecret: "keycloak-db-custom"',
            "existingSecretUserKey: username",
            "existingSecretPasswordKey: password",
        ],
    },
    {
        "label": "Grafana admin credentials",
        "env": "GRAFANA_ADMIN_SECRET_NAME",
        "default": "grafana-admin",
        "namespace": "monitoring",
        "keys": ["admin-user", "admin-password"],
        "static_file": PREMIUM_APPS / "monitoring" / "values.yaml",
        "static_needles": [
            "existingSecret: grafana-admin",
            "userKey: admin-user",
            "passwordKey: admin-password",
        ],
        "rendered_app": "monitoring",
        "custom_secret": "grafana-admin-custom",
        "rendered_needles": [
            'existingSecret: "grafana-admin-custom"',
            "userKey: admin-user",
            "passwordKey: admin-password",
        ],
    },
    {
        "label": "Grafana external database password",
        "env": "GRAFANA_DATABASE_SECRET_NAME",
        "default": "grafana-database",
        "namespace": "monitoring",
        "secondary_namespaces": ["platform-databases"],
        "keys": ["username", "password"],
        "static_file": PREMIUM_APPS / "monitoring" / "values.yaml",
        "static_needles": [
            "grafana.ini:\n    database:\n      type: postgres",
            "name: grafana-database",
        ],
        "static_related": [
            (
                PREMIUM_APPS / "platform-postgres" / "postgres-cluster.yaml",
                ["- name: grafana", "name: grafana-database"],
            ),
            (
                PREMIUM_APPS / "platform-postgres" / "grafana-database.yaml",
                ["name: grafana", "owner: grafana", "databaseReclaimPolicy: retain"],
            ),
        ],
        "rendered_app": "monitoring",
        "custom_secret": "grafana-db-custom",
        "rendered_needles": [
            "envValueFrom:\n    GF_DATABASE_PASSWORD:",
            'name: "grafana-db-custom"',
            "grafana.ini:\n    database:\n      type: postgres",
            'password: "$__env{GF_DATABASE_PASSWORD}"',
        ],
        "secondary_rendered_app": "cnpg",
        "secondary_rendered_needles": ['name: "grafana"', 'name: "grafana-db-custom"'],
    },
    {
        "label": "Loki object storage",
        "env": "LOKI_OBJECT_STORAGE_SECRET_NAME",
        "default": "loki-object-storage",
        "namespace": "logging",
        "keys": ["LOKI_S3_ACCESS_KEY_ID", "LOKI_S3_SECRET_ACCESS_KEY"],
        "static_file": None,
        "static_needles": [],
        "rendered_app": "loki",
        "custom_secret": "loki-object-custom",
        "rendered_needles": [
            'name: "loki-object-custom"',
            'accessKeyId: "${LOKI_S3_ACCESS_KEY_ID}"',
            'secretAccessKey: "${LOKI_S3_SECRET_ACCESS_KEY}"',
        ],
    },
    {
        "label": "CloudNativePG object storage",
        "env": "CNPG_OBJECT_STORE_SECRET_NAME",
        "default": "cnpg-object-store",
        "namespace": "platform-databases",
        "keys": ["ACCESS_KEY_ID", "SECRET_ACCESS_KEY"],
        "static_file": PREMIUM_APPS / "cloudnativepg" / "postgres-cluster.premium.example.yaml",
        "static_needles": [
            "name: cnpg-object-store",
            "key: ACCESS_KEY_ID",
            "key: SECRET_ACCESS_KEY",
        ],
        "rendered_app": "cnpg",
        "custom_secret": "cnpg-object-custom",
        "rendered_needles": [
            'destinationPath: "s3://platform-test-cnpg-backups/platform-postgres"',
            'endpointURL: "https://object.example.test"',
            'name: "cnpg-object-custom"',
            "key: ACCESS_KEY_ID",
            "key: SECRET_ACCESS_KEY",
        ],
    },
    {
        "label": "Velero cloud credentials",
        "env": "VELERO_CREDENTIALS_SECRET_NAME",
        "default": "velero-credentials",
        "namespace": "velero",
        "keys": ["cloud"],
        "static_file": PREMIUM_APPS / "velero" / "values.yaml",
        "static_needles": ["existingSecret: velero-credentials"],
        "rendered_app": "velero",
        "custom_secret": "velero-cloud-custom",
        "rendered_needles": ['existingSecret: "velero-cloud-custom"'],
    },
]


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


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_private_platform_values", RENDERER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {RENDERER_PATH.relative_to(ROOT)}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def needle_variants(needle: str) -> set[str]:
    variants = {needle}
    if "\n" in needle or '"' in needle:
        return variants
    if ": " in needle:
        key, value = needle.rsplit(": ", 1)
        if value:
            variants.add(f'{key}: "{value}"')
    stripped = needle.lstrip()
    indent = needle[: len(needle) - len(stripped)]
    if stripped.startswith("- "):
        value = stripped[2:]
        if value:
            variants.add(f'{indent}- "{value}"')
    return variants


def require_contains_any(text: str, needles: set[str], label: str) -> None:
    if not any(needle in text for needle in needles):
        options = ", ".join(repr(needle) for needle in sorted(needles))
        raise AssertionError(f"{label} is missing one of: {options}")


def check_static_values() -> None:
    for contract in CONTRACTS:
        static_file = contract["static_file"]
        if static_file is None:
            continue
        text = static_file.read_text(encoding="utf-8")
        static_when_any = contract.get("static_when_any", [])
        if static_when_any and not any(
            variant in text
            for needle in static_when_any
            for variant in needle_variants(needle)
        ):
            continue
        for needle in contract["static_needles"]:
            require_contains_any(
                text,
                needle_variants(needle),
                f"{static_file.relative_to(ROOT)} for {contract['label']}",
            )
        for related_file, related_needles in contract.get("static_related", []):
            related_text = related_file.read_text(encoding="utf-8")
            for needle in related_needles:
                require_contains_any(
                    related_text,
                    needle_variants(needle),
                    f"{related_file.relative_to(ROOT)} for {contract['label']}",
                )


def check_renderer_and_secret_playbook() -> None:
    renderer_text = RENDERER_PATH.read_text(encoding="utf-8")
    playbook_text = APP_SECRETS_PLAYBOOK.read_text(encoding="utf-8")
    for contract in CONTRACTS:
        env_name = contract["env"]
        default_secret = contract["default"]
        require_contains(
            renderer_text,
            f'os.environ.get("{env_name}", "{default_secret}")',
            f"renderer default for {contract['label']}",
        )
        require_contains(
            playbook_text,
            f"lookup('ansible.builtin.env', '{env_name}') | default('{default_secret}', true)",
            f"app-secret playbook default for {contract['label']}",
        )
        require_contains(
            playbook_text,
            f"create namespace {contract['namespace']}",
            f"app-secret playbook namespace for {contract['label']}",
        )
        for namespace in contract.get("secondary_namespaces", []):
            require_contains(
                playbook_text,
                f"create namespace {namespace}",
                f"app-secret playbook secondary namespace for {contract['label']}",
            )
        for key in contract["keys"]:
            key_needles = contract.get("playbook_key_needles", [f"--from-literal={key}="])
            if not any(needle in playbook_text for needle in key_needles):
                raise AssertionError(
                    f"app-secret playbook literal key for {contract['label']} is missing one of: "
                    + ", ".join(key_needles)
                )
    require_contains(
        renderer_text,
        'INTERNAL_MINIO_ENDPOINT = "http://platform-minio.object-storage.svc.cluster.local:9000"',
        "renderer internal MinIO default",
    )
    for needle in (
        'credentials_source="minio-root"',
        'result_state="reconciled"',
        'state=present source=minio-root',
        "'state=reconciled' in platform_velero_secret_result.stdout",
    ):
        require_contains(
            playbook_text,
            needle,
            "Velero MinIO credential reconciliation",
        )

    for needle in (
        "platform_sso_keycloak_secret_name",
        "platform_sso_argocd_secret_name",
        "platform_sso_grafana_secret_name",
        "platform_sso_prometheus_secret_name",
        "--from-literal=PLATFORM_SSO_BOOTSTRAP_ADMIN_PASSWORD=",
        "--from-literal=PLATFORM_SSO_ARGOCD_CLIENT_SECRET=",
        "--from-literal=GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET=",
        "--from-literal=cookie-secret=",
        "app.kubernetes.io/part-of=argocd",
    ):
        require_contains(playbook_text, needle, "platform SSO secret automation")
    for needle in (
        "platform_sso_enabled",
        "keycloakConfigCli:",
        "platform-sso-argocd",
        "platform-sso-grafana",
        "platform-sso-prometheus",
        "disable_login_form: true",
        "oauth_auto_login: true",
        "prometheus-oauth2-proxy",
    ):
        require_contains(renderer_text, needle, "platform SSO renderer contract")


def render_with_custom_secret_names() -> dict[str, str]:
    renderer = load_renderer()
    env = {
        "HARBOR_ADMIN_SECRET_NAME": "harbor-admin-custom",
        "HARBOR_SECRET_KEY_SECRET_NAME": "harbor-key-custom",
        "HARBOR_DATABASE_MODE": "external",
        "HARBOR_DATABASE_HOST": "harbor-postgres.example.test",
        "HARBOR_DATABASE_SECRET_NAME": "harbor-db-custom",
        "HARBOR_REDIS_MODE": "external",
        "HARBOR_REDIS_ADDR": "harbor-redis.example.test:6379",
        "HARBOR_REDIS_SECRET_NAME": "harbor-redis-custom",
        "HARBOR_STORAGE_MODE": "s3",
        "HARBOR_S3_BUCKET": "platform-test-harbor-registry",
        "HARBOR_S3_SECRET_NAME": "harbor-s3-custom",
        "OBJECT_STORAGE_ENDPOINT": "https://object.example.test",
        "OBJECT_STORAGE_REGION": "eu-test-1",
        "OBJECT_STORAGE_BUCKET_PREFIX": "platform-test",
        "CNPG_BACKUP_ENABLED": "true",
        "LONGHORN_BACKUP_TARGET": "s3://platform-test-longhorn-backups@eu-test-1/",
        "LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME": "longhorn-backup-custom",
        "PLATFORM_VALKEY_AUTH_SECRET_NAME": "platform-valkey-custom",
        "PLATFORM_VALKEY_PASSWORD_KEY": "valkey-password-custom",
        "FORGEJO_DATABASE_MODE": "external",
        "FORGEJO_DATABASE_HOST": "forgejo-postgres.example.test",
        "FORGEJO_DATABASE_NAME": "forgejo",
        "FORGEJO_DATABASE_USER": "forgejo",
        "FORGEJO_DATABASE_SECRET_NAME": "forgejo-db-custom",
        "FORGEJO_REDIS_MODE": "redis",
        "FORGEJO_REDIS_SECRET_NAME": "forgejo-redis-custom",
        "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME": "woodpecker-oauth-custom",
        "WOODPECKER_AGENT_SECRET_NAME": "woodpecker-agent-custom",
        "WOODPECKER_DATABASE_MODE": "postgres",
        "WOODPECKER_DATABASE_SECRET_NAME": "woodpecker-db-custom",
        "KEYCLOAK_ADMIN_SECRET_NAME": "keycloak-admin-custom",
        "KEYCLOAK_DATABASE_SECRET_NAME": "keycloak-db-custom",
        "PLATFORM_SSO_KEYCLOAK_SECRET_NAME": "platform-sso-clients-custom",
        "PLATFORM_SSO_ARGOCD_SECRET_NAME": "platform-sso-argocd-custom",
        "PLATFORM_SSO_GRAFANA_SECRET_NAME": "platform-sso-grafana-custom",
        "PLATFORM_SSO_PROMETHEUS_SECRET_NAME": "platform-sso-prometheus-custom",
        "GRAFANA_ADMIN_SECRET_NAME": "grafana-admin-custom",
        "GRAFANA_DATABASE_MODE": "postgres",
        "GRAFANA_DATABASE_HOST": "grafana-postgres.example.test",
        "GRAFANA_DATABASE_SECRET_NAME": "grafana-db-custom",
        "LOKI_OBJECT_STORAGE_SECRET_NAME": "loki-object-custom",
        "CNPG_OBJECT_STORE_SECRET_NAME": "cnpg-object-custom",
        "VELERO_CREDENTIALS_SECRET_NAME": "velero-cloud-custom",
    }
    inventory = {
        "platform_ci_host": "ci.example.test",
        "platform_argocd_host": "argocd.example.test",
        "platform_git_host": "git.example.test",
        "platform_grafana_host": "grafana.example.test",
        "platform_loki_host": "loki.example.test",
        "platform_prometheus_host": "prometheus.example.test",
        "platform_registry_host": "registry.example.test",
        "platform_keycloak_host": "sso.example.test",
    }
    rendered: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="platform-secret-contract-") as tmp, patched_env(env):
        base = Path(tmp)
        paths = {
            "harbor": base / "harbor-values.yaml",
            "forgejo": base / "forgejo-values.yaml",
            "woodpecker": base / "woodpecker-values.yaml",
            "monitoring": base / "monitoring-values.yaml",
            "loki": base / "loki-values.yaml",
            "cnpg": base / "cnpg-postgres.yaml",
            "velero": base / "velero-values.yaml",
            "valkey": base / "valkey-values.yaml",
            "keycloak": base / "keycloak-values.yaml",
            "argocd": base / "argocd-values.yaml",
            "longhorn": base / "longhorn-values.yaml",
        }
        paths["longhorn"].write_text(
            "defaultSettings:\n"
            '  backupTarget: "<LONGHORN_BACKUP_TARGET>"\n'
            "  backupTargetCredentialSecret: <LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME>\n"
            "  storageOverProvisioningPercentage: 100\n",
            encoding="utf-8",
        )
        paths["argocd"].write_text(
            (PREMIUM_APPS / "argocd-ha" / "values.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        renderer.render_platform_valkey(paths["valkey"])
        renderer.render_harbor(paths["harbor"], inventory)
        renderer.render_forgejo(paths["forgejo"], inventory)
        renderer.render_woodpecker(paths["woodpecker"], inventory)
        renderer.render_monitoring(paths["monitoring"], inventory)
        renderer.render_loki(paths["loki"], inventory)
        renderer.render_cnpg_postgres_cluster(paths["cnpg"])
        renderer.render_velero(paths["velero"])
        renderer.render_keycloak(paths["keycloak"], inventory)
        renderer.render_argocd(paths["argocd"], inventory)
        renderer.render_longhorn(paths["longhorn"], env["LONGHORN_BACKUP_TARGET"])
        for app, path in paths.items():
            rendered[app] = path.read_text(encoding="utf-8")
    return rendered


def check_custom_rendering() -> None:
    rendered = render_with_custom_secret_names()
    for contract in CONTRACTS:
        text = rendered[contract["rendered_app"]]
        for needle in contract["rendered_needles"]:
            require_contains(text, needle, f"custom rendered {contract['rendered_app']} values for {contract['label']}")
        secondary_app = contract.get("secondary_rendered_app")
        if secondary_app:
            secondary_text = rendered[secondary_app]
            for needle in contract.get("secondary_rendered_needles", []):
                require_contains(
                    secondary_text,
                    needle,
                    f"custom rendered {secondary_app} values for {contract['label']}",
                )
    require_contains(
        rendered["keycloak"],
        'extraEnvVarsSecret: "platform-sso-clients-custom"',
        "custom rendered Keycloak SSO secret",
    )
    require_contains(
        rendered["argocd"],
        "clientSecret: $" + "platform-sso-argocd-custom:client-secret",
        "custom rendered Argo CD SSO secret",
    )
    for needle in (
        'envFromSecret: "platform-sso-grafana-custom"',
        'name: "platform-sso-prometheus-custom"',
        "disable_login_form: true",
        "oauth_auto_login: true",
    ):
        require_contains(rendered["monitoring"], needle, "custom rendered monitoring SSO secrets")


def main() -> int:
    check_static_values()
    check_renderer_and_secret_playbook()
    check_custom_rendering()
    print(f"Platform app secret contract validation passed for {len(CONTRACTS)} generated secret contracts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
