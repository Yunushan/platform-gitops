#!/usr/bin/env python3
"""Prepare fresh Rook cluster policy and read-only, explicitly selected device probes."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_bytes
from kubernetes_management import validate_target
import ceph_device_probe as probe
import render_ceph_storage as storage
from render_gerrit_router import write_bundle
from standalone_python import standalone_script
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SHA256 = "9e742a74eb0255274d774525aa3ef8d0dec1ff5275f9bcf046e1e7d6072db46e"
OWNER = "platform.gitops/ceph-provider"
UNVERIFIED = {k: False for k in ("devices_observed", "provider_installed", "data_roundtrip_verified", "node_loss_verified", "recovery_verified", "ha_verified", "full_stack_verified")}


def validate(plan, runtime):
    if (runtime not in storage.RUNTIMES or not isinstance(plan, dict) or set(plan) != {"schema_version", "cluster_id", "namespace", "namespace_uid", "name", "target", "operator", "images", "ceph_version", "domain_label", "nodes", "resources", "probe_timeout_seconds", "admitted_sha256"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not storage.uid(plan["cluster_id"]) or not storage.uid(plan["namespace_uid"])
            or any(not storage.name(plan[k]) or len(plan[k]) > 24 for k in ("name", "namespace")) or not storage.integer(plan["probe_timeout_seconds"], 30, 86400)
            or plan["admitted_sha256"] is not None and not storage.sha(plan["admitted_sha256"])): raise ValueError("Use a complete fresh-provider scope")
    validate_target(plan["target"])
    if (not isinstance(plan["ceph_version"], str) or not re.fullmatch(r"20[.]2[.][0-9]+", plan["ceph_version"])
            or plan["domain_label"] not in {"topology.kubernetes.io/zone", "topology.rook.io/rack"}
            or not isinstance(plan["images"], dict) or set(plan["images"]) != {"ceph", "probe"} or any(not storage.image(v) for v in plan["images"].values())): raise ValueError("Pin supported Ceph and isolated Python probe images")
    operator = plan["operator"]
    storage.validate_operator(operator)
    if operator["namespace"] == plan["namespace"] and operator["namespace_uid"] != plan["namespace_uid"]: raise ValueError("One namespace cannot have conflicting UIDs")
    nodes = plan["nodes"]
    if not isinstance(nodes, list) or not 4 <= len(nodes) <= 32: raise ValueError("Reserve at least four independent OSD hosts")
    for node in nodes:
        if (not isinstance(node, dict) or set(node) != {"name", "uid", "failure_domain", "machine_id_sha256", "boot_id", "devices"}
                or not storage.resource(node["name"]) or not storage.uid(node["uid"]) or not storage.uid(node["boot_id"])
                or not storage.name(node["failure_domain"]) or not storage.sha(node["machine_id_sha256"]) or not isinstance(node["devices"], list) or not 1 <= len(node["devices"]) <= 8): raise ValueError("Pin the host identity, boot, failure domain and exact disks")
        for device in node["devices"]:
            if (not isinstance(device, dict) or set(device) != {"by_id", "major", "minor", "bytes", "identity_sha256"}
                    or not isinstance(device["by_id"], str) or not re.fullmatch(r"/dev/disk/by-id/[A-Za-z0-9][A-Za-z0-9_.:+-]{5,159}", device["by_id"])
                    or re.search(r"-part[0-9]+$", device["by_id"])): raise ValueError("Only explicit stable whole-device paths are supported")
        probe.validate(scope(plan, node))
    for field in ("name", "uid", "failure_domain", "machine_id_sha256", "boot_id"):
        if len({n[field] for n in nodes}) != len(nodes): raise ValueError("Storage hosts and failure domains must be independent")
    identities = [d["identity_sha256"] for n in nodes for d in n["devices"]]
    if len(set(identities)) != len(identities): raise ValueError("An underlying device cannot be assigned to two OSD hosts")
    resources = plan["resources"]
    if not isinstance(resources, dict) or set(resources) != {"mon", "mgr", "osd"}: raise ValueError("Size each provider service explicitly")
    for value in resources.values(): storage.resource_budget(value)
    memory = resources["osd"]["requests"]["memory"]
    if int(memory[:-2]) * (1024 ** 2 if memory.endswith("Mi") else 1024 ** 3) < 4 * 1024 ** 3: raise ValueError("Reserve at least 4 GiB per OSD")


def scope(plan, node):
    return {"schema_version": 1, "cluster_id": plan["cluster_id"], "node_name": node["name"], "machine_id_sha256": node["machine_id_sha256"], "boot_id": node["boot_id"],
        "timeout_seconds": plan["probe_timeout_seconds"], "devices": [{"path": "/devices/osd-" + str(i), **{k: v for k, v in d.items() if k != "by_id"}} for i, d in enumerate(node["devices"])]}


def schema():
    raw = read_bounded_bytes(ROOT / "schemas/rook/v1.21.2/cluster.json", max_bytes=256 * 1024)
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256: raise ValueError("The reviewed provider schema changed")
    return loads_strict_json(raw)


def cluster(plan):
    def placement(role):
        return {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": sorted(n["name"] for n in plan["nodes"])}]}]}},
            **({"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": plan["domain_label"], "labelSelector": {"matchLabels": {"app": "rook-ceph-" + role}}}]}} if role in {"mon", "mgr"} else {})}
    return {"apiVersion": "ceph.rook.io/v1", "kind": "CephCluster", "metadata": {"name": plan["name"], "namespace": plan["namespace"], "labels": {OWNER: plan["name"]}}, "spec": {
        "cephVersion": {"image": plan["images"]["ceph"], "allowUnsupported": False}, "dataDirHostPath": "/var/lib/rook/" + plan["cluster_id"],
        "mon": {"count": 3, "allowMultiplePerNode": False}, "mgr": {"count": 2, "allowMultiplePerNode": False, "modules": [{"name": "rook", "enabled": False}]},
        "dashboard": {"enabled": False}, "network": {"hostNetwork": False, "ipFamily": "IPv4", "connections": {"encryption": {"enabled": True}, "requireMsgr2": True}},
        "security": {"cephx": {"csi": {"keyType": "aes"}}}, "placement": {role: placement(role) for role in ("all", "mon", "mgr")},
        "resources": plan["resources"], "skipUpgradeChecks": False, "continueUpgradeAfterChecksEvenIfNotHealthy": False, "upgradeOSDRequiresHealthyPGs": True,
        "removeOSDsIfOutAndSafeToRemove": False, "disruptionManagement": {"managePodBudgets": True, "osdMaintenanceTimeout": 30},
        "cleanupPolicy": {"confirmation": "", "allowUninstallWithVolumes": False, "wipeDevicesFromOtherClusters": False},
        "storage": {"useAllNodes": False, "useAllDevices": False, "config": {"osdsPerDevice": "1", "encryptedDevice": "true"},
            "nodes": [{"name": n["name"], "devices": [{"name": d["by_id"]} for d in n["devices"]]} for n in plan["nodes"]],
            "nearFullRatio": 0.70, "backfillFullRatio": 0.80, "fullRatio": 0.85}}}


def probe_objects(plan, operation):
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation): raise ValueError("Bind probes to one original operation")
    program = standalone_script(ROOT / "scripts/ceph_device_probe.py", "strict_json")
    name = plan["name"] + "-probe-" + operation[:8]
    def metadata(value): return {"name": value, "namespace": plan["namespace"], "labels": {OWNER: plan["name"], "platform.gitops/ceph-probe": operation}}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(name), "immutable": True,
        "data": {"probe.py": program, **{"scope-" + str(i) + ".json": storage.canonical(scope(plan, node)) for i, node in enumerate(plan["nodes"])}}}
    jobs = []
    for i, node in enumerate(plan["nodes"]):
        mounts = [{"name": "program", "mountPath": "/program", "readOnly": True}]
        volumes = [{"name": "program", "configMap": {"name": name, "defaultMode": 0o444}}]
        sources = [("machine-id", "/etc/machine-id", "/host/machine-id", "File"), ("boot-id", "/proc/sys/kernel/random/boot_id", "/host/boot-id", "File"),
            ("mounts", "/proc/1/mountinfo", "/host/mountinfo", "File"), ("sys", "/sys", "/host/sys", "Directory"), ("rook", "/var/lib/rook", "/host/rook", "DirectoryOrCreate")]
        sources += [("disk-" + str(j), disk["by_id"], "/devices/osd-" + str(j), "BlockDevice") for j, disk in enumerate(node["devices"])]
        for volume, host, target, kind in sources:
            mounts.append({"name": volume, "mountPath": target, "readOnly": True}); volumes.append({"name": volume, "hostPath": {"path": host, "type": kind}})
        labels = {**metadata(name)["labels"], "platform.gitops/ceph-probe-node": str(i)}
        jobs.append({"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata(name + "-" + str(i)), "spec": {"backoffLimit": 0, "manualSelector": True, "selector": {"matchLabels": labels}, "activeDeadlineSeconds": plan["probe_timeout_seconds"] + 60,
            "template": {"metadata": {"labels": labels}, "spec": {"nodeName": node["name"], "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
                "securityContext": {"runAsUser": 0, "runAsGroup": 0, "seccompProfile": {"type": "Unconfined"}}, "containers": [{"name": "probe", "image": plan["images"]["probe"], "imagePullPolicy": "IfNotPresent",
                    "command": ["python3", "-I", "-B", "/program/probe.py", "--scope", "/program/scope-" + str(i) + ".json", "--probe"],
                    "env": [{"name": "PROBE_NODE_NAME", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "spec.nodeName"}}}], "volumeMounts": mounts,
                    # A hostPath bind does not grant a device-cgroup allowance.
                    # Standard CRI access to these raw devices requires an
                    # explicitly reviewed privileged probe, never a fallback.
                    "securityContext": {"readOnlyRootFilesystem": True, "privileged": True, "allowPrivilegeEscalation": True},
                    "resources": {"requests": {"cpu": "250m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "128Mi"}}}], "volumes": volumes}}}})
    return [config, *jobs]


def render(plan, runtime):
    validate(plan, runtime); schema()
    if yaml.__version__ != "6.0.3": raise ValueError("Use the reviewed PyYAML 6.0.3 renderer")
    plan = loads_strict_json(storage.canonical(plan))
    # Preview names are placeholders; installation will bind one random original
    # operation before server admission and create-only requests are reviewed.
    files = {"ceph-cluster.yaml": yaml.safe_dump(cluster(plan), sort_keys=False),
        "device-probes-preview.yaml": yaml.safe_dump_all(probe_objects(plan, "0" * 32), sort_keys=False)}
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "schema_sha256": SCHEMA_SHA256,
        "files_sha256": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}}
    files["provider-contract.json"] = storage.canonical(contract) + "\n"
    files["render-status.json"] = storage.canonical({"rendered": True, "guarded_cluster_creation_implemented": True, "operator_installation_implemented": False,
        "operator_bootstrap_helper": "scripts/deploy_rook_bootstrap.py",
        "remaining": ["live-operator-csi-reconciliation", "live-provider-storage-io-and-failure-acceptance"], **UNVERIFIED}) + "\n"
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
        print("Ceph provider preparation refused; private inputs were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
