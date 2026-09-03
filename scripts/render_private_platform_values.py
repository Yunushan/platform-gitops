#!/usr/bin/env python3
"""Render private platform values from env/inventory for first deployment."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from forgejo_config_env import ConfigEnvironmentError, normalize_config_env
from forgejo_database_contract import FORGEJO_NON_POSTGRES_DATABASE_TYPES
from forgejo_storage_contract import (
    FILESYSTEM_MODES, OBJECT_MODES, StorageContractError, repair_minio_inheritance,
    select_filesystem_storage,
)
from strict_yaml import StrictYamlError, loads_strict_yaml_all


INTERNAL_MINIO_ENDPOINT = "http://platform-minio.object-storage.svc.cluster.local:9000"
FORGEJO_DEFAULT_IMAGE_TAG = "15.0.6"


def read_inventory_vars(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in read_bounded_text(path, encoding="utf-8").splitlines():
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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def postgres_ssl_mode(name: str) -> str:
    mode = os.environ.get(name, "verify-full").strip().lower() or "verify-full"
    allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
    if mode not in allowed:
        raise SystemExit(f"{name} must be one of: {', '.join(sorted(allowed))}")
    if env_bool("PLATFORM_PRODUCTION_STRICT", True) and mode != "verify-full":
        raise SystemExit(f"{name} must be verify-full when PLATFORM_PRODUCTION_STRICT=true")
    return mode


def is_cluster_local_endpoint(endpoint: str) -> bool:
    parsed_endpoint = endpoint if "://" in endpoint else f"//{endpoint}"
    hostname = (urlparse(parsed_endpoint).hostname or "").lower()
    return (
        endpoint.rstrip("/") == INTERNAL_MINIO_ENDPOINT.rstrip("/")
        or hostname in {"localhost", "127.0.0.1", "::1"}
        or hostname.endswith(".svc")
        or hostname.endswith(".svc.cluster.local")
    )


def forgejo_object_storage_values() -> tuple[str, str]:
    """Return Forgejo's secret-backed S3 env entries and config block."""
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    mode = os.environ.get("FORGEJO_OBJECT_STORAGE_MODE", "").strip().lower()
    if not mode:
        mode = "s3" if production_strict else "filesystem"
    if mode in {"filesystem", "file", "local", "disk"}:
        if production_strict:
            raise SystemExit(
                "FORGEJO_OBJECT_STORAGE_MODE must be s3 in production-strict mode"
            )
        config: dict = {}
        select_filesystem_storage(config, [])
        return "", "\n".join("    " + line for line in yaml.safe_dump(config, sort_keys=False).splitlines())
    if mode not in {"s3", "minio", "object", "object-storage", "object_storage"}:
        raise SystemExit(
            "FORGEJO_OBJECT_STORAGE_MODE must be filesystem or s3"
        )

    endpoint = first_value(
        os.environ.get("FORGEJO_S3_ENDPOINT", "").strip(),
        os.environ.get("FORGEJO_OBJECT_STORAGE_ENDPOINT", "").strip(),
        os.environ.get("OBJECT_STORAGE_ENDPOINT", "").strip(),
    )
    if not endpoint and not production_strict:
        endpoint = INTERNAL_MINIO_ENDPOINT
    endpoint = require(
        "FORGEJO_S3_ENDPOINT or FORGEJO_OBJECT_STORAGE_ENDPOINT or OBJECT_STORAGE_ENDPOINT",
        endpoint,
    ).rstrip("/")
    if production_strict and is_cluster_local_endpoint(endpoint):
        raise SystemExit(
            "Production Forgejo object storage must use an off-cluster S3-compatible endpoint; "
            "set FORGEJO_S3_ENDPOINT or OBJECT_STORAGE_ENDPOINT to external storage"
        )

    endpoint_without_scheme = re.sub(r"^https?://", "", endpoint, flags=re.IGNORECASE)
    if not endpoint_without_scheme or "/" in endpoint_without_scheme:
        raise SystemExit(
            "FORGEJO_S3_ENDPOINT must be an S3 host with an optional port, without a path"
        )
    secure = env_bool(
        "FORGEJO_S3_SECURE",
        not endpoint.lower().startswith("http://"),
    )
    if production_strict and not secure:
        raise SystemExit("FORGEJO_S3_SECURE must be true when PLATFORM_PRODUCTION_STRICT=true")

    region = first_value(
        os.environ.get("FORGEJO_S3_REGION", "").strip(),
        os.environ.get("OBJECT_STORAGE_REGION", "us-east-1").strip(),
    ) or "us-east-1"
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    bucket = os.environ.get("FORGEJO_S3_BUCKET", f"{bucket_prefix}-forgejo").strip()
    bucket = require("FORGEJO_S3_BUCKET", bucket)
    secret_name = os.environ.get("FORGEJO_S3_SECRET_NAME", "forgejo-object-storage").strip() or "forgejo-object-storage"

    env_block = f"""    - name: FORGEJO__STORAGE__MINIO_ACCESS_KEY_ID
      valueFrom:
        secretKeyRef:
          name: {yaml_string(secret_name)}
          key: access-key-id
    - name: FORGEJO__STORAGE__MINIO_SECRET_ACCESS_KEY
      valueFrom:
        secretKeyRef:
          name: {yaml_string(secret_name)}
          key: secret-access-key"""
    config_block = f"""    attachment:
      STORAGE_TYPE: minio
    lfs:
      STORAGE_TYPE: minio
    picture:
      AVATAR_STORAGE_TYPE: minio
    'storage.packages':
      STORAGE_TYPE: minio
    storage:
      MINIO_ENDPOINT: {yaml_string(endpoint_without_scheme)}
      MINIO_LOCATION: {yaml_string(region)}
      MINIO_BUCKET: {yaml_string(bucket)}
      MINIO_USE_SSL: {str(secure).lower()}"""
    config = loads_strict_yaml_all(config_block)[0]
    env = loads_strict_yaml_all(env_block)[0]
    repair_minio_inheritance(config, env)
    # Keep the existing global entries for compatibility; modern Forgejo also
    # needs the named storage type and its secret-backed environment bindings.
    alias = yaml.safe_dump({"storage.minio": config["storage.minio"]}, sort_keys=False)
    config_block += "\n" + "\n".join("    " + line for line in alias.splitlines())
    for entry in env[2:]:
        env_block += "\n" + "\n".join("    " + line for line in yaml.safe_dump([entry], sort_keys=False).splitlines())
    return env_block, config_block


def backup_object_storage_endpoint() -> str:
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    endpoint = first_value(
        os.environ.get("BACKUP_OBJECT_STORAGE_ENDPOINT", "").strip(),
        os.environ.get("OBJECT_STORAGE_ENDPOINT", "").strip(),
    )
    if not endpoint and not production_strict:
        endpoint = INTERNAL_MINIO_ENDPOINT
    if production_strict:
        require("BACKUP_OBJECT_STORAGE_ENDPOINT or OBJECT_STORAGE_ENDPOINT", endpoint)
        if is_cluster_local_endpoint(endpoint):
            raise SystemExit(
                "Production backups must use an off-cluster object-storage endpoint; "
                "set BACKUP_OBJECT_STORAGE_ENDPOINT to external S3-compatible storage "
                "or set PLATFORM_PRODUCTION_STRICT=false for a non-production deployment"
            )
    return endpoint


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


