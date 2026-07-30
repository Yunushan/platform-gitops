#!/usr/bin/env python3
"""Recycle provably empty, faulted Longhorn claims owned by StatefulSets."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from subprocess_timeout import bounded_timeout_seconds


JsonObject = dict[str, Any]
KUBECTL_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Candidate:
    volume_name: str
    namespace: str
    pvc_name: str
    pvc_uid: str
    pv_name: str
    statefulset_name: str
    statefulset_uid: str
    pod_name: str
    ordinal: int
    desired_replicas: int
    app_name: str


def controller_owner(obj: JsonObject, kind: str) -> JsonObject | None:
    return next(
        (
            owner
            for owner in obj.get("metadata", {}).get("ownerReferences", [])
            if owner.get("controller") is True and owner.get("kind") == kind
        ),
        None,
    )


def pod_uses_claim(pod: JsonObject, claim_name: str) -> bool:
    return any(
        volume.get("persistentVolumeClaim", {}).get("claimName") == claim_name
        for volume in pod.get("spec", {}).get("volumes", [])
    )


def pod_is_ready(pod: JsonObject) -> bool:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    expected = len(pod.get("spec", {}).get("containers", []))
    return (
        pod.get("status", {}).get("phase") == "Running"
        and expected > 0
        and len(statuses) == expected
        and all(status.get("ready") is True for status in statuses)
    )


def pod_has_never_started(pod: JsonObject) -> bool:
    statuses = (
        pod.get("status", {}).get("initContainerStatuses", [])
        + pod.get("status", {}).get("containerStatuses", [])
    )
    return all(
        int(status.get("restartCount") or 0) == 0
        and not status.get("containerID")
        and not status.get("state", {}).get("running")
        and not status.get("state", {}).get("terminated")
        for status in statuses
    )


def references_volume(obj: JsonObject, volume_name: str) -> bool:
    metadata = obj.get("metadata", {})
    spec = obj.get("spec", {})
    status = obj.get("status", {})
    labels = metadata.get("labels", {})
    return volume_name in {
        spec.get("volume"),
        spec.get("volumeName"),
        status.get("volume"),
        status.get("volumeName"),
        labels.get("longhornvolume"),
        labels.get("longhorn.io/volume"),
    }


def statefulset_retains_claims(statefulset: JsonObject) -> bool:
    policy = statefulset.get("spec", {}).get("persistentVolumeClaimRetentionPolicy", {})
    return policy.get("whenDeleted") == "Retain" and policy.get("whenScaled") == "Retain"


def evaluate_candidate(
    *,
    volume: JsonObject,
    replicas: list[JsonObject],
    snapshots: list[JsonObject],
    backups: list[JsonObject],
    pvc: JsonObject,
    pv: JsonObject,
    statefulset: JsonObject,
    pod: JsonObject,
    peer_pods: list[JsonObject],
) -> tuple[Candidate | None, str]:
    metadata = volume.get("metadata", {})
    status = volume.get("status", {})
    spec = volume.get("spec", {})
    volume_name = metadata.get("name", "")
    try:
        actual_size = int(status.get("actualSize") or 0)
    except (TypeError, ValueError):
        return None, "invalid-actual-size"
    if metadata.get("deletionTimestamp"):
        return None, "volume-deleting"
    if actual_size != 0:
        return None, "volume-has-data"
    if status.get("state") != "detached" or status.get("robustness") != "faulted":
        return None, "volume-not-detached-faulted"
    if spec.get("fromBackup") or status.get("lastBackup") or status.get("lastBackupAt"):
        return None, "volume-has-backup-history"
    if any(replica.get("spec", {}).get("volumeName") == volume_name for replica in replicas):
        return None, "volume-has-replicas"
    if any(references_volume(snapshot, volume_name) for snapshot in snapshots):
        return None, "volume-has-snapshots"
    if any(references_volume(backup, volume_name) for backup in backups):
        return None, "volume-has-backups"

    kubernetes_status = status.get("kubernetesStatus", {})
    namespace = kubernetes_status.get("namespace", "")
    pvc_name = kubernetes_status.get("pvcName", "")
    pv_name = kubernetes_status.get("pvName", "")
    pvc_metadata = pvc.get("metadata", {})
    pvc_spec = pvc.get("spec", {})
    pvc_uid = pvc_metadata.get("uid", "")
    if not namespace or not pvc_name or not pv_name or not pvc_uid:
        return None, "missing-kubernetes-identity"
    if pvc_metadata.get("name") != pvc_name or pvc_metadata.get("namespace") != namespace:
        return None, "pvc-identity-mismatch"
    if pvc_metadata.get("deletionTimestamp") or pvc.get("status", {}).get("phase") != "Bound":
        return None, "pvc-not-stable-bound"
    if pvc_spec.get("volumeName") != pv_name:
        return None, "pvc-pv-mismatch"

    pv_spec = pv.get("spec", {})
    claim_ref = pv_spec.get("claimRef", {})
    csi = pv_spec.get("csi", {})
    if not (
        pv.get("metadata", {}).get("name") == pv_name
        and claim_ref.get("uid") == pvc_uid
        and claim_ref.get("namespace") == namespace
        and claim_ref.get("name") == pvc_name
        and csi.get("driver") == "driver.longhorn.io"
        and csi.get("volumeHandle") == volume_name
        and pv_spec.get("persistentVolumeReclaimPolicy") in {"Retain", "Delete"}
    ):
        return None, "pv-contract-mismatch"

    pvc_owner = controller_owner(pvc, "StatefulSet")
    sts_metadata = statefulset.get("metadata", {})
    sts_name = sts_metadata.get("name", "")
    sts_uid = sts_metadata.get("uid", "")
    if not pvc_owner or pvc_owner.get("name") != sts_name or pvc_owner.get("uid") != sts_uid:
        return None, "pvc-not-statefulset-owned"
    if not statefulset_retains_claims(statefulset):
        return None, "statefulset-pvc-retention-not-retain"

    pod_owner = controller_owner(pod, "StatefulSet")
    pod_name = pod.get("metadata", {}).get("name", "")
    match = re.fullmatch(rf"{re.escape(sts_name)}-(\d+)", pod_name)
    if not pod_owner or pod_owner.get("name") != sts_name or pod_owner.get("uid") != sts_uid:
        return None, "pod-not-statefulset-owned"
    if not match:
        return None, "pod-ordinal-unrecognized"
    ordinal = int(match.group(1))
    if ordinal <= 0:
        return None, "ordinal-zero-not-automatically-recycled"

    try:
        desired_replicas = int(statefulset.get("spec", {}).get("replicas") or 0)
    except (TypeError, ValueError):
        return None, "invalid-statefulset-replicas"
    if desired_replicas <= ordinal:
        return None, "ordinal-outside-desired-replicas"
    template_names = {
        template.get("metadata", {}).get("name", "")
        for template in statefulset.get("spec", {}).get("volumeClaimTemplates", [])
    }
    if not any(f"{template_name}-{pod_name}" == pvc_name for template_name in template_names):
        return None, "pvc-name-not-from-statefulset-template"
    if not pod_uses_claim(pod, pvc_name):
        return None, "pod-does-not-use-pvc"
    if pod_is_ready(pod) or not pod_has_never_started(pod):
        return None, "pod-has-started"

    ready_peers = [peer for peer in peer_pods if pod_is_ready(peer)]
    if len(ready_peers) < 2:
        return None, "fewer-than-two-ready-peers"

    tracking_id = statefulset.get("metadata", {}).get("annotations", {}).get(
        "argocd.argoproj.io/tracking-id", ""
    )
    app_name = tracking_id.split(":", 1)[0] if ":" in tracking_id else ""
    if not app_name:
        return None, "statefulset-not-argocd-managed"

    return (
        Candidate(
            volume_name=volume_name,
            namespace=namespace,
            pvc_name=pvc_name,
            pvc_uid=pvc_uid,
            pv_name=pv_name,
            statefulset_name=sts_name,
            statefulset_uid=sts_uid,
            pod_name=pod_name,
            ordinal=ordinal,
            desired_replicas=desired_replicas,
            app_name=app_name,
        ),
        "safe-empty-faulted-statefulset-claim",
    )


class Kubectl:
    def __init__(self, binary: str, kubeconfig: str) -> None:
        self.command = [binary, "--kubeconfig", kubeconfig]

    def execute(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            timeout = bounded_timeout_seconds(
                KUBECTL_TIMEOUT_SECONDS,
                "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        try:
            return subprocess.run(
                self.command + list(args),
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"kubectl timed out after {timeout:g} seconds: {' '.join(args)}"
            ) from None

    def run(self, *args: str, check: bool = True) -> str:
        result = self.execute(*args)
        if check and result.returncode != 0:
            sys.stderr.write((result.stderr or "") + (result.stdout or ""))
            raise RuntimeError(f"kubectl failed with rc={result.returncode}: {' '.join(args)}")
        return result.stdout

    def get(self, *args: str) -> JsonObject:
        return json.loads(self.run(*args, "-o", "json"))

    def get_optional(self, *args: str) -> JsonObject | None:
        result = self.execute(*args, "-o", "json")
        if result.returncode == 0:
            return json.loads(result.stdout)
        if "NotFound" in (result.stderr or ""):
            return None
        sys.stderr.write((result.stderr or "") + (result.stdout or ""))
        raise RuntimeError(f"kubectl lookup failed with rc={result.returncode}: {' '.join(args)}")

    def items(self, *args: str) -> list[JsonObject]:
        return self.get(*args).get("items", [])


def wait_until(predicate, timeout: int, interval: int = 3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def objects_referencing_volume(objects: list[JsonObject], volume_name: str) -> list[JsonObject]:
    return [obj for obj in objects if references_volume(obj, volume_name)]


def discover_candidates(kube: Kubectl) -> list[Candidate]:
    volumes = kube.items("-n", "longhorn-system", "get", "volumes.longhorn.io")
    replicas = kube.items("-n", "longhorn-system", "get", "replicas.longhorn.io")
    snapshots = kube.items("-n", "longhorn-system", "get", "snapshots.longhorn.io")
    backups = kube.items("-n", "longhorn-system", "get", "backups.longhorn.io")
    candidates: list[Candidate] = []

    for volume in volumes:
        status = volume.get("status", {})
        try:
            actual_size = int(status.get("actualSize") or 0)
        except (TypeError, ValueError):
            continue
        if not (
            actual_size == 0
            and status.get("state") == "detached"
            and status.get("robustness") == "faulted"
        ):
            continue
        volume_name = volume.get("metadata", {}).get("name", "")
        kubernetes_status = status.get("kubernetesStatus", {})
        namespace = kubernetes_status.get("namespace", "")
        pvc_name = kubernetes_status.get("pvcName", "")
        pv_name = kubernetes_status.get("pvName", "")
        if not namespace or not pvc_name or not pv_name:
            print(f"longhorn_volume={volume_name} action=retain reason=missing-kubernetes-identity")
            continue
        pvc = kube.get_optional("-n", namespace, "get", f"pvc/{pvc_name}")
        pv = kube.get_optional("get", f"pv/{pv_name}")
        if not pvc or not pv:
            print(f"longhorn_volume={volume_name} action=retain reason=pvc-or-pv-absent")
            continue
        owner = controller_owner(pvc, "StatefulSet")
        sts_name = owner.get("name", "") if owner else ""
        statefulset = (
            kube.get_optional("-n", namespace, "get", f"statefulset/{sts_name}")
            if sts_name
            else None
        )
        if not statefulset:
            print(f"longhorn_volume={volume_name} action=retain reason=statefulset-absent")
            continue
        pods = kube.items("-n", namespace, "get", "pods")
        sts_uid = statefulset.get("metadata", {}).get("uid", "")
        owned_pods = [
            pod
            for pod in pods
            if (controller_owner(pod, "StatefulSet") or {}).get("uid") == sts_uid
        ]
        pod = next((item for item in owned_pods if pod_uses_claim(item, pvc_name)), None)
        if not pod:
            print(f"longhorn_volume={volume_name} action=retain reason=consumer-pod-absent")
            continue
        peers = [item for item in owned_pods if item is not pod]
        candidate, reason = evaluate_candidate(
            volume=volume,
            replicas=replicas,
            snapshots=objects_referencing_volume(snapshots, volume_name),
            backups=objects_referencing_volume(backups, volume_name),
            pvc=pvc,
            pv=pv,
            statefulset=statefulset,
            pod=pod,
            peer_pods=peers,
        )
        if candidate:
            candidates.append(candidate)
        else:
            print(f"longhorn_volume={volume_name} action=retain reason={reason}")
    return candidates


def pod_ordinal(pod_name: str, statefulset_name: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(statefulset_name)}-(\d+)", pod_name)
    return int(match.group(1)) if match else None


def assert_destructive_contract(kube: Kubectl, candidate: Candidate) -> None:
    pvc = kube.get_optional("-n", candidate.namespace, "get", f"pvc/{candidate.pvc_name}")
    pv = kube.get_optional("get", f"pv/{candidate.pv_name}")
    volume = kube.get_optional(
        "-n", "longhorn-system", "get", f"volumes.longhorn.io/{candidate.volume_name}"
    )
    statefulset = kube.get_optional(
        "-n",
        candidate.namespace,
        "get",
        f"statefulset/{candidate.statefulset_name}",
    )
    if not pvc or not pv or not volume or not statefulset:
        raise RuntimeError("PVC, PV, Longhorn volume, or StatefulSet disappeared before deletion")
    claim_ref = pv.get("spec", {}).get("claimRef", {})
    csi = pv.get("spec", {}).get("csi", {})
    status = volume.get("status", {})
    if not (
        pvc.get("metadata", {}).get("uid") == candidate.pvc_uid
        and pvc.get("status", {}).get("phase") == "Bound"
        and pvc.get("spec", {}).get("volumeName") == candidate.pv_name
        and claim_ref.get("uid") == candidate.pvc_uid
        and csi.get("driver") == "driver.longhorn.io"
        and csi.get("volumeHandle") == candidate.volume_name
        and int(status.get("actualSize") or 0) == 0
        and status.get("state") == "detached"
        and status.get("robustness") == "faulted"
        and not volume.get("spec", {}).get("fromBackup")
        and not status.get("lastBackup")
        and not status.get("lastBackupAt")
        and statefulset.get("metadata", {}).get("uid") == candidate.statefulset_uid
        and statefulset_retains_claims(statefulset)
    ):
        raise RuntimeError("empty faulted claim identity or data-safety contract changed")
    for resource in ("replicas.longhorn.io", "snapshots.longhorn.io", "backups.longhorn.io"):
        objects = kube.items("-n", "longhorn-system", "get", resource)
        if objects_referencing_volume(objects, candidate.volume_name):
            raise RuntimeError(f"{resource} acquired a recovery source before deletion")


def recover_candidate(kube: Kubectl, candidate: Candidate, timeout: int) -> None:
    app = kube.get_optional("-n", "argocd", "get", f"application/{candidate.app_name}")
    if not app:
        raise RuntimeError(f"Argo CD application {candidate.app_name} is absent")
    annotations = app.get("metadata", {}).get("annotations", {})
    skip_key = "argocd.argoproj.io/skip-reconcile"
    previous_skip = annotations.get(skip_key)
    paused = False
    scaled = False
    try:
        kube.run(
            "-n",
            "argocd",
            "annotate",
            f"application/{candidate.app_name}",
            f"{skip_key}=true",
            "--overwrite",
        )
        paused = True
        print(f"argocd_application={candidate.app_name} action=pause-for-empty-faulted-claim-repair")

        current = next(
            (
                item
                for item in discover_candidates(kube)
                if item.volume_name == candidate.volume_name
                and item.pvc_uid == candidate.pvc_uid
            ),
            None,
        )
        if not current:
            raise RuntimeError("faulted claim safety contract changed before scale-down")

        kube.run(
            "-n",
            candidate.namespace,
            "scale",
            f"statefulset/{candidate.statefulset_name}",
            f"--replicas={candidate.ordinal}",
        )
        scaled = True
        print(
            f"statefulset={candidate.namespace}/{candidate.statefulset_name} "
            f"action=scale-below-empty-faulted-ordinal replicas={candidate.ordinal} "
            f"restore={candidate.desired_replicas}"
        )

        def higher_ordinals_absent() -> bool:
            pods = kube.items("-n", candidate.namespace, "get", "pods")
            return not any(
                (ordinal := pod_ordinal(pod.get("metadata", {}).get("name", ""), candidate.statefulset_name))
                is not None
                and ordinal >= candidate.ordinal
                for pod in pods
            )

        if not wait_until(higher_ordinals_absent, min(timeout, 120)):
            raise RuntimeError("StatefulSet pods at or above the faulted ordinal did not terminate")

        assert_destructive_contract(kube, candidate)
        kube.run("-n", candidate.namespace, "delete", f"pvc/{candidate.pvc_name}", "--wait=false")
        print(
            f"pvc={candidate.namespace}/{candidate.pvc_name} uid={candidate.pvc_uid} "
            "action=delete-empty-faulted-statefulset-claim"
        )
        if not wait_until(
            lambda: kube.get_optional("-n", candidate.namespace, "get", f"pvc/{candidate.pvc_name}")
            is None,
            min(timeout, 120),
        ):
            raise RuntimeError("empty faulted PVC deletion timed out")

        old_pv = kube.get_optional("get", f"pv/{candidate.pv_name}")
        if old_pv:
            claim_ref = old_pv.get("spec", {}).get("claimRef", {})
            csi = old_pv.get("spec", {}).get("csi", {})
            if claim_ref.get("uid") != candidate.pvc_uid or csi.get("volumeHandle") != candidate.volume_name:
                raise RuntimeError("PV identity changed before cleanup")
            kube.run("delete", f"pv/{candidate.pv_name}", "--wait=false")
            print(f"pv={candidate.pv_name} action=delete-empty-faulted-retained-pv")
            if not wait_until(
                lambda: kube.get_optional("get", f"pv/{candidate.pv_name}") is None,
                min(timeout, 120),
            ):
                raise RuntimeError("retained PV deletion timed out")

        old_volume = kube.get_optional(
            "-n", "longhorn-system", "get", f"volumes.longhorn.io/{candidate.volume_name}"
        )
        if old_volume:
            status = old_volume.get("status", {})
            if not (
                int(status.get("actualSize") or 0) == 0
                and status.get("state") == "detached"
                and status.get("robustness") == "faulted"
            ):
                raise RuntimeError("Longhorn volume safety contract changed before cleanup")
            kube.run(
                "-n",
                "longhorn-system",
                "delete",
                f"volumes.longhorn.io/{candidate.volume_name}",
                "--wait=false",
            )
            print(f"longhorn_volume={candidate.volume_name} action=delete-empty-faulted-volume")
            if not wait_until(
                lambda: kube.get_optional(
                    "-n", "longhorn-system", "get", f"volumes.longhorn.io/{candidate.volume_name}"
                )
                is None,
                min(timeout, 120),
            ):
                raise RuntimeError("faulted Longhorn volume deletion timed out")
    finally:
        if scaled:
            kube.run(
                "-n",
                candidate.namespace,
                "scale",
                f"statefulset/{candidate.statefulset_name}",
                f"--replicas={candidate.desired_replicas}",
                check=False,
            )
        if paused:
            annotation = f"{skip_key}-" if previous_skip is None else f"{skip_key}={previous_skip}"
            kube.run(
                "-n",
                "argocd",
                "annotate",
                f"application/{candidate.app_name}",
                annotation,
                "--overwrite",
                check=False,
            )
            kube.run(
                "-n",
                "argocd",
                "annotate",
                f"application/{candidate.app_name}",
                "argocd.argoproj.io/refresh=hard",
                "--overwrite",
                check=False,
            )

    def replacement_ready() -> bool:
        pvc = kube.get_optional("-n", candidate.namespace, "get", f"pvc/{candidate.pvc_name}")
        pod = kube.get_optional("-n", candidate.namespace, "get", f"pod/{candidate.pod_name}")
        if not pvc or not pod:
            return False
        if pvc.get("metadata", {}).get("uid") == candidate.pvc_uid:
            return False
        if pvc.get("status", {}).get("phase") != "Bound" or not pod_is_ready(pod):
            return False
        pv_name = pvc.get("spec", {}).get("volumeName", "")
        pv = kube.get_optional("get", f"pv/{pv_name}") if pv_name else None
        handle = pv.get("spec", {}).get("csi", {}).get("volumeHandle", "") if pv else ""
        volume = (
            kube.get_optional("-n", "longhorn-system", "get", f"volumes.longhorn.io/{handle}")
            if handle
            else None
        )
        return bool(
            volume
            and volume.get("status", {}).get("state") == "attached"
            and volume.get("status", {}).get("robustness") == "healthy"
        )

    if not wait_until(replacement_ready, timeout):
        raise RuntimeError("replacement claim, Longhorn volume, and StatefulSet pod did not become healthy")
    print(
        f"pvc={candidate.namespace}/{candidate.pvc_name} pod={candidate.pod_name} "
        "action=recycle-empty-faulted-statefulset-claim result=healthy"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    kube = Kubectl(args.kubectl, args.kubeconfig)
    candidates = discover_candidates(kube)
    grouped: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.namespace, candidate.statefulset_name), []).append(candidate)

    safe_candidates: list[Candidate] = []
    for key, group in grouped.items():
        if len(group) != 1:
            for candidate in group:
                print(
                    f"longhorn_volume={candidate.volume_name} action=retain "
                    f"reason=multiple-faulted-claims-for-statefulset statefulset={key[0]}/{key[1]}"
                )
            continue
        safe_candidates.extend(group)

    if not safe_candidates:
        print("longhorn_empty_faulted_statefulset_claim_repair=not-needed")
        return 0
    for candidate in safe_candidates:
        recover_candidate(kube, candidate, args.timeout)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"result=fail reason=empty-faulted-statefulset-claim-repair error={error}", file=sys.stderr)
        raise SystemExit(1) from error
