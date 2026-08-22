#!/usr/bin/env python3
"""Reclaim one proven-unregistered Longhorn replica directory under pressure."""

from __future__ import annotations

import argparse
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
MAX_DIRECTORY_ENTRIES = 100_000
TOMBSTONE_SUFFIX = ".platform-stale-replica-reclaim"
LEGACY_PRESSURE_ANNOTATION = "platform.gitops.io/root-pressure-eviction"
VOLUME_DIRECTORY = re.compile(
    r"^(?P<volume>pvc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})-(?P<suffix>[0-9a-f]{8})$"
)


@dataclass(frozen=True)
class RegisteredDisk:
    node_name: str
    disk_name: str
    disk_id: str
    disk_path: str


@dataclass(frozen=True)
class ReplicaDirectory:
    volume_name: str
    directory_name: str
    path: Path
    disk_name: str
    disk_id: str
    allocated_bytes: int
    newest_mtime_ns: int
    device: int
    inode: int
    tombstone: bool

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.volume_name,
            self.directory_name,
            self.disk_name,
            self.disk_id,
            self.allocated_bytes,
            self.newest_mtime_ns,
            self.device,
            self.inode,
            self.tombstone,
        )


@dataclass(frozen=True)
class ReclaimCandidate:
    directory: ReplicaDirectory
    pvc_namespace: str
    pvc_name: str
    pv_name: str

    def fingerprint(self) -> tuple[Any, ...]:
        return self.directory.fingerprint() + (
            self.pvc_namespace,
            self.pvc_name,
            self.pv_name,
        )


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def condition_status(obj: JsonObject, condition_type: str) -> str:
    for condition in obj.get("status", {}).get("conditions") or []:
        if condition.get("type") == condition_type:
            return str(condition.get("status", "Unknown"))
    return "Unknown"


def pod_is_ready(pod: JsonObject) -> bool:
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return False
    if pod.get("status", {}).get("phase") != "Running":
        return False
    containers = pod.get("spec", {}).get("containers") or []
    statuses = pod.get("status", {}).get("containerStatuses") or []
    return bool(containers) and len(statuses) == len(containers) and all(
        status.get("ready") is True for status in statuses
    )


def parse_directory_name(name: str) -> tuple[str, str, bool] | None:
    tombstone = False
    logical_name = name
    if name.startswith(".") and name.endswith(TOMBSTONE_SUFFIX):
        logical_name = name[1 : -len(TOMBSTONE_SUFFIX)]
        tombstone = True
    match = VOLUME_DIRECTORY.fullmatch(logical_name)
    if not match:
        return None
    return match.group("volume"), logical_name, tombstone


def path_shares_root(path: str) -> bool:
    try:
        return os.stat(os.path.realpath(path)).st_dev == os.stat("/").st_dev
    except OSError:
        return False


def allocated_bytes(file_stat: os.stat_result) -> int:
    """Return allocated bytes on Linux and a conservative test fallback elsewhere."""
    blocks = getattr(file_stat, "st_blocks", None)
    if blocks is not None:
        return int(blocks) * 512
    return int(file_stat.st_size)


def registered_disks(nodes: list[JsonObject]) -> dict[str, dict[str, RegisteredDisk]]:
    result: dict[str, dict[str, RegisteredDisk]] = {}
    for node in nodes:
        metadata = node.get("metadata", {})
        node_name = metadata.get("name", "")
        if not node_name or metadata.get("deletionTimestamp"):
            continue
        spec = node.get("spec", {})
        status = node.get("status", {})
        status_disks = status.get("diskStatus") or {}
        for disk_name, disk_spec in (spec.get("disks") or {}).items():
            disk_status = status_disks.get(disk_name) or {}
            disk_id = str(disk_status.get("diskUUID", ""))
            spec_path = str(disk_spec.get("path", ""))
            status_path = str(disk_status.get("diskPath", ""))
            disk_path = spec_path or status_path
            if not (
                disk_id
                and disk_path
                and PurePosixPath(disk_path).is_absolute()
                and disk_spec.get("diskType", "filesystem") == "filesystem"
                and disk_status.get("diskType", "filesystem") == "filesystem"
                and disk_spec.get("evictionRequested", False) is False
                and (not spec_path or not status_path or spec_path == status_path)
            ):
                continue
            result.setdefault(node_name, {})[disk_id] = RegisteredDisk(
                node_name=node_name,
                disk_name=disk_name,
                disk_id=disk_id,
                disk_path=disk_path,
            )
    return result


