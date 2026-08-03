#!/usr/bin/env python3
"""Self-test the production data-protection and restore-evidence gates."""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_platform_data_protection as protection  # noqa: E402
import run_forgejo_recovery_drill as forgejo_drill  # noqa: E402
import verify_forgejo_recovery_evidence as forgejo_evidence  # noqa: E402
import verify_restore_evidence as evidence  # noqa: E402


def fail(message: str) -> None:
    raise AssertionError(message)


def proof(name: str, verified_at: datetime) -> dict[str, object]:
    return {
        "status": "passed",
        "evidence": {
            "uri": f"ticket://test/{name}",
            "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
            "verifiedAt": verified_at.isoformat(),
        },
    }


def valid_evidence(now: datetime) -> dict[str, object]:
    recovery_started_at = now - timedelta(hours=2)
    completed_at = now - timedelta(hours=1)
    verified_at = now - timedelta(minutes=90)
    return {
        "schemaVersion": 2,
        "drillId": "drill-test",
        "backupCompletedAt": (now - timedelta(hours=4)).isoformat(),
        "recoveryStartedAt": recovery_started_at.isoformat(),
        "completedAt": completed_at.isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "sourceCommit": "a" * 40,
        "result": "passed",
        "rpoHours": 24,
        "rtoMinutes": 240,
        "elapsedMinutes": 60,
        "recoveryTarget": {
            "type": "isolated-cluster",
            "identifier": "recovery-test",
            "isProduction": False,
            "failureDomainSeparated": True,
        },
        "checks": {
            name: proof(name, verified_at)
            for name in evidence.REQUIRED_CHECKS
        },
        "continuity": {
            "failover": {
                **proof("failover", verified_at),
                "dnsVipTlsValidated": True,
                "dataConsistencyValidated": True,
            },
            "failback": {
                **proof("failback", verified_at),
                "currentBackupValidated": True,
                "dataReconciled": True,
            },
        },
    }


def recovery_state(
    *, pod_name: str, pod_uid: str, pod_ip: str, node: str
) -> dict[str, object]:
    return {
        "deploymentUid": "deployment-uid",
        "deploymentGeneration": 7,
        "argocdApplicationUid": "argocd-application-uid",
        "argocdRevision": "a" * 40,
        "podName": pod_name,
        "podUid": pod_uid,
        "podIP": pod_ip,
        "node": node,
        "imageIDs": ["registry.example.test/forgejo@sha256:" + "a" * 64],
        "serviceUid": "service-uid",
        "serviceClusterIP": "198.51.100.20",
        "servicePort": 3000,
        "endpointAddresses": [pod_ip],
        "httpCode": 200,
        "storageClaims": [
            {
                "name": "forgejo-data",
                "uid": "claim-uid",
                "volumeName": "volume-uid",
                "storageClass": "longhorn-critical-encrypted",
                "csiDriver": "driver.longhorn.io",
                "encrypted": True,
                "encryptionSecretName": "longhorn-crypto",
                "encryptionSecretRefs": {
                    "nodePublishSecretRef": "longhorn-system/longhorn-crypto",
                    "nodeStageSecretRef": "longhorn-system/longhorn-crypto",
                    "nodeExpandSecretRef": "longhorn-system/longhorn-crypto",
                },
                "longhornState": "attached",
                "longhornRobustness": "healthy",
            }
        ],
    }


def valid_forgejo_evidence(now: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "drillId": "forgejo-test",
        "completedAt": (now - timedelta(hours=2)).isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "sourceCommit": "a" * 40,
        "result": "passed",
        "rtoSeconds": 300,
        "elapsedSeconds": 75.5,
        "namespace": "forgejo",
        "deployment": "forgejo",
        "service": "forgejo-http",
        "recoveryMode": "node-failover",
        "sourceNode": "node-a",
        "targetNode": "node-b",
        "eligibleRecoveryNodes": ["node-b", "node-c"],
        "sourceNodeWasSchedulable": True,
        "sourceNodeRestoredSchedulable": True,
        "preRecovery": recovery_state(
            pod_name="forgejo-old",
            pod_uid="pod-old",
            pod_ip="192.0.2.10",
            node="node-a",
        ),
        "postRecovery": recovery_state(
            pod_name="forgejo-new",
            pod_uid="pod-new",
            pod_ip="192.0.2.11",
            node="node-b",
        ),
    }


