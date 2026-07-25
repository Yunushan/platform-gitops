#!/usr/bin/env python3
"""Fail-closed live backup verification for the premium platform profile."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


class ProtectionError(RuntimeError):
    """Raised for a failed production data-protection contract."""


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def nested(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for segment in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


def newest_timestamp(
    objects: Iterable[dict[str, Any]], paths: Iterable[str]
) -> tuple[datetime | None, str]:
    newest: datetime | None = None
    newest_name = ""
    for item in objects:
        candidate: datetime | None = None
        for path in paths:
            candidate = parse_timestamp(nested(item, path))
            if candidate is not None:
                break
        if candidate is not None and (newest is None or candidate > newest):
            newest = candidate
            newest_name = str(nested(item, "metadata.name") or "unknown")
    return newest, newest_name


def endpoint_host(endpoint: str) -> str:
    candidate = endpoint.strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    return (parsed.hostname or "").strip().lower().rstrip(".")


def is_cluster_local_endpoint(endpoint: str) -> bool:
    host = endpoint_host(endpoint)
    if not host:
        return True
    if host in {"localhost", "minio", "object-storage", "127.0.0.1", "::1"}:
        return True
    if host.endswith((".svc", ".svc.cluster.local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_link_local


def require_external_endpoint(endpoint: str, label: str) -> None:
    if is_cluster_local_endpoint(endpoint):
        raise ProtectionError(f"{label} is missing or points inside the cluster")


def require_fresh(
    timestamp: datetime | None,
    *,
    name: str,
    label: str,
    max_age: timedelta,
    now: datetime,
) -> None:
    if timestamp is None:
        raise ProtectionError(f"{label} has no successful backup timestamp")
    age = now - timestamp
    if age < timedelta(minutes=-5):
        raise ProtectionError(f"{label} timestamp is in the future")
    if age > max_age:
        raise ProtectionError(
            f"{label} {name} is stale ({age.total_seconds() / 3600:.1f}h; "
            f"maximum {max_age.total_seconds() / 3600:.1f}h)"
        )


class Kubectl:
    def __init__(self, binary: str, kubeconfig: str) -> None:
        self.prefix = [binary, "--kubeconfig", kubeconfig]

    def run(self, *args: str) -> str:
        process = subprocess.run(
            [*self.prefix, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if process.returncode != 0:
            detail = process.stderr.strip().splitlines()
            message = detail[-1] if detail else "kubectl returned no error detail"
            raise ProtectionError(f"kubectl {' '.join(args)} failed: {message}")
        return process.stdout

    def json(self, *args: str) -> dict[str, Any]:
        try:
            document = json.loads(self.run(*args, "-o", "json"))
        except json.JSONDecodeError as exc:
            raise ProtectionError(f"kubectl {' '.join(args)} returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise ProtectionError(f"kubectl {' '.join(args)} did not return an object")
        return document


def top_level_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line[0].isspace() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, value = raw_line.partition(":")
        if not separator:
            continue
        scalar = value.strip().split(" #", 1)[0].strip()
        if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in {"'", '"'}:
            scalar = scalar[1:-1]
        values[key.strip()] = scalar
    return values


def decode_secret_value(secret: dict[str, Any], key: str) -> str:
    encoded = nested(secret, f"data.{key}")
    if not isinstance(encoded, str) or not encoded:
        raise ProtectionError(
            f"secret {nested(secret, 'metadata.namespace')}/{nested(secret, 'metadata.name')} "
            f"is missing key {key}"
        )
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtectionError(f"secret key {key} is not valid base64 UTF-8 data") from exc


def verify_etcd(
    kube: Kubectl,
    config_path: Path,
    now: datetime,
    max_age: timedelta,
) -> str:
    config = top_level_config(config_path)
    if config.get("etcd-s3", "").lower() not in {"true", "yes", "1"}:
        raise ProtectionError("RKE2 etcd S3 snapshots are not enabled")
    secret_name = config.get("etcd-s3-config-secret", "").strip()
    if not secret_name:
        raise ProtectionError("RKE2 etcd-s3-config-secret is not configured")
    secret = kube.json("-n", "kube-system", "get", "secret", secret_name)
    endpoint = decode_secret_value(secret, "etcd-s3-endpoint")
    require_external_endpoint(endpoint, "RKE2 etcd S3 endpoint")
    for key in ("etcd-s3-access-key", "etcd-s3-secret-key", "etcd-s3-bucket"):
        decode_secret_value(secret, key)

    snapshots = kube.json("get", "etcdsnapshotfiles.k3s.cattle.io").get("items", [])
    off_cluster = []
    for snapshot in snapshots if isinstance(snapshots, list) else []:
        blob = json.dumps(snapshot.get("spec", {}), sort_keys=True).lower()
        if "s3" in blob:
            off_cluster.append(snapshot)
    timestamp, name = newest_timestamp(
        off_cluster,
        (
            "status.createdAt",
            "spec.createdAt",
            "spec.creationTime",
            "metadata.creationTimestamp",
        ),
    )
    require_fresh(timestamp, name=name, label="RKE2 off-cluster etcd snapshot", max_age=max_age, now=now)
    return f"etcd=ok snapshot={name} age_hours={(now - timestamp).total_seconds() / 3600:.1f}"


def verify_velero(kube: Kubectl, now: datetime, max_age: timedelta) -> str:
    locations = kube.json("-n", "velero", "get", "backupstoragelocations.velero.io").get("items", [])
    available: list[dict[str, Any]] = []
    for location in locations if isinstance(locations, list) else []:
        if str(nested(location, "status.phase") or "").lower() == "available":
            available.append(location)
    if not available:
        raise ProtectionError("Velero has no Available BackupStorageLocation")
    for location in available:
        bucket = str(nested(location, "spec.objectStorage.bucket") or "").strip()
        if not bucket:
            raise ProtectionError("Velero BackupStorageLocation has no bucket")
        endpoint = str(nested(location, "spec.config.s3Url") or "").strip()
        provider = str(nested(location, "spec.provider") or "").strip().lower()
        if endpoint:
            require_external_endpoint(endpoint, "Velero object-storage endpoint")
        elif provider not in {"aws"}:
            raise ProtectionError("Velero non-AWS BackupStorageLocation has no external endpoint")

    schedules = kube.json("-n", "velero", "get", "schedules.velero.io").get("items", [])
    enabled = [
        item
        for item in schedules if isinstance(schedules, list)
        if not bool(nested(item, "spec.paused"))
    ]
    if not enabled:
        raise ProtectionError("Velero has no enabled backup schedule")
    if not all(bool(nested(item, "spec.template.snapshotMoveData")) for item in enabled):
        raise ProtectionError("every enabled Velero schedule must set snapshotMoveData=true")

    backups = kube.json("-n", "velero", "get", "backups.velero.io").get("items", [])
    completed = [
        item
        for item in backups if isinstance(backups, list)
        if str(nested(item, "status.phase") or "").lower() == "completed"
    ]
    timestamp, name = newest_timestamp(
        completed,
        ("status.completionTimestamp", "metadata.creationTimestamp"),
    )
    require_fresh(timestamp, name=name, label="Velero backup", max_age=max_age, now=now)
    return f"velero=ok backup={name} age_hours={(now - timestamp).total_seconds() / 3600:.1f}"


def verify_cnpg(kube: Kubectl, now: datetime, max_age: timedelta) -> str:
    cluster = kube.json(
        "-n", "platform-databases", "get", "cluster.postgresql.cnpg.io", "platform-postgres"
    )
    if nested(cluster, "status.readyInstances") != nested(cluster, "spec.instances"):
        raise ProtectionError("CloudNativePG platform-postgres is not fully Ready")
    destination = str(nested(cluster, "spec.backup.barmanObjectStore.destinationPath") or "")
    endpoint = str(nested(cluster, "spec.backup.barmanObjectStore.endpointURL") or "")
    if not destination.lower().startswith("s3://"):
        raise ProtectionError("CloudNativePG backup destination is not S3")
    require_external_endpoint(endpoint, "CloudNativePG object-storage endpoint")
    conditions = nested(cluster, "status.conditions")
    archive = [
        condition
        for condition in conditions if isinstance(conditions, list)
        if condition.get("type") == "ContinuousArchiving"
    ]
    if not archive or str(archive[-1].get("status", "")).lower() != "true":
        raise ProtectionError("CloudNativePG continuous WAL archiving is not healthy")

    schedules = kube.json(
        "-n", "platform-databases", "get", "scheduledbackups.postgresql.cnpg.io"
    ).get("items", [])
    enabled = [
        item
        for item in schedules if isinstance(schedules, list)
        if not bool(nested(item, "spec.suspend"))
    ]
    if not enabled:
        raise ProtectionError("CloudNativePG has no enabled ScheduledBackup")

    backups = kube.json("-n", "platform-databases", "get", "backups.postgresql.cnpg.io").get("items", [])
    completed = [
        item
        for item in backups if isinstance(backups, list)
        if str(nested(item, "status.phase") or "").lower() in {"completed", "succeeded"}
    ]
    timestamp, name = newest_timestamp(
        completed,
        ("status.stoppedAt", "status.endTime", "metadata.creationTimestamp"),
    )
    require_fresh(timestamp, name=name, label="CloudNativePG backup", max_age=max_age, now=now)
    return f"cnpg=ok backup={name} age_hours={(now - timestamp).total_seconds() / 3600:.1f}"


def setting_value(kube: Kubectl, name: str) -> str:
    setting = kube.json("-n", "longhorn-system", "get", "settings.longhorn.io", name)
    return str(setting.get("value") or "").strip()


def verify_longhorn(kube: Kubectl, now: datetime, max_age: timedelta) -> str:
    target = setting_value(kube, "backup-target")
    if not target.lower().startswith("s3://"):
        raise ProtectionError("Longhorn backup target is not configured as S3")
    require_external_endpoint(target, "Longhorn backup target")
    secret_name = setting_value(kube, "backup-target-credential-secret")
    if not secret_name:
        raise ProtectionError("Longhorn backup target credential secret is not configured")
    secret = kube.json("-n", "longhorn-system", "get", "secret", secret_name)
    endpoint = decode_secret_value(secret, "AWS_ENDPOINTS")
    require_external_endpoint(endpoint, "Longhorn object-storage endpoint")
    decode_secret_value(secret, "AWS_ACCESS_KEY_ID")
    decode_secret_value(secret, "AWS_SECRET_ACCESS_KEY")

    backups = kube.json("-n", "longhorn-system", "get", "backups.longhorn.io").get("items", [])
    completed = [
        item
        for item in backups if isinstance(backups, list)
        if str(nested(item, "status.state") or "").lower() in {"completed", "ready"}
    ]
    timestamp, name = newest_timestamp(
        completed,
        (
            "status.snapshotCreatedAt",
            "status.backupCreatedAt",
            "status.created",
            "metadata.creationTimestamp",
        ),
    )
    require_fresh(timestamp, name=name, label="Longhorn volume backup", max_age=max_age, now=now)
    return f"longhorn=ok backup={name} age_hours={(now - timestamp).total_seconds() / 3600:.1f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    parser.add_argument("--rke2-config", type=Path, default=Path("/etc/rancher/rke2/config.yaml"))
    parser.add_argument("--max-backup-age-hours", type=int, default=26)
    parser.add_argument("--max-etcd-age-hours", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_backup_age_hours <= 0 or args.max_etcd_age_hours <= 0:
        print("backup age limits must be greater than zero", file=sys.stderr)
        return 2
    if not args.rke2_config.is_file():
        print(f"RKE2 config does not exist: {args.rke2_config}", file=sys.stderr)
        return 1

    kube = Kubectl(args.kubectl, args.kubeconfig)
    now = datetime.now(timezone.utc)
    checks = (
        lambda: verify_etcd(
            kube,
            args.rke2_config,
            now,
            timedelta(hours=args.max_etcd_age_hours),
        ),
        lambda: verify_velero(kube, now, timedelta(hours=args.max_backup_age_hours)),
        lambda: verify_cnpg(kube, now, timedelta(hours=args.max_backup_age_hours)),
        lambda: verify_longhorn(kube, now, timedelta(hours=args.max_backup_age_hours)),
    )
    results: list[str] = []
    failures: list[str] = []
    for check in checks:
        try:
            results.append(check())
        except (OSError, ProtectionError) as exc:
            failures.append(str(exc))

    for result in results:
        print(result)
    if failures:
        print("Data-protection verification failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Production data-protection verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
