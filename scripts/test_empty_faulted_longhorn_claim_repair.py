#!/usr/bin/env python3
"""Self-test guarded empty/faulted Longhorn StatefulSet claim selection."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repair_empty_faulted_longhorn_claims import evaluate_candidate  # noqa: E402


def fixtures(ordinal: int = 1):
    volume_name = "pvc-safe-empty"
    namespace = "logging"
    statefulset_name = "loki-backend"
    pod_name = f"{statefulset_name}-{ordinal}"
    pvc_name = f"data-{pod_name}"
    pvc_uid = "claim-uid"
    owner = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "name": statefulset_name,
        "uid": "statefulset-uid",
        "controller": True,
    }
    volume = {
        "metadata": {"name": volume_name},
        "spec": {},
        "status": {
            "actualSize": 0,
            "state": "detached",
            "robustness": "faulted",
            "lastBackup": "",
            "lastBackupAt": "",
            "kubernetesStatus": {
                "namespace": namespace,
                "pvcName": pvc_name,
                "pvName": volume_name,
            },
        },
    }
    pvc = {
        "metadata": {
            "name": pvc_name,
            "namespace": namespace,
            "uid": pvc_uid,
            "ownerReferences": [owner],
        },
        "spec": {"volumeName": volume_name},
        "status": {"phase": "Bound"},
    }
    pv = {
        "metadata": {"name": volume_name},
        "spec": {
            "claimRef": {"uid": pvc_uid, "namespace": namespace, "name": pvc_name},
            "csi": {"driver": "driver.longhorn.io", "volumeHandle": volume_name},
            "persistentVolumeReclaimPolicy": "Retain",
        },
    }
    statefulset = {
        "metadata": {
            "name": statefulset_name,
            "namespace": namespace,
            "uid": "statefulset-uid",
            "annotations": {
                "argocd.argoproj.io/tracking-id": (
                    f"loki:apps/StatefulSet:{namespace}/{statefulset_name}"
                )
            },
        },
        "spec": {
            "replicas": 3,
            "persistentVolumeClaimRetentionPolicy": {
                "whenDeleted": "Retain",
                "whenScaled": "Retain",
            },
            "volumeClaimTemplates": [{"metadata": {"name": "data"}}],
        },
    }
    pod = {
        "metadata": {"name": pod_name, "namespace": namespace, "ownerReferences": [owner]},
        "spec": {
            "containers": [{"name": "loki"}],
            "volumes": [{"persistentVolumeClaim": {"claimName": pvc_name}}],
        },
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {
                    "name": "loki",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "ContainerCreating"}},
                }
            ],
        },
    }
    peers = []
    for peer_ordinal in (0, 2):
        peer = copy.deepcopy(pod)
        peer["metadata"]["name"] = f"{statefulset_name}-{peer_ordinal}"
        peer["status"] = {
            "phase": "Running",
            "containerStatuses": [
                {"name": "loki", "ready": True, "restartCount": 0, "state": {"running": {}}}
            ],
        }
        peers.append(peer)
    return volume, pvc, pv, statefulset, pod, peers


def evaluate(*, ordinal: int = 1, mutate=None, backups=None, peers=None):
    volume, pvc, pv, statefulset, pod, fixture_peers = fixtures(ordinal)
    if mutate:
        mutate(volume, pvc, pv, statefulset, pod)
    return evaluate_candidate(
        volume=volume,
        replicas=[],
        snapshots=[],
        backups=backups or [],
        pvc=pvc,
        pv=pv,
        statefulset=statefulset,
        pod=pod,
        peer_pods=fixture_peers if peers is None else peers,
    )


def main() -> int:
    candidate, reason = evaluate()
    assert candidate is not None, reason
    assert candidate.ordinal == 1
    assert reason == "safe-empty-faulted-statefulset-claim"

    candidate, reason = evaluate(mutate=lambda volume, *_: volume["status"].update(actualSize=1))
    assert candidate is None and reason == "volume-has-data"

    candidate, reason = evaluate(backups=[{"spec": {"volumeName": "pvc-safe-empty"}}])
    assert candidate is None and reason == "volume-has-backups"

    def mark_started(_volume, _pvc, _pv, _sts, pod):
        pod["status"]["containerStatuses"][0]["restartCount"] = 1

    candidate, reason = evaluate(mutate=mark_started)
    assert candidate is None and reason == "pod-has-started"

    candidate, reason = evaluate(ordinal=0)
    assert candidate is None and reason == "ordinal-zero-not-automatically-recycled"

    candidate, reason = evaluate(peers=[])
    assert candidate is None and reason == "fewer-than-two-ready-peers"

    def enable_claim_auto_delete(_volume, _pvc, _pv, statefulset, _pod):
        statefulset["spec"]["persistentVolumeClaimRetentionPolicy"] = {
            "whenDeleted": "Delete",
            "whenScaled": "Delete",
        }

    candidate, reason = evaluate(mutate=enable_claim_auto_delete)
    assert candidate is None and reason == "statefulset-pvc-retention-not-retain"

    def break_claim_identity(_volume, _pvc, pv, _sts, _pod):
        pv["spec"]["claimRef"]["uid"] = "different-uid"

    candidate, reason = evaluate(mutate=break_claim_identity)
    assert candidate is None and reason == "pv-contract-mismatch"

    print("Empty faulted Longhorn StatefulSet claim repair self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
