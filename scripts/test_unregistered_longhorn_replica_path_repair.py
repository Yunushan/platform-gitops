#!/usr/bin/env python3
"""Self-test safe relocation of replicas from removed Longhorn disks."""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repair_unregistered_longhorn_replica_paths import (  # noqa: E402
    Candidate,
    directory_manifest,
    evaluate_candidate,
    relocate_copy,
)


NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)


def longhorn_node(name: str, disk_id: str, disk_path: str):
    return {
        "metadata": {"name": name},
        "spec": {
            "allowScheduling": True,
            "disks": {
                "default-disk": {
                    "allowScheduling": True,
                    "diskType": "filesystem",
                    "path": disk_path,
                }
            },
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Schedulable", "status": "True"},
            ],
            "diskStatus": {
                "default-disk": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "Schedulable", "status": "True"},
                    ],
                    "diskPath": disk_path,
                    "diskType": "filesystem",
                    "diskUUID": disk_id,
                }
            },
        },
    }


def fixtures():
    volume_name = "pvc-forgejo"
    pv_name = volume_name
    target_node = "node-1"
    volume = {
        "metadata": {"name": volume_name},
        "spec": {
            "accessMode": "rwo",
            "fromBackup": "",
            "migratable": False,
            "migrationNodeID": "",
            "nodeID": target_node,
            "numberOfReplicas": 3,
        },
        "status": {
            "actualSize": 4096,
            "currentMigrationNodeID": "",
            "currentNodeID": "",
            "kubernetesStatus": {
                "namespace": "forgejo",
                "pvcName": "gitea-shared-storage",
                "pvName": pv_name,
            },
            "restoreRequired": False,
            "robustness": "unknown",
            "state": "attaching",
        },
    }
    engines = [
        {
            "metadata": {"name": f"{volume_name}-e-0"},
            "spec": {
                "desireState": "stopped",
                "nodeID": "",
                "volumeName": volume_name,
            },
            "status": {
                "currentState": "stopped",
                "instanceManagerName": "",
                "ip": "",
            },
        }
    ]
    replicas = [
        {
            "metadata": {"name": f"{volume_name}-r-good", "uid": "healthy-uid"},
            "spec": {
                "active": True,
                "dataDirectoryName": f"{volume_name}-healthy",
                "desireState": "running",
                "diskID": "disk-node-2",
                "diskPath": "/var/lib/longhorn",
                "failedAt": "",
                "healthyAt": "2026-08-18T08:00:00Z",
                "lastFailedAt": "2026-08-17T08:00:00Z",
                "lastHealthyAt": "2026-08-18T08:00:00Z",
                "nodeID": "node-2",
                "volumeName": volume_name,
            },
            "status": {
                "currentState": "running",
                "instanceManagerName": "instance-manager-node-2",
                "ip": "192.0.2.42",
                "port": 10000,
                "storageIP": "192.0.2.42",
            },
        },
        {
            "metadata": {"name": f"{volume_name}-r-stale", "uid": "stale-uid"},
            "spec": {
                "active": True,
                "dataDirectoryName": f"{volume_name}-stale",
                "desireState": "running",
                "diskID": "removed-node-3-disk",
                "diskPath": "/home/longhorn",
                "failedAt": "",
                "healthyAt": "2026-08-18T08:00:00Z",
                "lastFailedAt": "2026-08-17T08:00:00Z",
                "lastHealthyAt": "2026-08-18T08:00:00Z",
                "nodeID": "node-3",
                "volumeName": volume_name,
            },
            "status": {
                "currentState": "stopped",
                "instanceManagerName": "",
                "ip": "",
                "port": 0,
                "storageIP": "",
            },
        },
    ]
    longhorn_nodes = [
        longhorn_node("node-1", "disk-node-1", "/var/lib/longhorn"),
        longhorn_node("node-2", "disk-node-2", "/var/lib/longhorn"),
        longhorn_node("node-3", "disk-node-3", "/var/lib/longhorn"),
    ]
    longhorn_attachment = {
        "metadata": {"name": volume_name},
        "spec": {
            "attachmentTickets": {
                "csi-ticket": {
                    "nodeID": target_node,
                    "type": "csi-attacher",
                }
            }
        },
        "status": {
            "attachmentTicketStatuses": {
                "csi-ticket": {"satisfied": False}
            }
        },
    }
    native_attachments = [
        {
            "metadata": {
                "creationTimestamp": "2026-08-19T08:50:00Z",
                "name": "csi-failed",
            },
            "spec": {
                "attacher": "driver.longhorn.io",
                "nodeName": target_node,
                "source": {"persistentVolumeName": pv_name},
            },
            "status": {
                "attached": False,
                "attachError": {"message": "rpc deadline exceeded"},
            },
        }
    ]
    pv = {
        "metadata": {"name": pv_name},
        "spec": {
            "csi": {
                "driver": "driver.longhorn.io",
                "volumeHandle": volume_name,
            }
        },
    }
    pods = [
        {
            "metadata": {
                "name": "forgejo-0",
                "namespace": "forgejo",
                "ownerReferences": [
                    {"controller": True, "kind": "ReplicaSet", "name": "forgejo"}
                ],
            },
            "spec": {
                "nodeName": target_node,
                "volumes": [
                    {
                        "persistentVolumeClaim": {
                            "claimName": "gitea-shared-storage"
                        }
                    }
                ],
            },
            "status": {"phase": "Pending"},
        }
    ]
    return {
        "local_node": "node-3",
        "volume": volume,
        "engines": engines,
        "replicas": replicas,
        "longhorn_nodes": longhorn_nodes,
        "longhorn_attachment": longhorn_attachment,
        "native_attachments": native_attachments,
        "pv": pv,
        "pods": pods,
    }


