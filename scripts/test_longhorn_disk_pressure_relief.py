#!/usr/bin/env python3
"""Self-test restoration of unsupported legacy Longhorn pressure state."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from relieve_longhorn_disk_pressure import (  # noqa: E402
    ANNOTATION,
    annotation_state,
    run,
)


def legacy_state():
    return {
        "version": 1,
        "node": "node-1",
        "disks": {
            "default-disk": {
                "allowScheduling": True,
                "evictionRequested": False,
            }
        },
        "replicas": {"pvc-example-r-1": False},
    }


def longhorn_node(with_state: bool = True):
    annotations = {}
    if with_state:
        annotations[ANNOTATION] = json.dumps(legacy_state())
    return {
        "metadata": {"name": "node-1", "annotations": annotations},
        "spec": {
            "disks": {
                "default-disk": {
                    "allowScheduling": False,
                    "evictionRequested": False,
                }
            }
        },
    }


class RecordingKubectl:
    def __init__(self, pressure: str, with_state: bool = True):
        self.pressure = pressure
        self.node = longhorn_node(with_state)
        self.node_patches = []
        self.replica_patches = []

    def get_json(self, *args):
        if args == ("get", "node/node-1"):
            return {
                "status": {
                    "conditions": [
                        {"type": "DiskPressure", "status": self.pressure}
                    ]
                }
            }
        raise AssertionError(f"unexpected get_json call: {args}")

    def get_optional_json(self, *args):
        return self.node

    def patch_longhorn_node(self, node_name, patch):
        self.node_patches.append((node_name, patch))

    def patch_longhorn_replica(self, replica_name, eviction_requested):
        self.replica_patches.append((replica_name, eviction_requested))
        return True


def args():
    return argparse.Namespace(
        node="node-1",
        kubectl="kubectl",
        kubeconfig="kubeconfig",
        timeout=1200,
        poll_interval=10,
        target_free_percentage=20,
    )


def execute(kube: RecordingKubectl) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = run(args(), kube)
    if result != 0:
        raise AssertionError(f"legacy state restoration returned {result}")
    return output.getvalue()


def verify_legacy_state_restored_during_pressure() -> None:
    kube = RecordingKubectl("True")
    output = execute(kube)
    if "longhorn_pressure_evacuation=legacy-state-restored" not in output:
        raise AssertionError("legacy pressure state was not restored")
    if "longhorn-node-not-ready-during-kubernetes-disk-pressure" not in output:
        raise AssertionError("pressure-time Longhorn eviction was not deferred")
    if kube.replica_patches != [("pvc-example-r-1", False)]:
        raise AssertionError("legacy replica state was not restored exactly")
    if any(value is True for _, value in kube.replica_patches):
        raise AssertionError("helper started unsupported direct replica eviction")
    patch = kube.node_patches[0][1]
    if patch["metadata"]["annotations"][ANNOTATION] is not None:
        raise AssertionError("legacy recovery annotation was retained")
    if patch["spec"]["disks"] != legacy_state()["disks"]:
        raise AssertionError("original disk state was not restored")


def verify_no_new_evacuation_state() -> None:
    kube = RecordingKubectl("True", with_state=False)
    output = execute(kube)
    if "longhorn-node-not-ready-during-kubernetes-disk-pressure" not in output:
        raise AssertionError("active pressure did not fail closed")
    if kube.node_patches or kube.replica_patches:
        raise AssertionError("helper created a new pressure eviction request")


def verify_clear_pressure_restores_state() -> None:
    kube = RecordingKubectl("False")
    output = execute(kube)
    if "longhorn_pressure_evacuation=completed" not in output:
        raise AssertionError("clear pressure did not complete state restoration")
    if kube.replica_patches != [("pvc-example-r-1", False)]:
        raise AssertionError("clear-pressure restore changed replica intent")


def verify_annotation_validation() -> None:
    node = longhorn_node()
    if annotation_state(node, "node-1") != legacy_state():
        raise AssertionError("valid legacy state did not round-trip")
    node["metadata"]["annotations"][ANNOTATION] = '{"version":2}'
    try:
        annotation_state(node, "node-1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("unsupported legacy state version was accepted")
    node = longhorn_node()
    unsafe = legacy_state()
    unsafe["replicas"]["pvc-example-r-1"] = True
    node["metadata"]["annotations"][ANNOTATION] = json.dumps(unsafe)
    try:
        annotation_state(node, "node-1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("legacy state could restore an active eviction request")


def verify_repository_integration() -> None:
    playbook = (ROOT / "ansible/playbooks/cleanup-node-storage.yml").read_text(
        encoding="utf-8"
    )
    required = (
        "Restore unsupported legacy Longhorn pressure eviction state",
        "Reclaim one proven stale Longhorn replica directory",
        "reclaim_stale_longhorn_replica_data.py",
        "longhorn_stale_replica_reclaim=completed",
        "Inspect legacy Longhorn root-pressure recovery state",
    )
    missing = [fragment for fragment in required if fragment not in playbook]
    if missing:
        raise AssertionError(
            "node storage cleanup is missing pressure recovery integration: "
            + ", ".join(missing)
        )
    if "retries: 3" not in playbook or "in [0, 42]" not in playbook:
        raise AssertionError(
            "legacy pressure-state inspection must retry transient failures"
        )


def main() -> int:
    verify_legacy_state_restored_during_pressure()
    verify_no_new_evacuation_state()
    verify_clear_pressure_restores_state()
    verify_annotation_validation()
    verify_repository_integration()
    print("Longhorn disk pressure relief self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