def directory_metrics(path: Path) -> tuple[int, int, int, int]:
    root_stat = path.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError(f"replica path is not a real directory: {path}")
    allocated = allocated_bytes(root_stat)
    newest_mtime_ns = root_stat.st_mtime_ns
    entries_seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise RuntimeError(f"cannot inspect replica path {current}: {exc}") from None
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_DIRECTORY_ENTRIES:
                raise RuntimeError(
                    f"replica directory exceeds {MAX_DIRECTORY_ENTRIES} entries"
                )
            try:
                current_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    f"cannot inspect replica entry {entry.path}: {exc}"
                ) from None
            if stat.S_ISLNK(current_stat.st_mode):
                raise RuntimeError(f"replica directory contains a symlink: {entry.path}")
            if not (
                stat.S_ISDIR(current_stat.st_mode)
                or stat.S_ISREG(current_stat.st_mode)
            ):
                raise RuntimeError(
                    f"replica directory contains a special file: {entry.path}"
                )
            allocated += allocated_bytes(current_stat)
            newest_mtime_ns = max(newest_mtime_ns, current_stat.st_mtime_ns)
            if stat.S_ISDIR(current_stat.st_mode):
                stack.append(Path(entry.path))
    return allocated, newest_mtime_ns, root_stat.st_dev, root_stat.st_ino


def discover_directories(
    *,
    node_name: str,
    nodes: list[JsonObject],
) -> list[ReplicaDirectory]:
    disks = registered_disks(nodes).get(node_name, {})
    result: list[ReplicaDirectory] = []
    for disk in disks.values():
        if not path_shares_root(disk.disk_path):
            continue
        disk_root = Path(disk.disk_path)
        replicas_root = disk_root / "replicas"
        try:
            disk_real = disk_root.resolve(strict=True)
            replicas_stat = replicas_root.lstat()
            replicas_real = replicas_root.resolve(strict=True)
        except OSError:
            continue
        if (
            stat.S_ISLNK(replicas_stat.st_mode)
            or not stat.S_ISDIR(replicas_stat.st_mode)
            or replicas_real.parent != disk_real
        ):
            raise RuntimeError(
                f"unsafe Longhorn replicas directory identity: {replicas_root}"
            )
        try:
            entries = list(os.scandir(replicas_real))
        except OSError as exc:
            raise RuntimeError(
                f"cannot enumerate Longhorn replicas at {replicas_real}: {exc}"
            ) from None
        for entry in entries:
            parsed = parse_directory_name(entry.name)
            if parsed is None:
                continue
            volume_name, directory_name, tombstone = parsed
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise RuntimeError(f"unsafe Longhorn replica entry: {entry.path}")
            path = Path(entry.path)
            if path.resolve(strict=True).parent != replicas_real:
                raise RuntimeError(f"replica path escaped its registered disk: {path}")
            allocated, newest, device, inode = directory_metrics(path)
            if device != os.stat("/").st_dev:
                continue
            result.append(
                ReplicaDirectory(
                    volume_name=volume_name,
                    directory_name=directory_name,
                    path=path,
                    disk_name=disk.disk_name,
                    disk_id=disk.disk_id,
                    allocated_bytes=allocated,
                    newest_mtime_ns=newest,
                    device=device,
                    inode=inode,
                    tombstone=tombstone,
                )
            )
    return result


