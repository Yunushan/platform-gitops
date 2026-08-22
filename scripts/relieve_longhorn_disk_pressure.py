#!/usr/bin/env python3
"""Restore legacy Longhorn pressure state without starting blocked eviction."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

from bounded_subprocess import BoundedSubprocessError, run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


JsonObject = dict[str, Any]
ANNOTATION = "platform.gitops.io/root-pressure-eviction"
KUBECTL_TIMEOUT_SECONDS = 120


class Kubectl:
    def __init__(self, executable: str, kubeconfig: str):
        self.base = [executable, "--kubeconfig", kubeconfig]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            timeout = bounded_timeout_seconds(
                KUBECTL_TIMEOUT_SECONDS,
                "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
            )
            result = run_bounded(
                self.base + list(args), check=False, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"kubectl timed out after {timeout:g} seconds: " + " ".join(args)
            ) from None
        except (BoundedSubprocessError, ValueError) as exc:
            raise RuntimeError(f"kubectl output rejected: {exc}") from None
        if check and result.returncode != 0:
            diagnostic = (result.stderr or "") + (result.stdout or "")
            raise RuntimeError(f"kubectl failed: {' '.join(args)}: {diagnostic.strip()}")
        return result

    def get_json(self, *args: str) -> JsonObject:
        result = self.run(*args, "-o", "json")
        try:
            value = loads_strict_json(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"kubectl returned invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RuntimeError("kubectl returned a non-object JSON document")
        return value

    def get_optional_json(self, *args: str) -> JsonObject | None:
        result = self.run(*args, "-o", "json", check=False)
        if result.returncode != 0:
            diagnostic = (result.stderr or "") + (result.stdout or "")
            if "NotFound" in diagnostic or "doesn't have a resource type" in diagnostic:
                return None
            raise RuntimeError(f"kubectl failed: {' '.join(args)}: {diagnostic.strip()}")
        try:
            value = loads_strict_json(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"kubectl returned invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RuntimeError("kubectl returned a non-object JSON document")
        return value

    def patch_longhorn_node(self, node_name: str, patch: JsonObject) -> None:
        self.run(
            "-n",
            "longhorn-system",
            "patch",
            f"nodes.longhorn.io/{node_name}",
            "--type=merge",
            "-p",
            json.dumps(patch, separators=(",", ":")),
        )

    def patch_longhorn_replica(
        self, replica_name: str, eviction_requested: bool
    ) -> bool:
        result = self.run(
            "-n",
            "longhorn-system",
            "patch",
            f"replicas.longhorn.io/{replica_name}",
            "--type=merge",
            "-p",
            json.dumps(
                {"spec": {"evictionRequested": eviction_requested}},
                separators=(",", ":"),
            ),
            check=False,
        )
        if result.returncode == 0:
            return True
        diagnostic = (result.stderr or "") + (result.stdout or "")
        if "NotFound" in diagnostic:
            return False
        raise RuntimeError(
            f"kubectl failed while restoring Longhorn replica {replica_name}: "
            f"{diagnostic.strip()}"
        )


def kubernetes_disk_pressure(kube: Kubectl, node_name: str) -> str:
    node = kube.get_json("get", f"node/{node_name}")
    for condition in node.get("status", {}).get("conditions") or []:
        if condition.get("type") == "DiskPressure":
            return str(condition.get("status", "Unknown"))
    return "Unknown"


def annotation_state(node: JsonObject, node_name: str) -> JsonObject | None:
    raw = node.get("metadata", {}).get("annotations", {}).get(ANNOTATION, "")
    if not raw:
        return None
    try:
        state = loads_strict_json(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid {ANNOTATION} state: {exc}") from None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError(f"unsupported {ANNOTATION} state")
    if state.get("node") != node_name or not isinstance(state.get("disks"), dict):
        raise RuntimeError(f"mismatched {ANNOTATION} state")
    replicas = state.get("replicas", {})
    if not isinstance(replicas, dict):
        raise RuntimeError(f"invalid {ANNOTATION} replica state")
    for disk_name, disk_state in state["disks"].items():
        if (
            not isinstance(disk_name, str)
            or not disk_name
            or not isinstance(disk_state, dict)
            or set(disk_state) != {"allowScheduling", "evictionRequested"}
            or not isinstance(disk_state.get("allowScheduling"), bool)
            or not isinstance(disk_state.get("evictionRequested"), bool)
        ):
            raise RuntimeError(f"invalid {ANNOTATION} disk state")
    for replica_name, eviction_requested in replicas.items():
        if (
            not isinstance(replica_name, str)
            or not replica_name
            or eviction_requested is not False
        ):
            raise RuntimeError(f"invalid {ANNOTATION} replica state")
    return state


def restore_legacy_state(
    kube: Kubectl,
    node_name: str,
    state: JsonObject,
    current_node: JsonObject,
) -> None:
    for replica_name, original in state.get("replicas", {}).items():
        kube.patch_longhorn_replica(replica_name, original)
    current_disks = current_node.get("spec", {}).get("disks") or {}
    original_disks = {
        name: disk for name, disk in state["disks"].items() if name in current_disks
    }
    patch: JsonObject = {"metadata": {"annotations": {ANNOTATION: None}}}
    if original_disks:
        patch["spec"] = {"disks": original_disks}
    kube.patch_longhorn_node(node_name, patch)


def run(args: argparse.Namespace, kube: Kubectl | None = None) -> int:
    kube = kube or Kubectl(args.kubectl, args.kubeconfig)
    pressure = kubernetes_disk_pressure(kube, args.node)
    source_node = kube.get_optional_json(
        "-n", "longhorn-system", "get", f"nodes.longhorn.io/{args.node}"
    )
    if source_node is None:
        print(
            "longhorn_pressure_evacuation=not-needed "
            f"node={args.node} reason=longhorn-node-absent"
        )
        return 0
    state = annotation_state(source_node, args.node)
    if state is not None:
        restore_legacy_state(kube, args.node, state, source_node)
        print(
            "longhorn_pressure_evacuation=legacy-state-restored "
            f"node={args.node} selectedReplicas={len(state.get('replicas', {}))}"
        )
    if pressure == "False":
        print(
            "longhorn_pressure_evacuation=completed "
            f"node={args.node} disk_pressure={pressure} schedulingState=restored"
        )
        return 0
    if pressure == "True":
        print(
            "longhorn_pressure_evacuation=deferred "
            f"node={args.node} "
            "reason=longhorn-node-not-ready-during-kubernetes-disk-pressure"
        )
        return 0
    print(
        "longhorn_pressure_evacuation=deferred "
        f"node={args.node} reason=disk-pressure-unclassified "
        f"disk_pressure={pressure}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--node", required=True)
    result.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    result.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    # Retained for command-line compatibility with earlier cleanup playbooks.
    result.add_argument("--timeout", type=int, default=1200)
    result.add_argument("--poll-interval", type=int, default=10)
    result.add_argument("--target-free-percentage", type=int, default=20)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.timeout < 300:
        raise SystemExit("--timeout must be at least 300 seconds")
    if not 15 <= args.target_free_percentage <= 50:
        raise SystemExit("--target-free-percentage must be between 15 and 50")
    if not 5 <= args.poll_interval <= 60:
        raise SystemExit("--poll-interval must be between 5 and 60 seconds")
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"longhorn_pressure_evacuation=failed reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
