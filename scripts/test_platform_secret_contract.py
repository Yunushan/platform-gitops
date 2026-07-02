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


CONTRACTS = [
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
        "rendered_app": "woodpecker",
        "custom_secret": "woodpecker-db-custom",
        "rendered_needles": [
            'WOODPECKER_DATABASE_DRIVER: "postgres"',
            '- "woodpecker-db-custom"',
        ],
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
    previous = {key: os.environ.get(key) for key in values}
    try:
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


def check_static_values() -> None:
    for contract in CONTRACTS:
        static_file = contract["static_file"]
        if static_file is None:
            continue
        text = static_file.read_text(encoding="utf-8")
        for needle in contract["static_needles"]:
            require_contains(text, needle, f"{static_file.relative_to(ROOT)} for {contract['label']}")


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
        for key in contract["keys"]:
            require_contains(
                playbook_text,
                f"--from-literal={key}=",
                f"app-secret playbook literal key for {contract['label']}",
            )


def render_with_custom_secret_names() -> dict[str, str]:
    renderer = load_renderer()
    env = {
        "HARBOR_ADMIN_SECRET_NAME": "harbor-admin-custom",
        "HARBOR_SECRET_KEY_SECRET_NAME": "harbor-key-custom",
        "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME": "woodpecker-oauth-custom",
        "WOODPECKER_DATABASE_MODE": "postgres",
        "WOODPECKER_DATABASE_SECRET_NAME": "woodpecker-db-custom",
        "LOKI_OBJECT_STORAGE_SECRET_NAME": "loki-object-custom",
        "VELERO_CREDENTIALS_SECRET_NAME": "velero-cloud-custom",
    }
    inventory = {
        "platform_ci_host": "ci.example.test",
        "platform_git_host": "git.example.test",
        "platform_loki_host": "loki.example.test",
        "platform_registry_host": "registry.example.test",
    }
    rendered: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="platform-secret-contract-") as tmp, patched_env(env):
        base = Path(tmp)
        paths = {
            "harbor": base / "harbor-values.yaml",
            "woodpecker": base / "woodpecker-values.yaml",
            "loki": base / "loki-values.yaml",
            "velero": base / "velero-values.yaml",
        }
        renderer.render_harbor(paths["harbor"], inventory)
        renderer.render_woodpecker(paths["woodpecker"], inventory)
        renderer.render_loki(paths["loki"], inventory)
        renderer.render_velero(paths["velero"])
        for app, path in paths.items():
            rendered[app] = path.read_text(encoding="utf-8")
    return rendered


def check_custom_rendering() -> None:
    rendered = render_with_custom_secret_names()
    for contract in CONTRACTS:
        text = rendered[contract["rendered_app"]]
        for needle in contract["rendered_needles"]:
            require_contains(text, needle, f"custom rendered {contract['rendered_app']} values for {contract['label']}")


def main() -> int:
    check_static_values()
    check_renderer_and_secret_playbook()
    check_custom_rendering()
    print(f"Platform app secret contract validation passed for {len(CONTRACTS)} generated secret contracts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
