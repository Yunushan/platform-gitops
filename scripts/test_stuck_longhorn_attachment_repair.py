#!/usr/bin/env python3
"""Self-test guarded Longhorn unmapped-replica quarantine selection."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repair_stuck_longhorn_attachments import evaluate_candidate  # noqa: E402


NOW = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)


def verify_ansible_repair_bundle() -> None:
    playbook_path = ROOT / "ansible/playbooks/repair-longhorn-runtime.yml"
    playbook = playbook_path.read_text(encoding="utf-8")
    required_fragments = (
        "Create temporary Longhorn Python repair workspace",
        "Install Longhorn Python repair bundle temporarily",
        "repair_stuck_longhorn_attachments.py",
        "repair_empty_faulted_longhorn_claims.py",
        "bounded_subprocess.py",
        "strict_json.py",
        "subprocess_timeout.py",
        "Remove temporary Longhorn Python repair workspace",
    )
    missing = [fragment for fragment in required_fragments if fragment not in playbook]
    if missing:
        raise AssertionError(
            "Longhorn runtime repair playbook is missing its remote Python bundle: "
            + ", ".join(missing)
        )

    for script_name in (
        "repair_stuck_longhorn_attachments.py",
        "repair_empty_faulted_longhorn_claims.py",
    ):
        remote_path = (
            '"{{ platform_longhorn_runtime_python_workspace.path }}/'
            f'{script_name}"'
        )
        if remote_path not in playbook:
            raise AssertionError(
                f"Longhorn runtime repair does not execute staged {script_name}"
            )


def fixtures():
    volume_name = "pvc-data"
    pv_name = volume_name
    target_node = "node-2"
    volume = {
        "metadata": {"name": volume_name},
        "spec": {
            "accessMode": "rwo",
            "migratable": False,
            "migrationNodeID": "",
            "nodeID": target_node,
            "numberOfReplicas": 3,
        },
        "status": {
            "actualSize": 1024,
            "currentMigrationNodeID": "",
            "currentNodeID": "",
            "kubernetesStatus": {
                "namespace": "platform-databases",
                "pvcName": "platform-postgres-4",
                "pvName": pv_name,
            },
            "ownerID": target_node,
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
    replicas = []
    for index, node in enumerate(("node-1", "node-2"), start=1):
        replicas.append(
            {
                "metadata": {"name": f"{volume_name}-r-{index}"},
                "spec": {
                    "active": True,
                    "desireState": "running",
                    "diskID": f"registered-disk-{index}",
                    "diskPath": "/var/lib/longhorn",
                    "failedAt": "",
                    "healthyAt": "2026-07-21T11:00:00Z",
                    "lastFailedAt": "2026-07-20T11:00:00Z",
                    "lastHealthyAt": "2026-07-21T11:00:00Z",
                    "nodeID": node,
                    "volumeName": volume_name,
                },
                "status": {
                    "currentState": "running",
                    "ip": f"10.42.{index}.10",
                    "port": 10000 + index,
                    "storageIP": f"10.42.{index}.10",
                },
            }
        )
    replicas.append(
        {
            "metadata": {"name": f"{volume_name}-r-3"},
            "spec": {
                "active": True,
                "desireState": "running",
                "diskID": "removed-disk-3",
                "diskPath": "/home/longhorn",
                "failedAt": "",
                "healthyAt": "2026-07-21T11:00:00Z",
                "lastHealthyAt": "2026-07-21T11:00:00Z",
                "nodeID": "node-3",
                "volumeName": volume_name,
            },
            "status": {"currentState": "stopped"},
        }
    )
    longhorn_nodes = [
        {
            "metadata": {"name": f"node-{index}"},
            "status": {
                "diskStatus": {
                    "default-disk": {"diskUUID": f"registered-disk-{index}"}
                }
            },
        }
        for index in range(1, 4)
    ]
    longhorn_attachment = {
        "metadata": {"name": volume_name},
        "spec": {
            "attachmentTickets": {
                "csi-ticket": {
                    "id": "csi-ticket",
                    "nodeID": target_node,
                    "type": "csi-attacher",
                }
            }
        },
        "status": {
            "attachmentTicketStatuses": {
                "csi-ticket": {"id": "csi-ticket", "satisfied": False}
            }
        },
    }
    native_attachments = [
        {
            "metadata": {
                "name": "csi-native",
                "creationTimestamp": "2026-07-24T07:50:00Z",
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
                "name": "platform-postgres-4",
                "namespace": "platform-databases",
                "ownerReferences": [
                    {
                        "controller": True,
                        "kind": "Cluster",
                        "name": "platform-postgres",
                    }
                ],
            },
            "spec": {
                "nodeName": target_node,
                "volumes": [
                    {"persistentVolumeClaim": {"claimName": "platform-postgres-4"}}
                ],
            },
            "status": {"phase": "Pending"},
        }
    ]
    return {
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


def expect_rejected(reason: str, mutate) -> None:
    candidate, actual_reason = evaluate(mutate)
    assert candidate is None and actual_reason == reason, (actual_reason, reason)


def main() -> int:
    verify_ansible_repair_bundle()

    candidate, reason = evaluate()
    assert candidate is not None, reason
    assert candidate.target_node == "node-2"
    assert candidate.orphan_replica == "pvc-data-r-3"
    assert candidate.orphan_node == "node-3"
    assert candidate.orphan_disk_id == "removed-disk-3"
    assert candidate.healthy_replicas == ("pvc-data-r-1", "pvc-data-r-2")
    assert reason == "unmapped-stopped-replica-blocking-attachment"

    expect_rejected(
        "old-failed-native-attachment-cardinality-not-one",
        lambda data: data["native_attachments"][0]["metadata"].update(
            creationTimestamp="2026-07-24T07:59:30Z"
        ),
    )
    expect_rejected(
        "old-failed-native-attachment-cardinality-not-one",
        lambda data: data["native_attachments"][0]["status"].update(attached=True),
    )
    expect_rejected(
        "active-unsatisfied-csi-ticket-absent",
        lambda data: data["longhorn_attachment"]["status"][
            "attachmentTicketStatuses"
        ]["csi-ticket"].update(satisfied=True),
    )
    expect_rejected(
        "engine-not-stopped-unassigned",
        lambda data: data["engines"][0]["spec"].update(
            nodeID="node-2", desireState="running"
        ),
    )
    expect_rejected(
        "insufficient-safe-running-replicas",
        lambda data: data["replicas"][1]["status"].update(currentState="stopped"),
    )
    expect_rejected(
        "pending-controller-managed-consumer-absent",
        lambda data: data["pods"][0]["metadata"].update(ownerReferences=[]),
    )
    expect_rejected(
        "volume-has-insufficient-declared-redundancy",
        lambda data: data["volume"]["spec"].update(numberOfReplicas=2),
    )
    expect_rejected(
        "volume-not-nonmigratable-rwo",
        lambda data: data["volume"]["spec"].update(migratable=True),
    )
    expect_rejected(
        "volume-target-state-not-stuck-signature",
        lambda data: data["volume"]["status"].update(currentNodeID="node-2"),
    )
    expect_rejected(
        "unmapped-stopped-replica-cardinality-not-one",
        lambda data: data["longhorn_nodes"][2]["status"]["diskStatus"].update(
            legacy={"diskUUID": "removed-disk-3"}
        ),
    )
    expect_rejected(
        "unmapped-stopped-replica-cardinality-not-one",
        lambda data: data["replicas"][2]["spec"].update(
            failedAt="2026-07-24T07:00:00Z"
        ),
    )
    expect_rejected(
        "unmapped-stopped-replica-cardinality-not-one",
        lambda data: data["replicas"].append(
            {
                "metadata": {"name": "pvc-data-r-4"},
                "spec": {
                    "active": True,
                    "desireState": "running",
                    "diskID": "removed-disk-4",
                    "diskPath": "/old/longhorn",
                    "failedAt": "",
                    "healthyAt": "2026-07-21T11:00:00Z",
                    "nodeID": "node-4",
                    "volumeName": "pvc-data",
                },
                "status": {"currentState": "stopped"},
            }
        ),
    )
    expect_rejected(
        "insufficient-safe-running-replicas",
        lambda data: data["replicas"][0]["spec"].update(
            lastFailedAt="2026-07-22T11:00:00Z"
        ),
    )

    print("Stuck Longhorn unmapped-replica repair self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
