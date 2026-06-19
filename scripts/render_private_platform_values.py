#!/usr/bin/env python3
"""Render private platform values from env/inventory for first deployment."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def read_inventory_vars(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^#\s]+)", line):
            values[key] = value
    return values


def env_or_inventory(name: str, inventory: dict[str, str], *inventory_names: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for inventory_name in inventory_names:
        value = inventory.get(inventory_name, "").strip()
        if value:
            return value
    return ""


def require(name: str, value: str) -> str:
    if not value:
        raise SystemExit(f"Required value is missing: {name}")
    return value


def render_longhorn(path: Path, backup_target: str) -> bool:
    text = path.read_text(encoding="utf-8")
    rendered = re.sub(
        r"^(\s*backupTarget:\s*).*$",
        lambda match: f'{match.group(1)}"{backup_target}"',
        text,
        flags=re.MULTILINE,
    )
    changed = rendered != text
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def forgejo_bootstrap_values(host: str, data_size: str, storage_class: str) -> str:
    return f"""# Forgejo bootstrap profile rendered by scripts/render_private_platform_values.py.
# This mode uses SQLite and in-process cache/queue so the first dashboard can
# come online before external PostgreSQL and Redis are configured.
replicaCount: 1

strategy:
  type: Recreate

image:
  rootless: true

ingress:
  enabled: true
  className: traefik
  hosts:
    - host: {host}
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: forgejo-tls
      hosts:
        - {host}

postgresql:
  enabled: false

redis-cluster:
  enabled: false

persistence:
  enabled: true
  size: {data_size}
  storageClass: {storage_class}

gitea:
  config:
    server:
      DOMAIN: {host}
      ROOT_URL: https://{host}/
      SSH_DOMAIN: {host}
      START_SSH_SERVER: true
    service:
      DISABLE_REGISTRATION: true
      REQUIRE_SIGNIN_VIEW: true
    repository:
      DEFAULT_BRANCH: main
    database:
      DB_TYPE: sqlite3
    session:
      PROVIDER: file
    cache:
      ADAPTER: memory
    queue:
      TYPE: level

resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    memory: 2Gi
"""


def forgejo_external_values(
    host: str,
    data_size: str,
    storage_class: str,
    database_host: str,
    database_name: str,
    database_user: str,
    redis_host: str,
    redis_url: str,
) -> str:
    return f"""# Forgejo external database profile rendered by scripts/render_private_platform_values.py.
replicaCount: 1

strategy:
  type: Recreate

image:
  rootless: true

ingress:
  enabled: true
  className: traefik
  hosts:
    - host: {host}
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: forgejo-tls
      hosts:
        - {host}

postgresql:
  enabled: false

redis-cluster:
  enabled: false

persistence:
  enabled: true
  size: {data_size}
  storageClass: {storage_class}

gitea:
  config:
    server:
      DOMAIN: {host}
      ROOT_URL: https://{host}/
      SSH_DOMAIN: {host}
      START_SSH_SERVER: true
    service:
      DISABLE_REGISTRATION: true
      REQUIRE_SIGNIN_VIEW: true
    repository:
      DEFAULT_BRANCH: main
    database:
      DB_TYPE: postgres
      HOST: {database_host}
      NAME: {database_name}
      USER: {database_user}
    session:
      PROVIDER: db
    cache:
      ADAPTER: redis
      HOST: {redis_host}
    queue:
      TYPE: redis
      CONN_STR: {redis_url}

resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    memory: 2Gi
"""


def render_forgejo(path: Path, inventory: dict[str, str]) -> bool:
    host = env_or_inventory(
        "PLATFORM_FORGEJO_HOST",
        inventory,
        "platform_forgejo_host",
        "platform_git_host",
    )
    if not host:
        host = env_or_inventory("PLATFORM_GIT_HOST", inventory, "platform_git_host")
    if not host:
        domain = env_or_inventory(
            "PLATFORM_DOMAIN",
            inventory,
            "platform_domain",
            "rke2_platform_domain",
        )
        if domain:
            host = f"forgejo.{domain}"
    host = require("PLATFORM_FORGEJO_HOST or platform_git_host", host)

    data_size = os.environ.get("FORGEJO_DATA_SIZE", "20Gi").strip() or "20Gi"
    storage_class = os.environ.get("FORGEJO_STORAGE_CLASS", "longhorn-critical").strip()
    database_mode = os.environ.get("FORGEJO_DATABASE_MODE", "sqlite").strip().lower()

    if database_mode == "sqlite":
        rendered = forgejo_bootstrap_values(host, data_size, storage_class)
    elif database_mode == "external":
        database_host = require("FORGEJO_DATABASE_HOST", os.environ.get("FORGEJO_DATABASE_HOST", "").strip())
        database_name = os.environ.get("FORGEJO_DATABASE_NAME", "forgejo").strip() or "forgejo"
        database_user = os.environ.get("FORGEJO_DATABASE_USER", "forgejo").strip() or "forgejo"
        redis_host = require("FORGEJO_REDIS_HOST", os.environ.get("FORGEJO_REDIS_HOST", "").strip())
        redis_url = require("FORGEJO_REDIS_URL", os.environ.get("FORGEJO_REDIS_URL", "").strip())
        rendered = forgejo_external_values(
            host,
            data_size,
            storage_class,
            database_host,
            database_name,
            database_user,
            redis_host,
            redis_url,
        )
    else:
        raise SystemExit("FORGEJO_DATABASE_MODE must be sqlite or external")

    old = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=Path("inventory/hosts.local.ini"))
    parser.add_argument(
        "--forgejo-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml"),
    )
    parser.add_argument(
        "--longhorn-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/longhorn/values.yaml"),
    )
    parser.add_argument("--skip-longhorn", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    inventory = read_inventory_vars(args.inventory)
    changed: list[str] = []

    if args.dry_run:
        host = env_or_inventory(
            "PLATFORM_FORGEJO_HOST",
            inventory,
            "platform_forgejo_host",
            "platform_git_host",
        ) or env_or_inventory("PLATFORM_GIT_HOST", inventory, "platform_git_host")
        if not host:
            domain = env_or_inventory(
                "PLATFORM_DOMAIN",
                inventory,
                "platform_domain",
                "rke2_platform_domain",
            )
            host = f"forgejo.{domain}" if domain else ""
        print(f"FORGEJO_HOST={host or '<missing>'}")
        print(f"FORGEJO_DATA_SIZE={os.environ.get('FORGEJO_DATA_SIZE', '20Gi')}")
        print(f"FORGEJO_STORAGE_CLASS={os.environ.get('FORGEJO_STORAGE_CLASS', 'longhorn-critical')}")
        print(f"FORGEJO_DATABASE_MODE={os.environ.get('FORGEJO_DATABASE_MODE', 'sqlite')}")
        print(f"LONGHORN_BACKUP_TARGET={os.environ.get('LONGHORN_BACKUP_TARGET', '')}")
        return 0 if host else 1

    if render_forgejo(args.forgejo_values, inventory):
        changed.append(str(args.forgejo_values))

    if not args.skip_longhorn and args.longhorn_values.exists():
        backup_target = os.environ.get("LONGHORN_BACKUP_TARGET", "").strip()
        if render_longhorn(args.longhorn_values, backup_target):
            changed.append(str(args.longhorn_values))

    if changed:
        print("Rendered private platform values:")
        for path in changed:
            print(f"- {path}")
    else:
        print("Private platform values already rendered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
