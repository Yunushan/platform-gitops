#!/usr/bin/env python3
"""Run an opt-in Forgejo singleton recovery drill against an RKE2 cluster."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from bounded_subprocess import BoundedSubprocessError, run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


CONFIRMATION = "FAILOVER_FORGEJO_SINGLETON"
SELECTOR = "app.kubernetes.io/name=forgejo,app.kubernetes.io/instance=forgejo"
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
KUBERNETES_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
LONGHORN_DRIVER = "driver.longhorn.io"
LONGHORN_SECRET_REFS = (
    "nodePublishSecretRef",
    "nodeStageSecretRef",
    "nodeExpandSecretRef",
)
KUBECTL_TIMEOUT_SECONDS = 120


class DrillError(RuntimeError):
    """Raised when the recovery drill cannot prove a safe recovery."""


def nested(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for segment in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


class Kubectl:
    def __init__(self, binary: str, kubeconfig: str) -> None:
        self.prefix = [binary, "--kubeconfig", kubeconfig]

    def run(self, *args: str) -> str:
        try:
            timeout = bounded_timeout_seconds(
                KUBECTL_TIMEOUT_SECONDS,
                "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
            )
        except ValueError as exc:
            raise DrillError(str(exc)) from None
        try:
            process = run_bounded(
                [*self.prefix, *args],
                check=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise DrillError(
                f"kubectl timed out after {timeout:g} seconds: {' '.join(args)}"
            ) from None
        except (BoundedSubprocessError, ValueError) as exc:
            raise DrillError(f"kubectl output rejected: {exc}") from None
        if process.returncode != 0:
            detail = process.stderr.strip().splitlines()
            message = detail[-1] if detail else "kubectl returned no error detail"
            raise DrillError(f"kubectl {' '.join(args)} failed: {message}")
        return process.stdout

    def json(self, *args: str) -> dict[str, Any]:
        try:
            document = loads_strict_json(self.run(*args, "-o", "json"))
        except json.JSONDecodeError as exc:
            raise DrillError(f"kubectl {' '.join(args)} returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise DrillError(f"kubectl {' '.join(args)} did not return an object")
        return document


def pod_is_ready(pod: dict[str, Any]) -> bool:
    if nested(pod, "metadata.deletionTimestamp"):
        return False
    if nested(pod, "status.phase") != "Running":
        return False
    statuses = nested(pod, "status.containerStatuses")
    if not isinstance(statuses, list) or not statuses:
        return False
    if not all(status.get("ready") is True for status in statuses):
        return False
    conditions = nested(pod, "status.conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def node_is_ready(node: dict[str, Any]) -> bool:
    conditions = nested(node, "status.conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def node_is_schedulable(node: dict[str, Any]) -> bool:
    return node_is_ready(node) and nested(node, "spec.unschedulable") is not True


def ready_schedulable_nodes(kube: Kubectl) -> list[str]:
    document = kube.json("get", "nodes")
    items = document.get("items", [])
    names = sorted(
        str(nested(node, "metadata.name") or "").strip()
        for node in items if isinstance(items, list) and isinstance(node, dict)
        if node_is_schedulable(node)
    )
    return [name for name in names if name]


def service_port(service: dict[str, Any]) -> int:
    ports = nested(service, "spec.ports")
    if not isinstance(ports, list) or not ports:
        raise DrillError("Forgejo service has no ports")
    preferred = [
        item
        for item in ports
        if str(item.get("name", "")).lower() in {"http", "web"}
    ]
    candidates = preferred or [item for item in ports if item.get("protocol", "TCP") == "TCP"]
    if not candidates:
        raise DrillError("Forgejo service has no TCP port")
    try:
        port = int(candidates[0]["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DrillError("Forgejo service HTTP port is invalid") from exc
    if not 1 <= port <= 65535:
        raise DrillError("Forgejo service HTTP port is outside the valid range")
    return port


def ready_endpoint_addresses(endpoint_slices: dict[str, Any]) -> set[str]:
    addresses: set[str] = set()
    items = endpoint_slices.get("items", [])
    for item in items if isinstance(items, list) else []:
        endpoints = item.get("endpoints", [])
        for endpoint in endpoints if isinstance(endpoints, list) else []:
            conditions = endpoint.get("conditions", {})
            if isinstance(conditions, dict):
                if conditions.get("ready") is False or conditions.get("terminating") is True:
                    continue
            values = endpoint.get("addresses", [])
            for address in values if isinstance(values, list) else []:
                if isinstance(address, str) and address.strip():
                    addresses.add(address.strip())
    return addresses


def image_ids(pod: dict[str, Any]) -> list[str]:
    statuses = nested(pod, "status.containerStatuses")
    if not isinstance(statuses, list):
        raise DrillError("Forgejo pod has no runtime container statuses")
    values = sorted(
        str(status.get("imageID", "")).strip()
        for status in statuses
    )
    if not values or any(not value for value in values):
        raise DrillError("Forgejo pod does not expose immutable runtime image IDs")
    return values


def http_health_code(host: str, port: int, timeout: int) -> int:
    request = Request(
        f"http://{host}:{port}/api/healthz",
        headers={"User-Agent": "platform-forgejo-recovery-drill/1"},
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)
    except (OSError, URLError) as exc:
        raise DrillError(f"Forgejo health endpoint is unreachable: {exc}") from exc


def require_singleton_deployment(deployment: dict[str, Any]) -> None:
    replicas = nested(deployment, "spec.replicas")
    strategy = nested(deployment, "spec.strategy.type")
    generation = nested(deployment, "metadata.generation")
    observed = nested(deployment, "status.observedGeneration")
    if replicas != 1:
        raise DrillError("Forgejo recovery drill requires exactly one desired replica")
    if strategy != "Recreate":
        raise DrillError("Forgejo recovery drill requires the Recreate strategy")
    if not isinstance(generation, int) or observed != generation:
        raise DrillError("Forgejo Deployment has not observed its current generation")
    if nested(deployment, "status.readyReplicas") != 1:
        raise DrillError("Forgejo Deployment does not have exactly one Ready replica")
    if nested(deployment, "status.availableReplicas") != 1:
        raise DrillError("Forgejo Deployment does not have exactly one Available replica")
    if nested(deployment, "status.updatedReplicas") != 1:
        raise DrillError("Forgejo Deployment does not have exactly one Updated replica")


def require_singleton_pdb(kube: Kubectl, namespace: str) -> None:
    document = kube.json(
        "-n", namespace, "get", "poddisruptionbudgets.policy", "-l", SELECTOR
    )
    items = document.get("items", [])
    protected = any(
        str(nested(item, "spec.minAvailable") or "") == "1"
        and nested(item, "spec.maxUnavailable") is None
        for item in items if isinstance(items, list)
    )
    if not protected:
        raise DrillError("Forgejo singleton is not protected by minAvailable=1 PDB")


def storage_claims(
    kube: Kubectl,
    namespace: str,
    pod: dict[str, Any],
    encryption_secret_name: str,
) -> list[dict[str, Any]]:
    volumes = nested(pod, "spec.volumes")
    if not isinstance(volumes, list):
        raise DrillError("Forgejo pod has no volume definitions")
    claim_names = sorted(
        {
            str(nested(volume, "persistentVolumeClaim.claimName") or "").strip()
            for volume in volumes
        }
        - {""}
    )
    if not claim_names:
        raise DrillError("Forgejo pod has no persistent volume claim")

    claims: list[dict[str, Any]] = []
    for claim_name in claim_names:
        claim = kube.json("-n", namespace, "get", "persistentvolumeclaim", claim_name)
        if nested(claim, "status.phase") != "Bound":
            raise DrillError(f"Forgejo PVC {claim_name} is not Bound")
        volume_name = str(nested(claim, "spec.volumeName") or "").strip()
        claim_uid = str(nested(claim, "metadata.uid") or "").strip()
        storage_class = str(nested(claim, "spec.storageClassName") or "").strip()
        if not volume_name or not claim_uid:
            raise DrillError(f"Forgejo PVC {claim_name} has incomplete identity")
        persistent_volume = kube.json("get", "persistentvolume", volume_name)
        csi = nested(persistent_volume, "spec.csi")
        if not isinstance(csi, dict) or csi.get("driver") != LONGHORN_DRIVER:
            raise DrillError(
                f"Forgejo PVC {claim_name} is not backed by the Longhorn CSI driver"
            )
        if not storage_class.startswith("longhorn") or not storage_class.endswith("-encrypted"):
            raise DrillError(
                f"Forgejo PVC {claim_name} does not use an encrypted Longhorn storage class"
            )
        attributes = csi.get("volumeAttributes")
        if not isinstance(attributes, dict) or str(attributes.get("encrypted", "")).lower() != "true":
            raise DrillError(
                f"Forgejo PV {volume_name} does not declare Longhorn encryption"
            )
        encryption_refs: dict[str, str] = {}
        for ref_name in LONGHORN_SECRET_REFS:
            ref = csi.get(ref_name)
            if not isinstance(ref, dict):
                raise DrillError(
                    f"Forgejo PV {volume_name} is missing CSI {ref_name}"
                )
            ref_secret = str(ref.get("name") or "").strip()
            ref_namespace = str(ref.get("namespace") or "").strip()
            if ref_secret != encryption_secret_name or ref_namespace != "longhorn-system":
                raise DrillError(
                    f"Forgejo PV {volume_name} CSI {ref_name} does not reference "
                    f"longhorn-system/{encryption_secret_name}"
                )
            encryption_refs[ref_name] = f"{ref_namespace}/{ref_secret}"
        entry: dict[str, Any] = {
            "name": claim_name,
            "uid": claim_uid,
            "volumeName": volume_name,
            "storageClass": storage_class,
            "csiDriver": LONGHORN_DRIVER,
            "encrypted": True,
            "encryptionSecretName": encryption_secret_name,
            "encryptionSecretRefs": encryption_refs,
        }
        volume = kube.json(
            "-n", "longhorn-system", "get", "volumes.longhorn.io", volume_name
        )
        state = str(nested(volume, "status.state") or "").lower()
        robustness = str(nested(volume, "status.robustness") or "").lower()
        if state != "attached" or robustness != "healthy":
            raise DrillError(
                f"Forgejo Longhorn volume {volume_name} is {state or 'unknown'}/"
                f"{robustness or 'unknown'}"
            )
        entry["longhornState"] = state
        entry["longhornRobustness"] = robustness
        claims.append(entry)
    return claims


def claim_identity(claims: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    return [
        (
            str(claim["name"]),
            str(claim["uid"]),
            str(claim["volumeName"]),
            str(claim["storageClass"]),
            str(claim["csiDriver"]),
            str(claim["encryptionSecretName"]),
            json.dumps(claim["encryptionSecretRefs"], sort_keys=True),
        )
        for claim in claims
    ]


def argocd_application_state(kube: Kubectl, source_commit: str) -> dict[str, Any]:
    application = kube.json(
        "-n", "argocd", "get", "application.argoproj.io", "forgejo"
    )
    revision = str(nested(application, "status.sync.revision") or "").lower()
    sync = str(nested(application, "status.sync.status") or "")
    health = str(nested(application, "status.health.status") or "")
    uid = str(nested(application, "metadata.uid") or "").strip()
    if sync != "Synced" or health != "Healthy":
        raise DrillError(f"Argo CD Forgejo application is {sync or 'Unknown'}/{health or 'Unknown'}")
    if revision != source_commit:
        raise DrillError(
            f"Argo CD Forgejo revision {revision or 'unknown'} does not match tested commit {source_commit}"
        )
    if not uid:
        raise DrillError("Argo CD Forgejo application has no UID")
    return {"uid": uid, "revision": revision, "sync": sync, "health": health}


def snapshot(
    kube: Kubectl,
    namespace: str,
    http_timeout: int,
    source_commit: str,
    encryption_secret_name: str,
) -> dict[str, Any]:
    application = argocd_application_state(kube, source_commit)
    deployment = kube.json("-n", namespace, "get", "deployment", "forgejo")
    require_singleton_deployment(deployment)
    pods = kube.json("-n", namespace, "get", "pods", "-l", SELECTOR).get("items", [])
    active = [
        pod
        for pod in pods if isinstance(pods, list)
        if not nested(pod, "metadata.deletionTimestamp")
    ]
    ready = [pod for pod in active if pod_is_ready(pod)]
    if len(active) != 1 or len(ready) != 1:
        raise DrillError(
            f"Forgejo must have exactly one active Ready pod (active={len(active)}, ready={len(ready)})"
        )
    pod = ready[0]
    pod_ip = str(nested(pod, "status.podIP") or "").strip()
    if not pod_ip:
        raise DrillError("Forgejo Ready pod has no pod IP")

    service = kube.json("-n", namespace, "get", "service", "forgejo-http")
    cluster_ip = str(nested(service, "spec.clusterIP") or "").strip()
    service_uid = str(nested(service, "metadata.uid") or "").strip()
    if not cluster_ip or cluster_ip == "None" or not service_uid:
        raise DrillError("Forgejo HTTP service has no stable ClusterIP identity")
    port = service_port(service)

    slices = kube.json(
        "-n", namespace, "get", "endpointslices.discovery.k8s.io",
        "-l", "kubernetes.io/service-name=forgejo-http",
    )
    endpoint_addresses = ready_endpoint_addresses(slices)
    if pod_ip not in endpoint_addresses:
        raise DrillError("Forgejo Ready pod is absent from the service EndpointSlices")
    code = http_health_code(cluster_ip, port, http_timeout)
    if code != 200:
        raise DrillError(f"Forgejo /api/healthz returned HTTP {code}, expected 200")

    return {
        "deploymentUid": str(nested(deployment, "metadata.uid") or ""),
        "deploymentGeneration": int(nested(deployment, "metadata.generation") or 0),
        "argocdApplicationUid": application["uid"],
        "argocdRevision": application["revision"],
        "podName": str(nested(pod, "metadata.name") or ""),
        "podUid": str(nested(pod, "metadata.uid") or ""),
        "podIP": pod_ip,
        "node": str(nested(pod, "spec.nodeName") or ""),
        "imageIDs": image_ids(pod),
        "serviceUid": service_uid,
        "serviceClusterIP": cluster_ip,
        "servicePort": port,
        "endpointAddresses": sorted(endpoint_addresses),
        "httpCode": code,
        "storageClaims": storage_claims(
            kube, namespace, pod, encryption_secret_name
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    parser.add_argument("--namespace", default="forgejo")
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--drill-id", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--encryption-secret-name", default="longhorn-crypto")
    parser.add_argument("--max-rto-seconds", type=int, default=300)
    parser.add_argument("--http-timeout-seconds", type=int, default=10)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.confirmation != CONFIRMATION:
        raise DrillError(f"--confirmation must be exactly {CONFIRMATION}")
    if not ID_RE.fullmatch(args.drill_id):
        raise DrillError("--drill-id may contain only letters, digits, dot, underscore, and hyphen")
    if not args.operator.strip() or not args.approver.strip():
        raise DrillError("operator and approver must be non-empty")
    if args.operator.strip().casefold() == args.approver.strip().casefold():
        raise DrillError("operator and approver must be different people")
    if not ID_RE.fullmatch(args.profile):
        raise DrillError("profile contains unsupported characters")
    if not COMMIT_RE.fullmatch(args.source_commit):
        raise DrillError("source commit must be a 40-character lowercase Git SHA")
    if not KUBERNETES_NAME_RE.fullmatch(args.encryption_secret_name):
        raise DrillError("encryption secret name must be a valid Kubernetes name")
    if not 30 <= args.max_rto_seconds <= 3600:
        raise DrillError("max RTO must be between 30 and 3600 seconds")
    if not 1 <= args.http_timeout_seconds <= 60:
        raise DrillError("HTTP timeout must be between 1 and 60 seconds")
    if not 0.5 <= args.poll_seconds <= 30:
        raise DrillError("poll interval must be between 0.5 and 30 seconds")


def run(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    kube = Kubectl(args.kubectl, args.kubeconfig)
    require_singleton_pdb(kube, args.namespace)
    before = snapshot(
        kube,
        args.namespace,
        args.http_timeout_seconds,
        args.source_commit,
        args.encryption_secret_name,
    )
    source_node = str(before["node"])
    schedulable_nodes = ready_schedulable_nodes(kube)
    if source_node not in schedulable_nodes:
        raise DrillError(
            f"Forgejo source node {source_node or 'unknown'} is not Ready and schedulable"
        )
    eligible_recovery_nodes = [
        node for node in schedulable_nodes if node != source_node
    ]
    if not eligible_recovery_nodes:
        raise DrillError(
            "Forgejo node failover requires another Ready and schedulable node"
        )

    started: float | None = None
    elapsed: float | None = None
    after: dict[str, Any] | None = None
    primary_error: Exception | None = None
    cleanup_error: Exception | None = None
    source_node_cordoned = False
    source_node_restored = False
    try:
        print(
            f"Cordoning Forgejo source node {source_node} before the approved failover.",
            file=sys.stderr,
        )
        kube.run("cordon", source_node)
        source_node_cordoned = True
        cordoned_node = kube.json("get", "node", source_node)
        if nested(cordoned_node, "spec.unschedulable") is not True:
            raise DrillError(f"Forgejo source node {source_node} was not cordoned")

        started = time.monotonic()
        print(
            f"Deleting Forgejo pod {before['podName']} to prove cross-node recovery.",
            file=sys.stderr,
        )
        kube.run(
            "-n", args.namespace, "delete", "pod", str(before["podName"]),
            "--wait=false",
        )

        deadline = started + args.max_rto_seconds
        last_error = "replacement pod has not appeared"
        while time.monotonic() < deadline:
            try:
                candidate = snapshot(
                    kube,
                    args.namespace,
                    args.http_timeout_seconds,
                    args.source_commit,
                    args.encryption_secret_name,
                )
                if candidate["podUid"] == before["podUid"]:
                    last_error = "the original pod is still active"
                elif candidate["node"] == source_node:
                    last_error = "the replacement pod returned to the source node"
                elif candidate["node"] not in eligible_recovery_nodes:
                    raise DrillError(
                        "Forgejo recovered on a node that was not Ready and schedulable "
                        "during preflight"
                    )
                elif candidate["deploymentUid"] != before["deploymentUid"]:
                    raise DrillError("Forgejo Deployment identity changed during the drill")
                elif candidate["deploymentGeneration"] != before["deploymentGeneration"]:
                    raise DrillError("Forgejo Deployment generation changed during the drill")
                elif candidate["argocdApplicationUid"] != before["argocdApplicationUid"]:
                    raise DrillError("Argo CD Forgejo Application identity changed during the drill")
                elif candidate["argocdRevision"] != before["argocdRevision"]:
                    raise DrillError("Argo CD Forgejo revision changed during the drill")
                elif candidate["serviceUid"] != before["serviceUid"]:
                    raise DrillError("Forgejo service identity changed during the drill")
                elif candidate["serviceClusterIP"] != before["serviceClusterIP"]:
                    raise DrillError("Forgejo service ClusterIP changed during the drill")
                elif candidate["imageIDs"] != before["imageIDs"]:
                    raise DrillError("Forgejo runtime image changed during the drill")
                elif claim_identity(candidate["storageClaims"]) != claim_identity(before["storageClaims"]):
                    raise DrillError("Forgejo PVC, PV, or encryption identity changed during the drill")
                else:
                    after = candidate
                    break
            except DrillError as exc:
                last_error = str(exc)
            time.sleep(args.poll_seconds)

        if after is None:
            raise DrillError(
                f"Forgejo did not recover on another node within "
                f"{args.max_rto_seconds}s: {last_error}"
            )
        elapsed = round(time.monotonic() - started, 3)
        if elapsed > args.max_rto_seconds:
            raise DrillError(
                f"Forgejo recovered in {elapsed:.3f}s, exceeding the "
                f"{args.max_rto_seconds}s RTO"
            )
    except (OSError, DrillError) as exc:
        primary_error = exc
    finally:
        if source_node_cordoned:
            try:
                kube.run("uncordon", source_node)
                restored_node = kube.json("get", "node", source_node)
                if not node_is_schedulable(restored_node):
                    raise DrillError(
                        f"Forgejo source node {source_node} was not restored to "
                        "Ready and schedulable"
                    )
                source_node_restored = True
            except (OSError, DrillError) as exc:
                cleanup_error = exc

    if primary_error is not None or cleanup_error is not None:
        details: list[str] = []
        if primary_error is not None:
            details.append(str(primary_error))
        if cleanup_error is not None:
            details.append(f"source-node cleanup failed: {cleanup_error}")
        raise DrillError("; ".join(details))
    if after is None or started is None or elapsed is None or not source_node_restored:
        raise DrillError("Forgejo failover completed without complete recovery evidence")

    return {
        "schemaVersion": 2,
        "drillId": args.drill_id,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "operator": args.operator.strip(),
        "approver": args.approver.strip(),
        "profile": args.profile,
        "sourceCommit": args.source_commit,
        "result": "passed",
        "rtoSeconds": args.max_rto_seconds,
        "elapsedSeconds": elapsed,
        "namespace": args.namespace,
        "deployment": "forgejo",
        "service": "forgejo-http",
        "recoveryMode": "node-failover",
        "sourceNode": source_node,
        "targetNode": str(after["node"]),
        "eligibleRecoveryNodes": eligible_recovery_nodes,
        "sourceNodeWasSchedulable": True,
        "sourceNodeRestoredSchedulable": source_node_restored,
        "preRecovery": before,
        "postRecovery": after,
    }


def main() -> int:
    args = parse_args()
    try:
        document = run(args)
    except (OSError, DrillError) as exc:
        print(f"Forgejo recovery drill failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
