#!/usr/bin/env python3
"""Relieve Kubernetes DiskPressure through guarded Longhorn disk eviction."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bounded_subprocess import BoundedSubprocessError, run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


JsonObject = dict[str, Any]
ANNOTATION = "platform.gitops.io/root-pressure-eviction"
KUBECTL_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Disk:
    node_name: str
    disk_name: str
    disk_id: str
    disk_path: str
    storage_available: int
    usable_capacity: int


@dataclass(frozen=True)
class Candidate:
    replica_name: str
    volume_name: str
    volume_size: int
    estimated_physical_bytes: int
    destination_nodes: tuple[str, ...]


@dataclass(frozen=True)
class EvacuationPlan:
    source_disks: tuple[Disk, ...]
    candidates: tuple[Candidate, ...]
    required_relief_bytes: int
    estimated_relief_bytes: int


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def condition_is_true(conditions: list[JsonObject], condition_type: str) -> bool:
    return any(
        condition.get("type") == condition_type
        and condition.get("status") == "True"
        for condition in conditions
    )


def disk_path(disk_spec: JsonObject, disk_status: JsonObject) -> str:
    return str(
        disk_spec.get("path") or disk_status.get("diskPath") or ""
    ).strip()


def disk_usable_capacity(
    disk_spec: JsonObject,
    disk_status: JsonObject,
    *,
    minimum_available_percentage: int,
    over_provisioning_percentage: int,
) -> int:
    storage_maximum = integer(disk_status.get("storageMaximum"))
    storage_available = integer(disk_status.get("storageAvailable"))
    storage_scheduled = integer(disk_status.get("storageScheduled"))
    storage_reserved = integer(disk_spec.get("storageReserved"))
    minimum_available = storage_maximum * minimum_available_percentage // 100
    physical_headroom = max(0, storage_available - minimum_available)
    provisioned_limit = (
        max(0, storage_maximum - storage_reserved)
        * over_provisioning_percentage
        // 100
    )
    logical_headroom = max(0, provisioned_limit - storage_scheduled)
    return min(physical_headroom, logical_headroom)


def discover_disks(
    nodes: list[JsonObject],
    *,
    source_node: str,
    root_shared_disk_names: set[str],
    minimum_available_percentage: int,
    over_provisioning_percentage: int,
) -> tuple[list[Disk], list[Disk]]:
    source_disks: list[Disk] = []
    destination_disks: list[Disk] = []
    for node in nodes:
        metadata = node.get("metadata", {})
        node_name = metadata.get("name", "")
        node_spec = node.get("spec", {})
        node_status = node.get("status", {})
        if not node_name or metadata.get("deletionTimestamp"):
            continue
        node_ready = condition_is_true(
            node_status.get("conditions") or [], "Ready"
        )
        spec_disks = node_spec.get("disks") or {}
        status_disks = node_status.get("diskStatus") or {}
        for disk_name, disk_spec in spec_disks.items():
            disk_status = status_disks.get(disk_name) or {}
            disk_id = disk_status.get("diskUUID", "")
            current_disk_path = disk_path(disk_spec, disk_status)
            if not disk_id or not current_disk_path:
                continue
            usable = disk_usable_capacity(
                disk_spec,
                disk_status,
                minimum_available_percentage=minimum_available_percentage,
                over_provisioning_percentage=over_provisioning_percentage,
            )
            disk = Disk(
                node_name=node_name,
                disk_name=disk_name,
                disk_id=disk_id,
                disk_path=current_disk_path,
                storage_available=integer(disk_status.get("storageAvailable")),
                usable_capacity=usable,
            )
            if node_name == source_node and disk_name in root_shared_disk_names:
                source_disks.append(disk)
                continue
            if node_name == source_node:
                continue
            if not node_ready:
                continue
            if not condition_is_true(
                disk_status.get("conditions") or [], "Ready"
            ):
                continue
            if not node_spec.get("allowScheduling", False):
                continue
            if not disk_spec.get("allowScheduling", False):
                continue
            if disk_spec.get("evictionRequested", False):
                continue
            if not condition_is_true(
                disk_status.get("conditions") or [], "Schedulable"
            ):
                continue
            if usable > 0:
                destination_disks.append(disk)
    return source_disks, destination_disks


def build_plan(
    *,
    source_node: str,
    root_shared_disk_names: set[str],
    nodes: list[JsonObject],
    replicas: list[JsonObject],
    volumes: list[JsonObject],
    physical_bytes_by_replica: dict[str, int],
    root_total_bytes: int,
    root_available_bytes: int,
    target_free_percentage: int,
    minimum_available_percentage: int,
    over_provisioning_percentage: int,
) -> tuple[EvacuationPlan | None, str]:
    source_disks, destination_disks = discover_disks(
        nodes,
        source_node=source_node,
        root_shared_disk_names=root_shared_disk_names,
        minimum_available_percentage=minimum_available_percentage,
        over_provisioning_percentage=over_provisioning_percentage,
    )
    if not source_disks:
        return None, "root-shared-longhorn-source-disk-absent"
    if not destination_disks:
        return None, "alternate-schedulable-longhorn-disk-absent"

    required_relief = max(
        0,
        root_total_bytes * target_free_percentage // 100 - root_available_bytes,
    )
    if required_relief == 0:
        return None, "root-free-target-already-met"

    source_disk_ids = {disk.disk_id for disk in source_disks}
    volumes_by_name = {
        volume.get("metadata", {}).get("name", ""): volume
        for volume in volumes
        if volume.get("metadata", {}).get("name")
    }
    replica_nodes_by_volume: dict[str, set[str]] = {}
    for replica in replicas:
        metadata = replica.get("metadata", {})
        spec = replica.get("spec", {})
        if metadata.get("deletionTimestamp") or spec.get("failedAt"):
            continue
        volume_name = spec.get("volumeName", "")
        node_name = spec.get("nodeID", "")
        if volume_name and node_name:
            replica_nodes_by_volume.setdefault(volume_name, set()).add(node_name)

    candidates: list[Candidate] = []
    for replica in replicas:
        metadata = replica.get("metadata", {})
        spec = replica.get("spec", {})
        replica_name = metadata.get("name", "")
        volume_name = spec.get("volumeName", "")
        if (
            not replica_name
            or not volume_name
            or metadata.get("deletionTimestamp")
            or spec.get("nodeID") != source_node
            or spec.get("diskID") not in source_disk_ids
            or spec.get("failedAt")
            or spec.get("evictionRequested", False)
        ):
            continue
        volume = volumes_by_name.get(volume_name) or {}
        volume_metadata = volume.get("metadata", {})
        volume_spec = volume.get("spec", {})
        volume_status = volume.get("status", {})
        if volume_metadata.get("deletionTimestamp"):
            continue
        if volume_status.get("robustness") == "faulted":
            continue
        if volume_spec.get("cloneMode") == "linked-clone":
            continue
        if integer(volume_spec.get("numberOfReplicas")) < 2:
            continue
        volume_size = integer(spec.get("volumeSize")) or integer(
            volume_spec.get("size")
        )
        if volume_size <= 0:
            continue
        occupied_nodes = replica_nodes_by_volume.get(volume_name, set()) - {
            source_node
        }
        destinations = sorted(
            {
                disk.node_name
                for disk in destination_disks
                if disk.node_name not in occupied_nodes
                and disk.usable_capacity >= volume_size
            }
        )
        if not destinations:
            continue
        estimated = physical_bytes_by_replica.get(replica_name, 0)
        if estimated <= 0:
            estimated = integer(volume_status.get("actualSize"))
        if estimated <= 0:
            continue
        candidates.append(
            Candidate(
                replica_name=replica_name,
                volume_name=volume_name,
                volume_size=volume_size,
                estimated_physical_bytes=estimated,
                destination_nodes=tuple(destinations),
            )
        )

    candidates.sort(
        key=lambda candidate: (
            candidate.estimated_physical_bytes,
            candidate.volume_size,
            candidate.replica_name,
        ),
        reverse=True,
    )
    remaining_capacity = {
        (disk.node_name, disk.disk_name): disk.usable_capacity
        for disk in destination_disks
    }
    selected: list[Candidate] = []
    selected_volumes: set[str] = set()
    estimated_relief = 0
    for candidate in candidates:
        if candidate.volume_name in selected_volumes:
            continue
        compatible_disks = [
            disk
            for disk in destination_disks
            if disk.node_name in candidate.destination_nodes
            and remaining_capacity[(disk.node_name, disk.disk_name)]
            >= candidate.volume_size
        ]
        if not compatible_disks:
            continue
        destination = min(
            compatible_disks,
            key=lambda disk: (
                remaining_capacity[(disk.node_name, disk.disk_name)]
                - candidate.volume_size,
                disk.node_name,
                disk.disk_name,
            ),
        )
        remaining_capacity[(destination.node_name, destination.disk_name)] -= (
            candidate.volume_size
        )
        selected.append(
            Candidate(
                replica_name=candidate.replica_name,
                volume_name=candidate.volume_name,
                volume_size=candidate.volume_size,
                estimated_physical_bytes=candidate.estimated_physical_bytes,
                destination_nodes=(destination.node_name,),
            )
        )
        selected_volumes.add(candidate.volume_name)
        estimated_relief += candidate.estimated_physical_bytes
        if estimated_relief >= required_relief:
            break
    if not selected:
        return None, "safe-evacuation-candidate-absent"
    if estimated_relief < required_relief:
        return None, "safe-evacuation-relief-insufficient"
    return (
        EvacuationPlan(
            source_disks=tuple(source_disks),
            candidates=tuple(selected),
            required_relief_bytes=required_relief,
            estimated_relief_bytes=estimated_relief,
        ),
        "safe-root-disk-evacuation-available",
    )


def allocated_directory_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        for entry in entries:
            try:
                stat = entry.stat(follow_symlinks=False)
            except (FileNotFoundError, PermissionError):
                continue
            total += getattr(stat, "st_blocks", 0) * 512
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return total


def root_filesystem_metrics() -> tuple[int, int, int]:
    stat = os.statvfs("/")
    total = stat.f_blocks * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    percentage = available * 100 // total if total else 0
    return total, available, percentage


def path_shares_root(path: str) -> bool:
    if not path:
        return False
    try:
        return os.stat(os.path.realpath(path)).st_dev == os.stat("/").st_dev
    except OSError:
        return False


class Kubectl:
    def __init__(self, executable: str, kubeconfig: str):
        self.base = [executable, "--kubeconfig", kubeconfig]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            timeout = bounded_timeout_seconds(
                KUBECTL_TIMEOUT_SECONDS,
                "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        try:
            result = run_bounded(
                self.base + list(args),
                check=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"kubectl timed out after {timeout:g} seconds: "
                + " ".join(args)
            ) from None
        except (BoundedSubprocessError, ValueError) as exc:
            raise RuntimeError(f"kubectl output rejected: {exc}") from None
        if check and result.returncode != 0:
            sys.stderr.write((result.stderr or "") + (result.stdout or ""))
            raise RuntimeError("kubectl failed: " + " ".join(args))
        return result

    def get_json(self, *args: str) -> JsonObject:
        output = self.run(*args, "-o", "json").stdout
        try:
            value = loads_strict_json(output)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"kubectl returned invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RuntimeError("kubectl returned a non-object JSON document")
        return value

    def get_optional_json(self, *args: str) -> JsonObject | None:
        result = self.run(*args, "-o", "json", check=False)
        if result.returncode != 0:
            diagnostic = (result.stderr or "") + (result.stdout or "")
            if "NotFound" in diagnostic or "doesn't have a resource type" in diagnostic:
                return None
            raise RuntimeError("kubectl failed: " + " ".join(args))
        try:
            value = loads_strict_json(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"kubectl returned invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RuntimeError("kubectl returned a non-object JSON document")
        return value

    def patch_longhorn_node(self, node_name: str, patch: JsonObject) -> None:
        self.run(
            "-n",
            "longhorn-system",
            "patch",
            f"nodes.longhorn.io/{node_name}",
            "--type=merge",
            "-p",
            json.dumps(patch, separators=(",", ":")),
        )

    def patch_longhorn_replica(
        self, replica_name: str, eviction_requested: bool
    ) -> bool:
        result = self.run(
            "-n",
            "longhorn-system",
            "patch",
            f"replicas.longhorn.io/{replica_name}",
            "--type=merge",
            "-p",
            json.dumps(
                {"spec": {"evictionRequested": eviction_requested}},
                separators=(",", ":"),
            ),
            check=False,
        )
        if result.returncode == 0:
            return True
        diagnostic = (result.stderr or "") + (result.stdout or "")
        if "NotFound" in diagnostic:
            return False
        sys.stderr.write(diagnostic)
        raise RuntimeError(
            f"kubectl failed while patching Longhorn replica {replica_name}"
        )


def kubernetes_disk_pressure(kube: Kubectl, node_name: str) -> str:
    node = kube.get_json("get", f"node/{node_name}")
    for condition in node.get("status", {}).get("conditions") or []:
        if condition.get("type") == "DiskPressure":
            return condition.get("status", "Unknown")
    return "Unknown"


def annotation_state(node: JsonObject, node_name: str) -> JsonObject | None:
    raw = (
        node.get("metadata", {}).get("annotations", {}).get(ANNOTATION, "")
    )
    if not raw:
        return None
    try:
        state = loads_strict_json(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid {ANNOTATION} state: {exc}") from None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError(f"unsupported {ANNOTATION} state")
    if state.get("node") != node_name or not isinstance(state.get("disks"), dict):
        raise RuntimeError(f"mismatched {ANNOTATION} state")
    replicas = state.get("replicas", {})
    if not isinstance(replicas, dict):
        raise RuntimeError(f"invalid {ANNOTATION} replica state")
    for disk_name, disk_state in state["disks"].items():
        if (
            not isinstance(disk_name, str)
            or not disk_name
            or not isinstance(disk_state, dict)
            or set(disk_state) != {"allowScheduling", "evictionRequested"}
            or not isinstance(disk_state.get("allowScheduling"), bool)
            or not isinstance(disk_state.get("evictionRequested"), bool)
        ):
            raise RuntimeError(f"invalid {ANNOTATION} disk state")
    for replica_name, eviction_requested in replicas.items():
        if (
            not isinstance(replica_name, str)
            or not replica_name
            or not isinstance(eviction_requested, bool)
        ):
            raise RuntimeError(f"invalid {ANNOTATION} replica state")
    state["replicas"] = replicas
    return state


def original_disk_state(node_name: str, node: JsonObject, plan: EvacuationPlan) -> JsonObject:
    spec_disks = node.get("spec", {}).get("disks") or {}
    disks: JsonObject = {}
    for source in plan.source_disks:
        spec = spec_disks.get(source.disk_name) or {}
        if not spec.get("allowScheduling", False) or spec.get(
            "evictionRequested", False
        ):
            raise RuntimeError(
                "root-backed Longhorn disk already has a manually managed "
                f"scheduling or eviction state: {source.disk_name}"
            )
        disks[source.disk_name] = {
            "allowScheduling": True,
            "evictionRequested": False,
        }
    replicas = {
        candidate.replica_name: False for candidate in plan.candidates
    }
    return {
        "version": 1,
        "node": node_name,
        "disks": disks,
        "replicas": replicas,
    }


def request_evacuation(
    kube: Kubectl,
    node_name: str,
    state: JsonObject,
) -> None:
    # Whole-disk eviction would expand the bounded plan to every source replica.
    disk_patch = {
        disk_name: {"allowScheduling": False, "evictionRequested": False}
        for disk_name in state["disks"]
    }
    kube.patch_longhorn_node(
        node_name,
        {
            "metadata": {
                "annotations": {
                    ANNOTATION: json.dumps(state, separators=(",", ":"))
                }
            },
            "spec": {"disks": disk_patch},
        },
    )
    for replica_name in state.get("replicas", {}):
        kube.patch_longhorn_replica(replica_name, True)


def restore_disk_state(
    kube: Kubectl,
    node_name: str,
    state: JsonObject,
    current_node: JsonObject | None = None,
) -> None:
    for replica_name, eviction_requested in state.get("replicas", {}).items():
        kube.patch_longhorn_replica(replica_name, eviction_requested)
    original_disks = state["disks"]
    if current_node is not None:
        current_disks = current_node.get("spec", {}).get("disks") or {}
        original_disks = {
            name: disk
            for name, disk in original_disks.items()
            if name in current_disks
        }
    patch: JsonObject = {
        "metadata": {"annotations": {ANNOTATION: None}},
    }
    if original_disks:
        patch["spec"] = {"disks": original_disks}
    kube.patch_longhorn_node(node_name, patch)


def disk_replica_count(node: JsonObject, disk_names: set[str]) -> int:
    status_disks = node.get("status", {}).get("diskStatus") or {}
    return sum(
        len((status_disks.get(name) or {}).get("scheduledReplica") or {})
        for name in disk_names
    )


def selected_replica_count(
    replicas: list[JsonObject],
    *,
    node_name: str,
    disk_ids: set[str],
    replica_names: set[str],
) -> int:
    return sum(
        1
        for replica in replicas
        if replica.get("metadata", {}).get("name") in replica_names
        and not replica.get("metadata", {}).get("deletionTimestamp")
        and replica.get("spec", {}).get("nodeID") == node_name
        and replica.get("spec", {}).get("diskID") in disk_ids
    )


def runtime_objects(kube: Kubectl) -> tuple[list[JsonObject], list[JsonObject], list[JsonObject]]:
    nodes = kube.get_json(
        "-n", "longhorn-system", "get", "nodes.longhorn.io"
    ).get("items", [])
    replicas = kube.get_json(
        "-n", "longhorn-system", "get", "replicas.longhorn.io"
    ).get("items", [])
    volumes = kube.get_json(
        "-n", "longhorn-system", "get", "volumes.longhorn.io"
    ).get("items", [])
    return nodes, replicas, volumes


def find_node(nodes: list[JsonObject], node_name: str) -> JsonObject:
    matches = [
        node
        for node in nodes
        if node.get("metadata", {}).get("name") == node_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one Longhorn node named {node_name}, found {len(matches)}"
        )
    return matches[0]


def physical_replica_sizes(
    node: JsonObject,
    replicas: list[JsonObject],
    root_shared_disk_names: set[str],
) -> dict[str, int]:
    spec_disks = node.get("spec", {}).get("disks") or {}
    status_disks = node.get("status", {}).get("diskStatus") or {}
    disk_paths_by_id = {
        (status_disks.get(name) or {}).get("diskUUID", ""): (
            disk_path(spec_disks.get(name) or {}, status_disks.get(name) or {})
        )
        for name in root_shared_disk_names
    }
    result: dict[str, int] = {}
    for replica in replicas:
        metadata = replica.get("metadata", {})
        spec = replica.get("spec", {})
        name = metadata.get("name", "")
        directory = spec.get("dataDirectoryName", "")
        replica_disk_path = disk_paths_by_id.get(spec.get("diskID", ""), "")
        if not name or not directory or not replica_disk_path:
            continue
        result[name] = allocated_directory_bytes(
            Path(replica_disk_path) / "replicas" / directory
        )
    return result


def setting_integer(kube: Kubectl, setting_name: str, default: int) -> int:
    setting = kube.get_json(
        "-n",
        "longhorn-system",
        "get",
        f"settings.longhorn.io/{setting_name}",
    )
    return integer(setting.get("value"), default)


def run(args: argparse.Namespace) -> int:
    kube = Kubectl(args.kubectl, args.kubeconfig)
    pressure = kubernetes_disk_pressure(kube, args.node)
    source_node = kube.get_optional_json(
        "-n",
        "longhorn-system",
        "get",
        f"nodes.longhorn.io/{args.node}",
    )
    if source_node is None:
        print(
            "longhorn_pressure_evacuation=not-needed "
            f"node={args.node} reason=longhorn-node-absent"
        )
        return 0
    existing_state = annotation_state(source_node, args.node)
    if pressure == "False":
        if existing_state is not None:
            restore_disk_state(kube, args.node, existing_state, source_node)
            print(
                "longhorn_pressure_evacuation=completed "
                f"node={args.node} disk_pressure={pressure} "
                "schedulingState=restored"
            )
            return 0
        print(
            "longhorn_pressure_evacuation=not-needed "
            f"node={args.node} disk_pressure={pressure}"
        )
        return 0
    if pressure != "True":
        print(
            "longhorn_pressure_evacuation=deferred "
            f"node={args.node} reason=disk-pressure-unclassified "
            f"disk_pressure={pressure}"
        )
        return 0

    if existing_state is not None:
        state = existing_state
        disk_names = set(state["disks"])
        current_disks = source_node.get("spec", {}).get("disks") or {}
        missing_disks = sorted(disk_names - set(current_disks))
        if missing_disks:
            raise RuntimeError(
                "annotated Longhorn pressure-eviction disks are absent: "
                + ",".join(missing_disks)
            )
        request_evacuation(kube, args.node, state)
        print(
            "longhorn_pressure_evacuation=resumed "
            f"node={args.node} disks={','.join(sorted(disk_names))}"
        )
    else:
        nodes, replicas, volumes = runtime_objects(kube)
        source_node = find_node(nodes, args.node)
        spec_disks = source_node.get("spec", {}).get("disks") or {}
        status_disks = source_node.get("status", {}).get("diskStatus") or {}
        root_shared_disk_names = {
            disk_name
            for disk_name, disk_spec in spec_disks.items()
            if path_shares_root(
                disk_path(disk_spec, status_disks.get(disk_name) or {})
            )
        }
        if not root_shared_disk_names:
            print(
                "longhorn_pressure_evacuation=not-needed "
                f"node={args.node} reason=root-shared-longhorn-disk-absent"
            )
            return 0

        total, available, free_percentage = root_filesystem_metrics()
        minimum_available_percentage = setting_integer(
            kube, "storage-minimal-available-percentage", 25
        )
        over_provisioning_percentage = setting_integer(
            kube, "storage-over-provisioning-percentage", 100
        )
        physical_sizes = physical_replica_sizes(
            source_node, replicas, root_shared_disk_names
        )
        plan, reason = build_plan(
            source_node=args.node,
            root_shared_disk_names=root_shared_disk_names,
            nodes=nodes,
            replicas=replicas,
            volumes=volumes,
            physical_bytes_by_replica=physical_sizes,
            root_total_bytes=total,
            root_available_bytes=available,
            target_free_percentage=args.target_free_percentage,
            minimum_available_percentage=minimum_available_percentage,
            over_provisioning_percentage=over_provisioning_percentage,
        )
        if plan is None:
            print(
                "longhorn_pressure_evacuation=deferred "
                f"node={args.node} reason={reason} "
                f"root_free_percent={free_percentage}"
            )
            return 0
        assert plan is not None
        state = original_disk_state(args.node, source_node, plan)
        disk_names = set(state["disks"])
        for candidate in plan.candidates:
            print(
                "longhorn_pressure_candidate="
                f"{candidate.replica_name} volume={candidate.volume_name} "
                f"estimatedBytes={candidate.estimated_physical_bytes} "
                f"destinationNodes={','.join(candidate.destination_nodes)}"
            )
        request_evacuation(kube, args.node, state)
        print(
            "longhorn_pressure_evacuation=requested "
            f"node={args.node} disks={','.join(sorted(disk_names))} "
            f"requiredReliefBytes={plan.required_relief_bytes} "
            f"estimatedReliefBytes={plan.estimated_relief_bytes}"
        )

    deadline = time.monotonic() + args.timeout
    previous_state: tuple[str, int, int, int] | None = None
    while time.monotonic() < deadline:
        pressure = kubernetes_disk_pressure(kube, args.node)
        _, _, free_percentage = root_filesystem_metrics()
        nodes = kube.get_json(
            "-n", "longhorn-system", "get", "nodes.longhorn.io"
        ).get("items", [])
        current_node = find_node(nodes, args.node)
        disk_replicas = disk_replica_count(current_node, disk_names)
        status_disks = current_node.get("status", {}).get("diskStatus") or {}
        source_disk_ids = {
            (status_disks.get(name) or {}).get("diskUUID", "")
            for name in disk_names
        } - {""}
        replicas = kube.get_json(
            "-n", "longhorn-system", "get", "replicas.longhorn.io"
        ).get("items", [])
        selected_replicas = selected_replica_count(
            replicas,
            node_name=args.node,
            disk_ids=source_disk_ids,
            replica_names=set(state.get("replicas", {})),
        )
        current_state = (
            pressure,
            free_percentage,
            selected_replicas,
            disk_replicas,
        )
        if current_state != previous_state:
            print(
                "longhorn_pressure_evacuation=waiting "
                f"node={args.node} diskPressure={pressure} "
                f"rootFreePercent={free_percentage} "
                f"selectedReplicas={selected_replicas} "
                f"diskReplicas={disk_replicas}"
            )
            previous_state = current_state
        if pressure == "False":
            restore_disk_state(kube, args.node, state, current_node)
            print(
                "longhorn_pressure_evacuation=completed "
                f"node={args.node} rootFreePercent={free_percentage} "
                "schedulingState=restored"
            )
            return 0
        time.sleep(args.poll_interval)

    print(
        "longhorn_pressure_evacuation=deferred "
        f"node={args.node} reason=evacuation-timeout timeout={args.timeout}s "
        "evictionState=retained-for-resume"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--node", required=True)
    result.add_argument(
        "--kubectl", default="/var/lib/rancher/rke2/bin/kubectl"
    )
    result.add_argument(
        "--kubeconfig", default="/etc/rancher/rke2/rke2.yaml"
    )
    result.add_argument("--timeout", type=int, default=1200)
    result.add_argument("--poll-interval", type=int, default=10)
    result.add_argument("--target-free-percentage", type=int, default=20)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.timeout < 300:
        raise SystemExit("--timeout must be at least 300 seconds")
    if not 15 <= args.target_free_percentage <= 50:
        raise SystemExit("--target-free-percentage must be between 15 and 50")
    if not 5 <= args.poll_interval <= 60:
        raise SystemExit("--poll-interval must be between 5 and 60 seconds")
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"longhorn_pressure_evacuation=failed reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