def normalize_forgejo_public_host(value: object, source: str, *, url: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw or re.search(r"<[A-Z0-9_]+>", raw):
        return ""
    if url:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").rstrip(".").lower()
    else:
        host = raw.rstrip(".").lower()
    labels = host.split(".")
    if (
        not host
        or len(host) > 253
        or any(
            not label
            or len(label) > 63
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
            for label in labels
        )
    ):
        raise SystemExit(f"{source} must contain a valid Forgejo hostname")
    return host


def existing_forgejo_public_host(path: Path) -> str:
    """Read the canonical public host from an existing private Forgejo render."""
    if not path.is_file():
        return ""
    try:
        documents = loads_strict_yaml_all(read_bounded_text(path, encoding="utf-8"))
    except StrictYamlError as exc:
        raise SystemExit(f"cannot infer Forgejo hostname from invalid YAML {path}: {exc}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SystemExit(f"expected one Forgejo values mapping in {path}")

    values = documents[0]
    candidates: dict[str, set[str]] = {}

    def add_candidate(value: object, source: str, *, url: bool = False) -> None:
        host = normalize_forgejo_public_host(value, source, url=url)
        if host:
            candidates.setdefault(host, set()).add(source)

    ingress = values.get("ingress")
    if isinstance(ingress, dict):
        hosts = ingress.get("hosts")
        if isinstance(hosts, list):
            for index, entry in enumerate(hosts):
                if isinstance(entry, dict):
                    add_candidate(
                        entry.get("host"),
                        f"{path}: ingress.hosts[{index}].host",
                    )
                elif isinstance(entry, str):
                    add_candidate(entry, f"{path}: ingress.hosts[{index}]")

    for chart_key in ("gitea", "forgejo"):
        chart = values.get(chart_key)
        config = chart.get("config") if isinstance(chart, dict) else None
        server = config.get("server") if isinstance(config, dict) else None
        if not isinstance(server, dict):
            continue
        add_candidate(server.get("DOMAIN"), f"{path}: {chart_key}.config.server.DOMAIN")
        add_candidate(
            server.get("ROOT_URL"),
            f"{path}: {chart_key}.config.server.ROOT_URL",
            url=True,
        )

    if len(candidates) > 1:
        details = ", ".join(
            f"{host} ({'; '.join(sorted(sources))})"
            for host, sources in sorted(candidates.items())
        )
        raise SystemExit(
            f"existing Forgejo public hostname is inconsistent in {path}: {details}"
        )
    return next(iter(candidates), "")


def forgejo_public_host(
    inventory: dict[str, str],
    *,
    existing_values_path: Path | None = None,
) -> str:
    """Resolve one Forgejo hostname without inventing drift during focused renders."""
    host = env_or_inventory(
        "PLATFORM_FORGEJO_HOST",
        inventory,
        "platform_forgejo_host",
        "platform_git_host",
    )
    if not host:
        host = env_or_inventory("PLATFORM_GIT_HOST", inventory, "platform_git_host")
    if not host and existing_values_path is not None:
        host = existing_forgejo_public_host(existing_values_path)
    if not host:
        domain = platform_domain(inventory)
        host = f"forgejo.{domain}" if domain else ""
    host = require("PLATFORM_FORGEJO_HOST or platform_git_host", host)
    return normalize_forgejo_public_host(
        host,
        "PLATFORM_FORGEJO_HOST or platform_git_host",
    )


def argocd_host(inventory: dict[str, str]) -> str:
    host = require(
        "PLATFORM_ARGOCD_HOST or platform_argocd_host",
        platform_host(
            "PLATFORM_ARGOCD_HOST",
            inventory,
            ("platform_argocd_host",),
            "argocd",
        ),
    )
    if re.search(r"<[A-Z0-9_]+>", host):
        raise SystemExit(
            "PLATFORM_ARGOCD_HOST or platform_argocd_host must resolve to a real hostname"
        )
    return host


def render_longhorn(
    path: Path,
    backup_target: str,
    storage_over_provisioning_percentage: str | None = None,
) -> bool:
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    if production_strict and not backup_target:
        raise SystemExit(
            "LONGHORN_BACKUP_TARGET is required when PLATFORM_PRODUCTION_STRICT=true"
        )
    if production_strict and backup_target.lower().startswith("s3://"):
        backup_object_storage_endpoint()
    backup_secret_name = (
        os.environ.get("LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME", "longhorn-backup-target").strip()
        or "longhorn-backup-target"
    )
    if storage_over_provisioning_percentage is None:
        storage_over_provisioning_percentage = os.environ.get(
            "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE",
            "100",
        ).strip() or "100"
    if (
        not storage_over_provisioning_percentage.isdigit()
        or not 100 <= int(storage_over_provisioning_percentage) <= 1000
    ):
        raise SystemExit(
            "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE "
            "must be an integer from 100 through 1000"
        )
    default_data_path = os.environ.get("PLATFORM_LONGHORN_DEFAULT_DISK_PATH", "").strip()
    if production_strict and not default_data_path:
        raise SystemExit(
            "PLATFORM_LONGHORN_DEFAULT_DISK_PATH is required when "
            "PLATFORM_PRODUCTION_STRICT=true; mount a dedicated filesystem "
            "on every RKE2 node before rendering Longhorn values"
        )
    default_data_path = default_data_path or "/var/lib/longhorn"
    if not default_data_path.startswith("/") or default_data_path == "/":
        raise SystemExit(
            "PLATFORM_LONGHORN_DEFAULT_DISK_PATH must be an absolute directory path"
        )
    backup_secret_value = (
        backup_secret_name
        if backup_target
        else "<LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME>"
    )
    text = read_bounded_text(path, encoding="utf-8")
    rendered = re.sub(
        r"^(\s*backupTarget:\s*).*$",
        lambda match: f'{match.group(1)}"{backup_target}"',
        text,
        flags=re.MULTILINE,
    )
    rendered = re.sub(
        r"^(\s*backupTargetCredentialSecret:\s*).*$",
        lambda match: f"{match.group(1)}{backup_secret_value}",
        rendered,
        flags=re.MULTILINE,
    )
    rendered = re.sub(
        r"^(\s*defaultDataPath:\s*).*$",
        lambda match: f"{match.group(1)}{yaml_string(default_data_path)}",
        rendered,
        flags=re.MULTILINE,
    )
    rendered = re.sub(
        r"^(\s*storageOverProvisioningPercentage:\s*).*$",
        lambda match: (
            f"{match.group(1)}{storage_over_provisioning_percentage}"
        ),
        rendered,
        flags=re.MULTILINE,
    )
    changed = rendered != text
    if changed:
        atomic_write_text(path, rendered)
    return changed


def render_longhorn_storageclasses(path: Path) -> bool:
    encryption_secret_name = (
        os.environ.get("LONGHORN_ENCRYPTION_SECRET_NAME", "longhorn-crypto").strip()
        or "longhorn-crypto"
    )
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", encryption_secret_name):
        raise SystemExit("LONGHORN_ENCRYPTION_SECRET_NAME must be a Kubernetes DNS label")
    text = read_bounded_text(path, encoding="utf-8")
    rendered = re.sub(
        r"(?m)^(\s*csi[.]storage[.]k8s[.]io/(?:provisioner|node-publish|node-stage|node-expand)-secret-name:\s*).*$",
        lambda match: f"{match.group(1)}{encryption_secret_name}",
        text,
    )
    changed = rendered != text
    if changed:
        atomic_write_text(path, rendered)
    return changed


def platform_valkey_values(
    auth_secret_name: str,
    auth_secret_key: str,
    storage_class: str,
    data_size: str,
    replica_count: str,
    metrics_enabled: bool,
    image_tag: str,
    haproxy_image: str,
) -> str:
    metrics_block = "  enabled: false\n"
    if metrics_enabled:
        metrics_block = """  enabled: true
  exporter:
    resources:
      requests:
        cpu: 50m
        memory: 128Mi
      limits:
        memory: 256Mi
    extraVolumeMounts:
      - name: platform-internal-roots
        mountPath: /trust
        readOnly: true
    extraEnvs:
      REDIS_ADDR: rediss://localhost:6379
      REDIS_USER: default
      REDIS_EXPORTER_TLS_CA_CERT_FILE: /trust/ca-certificates.crt
      REDIS_EXPORTER_SKIP_TLS_VERIFICATION: "false"
  serviceMonitor:
    enabled: true
    namespace: monitoring
    additionalLabels:
      release: monitoring
"""

    proxy_servers = "\n".join(
        (
            "          server valkey-{index} {host}:6379 "
            "check check-ssl check-sni {host} verify required verifyhost {host} "
            "ca-file /trust/ca-certificates.crt inter 1s fall 2 rise 1 "
            "resolvers kubernetes init-addr libc,none"
        ).format(
            index=index,
            host=(
                f"platform-valkey-{index}.platform-valkey-headless."
                "platform-cache.svc.cluster.local"
            ),
        )
        for index in range(int(replica_count) + 1)
    )

    return f"""# Shared platform Valkey profile rendered by scripts/render_private_platform_values.py.
# Argo CD keeps its dedicated Redis HA; this cache is for Forgejo and Harbor.
fullnameOverride: platform-valkey

image:
  tag: {yaml_string(image_tag)}

auth:
  enabled: true
  usersExistingSecret: {yaml_string(auth_secret_name)}
  aclUsers:
    default:
      passwordKey: {yaml_string(auth_secret_key)}
      permissions: "~* &* +@all"

tls:
  enabled: true
  existingSecret: platform-valkey-tls
  serverPublicKey: tls.crt
  serverKey: tls.key
  caPublicKey: ca.crt
  requireClientCertificate: false

valkeyConfig: |-
  appendonly yes
  appendfsync everysec
  aof-use-rdb-preamble yes
  save ""
  tls-auto-reload-interval 300

service:
  type: ClusterIP
  port: 6379

replica:
  enabled: true
  replicas: {replica_count}
  minReplicasToWrite: 1
  persistence:
    storageClass: {yaml_string(storage_class)}
    size: {yaml_string(data_size)}

# The upstream chart provides replication but intentionally does not perform
# failover. Persistent Sentinel sidecars elect a replacement primary, while
# the HAProxy sidecars expose only whichever Valkey member currently reports
# role:master. Consumers keep using platform-valkey-primary:6379.
extraInitContainers:
  - name: configure-ha
    image: {yaml_string(f"valkey/valkey:{image_tag}")}
    imagePullPolicy: IfNotPresent
    command: ["/bin/sh", "-ec"]
    args:
      - |-
        password="$(cat /auth/{auth_secret_key})"
        case "$password" in
          *[!A-Za-z0-9._~-]*)
            echo "{auth_secret_name} must use URL-safe characters for the HA proxy check" >&2
            exit 1
            ;;
        esac

        sentinel_dir=/data/sentinel
        sentinel_config="$sentinel_dir/sentinel.conf"
        mkdir -p "$sentinel_dir" /ha

        # Sentinel rewrites this persistent file after elections. Preserve its
        # dynamic topology while replacing transport and credential settings.
        sentinel_body="$(mktemp "$sentinel_dir/sentinel.conf.body.XXXXXX")"
        if [ -s "$sentinel_config" ]; then
          awk '
            $1 == "port" || $1 == "tls-port" ||
            $1 == "tls-cert-file" || $1 == "tls-key-file" ||
            $1 == "tls-ca-cert-file" || $1 == "tls-auth-clients" ||
            $1 == "tls-replication" || $1 == "tls-auto-reload-interval" ||
            $1 == "bind" || $1 == "protected-mode" || $1 == "dir" ||
            $1 == "requirepass" {{ next }}
            $1 == "sentinel" &&
              ($2 == "resolve-hostnames" || $2 == "announce-hostnames") {{ next }}
            $1 == "sentinel" && $3 == "platform-valkey" &&
              ($2 == "auth-user" || $2 == "auth-pass" ||
               $2 == "down-after-milliseconds" || $2 == "failover-timeout" ||
               $2 == "parallel-syncs") {{ next }}
            {{ print }}
          ' "$sentinel_config" >"$sentinel_body"
        else
          : >"$sentinel_body"
        fi
        if ! awk '$1 == "sentinel" && $2 == "monitor" && $3 == "platform-valkey" {{ found=1 }} END {{ exit !found }}' \\
          "$sentinel_body"; then
          echo "sentinel monitor platform-valkey platform-valkey-0.platform-valkey-headless.platform-cache.svc.cluster.local 6379 2" \\
            >>"$sentinel_body"
        fi

        cat >"$sentinel_config" <<EOF
        port 0
        tls-port 26379
        tls-cert-file /tls/tls.crt
        tls-key-file /tls/tls.key
        tls-ca-cert-file /tls/ca.crt
        tls-auth-clients no
        tls-replication yes
        tls-auto-reload-interval 300
        bind 0.0.0.0
        protected-mode no
        dir $sentinel_dir
        requirepass "$password"
        sentinel resolve-hostnames yes
        sentinel announce-hostnames yes
        EOF
        cat "$sentinel_body" >>"$sentinel_config"
        cat >>"$sentinel_config" <<EOF
        sentinel auth-user platform-valkey default
        sentinel auth-pass platform-valkey "$password"
        sentinel down-after-milliseconds platform-valkey 5000
        sentinel failover-timeout platform-valkey 60000
        sentinel parallel-syncs platform-valkey 1
        EOF
        rm -f "$sentinel_body"
        chmod 0600 "$sentinel_config"

        cat > /ha/haproxy.cfg <<EOF
        global
          log stdout format raw local0

        defaults
          mode tcp
          log global
          timeout connect 3s
          timeout client 60s
          timeout server 60s
          timeout check 3s

        resolvers kubernetes
          parse-resolv-conf
          hold valid 10s

        frontend valkey-primary
          bind :6380
          default_backend elected-primary

        backend elected-primary
          option tcp-check
          tcp-check connect
          tcp-check send "AUTH default $password\\r\\n"
          tcp-check expect string +OK
          tcp-check send "INFO replication\\r\\n"
          tcp-check expect string role:master
          tcp-check send "QUIT\\r\\n"
{proxy_servers}
        EOF
        chmod 0440 /ha/haproxy.cfg
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 1000
    resources:
      requests:
        cpu: 10m
        memory: 32Mi
      limits:
        memory: 64Mi
    volumeMounts:
      - name: valkey-data
        mountPath: /data
      - name: valkey-users-secret
        mountPath: /auth
        readOnly: true
      - name: platform-valkey-ha-config
        mountPath: /ha

extraContainers:
  - name: sentinel
    image: {yaml_string(f"valkey/valkey:{image_tag}")}
    imagePullPolicy: IfNotPresent
    command: ["valkey-server", "/data/sentinel/sentinel.conf", "--sentinel"]
    ports:
      - name: sentinel
        containerPort: 26379
        protocol: TCP
    readinessProbe:
      exec:
        command:
          - /bin/sh
          - -ec
          - valkey-cli --tls --cacert /tls/ca.crt -h localhost --user default --no-auth-warning -a "$(cat /auth/{auth_secret_key})" -p 26379 ping | grep -qx PONG
      periodSeconds: 5
      timeoutSeconds: 3
    livenessProbe:
      exec:
        command:
          - /bin/sh
          - -ec
          - valkey-cli --tls --cacert /tls/ca.crt -h localhost --user default --no-auth-warning -a "$(cat /auth/{auth_secret_key})" -p 26379 ping | grep -qx PONG
      initialDelaySeconds: 10
      periodSeconds: 10
      timeoutSeconds: 3
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 1000
    resources:
      requests:
        cpu: 25m
        memory: 64Mi
      limits:
        memory: 128Mi
    volumeMounts:
      - name: valkey-data
        mountPath: /data
      - name: valkey-users-secret
        mountPath: /auth
        readOnly: true
      - name: platform-valkey-tls
        mountPath: /tls
        readOnly: true
  - name: primary-proxy
    image: {yaml_string(haproxy_image)}
    imagePullPolicy: IfNotPresent
    args: ["haproxy", "-W", "-db", "-f", "/ha/haproxy.cfg"]
    ports:
      - name: primary-proxy
        containerPort: 6380
        protocol: TCP
    readinessProbe:
      tcpSocket:
        port: primary-proxy
      periodSeconds: 5
      timeoutSeconds: 2
    livenessProbe:
      tcpSocket:
        port: primary-proxy
      initialDelaySeconds: 10
      periodSeconds: 10
      timeoutSeconds: 2
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 99
    resources:
      requests:
        cpu: 25m
        memory: 32Mi
      limits:
        memory: 128Mi
    volumeMounts:
      - name: platform-valkey-ha-config
        mountPath: /ha
        readOnly: true
      - name: platform-internal-roots
        mountPath: /trust
        readOnly: true

extraVolumes:
  - name: platform-valkey-ha-config
    emptyDir: {{}}
  - name: platform-internal-roots
    configMap:
      name: platform-internal-roots

podDisruptionBudget:
  enabled: true
  minAvailable: 2

topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app.kubernetes.io/instance: platform-valkey
        app.kubernetes.io/name: valkey

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    memory: 1Gi

metrics:
{metrics_block}"""


def render_platform_valkey(path: Path) -> bool:
    replica_count = os.environ.get("PLATFORM_VALKEY_REPLICA_COUNT", "2").strip() or "2"
    if not replica_count.isdigit() or int(replica_count) < 2:
        raise SystemExit("PLATFORM_VALKEY_REPLICA_COUNT must be at least 2 for HA")
    rendered = platform_valkey_values(
        auth_secret_name=os.environ.get("PLATFORM_VALKEY_AUTH_SECRET_NAME", "platform-valkey-auth").strip()
        or "platform-valkey-auth",
        auth_secret_key=os.environ.get("PLATFORM_VALKEY_PASSWORD_KEY", "valkey-password").strip()
        or "valkey-password",
        storage_class=os.environ.get("PLATFORM_VALKEY_STORAGE_CLASS", "longhorn-critical-encrypted").strip()
        or "longhorn-critical-encrypted",
        data_size=os.environ.get("PLATFORM_VALKEY_DATA_SIZE", "8Gi").strip() or "8Gi",
        replica_count=replica_count,
        metrics_enabled=env_bool("PLATFORM_VALKEY_METRICS", True),
        image_tag=os.environ.get("PLATFORM_VALKEY_IMAGE_TAG", "9.1.0").strip() or "9.1.0",
        haproxy_image=os.environ.get("PLATFORM_VALKEY_HAPROXY_IMAGE", "haproxy:3.4.2-alpine").strip()
        or "haproxy:3.4.2-alpine",
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def minio_values(
    root_secret_name: str,
    root_user_secret_key: str,
    root_password_secret_key: str,
    storage_class: str,
    data_size: str,
    replica_count: str,
    zones: str,
    drives_per_node: str,
    metrics_enabled: bool,
    buckets: list[str],
) -> str:
    metrics_block = "  enabled: false\n"
    if metrics_enabled:
        metrics_block = """  enabled: true
  prometheusAuthType: public
  serviceMonitor:
    enabled: true
    namespace: monitoring
    labels:
      release: monitoring
"""

    return f"""# In-cluster MinIO profile rendered by scripts/render_private_platform_values.py.
# Use this for controlled internal S3-compatible services. For production
# disaster recovery, keep an additional off-cluster/object-store target.
global:
  defaultStorageClass: {yaml_string(storage_class)}

mode: distributed

image:
  # Bitnami's historical community images now live in bitnamilegacy.
  repository: bitnamilegacy/minio
  tag: 2025.7.23-debian-12-r3

console:
  image:
    repository: bitnamilegacy/minio-object-browser
    tag: 2.0.2-debian-12-r3

clientImage:
  repository: bitnamilegacy/minio-client
  tag: 2025.7.21-debian-12-r2

defaultInitContainers:
  volumePermissions:
    image:
      repository: bitnamilegacy/os-shell
      tag: 12-debian-12-r50

auth:
  existingSecret: {yaml_string(root_secret_name)}
  rootUserSecretKey: {yaml_string(root_user_secret_key)}
  rootPasswordSecretKey: {yaml_string(root_password_secret_key)}
  forcePassword: true

statefulset:
  replicaCount: {replica_count}
  zones: {zones}
  drivesPerNode: {drives_per_node}

persistence:
  enabled: true
  storageClass: {yaml_string(storage_class)}
  size: {yaml_string(data_size)}

networkPolicy:
  enabled: true

resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    memory: 2Gi

metrics:
{metrics_block}
provisioning:
  enabled: true
  buckets:
{''.join(f'    - name: {yaml_string(bucket)}\n' for bucket in buckets)}"""


def render_minio(path: Path) -> bool:
    replica_count = os.environ.get("MINIO_REPLICA_COUNT", "4").strip() or "4"
    zones = os.environ.get("MINIO_ZONES", "1").strip() or "1"
    drives_per_node = os.environ.get("MINIO_DRIVES_PER_NODE", "1").strip() or "1"
    if not replica_count.isdigit() or int(replica_count) < 4:
        raise SystemExit("MINIO_REPLICA_COUNT must be at least 4 for distributed MinIO")
    if not zones.isdigit() or int(zones) < 1:
        raise SystemExit("MINIO_ZONES must be at least 1")
    if not drives_per_node.isdigit() or int(drives_per_node) < 1:
        raise SystemExit("MINIO_DRIVES_PER_NODE must be at least 1")
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    rendered = minio_values(
        root_secret_name=os.environ.get("MINIO_ROOT_SECRET_NAME", "minio-root").strip() or "minio-root",
        root_user_secret_key=os.environ.get("MINIO_ROOT_USER_SECRET_KEY", "root-user").strip() or "root-user",
        root_password_secret_key=os.environ.get("MINIO_ROOT_PASSWORD_SECRET_KEY", "root-password").strip()
        or "root-password",
        storage_class=os.environ.get("MINIO_STORAGE_CLASS", "longhorn-critical-encrypted").strip() or "longhorn-critical-encrypted",
        data_size=os.environ.get("MINIO_DATA_SIZE", "50Gi").strip() or "50Gi",
        replica_count=replica_count,
        zones=zones,
        drives_per_node=drives_per_node,
        metrics_enabled=env_bool("MINIO_METRICS", True),
        buckets=[
            os.environ.get("LOKI_CHUNKS_BUCKET", f"{bucket_prefix}-loki-chunks").strip(),
            os.environ.get("LOKI_RULER_BUCKET", f"{bucket_prefix}-loki-ruler").strip(),
            os.environ.get("LOKI_ADMIN_BUCKET", f"{bucket_prefix}-loki-admin").strip(),
            os.environ.get("BACKUP_BUCKET", f"{bucket_prefix}-velero-backups").strip(),
            os.environ.get("CNPG_BACKUP_BUCKET", f"{bucket_prefix}-cnpg-backups").strip(),
            os.environ.get("LONGHORN_BACKUP_BUCKET", f"{bucket_prefix}-longhorn-backups").strip(),
            os.environ.get("HARBOR_S3_BUCKET", f"{bucket_prefix}-harbor-registry").strip(),
            os.environ.get("FORGEJO_S3_BUCKET", f"{bucket_prefix}-forgejo").strip(),
        ],
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def platform_sso_enabled() -> bool:
    return env_bool(
        "PLATFORM_SSO_ENABLED",
        env_bool("PLATFORM_PRODUCTION_STRICT", True),
    )


def keycloak_oidc_client(
    client_id: str,
    client_secret_env: str,
    redirect_uris: list[str],
    web_origins: list[str],
    *,
    audience: bool = False,
) -> dict[str, object]:
    protocol_mappers: list[dict[str, object]] = [
        {
            "name": "realm-roles-as-groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-realm-role-mapper",
            "consentRequired": False,
            "config": {
                "multivalued": "true",
                "userinfo.token.claim": "true",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "claim.name": "groups",
                "jsonType.label": "String",
            },
        }
    ]
    if audience:
        protocol_mappers.append(
            {
                "name": f"{client_id}-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.client.audience": client_id,
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                },
            }
        )
    return {
        "clientId": client_id,
        "name": client_id,
        "enabled": True,
        "protocol": "openid-connect",
        "clientAuthenticatorType": "client-secret",
        "secret": f"$(env:{client_secret_env})",
        "publicClient": False,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "frontchannelLogout": True,
        "redirectUris": redirect_uris,
        "webOrigins": web_origins,
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": "+",
        },
        "protocolMappers": protocol_mappers,
    }


def keycloak_realm_configuration(
    realm: str,
    bootstrap_username: str,
    argocd_host: str,
    grafana_host: str,
    prometheus_host: str,
) -> str:
    argocd_origin = f"https://{argocd_host}"
    grafana_origin = f"https://{grafana_host}"
    prometheus_origin = f"https://{prometheus_host}"
    configuration = {
        "realm": realm,
        "displayName": "Platform SSO",
        "enabled": True,
        "sslRequired": "external",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "rememberMe": True,
        "verifyEmail": False,
        "loginWithEmailAllowed": True,
        "duplicateEmailsAllowed": False,
        "bruteForceProtected": True,
        "failureFactor": 5,
        "waitIncrementSeconds": 60,
        "maxFailureWaitSeconds": 900,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 36000,
        "eventsEnabled": True,
        "adminEventsEnabled": True,
        "adminEventsDetailsEnabled": True,
        "roles": {
            "realm": [
                {
                    "name": "platform-viewer",
                    "description": "Read-only platform access",
                },
                {
                    "name": "platform-admin",
                    "description": "Platform administrator",
                    "composite": True,
                    "composites": {"realm": ["platform-viewer"]},
                },
            ]
        },
        "users": [
            {
                "username": bootstrap_username,
                "enabled": True,
                "emailVerified": True,
                "email": f"{bootstrap_username}@local.invalid",
                "realmRoles": ["platform-admin"],
                "requiredActions": ["CONFIGURE_TOTP"],
                "credentials": [
                    {
                        "type": "password",
                        "value": "$(env:PLATFORM_SSO_BOOTSTRAP_ADMIN_PASSWORD)",
                        "temporary": False,
                    }
                ],
            }
        ],
        "clients": [
            keycloak_oidc_client(
                "argocd",
                "PLATFORM_SSO_ARGOCD_CLIENT_SECRET",
                [
                    f"{argocd_origin}/auth/callback",
                    "http://localhost:8085/auth/callback",
                    "http://127.0.0.1:8085/auth/callback",
                ],
                [argocd_origin],
            ),
            keycloak_oidc_client(
                "grafana",
                "PLATFORM_SSO_GRAFANA_CLIENT_SECRET",
                [f"{grafana_origin}/login/generic_oauth"],
                [grafana_origin],
            ),
            keycloak_oidc_client(
                "prometheus",
                "PLATFORM_SSO_PROMETHEUS_CLIENT_SECRET",
                [f"{prometheus_origin}/oauth2/callback"],
                [prometheus_origin],
                audience=True,
            ),
        ],
    }
    return json.dumps(configuration, indent=2, sort_keys=True)


def keycloak_values(
    host: str,
    argocd_host: str,
    grafana_host: str,
    prometheus_host: str,
    sso_enabled: bool,
    sso_realm: str,
    sso_clients_secret_name: str,
    sso_bootstrap_username: str,
    admin_secret_name: str,
    admin_password_key: str,
    database_host: str,
    database_port: str,
    database_name: str,
    database_user: str,
    database_secret_name: str,
    database_ssl_mode: str,
    storage_class: str,
    replica_count: str,
    image_registry: str,
    image_repository: str,
    image_tag: str,
    config_cli_image_registry: str,
    config_cli_image_repository: str,
    config_cli_image_tag: str,
) -> str:
    if sso_enabled:
        realm_json = keycloak_realm_configuration(
            sso_realm,
            sso_bootstrap_username,
            argocd_host,
            grafana_host,
            prometheus_host,
        )
        indented_realm_json = "\n".join(f"      {line}" for line in realm_json.splitlines())
        sso_block = f"""
keycloakConfigCli:
  enabled: true
  image:
    registry: {yaml_string(config_cli_image_registry)}
    repository: {yaml_string(config_cli_image_repository)}
    tag: {yaml_string(config_cli_image_tag)}
  command:
    - java
  args:
    - -jar
    - /app/keycloak-config-cli.jar
  podSecurityContext:
    enabled: true
    fsGroupChangePolicy: OnRootMismatch
    fsGroup: 65534
  containerSecurityContext:
    enabled: true
    runAsUser: 65534
    runAsGroup: 65534
    runAsNonRoot: true
    privileged: false
    readOnlyRootFilesystem: true
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    seccompProfile:
      type: RuntimeDefault
  automountServiceAccountToken: false
  backoffLimit: 3
  cleanupAfterFinished:
    enabled: true
    seconds: 900
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 512Mi
  extraEnvVars:
    - name: IMPORT_VARSUBSTITUTION_ENABLED
      value: "true"
  extraEnvVarsSecret: {yaml_string(sso_clients_secret_name)}
  configuration:
    platform-realm.json: |
{indented_realm_json}
"""
    else:
        sso_block = "\nkeycloakConfigCli:\n  enabled: false\n"
    return f"""# Keycloak SSO profile rendered by scripts/render_private_platform_values.py.
# Uses the shared CloudNativePG platform-postgres cluster through
# secret/{database_secret_name}. Keep credentials out of Git.
global:
  defaultStorageClass: {yaml_string(storage_class)}
  # This disables only the vendored chart's Bitnami repository-name allow-list.
  # Runtime image admission and signatures remain platform policy concerns.
  security:
    allowInsecureImages: true

image:
  registry: {yaml_string(image_registry)}
  repository: {yaml_string(image_repository)}
  tag: {yaml_string(image_tag)}

# The upstream image uses a different filesystem layout and UID than the
# historical Bitnami image. A standard production start applies the PostgreSQL
# build option at boot; use a pre-optimized private image before enabling a
# read-only root filesystem.
command:
  - /opt/keycloak/bin/kc.sh
args:
  - start

automountServiceAccountToken: false

podSecurityContext:
  enabled: true
  fsGroupChangePolicy: OnRootMismatch
  fsGroup: 0

containerSecurityContext:
  enabled: true
  runAsUser: 1000
  runAsGroup: 0
  runAsNonRoot: true
  privileged: false
  readOnlyRootFilesystem: false
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
  seccompProfile:
    type: RuntimeDefault

defaultInitContainers:
  prepareWriteDirs:
    enabled: false

extraEnvVars:
  - name: KC_DB
    value: postgres
  - name: KC_HEALTH_ENABLED
    value: "true"

startupProbe:
  enabled: true
  initialDelaySeconds: 20
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 30
  successThreshold: 1

auth:
  adminUser: admin
  existingSecret: {yaml_string(admin_secret_name)}
  passwordSecretKey: {yaml_string(admin_password_key)}

production: true
proxyHeaders: xforwarded
hostnameStrict: true
httpEnabled: true
replicaCount: {replica_count}
podAntiAffinityPreset: hard

resources:
  requests:
    cpu: 250m
    memory: 1Gi
  limits:
    memory: 2Gi

pdb:
  create: true
  minAvailable: 1

postgresql:
  enabled: false

externalDatabase:
  host: {yaml_string(database_host)}
  port: {database_port}
  user: {yaml_string(database_user)}
  database: {yaml_string(database_name)}
  existingSecret: {yaml_string(database_secret_name)}
  existingSecretUserKey: username
  existingSecretPasswordKey: password
  extraParams: sslmode={database_ssl_mode}&sslrootcert=/etc/ssl/platform-postgres/ca-certificates.crt

extraVolumes:
  - name: platform-postgres-ca
    configMap:
      name: platform-internal-roots

extraVolumeMounts:
  - name: platform-postgres-ca
    mountPath: /etc/ssl/platform-postgres
    readOnly: true

ingress:
  enabled: true
  ingressClassName: traefik
  hostname: {yaml_string(host)}
  path: /
  pathType: Prefix
  servicePort: http
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls: "true"
  tls: true
  extraTls:
    - hosts:
        - {yaml_string(host)}
      secretName: keycloak-tls

networkPolicy:
  enabled: true
  allowExternal: true
  allowExternalEgress: true

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
    namespace: monitoring
    labels:
      release: monitoring
{sso_block.rstrip()}
"""


def render_keycloak(path: Path, inventory: dict[str, str]) -> bool:
    host = require(
        "PLATFORM_KEYCLOAK_HOST or platform_keycloak_host",
        platform_host("PLATFORM_KEYCLOAK_HOST", inventory, ("platform_keycloak_host",), "sso"),
    )
    replica_count = os.environ.get("KEYCLOAK_REPLICAS", "2").strip() or "2"
    if not replica_count.isdigit() or int(replica_count) < 2:
        raise SystemExit("KEYCLOAK_REPLICAS must be at least 2 for the premium profile")
    database_port = os.environ.get("KEYCLOAK_DATABASE_PORT", "5432").strip() or "5432"
    if not database_port.isdigit():
        raise SystemExit("KEYCLOAK_DATABASE_PORT must be numeric")
    image_registry = os.environ.get("KEYCLOAK_IMAGE_REGISTRY", "quay.io").strip() or "quay.io"
    image_repository = os.environ.get("KEYCLOAK_IMAGE_REPOSITORY", "keycloak/keycloak").strip() or "keycloak/keycloak"
    image_tag = os.environ.get("KEYCLOAK_IMAGE_TAG", "26.7.0").strip() or "26.7.0"
    config_cli_image_registry = (
        os.environ.get("KEYCLOAK_CONFIG_CLI_IMAGE_REGISTRY", "quay.io").strip() or "quay.io"
    )
    config_cli_image_repository = (
        os.environ.get("KEYCLOAK_CONFIG_CLI_IMAGE_REPOSITORY", "adorsys/keycloak-config-cli").strip()
        or "adorsys/keycloak-config-cli"
    )
    config_cli_image_tag = (
        os.environ.get("KEYCLOAK_CONFIG_CLI_IMAGE_TAG", "6.5.1").strip() or "6.5.1"
    )
    for name, value in (
        ("KEYCLOAK_IMAGE_TAG", image_tag),
        ("KEYCLOAK_CONFIG_CLI_IMAGE_TAG", config_cli_image_tag),
    ):
        if value.lower() in {"latest", "nightly", "dev", "main", "master"}:
            raise SystemExit(f"{name} must be a stable release tag")

    argocd_host = require(
        "PLATFORM_ARGOCD_HOST or platform_argocd_host",
        platform_host("PLATFORM_ARGOCD_HOST", inventory, ("platform_argocd_host",), "argocd"),
    )
    grafana_host = require(
        "PLATFORM_GRAFANA_HOST or platform_grafana_host",
        platform_host("PLATFORM_GRAFANA_HOST", inventory, ("platform_grafana_host",), "grafana"),
    )
    prometheus_host = require(
        "PLATFORM_PROMETHEUS_HOST or platform_prometheus_host",
        platform_host(
            "PLATFORM_PROMETHEUS_HOST",
            inventory,
            ("platform_prometheus_host",),
            "prometheus",
        ),
    )

    rendered = keycloak_values(
        host=host,
        argocd_host=argocd_host,
        grafana_host=grafana_host,
        prometheus_host=prometheus_host,
        sso_enabled=platform_sso_enabled(),
        sso_realm=os.environ.get("PLATFORM_SSO_REALM", "platform").strip() or "platform",
        sso_clients_secret_name=os.environ.get(
            "PLATFORM_SSO_KEYCLOAK_SECRET_NAME",
            "platform-sso-clients",
        ).strip()
        or "platform-sso-clients",
        sso_bootstrap_username=os.environ.get(
            "PLATFORM_SSO_BOOTSTRAP_ADMIN_USERNAME",
            os.environ.get("PLATFORM_SSO_BOOTSTRAP_USERNAME", "platform-admin"),
        ).strip()
        or "platform-admin",
        admin_secret_name=os.environ.get("KEYCLOAK_ADMIN_SECRET_NAME", "keycloak-admin").strip()
        or "keycloak-admin",
        admin_password_key=os.environ.get("KEYCLOAK_ADMIN_PASSWORD_KEY", "admin-password").strip()
        or "admin-password",
        database_host=os.environ.get(
            "KEYCLOAK_DATABASE_HOST",
            "platform-postgres-rw.platform-databases.svc.cluster.local",
        ).strip()
        or "platform-postgres-rw.platform-databases.svc.cluster.local",
        database_port=database_port,
        database_name=os.environ.get("KEYCLOAK_DATABASE_NAME", "keycloak").strip() or "keycloak",
        database_user=os.environ.get("KEYCLOAK_DATABASE_USER", "keycloak").strip() or "keycloak",
        database_secret_name=os.environ.get("KEYCLOAK_DATABASE_SECRET_NAME", "keycloak-database").strip()
        or "keycloak-database",
        database_ssl_mode=postgres_ssl_mode("KEYCLOAK_DATABASE_SSL_MODE"),
        storage_class=os.environ.get("KEYCLOAK_STORAGE_CLASS", "longhorn-critical-encrypted").strip()
        or "longhorn-critical-encrypted",
        replica_count=replica_count,
        image_registry=image_registry,
        image_repository=image_repository,
        image_tag=image_tag,
        config_cli_image_registry=config_cli_image_registry,
        config_cli_image_repository=config_cli_image_repository,
        config_cli_image_tag=config_cli_image_tag,
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def forgejo_image_block(image_tag: str) -> str:
    tag_line = ""
    if image_tag:
        tag_line = f"  tag: {yaml_string(image_tag)}\n"
    return f"""image:
  rootless: true
{tag_line}"""


def refresh_forgejo_reviewed_image_pin(path: Path) -> bool:
    """Refresh only Forgejo's reviewed image pin in an existing private render."""
    text = read_bounded_text(path, encoding="utf-8")
    try:
        documents = loads_strict_yaml_all(text)
    except StrictYamlError as exc:
        raise SystemExit(f"cannot refresh Forgejo image pin in invalid YAML {path}: {exc}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SystemExit(f"expected one Forgejo values mapping in {path}")
    image = documents[0].get("image")
    if not isinstance(image, dict) or not isinstance(image.get("tag"), str):
        raise SystemExit(f"expected Forgejo image.tag in {path}")

    lines = text.splitlines(keepends=True)
    image_blocks = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == "image:"
    ]
    if len(image_blocks) != 1:
        raise SystemExit(f"expected exactly one top-level Forgejo image block in {path}")

    tag_lines: list[int] = []
    for index in range(image_blocks[0] + 1, len(lines)):
        stripped_newline = lines[index].rstrip("\r\n")
        if (
            stripped_newline
            and not stripped_newline[0].isspace()
            and not stripped_newline.startswith("#")
        ):
            break
        if re.match(r"^  tag:\s*", stripped_newline):
            tag_lines.append(index)
    if len(tag_lines) != 1:
        raise SystemExit(f"expected exactly one Forgejo image.tag scalar in {path}")

    tag_index = tag_lines[0]
    newline = "\r\n" if lines[tag_index].endswith("\r\n") else "\n"
    expected_tag_line = f"  tag: {yaml_string(FORGEJO_DEFAULT_IMAGE_TAG)}{newline}"
    if image["tag"] == FORGEJO_DEFAULT_IMAGE_TAG and lines[tag_index] == expected_tag_line:
        return False
    lines[tag_index] = expected_tag_line
    rendered = "".join(lines)
    try:
        rendered_documents = loads_strict_yaml_all(rendered)
    except StrictYamlError as exc:
        raise SystemExit(f"refreshed Forgejo values are invalid YAML in {path}: {exc}") from exc
    rendered_image = rendered_documents[0].get("image")
    if not isinstance(rendered_image, dict) or rendered_image.get("tag") != FORGEJO_DEFAULT_IMAGE_TAG:
        raise SystemExit(f"failed to refresh Forgejo image.tag in {path}")
    atomic_write_text(path, rendered)
    return True


def refresh_forgejo_config_env(path: Path) -> bool:
    """Migrate private secret bindings without re-rendering dependency settings."""
    text = read_bounded_text(path, encoding="utf-8")
    try:
        documents = loads_strict_yaml_all(text)
    except StrictYamlError as exc:
        raise SystemExit("Cannot migrate Forgejo environment bindings in invalid YAML; private values were not logged.") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SystemExit(f"expected one Forgejo values mapping in {path}")
    values = documents[0]
    before = json.dumps(values, sort_keys=True)
    try:
        for parent, field in (("gitea", "additionalConfigFromEnvs"), ("deployment", "env")):
            mapping = values.get(parent, {})
            if not isinstance(mapping, dict):
                raise ConfigEnvironmentError("Expected a Forgejo values mapping.")
            if field in mapping:
                mapping[field] = normalize_config_env(mapping[field])
        # Both lists are injected into init-app-ini. Detect collisions across them too.
        normalize_config_env(
            values.get("deployment", {}).get("env", [])
            + values.get("gitea", {}).get("additionalConfigFromEnvs", [])
        )
    except ConfigEnvironmentError as exc:
        raise SystemExit(str(exc)) from exc
    if json.dumps(values, sort_keys=True) == before:
        return False
    header = []
    for line in text.splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        header.append(line)
    atomic_write_text(path, "".join(header) + yaml.safe_dump(values, sort_keys=False))
    print("forgejo_config_env=refreshed credential_values=preserved")
    return True


def refresh_forgejo_storage(path: Path) -> bool:
    """Fix generated storage bindings without re-rendering private dependencies."""
    text = read_bounded_text(path, encoding="utf-8")
    documents = loads_strict_yaml_all(text)
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SystemExit(f"expected one Forgejo values mapping in {path}")
    values = documents[0]
    gitea = values.get("gitea")
    if not isinstance(gitea, dict) or not isinstance(gitea.get("config"), dict):
        raise SystemExit(f"expected Forgejo gitea.config mapping in {path}")
    config = gitea["config"]
    env = gitea.setdefault("additionalConfigFromEnvs", [])
    if not isinstance(env, list) or any(not isinstance(item, dict) for item in env):
        raise SystemExit(f"expected Forgejo additionalConfigFromEnvs list in {path}")
    if gitea.get("additionalConfigSources"):
        raise SystemExit("Focused Forgejo storage refresh cannot reconcile opaque additionalConfigSources.")
    before = json.dumps(values, sort_keys=True)
    mode = os.environ.get("FORGEJO_OBJECT_STORAGE_MODE", "").strip().lower()
    if mode and mode not in FILESYSTEM_MODES | OBJECT_MODES:
        raise SystemExit("FORGEJO_OBJECT_STORAGE_MODE must be filesystem or s3")
    try:
        if mode in FILESYSTEM_MODES:
            if env_bool("PLATFORM_PRODUCTION_STRICT", True):
                raise SystemExit("FORGEJO_OBJECT_STORAGE_MODE must be s3 in production-strict mode")
            select_filesystem_storage(config, env)
        else:
            repair_minio_inheritance(config, env)
    except StorageContractError as exc:
        raise SystemExit(str(exc)) from exc
    if json.dumps(values, sort_keys=True) == before:
        return False
    header = []
    for line in text.splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        header.append(line)
    atomic_write_text(path, "".join(header) + yaml.safe_dump(values, sort_keys=False))
    print("forgejo_storage_bindings=refreshed data_migration=not-performed")
    return True


def refresh_forgejo_postgres_tls(path: Path) -> bool:
    """Restore Forgejo's PostgreSQL trust contract without rendering private dependencies."""
    text = read_bounded_text(path, encoding="utf-8")
    try:
        documents = loads_strict_yaml_all(text)
    except StrictYamlError as exc:
        raise SystemExit(f"cannot refresh Forgejo PostgreSQL TLS in invalid YAML {path}: {exc}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SystemExit(f"expected one Forgejo values mapping in {path}")

    values = documents[0]

    def mapping(parent: dict, key: str, label: str, *, create: bool = False) -> dict:
        value = parent.get(key)
        if value is None and create:
            value = {}
            parent[key] = value
        if not isinstance(value, dict):
            raise SystemExit(f"expected Forgejo {label} mapping in {path}")
        return value

    def upsert_list_entry(
        key: str,
        identity: dict[str, str],
        desired: dict,
        *,
        parent: dict | None = None,
    ) -> bool:
        container = values if parent is None else parent
        entries = container.get(key)
        if entries is None:
            entries = []
            container[key] = entries
        if not isinstance(entries, list):
            raise SystemExit(f"expected Forgejo {key} list in {path}")
        matches = [
            index
            for index, entry in enumerate(entries)
            if isinstance(entry, dict)
            and all(entry.get(identity_key) == identity_value for identity_key, identity_value in identity.items())
        ]
        if len(matches) > 1:
            identity_text = ", ".join(f"{name}={value}" for name, value in identity.items())
            raise SystemExit(f"found duplicate Forgejo {key} entries for {identity_text} in {path}")
        if matches:
            index = matches[0]
            if entries[index] == desired:
                return False
            entries[index] = desired
            return True
        entries.append(desired)
        return True

    def direct_config_env(name: str) -> tuple[bool, str]:
        entries = gitea.get("additionalConfigFromEnvs")
        if entries is None:
            return False, ""
        if not isinstance(entries, list):
            raise SystemExit(f"expected Forgejo gitea.additionalConfigFromEnvs list in {path}")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("name") == name
        ]
        if len(matches) > 1:
            raise SystemExit(f"found duplicate Forgejo {name} environment entries in {path}")
        if not matches:
            return False, ""
        value = matches[0].get("value")
        if not isinstance(value, str) or not value.strip():
            return True, ""
        return True, value.strip()

    def is_postgres_endpoint(value: str) -> bool:
        endpoint = value.strip().lower()
        if not endpoint:
            return False
        if endpoint.startswith(("postgres://", "postgresql://")):
            return True
        if re.search(r":5432(?:$|[/])", endpoint):
            return True
        hostname = endpoint.split(":", 1)[0].rstrip(".")
        labels = hostname.split(".")
        return (
            len(labels) > 2
            and "svc" in labels
            and (labels[0].endswith("-rw") or "postgres" in labels[0])
        )

    gitea = mapping(values, "gitea", "gitea")
    config = mapping(gitea, "config", "gitea.config")
    database = mapping(config, "database", "gitea.config.database")

    additional_sources = gitea.get("additionalConfigSources")
    if additional_sources is None:
        additional_sources = []
    if not isinstance(additional_sources, list):
        raise SystemExit(f"expected Forgejo gitea.additionalConfigSources list in {path}")

    configured_type = database.get("DB_TYPE")
    if configured_type is not None and not isinstance(configured_type, str):
        raise SystemExit(f"expected Forgejo gitea.config.database.DB_TYPE string in {path}")
    database_type = configured_type.strip().lower() if isinstance(configured_type, str) else ""
    forgejo_type_present, forgejo_database_type = direct_config_env(
        "FORGEJO__DATABASE__DB_TYPE"
    )
    legacy_type_present, legacy_database_type = direct_config_env(
        "GITEA__database__DB_TYPE"
    )
    forgejo_database_type = forgejo_database_type.lower()
    legacy_database_type = legacy_database_type.lower()

    if forgejo_type_present and not forgejo_database_type:
        raise SystemExit(
            "focused Forgejo PostgreSQL TLS refresh cannot verify a dynamic "
            f"FORGEJO__DATABASE__DB_TYPE value in {path}"
        )
    if forgejo_type_present:
        effective_database_type = forgejo_database_type
    elif additional_sources:
        raise SystemExit(
            "focused Forgejo PostgreSQL TLS refresh cannot verify database settings from "
            f"gitea.additionalConfigSources in {path}"
        )
    elif legacy_type_present:
        if not legacy_database_type:
            raise SystemExit(
                "focused Forgejo PostgreSQL TLS refresh cannot verify a dynamic "
                f"GITEA__database__DB_TYPE value in {path}"
            )
        effective_database_type = legacy_database_type
    else:
        effective_database_type = database_type

    if effective_database_type and effective_database_type not in {"postgres", "postgresql"}:
        if effective_database_type in FORGEJO_NON_POSTGRES_DATABASE_TYPES:
            print(
                "forgejo_postgres_tls=skipped "
                f"database_type={effective_database_type} "
                "reason=explicit-non-postgres-backend"
            )
            return False
        raise SystemExit(
            "focused Forgejo PostgreSQL TLS refresh found an unsupported database type "
            f"{effective_database_type} in {path}"
        )

    if not effective_database_type:
        configured_host = database.get("HOST")
        if configured_host is not None and not isinstance(configured_host, str):
            raise SystemExit(f"expected Forgejo gitea.config.database.HOST string in {path}")
        forgejo_host_present, forgejo_host = direct_config_env("FORGEJO__DATABASE__HOST")
        legacy_host_present, legacy_host = direct_config_env("GITEA__database__HOST")
        if forgejo_host_present and not forgejo_host:
            raise SystemExit(
                "focused Forgejo PostgreSQL TLS refresh cannot verify a dynamic "
                f"FORGEJO__DATABASE__HOST value in {path}"
            )
        if forgejo_host_present:
            database_hosts = [forgejo_host]
        else:
            if legacy_host_present and not legacy_host:
                raise SystemExit(
                    "focused Forgejo PostgreSQL TLS refresh cannot verify a dynamic "
                    f"GITEA__database__HOST value in {path}"
                )
            database_hosts = [
                value
                for value in (
                    configured_host.strip() if isinstance(configured_host, str) else "",
                    legacy_host,
                )
                if value
            ]
        if not database_hosts or not all(is_postgres_endpoint(value) for value in database_hosts):
            raise SystemExit(
                "focused Forgejo PostgreSQL TLS refresh could not prove a PostgreSQL backend in "
                f"{path}; set gitea.config.database.DB_TYPE=postgres or retain a PostgreSQL "
                "URI, port 5432, or CloudNativePG service in the existing database HOST"
            )

    changed = False
    if database.get("DB_TYPE") != "postgres":
        database["DB_TYPE"] = "postgres"
        changed = True
    if database.get("SSL_MODE") != "verify-full":
        database["SSL_MODE"] = "verify-full"
        changed = True

    deployment = mapping(values, "deployment", "deployment", create=True)
    changed |= upsert_list_entry(
        "env",
        {"name": "SSL_CERT_FILE"},
        {
            "name": "SSL_CERT_FILE",
            "value": "/data/gitea/git/.postgresql/ca-certificates.crt",
        },
        parent=deployment,
    )
    changed |= upsert_list_entry(
        "extraVolumes",
        {"name": "platform-postgres-ca"},
        {
            "name": "platform-postgres-ca",
            "configMap": {
                "name": "platform-internal-roots",
                "items": [
                    {"key": "ca-certificates.crt", "path": "root.crt"},
                    {"key": "ca-certificates.crt", "path": "ca-certificates.crt"},
                ],
            },
        },
    )
    for mount_path in ("/data/gitea/git/.postgresql", "/etc/ssl/platform"):
        changed |= upsert_list_entry(
            "extraContainerVolumeMounts",
            {"name": "platform-postgres-ca", "mountPath": mount_path},
            {
                "name": "platform-postgres-ca",
                "mountPath": mount_path,
                "readOnly": True,
            },
        )
    changed |= upsert_list_entry(
        "extraInitVolumeMounts",
        {"name": "platform-postgres-ca", "mountPath": "/data/gitea/git/.postgresql"},
        {
            "name": "platform-postgres-ca",
            "mountPath": "/data/gitea/git/.postgresql",
            "readOnly": True,
        },
    )

    canonical_needles = (
        "SSL_MODE: verify-full",
        "name: platform-internal-roots",
        "mountPath: /data/gitea/git/.postgresql",
        "name: SSL_CERT_FILE",
        "value: /data/gitea/git/.postgresql/ca-certificates.crt",
    )
    if not changed and all(needle in text for needle in canonical_needles):
        return False

    rendered = yaml.safe_dump(
        values,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    )
    try:
        rendered_documents = loads_strict_yaml_all(rendered)
    except StrictYamlError as exc:
        raise SystemExit(f"refreshed Forgejo PostgreSQL TLS values are invalid in {path}: {exc}") from exc
    if len(rendered_documents) != 1 or not isinstance(rendered_documents[0], dict):
        raise SystemExit(f"failed to refresh Forgejo PostgreSQL TLS values in {path}")
    atomic_write_text(path, rendered)
    return True


def forgejo_bootstrap_values(host: str, data_size: str, storage_class: str, image_tag: str) -> str:
    return f"""# Forgejo bootstrap profile rendered by scripts/render_private_platform_values.py.
# This opt-in mode uses SQLite and in-process cache/queue for dependency-light
# lab bootstrap. The default SQL selector renders PostgreSQL.
replicaCount: 1

strategy:
  type: Recreate

podDisruptionBudget:
  minAvailable: 1

{forgejo_image_block(image_tag)}

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
    image_tag: str,
    database_type: str,
    database_host: str,
    database_name: str,
    database_user: str,
    database_secret_name: str,
    database_ssl_mode: str,
    redis_secret_name: str | None,
    object_storage_env: str,
    object_storage_config: str,
) -> str:
    redis_config_env = ""
    redis_config = """    cache:
      ADAPTER: memory
    queue:
      TYPE: level"""
    if redis_secret_name:
        redis_config_env = f"""
    - name: FORGEJO__CACHE__HOST
      valueFrom:
        secretKeyRef:
          name: {yaml_string(redis_secret_name)}
          key: uri
    - name: FORGEJO__QUEUE__CONN_STR
      valueFrom:
        secretKeyRef:
          name: {yaml_string(redis_secret_name)}
          key: uri"""
        redis_config = """    cache:
      ADAPTER: redis
    queue:
      TYPE: redis"""

    if database_type == "postgres":
        database_trust = """extraVolumes:
  - name: platform-postgres-ca
    configMap:
      name: platform-internal-roots
      items:
        - key: ca-certificates.crt
          path: root.crt
        - key: ca-certificates.crt
          path: ca-certificates.crt

extraContainerVolumeMounts:
  - name: platform-postgres-ca
    mountPath: /data/gitea/git/.postgresql
    readOnly: true
  - name: platform-postgres-ca
    mountPath: /etc/ssl/platform
    readOnly: true

extraInitVolumeMounts:
  - name: platform-postgres-ca
    mountPath: /data/gitea/git/.postgresql
    readOnly: true
"""
    else:
        database_trust = """extraVolumes:
  - name: platform-database-ca
    configMap:
      name: platform-internal-roots
      items:
        - key: ca-certificates.crt
          path: ca-certificates.crt

extraContainerVolumeMounts:
  - name: platform-database-ca
    mountPath: /etc/ssl/platform
    readOnly: true

extraInitVolumeMounts:
  - name: platform-database-ca
    mountPath: /etc/ssl/platform
    readOnly: true
"""

    return f"""# Forgejo external database profile rendered by scripts/render_private_platform_values.py.
# Database type is {database_type}. The premium default uses shared platform
# Valkey for cache/queue; set FORGEJO_REDIS_MODE=memory for dependency-light
# local cache/queue.
replicaCount: 1

strategy:
  type: Recreate

podDisruptionBudget:
  minAvailable: 1

deployment:
  env:
    - name: SSL_CERT_FILE
      value: /data/gitea/git/.postgresql/ca-certificates.crt

{forgejo_image_block(image_tag)}

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

{database_trust.rstrip()}

gitea:
  additionalConfigFromEnvs:
    - name: FORGEJO__DATABASE__PASSWD
      valueFrom:
        secretKeyRef:
          name: {yaml_string(database_secret_name)}
          key: password
{object_storage_env}
{redis_config_env}
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
      DB_TYPE: {database_type}
      HOST: {yaml_string(database_host)}
      NAME: {yaml_string(database_name)}
      USER: {yaml_string(database_user)}
      SSL_MODE: {yaml_string(database_ssl_mode)}
    session:
      PROVIDER: db
{object_storage_config}
{redis_config}

resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    memory: 2Gi
"""


def render_forgejo(path: Path, inventory: dict[str, str]) -> bool:
    host = forgejo_public_host(inventory)

    data_size = os.environ.get("FORGEJO_DATA_SIZE", "20Gi").strip() or "20Gi"
    storage_class = os.environ.get("FORGEJO_STORAGE_CLASS", "longhorn-critical-encrypted").strip()
    image_tag = (
        os.environ.get("FORGEJO_IMAGE_TAG", FORGEJO_DEFAULT_IMAGE_TAG).strip()
        or FORGEJO_DEFAULT_IMAGE_TAG
    )
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", image_tag):
        raise SystemExit("FORGEJO_IMAGE_TAG must be an immutable release tag such as 15.0.6-rootless")
    database_mode = (
        os.environ.get("FORGEJO_DATABASE_MODE")
        or os.environ.get("PLATFORM_SQL_DATABASE_MODE")
        or "postgres"
    ).strip().lower()

    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    if database_mode in {"sqlite", "sqlite3"}:
        if production_strict:
            raise SystemExit(
                "FORGEJO_DATABASE_MODE=sqlite is only supported when PLATFORM_PRODUCTION_STRICT=false"
            )
        rendered = forgejo_bootstrap_values(host, data_size, storage_class, image_tag)
    elif database_mode in ("external", "postgres", "postgresql", "mysql", "mariadb"):
        database_type = "mysql" if database_mode in {"mysql", "mariadb"} else "postgres"
        database_host = os.environ.get("FORGEJO_DATABASE_HOST", "").strip()
        if not database_host and database_type == "postgres":
            cnpg_namespace = os.environ.get("CNPG_CLUSTER_NAMESPACE", "platform-databases").strip()
            cnpg_name = os.environ.get("CNPG_CLUSTER_NAME", "platform-postgres").strip()
            database_host = f"{cnpg_name}-rw.{cnpg_namespace}.svc.cluster.local:5432"
        database_host = require("FORGEJO_DATABASE_HOST", database_host)
        database_name = os.environ.get("FORGEJO_DATABASE_NAME", "forgejo").strip() or "forgejo"
        database_user = os.environ.get("FORGEJO_DATABASE_USER", "forgejo").strip() or "forgejo"
        database_secret_name = os.environ.get("FORGEJO_DATABASE_SECRET_NAME", "forgejo-database").strip()
        database_secret_name = database_secret_name or "forgejo-database"
        if database_type == "postgres":
            database_ssl_mode = postgres_ssl_mode("FORGEJO_DATABASE_SSL_MODE")
        else:
            database_ssl_mode = os.environ.get("FORGEJO_DATABASE_SSL_MODE", "true").strip().lower() or "true"
            mysql_ssl_modes = {"true", "false", "disable", "skip-verify", "prefer"}
            if database_ssl_mode not in mysql_ssl_modes:
                raise SystemExit(
                    "FORGEJO_DATABASE_SSL_MODE must be true, false, disable, skip-verify, or prefer for MySQL/MariaDB"
                )
            if env_bool("PLATFORM_PRODUCTION_STRICT", True) and database_ssl_mode != "true":
                raise SystemExit(
                    "FORGEJO_DATABASE_SSL_MODE must be true for MySQL/MariaDB when PLATFORM_PRODUCTION_STRICT=true"
                )
        redis_mode = os.environ.get("FORGEJO_REDIS_MODE", "redis").strip().lower() or "redis"
        if redis_mode not in {"memory", "local", "redis", "external", "valkey"}:
            raise SystemExit("FORGEJO_REDIS_MODE must be memory, local, redis, external, or valkey")
        if production_strict and redis_mode in {"memory", "local"}:
            raise SystemExit(
                "FORGEJO_REDIS_MODE must use an external Redis-compatible service in production-strict mode"
            )
        redis_secret_name = None
        if redis_mode in {"redis", "external", "valkey"}:
            redis_secret_name = os.environ.get("FORGEJO_REDIS_SECRET_NAME", "forgejo-redis").strip()
            redis_secret_name = redis_secret_name or "forgejo-redis"
        rendered = forgejo_external_values(
            host,
            data_size,
            storage_class,
            image_tag,
            database_type,
            database_host,
            database_name,
            database_user,
            database_secret_name,
            database_ssl_mode,
            redis_secret_name,
            *forgejo_object_storage_values(),
        )
    else:
        raise SystemExit("FORGEJO_DATABASE_MODE must be sqlite, postgres, postgresql, external, mysql, or mariadb")

    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def refresh_argocd_host(path: Path, inventory: dict[str, str]) -> bool:
    """Replace only the public Argo CD hostname placeholder in a private seed."""
    text = read_bounded_text(path, encoding="utf-8")
    placeholder = "argocd.<PLATFORM_DOMAIN>"
    if placeholder not in text:
        return False

    host = argocd_host(inventory)
    rendered = text.replace(placeholder, host)
    if placeholder in rendered:
        raise SystemExit(
            f"Argo CD values still contain the unresolved hostname placeholder: {path}"
        )
    changed = rendered != text
    if changed:
        atomic_write_text(path, rendered)
    return changed


def render_argocd(path: Path, inventory: dict[str, str]) -> bool:
    host = argocd_host(inventory)

    text = read_bounded_text(path, encoding="utf-8")
    sso_enabled = platform_sso_enabled()
    admin_enabled = env_bool("PLATFORM_ARGOCD_ADMIN_ENABLED", default=not sso_enabled)
    if not admin_enabled and not sso_enabled:
        raise SystemExit(
            "PLATFORM_ARGOCD_ADMIN_ENABLED=false requires PLATFORM_SSO_ENABLED=true "
            "or another configured login provider"
        )

    rendered = text.replace("argocd.<PLATFORM_DOMAIN>", host)
    marker_pattern = re.compile(
        r"(?ms)^    # BEGIN PLATFORM SSO\n.*?^    # END PLATFORM SSO\n?"
    )
    rendered = marker_pattern.sub("", rendered)
    if sso_enabled:
        keycloak_host = require(
            "PLATFORM_KEYCLOAK_HOST or platform_keycloak_host",
            platform_host(
                "PLATFORM_KEYCLOAK_HOST",
                inventory,
                ("platform_keycloak_host",),
                "sso",
            ),
        )
        realm = os.environ.get("PLATFORM_SSO_REALM", "platform").strip() or "platform"
        secret_name = os.environ.get(
            "PLATFORM_SSO_ARGOCD_SECRET_NAME",
            "platform-sso-argocd",
        ).strip() or "platform-sso-argocd"
        oidc_block = f"""    # BEGIN PLATFORM SSO
    oidc.config: |
      name: Platform SSO
      issuer: https://{keycloak_host}/realms/{realm}
      clientID: argocd
      clientSecret: ${secret_name}:client-secret
      requestedScopes: [\"openid\", \"profile\", \"email\", \"groups\"]
    # END PLATFORM SSO
"""
        rendered, substitutions = re.subn(
            r"^(  cm:\s*)$",
            lambda match: f"{match.group(1)}\n{oidc_block.rstrip()}",
            rendered,
            count=1,
            flags=re.MULTILINE,
        )
        if substitutions != 1:
            raise SystemExit("Argo CD values must define configs.cm for platform SSO")
    admin_value = "true" if admin_enabled else "false"
    admin_pattern = r"^(\s*admin\.enabled:\s*).*$"
    if re.search(admin_pattern, rendered, flags=re.MULTILINE):
        rendered = re.sub(
            admin_pattern,
            lambda match: f'{match.group(1)}"{admin_value}"',
            rendered,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        rendered, substitutions = re.subn(
            r"^(\s{2}cm:\s*)$",
            lambda match: f'{match.group(1)}\n    admin.enabled: "{admin_value}"',
            rendered,
            count=1,
            flags=re.MULTILINE,
        )
        if substitutions != 1:
            raise SystemExit("Argo CD values must define configs.cm")

    changed = rendered != text
    if changed:
        atomic_write_text(path, rendered)
    return changed


def woodpecker_bootstrap_values(
    host: str,
    forgejo_url: str,
    data_size: str,
    storage_class: str,
    admin_users: str,
    open_registration: bool,
    oauth_secret_name: str,
    agent_secret_name: str,
    image_tag: str,
    server_replicas: str,
    agent_replicas: str,
    database_mode: str,
    database_secret_name: str,
    log_level: str,
    default_pipeline_timeout: str,
    max_pipeline_timeout: str,
) -> str:
    postgres_mode = database_mode in {"postgres", "postgresql", "external"}
    replica_count = server_replicas if postgres_mode else "1"
    database_comment = (
        "Uses PostgreSQL-backed state so multiple Woodpecker server replicas can run safely."
        if postgres_mode
        else "Uses single-server SQLite so CI can come online before external PostgreSQL is configured."
    )
    database_env = ""
    database_secret = ""
    database_trust = ""
    if postgres_mode:
        database_env = """
    WOODPECKER_DATABASE_DRIVER: "postgres"
"""
        database_secret = f"    - {yaml_string(database_secret_name)}\n"
        database_trust = """  extraVolumes:
    - name: platform-postgres-ca
      configMap:
        name: platform-internal-roots
        items:
          - key: ca-certificates.crt
            path: ca-certificates.crt
  extraVolumeMounts:
    - name: platform-postgres-ca
      mountPath: /etc/ssl/platform-postgres
      readOnly: true
"""

    return f"""# Woodpecker bootstrap profile rendered by scripts/render_private_platform_values.py.
# {database_comment}
# For Forgejo login, create the OAuth app in Forgejo and store its client/secret in
# the {oauth_secret_name} Kubernetes secret before syncing this app.
# The shared server/agent token is preserved in secret/{agent_secret_name};
# chart-side random generation is disabled so Argo CD renders are deterministic.
# When WOODPECKER_DATABASE_MODE=postgres, run make platform-app-secrets so
# secret/{database_secret_name} exists before syncing.
server:
  enabled: true
  statefulSet:
    replicaCount: {replica_count}
  podSecurityContext:
    runAsNonRoot: true
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app.kubernetes.io/name: server
              app.kubernetes.io/instance: woodpecker
          topologyKey: kubernetes.io/hostname
  image:
    registry: docker.io
    repository: woodpeckerci/woodpecker-server
    tag: {yaml_string(image_tag)}
  env:
    WOODPECKER_ADMIN: {yaml_string(admin_users)}
    WOODPECKER_HOST: {yaml_string(f"https://{host}")}
    WOODPECKER_OPEN: {yaml_string("true" if open_registration else "false")}
    WOODPECKER_FORGEJO: "true"
    WOODPECKER_FORGEJO_URL: {yaml_string(forgejo_url)}
{database_env.rstrip()}
    WOODPECKER_SERVER_ADDR: ":8000"
    WOODPECKER_GRPC_ADDR: ":9000"
    WOODPECKER_LOG_LEVEL: {yaml_string(log_level)}
    WOODPECKER_DEFAULT_PIPELINE_TIMEOUT: {yaml_string(default_pipeline_timeout)}
    WOODPECKER_MAX_PIPELINE_TIMEOUT: {yaml_string(max_pipeline_timeout)}
  probes:
    liveness:
      timeoutSeconds: 10
      periodSeconds: 10
      successThreshold: 1
      failureThreshold: 30
    readiness:
      timeoutSeconds: 10
      periodSeconds: 10
      successThreshold: 1
      failureThreshold: 3
  extraSecretNamesForEnvFrom:
    - {yaml_string(agent_secret_name)}
    - {yaml_string(oauth_secret_name)}
{database_secret.rstrip()}
{database_trust.rstrip()}
  createAgentSecret: false
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
      cpu: 50m
      memory: 256Mi
    limits:
      memory: 1Gi

agent:
  enabled: true
  replicaCount: {agent_replicas}
  podSecurityContext:
    runAsNonRoot: true
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app.kubernetes.io/name: agent
              app.kubernetes.io/instance: woodpecker
          topologyKey: kubernetes.io/hostname
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: DoNotSchedule
      labelSelector:
        matchLabels:
          app.kubernetes.io/name: agent
          app.kubernetes.io/instance: woodpecker
  mapAgentSecret: false
  extraSecretNamesForEnvFrom:
    - {yaml_string(agent_secret_name)}
  image:
    registry: docker.io
    repository: woodpeckerci/woodpecker-agent
    tag: {yaml_string(image_tag)}
  env:
    WOODPECKER_BACKEND: kubernetes
    WOODPECKER_BACKEND_K8S_NAMESPACE: woodpecker
    WOODPECKER_BACKEND_K8S_STORAGE_CLASS: {yaml_string(storage_class)}
    WOODPECKER_BACKEND_K8S_VOLUME_SIZE: 10G
    WOODPECKER_BACKEND_K8S_STORAGE_RWX: "false"
    WOODPECKER_MAX_WORKFLOWS: "2"
  extraVolumes:
    - name: agent-config
      emptyDir: {{}}
  extraVolumeMounts:
    - name: agent-config
      mountPath: /etc/woodpecker
  persistence:
    enabled: false
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 1Gi
"""


def normalize_woodpecker_image_tag(image_tag: str) -> str:
    tag = image_tag.strip()
    if tag and tag[0].isdigit():
        return f"v{tag}"
    return tag


def render_woodpecker(
    path: Path,
    inventory: dict[str, str],
    forgejo_values_path: Path | None = None,
) -> bool:
    host = require(
        "PLATFORM_WOODPECKER_HOST or platform_ci_host",
        platform_host(
            "PLATFORM_WOODPECKER_HOST",
            inventory,
            ("platform_woodpecker_host", "platform_ci_host"),
            "woodpecker",
        ),
    )
    forgejo_host = forgejo_public_host(
        inventory,
        existing_values_path=forgejo_values_path,
    )
    data_size = os.environ.get("WOODPECKER_DATA_SIZE", "10Gi").strip() or "10Gi"
    storage_class = os.environ.get("WOODPECKER_STORAGE_CLASS", "longhorn-standard-encrypted").strip() or "longhorn-standard-encrypted"
    admin_users = os.environ.get("WOODPECKER_ADMIN_USERS", "admin").strip() or "admin"
    open_registration = env_bool("WOODPECKER_OPEN", False)
    oauth_secret_name = os.environ.get("WOODPECKER_FORGEJO_OAUTH_SECRET_NAME", "woodpecker-forgejo-oauth").strip()
    agent_secret_name = os.environ.get("WOODPECKER_AGENT_SECRET_NAME", "woodpecker-agent-secret").strip() or "woodpecker-agent-secret"
    image_tag = normalize_woodpecker_image_tag(os.environ.get("WOODPECKER_IMAGE_TAG", "v3.16.0").strip() or "v3.16.0")
    log_level = os.environ.get("WOODPECKER_LOG_LEVEL", "info").strip().lower() or "info"
    default_pipeline_timeout = os.environ.get("WOODPECKER_DEFAULT_PIPELINE_TIMEOUT", "60").strip() or "60"
    max_pipeline_timeout = os.environ.get("WOODPECKER_MAX_PIPELINE_TIMEOUT", "120").strip() or "120"
    database_mode = os.environ.get("WOODPECKER_DATABASE_MODE", "postgres").strip().lower() or "postgres"
    database_secret_name = os.environ.get("WOODPECKER_DATABASE_SECRET_NAME", "woodpecker-database").strip() or "woodpecker-database"
    default_server_replicas = "3" if database_mode in {"postgres", "postgresql", "external"} else "1"
    server_replicas = os.environ.get("WOODPECKER_SERVER_REPLICAS", default_server_replicas).strip() or default_server_replicas
    agent_replicas = os.environ.get("WOODPECKER_AGENT_REPLICAS", "3").strip() or "3"
    if database_mode not in {"sqlite", "postgres", "postgresql", "external"}:
        raise SystemExit("WOODPECKER_DATABASE_MODE must be sqlite, postgres, postgresql, or external")
    for name, value in (
        ("WOODPECKER_SERVER_REPLICAS", server_replicas),
        ("WOODPECKER_AGENT_REPLICAS", agent_replicas),
        ("WOODPECKER_DEFAULT_PIPELINE_TIMEOUT", default_pipeline_timeout),
        ("WOODPECKER_MAX_PIPELINE_TIMEOUT", max_pipeline_timeout),
    ):
        if not value.isdigit() or int(value) < 1:
            raise SystemExit(f"{name} must be a positive integer")
    if int(max_pipeline_timeout) < int(default_pipeline_timeout):
        raise SystemExit(
            "WOODPECKER_MAX_PIPELINE_TIMEOUT must be greater than or equal to "
            "WOODPECKER_DEFAULT_PIPELINE_TIMEOUT"
        )
    if database_mode in {"postgres", "postgresql", "external"} and int(server_replicas) < 2:
        raise SystemExit("WOODPECKER_SERVER_REPLICAS must be at least 2 when WOODPECKER_DATABASE_MODE=postgres")
    if database_mode == "sqlite" and int(server_replicas) != 1:
        raise SystemExit("WOODPECKER_SERVER_REPLICAS must be 1 when WOODPECKER_DATABASE_MODE=sqlite")
    if image_tag.lower() in {"latest", "next", "nightly", "dev"}:
        raise SystemExit("WOODPECKER_IMAGE_TAG must be a stable release tag, not latest/next/nightly/dev")
    if log_level not in {"trace", "debug", "info", "warn", "error", "fatal", "panic", "disabled"}:
        raise SystemExit(
            "WOODPECKER_LOG_LEVEL must be trace, debug, info, warn, error, fatal, panic, or disabled"
        )

    rendered = woodpecker_bootstrap_values(
        host,
        f"https://{forgejo_host}",
        data_size,
        storage_class,
        admin_users,
        open_registration,
        oauth_secret_name,
        agent_secret_name,
        image_tag,
        server_replicas,
        agent_replicas,
        database_mode,
        database_secret_name,
        log_level,
        default_pipeline_timeout,
        max_pipeline_timeout,
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def harbor_bootstrap_values(
    host: str,
    tls_cert_source: str,
    tls_secret_name: str,
    registry_size: str,
    joblog_size: str,
    database_size: str,
    redis_size: str,
    trivy_size: str,
    storage_class: str,
    admin_secret_name: str,
    secret_key_secret_name: str,
    registry_storage_block: str,
    database_block: str,
    redis_block: str,
    core_redis_url_env_block: str,
    dependency_note: str,
    replicas: str,
) -> str:
    tls_secret_block = ""
    if tls_cert_source == "secret":
        tls_secret_block = "\n    " + "secret" + f":\n      secretName: {yaml_string(tls_secret_name)}"
    high_availability = int(replicas) > 1
    component_availability = harbor_component_availability_block(replicas, high_availability)
    job_loggers = "  jobLoggers:\n    - database\n" if high_availability else ""
    update_strategy = "RollingUpdate" if high_availability else "Recreate"
    if high_availability:
        persistence_block = f"""persistence:
  enabled: false
{registry_storage_block}"""
    else:
        persistence_block = f"""persistence:
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
{registry_storage_block}"""
    return f"""# Harbor bootstrap profile rendered by scripts/render_private_platform_values.py.
# {dependency_note}
# Store HARBOR_ADMIN_PASSWORD in secret/{admin_secret_name} and secretKey in
# secret/{secret_key_secret_name} before syncing this app.
expose:
  type: ingress
  tls:
    enabled: true
    certSource: {yaml_string(tls_cert_source)}{tls_secret_block}
  ingress:
    className: traefik
    hosts:
      core: {yaml_string(host)}

externalURL: {yaml_string(f"https://{host}")}

# Kustomize replaces this chart-generated Secret volume with the
# trust-manager ConfigMap of the same name, avoiding broad Secret RBAC.
caBundleSecretName: platform-internal-roots

portal:
{component_availability}
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      memory: 256Mi
core:
{component_availability}
{core_redis_url_env_block}
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
    limits:
      memory: 1Gi
jobservice:
{component_availability}
{job_loggers}  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 512Mi
registry:
{component_availability}
  registry:
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        memory: 1Gi
  controller:
    resources:
      requests:
        cpu: 50m
        memory: 128Mi
      limits:
        memory: 256Mi
trivy:
  enabled: true
{component_availability}
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
    limits:
      memory: 2Gi
exporter:
{component_availability}
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      memory: 256Mi

updateStrategy:
  type: {update_strategy}

{persistence_block}

{database_block}

{redis_block}

existingSecretAdminPassword: {yaml_string(admin_secret_name)}
existingSecretAdminPasswordKey: HARBOR_ADMIN_PASSWORD
existingSecretSecretKey: {yaml_string(secret_key_secret_name)}

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
"""


def harbor_component_availability_block(replicas: str, high_availability: bool) -> str:
    if not high_availability:
        return f"  replicas: {replicas}"
    return f"""  replicas: {replicas}
  podDisruptionBudget:
    enabled: true
    minAvailable: 1
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: DoNotSchedule"""


def harbor_filesystem_storage_block() -> str:
    return """  imageChartStorage:
    type: filesystem
    filesystem:
      rootdirectory: /storage"""


def harbor_s3_storage_block(
    endpoint: str,
    region: str,
    bucket: str,
    secret_name: str,
    secure: bool,
    skipverify: bool,
    disableredirect: bool,
) -> str:
    return f"""  imageChartStorage:
    disableredirect: {str(disableredirect).lower()}
    type: s3
    s3:
      region: {yaml_string(region)}
      bucket: {yaml_string(bucket)}
      regionendpoint: {yaml_string(endpoint)}
      secure: {str(secure).lower()}
      skipverify: {str(skipverify).lower()}
      v4auth: true
      existingSecret: {yaml_string(secret_name)}"""


def harbor_internal_database_block() -> str:
    return """database:
  type: internal
  internal:
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        memory: 1Gi"""


def harbor_external_database_block(
    host: str,
    port: str,
    database_name: str,
    username: str,
    secret_name: str,
    sslmode: str,
) -> str:
    return f"""database:
  type: external
  external:
    host: {yaml_string(host)}
    port: {yaml_string(port)}
    username: {yaml_string(username)}
    coreDatabase: {yaml_string(database_name)}
    existingSecret: {yaml_string(secret_name)}
    sslmode: {yaml_string(sslmode)}
    maxIdleConns: 100
    maxOpenConns: 900"""


def harbor_internal_redis_block() -> str:
    return """redis:
  type: internal
  internal:
    resources:
      requests:
        cpu: 50m
        memory: 128Mi
      limits:
        memory: 256Mi"""


def harbor_external_redis_block(addr: str, username: str, secret_name: str, tls_enabled: bool) -> str:
    return f"""redis:
  type: external
  jobserviceDatabaseIndex: "1"
  registryDatabaseIndex: "2"
  trivyAdapterIndex: "5"
  external:
    addr: {yaml_string(addr)}
    sentinelMasterSet: ""
    tlsOptions:
      enable: {str(tls_enabled).lower()}
    coreDatabaseIndex: "0"
    jobserviceDatabaseIndex: "1"
    registryDatabaseIndex: "2"
    trivyAdapterIndex: "5"
    username: {yaml_string(username)}
    existingSecret: {yaml_string(secret_name)}"""


def harbor_core_redis_url_env_block(secret_name: str) -> str:
    return f"""  # Harbor's chart cannot build the core Redis URL from existingSecret.
  # Keep the URL, including the password, in generated Secret data instead.
  extraEnvVars:
    - name: _REDIS_URL_CORE
      valueFrom:
        secretKeyRef:
          name: {yaml_string(secret_name)}
          key: REDIS_URL_CORE"""


def harbor_registry_storage_settings() -> tuple[str, str, str]:
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    default_mode = "s3" if production_strict else "filesystem"
    storage_mode = os.environ.get("HARBOR_STORAGE_MODE", default_mode).strip().lower() or default_mode
    if storage_mode in {"filesystem", "local", "pvc"}:
        if production_strict:
            raise SystemExit(
                "HARBOR_STORAGE_MODE must be s3 when PLATFORM_PRODUCTION_STRICT=true"
            )
        return (
            harbor_filesystem_storage_block(),
            "filesystem registry storage for first deployment",
            storage_mode,
        )
    if storage_mode not in {"s3", "object", "object-storage", "object_storage"}:
        raise SystemExit("HARBOR_STORAGE_MODE must be filesystem or s3")

    endpoint = first_value(
        os.environ.get("HARBOR_S3_ENDPOINT", "").strip(),
        os.environ.get("OBJECT_STORAGE_ENDPOINT", "").strip(),
    )
    if production_strict:
        require("HARBOR_S3_ENDPOINT or OBJECT_STORAGE_ENDPOINT", endpoint)
        if is_cluster_local_endpoint(endpoint):
            raise SystemExit(
                "Production Harbor registry storage must use an off-cluster S3 endpoint; "
                "set HARBOR_S3_ENDPOINT or OBJECT_STORAGE_ENDPOINT to external storage"
            )
    elif not endpoint:
        endpoint = INTERNAL_MINIO_ENDPOINT
    region = first_value(
        os.environ.get("HARBOR_S3_REGION", "").strip(),
        os.environ.get("OBJECT_STORAGE_REGION", "us-east-1").strip(),
    ) or "us-east-1"
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    bucket = os.environ.get("HARBOR_S3_BUCKET", f"{bucket_prefix}-harbor-registry").strip()
    secret_name = os.environ.get("HARBOR_S3_SECRET_NAME", "harbor-registry-s3").strip() or "harbor-registry-s3"
    secure_default = not endpoint.lower().startswith("http://")
    skipverify_default = env_bool("OBJECT_STORAGE_INSECURE", False)
    disableredirect_default = "amazonaws.com" not in endpoint.lower()
    return (
        harbor_s3_storage_block(
            endpoint=endpoint,
            region=region,
            bucket=bucket,
            secret_name=secret_name,
            secure=env_bool("HARBOR_S3_SECURE", secure_default),
            skipverify=env_bool("HARBOR_S3_SKIPVERIFY", skipverify_default),
            disableredirect=env_bool("HARBOR_S3_DISABLE_REDIRECT", disableredirect_default),
        ),
        f"S3-compatible registry storage in secret/{secret_name}",
        "s3",
    )


def harbor_database_settings() -> tuple[str, str, str]:
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    default_mode = "external" if production_strict else "internal"
    database_mode = os.environ.get("HARBOR_DATABASE_MODE", default_mode).strip().lower() or default_mode
    if database_mode in {"internal", "local"}:
        if production_strict:
            raise SystemExit(
                "HARBOR_DATABASE_MODE must be external when PLATFORM_PRODUCTION_STRICT=true"
            )
        return (harbor_internal_database_block(), "internal PostgreSQL", database_mode)
    if database_mode not in {"external", "postgres", "postgresql"}:
        raise SystemExit("HARBOR_DATABASE_MODE must be internal or external")

    host = require(
        "HARBOR_DATABASE_HOST",
        os.environ.get(
            "HARBOR_DATABASE_HOST",
            "platform-postgres-rw.platform-databases.svc.cluster.local",
        ).strip(),
    )
    return (
        harbor_external_database_block(
            host=host,
            port=os.environ.get("HARBOR_DATABASE_PORT", "5432").strip() or "5432",
            database_name=os.environ.get("HARBOR_DATABASE_NAME", "registry").strip() or "registry",
            username=os.environ.get("HARBOR_DATABASE_USER", "harbor").strip() or "harbor",
            secret_name=os.environ.get("HARBOR_DATABASE_SECRET_NAME", "harbor-database").strip()
            or "harbor-database",
            sslmode=postgres_ssl_mode("HARBOR_DATABASE_SSLMODE"),
        ),
        "external PostgreSQL",
        "external",
    )


def harbor_redis_settings() -> tuple[str, str, str, str]:
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    redis_mode = os.environ.get("HARBOR_REDIS_MODE", "external").strip().lower() or "external"
    if redis_mode in {"internal", "local"}:
        return (harbor_internal_redis_block(), "", "internal Redis", redis_mode)
    if redis_mode not in {"external", "redis", "valkey"}:
        raise SystemExit("HARBOR_REDIS_MODE must be internal, external, redis, or valkey")

    addr = os.environ.get("HARBOR_REDIS_ADDR", "").strip()
    shared_valkey = not addr and not os.environ.get("HARBOR_REDIS_HOST", "").strip()
    if not addr:
        host = os.environ.get("HARBOR_REDIS_HOST", "").strip()
        host = host or os.environ.get("PLATFORM_VALKEY_PRIMARY_HOST", "platform-valkey-primary.platform-cache.svc.cluster.local").strip()
        host = require("HARBOR_REDIS_HOST or HARBOR_REDIS_ADDR", host)
        port = os.environ.get("HARBOR_REDIS_PORT", os.environ.get("PLATFORM_VALKEY_PORT", "6379")).strip() or "6379"
        addr = f"{host}:{port}"
    username = os.environ.get("HARBOR_REDIS_USERNAME", "").strip()
    if not username and shared_valkey:
        username = "default"
    tls_enabled = env_bool("HARBOR_REDIS_TLS", True)
    if production_strict and not tls_enabled:
        raise SystemExit(
            "HARBOR_REDIS_TLS must be true for external Redis/Valkey when "
            "PLATFORM_PRODUCTION_STRICT=true"
        )
    return (
        harbor_external_redis_block(
            addr=addr,
            username=username,
            secret_name=os.environ.get("HARBOR_REDIS_SECRET_NAME", "harbor-redis").strip() or "harbor-redis",
            tls_enabled=tls_enabled,
        ),
        harbor_core_redis_url_env_block(
            os.environ.get("HARBOR_REDIS_URL_SECRET_NAME", "harbor-redis-url").strip()
            or "harbor-redis-url"
        ),
        "external Redis",
        "external",
    )


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
    tls_cert_source = os.environ.get("HARBOR_TLS_CERT_SOURCE", "auto").strip().lower() or "auto"
    if tls_cert_source not in {"auto", "secret"}:
        raise SystemExit("HARBOR_TLS_CERT_SOURCE must be auto or secret")
    tls_secret_name = os.environ.get("HARBOR_TLS_SECRET_NAME", "harbor-tls").strip() or "harbor-tls"
    storage_class = os.environ.get("HARBOR_STORAGE_CLASS", "longhorn-critical-encrypted").strip() or "longhorn-critical-encrypted"
    registry_storage_block, registry_note, registry_mode = harbor_registry_storage_settings()
    database_block, database_note, database_mode = harbor_database_settings()
    redis_block, core_redis_url_env_block, redis_note, redis_mode = harbor_redis_settings()
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    replicas = os.environ.get("HARBOR_REPLICAS", "2" if production_strict else "1").strip()
    if int(replicas) < 1:
        raise SystemExit("HARBOR_REPLICAS must be at least 1")
    if production_strict and int(replicas) < 2:
        raise SystemExit("HARBOR_REPLICAS must be at least 2 when PLATFORM_PRODUCTION_STRICT=true")
    if int(replicas) > 1 and (
        database_mode not in {"external", "postgres", "postgresql"}
        or redis_mode not in {"external", "redis", "valkey"}
        or registry_mode not in {"s3", "object", "object-storage", "object_storage"}
    ):
        raise SystemExit(
            "HARBOR_REPLICAS greater than 1 requires external PostgreSQL, external Redis/Valkey, "
            "and S3 registry storage"
        )
    rendered = harbor_bootstrap_values(
        host,
        tls_cert_source,
        tls_secret_name,
        os.environ.get("HARBOR_REGISTRY_SIZE", "50Gi").strip() or "50Gi",
        os.environ.get("HARBOR_JOBLOG_SIZE", "5Gi").strip() or "5Gi",
        os.environ.get("HARBOR_DATABASE_SIZE", "10Gi").strip() or "10Gi",
        os.environ.get("HARBOR_REDIS_SIZE", "5Gi").strip() or "5Gi",
        os.environ.get("HARBOR_TRIVY_SIZE", "10Gi").strip() or "10Gi",
        storage_class,
        os.environ.get("HARBOR_ADMIN_SECRET_NAME", "harbor-admin").strip() or "harbor-admin",
        os.environ.get("HARBOR_SECRET_KEY_SECRET_NAME", "harbor-secret-key").strip() or "harbor-secret-key",
        registry_storage_block,
        database_block,
        redis_block,
        core_redis_url_env_block,
        f"Uses {database_note}, {redis_note}, and {registry_note}.",
        replicas,
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def prometheus_oauth2_proxy_manifests(
    prometheus_host: str,
    keycloak_host: str,
    realm: str,
    secret_name: str,
) -> str:
    return f"""
extraManifests:
  - apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: prometheus-oauth2-proxy
      namespace: monitoring
      labels:
        app.kubernetes.io/name: prometheus-oauth2-proxy
        app.kubernetes.io/part-of: platform-monitoring
    spec:
      replicas: 2
      strategy:
        type: RollingUpdate
        rollingUpdate:
          maxUnavailable: 1
          maxSurge: 1
      selector:
        matchLabels:
          app.kubernetes.io/name: prometheus-oauth2-proxy
      template:
        metadata:
          labels:
            app.kubernetes.io/name: prometheus-oauth2-proxy
            app.kubernetes.io/part-of: platform-monitoring
        spec:
          automountServiceAccountToken: false
          securityContext:
            runAsNonRoot: true
            runAsUser: 65532
            runAsGroup: 65532
            fsGroup: 65532
            seccompProfile:
              type: RuntimeDefault
          affinity:
            podAntiAffinity:
              requiredDuringSchedulingIgnoredDuringExecution:
                - labelSelector:
                    matchLabels:
                      app.kubernetes.io/name: prometheus-oauth2-proxy
                  topologyKey: kubernetes.io/hostname
          containers:
            - name: oauth2-proxy
              image: quay.io/oauth2-proxy/oauth2-proxy:v7.15.3
              imagePullPolicy: IfNotPresent
              args:
                - --provider=keycloak-oidc
                - --oidc-issuer-url=https://{keycloak_host}/realms/{realm}
                - --client-id=prometheus
                - --redirect-url=https://{prometheus_host}/oauth2/callback
                - --email-domain=*
                - --allowed-role=platform-viewer
                - --upstream=http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
                - --http-address=0.0.0.0:4180
                - --reverse-proxy=true
                - --cookie-secure=true
                - --cookie-samesite=lax
                - --cookie-name=__Host-platform-prometheus
                - --cookie-refresh=1h
                - --cookie-expire=8h
                - --scope=openid profile email groups
                - --skip-provider-button=true
                - --code-challenge-method=S256
                - --set-xauthrequest=true
                - --pass-access-token=true
                - --silence-ping-logging=true
              env:
                - name: OAUTH2_PROXY_CLIENT_SECRET
                  valueFrom:
                    secretKeyRef:
                      name: {yaml_string(secret_name)}
                      key: client-secret
                - name: OAUTH2_PROXY_COOKIE_SECRET
                  valueFrom:
                    secretKeyRef:
                      name: {yaml_string(secret_name)}
                      key: cookie-secret
              ports:
                - name: http
                  containerPort: 4180
              readinessProbe:
                httpGet:
                  path: /ready
                  port: http
                periodSeconds: 10
                timeoutSeconds: 3
              livenessProbe:
                httpGet:
                  path: /ping
                  port: http
                periodSeconds: 10
                timeoutSeconds: 3
              resources:
                requests:
                  cpu: 50m
                  memory: 64Mi
                limits:
                  memory: 256Mi
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities:
                  drop:
                    - ALL
  - apiVersion: v1
    kind: Service
    metadata:
      name: prometheus-oauth2-proxy
      namespace: monitoring
      labels:
        app.kubernetes.io/name: prometheus-oauth2-proxy
    spec:
      selector:
        app.kubernetes.io/name: prometheus-oauth2-proxy
      ports:
        - name: http
          port: 4180
          targetPort: http
  - apiVersion: policy/v1
    kind: PodDisruptionBudget
    metadata:
      name: prometheus-oauth2-proxy
      namespace: monitoring
    spec:
      minAvailable: 1
      unhealthyPodEvictionPolicy: AlwaysAllow
      selector:
        matchLabels:
          app.kubernetes.io/name: prometheus-oauth2-proxy
  - apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: prometheus-authenticated
      namespace: monitoring
      annotations:
        traefik.ingress.kubernetes.io/router.entrypoints: websecure
        traefik.ingress.kubernetes.io/router.tls: "true"
    spec:
      ingressClassName: traefik
      rules:
        - host: {yaml_string(prometheus_host)}
          http:
            paths:
              - path: /
                pathType: Prefix
                backend:
                  service:
                    name: prometheus-oauth2-proxy
                    port:
                      number: 4180
      tls:
        - secretName: prometheus-tls
          hosts:
            - {yaml_string(prometheus_host)}
"""


def monitoring_bootstrap_values(
    prometheus_host: str,
    grafana_host: str,
    retention_size: str,
    prometheus_size: str,
    alertmanager_size: str,
    grafana_size: str,
    storage_class: str,
    grafana_admin_secret_name: str,
    grafana_database_block: str,
    grafana_database_note: str,
    grafana_database_mode: str,
    grafana_replicas: str,
    sso_enabled: bool,
    keycloak_host: str,
    sso_realm: str,
    grafana_sso_secret_name: str,
    prometheus_sso_secret_name: str,
) -> str:
    grafana_external_database = grafana_database_mode in {"postgres", "postgresql", "external"}
    if grafana_external_database:
        grafana_availability_block = f"""  replicas: {grafana_replicas}
  deploymentStrategy:
    type: RollingUpdate
  podDisruptionBudget:
    minAvailable: 1
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: DoNotSchedule"""
        grafana_persistence_block = """  persistence:
    enabled: false"""
        grafana_tls_mount_block = """  extraConfigmapMounts:
    - name: platform-postgres-ca
      mountPath: /etc/ssl/platform-postgres
      configMap: platform-internal-roots
      readOnly: true"""
    else:
        grafana_availability_block = "  replicas: 1"
        grafana_persistence_block = f"""  persistence:
    enabled: true
    type: pvc
    storageClassName: {yaml_string(storage_class)}
    accessModes:
      - ReadWriteOnce
    size: {yaml_string(grafana_size)}"""
        grafana_tls_mount_block = ""
    if sso_enabled:
        if "  grafana.ini:\n" in grafana_database_block:
            grafana_database_block = grafana_database_block.replace(
                "  grafana.ini:\n",
                f"  envFromSecret: {yaml_string(grafana_sso_secret_name)}\n  grafana.ini:\n",
                1,
            )
        else:
            grafana_database_block = (
                f"  envFromSecret: {yaml_string(grafana_sso_secret_name)}\n"
                "  grafana.ini:\n"
            )
        grafana_database_block = grafana_database_block.rstrip() + f"""
    server:
      root_url: {yaml_string(f"https://{grafana_host}")}
    auth:
      disable_login_form: true
      oauth_auto_login: true
    auth.generic_oauth:
      enabled: true
      name: Platform SSO
      allow_sign_up: true
      client_id: grafana
      client_secret: {yaml_string("$__env{GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET}")}
      scopes: openid profile email groups
      auth_url: {yaml_string(f"https://{keycloak_host}/realms/{sso_realm}/protocol/openid-connect/auth")}
      token_url: {yaml_string(f"https://{keycloak_host}/realms/{sso_realm}/protocol/openid-connect/token")}
      api_url: {yaml_string(f"https://{keycloak_host}/realms/{sso_realm}/protocol/openid-connect/userinfo")}
      use_pkce: true
      use_refresh_token: true
      login_attribute_path: preferred_username
      email_attribute_path: email
      role_attribute_path: {yaml_string("contains(groups[*], 'platform-admin') && 'GrafanaAdmin' || 'Viewer'")}
      role_attribute_strict: true
      allow_assign_grafana_admin: true
"""
        prometheus_ingress_block = "  ingress:\n    enabled: false"
        extra_manifests = prometheus_oauth2_proxy_manifests(
            prometheus_host,
            keycloak_host,
            sso_realm,
            prometheus_sso_secret_name,
        )
    else:
        prometheus_ingress_block = f"""  ingress:
    enabled: true
    ingressClassName: traefik
    hosts:
      - {yaml_string(prometheus_host)}
    tls:
      - secretName: prometheus-tls
        hosts:
          - {yaml_string(prometheus_host)}"""
        extra_manifests = ""

    loki_env = """  envValueFrom:
    LOKI_GATEWAY_USERNAME:
      secretKeyRef:
        name: platform-loki-client
        key: username
    LOKI_GATEWAY_PASSWORD:
      secretKeyRef:
        name: platform-loki-client
        key: password
"""
    if "  envValueFrom:\n" in grafana_database_block:
        grafana_database_block = grafana_database_block.replace(
            "  envValueFrom:\n",
            loki_env,
            1,
        )
    else:
        grafana_database_block = loki_env + grafana_database_block

    return f"""# Monitoring bootstrap profile rendered by scripts/render_private_platform_values.py.
# {grafana_database_note}
crds:
  enabled: true

prometheus:
  podDisruptionBudget:
    enabled: true
    minAvailable: 1
{prometheus_ingress_block}
  prometheusSpec:
    replicas: 2
    podAntiAffinity: hard
    podAntiAffinityTopologyKey: kubernetes.io/hostname
    retention: 15d
    retentionSize: {yaml_string(retention_size)}
    podMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelectorNilUsesHelmValues: false
    resources:
      requests:
        cpu: 250m
        memory: 2Gi
      limits:
        memory: 4Gi
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
  podDisruptionBudget:
    enabled: true
    minAvailable: 2
  alertmanagerSpec:
    useExistingSecret: true
    configSecret: alertmanager-platform-config
    replicas: 3
    podAntiAffinity: hard
    podAntiAffinityTopologyKey: kubernetes.io/hostname
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        memory: 512Mi
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
{grafana_availability_block}
  admin:
    existingSecret: {yaml_string(grafana_admin_secret_name)}
    userKey: admin-user
    passwordKey: admin-password
{grafana_database_block}
{grafana_tls_mount_block}
  additionalDataSources:
    - name: Loki
      uid: loki
      type: loki
      access: proxy
      url: http://loki-gateway.logging.svc.cluster.local
      basicAuth: true
      basicAuthUser: {yaml_string("$__env{LOKI_GATEWAY_USERNAME}")}
      editable: false
      jsonData:
        httpHeaderName1: X-Scope-OrgID
      secureJsonData:
        basicAuthPassword: {yaml_string("$__env{LOKI_GATEWAY_PASSWORD}")}
        httpHeaderValue1: platform
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 512Mi
{grafana_persistence_block}
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
{extra_manifests.rstrip()}
"""


def grafana_database_settings() -> tuple[str, str, str]:
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    default_mode = "postgres" if production_strict else "sqlite"
    database_mode = os.environ.get("GRAFANA_DATABASE_MODE", default_mode).strip().lower() or default_mode
    if database_mode in {"sqlite", "internal", "local"}:
        if production_strict:
            raise SystemExit(
                "GRAFANA_DATABASE_MODE must be postgres when PLATFORM_PRODUCTION_STRICT=true"
            )
        return (
            "",
            "Uses persistent Grafana SQLite for first deployment. Set GRAFANA_DATABASE_MODE=postgres for long-term HA.",
            "sqlite",
        )
    if database_mode not in {"postgres", "postgresql", "external"}:
        raise SystemExit("GRAFANA_DATABASE_MODE must be sqlite, postgres, postgresql, or external")

    host = require(
        "GRAFANA_DATABASE_HOST",
        os.environ.get(
            "GRAFANA_DATABASE_HOST",
            "platform-postgres-rw.platform-databases.svc.cluster.local",
        ).strip(),
    )
    port = os.environ.get("GRAFANA_DATABASE_PORT", "5432").strip() or "5432"
    database_host = host if ":" in host else f"{host}:{port}"
    database_name = os.environ.get("GRAFANA_DATABASE_NAME", "grafana").strip() or "grafana"
    database_user = os.environ.get("GRAFANA_DATABASE_USER", "grafana").strip() or "grafana"
    secret_name = os.environ.get("GRAFANA_DATABASE_SECRET_NAME", "grafana-database").strip() or "grafana-database"
    ssl_mode = postgres_ssl_mode("GRAFANA_DATABASE_SSL_MODE")
    block = f"""  envValueFrom:
    GF_DATABASE_PASSWORD:
      secretKeyRef:
        name: {yaml_string(secret_name)}
        key: password
  grafana.ini:
    database:
      type: postgres
      host: {yaml_string(database_host)}
      name: {yaml_string(database_name)}
      user: {yaml_string(database_user)}
      password: {yaml_string("$__env{GF_DATABASE_PASSWORD}")}
      ssl_mode: {yaml_string(ssl_mode)}
      ca_cert_path: /etc/ssl/platform-postgres/ca-certificates.crt
"""
    return (
        block,
        f"Uses external PostgreSQL for Grafana state. Store the password in secret/{secret_name} key password.",
        "postgres",
    )


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
    storage_class = os.environ.get("MONITORING_STORAGE_CLASS", "longhorn-standard-encrypted").strip() or "longhorn-standard-encrypted"
    grafana_database_block, grafana_database_note, grafana_database_mode = grafana_database_settings()
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    sso_enabled = platform_sso_enabled()
    keycloak_host = require(
        "PLATFORM_KEYCLOAK_HOST or platform_keycloak_host",
        platform_host("PLATFORM_KEYCLOAK_HOST", inventory, ("platform_keycloak_host",), "sso"),
    ) if sso_enabled else ""
    default_replicas = "2" if grafana_database_mode == "postgres" else "1"
    grafana_replicas = os.environ.get("GRAFANA_REPLICAS", default_replicas).strip() or default_replicas
    if int(grafana_replicas) < 1:
        raise SystemExit("GRAFANA_REPLICAS must be at least 1")
    if grafana_database_mode == "sqlite" and int(grafana_replicas) != 1:
        raise SystemExit("GRAFANA_REPLICAS must be 1 when GRAFANA_DATABASE_MODE=sqlite")
    if production_strict and int(grafana_replicas) < 2:
        raise SystemExit("GRAFANA_REPLICAS must be at least 2 when PLATFORM_PRODUCTION_STRICT=true")
    rendered = monitoring_bootstrap_values(
        prometheus_host,
        grafana_host,
        os.environ.get("PROMETHEUS_RETENTION_SIZE", "20GB").strip() or "20GB",
        os.environ.get("PROMETHEUS_DATA_SIZE", "50Gi").strip() or "50Gi",
        os.environ.get("ALERTMANAGER_DATA_SIZE", "10Gi").strip() or "10Gi",
        os.environ.get("GRAFANA_DATA_SIZE", "10Gi").strip() or "10Gi",
        storage_class,
        os.environ.get("GRAFANA_ADMIN_SECRET_NAME", "grafana-admin").strip() or "grafana-admin",
        grafana_database_block,
        grafana_database_note,
        grafana_database_mode,
        grafana_replicas,
        sso_enabled,
        keycloak_host,
        os.environ.get("PLATFORM_SSO_REALM", "platform").strip() or "platform",
        os.environ.get("PLATFORM_SSO_GRAFANA_SECRET_NAME", "platform-sso-grafana").strip()
        or "platform-sso-grafana",
        os.environ.get("PLATFORM_SSO_PROMETHEUS_SECRET_NAME", "platform-sso-prometheus").strip()
        or "platform-sso-prometheus",
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
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
    retention_period: str,
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
  auth_enabled: true
  commonConfig:
    replication_factor: 3
  limits_config:
    retention_period: {yaml_string(retention_period)}
  compactor:
    retention_enabled: true
    delete_request_store: s3
    delete_request_cancel_period: 24h
    retention_delete_delay: 2h
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
  resources:
    requests:
      cpu: 250m
      memory: 1Gi
    limits:
      memory: 2Gi
  persistence:
    enableStatefulSetAutoDeletePVC: false
    storageClass: {yaml_string(storage_class)}
    size: {yaml_string(write_cache_size)}

read:
  replicas: 3
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
    limits:
      memory: 1Gi

backend:
  replicas: 3
  resources:
    requests:
      cpu: 250m
      memory: 1Gi
    limits:
      memory: 2Gi
  persistence:
    enableStatefulSetAutoDeletePVC: false
    storageClass: {yaml_string(storage_class)}
    size: {yaml_string(backend_cache_size)}

gateway:
  enabled: true
  replicas: 3
  basicAuth:
    enabled: true
    existingSecret: loki-gateway-basic-auth
  nginxConfig:
    locationSnippet: "proxy_set_header X-Scope-OrgID platform;"
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
    limits:
      memory: 256Mi
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

lokiCanary:
  enabled: true
  push: true
  extraArgs:
    - -tenant-id=platform
    - -user=$(LOKI_GATEWAY_USERNAME)
    - -pass=$(LOKI_GATEWAY_PASSWORD)
  extraEnv:
    - name: LOKI_GATEWAY_USERNAME
      valueFrom:
        secretKeyRef:
          name: loki-gateway-basic-auth
          key: username
    - name: LOKI_GATEWAY_PASSWORD
      valueFrom:
        secretKeyRef:
          name: loki-gateway-basic-auth
          key: password
"""


def render_loki(path: Path, inventory: dict[str, str]) -> bool:
    host = require(
        "PLATFORM_LOKI_HOST or platform_loki_host",
        platform_host("PLATFORM_LOKI_HOST", inventory, ("platform_loki_host",), "loki"),
    )
    endpoint = os.environ.get("OBJECT_STORAGE_ENDPOINT", INTERNAL_MINIO_ENDPOINT).strip()
    region = os.environ.get("OBJECT_STORAGE_REGION", "us-east-1").strip() or "us-east-1"
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    storage_class = os.environ.get("LOKI_STORAGE_CLASS", "longhorn-standard-encrypted").strip() or "longhorn-standard-encrypted"
    object_secret_name = os.environ.get("LOKI_OBJECT_STORAGE_SECRET_NAME", "loki-object-storage").strip()
    force_path_style = os.environ.get("OBJECT_STORAGE_FORCE_PATH_STYLE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    insecure = os.environ.get(
        "OBJECT_STORAGE_INSECURE", str(endpoint.lower().startswith("http://"))
    ).strip().lower() in {"1", "true", "yes"}
    retention_period = os.environ.get("LOKI_RETENTION_PERIOD", "720h").strip() or "720h"
    if not re.fullmatch(r"[1-9][0-9]*(?:h|d|w)", retention_period):
        raise SystemExit("LOKI_RETENTION_PERIOD must be a positive duration such as 720h or 30d")

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
        retention_period=retention_period,
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
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

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    memory: 512Mi

nodeAgent:
  resources:
    requests:
      cpu: 250m
      memory: 256Mi
    limits:
      memory: 1Gi

snapshotsEnabled: true

schedules:
  platform-daily:
    disabled: false
    schedule: {yaml_string(schedule)}
    template:
      ttl: 720h0m0s
      snapshotMoveData: true
      includedNamespaces:
        - argocd
        - cert-manager
        - cnpg-system
        - external-secrets
        - forgejo
        - harbor
        - keycloak
        - logging
        - longhorn-system
        - metallb-system
        - monitoring
        - object-storage
        - openbao
        - platform-cache
        - platform-databases
        - step-ca
        - tetragon
        - traefik
        - velero
        - woodpecker

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
"""


def render_velero(path: Path) -> bool:
    provider = os.environ.get("BACKUP_PROVIDER", "aws").strip() or "aws"
    if provider != "aws":
        raise SystemExit("BACKUP_PROVIDER currently supports aws for automatic Velero rendering")
    endpoint = backup_object_storage_endpoint()
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
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def cnpg_required_managed_database_roles() -> list[dict[str, object]]:
    """Return the application roles required by the selected database modes."""
    roles: list[dict[str, object]] = [
        {
            "name": "keycloak",
            "ensure": "present",
            "login": True,
            "superuser": False,
            "passwordSecret": {
                "name": os.environ.get(
                    "KEYCLOAK_DATABASE_SECRET_NAME", "keycloak-database"
                ).strip()
                or "keycloak-database"
            },
        }
    ]

    woodpecker_database_mode = (
        os.environ.get("WOODPECKER_DATABASE_MODE", "postgres").strip().lower()
        or "postgres"
    )
    if woodpecker_database_mode in {"postgres", "postgresql", "external"}:
        roles.append(
            {
                "name": os.environ.get(
                    "WOODPECKER_DATABASE_USER", "woodpecker"
                ).strip()
                or "woodpecker",
                "ensure": "present",
                "login": True,
                "superuser": False,
                "passwordSecret": {
                    "name": os.environ.get(
                        "WOODPECKER_DATABASE_SECRET_NAME", "woodpecker-database"
                    ).strip()
                    or "woodpecker-database"
                },
            }
        )

    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    harbor_database_mode = os.environ.get(
        "HARBOR_DATABASE_MODE", "external" if production_strict else "internal"
    ).strip().lower()
    if harbor_database_mode in {"external", "postgres", "postgresql"}:
        roles.append(
            {
                "name": os.environ.get("HARBOR_DATABASE_USER", "harbor").strip()
                or "harbor",
                "ensure": "present",
                "login": True,
                "superuser": False,
                "passwordSecret": {
                    "name": os.environ.get(
                        "HARBOR_DATABASE_SECRET_NAME", "harbor-database"
                    ).strip()
                    or "harbor-database"
                },
            }
        )

    grafana_database_mode = os.environ.get(
        "GRAFANA_DATABASE_MODE", "postgres" if production_strict else "sqlite"
    ).strip().lower()
    if grafana_database_mode in {"external", "postgres", "postgresql"}:
        roles.append(
            {
                "name": os.environ.get("GRAFANA_DATABASE_USER", "grafana").strip()
                or "grafana",
                "ensure": "present",
                "login": True,
                "superuser": False,
                "passwordSecret": {
                    "name": os.environ.get(
                        "GRAFANA_DATABASE_SECRET_NAME", "grafana-database"
                    ).strip()
                    or "grafana-database"
                },
            }
        )
    return roles


def cnpg_managed_database_roles_block() -> str:
    blocks: list[str] = []
    for role in cnpg_required_managed_database_roles():
        password_secret = role["passwordSecret"]
        if not isinstance(password_secret, dict):
            raise AssertionError("CNPG managed role passwordSecret must be a mapping")
        blocks.append(
            f"""      - name: {yaml_string(str(role['name']))}
        ensure: present
        login: true
        superuser: false
        passwordSecret:
          name: {yaml_string(str(password_secret['name']))}"""
        )
    return "\n".join(blocks)


def cnpg_server_certificate_secret(
    documents: list[object],
    cluster: dict[str, object],
    path: Path,
) -> str | None:
    """Return the existing server Certificate secret for a private CNPG cluster."""
    metadata = cluster.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit(f"expected CloudNativePG metadata mapping in {path}")
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not isinstance(name, str) or not name.strip():
        raise SystemExit(f"expected CloudNativePG metadata.name in {path}")
    if not isinstance(namespace, str) or not namespace.strip():
        raise SystemExit(f"expected CloudNativePG metadata.namespace in {path}")

    expected_certificate_name = f"{name}-server"
    expected_rw_dns = f"{name}-rw.{namespace}.svc.cluster.local"
    candidates: list[str] = []
    for document in documents:
        if (
            not isinstance(document, dict)
            or document.get("apiVersion") != "cert-manager.io/v1"
            or document.get("kind") != "Certificate"
        ):
            continue
        certificate_metadata = document.get("metadata")
        certificate_spec = document.get("spec")
        if not isinstance(certificate_metadata, dict) or not isinstance(certificate_spec, dict):
            continue
        if certificate_metadata.get("namespace") != namespace:
            continue
        dns_names = certificate_spec.get("dnsNames")
        matches_cluster = certificate_metadata.get("name") == expected_certificate_name or (
            isinstance(dns_names, list) and expected_rw_dns in dns_names
        )
        if not matches_cluster:
            continue
        secret_name = certificate_spec.get("secretName")
        if not isinstance(secret_name, str) or not secret_name.strip():
            raise SystemExit(
                f"matching CloudNativePG server Certificate is missing spec.secretName in {path}"
            )
        candidates.append(secret_name.strip())

    unique_candidates = sorted(set(candidates))
    if len(unique_candidates) > 1:
        raise SystemExit(
            f"multiple CloudNativePG server Certificate secrets match cluster {name!r} in {path}"
        )
    return unique_candidates[0] if unique_candidates else None


def refresh_cnpg_managed_database_roles(path: Path) -> bool:
    """Refresh Woodpecker's shared CNPG contract without replacing private state."""
    text = read_bounded_text(path, encoding="utf-8")
    try:
        documents = loads_strict_yaml_all(text)
    except StrictYamlError as exc:
        raise SystemExit(f"cannot refresh CNPG managed roles in invalid YAML {path}: {exc}") from exc
    if not documents or any(not isinstance(document, dict) for document in documents):
        raise SystemExit(f"expected only Kubernetes resource mappings in {path}")

    clusters = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("apiVersion") == "postgresql.cnpg.io/v1"
        and document.get("kind") == "Cluster"
    ]
    if len(clusters) != 1:
        raise SystemExit(f"expected exactly one CloudNativePG Cluster in {path}")

    cluster = clusters[0]
    spec = cluster.get("spec")
    if not isinstance(spec, dict):
        raise SystemExit(f"expected CloudNativePG spec mapping in {path}")

    certificates = spec.get("certificates")
    if certificates is None:
        certificates = {}
        spec["certificates"] = certificates
    if not isinstance(certificates, dict):
        raise SystemExit(f"expected CloudNativePG spec.certificates mapping in {path}")
    certificate_keys = ("serverCASecret", "serverTLSSecret")
    expected_certificate_references: dict[str, str] = {}
    missing_certificate_keys: list[str] = []
    for key in certificate_keys:
        value = certificates.get(key)
        if value is None:
            missing_certificate_keys.append(key)
            continue
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"expected CloudNativePG spec.certificates.{key} name in {path}")
        expected_certificate_references[key] = value.strip()
    discovered_server_secret = (
        cnpg_server_certificate_secret(documents, cluster, path)
        if missing_certificate_keys
        else None
    )
    if missing_certificate_keys and discovered_server_secret is None:
        raise SystemExit(
            "cannot restore missing CloudNativePG server TLS references in "
            f"{path}; retain a matching cert-manager Certificate or configure each reference explicitly"
        )
    for key in missing_certificate_keys:
        expected_certificate_references[key] = discovered_server_secret

    managed = spec.get("managed")
    if managed is None:
        managed = {}
        spec["managed"] = managed
    if not isinstance(managed, dict):
        raise SystemExit(f"expected CloudNativePG spec.managed mapping in {path}")
    roles = managed.get("roles")
    if roles is None:
        roles = []
        managed["roles"] = roles
    if not isinstance(roles, list):
        raise SystemExit(f"expected CloudNativePG spec.managed.roles list in {path}")

    roles_by_name: dict[str, dict[str, object]] = {}
    for role in roles:
        if not isinstance(role, dict) or not isinstance(role.get("name"), str):
            raise SystemExit(f"expected named CloudNativePG managed role mappings in {path}")
        name = role["name"]
        if name in roles_by_name:
            raise SystemExit(f"duplicate CloudNativePG managed role {name!r} in {path}")
        roles_by_name[name] = role

    changed = False
    for key in missing_certificate_keys:
        if certificates.get(key) != expected_certificate_references[key]:
            certificates[key] = expected_certificate_references[key]
            changed = True
    for required_role in cnpg_required_managed_database_roles():
        name = str(required_role["name"])
        existing_role = roles_by_name.get(name)
        if existing_role is None:
            new_role = {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in required_role.items()
            }
            roles.append(new_role)
            roles_by_name[name] = new_role
            changed = True
            continue
        for key, required_value in required_role.items():
            normalized_value = (
                dict(required_value)
                if isinstance(required_value, dict)
                else required_value
            )
            if existing_role.get(key) != normalized_value:
                existing_role[key] = normalized_value
                changed = True

    if not changed:
        return False

    rendered = yaml.safe_dump_all(
        documents,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )
    try:
        refreshed_documents = loads_strict_yaml_all(rendered)
    except StrictYamlError as exc:
        raise SystemExit(f"refreshed CNPG manifest is invalid YAML in {path}: {exc}") from exc
    refreshed_clusters = [
        document
        for document in refreshed_documents
        if isinstance(document, dict)
        and document.get("apiVersion") == "postgresql.cnpg.io/v1"
        and document.get("kind") == "Cluster"
    ]
    refreshed_certificates = refreshed_clusters[0]["spec"].get("certificates")
    if not isinstance(refreshed_certificates, dict) or any(
        refreshed_certificates.get(key) != expected_certificate_references[key]
        for key in certificate_keys
    ):
        raise SystemExit(f"failed to refresh CloudNativePG server TLS references in {path}")
    refreshed_roles = refreshed_clusters[0]["spec"]["managed"]["roles"]
    refreshed_by_name = {
        role["name"]: role
        for role in refreshed_roles
        if isinstance(role, dict) and isinstance(role.get("name"), str)
    }
    for required_role in cnpg_required_managed_database_roles():
        refreshed_role = refreshed_by_name.get(required_role["name"])
        if not isinstance(refreshed_role, dict) or any(
            refreshed_role.get(key) != value
            for key, value in required_role.items()
        ):
            raise SystemExit(
                f"failed to refresh required CNPG role {required_role['name']!r} in {path}"
            )

    atomic_write_text(path, rendered)
    return True


def cnpg_postgres_cluster_manifest(
    namespace: str,
    name: str,
    instances: str,
    data_size: str,
    storage_class: str,
    app_database: str,
    app_owner: str,
    app_secret_name: str,
    backup_destination: str,
    endpoint: str,
    secret_name: str,
    retention_policy: str,
    schedule: str,
    backup_enabled: bool,
    image_name: str,
) -> str:
    backup_block = ""
    scheduled_backup_block = ""
    if backup_enabled:
        backup_block = f"""
  backup:
    retentionPolicy: {yaml_string(retention_policy)}
    barmanObjectStore:
      destinationPath: {yaml_string(backup_destination)}
      endpointURL: {yaml_string(endpoint)}
      s3Credentials:
        accessKeyId:
          name: {yaml_string(secret_name)}
          key: ACCESS_KEY_ID
        secretAccessKey:
          name: {yaml_string(secret_name)}
          key: SECRET_ACCESS_KEY
      wal:
        compression: gzip
        maxParallel: 4
      data:
        compression: gzip
        jobs: 2"""
        scheduled_backup_block = f"""
---
apiVersion: postgresql.cnpg.io/v1
kind: ScheduledBackup
metadata:
  name: {yaml_string(name + "-daily")}
  namespace: {yaml_string(namespace)}
spec:
  schedule: {yaml_string(schedule)}
  backupOwnerReference: self
  cluster:
    name: {yaml_string(name)}
  method: barmanObjectStore"""

    managed_roles_block = cnpg_managed_database_roles_block()

    return f"""apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: {yaml_string(name + "-server")}
  namespace: {yaml_string(namespace)}
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
spec:
  secretName: {yaml_string(name + "-server-tls")}
  secretTemplate:
    labels:
      cnpg.io/reload: ""
  duration: 2160h
  renewBefore: 360h
  privateKey:
    algorithm: ECDSA
    size: 384
    rotationPolicy: Always
  usages:
    - server auth
  dnsNames:
    - {yaml_string(name + "-rw")}
    - {yaml_string(name + "-rw." + namespace)}
    - {yaml_string(name + "-rw." + namespace + ".svc")}
    - {yaml_string(name + "-rw." + namespace + ".svc.cluster.local")}
    - {yaml_string(name + "-r")}
    - {yaml_string(name + "-r." + namespace)}
    - {yaml_string(name + "-r." + namespace + ".svc")}
    - {yaml_string(name + "-r." + namespace + ".svc.cluster.local")}
    - {yaml_string(name + "-ro")}
    - {yaml_string(name + "-ro." + namespace)}
    - {yaml_string(name + "-ro." + namespace + ".svc")}
    - {yaml_string(name + "-ro." + namespace + ".svc.cluster.local")}
  issuerRef:
    name: platform-internal-ca
    kind: ClusterIssuer
    group: cert-manager.io
---
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: {yaml_string(name)}
  namespace: {yaml_string(namespace)}
  annotations:
    argocd.argoproj.io/sync-wave: "0"
spec:
  instances: {instances}
  imageName: {yaml_string(image_name)}
  primaryUpdateStrategy: unsupervised
  certificates:
    serverCASecret: {yaml_string(name + "-server-tls")}
    serverTLSSecret: {yaml_string(name + "-server-tls")}
  managed:
    roles:
{managed_roles_block}
  bootstrap:
    initdb:
      database: {yaml_string(app_database)}
      owner: {yaml_string(app_owner)}
      secret:
        name: {yaml_string(app_secret_name)}
  storage:
    size: {yaml_string(data_size)}
    storageClass: {yaml_string(storage_class)}
  monitoring:
    enablePodMonitor: true
{backup_block}{scheduled_backup_block}
"""


def render_cnpg_postgres_cluster(path: Path) -> bool:
    bucket_prefix = os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform").strip() or "platform"
    namespace = os.environ.get("CNPG_CLUSTER_NAMESPACE", "platform-databases").strip() or "platform-databases"
    name = os.environ.get("CNPG_CLUSTER_NAME", "platform-postgres").strip() or "platform-postgres"
    backup_mode = os.environ.get("CNPG_BACKUP_ENABLED", "true").strip().lower()
    if backup_mode not in {"0", "1", "false", "true", "no", "yes", "disabled", "enabled"}:
        raise SystemExit("CNPG_BACKUP_ENABLED must be true or false")
    backup_enabled = backup_mode in {"1", "true", "yes", "enabled"}
    production_strict = env_bool("PLATFORM_PRODUCTION_STRICT", True)
    if production_strict and not backup_enabled:
        raise SystemExit(
            "CNPG_BACKUP_ENABLED must remain true when PLATFORM_PRODUCTION_STRICT=true"
        )
    endpoint = backup_object_storage_endpoint() if backup_enabled else ""
    instances = os.environ.get("CNPG_INSTANCES", "3").strip() or "3"
    if int(instances) < 1:
        raise SystemExit("CNPG_INSTANCES must be at least 1")
    backup_destination = os.environ.get(
        "CNPG_BACKUP_DESTINATION",
        f"s3://{bucket_prefix}-cnpg-backups/{name}",
    ).strip()
    rendered = cnpg_postgres_cluster_manifest(
        namespace=namespace,
        name=name,
        instances=instances,
        data_size=os.environ.get("POSTGRES_DATA_SIZE", "50Gi").strip() or "50Gi",
        storage_class=os.environ.get("CNPG_STORAGE_CLASS", "longhorn-critical-encrypted").strip() or "longhorn-critical-encrypted",
        app_database=os.environ.get("FORGEJO_DATABASE_NAME", "forgejo").strip() or "forgejo",
        app_owner=os.environ.get("FORGEJO_DATABASE_USER", "forgejo").strip() or "forgejo",
        app_secret_name=os.environ.get("FORGEJO_DATABASE_SECRET_NAME", "forgejo-database").strip()
        or "forgejo-database",
        backup_destination=backup_destination,
        endpoint=endpoint,
        secret_name=os.environ.get("CNPG_OBJECT_STORE_SECRET_NAME", "cnpg-object-store").strip()
        or "cnpg-object-store",
        retention_policy=os.environ.get("CNPG_RETENTION_POLICY", "30d").strip() or "30d",
        schedule=os.environ.get("CNPG_BACKUP_SCHEDULE", "0 2 * * *").strip() or "0 2 * * *",
        backup_enabled=backup_enabled,
        image_name=os.environ.get(
            "CNPG_POSTGRES_IMAGE",
            "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie",
        ).strip()
        or "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie",
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
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
    storage_class = os.environ.get("STEP_CA_STORAGE_CLASS", "longhorn-critical-encrypted").strip() or "longhorn-critical-encrypted"
    db_size = os.environ.get("STEP_CA_DB_SIZE", "10Gi").strip() or "10Gi"

    rendered = step_ca_bootstrap_values(
        name=name,
        dns_names=dns_names,
        url=url,
        storage_class=storage_class,
        db_size=db_size,
        ingress_host=host,
    )
    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        atomic_write_text(path, rendered)
    return changed


def render_platform_policy_enforcement(paths: list[Path]) -> bool:
    """Render the requested operator mode into stable Kyverno CEL actions."""
    configured = os.environ.get("PLATFORM_POLICY_ENFORCEMENT", "Audit").strip().lower()
    modes = {"audit": "Audit", "enforce": "Deny"}
    if configured not in modes:
        raise SystemExit("PLATFORM_POLICY_ENFORCEMENT must be Audit or Enforce")

    changed = False
    for path in paths:
        text = read_bounded_text(path, encoding="utf-8")
        rendered, replacements = re.subn(
            r"(?m)^([ \t]*validationActions:[ \t]*\r?\n[ \t]*-[ \t]*)(Audit|Deny)[ \t]*$",
            lambda match: f"{match.group(1)}{modes[configured]}",
            text,
        )
        if replacements != 1:
            raise SystemExit(
                f"expected exactly one stable validationActions entry in {path}; found {replacements}"
            )
        if rendered != text:
            atomic_write_text(path, rendered)
            changed = True
    return changed


def platform_image_integrity_policy(
    registry: str,
    public_key: str,
    rekor_url: str,
    validation_action: str,
) -> str:
    key_block = "\n".join(f"            {line}" for line in public_key.splitlines())
    registry_expression = yaml_string(f"image.registry == '{registry}'")
    return f"""apiVersion: policies.kyverno.io/v1
kind: ImageValidatingPolicy
metadata:
  name: verify-platform-image-signatures
  annotations:
    policies.kyverno.io/title: Verify platform image signatures
    policies.kyverno.io/category: Software Supply Chain Security
    policies.kyverno.io/severity: high
    policies.kyverno.io/subject: Pod
    policies.kyverno.io/minversion: 1.18.0
spec:
  evaluation:
    admission:
      enabled: true
    background:
      enabled: true
  validationActions:
    - {validation_action}
  failurePolicy: Fail
  webhookConfiguration:
    timeoutSeconds: 15
  matchConstraints:
    resourceRules:
      - apiGroups:
          - ""
        apiVersions:
          - v1
        operations:
          - CREATE
          - UPDATE
        resources:
          - pods
  matchImageReferences:
    - expression: {registry_expression}
  attestors:
    - name: platformCosign
      cosign:
        key:
          data: |
{key_block}
        ctlog:
          url: {yaml_string(rekor_url)}
          insecureIgnoreTlog: false
          insecureIgnoreSCT: false
  validationConfigurations:
    mutateDigest: true
    required: true
    verifyDigest: true
  validations:
    - expression: >-
        images.containers.map(image, verifyImageSignatures(image, [attestors.platformCosign])).all(result, result > 0)
      message: Platform registry images must carry a valid Cosign signature from the approved release key.
"""


def validate_cosign_public_key(path: Path) -> str:
    try:
        raw = read_bounded_text(path, encoding="ascii")
    except FileNotFoundError as exc:
        raise SystemExit(f"PLATFORM_COSIGN_PUBLIC_KEY_FILE does not exist: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SystemExit("PLATFORM_COSIGN_PUBLIC_KEY_FILE must be an ASCII PEM public key") from exc
    if len(raw.encode("ascii")) > 65536:
        raise SystemExit("PLATFORM_COSIGN_PUBLIC_KEY_FILE exceeds the 64 KiB safety limit")

    normalized = raw.replace("\r\n", "\n").strip()
    match = re.fullmatch(
        r"-----BEGIN PUBLIC KEY-----\n([A-Za-z0-9+/=\n]+)\n-----END PUBLIC KEY-----",
        normalized,
    )
    if not match or "PRIVATE KEY" in normalized:
        raise SystemExit(
            "PLATFORM_COSIGN_PUBLIC_KEY_FILE must contain exactly one PEM PUBLIC KEY block"
        )
    try:
        der = base64.b64decode("".join(match.group(1).splitlines()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit("PLATFORM_COSIGN_PUBLIC_KEY_FILE contains invalid PEM base64") from exc
    if not 32 <= len(der) <= 16384 or der[0] != 0x30:
        raise SystemExit("PLATFORM_COSIGN_PUBLIC_KEY_FILE is not a plausible DER public key")
    return normalized


def render_platform_image_integrity(path: Path, inventory: dict[str, str]) -> bool:
    mode = os.environ.get("PLATFORM_IMAGE_INTEGRITY_MODE", "disabled").strip().lower()
    if mode not in {"disabled", "audit", "enforce"}:
        raise SystemExit(
            "PLATFORM_IMAGE_INTEGRITY_MODE must be disabled, Audit, or Enforce"
        )

    if mode == "disabled":
        rendered = platform_image_integrity_policy(
            "<PLATFORM_IMAGE_REGISTRY>",
            "<PLATFORM_COSIGN_PUBLIC_KEY>",
            "<PLATFORM_COSIGN_REKOR_URL>",
            "Audit",
        )
    else:
        registry = first_value(
            env_or_inventory(
                "PLATFORM_IMAGE_REGISTRY",
                inventory,
                "platform_image_registry",
                "platform_registry_host",
            ),
            platform_host(
                "PLATFORM_HARBOR_HOST",
                inventory,
                ("platform_harbor_host", "platform_registry_host"),
                "harbor",
            ),
        ).lower()
        require("PLATFORM_IMAGE_REGISTRY or platform_registry_host", registry)
        if (
            "://" in registry
            or "/" in registry
            or re.search(r"\s", registry)
            or not re.fullmatch(r"(?:\[[0-9a-f:]+\]|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[0-9]{1,5})?", registry)
        ):
            raise SystemExit(
                "PLATFORM_IMAGE_REGISTRY must be a registry host with optional port, without scheme or path"
            )

        public_key_file = Path(
            require(
                "PLATFORM_COSIGN_PUBLIC_KEY_FILE",
                os.environ.get("PLATFORM_COSIGN_PUBLIC_KEY_FILE", "").strip(),
            )
        ).expanduser()
        public_key = validate_cosign_public_key(public_key_file)
        rekor_url = (
            os.environ.get("PLATFORM_COSIGN_REKOR_URL", "https://rekor.sigstore.dev").strip()
            or "https://rekor.sigstore.dev"
        )
        parsed_rekor = urlparse(rekor_url)
        if (
            parsed_rekor.scheme != "https"
            or not parsed_rekor.hostname
            or parsed_rekor.username
            or parsed_rekor.password
            or parsed_rekor.query
            or parsed_rekor.fragment
        ):
            raise SystemExit(
                "PLATFORM_COSIGN_REKOR_URL must be an HTTPS URL without credentials, query, or fragment"
            )
        rendered = platform_image_integrity_policy(
            registry,
            public_key,
            rekor_url.rstrip("/"),
            "Deny" if mode == "enforce" else "Audit",
        )

    old = read_bounded_text(path, encoding="utf-8") if path.exists() else ""
    changed = rendered != old
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)
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
        "--longhorn-storageclasses",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/longhorn/storageclasses.yaml"),
    )
    parser.add_argument(
        "--argocd-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/values.yaml"),
    )
    parser.add_argument(
        "--refresh-argocd-host",
        action="store_true",
        help=(
            "Refresh only Argo CD's public hostname placeholder without changing "
            "private SSO, admin, storage, or sync settings."
        ),
    )
    parser.add_argument(
        "--skip-forgejo",
        action="store_true",
        help="Leave Forgejo values unchanged; useful for focused Woodpecker reconciliation.",
    )
    parser.add_argument(
        "--refresh-forgejo-release-pin",
        action="store_true",
        help="Refresh only Forgejo's reviewed image pin without rendering private dependencies.",
    )
    parser.add_argument(
        "--refresh-forgejo-storage",
        action="store_true",
        help="Repair existing MinIO bindings or apply explicitly requested filesystem storage without migrating data.",
    )
    parser.add_argument(
        "--refresh-forgejo-config-env",
        action="store_true",
        help="Migrate legacy Forgejo configuration environment names, preserving secret bindings.",
    )
    parser.add_argument(
        "--refresh-forgejo-postgres-tls",
        action="store_true",
        help=(
            "Refresh only Forgejo's existing PostgreSQL CA mounts and verify-full mode "
            "without rendering private object storage or credentials."
        ),
    )
    parser.add_argument(
        "--refresh-cnpg-database-roles",
        action="store_true",
        help=(
            "Refresh required CloudNativePG managed roles and existing server TLS references "
            "without re-rendering private storage or backups."
        ),
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
        "--cnpg-postgres-cluster",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/platform-postgres/postgres-cluster.yaml"),
    )
    parser.add_argument(
        "--platform-valkey-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/values.yaml"),
    )
    parser.add_argument(
        "--minio-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/minio/values.yaml"),
    )
    parser.add_argument(
        "--keycloak-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/keycloak/values.yaml"),
    )
    parser.add_argument(
        "--step-ca-values",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml"),
    )
    parser.add_argument(
        "--platform-secret-policy",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/platform-policies/no-plaintext-secrets.yaml"),
    )
    parser.add_argument(
        "--platform-workload-policy",
        type=Path,
        default=Path("gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-workload-baseline.yaml"),
    )
    parser.add_argument(
        "--platform-pod-security-policy",
        type=Path,
        default=Path(
            "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-pod-security-baseline.yaml"
        ),
    )
    parser.add_argument(
        "--platform-image-integrity-policy",
        type=Path,
        default=Path(
            "gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity/verify-platform-images.yaml"
        ),
    )
    parser.add_argument("--skip-longhorn", action="store_true")
    parser.add_argument("--skip-argocd", action="store_true")
    parser.add_argument("--skip-woodpecker", action="store_true")
    parser.add_argument("--skip-harbor", action="store_true")
    parser.add_argument("--skip-monitoring", action="store_true")
    parser.add_argument("--skip-loki", action="store_true")
    parser.add_argument("--skip-velero", action="store_true")
    parser.add_argument("--skip-cnpg-postgres-cluster", action="store_true")
    parser.add_argument("--skip-platform-valkey", action="store_true")
    parser.add_argument("--skip-minio", action="store_true")
    parser.add_argument("--skip-keycloak", action="store_true")
    parser.add_argument("--skip-step-ca", action="store_true")
    parser.add_argument("--skip-platform-image-integrity", action="store_true")
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
        print(f"FORGEJO_STORAGE_CLASS={os.environ.get('FORGEJO_STORAGE_CLASS', 'longhorn-critical-encrypted')}")
        print(f"FORGEJO_DATABASE_MODE={os.environ.get('FORGEJO_DATABASE_MODE', os.environ.get('PLATFORM_SQL_DATABASE_MODE', 'postgres'))}")
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
            "KEYCLOAK_HOST="
            + (
                platform_host("PLATFORM_KEYCLOAK_HOST", inventory, ("platform_keycloak_host",), "sso")
                or "<missing>"
            )
        )
        print(f"KEYCLOAK_ADMIN_SECRET_NAME={os.environ.get('KEYCLOAK_ADMIN_SECRET_NAME', 'keycloak-admin')}")
        print(
            "KEYCLOAK_DATABASE_HOST="
            + os.environ.get(
                "KEYCLOAK_DATABASE_HOST",
                "platform-postgres-rw.platform-databases.svc.cluster.local",
            )
        )
        print(f"KEYCLOAK_DATABASE_SECRET_NAME={os.environ.get('KEYCLOAK_DATABASE_SECRET_NAME', 'keycloak-database')}")
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
            "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE="
            f"{os.environ.get('PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE', '100')}"
        )
        print(
            "LOKI_HOST="
            + (
                platform_host("PLATFORM_LOKI_HOST", inventory, ("platform_loki_host",), "loki")
                or "<missing>"
            )
        )
        print(f"LOKI_OBJECT_STORAGE_SECRET_NAME={os.environ.get('LOKI_OBJECT_STORAGE_SECRET_NAME', 'loki-object-storage')}")
        print(f"BACKUP_PROVIDER={os.environ.get('BACKUP_PROVIDER', 'aws')}")
        print(
            "BACKUP_OBJECT_STORAGE_ENDPOINT="
            + first_value(
                os.environ.get("BACKUP_OBJECT_STORAGE_ENDPOINT", ""),
                os.environ.get("OBJECT_STORAGE_ENDPOINT", ""),
            )
        )
        print(f"BACKUP_BUCKET={os.environ.get('BACKUP_BUCKET', os.environ.get('OBJECT_STORAGE_BUCKET_PREFIX', 'platform') + '-velero-backups')}")
        print(f"VELERO_CREDENTIALS_SECRET_NAME={os.environ.get('VELERO_CREDENTIALS_SECRET_NAME', 'velero-credentials')}")
        print(f"CNPG_RENDER_POSTGRES_CLUSTER={os.environ.get('CNPG_RENDER_POSTGRES_CLUSTER', 'true')}")
        print(f"CNPG_BACKUP_ENABLED={os.environ.get('CNPG_BACKUP_ENABLED', 'true')}")
        print(f"CNPG_OBJECT_STORE_SECRET_NAME={os.environ.get('CNPG_OBJECT_STORE_SECRET_NAME', 'cnpg-object-store')}")
        print(
            "CNPG_BACKUP_DESTINATION="
            + os.environ.get(
                "CNPG_BACKUP_DESTINATION",
                "s3://"
                + os.environ.get("OBJECT_STORAGE_BUCKET_PREFIX", "platform")
                + "-cnpg-backups/"
                + os.environ.get("CNPG_CLUSTER_NAME", "platform-postgres"),
            )
        )
        print(f"PLATFORM_VALKEY_AUTH_SECRET_NAME={os.environ.get('PLATFORM_VALKEY_AUTH_SECRET_NAME', 'platform-valkey-auth')}")
        print(f"PLATFORM_VALKEY_PRIMARY_HOST={os.environ.get('PLATFORM_VALKEY_PRIMARY_HOST', 'platform-valkey-primary.platform-cache.svc.cluster.local')}")
        print(f"PLATFORM_VALKEY_REPLICA_COUNT={os.environ.get('PLATFORM_VALKEY_REPLICA_COUNT', '3')}")
        print(f"PLATFORM_VALKEY_DATA_SIZE={os.environ.get('PLATFORM_VALKEY_DATA_SIZE', '8Gi')}")
        print(f"LONGHORN_ENCRYPTION_SECRET_NAME={os.environ.get('LONGHORN_ENCRYPTION_SECRET_NAME', 'longhorn-crypto')}")
        print(f"LONGHORN_ENCRYPTION_AUTO_GENERATE={os.environ.get('LONGHORN_ENCRYPTION_AUTO_GENERATE', 'true')}")
        print(f"LONGHORN_ENCRYPTION_RECOVERY_FILE={os.environ.get('LONGHORN_ENCRYPTION_RECOVERY_FILE', 'private/longhorn-encryption.key')}")
        print(f"MINIO_ROOT_SECRET_NAME={os.environ.get('MINIO_ROOT_SECRET_NAME', 'minio-root')}")
        print(f"MINIO_DATA_SIZE={os.environ.get('MINIO_DATA_SIZE', '50Gi')}")
        print(f"MINIO_STORAGE_CLASS={os.environ.get('MINIO_STORAGE_CLASS', 'longhorn-critical-encrypted')}")
        print(f"MINIO_REPLICA_COUNT={os.environ.get('MINIO_REPLICA_COUNT', '4')}")
        print(f"STEP_CA_MODE={os.environ.get('STEP_CA_MODE', 'disabled')}")
        print(
            "STEP_CA_HOST="
            + (
                platform_host("STEP_CA_HOST", inventory, ("platform_step_ca_host",), "step-ca")
                or "<not exposed>"
            )
        )
        print(f"STEP_CA_STORAGE_CLASS={os.environ.get('STEP_CA_STORAGE_CLASS', 'longhorn-critical-encrypted')}")
        print(f"STEP_CA_DB_SIZE={os.environ.get('STEP_CA_DB_SIZE', '10Gi')}")
        print(f"PLATFORM_POLICY_ENFORCEMENT={os.environ.get('PLATFORM_POLICY_ENFORCEMENT', 'Audit')}")
        print(f"PLATFORM_IMAGE_INTEGRITY_MODE={os.environ.get('PLATFORM_IMAGE_INTEGRITY_MODE', 'disabled')}")
        return 0 if host else 1

    if (
        args.refresh_argocd_host
        and args.argocd_values.exists()
        and refresh_argocd_host(args.argocd_values, inventory)
    ):
        changed.append(str(args.argocd_values))

    if not args.skip_argocd and args.argocd_values.exists() and render_argocd(args.argocd_values, inventory):
        changed.append(str(args.argocd_values))

    if args.refresh_forgejo_config_env and refresh_forgejo_config_env(args.forgejo_values):
        changed.append(str(args.forgejo_values))

    if (
        args.refresh_forgejo_storage
        and refresh_forgejo_storage(args.forgejo_values)
    ):
        if str(args.forgejo_values) not in changed:
            changed.append(str(args.forgejo_values))

    if (
        args.refresh_forgejo_postgres_tls
        and refresh_forgejo_postgres_tls(args.forgejo_values)
    ):
        if str(args.forgejo_values) not in changed:
            changed.append(str(args.forgejo_values))

    if (
        args.refresh_forgejo_release_pin
        and refresh_forgejo_reviewed_image_pin(args.forgejo_values)
    ):
        if str(args.forgejo_values) not in changed:
            changed.append(str(args.forgejo_values))

    if (
        args.refresh_cnpg_database_roles
        and args.cnpg_postgres_cluster.exists()
        and refresh_cnpg_managed_database_roles(args.cnpg_postgres_cluster)
    ):
        changed.append(str(args.cnpg_postgres_cluster))

    if not args.skip_forgejo and render_forgejo(args.forgejo_values, inventory):
        if str(args.forgejo_values) in changed:
            changed.remove(str(args.forgejo_values))
        changed.append(str(args.forgejo_values))

    if not args.skip_longhorn and args.longhorn_values.exists():
        backup_target = os.environ.get("LONGHORN_BACKUP_TARGET", "").strip()
        if render_longhorn(args.longhorn_values, backup_target):
            changed.append(str(args.longhorn_values))
        if args.longhorn_storageclasses.exists() and render_longhorn_storageclasses(
            args.longhorn_storageclasses
        ):
            changed.append(str(args.longhorn_storageclasses))

    if (
        not args.skip_woodpecker
        and args.woodpecker_values.exists()
        and render_woodpecker(
            args.woodpecker_values,
            inventory,
            args.forgejo_values,
        )
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

    cnpg_render_mode = os.environ.get("CNPG_RENDER_POSTGRES_CLUSTER", "true").strip().lower()
    if (
        not args.skip_cnpg_postgres_cluster
        and args.cnpg_postgres_cluster.exists()
        and cnpg_render_mode in {"1", "true", "yes", "bootstrap"}
        and render_cnpg_postgres_cluster(args.cnpg_postgres_cluster)
    ):
        if str(args.cnpg_postgres_cluster) in changed:
            changed.remove(str(args.cnpg_postgres_cluster))
        changed.append(str(args.cnpg_postgres_cluster))

    if (
        not args.skip_platform_valkey
        and args.platform_valkey_values.exists()
        and render_platform_valkey(args.platform_valkey_values)
    ):
        changed.append(str(args.platform_valkey_values))

    if not args.skip_minio and args.minio_values.exists() and render_minio(args.minio_values):
        changed.append(str(args.minio_values))

    if not args.skip_keycloak and args.keycloak_values.exists() and render_keycloak(args.keycloak_values, inventory):
        changed.append(str(args.keycloak_values))

    if not args.skip_step_ca and render_step_ca(args.step_ca_values, inventory):
        changed.append(str(args.step_ca_values))

    policy_paths = [
        args.platform_secret_policy,
        args.platform_workload_policy,
        args.platform_pod_security_policy,
    ]
    if all(path.exists() for path in policy_paths) and render_platform_policy_enforcement(policy_paths):
        changed.extend(str(path) for path in policy_paths)

    if (
        not args.skip_platform_image_integrity
        and render_platform_image_integrity(args.platform_image_integrity_policy, inventory)
    ):
        changed.append(str(args.platform_image_integrity_policy))

    if changed:
        print("Rendered private platform values:")
        for path in changed:
            print(f"- {path}")
    else:
        print("Private platform values already rendered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