def replica_history_is_safe(replica: JsonObject) -> bool:
    spec = replica.get("spec", {})
    healthy = parse_timestamp(spec.get("lastHealthyAt") or spec.get("healthyAt", ""))
    failed = parse_timestamp(spec.get("lastFailedAt", ""))
    return healthy is not None and (failed is None or healthy > failed)


def replica_is_safe(
    replica: JsonObject,
    disks_by_node: dict[str, dict[str, RegisteredDisk]],
) -> bool:
    metadata = replica.get("metadata", {})
    spec = replica.get("spec", {})
    status = replica.get("status", {})
    node_name = str(spec.get("nodeID", ""))
    disk_id = str(spec.get("diskID", ""))
    parsed = parse_directory_name(str(spec.get("dataDirectoryName", "")))
    return bool(
        not metadata.get("deletionTimestamp")
        and spec.get("active") is True
        and spec.get("desireState") == "running"
        and spec.get("evictionRequested", False) is False
        and not spec.get("failedAt")
        and replica_history_is_safe(replica)
        and node_name
        and disk_id in disks_by_node.get(node_name, {})
        and parsed is not None
        and parsed[0] == spec.get("volumeName")
        and not parsed[2]
        and status.get("currentState") in {"running", "stopped"}
    )


def pod_uses_claim(pod: JsonObject, namespace: str, claim_name: str) -> bool:
    if pod.get("metadata", {}).get("namespace") != namespace:
        return False
    return any(
        volume.get("persistentVolumeClaim", {}).get("claimName") == claim_name
        for volume in pod.get("spec", {}).get("volumes", [])
    )


