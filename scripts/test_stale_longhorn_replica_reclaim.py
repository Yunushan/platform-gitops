#!/usr/bin/env python3
"""Self-test fail-closed stale Longhorn replica directory reclamation."""

from __future__ import annotations

import copy
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclaim_stale_longhorn_replica_data import (  # noqa: E402
    ReclaimCandidate,
    ReplicaDirectory,
    directory_metrics,
    parse_directory_name,
    quarantine_candidate,
    remove_quarantined_candidate,
    select_candidate,
)


VOLUME = "pvc-7ed0728c-4021-4919-9bec-d85ed2a4308b"
NOW_NS = 1_800_000_000_000_000_000
GIB = 1024**3


def longhorn_node(name: str, disk_id: str):
    return {
        "metadata": {"name": name},
        "spec": {
            "disks": {
                "default-disk": {
                    "diskType": "filesystem",
                    "evictionRequested": False,
                    "path": "/var/lib/longhorn",
                }
            }
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "reason": ""},
            ],
            "diskStatus": {
                "default-disk": {
                    "diskPath": "/var/lib/longhorn",
                    "diskType": "filesystem",
                    "diskUUID": disk_id,
                }
            }
        },
    }


def manager_pod(name: str, *, ready: bool = True, running: bool = True):
    state = (
        {"running": {"startedAt": "2026-08-22T00:00:00Z"}}
        if running
        else {"waiting": {"reason": "CrashLoopBackOff"}}
    )
    return {
        "metadata": {"name": f"longhorn-manager-{name}"},
        "spec": {"nodeName": name, "containers": [{"name": "manager"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "manager", "ready": ready, "state": state}
            ],
        },
    }


def replica(name: str, node: str, disk_id: str, directory: str):
    return {
        "metadata": {"name": name},
        "spec": {
            "active": True,
            "dataDirectoryName": directory,
            "desireState": "running",
            "diskID": disk_id,
            "failedAt": "",
            "healthyAt": "2026-08-20T08:00:00Z",
            "lastFailedAt": "2026-08-19T08:00:00Z",
            "lastHealthyAt": "2026-08-20T08:00:00Z",
            "nodeID": node,
            "volumeName": VOLUME,
        },
        "status": {"currentState": "stopped"},
    }


def directory(name: str, *, tombstone: bool = False, inode: int = 1):
    return ReplicaDirectory(
        volume_name=VOLUME,
        directory_name=name,
        path=Path("/var/lib/longhorn/replicas")
        / (f".{name}.platform-stale-replica-reclaim" if tombstone else name),
        disk_name="default-disk",
        disk_id="disk-node-1",
        allocated_bytes=32 * GIB,
        newest_mtime_ns=NOW_NS - 7200 * 1_000_000_000,
        device=1,
        inode=inode,
        tombstone=tombstone,
    )


def fixtures():
    live_name = f"{VOLUME}-1113cf9e"
    stale_name = f"{VOLUME}-8e679fdc"
    return {
        "node_name": "node-1",
        "kube_node": {
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True"},
                    {"type": "DiskPressure", "status": "True"},
                    {"type": "MemoryPressure", "status": "False"},
                    {"type": "PIDPressure", "status": "False"},
                    {"type": "NetworkUnavailable", "status": "False"},
                ]
            }
        },
        "manager_pods": [
            manager_pod("node-1"),
            manager_pod("node-2"),
            manager_pod("node-3"),
        ],
        "settings": {
            "orphan-resource-auto-deletion": "replica-data;instance",
            "orphan-resource-auto-deletion-grace-period": "300",
        },
        "directories": [directory(live_name), directory(stale_name, inode=2)],
        "volumes": [
            {
                "metadata": {"name": VOLUME},
                "spec": {
                    "dataEngine": "v1",
                    "fromBackup": "",
                    "migrationNodeID": "",
                    "nodeID": "",
                    "numberOfReplicas": 3,
                },
                "status": {
                    "actualSize": 32 * GIB,
                    "currentMigrationNodeID": "",
                    "currentNodeID": "",
                    "kubernetesStatus": {
                        "namespace": "platform-data",
                        "pvcName": "archive",
                        "pvName": VOLUME,
                    },
                    "ownerID": "node-2",
                    "restoreRequired": False,
                    "robustness": "unknown",
                    "state": "detached",
                },
            }
        ],
        "replicas": [
            replica(f"{VOLUME}-r-1", "node-1", "disk-node-1", live_name),
            replica(
                f"{VOLUME}-r-2", "node-2", "disk-node-2", f"{VOLUME}-22222222"
            ),
            replica(
                f"{VOLUME}-r-3", "node-3", "disk-node-3", f"{VOLUME}-33333333"
            ),
        ],
        "engines": [
            {
                "metadata": {"name": f"{VOLUME}-e-0"},
                "spec": {
                    "desireState": "stopped",
                    "nodeID": "",
                    "volumeName": VOLUME,
                },
                "status": {
                    "currentState": "stopped",
                    "instanceManagerName": "",
                },
            }
        ],
        "longhorn_attachments": [
            {
                "metadata": {"name": VOLUME},
                "spec": {"attachmentTickets": {}},
            }
        ],
        "native_attachments": [],
        "pvs": [
            {
                "metadata": {"name": VOLUME},
                "spec": {
                    "csi": {
                        "driver": "driver.longhorn.io",
                        "volumeHandle": VOLUME,
                    },
                    "claimRef": {
                        "name": "archive",
                        "namespace": "platform-data",
                        "uid": "claim-uid",
                    },
                },
                "status": {"phase": "Bound"},
            }
        ],
        "pvcs": [
            {
                "metadata": {
                    "name": "archive",
                    "namespace": "platform-data",
                    "uid": "claim-uid",
                },
                "spec": {"volumeName": VOLUME},
                "status": {"phase": "Bound"},
            }
        ],
        "pods": [],
        "nodes": [
            longhorn_node("node-1", "disk-node-1"),
            longhorn_node("node-2", "disk-node-2"),
            longhorn_node("node-3", "disk-node-3"),
        ],
        "now_ns": NOW_NS,
        "minimum_age_seconds": 3600,
        "minimum_reclaim_bytes": GIB,
    }


