#!/usr/bin/env python3
"""Relocate narrowly proven Longhorn replicas from removed disk registrations."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from bounded_subprocess import BoundedSubprocessError, run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


JsonObject = dict[str, Any]
KUBECTL_TIMEOUT_SECONDS = 120
MAX_MANIFEST_ENTRIES = 100_000
SAFE_DIRECTORY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


@dataclass(frozen=True)
class RegisteredDisk:
    node_name: str
    disk_name: str
    disk_id: str
    disk_path: str


@dataclass(frozen=True)
class Candidate:
    volume_name: str
    replica_name: str
    replica_uid: str
    source_node: str
    source_disk_id: str
    source_disk_path: str
    data_directory_name: str
    destination_disk_name: str
    destination_disk_id: str
    destination_disk_path: str
    target_node: str
    attachment_name: str
    namespace: str
    pvc_name: str
    healthy_replica: str
    consumer_pods: tuple[str, ...]


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def object_age_seconds(obj: JsonObject, now: datetime) -> float | None:
    created = parse_timestamp(obj.get("metadata", {}).get("creationTimestamp", ""))
    if created is None:
        return None
    return max(0.0, (now - created.astimezone(timezone.utc)).total_seconds())


def condition_is_true(conditions: list[JsonObject], condition_type: str) -> bool:
    return any(
        condition.get("type") == condition_type and condition.get("status") == "True"
        for condition in conditions
    )


def controller_owner(obj: JsonObject) -> JsonObject | None:
    return next(
        (
            owner
            for owner in obj.get("metadata", {}).get("ownerReferences", [])
            if owner.get("controller") is True
        ),
        None,
    )


def pod_uses_claim(pod: JsonObject, claim_name: str) -> bool:
    return any(
        volume.get("persistentVolumeClaim", {}).get("claimName") == claim_name
        for volume in pod.get("spec", {}).get("volumes", [])
    )


def ticket_is_active(longhorn_attachment: JsonObject, target_node: str) -> bool:
    tickets = longhorn_attachment.get("spec", {}).get("attachmentTickets") or {}
    statuses = longhorn_attachment.get("status", {}).get("attachmentTicketStatuses") or {}
    for ticket_id, ticket in tickets.items():
        if ticket.get("type") != "csi-attacher" or ticket.get("nodeID") != target_node:
            continue
        if statuses.get(ticket_id, {}).get("satisfied") is True:
            continue
        return True
    return False


def registered_filesystem_disks(node: JsonObject) -> list[RegisteredDisk]:
    metadata = node.get("metadata", {})
    spec = node.get("spec", {})
    status = node.get("status", {})
    node_name = metadata.get("name", "")
    if (
        not node_name
        or metadata.get("deletionTimestamp")
        or not condition_is_true(status.get("conditions") or [], "Ready")
        or not condition_is_true(status.get("conditions") or [], "Schedulable")
    ):
        return []

    disks = []
    for disk_name, disk_spec in (spec.get("disks") or {}).items():
        disk_status = (status.get("diskStatus") or {}).get(disk_name, {})
        disk_id = disk_status.get("diskUUID", "")
        spec_path = disk_spec.get("path", "")
        status_path = disk_status.get("diskPath", "")
        if not (
            disk_spec.get("allowScheduling") is True
            and disk_spec.get("diskType", "filesystem") == "filesystem"
            and disk_status.get("diskType", "filesystem") == "filesystem"
            and condition_is_true(disk_status.get("conditions") or [], "Ready")
            and condition_is_true(disk_status.get("conditions") or [], "Schedulable")
            and disk_id
            and spec_path
            and spec_path == status_path
            and PurePosixPath(spec_path).is_absolute()
        ):
            continue
        disks.append(
            RegisteredDisk(
                node_name=node_name,
                disk_name=disk_name,
                disk_id=disk_id,
                disk_path=spec_path,
            )
        )
    return disks


def replica_history_is_safe(replica: JsonObject) -> bool:
    spec = replica.get("spec", {})
    healthy_at = parse_timestamp(spec.get("lastHealthyAt") or spec.get("healthyAt", ""))
    failed_at = parse_timestamp(spec.get("lastFailedAt", ""))
    if healthy_at is None:
        return False
    return failed_at is None or healthy_at > failed_at


def running_replica_is_safe(
    replica: JsonObject,
    registered_ids: dict[str, set[str]],
) -> bool:
    spec = replica.get("spec", {})
    status = replica.get("status", {})
    node_id = spec.get("nodeID", "")
    disk_id = spec.get("diskID", "")
    return bool(
        spec.get("active") is True
        and spec.get("desireState") == "running"
        and spec.get("healthyAt")
        and not spec.get("failedAt")
        and replica_history_is_safe(replica)
        and status.get("currentState") == "running"
        and status.get("instanceManagerName")
        and status.get("ip")
        and status.get("storageIP")
        and int(status.get("port") or 0) > 0
        and node_id
        and disk_id in registered_ids.get(node_id, set())
    )


def stopped_unregistered_replica_is_safe(
    replica: JsonObject,
    registered_ids: dict[str, set[str]],
) -> bool:
    spec = replica.get("spec", {})
    status = replica.get("status", {})
    node_id = spec.get("nodeID", "")
    disk_id = spec.get("diskID", "")
    data_directory_name = spec.get("dataDirectoryName", "")
    try:
        port = int(status.get("port") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        spec.get("active") is True
        and spec.get("desireState") == "running"
        and spec.get("healthyAt")
        and not spec.get("failedAt")
        and replica_history_is_safe(replica)
        and status.get("currentState") == "stopped"
        and not status.get("instanceManagerName")
        and not status.get("ip")
        and not status.get("storageIP")
        and port == 0
        and node_id
        and disk_id
        and disk_id not in registered_ids.get(node_id, set())
        and spec.get("diskPath")
        and SAFE_DIRECTORY_NAME.fullmatch(data_directory_name)
    )


def evaluate_candidate(
    *,
    local_node: str,
    volume: JsonObject,
    engines: list[JsonObject],
    replicas: list[JsonObject],
    longhorn_nodes: list[JsonObject],
    longhorn_attachment: JsonObject,
    native_attachments: list[JsonObject],
    pv: JsonObject,
    pods: list[JsonObject],
    now: datetime,
    minimum_age_seconds: int,
) -> tuple[Candidate | None, str]:
    metadata = volume.get("metadata", {})
    spec = volume.get("spec", {})
    status = volume.get("status", {})
    volume_name = metadata.get("name", "")
    if not volume_name or metadata.get("deletionTimestamp"):
        return None, "volume-missing-or-deleting"
    if status.get("state") != "attaching":
        return None, "volume-not-attaching"
    if status.get("robustness") not in {"unknown", "degraded"}:
        return None, "volume-robustness-not-recoverable"
    try:
        actual_size = int(status.get("actualSize") or 0)
        declared_replicas = int(spec.get("numberOfReplicas") or 0)
    except (TypeError, ValueError):
        return None, "invalid-volume-size-or-replica-count"
    if actual_size <= 0 or declared_replicas < 3:
        return None, "volume-lacks-proven-data-or-premium-redundancy"
    if spec.get("accessMode") != "rwo" or spec.get("migratable") is True:
        return None, "volume-not-nonmigratable-rwo"
    if (
        spec.get("migrationNodeID")
        or status.get("currentMigrationNodeID")
        or spec.get("fromBackup")
        or status.get("restoreRequired")
    ):
        return None, "volume-migration-or-restore-active"

    target_node = spec.get("nodeID", "")
    if not target_node or status.get("currentNodeID"):
        return None, "volume-target-state-not-stuck-signature"

    kubernetes_status = status.get("kubernetesStatus") or {}
    namespace = kubernetes_status.get("namespace", "")
    pvc_name = kubernetes_status.get("pvcName", "")
    pv_name = kubernetes_status.get("pvName", "")
    pv_spec = pv.get("spec", {})
    csi = pv_spec.get("csi", {})
    if not (
        namespace
        and pvc_name
        and pv_name
        and pv.get("metadata", {}).get("name") == pv_name
        and csi.get("driver") == "driver.longhorn.io"
        and csi.get("volumeHandle") == volume_name
    ):
        return None, "kubernetes-volume-identity-mismatch"

    volume_engines = [
        engine
        for engine in engines
        if engine.get("spec", {}).get("volumeName") == volume_name
        and not engine.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(volume_engines) != 1:
        return None, "engine-cardinality-not-one"
    engine_spec = volume_engines[0].get("spec", {})
    engine_status = volume_engines[0].get("status", {})
    if not (
        not engine_spec.get("nodeID")
        and engine_spec.get("desireState") == "stopped"
        and engine_status.get("currentState") == "stopped"
        and not engine_status.get("instanceManagerName")
        and not engine_status.get("ip")
    ):
        return None, "engine-not-stopped-unassigned"

    nodes_by_name = {
        node.get("metadata", {}).get("name", ""): node
        for node in longhorn_nodes
        if node.get("metadata", {}).get("name")
    }
    local_longhorn_node = nodes_by_name.get(local_node)
    if not local_longhorn_node:
        return None, "local-longhorn-node-absent"
    disks_by_node = {
        node_name: registered_filesystem_disks(node)
        for node_name, node in nodes_by_name.items()
    }
    registered_ids = {
        node_name: {disk.disk_id for disk in disks}
        for node_name, disks in disks_by_node.items()
    }
    all_registered_ids = {
        disk_id for node_ids in registered_ids.values() for disk_id in node_ids
    }

    volume_replicas = [
        replica
        for replica in replicas
        if replica.get("spec", {}).get("volumeName") == volume_name
        and not replica.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(volume_replicas) != 2:
        return None, "replica-cardinality-not-exactly-two"
    healthy_replicas = [
        replica
        for replica in volume_replicas
        if running_replica_is_safe(replica, registered_ids)
    ]
    stopped_replicas = [
        replica
        for replica in volume_replicas
        if stopped_unregistered_replica_is_safe(replica, registered_ids)
    ]
    if len(healthy_replicas) != 1:
        return None, "safe-running-replica-cardinality-not-one"
    if len(stopped_replicas) != 1:
        return None, "safe-stopped-unregistered-replica-cardinality-not-one"

    orphan = stopped_replicas[0]
    orphan_spec = orphan.get("spec", {})
    if orphan_spec.get("nodeID") != local_node:
        return None, "stopped-unregistered-replica-not-local"
    if orphan_spec.get("diskID") in all_registered_ids:
        return None, "stopped-replica-disk-still-registered"
    if healthy_replicas[0].get("spec", {}).get("nodeID") == local_node:
        return None, "healthy-and-stopped-replicas-share-node"

    destination_disks = [
        disk
        for disk in disks_by_node.get(local_node, [])
        if disk.disk_id != orphan_spec.get("diskID")
        and disk.disk_path != orphan_spec.get("diskPath")
    ]
    if len(destination_disks) != 1:
        return None, "safe-local-destination-disk-cardinality-not-one"
    destination = destination_disks[0]

    if not longhorn_attachment or not ticket_is_active(longhorn_attachment, target_node):
        return None, "active-unsatisfied-csi-ticket-absent"

    failed_attachments = []
    for attachment in native_attachments:
        attachment_metadata = attachment.get("metadata", {})
        attachment_spec = attachment.get("spec", {})
        attachment_status = attachment.get("status", {})
        age = object_age_seconds(attachment, now)
        if not (
            not attachment_metadata.get("deletionTimestamp")
            and attachment_spec.get("attacher") == "driver.longhorn.io"
            and attachment_spec.get("source", {}).get("persistentVolumeName") == pv_name
            and attachment_spec.get("nodeName") == target_node
            and attachment_status.get("attached") is not True
            and (attachment_status.get("attachError") or {}).get("message")
            and age is not None
            and age >= minimum_age_seconds
        ):
            continue
        failed_attachments.append(attachment)
    if len(failed_attachments) != 1:
        return None, "old-failed-native-attachment-cardinality-not-one"

    claim_consumers = [
        pod
        for pod in pods
        if pod.get("metadata", {}).get("namespace") == namespace
        and not pod.get("metadata", {}).get("deletionTimestamp")
        and pod_uses_claim(pod, pvc_name)
    ]
    pending_consumers = [
        pod
        for pod in claim_consumers
        if pod.get("spec", {}).get("nodeName") == target_node
        and pod.get("status", {}).get("phase") == "Pending"
        and controller_owner(pod) is not None
    ]
    if not pending_consumers or len(pending_consumers) != len(claim_consumers):
        return None, "exclusive-pending-controller-managed-consumer-absent"

    return (
        Candidate(
            volume_name=volume_name,
            replica_name=orphan.get("metadata", {}).get("name", ""),
            replica_uid=orphan.get("metadata", {}).get("uid", ""),
            source_node=local_node,
            source_disk_id=orphan_spec.get("diskID", ""),
            source_disk_path=orphan_spec.get("diskPath", ""),
            data_directory_name=orphan_spec.get("dataDirectoryName", ""),
            destination_disk_name=destination.disk_name,
            destination_disk_id=destination.disk_id,
            destination_disk_path=destination.disk_path,
            target_node=target_node,
            attachment_name=failed_attachments[0].get("metadata", {}).get("name", ""),
            namespace=namespace,
            pvc_name=pvc_name,
            healthy_replica=healthy_replicas[0].get("metadata", {}).get("name", ""),
            consumer_pods=tuple(
                sorted(
                    pod.get("metadata", {}).get("name", "")
                    for pod in pending_consumers
                )
            ),
        ),
        "stopped-replica-data-on-removed-local-disk-registration",
    )


class Kubectl:
    def __init__(self, executable: str, kubeconfig: str):
        self.base = [executable, "--kubeconfig", kubeconfig]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            timeout = bounded_timeout_seconds(
                KUBECTL_TIMEOUT_SECONDS,
                "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
            )
            result = run_bounded(
                self.base + list(args),
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"kubectl timed out after {timeout:g} seconds: {' '.join(args)}"
            ) from None
        except (BoundedSubprocessError, ValueError) as exc:
            raise RuntimeError(f"kubectl output rejected: {exc}") from None
        if check and result.returncode != 0:
            sys.stderr.write((result.stderr or "") + (result.stdout or ""))
            raise RuntimeError(f"kubectl failed: {' '.join(args)}")
        return result

    def get_json(self, *args: str) -> JsonObject:
        return loads_strict_json(self.run(*args, "-o", "json").stdout)


def items(kube: Kubectl, *args: str) -> list[JsonObject]:
    return kube.get_json(*args).get("items", [])


def discover_candidates(
    kube: Kubectl,
    *,
    local_node: str,
    minimum_age_seconds: int,
    report: bool,
) -> list[Candidate]:
    volumes = items(kube, "-n", "longhorn-system", "get", "volumes.longhorn.io")
    engines = items(kube, "-n", "longhorn-system", "get", "engines.longhorn.io")
    replicas = items(kube, "-n", "longhorn-system", "get", "replicas.longhorn.io")
    longhorn_nodes = items(kube, "-n", "longhorn-system", "get", "nodes.longhorn.io")
    longhorn_attachments = {
        item.get("metadata", {}).get("name", ""): item
        for item in items(
            kube,
            "-n",
            "longhorn-system",
            "get",
            "volumeattachments.longhorn.io",
        )
    }
    native_attachments = items(kube, "get", "volumeattachments.storage.k8s.io")
    pvs = {
        item.get("metadata", {}).get("name", ""): item
        for item in items(kube, "get", "persistentvolumes")
    }
    pods = items(kube, "get", "pods", "--all-namespaces")
    now = datetime.now(timezone.utc)
    candidates = []
    for volume in volumes:
        status = volume.get("status", {})
        try:
            relevant = (
                status.get("state") == "attaching"
                and int(status.get("actualSize") or 0) > 0
            )
        except (TypeError, ValueError):
            relevant = False
        if not relevant:
            continue
        volume_name = volume.get("metadata", {}).get("name", "")
        pv_name = (status.get("kubernetesStatus") or {}).get("pvName", "")
        candidate, reason = evaluate_candidate(
            local_node=local_node,
            volume=volume,
            engines=engines,
            replicas=replicas,
            longhorn_nodes=longhorn_nodes,
            longhorn_attachment=longhorn_attachments.get(volume_name, {}),
            native_attachments=native_attachments,
            pv=pvs.get(pv_name, {}),
            pods=pods,
            now=now,
            minimum_age_seconds=minimum_age_seconds,
        )
        if candidate:
            candidates.append(candidate)
        elif report:
            print(
                f"longhorn_volume={volume_name} local_node={local_node} "
                f"replica_path_action=retain reason={reason}"
            )
    return candidates


ManifestEntry = tuple[str, str, int, int, int, int | str]


def directory_manifest(root: Path) -> tuple[ManifestEntry, ...]:
    entries: list[ManifestEntry] = []

    def walk(directory: Path, relative: Path) -> None:
        for item in sorted(os.scandir(directory), key=lambda entry: entry.name):
            relative_item = relative / item.name
            item_stat = item.stat(follow_symlinks=False)
            common = (
                relative_item.as_posix(),
                stat.S_IMODE(item_stat.st_mode),
                item_stat.st_uid,
                item_stat.st_gid,
            )
            if stat.S_ISDIR(item_stat.st_mode):
                entries.append((common[0], "directory", *common[1:], 0))
                walk(Path(item.path), relative_item)
            elif stat.S_ISREG(item_stat.st_mode):
                entries.append(
                    (common[0], "regular", *common[1:], int(item_stat.st_size))
                )
            elif stat.S_ISLNK(item_stat.st_mode):
                entries.append(
                    (common[0], "symlink", *common[1:], os.readlink(item.path))
                )
            else:
                raise RuntimeError(
                    f"replica directory contains unsupported special file: {item.path}"
                )
            if len(entries) > MAX_MANIFEST_ENTRIES:
                raise RuntimeError(
                    f"replica manifest exceeds {MAX_MANIFEST_ENTRIES} entries"
                )

    walk(root, Path())
    return tuple(entries)


def checked_replica_directory(disk_path: str, data_directory_name: str) -> Path:
    if not SAFE_DIRECTORY_NAME.fullmatch(data_directory_name):
        raise RuntimeError("unsafe Longhorn replica data directory name")
    disk_root = Path(disk_path)
    if not disk_root.is_absolute() or disk_root == Path(disk_root.anchor):
        raise RuntimeError(f"unsafe Longhorn disk root: {disk_path}")
    if disk_root.is_symlink() or not disk_root.is_dir():
        raise RuntimeError(f"Longhorn disk root is not a real directory: {disk_path}")
    replicas_root = disk_root / "replicas"
    if replicas_root.is_symlink() or not replicas_root.is_dir():
        raise RuntimeError(
            f"Longhorn replicas root is not a real directory: {replicas_root}"
        )
    replica_path = replicas_root / data_directory_name
    if replica_path.parent.resolve(strict=True) != replicas_root.resolve(strict=True):
        raise RuntimeError("replica directory escaped the configured replicas root")
    return replica_path


CopyTree = Callable[[Path, Path, int], None]


def reflink_copy_tree(source: Path, destination: Path, timeout_seconds: int) -> None:
    try:
        result = run_bounded(
            [
                "/usr/bin/cp",
                "-a",
                "--reflink=always",
                "--sparse=always",
                "--",
                str(source),
                str(destination),
            ],
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"same-filesystem reflink copy timed out after {timeout_seconds} seconds"
        ) from None
    except (BoundedSubprocessError, ValueError) as exc:
        raise RuntimeError(f"replica reflink copy output rejected: {exc}") from None
    if result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        raise RuntimeError(f"same-filesystem reflink copy failed: {detail or 'unknown error'}")


def relocate_copy(
    candidate: Candidate,
    *,
    timeout_seconds: int,
    copy_tree: CopyTree = reflink_copy_tree,
) -> tuple[Path, Path, str]:
    source = checked_replica_directory(
        candidate.source_disk_path,
        candidate.data_directory_name,
    )
    destination = checked_replica_directory(
        candidate.destination_disk_path,
        candidate.data_directory_name,
    )
    source_root = source.parent
    destination_root = destination.parent
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"source replica directory is absent or unsafe: {source}")
    if source.stat().st_dev != destination_root.stat().st_dev:
        raise RuntimeError(
            "source and destination Longhorn disks are not on the same filesystem; "
            "automatic reflink relocation refused"
        )
    if source.resolve(strict=True) == destination.resolve(strict=False):
        raise RuntimeError("source and destination replica directories are identical")

    source_manifest = directory_manifest(source)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError(f"destination replica path is unsafe: {destination}")
        if directory_manifest(destination) != source_manifest:
            raise RuntimeError(
                f"existing destination replica copy does not match source: {destination}"
            )
        return source, destination, "reuse-verified-relocation-copy"

    temporary = destination_root / (
        f".{candidate.data_directory_name}.platform-relocating"
    )
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_symlink() or not temporary.is_dir():
            raise RuntimeError(f"unsafe interrupted relocation path: {temporary}")
        shutil.rmtree(temporary)

    try:
        copy_tree(source, temporary, timeout_seconds)
        if temporary.is_symlink() or not temporary.is_dir():
            raise RuntimeError("reflink copy did not create a real directory")
        if directory_manifest(temporary) != source_manifest:
            raise RuntimeError("reflink copy manifest does not match the source replica")
        os.replace(temporary, destination)
        if hasattr(os, "sync"):
            os.sync()
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise
    if directory_manifest(destination) != source_manifest:
        raise RuntimeError("final replica copy manifest does not match the source replica")
    return source, destination, "create-verified-reflink-copy"


def replica_still_matches(kube: Kubectl, candidate: Candidate) -> bool:
    replica = kube.get_json(
        "-n",
        "longhorn-system",
        "get",
        f"replicas.longhorn.io/{candidate.replica_name}",
    )
    metadata = replica.get("metadata", {})
    spec = replica.get("spec", {})
    status = replica.get("status", {})
    return bool(
        metadata.get("uid") == candidate.replica_uid
        and not metadata.get("deletionTimestamp")
        and spec.get("nodeID") == candidate.source_node
        and spec.get("diskID") == candidate.source_disk_id
        and spec.get("diskPath") == candidate.source_disk_path
        and spec.get("dataDirectoryName") == candidate.data_directory_name
        and stopped_unregistered_replica_is_safe(replica, {candidate.source_node: set()})
        and status.get("currentState") == "stopped"
    )


def patch_replica(kube: Kubectl, candidate: Candidate) -> None:
    if not replica_still_matches(kube, candidate):
        raise RuntimeError(
            f"replica changed while its recovery copy was prepared: {candidate.replica_name}"
        )
    patch = json.dumps(
        {
            "metadata": {
                "labels": {
                    "longhorndiskuuid": candidate.destination_disk_id,
                    "longhornnode": candidate.source_node,
                }
            },
            "spec": {
                "diskID": candidate.destination_disk_id,
                "diskPath": candidate.destination_disk_path,
            },
        },
        separators=(",", ":"),
    )
    base = (
        "-n",
        "longhorn-system",
        "patch",
        f"replicas.longhorn.io/{candidate.replica_name}",
        "--type=merge",
        "-p",
        patch,
    )
    kube.run(*base, "--dry-run=server", "-o", "name")
    kube.run(*base)


def wait_for_replica(
    kube: Kubectl,
    candidate: Candidate,
    *,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_report = 0.0
    while time.monotonic() < deadline:
        replica = kube.get_json(
            "-n",
            "longhorn-system",
            "get",
            f"replicas.longhorn.io/{candidate.replica_name}",
        )
        spec = replica.get("spec", {})
        status = replica.get("status", {})
        if (
            spec.get("diskID") != candidate.destination_disk_id
            or spec.get("diskPath") != candidate.destination_disk_path
        ):
            raise RuntimeError("Longhorn changed the repaired replica destination")
        current_state = status.get("currentState", "unknown")
        ready = bool(
            current_state == "running"
            and status.get("instanceManagerName")
            and status.get("ip")
            and int(status.get("port") or 0) > 0
        )
        if ready:
            print(
                f"longhorn_volume={candidate.volume_name} "
                f"replica={candidate.replica_name} result=running "
                f"instance_manager={status.get('instanceManagerName')}"
            )
            return
        if current_state in {"error", "faulted"}:
            raise RuntimeError(
                f"repaired Longhorn replica entered {current_state}: "
                f"{candidate.replica_name}"
            )
        if time.monotonic() - last_report >= 15:
            print(
                f"longhorn_volume={candidate.volume_name} "
                f"replica={candidate.replica_name} result=waiting "
                f"state={current_state}"
            )
            last_report = time.monotonic()
        time.sleep(5)
    raise RuntimeError(
        f"repaired Longhorn replica did not become running within "
        f"{timeout_seconds} seconds: {candidate.replica_name}"
    )


def repair(
    kube: Kubectl,
    *,
    local_node: str,
    timeout_seconds: int,
    minimum_age_seconds: int,
    settle_seconds: int,
) -> int:
    candidates = discover_candidates(
        kube,
        local_node=local_node,
        minimum_age_seconds=minimum_age_seconds,
        report=True,
    )
    if not candidates:
        print(
            f"longhorn_unregistered_replica_path_repair=not-needed "
            f"node={local_node}"
        )
        return 0

    if settle_seconds > 0:
        print(
            f"longhorn_unregistered_replica_path_repair=settling "
            f"node={local_node} seconds={settle_seconds}"
        )
        time.sleep(settle_seconds)
        candidates = discover_candidates(
            kube,
            local_node=local_node,
            minimum_age_seconds=minimum_age_seconds,
            report=True,
        )
        if not candidates:
            print(
                f"longhorn_unregistered_replica_path_repair=progressing-before-copy "
                f"node={local_node}"
            )
            return 0

    for candidate in candidates:
        source, destination, copy_action = relocate_copy(
            candidate,
            timeout_seconds=timeout_seconds,
        )
        refreshed_candidates = discover_candidates(
            kube,
            local_node=local_node,
            minimum_age_seconds=minimum_age_seconds,
            report=False,
        )
        if candidate not in refreshed_candidates:
            raise RuntimeError(
                "Longhorn attachment safety signature changed while the recovery "
                f"copy was prepared: {candidate.replica_name}"
            )
        patch_replica(kube, candidate)
        print(
            f"longhorn_volume={candidate.volume_name} replica={candidate.replica_name} "
            f"source={source} destination={destination} "
            f"source_disk={candidate.source_disk_id} "
            f"destination_disk={candidate.destination_disk_id} "
            f"destination_disk_name={candidate.destination_disk_name} "
            f"healthy_replica={candidate.healthy_replica} "
            f"attachment={candidate.attachment_name} "
            f"consumers={','.join(candidate.consumer_pods)} "
            f"copy_action={copy_action} "
            "action=relocate-unregistered-replica-copy-preserved"
        )
        wait_for_replica(kube, candidate, timeout_seconds=timeout_seconds)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy and re-register one narrowly proven stopped Longhorn replica "
            "whose previous local disk registration was removed"
        )
    )
    parser.add_argument("--node", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--minimum-age", type=int, default=120)
    parser.add_argument("--settle", type=int, default=15)
    parser.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        not SAFE_DIRECTORY_NAME.fullmatch(args.node)
        or args.timeout <= 0
        or args.minimum_age < 60
        or args.settle < 0
    ):
        raise SystemExit(
            "node must be a safe Kubernetes name; timeout must be positive; "
            "minimum age must be at least 60 seconds"
        )
    kube = Kubectl(args.kubectl, args.kubeconfig)
    try:
        return repair(
            kube,
            local_node=args.node,
            timeout_seconds=args.timeout,
            minimum_age_seconds=args.minimum_age,
            settle_seconds=args.settle,
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(
            f"longhorn_unregistered_replica_path_repair=fail "
            f"node={args.node} error={exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