def evaluate(mutate=None):
    data = copy.deepcopy(fixtures())
    if mutate:
        mutate(data)
    return evaluate_candidate(
        **data,
        now=NOW,
        minimum_age_seconds=120,
    )


def assert_rejected(reason: str, mutate) -> None:
    candidate, actual = evaluate(mutate)
    if candidate is not None or actual != reason:
        raise AssertionError(
            f"expected rejection {reason!r}, got candidate={candidate!r} reason={actual!r}"
        )


def verify_candidate_guards() -> Candidate:
    candidate, reason = evaluate()
    if candidate is None:
        raise AssertionError(f"expected relocation candidate, got {reason}")
    if candidate.destination_disk_id != "disk-node-3":
        raise AssertionError("candidate did not select node-3 registered disk")

    assert_rejected(
        "volume-lacks-proven-data-or-premium-redundancy",
        lambda data: data["volume"]["spec"].update(numberOfReplicas=2),
    )
    assert_rejected(
        "stopped-unregistered-replica-not-local",
        lambda data: data.update(local_node="node-2"),
    )
    assert_rejected(
        "safe-stopped-unregistered-replica-cardinality-not-one",
        lambda data: data["replicas"][1]["spec"].update(failedAt="2026-08-18T09:00:00Z"),
    )
    assert_rejected(
        "old-failed-native-attachment-cardinality-not-one",
        lambda data: data["native_attachments"][0]["status"].update(attachError={}),
    )
    assert_rejected(
        "exclusive-pending-controller-managed-consumer-absent",
        lambda data: data["pods"][0]["status"].update(phase="Running"),
    )

    def add_second_disk(data):
        node = data["longhorn_nodes"][2]
        node["spec"]["disks"]["extra-disk"] = {
            "allowScheduling": True,
            "diskType": "filesystem",
            "path": "/srv/longhorn",
        }
        node["status"]["diskStatus"]["extra-disk"] = {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Schedulable", "status": "True"},
            ],
            "diskPath": "/srv/longhorn",
            "diskType": "filesystem",
            "diskUUID": "disk-node-3-extra",
        }

    assert_rejected("safe-local-destination-disk-cardinality-not-one", add_second_disk)
    return candidate


def verify_copy_preserves_source(candidate: Candidate) -> None:
    with tempfile.TemporaryDirectory(prefix="longhorn-relocation-test-") as temp:
        root = Path(temp)
        source_disk = root / "old-disk"
        destination_disk = root / "new-disk"
        source = source_disk / "replicas" / candidate.data_directory_name
        destination_root = destination_disk / "replicas"
        source.mkdir(parents=True)
        destination_root.mkdir(parents=True)
        (source / "volume.meta").write_text("metadata\n", encoding="utf-8")
        sparse = source / "volume-head-000.img"
        with sparse.open("wb") as stream:
            stream.seek(8 * 1024 * 1024)
            stream.write(b"longhorn")
        snapshots = source / "snapshots"
        snapshots.mkdir()
        (snapshots / "index").write_text("snapshot\n", encoding="utf-8")

        local_candidate = replace(
            candidate,
            source_disk_path=str(source_disk),
            destination_disk_path=str(destination_disk),
        )

        def test_copy(source_path: Path, destination_path: Path, timeout: int) -> None:
            if timeout != 60:
                raise AssertionError("copy timeout was not propagated")
            shutil.copytree(
                source_path,
                destination_path,
                symlinks=True,
                copy_function=shutil.copy2,
            )

        source_path, destination, action = relocate_copy(
            local_candidate,
            timeout_seconds=60,
            copy_tree=test_copy,
        )
        if action != "create-verified-reflink-copy":
            raise AssertionError(f"unexpected first relocation action: {action}")
        if not source_path.is_dir() or not destination.is_dir():
            raise AssertionError("relocation did not preserve source and create destination")
        if directory_manifest(source_path) != directory_manifest(destination):
            raise AssertionError("relocation manifests differ")

        _, _, action = relocate_copy(
            local_candidate,
            timeout_seconds=60,
            copy_tree=test_copy,
        )
        if action != "reuse-verified-relocation-copy":
            raise AssertionError(f"unexpected idempotent relocation action: {action}")

        (destination / "volume.meta").write_text("changed\n", encoding="utf-8")
        try:
            relocate_copy(
                local_candidate,
                timeout_seconds=60,
                copy_tree=test_copy,
            )
        except RuntimeError as exc:
            if "does not match source" not in str(exc):
                raise
        else:
            raise AssertionError("mismatched destination copy was not rejected")


def verify_ansible_integration() -> None:
    playbook = (ROOT / "ansible/playbooks/repair-longhorn-runtime.yml").read_text(
        encoding="utf-8"
    )
    required = (
        "longhorn_instance_manager=waiting",
        "repair_unregistered_longhorn_replica_paths.py",
        "Relocate stopped Longhorn replicas from removed local disk registrations",
        "action=relocate-unregistered-replica-copy-preserved",
    )
    missing = [fragment for fragment in required if fragment not in playbook]
    if missing:
        raise AssertionError(
            "Longhorn runtime repair is missing relocation integration: "
            + ", ".join(missing)
        )


def main() -> int:
    candidate = verify_candidate_guards()
    verify_copy_preserves_source(candidate)
    verify_ansible_integration()
    print("Unregistered Longhorn replica path repair self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
