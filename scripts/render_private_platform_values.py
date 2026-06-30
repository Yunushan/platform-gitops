#!/usr/bin/env python3
"""Render private platform values from env/inventory for first deployment."""

from __future__ import annotations

import argparse
import json
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


def first_value(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def yaml_string(value: str) -> str:
    return json.dumps(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def yaml_list(items: list[str], indent: int) -> str:
    prefix = " " * indent
    return "\n".join(f"{prefix}- {yaml_string(item)}" for item in items)


def platform_domain(inventory: dict[str, str]) -> str:
    domain = env_or_inventory(
        "PLATFORM_DOMAIN",
        inventory,
        "platform_domain",
        "rke2_platform_domain",
    )
    if not domain:
        api_dns = env_or_inventory("RKE2_API_DNS", inventory, "rke2_api_dns")
        if api_dns.startswith("api."):
            domain = api_dns.removeprefix("api.")
    return domain


def platform_host(
    env_name: str,
    inventory: dict[str, str],
    inventory_names: tuple[str, ...],
    default_prefix: str,
) -> str:
    host = env_or_inventory(env_name, inventory, *inventory_names)
    if host:
        return host
    domain = platform_domain(inventory)
    return f"{default_prefix}.{domain}" if domain else ""


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
    - host: {yaml_string(host)}
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: forgejo-tls
      hosts:
        - {yaml_string(host)}

postgresql:
  enabled: false

redis-cluster:
  enabled: false

persistence:
  enabled: true
  size: {yaml_string(data_size)}
  storageClass: {yaml_string(storage_class)}

gitea:
  config:
    server:
      DOMAIN: {yaml_string(host)}
      ROOT_URL: {yaml_string(f"https://{host}/")}
      SSH_DOMAIN: {yaml_string(host)}
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
    - host: {yaml_string(host)}
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: forgejo-tls
      hosts:
        - {yaml_string(host)}

postgresql:
  enabled: false

redis-cluster:
  enabled: false

persistence:
  enabled: true
  size: {yaml_string(data_size)}
  storageClass: {yaml_string(storage_class)}

gitea:
  config:
    server:
      DOMAIN: {yaml_string(host)}
      ROOT_URL: {yaml_string(f"https://{host}/")}
      SSH_DOMAIN: {yaml_string(host)}
      START_SSH_SERVER: true
    service:
      DISABLE_REGISTRATION: true
      REQUIRE_SIGNIN_VIEW: true
    repository:
      DEFAULT_BRANCH: main
    database:
      DB_TYPE: postgres
      HOST: {yaml_string(database_host)}
      NAME: {yaml_string(database_name)}
      USER: {yaml_string(database_user)}
    session:
      PROVIDER: db
    cache:
      ADAPTER: redis
      HOST: {yaml_string(redis_host)}
    queue:
      TYPE: redis
      CONN_STR: {yaml_string(redis_url)}

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


def render_argocd(path: Path, inventory: dict[str, str]) -> bool:
    host = require(
        "PLATFORM_ARGOCD_HOST or platform_argocd_host",
        platform_host(
            "PLATFORM_ARGOCD_HOST",
            inventory,
            ("platform_argocd_host",),
            "argocd",
        ),
    )

    text = path.read_text(encoding="utf-8")
    rendered = text.replace("argocd.<PLATFORM_DOMAIN>", host)
    changed = rendered != text
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def woodpecker_bootstrap_values(
    host: str,
    forgejo_url: str,
    data_size: str,
    storage_class: str,
    admin_users: str,
    oauth_secret_name: str,
) -> str:
    return f"""# Woodpecker bootstrap profile rendered by scripts/render_private_platform_values.py.
# Uses single-server SQLite so CI can come online before external PostgreSQL is configured.
# For Forgejo login, create the OAuth app in Forgejo and store its client/secret in
# the {oauth_secret_name} Kubernetes secret before syncing this app.
server:
  enabled: true
  statefulSet:
    replicaCount: 1
  env:
    WOODPECKER_ADMIN: {yaml_string(admin_users)}
    WOODPECKER_HOST: {yaml_string(f"https://{host}")}
    WOODPECKER_OPEN: "false"
    WOODPECKER_FORGEJO: "true"
    WOODPECKER_FORGEJO_URL: {yaml_string(forgejo_url)}
  extraSecretNamesForEnvFrom:
    - {yaml_string(oauth_secret_name)}
  createAgentSecret: true
  ingress:
    enabled: true
    ingressClassName: traefik
    annotations:
      traefik.ingress.kubernetes.io/router.entrypoints: websecure
      traefik.ingress.kubernetes.io/router.tls: "true"
    hosts:
      - host: {yaml_string(host)}
        paths:
          - path: /
    tls:
      - secretName: woodpecker-tls
        hosts:
          - {yaml_string(host)}
  persistentVolume:
    enabled: true
    size: {yaml_string(data_size)}
    storageClass: {yaml_string(storage_class)}
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 1Gi

agent:
  enabled: true
  replicaCount: 3
  env:
    WOODPECKER_BACKEND: kubernetes
    WOODPECKER_BACKEND_K8S_NAMESPACE: woodpecker
    WOODPECKER_BACKEND_K8S_STORAGE_CLASS: {yaml_string(storage_class)}
    WOODPECKER_BACKEND_K8S_VOLUME_SIZE: 10G
    WOODPECKER_BACKEND_K8S_STORAGE_RWX: "false"
    WOODPECKER_MAX_WORKFLOWS: "2"
  persistence:
    enabled: false
  resources:
    requests:
      cpu: 250m
      memory: 256Mi
    limits:
      memory: 1Gi
"""


def render_woodpecker(path: Path, inventory: dict[str, str]) -> bool:
    host = require(
        "PLATFORM_WOODPECKER_HOST or platform_ci_host",
        platform_host(
            "PLATFORM_WOODPECKER_HOST",
            inventory,
            ("platform_woodpecker_host", "platform_ci_host"),
            "woodpecker",
        ),
    )
    forgejo_host = require(
        "PLATFORM_FORGEJO_HOST or platform_git_host",
        platform_host(
            "PLATFORM_FORGEJO_HOST",
            inventory,
            ("platform_forgejo_host", "platform_git_host"),
            "forgejo",
        ),
    )
    data_size = os.environ.get("WOODPECKER_DATA_SIZE", "10Gi").strip() or "10Gi"
    storage_class = os.environ.get("WOODPECKER_STORAGE_CLASS", "longhorn-standard").strip() or "longhorn-standard"
    admin_users = os.environ.get("WOODPECKER_ADMIN_USERS", "admin").strip() or "admin"
    oauth_secret_name = os.environ.get("WOODPECKER_FORGEJO_OAUTH_SECRET_NAME", "woodpecker-forgejo-oauth").strip()

    rendered = woodpecker_bootstrap_values(
        host,
        f"https://{forgejo_host}",
        data_size,
        storage_class,
        admin_users,
        oauth_secret_name,
    )
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def harbor_bootstrap_values(
    host: str,
    registry_size: str,
    joblog_size: str,
    database_size: str,
    redis_size: str,
    trivy_size: str,
    storage_class: str,
    admin_secret_name: str,
    secret_key_secret_name: str,
) -> str:
    return f"""# Harbor bootstrap profile rendered by scripts/render_private_platform_values.py.
# Uses internal PostgreSQL, Redis, and filesystem registry storage for first deployment.
# Store HARBOR_ADMIN_PASSWORD in secret/{admin_secret_name} and secretKey in
# secret/{secret_key_secret_name} before syncing this app.
expose:
  type: ingress
  tls:
    enabled: true
    certSource: auto
  ingress:
    className: traefik
    hosts:
      core: {yaml_string(host)}

externalURL: {yaml_string(f"https://{host}")}

portal:
  replicas: 1
core:
  replicas: 1
jobservice:
  replicas: 1
registry:
  replicas: 1
trivy:
  enabled: true
  replicas: 1

updateStrategy:
  type: Recreate

persistence:
  enabled: true
  persistentVolumeClaim:
    registry:
      storageClass: {yaml_string(storage_class)}
      size: {yaml_string(registry_size)}
    jobservice:
      jobLog:
        storageClass: {yaml_string(storage_class)}
        size: {yaml_string(joblog_size)}
    database:
      storageClass: {yaml_string(storage_class)}
      size: {yaml_string(database_size)}
    redis:
      storageClass: {yaml_string(storage_class)}
      size: {yaml_string(redis_size)}
    trivy:
      storageClass: {yaml_string(storage_class)}
      size: {yaml_string(trivy_size)}
  imageChartStorage:
    type: filesystem
    filesystem:
      rootdirectory: /storage

database:
  type: internal

redis:
  type: internal

existingSecretAdminPassword: {yaml_string(admin_secret_name)}
existingSecretAdminPasswordKey: HARBOR_ADMIN_PASSWORD
existingSecretSecretKey: {yaml_string(secret_key_secret_name)}

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
"""


def render_harbor(path: Path, inventory: dict[str, str]) -> bool:
    host = require(
        "PLATFORM_HARBOR_HOST or platform_registry_host",
        platform_host(
            "PLATFORM_HARBOR_HOST",
            inventory,
            ("platform_harbor_host", "platform_registry_host"),
            "harbor",
        ),
    )
    storage_class = os.environ.get("HARBOR_STORAGE_CLASS", "longhorn-critical").strip() or "longhorn-critical"
    rendered = harbor_bootstrap_values(
        host,
        os.environ.get("HARBOR_REGISTRY_SIZE", "50Gi").strip() or "50Gi",
        os.environ.get("HARBOR_JOBLOG_SIZE", "5Gi").strip() or "5Gi",
        os.environ.get("HARBOR_DATABASE_SIZE", "10Gi").strip() or "10Gi",
        os.environ.get("HARBOR_REDIS_SIZE", "5Gi").strip() or "5Gi",
        os.environ.get("HARBOR_TRIVY_SIZE", "10Gi").strip() or "10Gi",
        storage_class,
        os.environ.get("HARBOR_ADMIN_SECRET_NAME", "harbor-admin").strip() or "harbor-admin",
        os.environ.get("HARBOR_SECRET_KEY_SECRET_NAME", "harbor-secret-key").strip() or "harbor-secret-key",
    )
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def monitoring_bootstrap_values(
    prometheus_host: str,
    grafana_host: str,
    retention_size: str,
    prometheus_size: str,
    alertmanager_size: str,
    grafana_size: str,
    storage_class: str,
) -> str:
    return f"""# Monitoring bootstrap profile rendered by scripts/render_private_platform_values.py.
# Uses persistent Grafana SQLite for first deployment. Switch Grafana to external
# PostgreSQL for long-term HA.
crds:
  enabled: true

prometheus:
  ingress:
    enabled: true
    ingressClassName: traefik
    hosts:
      - {yaml_string(prometheus_host)}
    tls:
      - secretName: prometheus-tls
        hosts:
          - {yaml_string(prometheus_host)}
  prometheusSpec:
    replicas: 2
    retention: 15d
    retentionSize: {yaml_string(retention_size)}
    podMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelectorNilUsesHelmValues: false
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: {yaml_string(storage_class)}
          accessModes:
            - ReadWriteOnce
          resources:
            requests:
              storage: {yaml_string(prometheus_size)}

alertmanager:
  enabled: true
  alertmanagerSpec:
    replicas: 3
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: {yaml_string(storage_class)}
          accessModes:
            - ReadWriteOnce
          resources:
            requests:
              storage: {yaml_string(alertmanager_size)}

grafana:
  replicas: 1
  persistence:
    enabled: true
    type: pvc
    storageClassName: {yaml_string(storage_class)}
    accessModes:
      - ReadWriteOnce
    size: {yaml_string(grafana_size)}
  ingress:
    enabled: true
    ingressClassName: traefik
    hosts:
      - {yaml_string(grafana_host)}
    tls:
      - secretName: grafana-tls
        hosts:
          - {yaml_string(grafana_host)}

defaultRules:
  create: true

kubeEtcd:
  enabled: true

kubeScheduler:
  enabled: true

kubeControllerManager:
  enabled: true
"""


def render_monitoring(path: Path, inventory: dict[str, str]) -> bool:
    prometheus_host = require(
        "PLATFORM_PROMETHEUS_HOST or platform_prometheus_host",
        platform_host(
            "PLATFORM_PROMETHEUS_HOST",
            inventory,
            ("platform_prometheus_host",),
            "prometheus",
        ),
    )
    grafana_host = require(
        "PLATFORM_GRAFANA_HOST or platform_grafana_host",
        platform_host(
            "PLATFORM_GRAFANA_HOST",
            inventory,
            ("platform_grafana_host",),
            "grafana",
        ),
    )
    storage_class = os.environ.get("MONITORING_STORAGE_CLASS", "longhorn-standard").strip() or "longhorn-standard"
    rendered = monitoring_bootstrap_values(
        prometheus_host,
        grafana_host,
        os.environ.get("PROMETHEUS_RETENTION_SIZE", "20GB").strip() or "20GB",
        os.environ.get("PROMETHEUS_DATA_SIZE", "50Gi").strip() or "50Gi",
        os.environ.get("ALERTMANAGER_DATA_SIZE", "10Gi").strip() or "10Gi",
        os.environ.get("GRAFANA_DATA_SIZE", "10Gi").strip() or "10Gi",
        storage_class,
    )
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def loki_bootstrap_values(
    host: str,
    endpoint: str,
    region: str,
    chunks_bucket: str,
    ruler_bucket: str,
    admin_bucket: str,
    write_cache_size: str,
    backend_cache_size: str,
    storage_class: str,
    object_secret_name: str,
    force_path_style: bool,
    insecure: bool,
) -> str:
    return f"""# Loki premium profile rendered by scripts/render_private_platform_values.py.
# Uses object storage for chunks/rules/admin data. Store LOKI_S3_ACCESS_KEY_ID
# and LOKI_S3_SECRET_ACCESS_KEY in secret/{object_secret_name}.
deploymentMode: SimpleScalable

global:
  extraArgs:
    - -config.expand-env=true
  extraEnvFrom:
    - secretRef:
        name: {yaml_string(object_secret_name)}

loki:
  auth_enabled: false
  commonConfig:
    replication_factor: 3
  storage:
    type: s3
    bucketNames:
      chunks: {yaml_string(chunks_bucket)}
      ruler: {yaml_string(ruler_bucket)}
      admin: {yaml_string(admin_bucket)}
    s3:
      endpoint: {yaml_string(endpoint)}
      region: {yaml_string(region)}
      accessKeyId: "${{LOKI_S3_ACCESS_KEY_ID}}"
      secretAccessKey: "${{LOKI_S3_SECRET_ACCESS_KEY}}"
      s3ForcePathStyle: {str(force_path_style).lower()}
      insecure: {str(insecure).lower()}
  schemaConfig:
    configs:
      - from: "2026-01-01"
        store: tsdb
        object_store: s3
        schema: v13
        index:
          prefix: loki_index_
          period: 24h

write:
  replicas: 3
  persistence:
    storageClass: {yaml_string(storage_class)}
    size: {yaml_string(write_cache_size)}

read:
  replicas: 3

backend:
  replicas: 3
  persistence:
    storageClass: {yaml_string(storage_class)}
    size: {yaml_string(backend_cache_size)}

gateway:
  enabled: true
  ingress:
    enabled: true
    ingressClassName: traefik
    hosts:
      - host: {yaml_string(host)}
        paths:
          - path: /
            pathType: Prefix
    tls:
      - secretName: loki-tls
        hosts:
          - {yaml_string(host)}

monitoring:
  serviceMonitor:
    enabled: true
"""


def render_loki(path: Path, inventory: dict[str, str]) -> bool:
    host = require(
        "PLATFORM_LOKI_HOST or platform_loki_host",
        platform_host("PLATFORM_LOKI_HOST", inventory, ("platform_loki_host",), "loki"),
    )
    endpoint = os.environ.get("OBJECT_STORAGE_ENDPOINT", "https://s3.amazonaws.com").strip()
    region = os.environ.get("OBJECT_STORAGE_REGION", "us-east-1").strip() or "us-east-1"
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    storage_class = os.environ.get("LOKI_STORAGE_CLASS", "longhorn-standard").strip() or "longhorn-standard"
    object_secret_name = os.environ.get("LOKI_OBJECT_STORAGE_SECRET_NAME", "loki-object-storage").strip()
    force_path_style = os.environ.get("OBJECT_STORAGE_FORCE_PATH_STYLE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    insecure = os.environ.get("OBJECT_STORAGE_INSECURE", "false").strip().lower() in {"1", "true", "yes"}

    rendered = loki_bootstrap_values(
        host=host,
        endpoint=endpoint,
        region=region,
        chunks_bucket=os.environ.get("LOKI_CHUNKS_BUCKET", f"{bucket_prefix}-loki-chunks").strip(),
        ruler_bucket=os.environ.get("LOKI_RULER_BUCKET", f"{bucket_prefix}-loki-ruler").strip(),
        admin_bucket=os.environ.get("LOKI_ADMIN_BUCKET", f"{bucket_prefix}-loki-admin").strip(),
        write_cache_size=os.environ.get("LOKI_WRITE_CACHE_SIZE", "20Gi").strip() or "20Gi",
        backend_cache_size=os.environ.get("LOKI_BACKEND_CACHE_SIZE", "20Gi").strip() or "20Gi",
        storage_class=storage_class,
        object_secret_name=object_secret_name,
        force_path_style=force_path_style,
        insecure=insecure,
    )
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def velero_bootstrap_values(
    provider: str,
    bucket: str,
    endpoint: str,
    region: str,
    credentials_secret_name: str,
    schedule: str,
    force_path_style: bool,
    plugin_image: str,
) -> str:
    return f"""# Velero premium profile rendered by scripts/render_private_platform_values.py.
# Store provider credentials in secret/{credentials_secret_name}. For S3-compatible
# storage, platform-app-secrets can create the secret from VELERO_CLOUD_CREDENTIALS
# or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.
initContainers:
  - name: {yaml_string(f"velero-plugin-for-{provider}")}
    image: {yaml_string(plugin_image)}
    imagePullPolicy: IfNotPresent
    volumeMounts:
      - mountPath: /target
        name: plugins

configuration:
  features: EnableCSI
  defaultVolumesToFsBackup: false
  backupStorageLocation:
    - name: default
      provider: {yaml_string(provider)}
      bucket: {yaml_string(bucket)}
      config:
        region: {yaml_string(region)}
        s3Url: {yaml_string(endpoint)}
        s3ForcePathStyle: "{str(force_path_style).lower()}"
  volumeSnapshotLocation:
    - name: default
      provider: {yaml_string(provider)}
      config:
        region: {yaml_string(region)}

credentials:
  useSecret: true
  existingSecret: {yaml_string(credentials_secret_name)}

deployNodeAgent: true

snapshotsEnabled: true

schedules:
  platform-daily:
    disabled: false
    schedule: {yaml_string(schedule)}
    template:
      ttl: 720h0m0s
      includedNamespaces:
        - argocd
        - cert-manager
        - forgejo
        - harbor
        - logging
        - monitoring
        - velero

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
"""


def render_velero(path: Path) -> bool:
    provider = os.environ.get("BACKUP_PROVIDER", "aws").strip() or "aws"
    if provider != "aws":
        raise SystemExit("BACKUP_PROVIDER currently supports aws for automatic Velero rendering")
    endpoint = os.environ.get("OBJECT_STORAGE_ENDPOINT", "https://s3.amazonaws.com").strip()
    region = os.environ.get("OBJECT_STORAGE_REGION", "us-east-1").strip() or "us-east-1"
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    force_path_style = os.environ.get("OBJECT_STORAGE_FORCE_PATH_STYLE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    rendered = velero_bootstrap_values(
        provider=provider,
        bucket=os.environ.get("BACKUP_BUCKET", f"{bucket_prefix}-velero-backups").strip(),
        endpoint=endpoint,
        region=region,
        credentials_secret_name=os.environ.get("VELERO_CREDENTIALS_SECRET_NAME", "velero-credentials").strip(),
        schedule=os.environ.get("VELERO_DAILY_BACKUP_CRON", "0 1 * * *").strip() or "0 1 * * *",
        force_path_style=force_path_style,
        plugin_image=os.environ.get("VELERO_AWS_PLUGIN_IMAGE", "velero/velero-plugin-for-aws:v1.13.1").strip(),
    )
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return changed


def step_ca_bootstrap_values(
    name: str,
    dns_names: list[str],
    url: str,
    storage_class: str,
    db_size: str,
    ingress_host: str,
) -> str:
    ingress = "ingress:\n  enabled: false\n"
    if ingress_host:
        ingress = f"""ingress:
  enabled: true
  ingressClassName: traefik
  hosts:
    - host: {yaml_string(ingress_host)}
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: step-ca-tls
      hosts:
        - {yaml_string(ingress_host)}
"""

    return f"""# step-ca bootstrap profile rendered by scripts/render_private_platform_values.py.
# Smallstep step-certificates supports one CA replica; production resilience
# depends on durable storage plus off-cluster backups.
kind: StatefulSet
replicaCount: 1

service:
  type: ClusterIP
  port: 443
  targetPort: 9000

ca:
  name: {yaml_string(name)}
  address: :9000
  dns: {yaml_string(",".join(dns_names))}
  url: {yaml_string(url)}
  db:
    enabled: true
    persistent: true
    storageClass: {yaml_string(storage_class)}
    accessModes:
      - ReadWriteOnce
    size: {yaml_string(db_size)}
  ssh:
    enabled: false

bootstrap:
  enabled: true
  configmaps: true
  secrets: true

existingSecrets:
  enabled: false

autocert:
  enabled: false

{ingress}
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    memory: 1Gi
"""


def render_step_ca(path: Path, inventory: dict[str, str]) -> bool:
    mode = os.environ.get("STEP_CA_MODE", "disabled").strip().lower()
    if mode in {"", "disabled", "skip", "false", "none"}:
        return False
    if mode != "bootstrap":
        raise SystemExit("STEP_CA_MODE currently supports disabled or bootstrap")

    host = platform_host(
        "STEP_CA_HOST",
        inventory,
        ("platform_step_ca_host",),
        "step-ca",
    )
    name = os.environ.get("STEP_CA_NAME", "Platform Internal CA").strip() or "Platform Internal CA"
    dns_raw = os.environ.get("STEP_CA_DNS_NAMES", "").strip()
    dns_names = split_csv(dns_raw) if dns_raw else []
    if host and host not in dns_names:
        dns_names.append(host)
    for default_dns in ("step-ca.step-ca.svc.cluster.local", "step-ca.step-ca.svc", "step-ca"):
        if default_dns not in dns_names:
            dns_names.append(default_dns)
    url = os.environ.get("STEP_CA_URL", "").strip() or "https://step-ca.step-ca.svc.cluster.local"
    storage_class = os.environ.get("STEP_CA_STORAGE_CLASS", "longhorn-critical").strip() or "longhorn-critical"
    db_size = os.environ.get("STEP_CA_DB_SIZE", "10Gi").strip() or "10Gi"

    rendered = step_ca_bootstrap_values(
        name=name,
        dns_names=dns_names,
        url=url,
        storage_class=storage_class,
        db_size=db_size,
        ingress_host=host,
    )
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
    parser.add_argument(
        "--argocd-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/values.yaml"),
    )
    parser.add_argument(
        "--woodpecker-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"),
    )
    parser.add_argument(
        "--harbor-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/harbor/values.yaml"),
    )
    parser.add_argument(
        "--monitoring-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/monitoring/values.yaml"),
    )
    parser.add_argument(
        "--loki-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/loki/values.yaml"),
    )
    parser.add_argument(
        "--velero-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/velero/values.yaml"),
    )
    parser.add_argument(
        "--step-ca-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml"),
    )
    parser.add_argument("--skip-longhorn", action="store_true")
    parser.add_argument("--skip-argocd", action="store_true")
    parser.add_argument("--skip-woodpecker", action="store_true")
    parser.add_argument("--skip-harbor", action="store_true")
    parser.add_argument("--skip-monitoring", action="store_true")
    parser.add_argument("--skip-loki", action="store_true")
    parser.add_argument("--skip-velero", action="store_true")
    parser.add_argument("--skip-step-ca", action="store_true")
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
        print(
            "ARGOCD_HOST="
            + (
                platform_host("PLATFORM_ARGOCD_HOST", inventory, ("platform_argocd_host",), "argocd")
                or "<missing>"
            )
        )
        print(
            "WOODPECKER_HOST="
            + (
                platform_host(
                    "PLATFORM_WOODPECKER_HOST",
                    inventory,
                    ("platform_woodpecker_host", "platform_ci_host"),
                    "woodpecker",
                )
                or "<missing>"
            )
        )
        print(
            "HARBOR_HOST="
            + (
                platform_host(
                    "PLATFORM_HARBOR_HOST",
                    inventory,
                    ("platform_harbor_host", "platform_registry_host"),
                    "harbor",
                )
                or "<missing>"
            )
        )
        print(
            "GRAFANA_HOST="
            + (
                platform_host("PLATFORM_GRAFANA_HOST", inventory, ("platform_grafana_host",), "grafana")
                or "<missing>"
            )
        )
        print(
            "PROMETHEUS_HOST="
            + (
                platform_host(
                    "PLATFORM_PROMETHEUS_HOST",
                    inventory,
                    ("platform_prometheus_host",),
                    "prometheus",
                )
                or "<missing>"
            )
        )
        print(f"LONGHORN_BACKUP_TARGET={os.environ.get('LONGHORN_BACKUP_TARGET', '')}")
        print(
            "LOKI_HOST="
            + (
                platform_host("PLATFORM_LOKI_HOST", inventory, ("platform_loki_host",), "loki")
                or "<missing>"
            )
        )
        print(f"LOKI_OBJECT_STORAGE_SECRET_NAME={os.environ.get('LOKI_OBJECT_STORAGE_SECRET_NAME', 'loki-object-storage')}")
        print(f"BACKUP_PROVIDER={os.environ.get('BACKUP_PROVIDER', 'aws')}")
        print(f"BACKUP_BUCKET={os.environ.get('BACKUP_BUCKET', os.environ.get('OBJECT_STORAGE_BUCKET_PREFIX', 'platform') + '-velero-backups')}")
        print(f"VELERO_CREDENTIALS_SECRET_NAME={os.environ.get('VELERO_CREDENTIALS_SECRET_NAME', 'velero-credentials')}")
        print(f"STEP_CA_MODE={os.environ.get('STEP_CA_MODE', 'disabled')}")
        print(
            "STEP_CA_HOST="
            + (
                platform_host("STEP_CA_HOST", inventory, ("platform_step_ca_host",), "step-ca")
                or "<not exposed>"
            )
        )
        print(f"STEP_CA_STORAGE_CLASS={os.environ.get('STEP_CA_STORAGE_CLASS', 'longhorn-critical')}")
        print(f"STEP_CA_DB_SIZE={os.environ.get('STEP_CA_DB_SIZE', '10Gi')}")
        return 0 if host else 1

    if not args.skip_argocd and args.argocd_values.exists() and render_argocd(args.argocd_values, inventory):
        changed.append(str(args.argocd_values))

    if render_forgejo(args.forgejo_values, inventory):
        changed.append(str(args.forgejo_values))

    if not args.skip_longhorn and args.longhorn_values.exists():
        backup_target = os.environ.get("LONGHORN_BACKUP_TARGET", "").strip()
        if render_longhorn(args.longhorn_values, backup_target):
            changed.append(str(args.longhorn_values))

    if (
        not args.skip_woodpecker
        and args.woodpecker_values.exists()
        and render_woodpecker(args.woodpecker_values, inventory)
    ):
        changed.append(str(args.woodpecker_values))

    if not args.skip_harbor and args.harbor_values.exists() and render_harbor(args.harbor_values, inventory):
        changed.append(str(args.harbor_values))

    if (
        not args.skip_monitoring
        and args.monitoring_values.exists()
        and render_monitoring(args.monitoring_values, inventory)
    ):
        changed.append(str(args.monitoring_values))

    if not args.skip_loki and args.loki_values.exists() and render_loki(args.loki_values, inventory):
        changed.append(str(args.loki_values))

    if not args.skip_velero and args.velero_values.exists() and render_velero(args.velero_values):
        changed.append(str(args.velero_values))

    if not args.skip_step_ca and render_step_ca(args.step_ca_values, inventory):
        changed.append(str(args.step_ca_values))

    if changed:
        print("Rendered private platform values:")
        for path in changed:
            print(f"- {path}")
    else:
        print("Private platform values already rendered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