def test_restore_evidence() -> None:
    now = datetime.now(timezone.utc)
    document = valid_evidence(now)
    summary = evidence.validate_evidence(
        document,
        now=now,
        max_age_days=92,
        expected_profile="premium-3node",
        expected_commit="a" * 40,
    )
    if summary["drill_id"] != "drill-test":
        fail("valid restore evidence did not preserve the drill id")

    try:
        evidence.validate_evidence(
            document,
            now=now,
            max_age_days=92,
            expected_commit="b" * 40,
        )
    except evidence.EvidenceError:
        pass
    else:
        fail("restore evidence from a different Git revision was accepted")

    stale = valid_evidence(now)
    stale["completedAt"] = (now - timedelta(days=93)).isoformat()
    try:
        evidence.validate_evidence(stale, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("stale restore evidence was accepted")

    incomplete = valid_evidence(now)
    del incomplete["checks"]["harbor"]  # type: ignore[index]
    try:
        evidence.validate_evidence(incomplete, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("incomplete restore evidence was accepted")

    missed_rpo = valid_evidence(now)
    missed_rpo["backupCompletedAt"] = (now - timedelta(hours=30)).isoformat()
    try:
        evidence.validate_evidence(missed_rpo, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("restore evidence exceeding its measured RPO was accepted")

    unsafe_target = valid_evidence(now)
    unsafe_target["recoveryTarget"]["failureDomainSeparated"] = False  # type: ignore[index]
    try:
        evidence.validate_evidence(unsafe_target, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("same-failure-domain restore evidence was accepted")

    incomplete_failback = valid_evidence(now)
    incomplete_failback["continuity"]["failback"]["dataReconciled"] = False  # type: ignore[index]
    try:
        evidence.validate_evidence(incomplete_failback, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("restore evidence without reconciled failback was accepted")


def test_forgejo_recovery_evidence() -> None:
    now = datetime.now(timezone.utc)
    document = valid_forgejo_evidence(now)
    summary = forgejo_evidence.validate_evidence(
        document,
        now=now,
        max_age_days=92,
        expected_profile="premium-3node",
        expected_commit="a" * 40,
    )
    if summary["drill_id"] != "forgejo-test":
        fail("valid Forgejo recovery evidence did not preserve the drill id")

    same_pod = valid_forgejo_evidence(now)
    same_pod["postRecovery"]["podUid"] = "pod-old"  # type: ignore[index]
    try:
        forgejo_evidence.validate_evidence(same_pod, now=now, max_age_days=92)
    except forgejo_evidence.EvidenceError:
        pass
    else:
        fail("Forgejo recovery evidence accepted the original pod UID")

    same_node = valid_forgejo_evidence(now)
    same_node["targetNode"] = "node-a"
    same_node["postRecovery"]["node"] = "node-a"  # type: ignore[index]
    try:
        forgejo_evidence.validate_evidence(same_node, now=now, max_age_days=92)
    except forgejo_evidence.EvidenceError:
        pass
    else:
        fail("Forgejo recovery evidence accepted same-node recovery")

    changed_volume = valid_forgejo_evidence(now)
    changed_volume["postRecovery"]["storageClaims"][0]["volumeName"] = "other-volume"  # type: ignore[index]
    try:
        forgejo_evidence.validate_evidence(changed_volume, now=now, max_age_days=92)
    except forgejo_evidence.EvidenceError:
        pass
    else:
        fail("Forgejo recovery evidence accepted a changed PV identity")

    unencrypted = valid_forgejo_evidence(now)
    unencrypted["postRecovery"]["storageClaims"][0]["encrypted"] = False  # type: ignore[index]
    try:
        forgejo_evidence.validate_evidence(unencrypted, now=now, max_age_days=92)
    except forgejo_evidence.EvidenceError:
        pass
    else:
        fail("Forgejo recovery evidence accepted an unencrypted persistent volume")

    cleanup_failed = valid_forgejo_evidence(now)
    cleanup_failed["sourceNodeRestoredSchedulable"] = False
    try:
        forgejo_evidence.validate_evidence(cleanup_failed, now=now, max_age_days=92)
    except forgejo_evidence.EvidenceError:
        pass
    else:
        fail("Forgejo recovery evidence accepted a cordoned source node")

    stale = valid_forgejo_evidence(now)
    stale["completedAt"] = (now - timedelta(days=93)).isoformat()
    try:
        forgejo_evidence.validate_evidence(stale, now=now, max_age_days=92)
    except forgejo_evidence.EvidenceError:
        pass
    else:
        fail("stale Forgejo recovery evidence was accepted")

    if forgejo_drill.service_port({"spec": {"ports": [{"name": "http", "port": 3000}]}}) != 3000:
        fail("Forgejo drill did not select the named HTTP service port")
    addresses = forgejo_drill.ready_endpoint_addresses(
        {
            "items": [
                {
                    "endpoints": [
                        {"addresses": ["192.0.2.10"], "conditions": {"ready": True}},
                        {"addresses": ["192.0.2.11"], "conditions": {"ready": False}},
                    ]
                }
            ]
        }
    )
    if addresses != {"192.0.2.10"}:
        fail("Forgejo drill accepted an unready endpoint")
    if not forgejo_drill.node_is_schedulable(
        {
            "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
    ):
        fail("Forgejo drill rejected a Ready schedulable node")
    if forgejo_drill.node_is_schedulable(
        {
            "spec": {"unschedulable": True},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
    ):
        fail("Forgejo drill accepted a cordoned node")


def test_endpoint_classification() -> None:
    for endpoint in (
        "http://minio.object-storage.svc.cluster.local:9000",
        "https://127.0.0.1:9000",
        "object-storage.svc",
    ):
        if not protection.is_cluster_local_endpoint(endpoint):
            fail(f"cluster-local endpoint was accepted: {endpoint}")
    for endpoint in (
        "https://s3.amazonaws.com",
        "https://backup.example.test:9443",
        "s3://platform-longhorn@us-east-1/",
    ):
        if protection.is_cluster_local_endpoint(endpoint):
            fail(f"external endpoint was rejected: {endpoint}")


def test_contract_wiring() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    wrapper = ROOT / "scripts" / "bootstrap" / "run-platform-data-protection.sh"
    playbook = ROOT / "ansible" / "playbooks" / "verify-platform-data-protection.yml"
    recovery_wrapper = ROOT / "scripts" / "bootstrap" / "run-forgejo-recovery-drill.sh"
    recovery_playbook = ROOT / "ansible" / "playbooks" / "run-forgejo-recovery-drill.yml"
    recovery_runner = ROOT / "scripts" / "run_forgejo_recovery_drill.py"
    recovery_validator = ROOT / "scripts" / "verify_forgejo_recovery_evidence.py"
    if "platform-data-protection:" not in makefile:
        fail("Makefile is missing platform-data-protection")
    if "run-platform-data-protection.sh" not in makefile:
        fail("production gate does not invoke the data-protection wrapper")
    if not wrapper.is_file() or not playbook.is_file():
        fail("data-protection wrapper or Ansible playbook is missing")
    if not all(
        path.is_file()
        for path in (
            recovery_wrapper,
            recovery_playbook,
            recovery_runner,
            recovery_validator,
        )
    ):
        fail("Forgejo recovery wrapper, playbook, runner, or validator is missing")
    if "platform-forgejo-recovery-drill:" not in makefile:
        fail("Makefile is missing platform-forgejo-recovery-drill")
    for path in (recovery_wrapper, recovery_playbook, recovery_runner):
        if "FAILOVER_FORGEJO_SINGLETON" not in path.read_text(encoding="utf-8"):
            fail(f"{path.relative_to(ROOT)} does not require explicit node failover approval")
    runner_text = recovery_runner.read_text(encoding="utf-8")
    for needle in (
        'kube.run("cordon", source_node)',
        'kube.run("uncordon", source_node)',
        '"schemaVersion": 2',
        '"recoveryMode": "node-failover"',
        '"encryptionSecretRefs"',
        '"sourceNodeRestoredSchedulable"',
    ):
        if needle not in runner_text:
            fail(f"Forgejo recovery runner is missing required failover proof: {needle}")
    validator_text = recovery_validator.read_text(encoding="utf-8")
    for needle in (
        "schemaVersion must be 2",
        "Forgejo must recover on a different node",
        "sourceNodeRestoredSchedulable",
        "encryptionSecretRefs",
    ):
        if needle not in validator_text:
            fail(f"Forgejo recovery validator is missing required proof: {needle}")
    wrapper_text = wrapper.read_text(encoding="utf-8")
    if "PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE" not in wrapper_text:
        fail("production data protection does not require Forgejo recovery evidence")
    if '--expected-commit "${expected_commit}"' not in wrapper_text:
        fail("production data protection does not bind restore evidence to HEAD")
    with tempfile.TemporaryDirectory(prefix="platform-restore-evidence-") as directory:
        path = Path(directory) / "evidence.json"
        path.write_text(json.dumps(valid_evidence(datetime.now(timezone.utc))), encoding="utf-8")
        if not json.loads(path.read_text(encoding="utf-8")):
            fail("restore evidence fixture was not written")


def main() -> int:
    test_restore_evidence()
    test_forgejo_recovery_evidence()
    test_endpoint_classification()
    test_contract_wiring()
    print("Production data-protection contract self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