def evaluate(mutate=None):
    data = copy.deepcopy(fixtures())
    if mutate:
        mutate(data)
    return select_candidate(**data)


def assert_rejected(mutate, expected_reason: str | None = None) -> None:
    candidate, reason = evaluate(mutate)
    if candidate is not None:
        raise AssertionError(f"unsafe stale directory candidate accepted: {candidate}")
    if expected_reason is not None and reason != expected_reason:
        raise AssertionError(f"expected rejection {expected_reason}, got {reason}")


def pressure_degrade_local_manager(data) -> None:
    data["manager_pods"][0]["status"]["containerStatuses"][0]["ready"] = False
    data["nodes"][0]["status"]["conditions"][0].update(
        status="False", reason="KubernetesNodePressure"
    )


def pressure_degrade_without_quorum(data) -> None:
    pressure_degrade_local_manager(data)
    data["manager_pods"][1]["status"]["containerStatuses"][0]["ready"] = False


def pressure_degrade_with_stopped_manager(data) -> None:
    pressure_degrade_local_manager(data)
    data["manager_pods"][0]["status"]["containerStatuses"][0]["state"] = {
        "waiting": {"reason": "CrashLoopBackOff"}
    }


def verify_candidate_guards() -> ReclaimCandidate:
    candidate, reason = evaluate()
    if candidate is None:
        raise AssertionError(f"expected stale replica candidate, got {reason}")
    if candidate.directory.directory_name != f"{VOLUME}-8e679fdc":
        raise AssertionError("reclaimer selected the registered replica directory")

    degraded_candidate, degraded_reason = evaluate(pressure_degrade_local_manager)
    if degraded_candidate is None:
        raise AssertionError(
            "pressure-degraded local manager with a ready quorum was rejected: "
            + degraded_reason
        )

    assert_rejected(
        lambda data: data["kube_node"]["status"]["conditions"][0].update(
            status="False"
        )
    )
    assert_rejected(
        lambda data: data["manager_pods"][0]["status"]["containerStatuses"][
            0
        ].update(ready=False),
        "longhorn-manager-unready-not-caused-by-kubernetes-pressure",
    )
    assert_rejected(
        pressure_degrade_without_quorum,
        "longhorn-manager-control-plane-quorum-unavailable",
    )
    offline_candidate, offline_reason = evaluate(
        pressure_degrade_with_stopped_manager
    )
    if offline_candidate is None:
        raise AssertionError(
            "offline pressure-node manager with a ready quorum was rejected: "
            + offline_reason
        )
    assert_rejected(
        lambda data: data["manager_pods"].pop(),
        "longhorn-manager-topology-not-safe",
    )
    assert_rejected(
        lambda data: data["volumes"][0]["status"].update(ownerID="node-9")
    )
    assert_rejected(
        lambda data: data["settings"].update(
            {"orphan-resource-auto-deletion": "instance"}
        )
    )
    assert_rejected(
        lambda data: data["volumes"][0]["status"].update(
            state="attached", currentNodeID="node-1"
        )
    )
    assert_rejected(lambda data: data["replicas"][0]["spec"].update(failedAt="now"))
    assert_rejected(
        lambda data: data["replicas"][0]["spec"].update(evictionRequested=True)
    )
    assert_rejected(
        lambda data: data["nodes"][0]["metadata"].update(
            annotations={
                "platform.gitops.io/root-pressure-eviction": '{"version":1}'
            }
        )
    )
    assert_rejected(
        lambda data: data["longhorn_attachments"][0]["spec"].update(
            attachmentTickets={"ticket": {"nodeID": "node-1"}}
        )
    )
    assert_rejected(
        lambda data: data["native_attachments"].append(
            {
                "metadata": {"name": "csi-attachment"},
                "spec": {"source": {"persistentVolumeName": VOLUME}},
            }
        )
    )
    assert_rejected(
        lambda data: data["native_attachments"].append(
            {
                "metadata": {
                    "name": "deleting-csi-attachment",
                    "deletionTimestamp": "2026-08-22T00:00:00Z",
                },
                "spec": {"source": {"persistentVolumeName": VOLUME}},
            }
        )
    )
    assert_rejected(
        lambda data: data["pods"].append(
            {
                "metadata": {"name": "consumer", "namespace": "platform-data"},
                "spec": {
                    "volumes": [
                        {"persistentVolumeClaim": {"claimName": "archive"}}
                    ]
                },
            }
        )
    )
    assert_rejected(
        lambda data: data["pods"].append(
            {
                "metadata": {
                    "name": "terminating-consumer",
                    "namespace": "platform-data",
                    "deletionTimestamp": "2026-08-22T00:00:00Z",
                },
                "spec": {
                    "volumes": [
                        {"persistentVolumeClaim": {"claimName": "archive"}}
                    ]
                },
            }
        )
    )
    assert_rejected(
        lambda data: data["directories"].__setitem__(
            1,
            replace(
                data["directories"][1],
                newest_mtime_ns=data["now_ns"] - 60 * 1_000_000_000,
            ),
        )
    )
    assert_rejected(
        lambda data: data["directories"].append(
            directory(f"{VOLUME}-44444444", inode=3)
        )
    )
    return candidate