def evaluate_directory(
    *,
    node_name: str,
    directory: ReplicaDirectory,
    volumes: list[JsonObject],
    replicas: list[JsonObject],
    engines: list[JsonObject],
    longhorn_attachments: list[JsonObject],
    native_attachments: list[JsonObject],
    pvs: list[JsonObject],
    pvcs: list[JsonObject],
    pods: list[JsonObject],
    nodes: list[JsonObject],
    directories: list[ReplicaDirectory],
    now_ns: int,
    minimum_age_seconds: int,
    minimum_reclaim_bytes: int,
) -> tuple[ReclaimCandidate | None, str]:
    if directory.allocated_bytes < minimum_reclaim_bytes:
        return None, "allocated-data-below-reclaim-floor"
    if now_ns - directory.newest_mtime_ns < minimum_age_seconds * 1_000_000_000:
        return None, "replica-directory-too-new"
    referenced_names = {
        str(replica.get("spec", {}).get("dataDirectoryName", ""))
        for replica in replicas
    }
    if directory.directory_name in referenced_names:
        return None, "replica-directory-still-registered"

    matching_volumes = [
        volume
        for volume in volumes
        if volume.get("metadata", {}).get("name") == directory.volume_name
        and not volume.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(matching_volumes) != 1:
        return None, "volume-identity-not-unique"
    volume = matching_volumes[0]
    spec = volume.get("spec", {})
    status = volume.get("status", {})
    desired_replicas = integer(spec.get("numberOfReplicas"))
    if (
        spec.get("dataEngine", "v1") not in {"", "v1"}
        or status.get("state") != "detached"
        or status.get("currentNodeID")
        or spec.get("nodeID")
        or spec.get("migrationNodeID")
        or status.get("currentMigrationNodeID")
        or spec.get("fromBackup")
        or status.get("restoreRequired")
        or integer(status.get("actualSize")) <= 0
        or desired_replicas < 2
    ):
        return None, "volume-not-safely-detached"

    current_replicas = [
        replica
        for replica in replicas
        if replica.get("spec", {}).get("volumeName") == directory.volume_name
    ]
    disks_by_node = registered_disks(nodes)
    if len(current_replicas) != desired_replicas:
        return None, "current-replica-count-does-not-match-desired"
    if not all(replica_is_safe(replica, disks_by_node) for replica in current_replicas):
        return None, "current-replica-health-history-not-safe"
    replica_nodes = [replica.get("spec", {}).get("nodeID", "") for replica in current_replicas]
    if len(set(replica_nodes)) != len(replica_nodes):
        return None, "current-replicas-not-node-distinct"

    directory_index = {
        (item.disk_id, item.directory_name): item
        for item in directories
        if not item.tombstone
    }
    local_replicas = [
        replica
        for replica in current_replicas
        if replica.get("spec", {}).get("nodeID") == node_name
        and replica.get("spec", {}).get("diskID") == directory.disk_id
    ]
    if len(local_replicas) != 1:
        return None, "registered-local-replica-not-unique"
    local_directory_name = str(
        local_replicas[0].get("spec", {}).get("dataDirectoryName", "")
    )
    if (directory.disk_id, local_directory_name) not in directory_index:
        return None, "registered-local-replica-directory-absent"

    volume_engines = [
        engine
        for engine in engines
        if engine.get("spec", {}).get("volumeName") == directory.volume_name
    ]
    if not volume_engines or any(
        engine.get("metadata", {}).get("deletionTimestamp")
        or engine.get("spec", {}).get("nodeID")
        or engine.get("spec", {}).get("desireState") != "stopped"
        or engine.get("status", {}).get("currentState") != "stopped"
        or engine.get("status", {}).get("instanceManagerName")
        for engine in volume_engines
    ):
        return None, "volume-engine-not-fully-stopped"

    attachments = [
        attachment
        for attachment in longhorn_attachments
        if attachment.get("metadata", {}).get("name") == directory.volume_name
        and not attachment.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(attachments) != 1 or attachments[0].get("spec", {}).get(
        "attachmentTickets"
    ):
        return None, "longhorn-attachment-ticket-present"

    kubernetes_status = status.get("kubernetesStatus") or {}
    namespace = str(kubernetes_status.get("namespace", ""))
    pvc_name = str(kubernetes_status.get("pvcName", ""))
    pv_name = str(kubernetes_status.get("pvName", ""))
    if not namespace or not pvc_name or not pv_name:
        return None, "kubernetes-volume-identity-incomplete"
    matching_pvs = [
        pv
        for pv in pvs
        if pv.get("metadata", {}).get("name") == pv_name
        and not pv.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(matching_pvs) != 1:
        return None, "persistent-volume-identity-not-unique"
    pv = matching_pvs[0]
    pv_spec = pv.get("spec", {})
    csi = pv_spec.get("csi") or {}
    claim_ref = pv_spec.get("claimRef") or {}
    if (
        pv.get("status", {}).get("phase") != "Bound"
        or csi.get("driver") != "driver.longhorn.io"
        or csi.get("volumeHandle") != directory.volume_name
        or claim_ref.get("namespace") != namespace
        or claim_ref.get("name") != pvc_name
    ):
        return None, "persistent-volume-not-safely-bound"
    matching_pvcs = [
        pvc
        for pvc in pvcs
        if pvc.get("metadata", {}).get("namespace") == namespace
        and pvc.get("metadata", {}).get("name") == pvc_name
        and not pvc.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(matching_pvcs) != 1:
        return None, "persistent-volume-claim-identity-not-unique"
    pvc = matching_pvcs[0]
    pvc_uid = str(pvc.get("metadata", {}).get("uid", ""))
    claim_uid = str(claim_ref.get("uid", ""))
    if (
        pvc.get("status", {}).get("phase") != "Bound"
        or pvc.get("spec", {}).get("volumeName") != pv_name
        or not pvc_uid
        or claim_uid != pvc_uid
    ):
        return None, "persistent-volume-claim-not-safely-bound"
    if any(pod_uses_claim(pod, namespace, pvc_name) for pod in pods):
        return None, "persistent-volume-claim-has-pod-reference"
    if any(
        attachment.get("spec", {}).get("source", {}).get(
            "persistentVolumeName"
        )
        == pv_name
        for attachment in native_attachments
    ):
        return None, "kubernetes-volume-attachment-present"
    return (
        ReclaimCandidate(
            directory=directory,
            pvc_namespace=namespace,
            pvc_name=pvc_name,
            pv_name=pv_name,
        ),
        "proven-unregistered-replica-directory",
    )


def select_candidate(
    *,
    node_name: str,
    kube_node: JsonObject,
    manager_pods: list[JsonObject],
    settings: dict[str, str],
    directories: list[ReplicaDirectory],
    volumes: list[JsonObject],
    replicas: list[JsonObject],
    engines: list[JsonObject],
    longhorn_attachments: list[JsonObject],
    native_attachments: list[JsonObject],
    pvs: list[JsonObject],
    pvcs: list[JsonObject],
    pods: list[JsonObject],
    nodes: list[JsonObject],
    now_ns: int,
    minimum_age_seconds: int,
    minimum_reclaim_bytes: int,
) -> tuple[ReclaimCandidate | None, str]:
    if condition_status(kube_node, "Ready") != "True":
        return None, "kubernetes-node-not-ready"
    if condition_status(kube_node, "DiskPressure") != "True":
        return None, "kubernetes-disk-pressure-not-active"
    for condition_type in ("MemoryPressure", "PIDPressure"):
        if condition_status(kube_node, condition_type) != "False":
            return None, f"kubernetes-{condition_type.lower()}-not-clear"
    if condition_status(kube_node, "NetworkUnavailable") == "True":
        return None, "kubernetes-network-unavailable"
    local_longhorn_nodes = [
        node
        for node in nodes
        if node.get("metadata", {}).get("name") == node_name
        and not node.get("metadata", {}).get("deletionTimestamp")
    ]
    if len(local_longhorn_nodes) != 1:
        return None, "longhorn-node-identity-not-unique"
    if (
        local_longhorn_nodes[0]
        .get("metadata", {})
        .get("annotations", {})
        .get(LEGACY_PRESSURE_ANNOTATION)
    ):
        return None, "legacy-pressure-eviction-state-present"
    ready_managers = [
        pod
        for pod in manager_pods
        if pod.get("spec", {}).get("nodeName") == node_name and pod_is_ready(pod)
    ]
    if not ready_managers:
        return None, "longhorn-manager-not-ready-on-node"
    deletion_modes = {
        item.strip()
        for item in settings.get("orphan-resource-auto-deletion", "").split(";")
        if item.strip()
    }
    grace = integer(settings.get("orphan-resource-auto-deletion-grace-period"))
    if "replica-data" not in deletion_modes or grace < 300:
        return None, "longhorn-orphan-policy-not-safe"
    effective_age = max(minimum_age_seconds, grace)

    eligible: list[ReclaimCandidate] = []
    reasons: list[str] = []
    for directory in directories:
        candidate, reason = evaluate_directory(
            node_name=node_name,
            directory=directory,
            volumes=volumes,
            replicas=replicas,
            engines=engines,
            longhorn_attachments=longhorn_attachments,
            native_attachments=native_attachments,
            pvs=pvs,
            pvcs=pvcs,
            pods=pods,
            nodes=nodes,
            directories=directories,
            now_ns=now_ns,
            minimum_age_seconds=effective_age,
            minimum_reclaim_bytes=minimum_reclaim_bytes,
        )
        if candidate is not None:
            eligible.append(candidate)
        else:
            reasons.append(reason)
    if not eligible:
        return None, "no-proven-unregistered-replica-directory"
    if len(eligible) != 1:
        return None, "multiple-proven-unregistered-replica-directories"
    return eligible[0], "proven-unregistered-replica-directory"


def directory_has_open_files(path: Path, proc_root: Path = Path("/proc")) -> bool:
    if not proc_root.is_dir():
        raise RuntimeError("proc filesystem is unavailable for open-file verification")
    prefix = str(path) + os.sep
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        fd_root = process / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            target = target.removesuffix(" (deleted)")
            if target == str(path) or target.startswith(prefix):
                return True
    return False


def quarantine_candidate(candidate: ReclaimCandidate) -> ReclaimCandidate:
    directory = candidate.directory
    if directory.tombstone:
        return candidate
    parent = directory.path.parent
    expected_parent = Path(os.path.realpath(parent))
    if directory.path.resolve(strict=True).parent != expected_parent:
        raise RuntimeError("candidate directory identity changed before quarantine")
    tombstone = parent / f".{directory.directory_name}{TOMBSTONE_SUFFIX}"
    if tombstone.exists() or tombstone.is_symlink():
        raise RuntimeError("stale-replica quarantine path already exists")
    if os.name == "nt":
        os.rename(directory.path, tombstone)
    else:
        directory_fd = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.rename(
                directory.path.name,
                tombstone.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
    current_stat = tombstone.lstat()
    if (
        current_stat.st_dev != directory.device
        or current_stat.st_ino != directory.inode
        or not stat.S_ISDIR(current_stat.st_mode)
        or stat.S_ISLNK(current_stat.st_mode)
    ):
        raise RuntimeError("quarantined replica directory identity changed")
    return ReclaimCandidate(
        directory=ReplicaDirectory(
            volume_name=directory.volume_name,
            directory_name=directory.directory_name,
            path=tombstone,
            disk_name=directory.disk_name,
            disk_id=directory.disk_id,
            allocated_bytes=directory.allocated_bytes,
            newest_mtime_ns=directory.newest_mtime_ns,
            device=directory.device,
            inode=directory.inode,
            tombstone=True,
        ),
        pvc_namespace=candidate.pvc_namespace,
        pvc_name=candidate.pvc_name,
        pv_name=candidate.pv_name,
    )


def remove_quarantined_candidate(
    candidate: ReclaimCandidate,
    open_file_check: Callable[[Path], bool] = directory_has_open_files,
) -> None:
    directory = candidate.directory
    parsed = parse_directory_name(directory.path.name)
    if not directory.tombstone or parsed is None or not parsed[2]:
        raise RuntimeError("refusing to remove a non-quarantined replica directory")
    current = directory_metrics(directory.path)
    expected = (
        directory.allocated_bytes,
        directory.newest_mtime_ns,
        directory.device,
        directory.inode,
    )
    if current != expected:
        raise RuntimeError("quarantined replica directory changed before removal")
    if open_file_check(directory.path):
        raise RuntimeError("quarantined replica directory still has open files")
    if os.name != "nt" and not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe descriptor-based recursive removal is unavailable")
    shutil.rmtree(directory.path)


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
                self.base + list(args), check=False, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"kubectl timed out after {timeout:g} seconds: " + " ".join(args)
            ) from None
        except (BoundedSubprocessError, ValueError) as exc:
            raise RuntimeError(f"kubectl output rejected: {exc}") from None
        if check and result.returncode != 0:
            diagnostic = (result.stderr or "") + (result.stdout or "")
            raise RuntimeError(f"kubectl failed: {' '.join(args)}: {diagnostic.strip()}")
        return result

    def get_json(self, *args: str) -> JsonObject:
        result = self.run(*args, "-o", "json")
        try:
            value = loads_strict_json(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"kubectl returned invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RuntimeError("kubectl returned a non-object JSON document")
        return value


def list_items(kube: Kubectl, *args: str) -> list[JsonObject]:
    return list(kube.get_json(*args).get("items") or [])


def collect_state(
    kube: Kubectl,
    node_name: str,
    *,
    minimum_age_seconds: int,
    minimum_reclaim_bytes: int,
) -> tuple[ReclaimCandidate | None, str]:
    kube_node = kube.get_json("get", f"node/{node_name}")
    manager_pods = list_items(
        kube,
        "-n",
        "longhorn-system",
        "get",
        "pods",
        "-l",
        "app=longhorn-manager",
    )
    nodes = list_items(kube, "-n", "longhorn-system", "get", "nodes.longhorn.io")
    volumes = list_items(kube, "-n", "longhorn-system", "get", "volumes.longhorn.io")
    replicas = list_items(kube, "-n", "longhorn-system", "get", "replicas.longhorn.io")
    engines = list_items(kube, "-n", "longhorn-system", "get", "engines.longhorn.io")
    attachments = list_items(
        kube, "-n", "longhorn-system", "get", "volumeattachments.longhorn.io"
    )
    native_attachments = list_items(
        kube, "get", "volumeattachments.storage.k8s.io"
    )
    pvs = list_items(kube, "get", "persistentvolumes")
    pvcs = list_items(kube, "get", "persistentvolumeclaims", "--all-namespaces")
    pods = list_items(kube, "get", "pods", "--all-namespaces")
    settings = {}
    for name in (
        "orphan-resource-auto-deletion",
        "orphan-resource-auto-deletion-grace-period",
    ):
        setting = kube.get_json(
            "-n", "longhorn-system", "get", f"settings.longhorn.io/{name}"
        )
        settings[name] = str(setting.get("value", ""))
    directories = discover_directories(node_name=node_name, nodes=nodes)
    return select_candidate(
        node_name=node_name,
        kube_node=kube_node,
        manager_pods=manager_pods,
        settings=settings,
        directories=directories,
        volumes=volumes,
        replicas=replicas,
        engines=engines,
        longhorn_attachments=attachments,
        native_attachments=native_attachments,
        pvs=pvs,
        pvcs=pvcs,
        pods=pods,
        nodes=nodes,
        now_ns=time.time_ns(),
        minimum_age_seconds=minimum_age_seconds,
        minimum_reclaim_bytes=minimum_reclaim_bytes,
    )


def run(args: argparse.Namespace) -> int:
    kube = Kubectl(args.kubectl, args.kubeconfig)

    def discover() -> tuple[ReclaimCandidate | None, str]:
        return collect_state(
            kube,
            args.node,
            minimum_age_seconds=args.minimum_age_seconds,
            minimum_reclaim_bytes=args.minimum_reclaim_bytes,
        )

    candidate, reason = discover()
    if candidate is None:
        print(
            "longhorn_stale_replica_reclaim=not-needed "
            f"node={args.node} reason={reason}"
        )
        return 0
    print(
        "longhorn_stale_replica_reclaim=settling "
        f"node={args.node} volume={candidate.directory.volume_name} "
        f"directory={candidate.directory.directory_name} "
        f"allocatedBytes={candidate.directory.allocated_bytes}"
    )
    time.sleep(args.settle_seconds)
    second, reason = discover()
    if second is None or second.fingerprint() != candidate.fingerprint():
        print(
            "longhorn_stale_replica_reclaim=deferred "
            f"node={args.node} reason=identity-changed-during-settle "
            f"detail={reason}"
        )
        return 0
    if directory_has_open_files(second.directory.path):
        print(
            "longhorn_stale_replica_reclaim=deferred "
            f"node={args.node} reason=replica-directory-has-open-files"
        )
        return 0

    quarantined = quarantine_candidate(second)
    third, reason = discover()
    if third is None or third.fingerprint() != quarantined.fingerprint():
        raise RuntimeError(
            "quarantined candidate failed final cluster-state verification: " + reason
        )
    remove_quarantined_candidate(quarantined)
    print(
        "longhorn_stale_replica_reclaim=completed "
        f"node={args.node} volume={quarantined.directory.volume_name} "
        f"directory={quarantined.directory.directory_name} "
        f"reclaimedBytes={quarantined.directory.allocated_bytes}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--node", required=True)
    result.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    result.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    result.add_argument("--minimum-age-seconds", type=int, default=3600)
    result.add_argument("--minimum-reclaim-bytes", type=int, default=1024**3)
    result.add_argument("--settle-seconds", type=int, default=15)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.minimum_age_seconds < 300:
        raise SystemExit("--minimum-age-seconds must be at least 300")
    if args.minimum_reclaim_bytes < 1024**2:
        raise SystemExit("--minimum-reclaim-bytes must be at least 1 MiB")
    if not 5 <= args.settle_seconds <= 60:
        raise SystemExit("--settle-seconds must be between 5 and 60")
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"longhorn_stale_replica_reclaim=failed reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
