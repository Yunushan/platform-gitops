#!/usr/bin/env python3
"""Quarantine one unmapped replica that blocks a proven Longhorn attachment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Candidate:
    volume_name: str
    engine_name: str
    pv_name: str
    namespace: str
    pvc_name: str
    target_node: str
    attachment_name: str
    orphan_replica: str
    orphan_node: str
    orphan_disk_id: str
    orphan_disk_path: str
    healthy_replicas: tuple[str, ...]
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


def registered_disk_ids(nodes: list[JsonObject]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in nodes:
        name = node.get("metadata", {}).get("name", "")
        if not name or node.get("metadata", {}).get("deletionTimestamp"):
            continue
        result[name] = {
            disk.get("diskUUID", "")
            for disk in (node.get("status", {}).get("diskStatus") or {}).values()
            if disk.get("diskUUID")
        }
    return result


def replica_history_is_safe(replica: JsonObject) -> bool:
    spec = replica.get("spec", {})
    healthy_at = parse_timestamp(spec.get("lastHealthyAt") or spec.get("healthyAt", ""))
    failed_at = parse_timestamp(spec.get("lastFailedAt", ""))
    if healthy_at is None:
        return False
    return failed_at is None or healthy_at > failed_at


def evaluate_candidate(
    *,
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
    if actual_size <= 0:
        return None, "volume-has-no-proven-data"
    if declared_replicas < 3:
        return None, "volume-has-insufficient-declared-redundancy"
    if spec.get("accessMode") != "rwo" or spec.get("migratable") is True:
        return None, "volume-not-nonmigratable-rwo"
    if spec.get("migrationNodeID") or status.get("currentMigrationNodeID"):
        return None, "volume-migration-active"

    target_node = spec.get("nodeID", "")
    current_node = status.get("currentNodeID", "")
    if not target_node or current_node:
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
    engine = volume_engines[0]
    engine_spec = engine.get("spec", {})
    engine_status = engine.get("status", {})
    if not (
        not engine_spec.get("nodeID")
        and engine_spec.get("desireState") == "stopped"
        and engine_status.get("currentState") == "stopped"
        and not engine_status.get("instanceManagerName")
        and not engine_status.get("ip")
    ):
        return None, "engine-not-stopped-unassigned"

    disk_ids = registered_disk_ids(longhorn_nodes)
    volume_replicas = [
        replica
        for replica in replicas
        if replica.get("spec", {}).get("volumeName") == volume_name
        and not replica.get("metadata", {}).get("deletionTimestamp")
    ]
    healthy_replicas = []
    for replica in volume_replicas:
        replica_spec = replica.get("spec", {})
        replica_status = replica.get("status", {})
        node_id = replica_spec.get("nodeID", "")
        disk_id = replica_spec.get("diskID", "")
        if not (
            replica_spec.get("active") is True
            and replica_spec.get("desireState") == "running"
            and replica_spec.get("healthyAt")
            and not replica_spec.get("failedAt")
            and replica_history_is_safe(replica)
            and replica_status.get("currentState") == "running"
            and replica_status.get("ip")
            and replica_status.get("storageIP")
            and int(replica_status.get("port") or 0) > 0
            and node_id
            and disk_id in disk_ids.get(node_id, set())
        ):
            continue
        healthy_replicas.append(replica)
    healthy_nodes = {
        replica.get("spec", {}).get("nodeID", "") for replica in healthy_replicas
    }
    if len(healthy_replicas) < 2 or len(healthy_nodes) < 2:
        return None, "insufficient-safe-running-replicas"

    unmapped_replicas = []
    for replica in volume_replicas:
        replica_spec = replica.get("spec", {})
        replica_status = replica.get("status", {})
        node_id = replica_spec.get("nodeID", "")
        disk_id = replica_spec.get("diskID", "")
        if not (
            replica_spec.get("active") is True
            and replica_spec.get("desireState") == "running"
            and replica_spec.get("healthyAt")
            and not replica_spec.get("failedAt")
            and replica_status.get("currentState") == "stopped"
            and node_id
            and disk_id
            and replica_spec.get("diskPath")
            and disk_id not in disk_ids.get(node_id, set())
        ):
            continue
        unmapped_replicas.append(replica)
    if len(unmapped_replicas) != 1:
        return None, "unmapped-stopped-replica-cardinality-not-one"
    orphan = unmapped_replicas[0]

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

    consumers = [
        pod
        for pod in pods
        if pod.get("metadata", {}).get("namespace") == namespace
        and not pod.get("metadata", {}).get("deletionTimestamp")
        and pod.get("spec", {}).get("nodeName") == target_node
        and pod.get("status", {}).get("phase") == "Pending"
        and controller_owner(pod) is not None
        and pod_uses_claim(pod, pvc_name)
    ]
    if not consumers:
        return None, "pending-controller-managed-consumer-absent"

    orphan_spec = orphan.get("spec", {})
    return (
        Candidate(
            volume_name=volume_name,
            engine_name=engine.get("metadata", {}).get("name", ""),
            pv_name=pv_name,
            namespace=namespace,
            pvc_name=pvc_name,
            target_node=target_node,
            attachment_name=failed_attachments[0].get("metadata", {}).get("name", ""),
            orphan_replica=orphan.get("metadata", {}).get("name", ""),
            orphan_node=orphan_spec.get("nodeID", ""),
            orphan_disk_id=orphan_spec.get("diskID", ""),
            orphan_disk_path=orphan_spec.get("diskPath", ""),
            healthy_replicas=tuple(
                sorted(replica.get("metadata", {}).get("name", "") for replica in healthy_replicas)
            ),
            consumer_pods=tuple(
                sorted(pod.get("metadata", {}).get("name", "") for pod in consumers)
            ),
        ),
        "unmapped-stopped-replica-blocking-attachment",
    )


class Kubectl:
    def __init__(self, executable: str, kubeconfig: str):
        self.base = [executable, "--kubeconfig", kubeconfig]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(self.base + list(args), text=True, capture_output=True)
        if check and result.returncode != 0:
            sys.stderr.write((result.stderr or "") + (result.stdout or ""))
            raise RuntimeError(f"kubectl failed: {' '.join(args)}")
        return result

    def get_json(self, *args: str) -> JsonObject:
        return json.loads(self.run(*args, "-o", "json").stdout)


def items(kube: Kubectl, *args: str) -> list[JsonObject]:
    return kube.get_json(*args).get("items", [])


def discover_candidates(
    kube: Kubectl,
    *,
    minimum_age_seconds: int,
    report: bool,
) -> list[Candidate]:
    volumes = items(kube, "-n", "longhorn-system", "get", "volumes.longhorn.io")
    engines = items(kube, "-n", "longhorn-system", "get", "engines.longhorn.io")
    replicas = items(kube, "-n", "longhorn-system", "get", "replicas.longhorn.io")
    longhorn_nodes = items(kube, "-n", "longhorn-system", "get", "nodes.longhorn.io")
    longhorn_attachments = {
        item.get("metadata", {}).get("name", ""): item
        for item in items(kube, "-n", "longhorn-system", "get", "volumeattachments.longhorn.io")
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
            data_bearing = int(status.get("actualSize") or 0) > 0
        except (TypeError, ValueError):
            data_bearing = False
        if status.get("state") != "attaching" or not data_bearing:
            continue
        volume_name = volume.get("metadata", {}).get("name", "")
        pv_name = (status.get("kubernetesStatus") or {}).get("pvName", "")
        candidate, reason = evaluate_candidate(
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
            print(f"longhorn_volume={volume_name} action=retain reason={reason}")
    return candidates


def attachment_state(kube: Kubectl, candidate: Candidate) -> tuple[bool, str]:
    volume = kube.get_json("-n", "longhorn-system", "get", f"volumes.longhorn.io/{candidate.volume_name}")
    engine = kube.get_json("-n", "longhorn-system", "get", f"engines.longhorn.io/{candidate.engine_name}")
    volume_status = volume.get("status", {})
    engine_spec = engine.get("spec", {})
    engine_status = engine.get("status", {})
    state = volume_status.get("state", "unknown")
    robustness = volume_status.get("robustness", "unknown")
    current_node = volume_status.get("currentNodeID", "")
    engine_node = engine_spec.get("nodeID", "")
    desired = engine_spec.get("desireState", "")
    current = engine_status.get("currentState", "")
    ready = (
        state == "attached"
        and robustness in {"healthy", "degraded"}
        and current_node == candidate.target_node
        and engine_node == candidate.target_node
        and desired == "running"
        and current == "running"
    )
    detail = (
        f"state={state} robustness={robustness} currentNode={current_node or 'none'} "
        f"engineNode={engine_node or 'none'} engineDesired={desired or 'none'} "
        f"engineCurrent={current or 'none'}"
    )
    return ready, detail


def repair(
    kube: Kubectl,
    *,
    timeout_seconds: int,
    minimum_age_seconds: int,
    settle_seconds: int,
) -> int:
    candidates = discover_candidates(kube, minimum_age_seconds=minimum_age_seconds, report=True)
    if not candidates:
        print("longhorn_stuck_attachment_replica_repair=not-needed")
        return 0

    if settle_seconds > 0:
        print(f"longhorn_stuck_attachment_replica_repair=settling seconds={settle_seconds}")
        time.sleep(settle_seconds)
        candidates = discover_candidates(kube, minimum_age_seconds=minimum_age_seconds, report=True)
        if not candidates:
            print("longhorn_stuck_attachment_replica_repair=progressing-before-quarantine")
            return 0

    failed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for candidate in candidates:
        patch = json.dumps(
            {
                "spec": {
                    "desireState": "stopped",
                    "failedAt": failed_at,
                    "lastFailedAt": failed_at,
                }
            },
            separators=(",", ":"),
        )
        kube.run(
            "-n",
            "longhorn-system",
            "patch",
            f"replicas.longhorn.io/{candidate.orphan_replica}",
            "--type=merge",
            "-p",
            patch,
        )
        print(
            f"longhorn_volume={candidate.volume_name} replica={candidate.orphan_replica} "
            f"orphanNode={candidate.orphan_node} orphanDisk={candidate.orphan_disk_id} "
            f"orphanPath={candidate.orphan_disk_path} attachment={candidate.attachment_name} "
            f"healthyReplicas={','.join(candidate.healthy_replicas)} "
            f"consumers={','.join(candidate.consumer_pods)} failedAt={failed_at} "
            "action=quarantine-unmapped-replica"
        )

    deadline = time.monotonic() + timeout_seconds
    pending = {candidate.volume_name: candidate for candidate in candidates}
    last_report = 0.0
    while pending and time.monotonic() < deadline:
        report_now = time.monotonic() - last_report >= 15
        for volume_name, candidate in list(pending.items()):
            ready, detail = attachment_state(kube, candidate)
            if ready:
                pending.pop(volume_name)
                print(f"longhorn_volume={volume_name} result=attached {detail}")
            elif "robustness=faulted" in detail:
                print(
                    f"longhorn_volume={volume_name} result=fail reason=volume-became-faulted {detail}",
                    file=sys.stderr,
                )
                return 1
            elif report_now:
                print(f"longhorn_volume={volume_name} result=waiting {detail}")
        if report_now:
            last_report = time.monotonic()
        if pending:
            time.sleep(5)

    if pending:
        for volume_name, candidate in sorted(pending.items()):
            _, detail = attachment_state(kube, candidate)
            print(
                f"longhorn_volume={volume_name} result=fail "
                f"reason=attachment-reconciliation-timeout {detail}",
                file=sys.stderr,
            )
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quarantine one unmapped Longhorn replica blocking a proven attachment",
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--minimum-age", type=int, default=120)
    parser.add_argument("--settle", type=int, default=30)
    parser.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.minimum_age < 60 or args.settle < 0:
        raise SystemExit("timeout must be positive; minimum age must be at least 60 seconds")
    kube = Kubectl(args.kubectl, args.kubeconfig)
    try:
        return repair(
            kube,
            timeout_seconds=args.timeout,
            minimum_age_seconds=args.minimum_age,
            settle_seconds=args.settle,
        )
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"longhorn_stuck_attachment_replica_repair=fail error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
