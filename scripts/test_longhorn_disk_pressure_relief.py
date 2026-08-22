#!/usr/bin/env python3
"""Self-test guarded Longhorn root-disk pressure relief planning."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from relieve_longhorn_disk_pressure import (  # noqa: E402
    ANNOTATION,
    Disk,
    EvacuationPlan,
    build_plan,
    disk_path,
    original_disk_state,
    physical_replica_sizes,
    request_evacuation,
    restore_disk_state,
)


GIB = 1024**3


def longhorn_node(
    name: str,
    disk_id: str,
    *,
    available_gib: int,
    scheduled_gib: int,
    schedulable: bool = True,
):
    return {
        "metadata": {"name": name},
        "spec": {
            "allowScheduling": True,
            "disks": {
                "default-disk": {
                    "allowScheduling": True,
                    "diskType": "filesystem",
                    "evictionRequested": False,
                    "path": "/var/lib/longhorn",
                    "storageReserved": 0,
                }
            },
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "diskStatus": {
                "default-disk": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {
                            "type": "Schedulable",
                            "status": "True" if schedulable else "False",
                        },
                    ],
                    "diskUUID": disk_id,
                    "storageAvailable": available_gib * GIB,
                    "storageMaximum": 200 * GIB,
                    "storageScheduled": scheduled_gib * GIB,
                }
            },
        },
    }


def fixtures():
    volume_name = "pvc-large"
    return {
        "source_node": "node-1",
        "root_shared_disk_names": {"default-disk"},
        "nodes": [
            longhorn_node(
                "node-1", "disk-node-1", available_gib=23, scheduled_gib=120
            ),
            longhorn_node(
                "node-2", "disk-node-2", available_gib=100, scheduled_gib=50
            ),
            longhorn_node(
                "node-3", "disk-node-3", available_gib=120, scheduled_gib=40
            ),
        ],
        "replicas": [
            {
                "metadata": {"name": f"{volume_name}-r-1"},
                "spec": {
                    "diskID": "disk-node-1",
                    "failedAt": "",
                    "nodeID": "node-1",
                    "volumeName": volume_name,
                    "volumeSize": 50 * GIB,
                },
            },
            {
                "metadata": {"name": f"{volume_name}-r-2"},
                "spec": {
                    "diskID": "disk-node-2",
                    "failedAt": "",
                    "nodeID": "node-2",
                    "volumeName": volume_name,
                    "volumeSize": 50 * GIB,
                },
            },
        ],
        "volumes": [
            {
                "metadata": {"name": volume_name},
                "spec": {
                    "cloneMode": "",
                    "numberOfReplicas": 2,
                    "size": 50 * GIB,
                },
                "status": {
                    "actualSize": 32 * GIB,
                    "robustness": "unknown",
                    "state": "detached",
                },
            }
        ],
        "physical_bytes_by_replica": {f"{volume_name}-r-1": 32 * GIB},
        "root_total_bytes": 183 * GIB,
        "root_available_bytes": 23 * GIB,
        "target_free_percentage": 20,
        "minimum_available_percentage": 25,
        "over_provisioning_percentage": 100,
    }


def evaluate(mutate=None):
    data = copy.deepcopy(fixtures())
    if mutate:
        mutate(data)
    return build_plan(**data)


def assert_rejected(reason: str, mutate) -> None:
    plan, actual = evaluate(mutate)
    if plan is not None or actual != reason:
        raise AssertionError(
            f"expected rejection {reason!r}, got plan={plan!r} reason={actual!r}"
        )


def verify_planner() -> None:
    if disk_path({}, {"diskPath": "/var/lib/longhorn"}) != "/var/lib/longhorn":
        raise AssertionError("planner did not use the Longhorn status disk path")
    physical_replica_sizes(
        fixtures()["nodes"][0],
        fixtures()["replicas"],
        {"default-disk"},
    )
    plan, reason = evaluate()
    if plan is None:
        raise AssertionError(f"expected evacuation plan, got {reason}")
    expected_relief = 183 * GIB * 20 // 100 - 23 * GIB
    if plan.required_relief_bytes != expected_relief:
        raise AssertionError("planner calculated the wrong root relief target")
    if len(plan.candidates) != 1:
        raise AssertionError("planner did not choose one sufficient replica")
    if plan.candidates[0].destination_nodes != ("node-3",):
        raise AssertionError("planner ignored existing replica node anti-affinity")

    def mark_pressured_source_not_ready(data):
        source = data["nodes"][0]
        source["status"]["conditions"][0]["status"] = "False"
        for condition in source["status"]["diskStatus"]["default-disk"][
            "conditions"
        ]:
            condition["status"] = "False"

    plan, reason = evaluate(mark_pressured_source_not_ready)
    if plan is None:
        raise AssertionError(
            "non-ready pressured source disk was not eligible for evacuation: "
            f"{reason}"
        )

    assert_rejected(
        "alternate-schedulable-longhorn-disk-absent",
        lambda data: [
            node["status"]["diskStatus"]["default-disk"]["conditions"][1].update(
                status="False"
            )
            for node in data["nodes"][1:]
        ],
    )
    assert_rejected(
        "safe-evacuation-candidate-absent",
        lambda data: data["volumes"][0]["status"].update(robustness="faulted"),
    )
    assert_rejected(
        "safe-evacuation-candidate-absent",
        lambda data: data["volumes"][0]["spec"].update(cloneMode="linked-clone"),
    )
    assert_rejected(
        "safe-evacuation-candidate-absent",
        lambda data: data["replicas"][0]["spec"].update(
            evictionRequested=True
        ),
    )
    assert_rejected(
        "root-free-target-already-met",
        lambda data: data.update(root_available_bytes=40 * GIB),
    )

    def add_capacity_competitor(data):
        second_volume = "pvc-second"
        data["root_available_bytes"] = 0
        data["replicas"].extend(
            [
                {
                    "metadata": {"name": f"{second_volume}-r-1"},
                    "spec": {
                        "diskID": "disk-node-1",
                        "failedAt": "",
                        "nodeID": "node-1",
                        "volumeName": second_volume,
                        "volumeSize": 50 * GIB,
                    },
                },
                {
                    "metadata": {"name": f"{second_volume}-r-2"},
                    "spec": {
                        "diskID": "disk-node-2",
                        "failedAt": "",
                        "nodeID": "node-2",
                        "volumeName": second_volume,
                        "volumeSize": 50 * GIB,
                    },
                },
            ]
        )
        data["volumes"].append(
            {
                "metadata": {"name": second_volume},
                "spec": {
                    "cloneMode": "",
                    "numberOfReplicas": 2,
                    "size": 50 * GIB,
                },
                "status": {"actualSize": 32 * GIB, "robustness": "healthy"},
            }
        )
        data["physical_bytes_by_replica"][f"{second_volume}-r-1"] = 32 * GIB

    assert_rejected("safe-evacuation-relief-insufficient", add_capacity_competitor)


class RecordingKubectl:
    def __init__(self):
        self.patches = []
        self.replica_patches = []

    def patch_longhorn_node(self, node_name, patch):
        self.patches.append((node_name, patch))

    def patch_longhorn_replica(self, replica_name, eviction_requested):
        self.replica_patches.append((replica_name, eviction_requested))
        return True


def verify_evacuation_state_round_trip() -> None:
    node = fixtures()["nodes"][0]
    source_disk = node["status"]["diskStatus"]["default-disk"]
    candidate_plan, reason = evaluate()
    if candidate_plan is None:
        raise AssertionError(f"fixture candidate is unavailable: {reason}")
    plan = EvacuationPlan(
        source_disks=(
            Disk(
                node_name="node-1",
                disk_name="default-disk",
                disk_id=source_disk["diskUUID"],
                disk_path="/var/lib/longhorn",
                storage_available=source_disk["storageAvailable"],
                usable_capacity=0,
            ),
        ),
        candidates=candidate_plan.candidates,
        required_relief_bytes=1,
        estimated_relief_bytes=1,
    )
    state = original_disk_state("node-1", node, plan)
    recorder = RecordingKubectl()
    request_evacuation(recorder, "node-1", state)
    restore_disk_state(recorder, "node-1", state)
    request_patch = recorder.patches[0][1]
    restore_patch = recorder.patches[1][1]
    if ANNOTATION not in request_patch["metadata"]["annotations"]:
        raise AssertionError("evacuation request did not persist recovery state")
    requested_disk = request_patch["spec"]["disks"]["default-disk"]
    if requested_disk != {"allowScheduling": False, "evictionRequested": True}:
        raise AssertionError("evacuation request used an unsafe disk state")
    if restore_patch["metadata"]["annotations"][ANNOTATION] is not None:
        raise AssertionError("evacuation restore did not remove recovery state")
    if restore_patch["spec"]["disks"] != state["disks"]:
        raise AssertionError("evacuation restore did not preserve original disk state")
    if recorder.replica_patches != [
        ("pvc-large-r-1", True),
        ("pvc-large-r-1", False),
    ]:
        raise AssertionError("selected replica eviction was not safely round-tripped")

    removed_disk_node = copy.deepcopy(node)
    removed_disk_node["spec"]["disks"] = {}
    recorder = RecordingKubectl()
    restore_disk_state(recorder, "node-1", state, removed_disk_node)
    if "spec" in recorder.patches[0][1]:
        raise AssertionError("restore recreated a disk removed by an operator")


def verify_repository_integration() -> None:
    playbook = (ROOT / "ansible/playbooks/cleanup-node-storage.yml").read_text(
        encoding="utf-8"
    )
    required = (
        "relieve_longhorn_disk_pressure.py",
        "Evacuate Longhorn replicas from pressured root-backed disks",
        "Inspect resumable Longhorn root-pressure evacuation state",
        "storage-minimal-available-percentage=25",
        "longhorn_pressure_evacuation=completed",
    )
    missing = [fragment for fragment in required if fragment not in playbook]
    if missing:
        raise AssertionError(
            "node storage cleanup is missing pressure relief integration: "
            + ", ".join(missing)
        )
    if "platform_node_storage_longhorn_pressure_state_before.rc" not in playbook:
        raise AssertionError(
            "pressure helper cannot resume saved state after pressure clears"
        )
    if "retries: 3" not in playbook or "in [0, 42]" not in playbook:
        raise AssertionError(
            "pressure-state inspection must retry transient node privilege failures"
        )


def main() -> int:
    verify_planner()
    verify_evacuation_state_round_trip()
    verify_repository_integration()
    print("Longhorn disk pressure relief self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
