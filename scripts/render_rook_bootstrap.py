#!/usr/bin/env python3
"""Render pinned Rook/CSI bootstrap APIs, runtime RBAC and finite controller policies."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_bytes
from kubernetes_management import validate_target
import render_ceph_storage as storage
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CATALOG_SHA256 = "e63630e57672214ba220d484ba42069ad2761cfd1b7ab0220636fc550c28655f"
OWNER = "platform.gitops/rook-bootstrap"
IMAGES = {"rook", "csi_operator", "plugin", "provisioner", "attacher", "resizer", "snapshotter", "snapshot_metadata", "registrar", "addons"}
BUDGETS = {"rook_operator", "csi_operator", "controller_sidecar", "node_sidecar", "controller_plugin", "node_plugin"}
UNVERIFIED = {k: False for k in ("provider_installed", "client_mounts_verified", "data_roundtrip_verified", "node_loss_verified", "ha_verified", "full_stack_verified")}


def budget(value):
    if not isinstance(value, dict) or set(value) != {"requests", "limits"}: raise ValueError("Specify finite bootstrap resource budgets")
    amounts = {}
    for kind, row in value.items():
        if (not isinstance(row, dict) or set(row) != {"cpu", "memory"} or not isinstance(row["cpu"], str) or not re.fullmatch(r"[1-9][0-9]{0,5}m?", row["cpu"])
                or not isinstance(row["memory"], str) or not re.fullmatch(r"[1-9][0-9]{0,5}(?:Mi|Gi)", row["memory"])): raise ValueError("Use exact CPU/binary-memory budgets")
        cpu = int(row["cpu"][:-1]) if row["cpu"].endswith("m") else int(row["cpu"]) * 1000
        memory = int(row["memory"][:-2]) * (1024 ** 2 if row["memory"].endswith("Mi") else 1024 ** 3)
        if not 10 <= cpu <= 64000 or not 32 * 1024 ** 2 <= memory <= 64 * 1024 ** 3: raise ValueError("A bootstrap budget is outside its reviewed bounds")
        amounts[kind] = (cpu, memory)
    if any(a > b for a, b in zip(amounts["requests"], amounts["limits"])): raise ValueError("Resource limits cannot be below requests")


def validate(plan, runtime):
    if (runtime not in storage.RUNTIMES or not isinstance(plan, dict) or set(plan) != {"schema_version", "namespace", "namespace_uid", "target", "nodes", "controller_nodes", "domain_label", "images", "kubelet_dir", "watch_current_namespace_only", "resources", "admitted_core_sha256"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not storage.name(plan["namespace"]) or not storage.uid(plan["namespace_uid"])
            or plan["domain_label"] not in {"topology.kubernetes.io/zone", "topology.rook.io/rack"} or type(plan["watch_current_namespace_only"]) is not bool
            or plan["kubelet_dir"] not in {"/var/lib/kubelet", "/var/lib/rancher/rke2/agent/kubelet", "/var/lib/rancher/k3s/agent/kubelet"}
            or plan["admitted_core_sha256"] is not None and not storage.sha(plan["admitted_core_sha256"])): raise ValueError("Use a complete reviewed Rook bootstrap scope")
    validate_target(plan["target"])
    if not isinstance(plan["images"], dict) or set(plan["images"]) != IMAGES or any(not storage.image(v) for v in plan["images"].values()): raise ValueError("Pin every operator/plugin/sidecar image; empty built-in fallbacks are forbidden")
    nodes = plan["nodes"]
    if not isinstance(nodes, list) or not 4 <= len(nodes) <= 128: raise ValueError("Select the complete approved storage-client node set")
    for node in nodes:
        if (not isinstance(node, dict) or set(node) != {"name", "uid", "failure_domain"} or not storage.resource(node["name"])
                or not storage.uid(node["uid"]) or not storage.name(node["failure_domain"])): raise ValueError("Pin all client node identities and domains")
    if any(len({n[k] for n in nodes}) != len(nodes) for k in ("name", "uid")): raise ValueError("Client nodes must be distinct")
    controllers = plan["controller_nodes"]
    if (not isinstance(controllers, list) or not 2 <= len(controllers) <= 8 or len(set(controllers)) != len(controllers)
            or not set(controllers).issubset({n["name"] for n in nodes}) or len({n["failure_domain"] for n in nodes if n["name"] in controllers}) < 2): raise ValueError("Place controllers on reviewed nodes spanning at least two domains")
    if not isinstance(plan["resources"], dict) or set(plan["resources"]) != BUDGETS: raise ValueError("Budget operators and all enabled CSI containers")
    for value in plan["resources"].values(): budget(value)


def catalog():
    raw = read_bounded_bytes(ROOT / "schemas/rook/v1.21.2/bootstrap.json", max_bytes=2 * 1024 ** 2)
    if hashlib.sha256(raw).hexdigest() != CATALOG_SHA256: raise ValueError("Pinned upstream bootstrap resources changed")
    return loads_strict_json(raw)


def canonical_budget(value):
    result = copy.deepcopy(value)
    for row in result.values():
        if row["cpu"].endswith("m") and int(row["cpu"][:-1]) % 1000 == 0: row["cpu"] = str(int(row["cpu"][:-1]) // 1000)
        if row["memory"].endswith("Mi") and int(row["memory"][:-2]) % 1024 == 0: row["memory"] = str(int(row["memory"][:-2]) // 1024) + "Gi"
    return result


def explicit_pod_defaults(pod):
    # Kubernetes core/v1 defaults, made concrete before admission/policy pinning.
    for key, value in {"restartPolicy": "Always", "dnsPolicy": "ClusterFirst", "securityContext": {}, "schedulerName": "default-scheduler", "terminationGracePeriodSeconds": 30}.items(): pod.setdefault(key, value)
    for container in pod["containers"]:
        for key, value in {"imagePullPolicy": "IfNotPresent", "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File"}.items(): container.setdefault(key, value)
        for variable in container.get("env", []):
            field = variable.get("valueFrom", {}).get("fieldRef")
            if field is not None: field.setdefault("apiVersion", "v1")
        for name in ("livenessProbe", "readinessProbe", "startupProbe"):
            if name not in container: continue
            probe = container[name]
            for key, value in {"timeoutSeconds": 1, "periodSeconds": 10, "successThreshold": 1, "failureThreshold": 3}.items(): probe.setdefault(key, value)
            if "httpGet" in probe:
                probe["httpGet"].setdefault("scheme", "HTTP"); probe["httpGet"].setdefault("path", "/")


def affinity(plan, names, selector=None):
    value = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": sorted(names)}]}]}}}
    if selector:
        value["podAntiAffinity"] = {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": plan["domain_label"], "labelSelector": {"matchLabels": selector}}]}
    return value


def schema_map():
    return {x["object"]["spec"]["group"] + "/" + v["name"] + "/" + x["object"]["spec"]["names"]["kind"]: v["schema"]["openAPIV3Schema"]
        for x in catalog()["objects"] if x["object"]["kind"] == "CustomResourceDefinition" for v in x["object"]["spec"]["versions"] if v.get("served") is True}


def objects(plan, runtime):
    validate(plan, runtime)
    entries = catalog()["objects"]
    sas = {"ceph-csi-controller-manager", "ceph-csi-rbd-ctrlplugin-sa", "ceph-csi-rbd-nodeplugin-sa", "ceph-csi-cephfs-ctrlplugin-sa", "ceph-csi-cephfs-nodeplugin-sa"}
    csi_bindings = [x["object"] for x in entries if x["source"] == "csi-operator.yaml" and x["object"]["kind"] in {"RoleBinding", "ClusterRoleBinding"}
        and x["object"].get("subjects") and all(s.get("kind") == "ServiceAccount" and s.get("name") in sas for s in x["object"]["subjects"])]
    refs = {(x["roleRef"]["kind"], x["roleRef"]["name"]) for x in csi_bindings}
    phases = {"apis": [], "core": [], "drivers": []}
    for entry in entries:
        obj = copy.deepcopy(entry["object"]); kind = obj["kind"]; name = obj["metadata"]["name"]
        if kind == "Namespace": continue  # The approved privileged namespace is a pinned prerequisite.
        if entry["source"] == "csi-operator.yaml":
            if kind == "ServiceAccount" and name not in sas: continue
            if kind in {"RoleBinding", "ClusterRoleBinding"} and obj not in csi_bindings: continue
            if kind in {"Role", "ClusterRole"} and (kind, name) not in refs: continue
        if "namespace" in obj["metadata"]: obj["metadata"]["namespace"] = plan["namespace"]
        obj["metadata"].setdefault("labels", {})[OWNER] = plan["namespace"]
        for subject in obj.get("subjects", []):
            if subject.get("kind") != "ServiceAccount" or subject.get("namespace") != "rook-ceph": raise ValueError("Review changed upstream RBAC subjects")
            subject["namespace"] = plan["namespace"]
        if kind == "ConfigMap":
            obj["immutable"] = True
            if name == "rook-ceph-operator-config":
                obj["data"].update(ROOK_ENABLE_DISCOVERY_DAEMON="false", ROOK_DISABLE_DEVICE_HOTPLUG="true", ROOK_CEPH_ALLOW_LOOP_DEVICES="false", ROOK_DELETE_UNUSED_CRUSH_RULES="false",
                    ROOK_CURRENT_NAMESPACE_ONLY=str(plan["watch_current_namespace_only"]).lower())
            elif name == "rook-csi-operator-image-set-configmap": obj["data"] = {**{k: plan["images"][k] for k in obj["data"]}, "snapshot-metadata": plan["images"]["snapshot_metadata"]}
            else: raise ValueError("Review an unknown upstream bootstrap ConfigMap")
        if kind == "Deployment":
            pod = obj["spec"]["template"]["spec"]; containers = pod["containers"]
            if len(containers) != 1: raise ValueError("Review changed upstream operator containers")
            csi = name == "ceph-csi-controller-manager"; role = "csi_operator" if csi else "rook_operator"
            if name not in {"rook-ceph-operator", "ceph-csi-controller-manager"}: raise ValueError("Review changed upstream operator workloads")
            containers[0]["image"] = plan["images"]["csi_operator" if csi else "rook"]
            containers[0]["resources"] = copy.deepcopy(plan["resources"][role])
            if csi:
                if "--leader-elect" not in containers[0].get("args", []): raise ValueError("CSI controller replication requires explicit leader election")
                obj["spec"]["replicas"] = 2
                watches = [e for e in containers[0].get("env", []) if e.get("name") == "WATCH_NAMESPACE"]
                if len(watches) != 1: raise ValueError("Review the CSI operator namespace-watch contract")
                if plan["watch_current_namespace_only"]: watches[0]["value"] = plan["namespace"]
                else: watches[0].pop("value", None)  # Empty EnvVar.Value is omitted by the Kubernetes serializer.
            else:
                obj["spec"]["replicas"] = 1
                for variable in containers[0].get("env", []):
                    if variable["name"] == "ROOK_CURRENT_NAMESPACE_ONLY": variable["value"] = str(plan["watch_current_namespace_only"]).lower()
            selector = obj["spec"]["selector"]["matchLabels"]
            pod["affinity"] = affinity(plan, plan["controller_nodes"], selector if csi else None)
            containers[0]["resources"] = canonical_budget(containers[0]["resources"])
            explicit_pod_defaults(pod)
        if kind == "OperatorConfig":
            # Every selected Driver below carries the complete policy too;
            # defaults cannot turn on optional addons/snapshots implicitly.
            obj["spec"] = {"driverSpecDefaults": {"imageSet": {"name": "rook-csi-operator-image-set-configmap"}, "deployCsiAddons": False, "snapshotPolicy": "none", "generateOMapInfo": False, "log": {"verbosity": 0}}}
        if kind == "Driver":
            driver = "rbd" if name == "rook-ceph.rbd.csi.ceph.com" else "cephfs" if name == "rook-ceph.cephfs.csi.ceph.com" else None
            if driver is None: raise ValueError("Only explicitly selected RBD and CephFS drivers are supported")
            obj["metadata"]["name"] = plan["namespace"] + "." + driver + ".csi.ceph.com"
            base = {"imageSet": {"name": "rook-csi-operator-image-set-configmap"}, "attachRequired": driver == "rbd", "fsGroupPolicy": "File", "cephFsClientType": "kernel",
                "enableMetadata": True, "generateOMapInfo": False, "deployCsiAddons": False, "enableFencing": False, "snapshotPolicy": "none", "log": {"verbosity": 0}}
            controller = {"replicas": 2, "hostNetwork": False, "privileged": False, "serviceAccountName": "ceph-csi-" + driver + "-ctrlplugin-sa", "imagePullPolicy": "IfNotPresent",
                "deploymentStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                "affinity": affinity(plan, plan["controller_nodes"], {"app": obj["metadata"]["name"] + "-ctrlplugin"}),
                "resources": {k: canonical_budget(plan["resources"]["controller_plugin" if k == "plugin" else "controller_sidecar"]) for k in ("plugin", "provisioner", "attacher", "resizer", "snapshotter", "liveness")}}
            node = {"kubeletDirPath": plan["kubelet_dir"], "enableSeLinuxHostMount": False, "serviceAccountName": "ceph-csi-" + driver + "-nodeplugin-sa", "imagePullPolicy": "IfNotPresent",
                "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}}, "affinity": affinity(plan, [n["name"] for n in plan["nodes"]]),
                "resources": {k: canonical_budget(plan["resources"]["node_plugin" if k == "plugin" else "node_sidecar"]) for k in ("plugin", "registrar", "liveness")}}
            obj["spec"] = {**base, "controllerPlugin": controller, "nodePlugin": node}
        phase = "apis" if kind == "CustomResourceDefinition" else "drivers" if obj["apiVersion"].startswith("csi.ceph.io/") else "core"
        phases[phase].append(obj)
    order = {"ServiceAccount": 0, "ClusterRole": 1, "Role": 2, "ClusterRoleBinding": 3, "RoleBinding": 4, "ConfigMap": 5, "NetworkPolicy": 6, "Deployment": 7}
    phases["core"].sort(key=lambda o: (order[o["kind"]], o["metadata"]["name"]))
    return phases


def render(plan, runtime):
    if yaml.__version__ != "6.0.3": raise ValueError("Use the reviewed YAML renderer version")
    plan = loads_strict_json(storage.canonical(plan)); phases = objects(plan, runtime)
    files = {"rook-" + phase + ".yaml": yaml.safe_dump_all(values, sort_keys=False) for phase, values in phases.items()}
    files["bootstrap-contract.json"] = storage.canonical({"schema_version": 1, "runtime": runtime, "plan": plan, "catalog_sha256": CATALOG_SHA256,
        "files_sha256": {n: hashlib.sha256(v.encode()).hexdigest() for n, v in files.items()}}) + "\n"
    files["render-status.json"] = storage.canonical({"rendered": True, "rook_operator_replicas": 1, "csi_operator_replicas": 2, "csi_controller_replicas": 2, **UNVERIFIED}) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=storage.RUNTIMES, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, **UNVERIFIED})); return 0
    except Exception:
        print("Rook bootstrap rendering refused; private inputs were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