def verify_directory_name_contract() -> None:
    live = f"{VOLUME}-1113cf9e"
    tombstone = f".{live}.platform-stale-replica-reclaim"
    if parse_directory_name(live) != (VOLUME, live, False):
        raise AssertionError("strict Longhorn directory name was rejected")
    if parse_directory_name(tombstone) != (VOLUME, live, True):
        raise AssertionError("strict quarantine directory name was rejected")
    for unsafe in ("../replica", "pvc-name-stale", ".hidden"):
        if parse_directory_name(unsafe) is not None:
            raise AssertionError(f"unsafe directory name was accepted: {unsafe}")


def verify_quarantine_before_delete() -> None:
    with tempfile.TemporaryDirectory(prefix="platform-stale-replica-") as temp:
        replicas_root = Path(temp) / "replicas"
        replicas_root.mkdir()
        live_name = f"{VOLUME}-1113cf9e"
        stale_name = f"{VOLUME}-8e679fdc"
        live_path = replicas_root / live_name
        stale_path = replicas_root / stale_name
        live_path.mkdir()
        stale_path.mkdir()
        (live_path / "volume-head-000.img").write_bytes(b"live")
        (stale_path / "volume-head-000.img").write_bytes(b"stale")
        allocated, newest, device, inode = directory_metrics(stale_path)
        candidate = ReclaimCandidate(
            directory=ReplicaDirectory(
                volume_name=VOLUME,
                directory_name=stale_name,
                path=stale_path,
                disk_name="default-disk",
                disk_id="disk-node-1",
                allocated_bytes=allocated,
                newest_mtime_ns=newest,
                device=device,
                inode=inode,
                tombstone=False,
            ),
            pvc_namespace="platform-data",
            pvc_name="archive",
            pv_name=VOLUME,
        )
        quarantined = quarantine_candidate(candidate)
        if stale_path.exists() or not quarantined.directory.path.exists():
            raise AssertionError("stale directory was not atomically quarantined")
        if not live_path.exists():
            raise AssertionError("registered sibling changed during quarantine")
        remove_quarantined_candidate(quarantined, lambda _path: False)
        if quarantined.directory.path.exists():
            raise AssertionError("quarantined stale directory was retained")
        if not live_path.exists():
            raise AssertionError("registered sibling was removed")


def verify_repository_integration() -> None:
    playbook = (ROOT / "ansible/playbooks/cleanup-node-storage.yml").read_text(
        encoding="utf-8"
    )
    required = (
        "PLATFORM_NODE_STORAGE_LONGHORN_STALE_REPLICA_RECLAIM",
        "reclaim_stale_longhorn_replica_data.py",
        "Reclaim one proven stale Longhorn replica directory",
        "longhorn_stale_replica_reclaim=completed",
    )
    missing = [fragment for fragment in required if fragment not in playbook]
    if missing:
        raise AssertionError(
            "node storage cleanup is missing stale replica integration: "
            + ", ".join(missing)
        )


def main() -> int:
    verify_candidate_guards()
    verify_directory_name_contract()
    verify_quarantine_before_delete()
    verify_repository_integration()
    print("Stale Longhorn replica data reclaimer self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
