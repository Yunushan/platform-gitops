#!/usr/bin/env python3
"""Render finite replicated storage on an explicitly selected prepared Rook provider."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

import yaml

from bounded_file import read_bounded_bytes
from kubernetes_management import validate_target
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("rke2", "k3s", "k8s", "docker", "raw")
OWNER = "platform.gitops/ceph-storage"
SCHEMA_SHA256 = "aa04978acbf546d74b50b91fb66b38de71d182634b3e81fb2b46ce2a2f2cdaaf"
CRDS = ("cephblockpools.ceph.rook.io", "cephfilesystems.ceph.rook.io")
UNVERIFIED = {k: False for k in ("provider_installed", "client_mounts_verified", "data_roundtrip_verified", "node_loss_verified", "recovery_verified", "ha_verified", "full_stack_verified")}


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def sha(value): return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
def name(value): return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9-]{0,38}[a-z0-9]", value))
def resource(value): return isinstance(value, str) and len(value) <= 253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split("."))
def uid(value):
    try: return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError): return False
def image(value): return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", value))
def integer(value, low, high): return type(value) is int and low <= value <= high


def resource_budget(value):
    if not isinstance(value, dict) or set(value) != {"requests", "limits"}: raise ValueError("Declare metadata-service resource requests and limits")
    quantities = {}
    for kind, row in value.items():
        if not isinstance(row, dict) or set(row) != {"cpu", "memory"} or not isinstance(row["cpu"], str) or not re.fullmatch(r"[1-9][0-9]{0,5}m?", row["cpu"]) or not isinstance(row["memory"], str) or not re.fullmatch(r"[1-9][0-9]{0,5}(?:Mi|Gi)", row["memory"]): raise ValueError("Use finite CPU and binary-memory quantities")
        cpu = int(row["cpu"][:-1]) if row["cpu"].endswith("m") else int(row["cpu"]) * 1000
        memory = int(row["memory"][:-2]) * (1024 ** 2 if row["memory"].endswith("Mi") else 1024 ** 3)
        if not 100 <= cpu <= 64000 or not 512 * 1024 ** 2 <= memory <= 256 * 1024 ** 3: raise ValueError("Metadata-service budgets are outside the reviewed bounds")
        quantities[kind] = (cpu, memory)
    if any(a > b for a, b in zip(quantities["requests"], quantities["limits"])): raise ValueError("Resource limits cannot be below requests")


def validate_operator(operator):
    if (not isinstance(operator, dict) or set(operator) != {"namespace", "namespace_uid", "deployment", "uid", "image", "cluster_crd_uid", "configmaps"}
            or not resource(operator["namespace"]) or not resource(operator["deployment"]) or not image(operator["image"])
            or any(not uid(operator[k]) for k in ("namespace_uid", "uid", "cluster_crd_uid"))): raise ValueError("Pin the original operator namespace, workload and cluster API")
    configs = operator["configmaps"]
    if (not isinstance(configs, dict) or set(configs) != {"rook-ceph-operator-config", "rook-csi-operator-image-set-configmap"}
            or any(not isinstance(v, dict) or set(v) != {"uid", "data_sha256"} or not uid(v["uid"]) or not sha(v["data_sha256"]) for v in configs.values())):
        raise ValueError("Pin the original immutable operator and CSI image configuration")


def validate(plan, runtime):
    if (runtime not in RUNTIMES or not isinstance(plan, dict)
            or set(plan) != {"schema_version", "target", "provider", "nodes", "access_nodes", "csi", "capacity", "names", "mds_resources", "admitted_sha256"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 2): raise ValueError("Use a complete reviewed v2 storage scope")
    validate_target(plan["target"])
    resource_budget(plan["mds_resources"])
    p = plan["provider"]
    if (not isinstance(p, dict) or set(p) != {"namespace", "namespace_uid", "cluster", "cluster_uid", "fsid", "spec_sha256", "image", "ceph_version", "domain_label", "operator", "toolbox", "crds"}
            or any(not name(p[k]) for k in ("namespace", "cluster")) or any(not uid(p[k]) for k in ("namespace_uid", "cluster_uid", "fsid"))
            or not sha(p["spec_sha256"]) or not image(p["image"]) or not isinstance(p["ceph_version"], str) or not re.fullmatch(r"20[.]2[.][0-9]+", p["ceph_version"])
            or p["domain_label"] not in {"topology.kubernetes.io/zone", "topology.rook.io/rack"}
            or not isinstance(p["crds"], dict) or set(p["crds"]) != set(CRDS) or any(not uid(v) for v in p["crds"].values())):
        raise ValueError("Pin the prepared provider, namespace, Ceph 20.2 identity and Rook v1.21.2 APIs")
    validate_operator(p["operator"])
    if p["operator"]["namespace"] == p["namespace"] and p["operator"]["namespace_uid"] != p["namespace_uid"]: raise ValueError("One namespace cannot have conflicting UIDs")
    item = p["toolbox"]
    if (not isinstance(item, dict) or set(item) != {"deployment", "uid", "image", "pod", "pod_uid", "container"} or not resource(item["deployment"]) or not uid(item["uid"])
            or not image(item["image"]) or not resource(item["pod"]) or not uid(item["pod_uid"]) or not name(item["container"]) or item["image"] != p["image"]): raise ValueError("Pin the original read-only toolbox process")
    nodes = plan["nodes"]
    if not isinstance(nodes, list) or not 4 <= len(nodes) <= 32: raise ValueError("Reserve at least four distinct storage failure domains for three-copy recovery")
    for node in nodes:
        if (not isinstance(node, dict) or set(node) != {"name", "uid", "failure_domain", "crush_host"} or not uid(node["uid"])
                or not name(node["failure_domain"]) or any(not resource(node[k]) for k in ("name", "crush_host"))): raise ValueError("Pin each storage node and CRUSH host")
    if any(len({n[k] for n in nodes}) != len(nodes) for k in ("name", "uid", "failure_domain", "crush_host")): raise ValueError("Storage identities and physical domains must be distinct")
    access = plan["access_nodes"]
    if not isinstance(access, list) or not len(nodes) <= len(access) <= 128: raise ValueError("Declare the complete reviewed CSI access-node set")
    for node in access:
        if (not isinstance(node, dict) or set(node) != {"name", "uid", "failure_domain"} or not resource(node["name"]) or not uid(node["uid"]) or not name(node["failure_domain"])): raise ValueError("Pin every CSI access node identity and domain")
    if any(len({n[k] for n in access}) != len(access) for k in ("name", "uid")): raise ValueError("CSI access nodes must be distinct")
    access_by_name = {n["name"]: n for n in access}
    if any(access_by_name.get(n["name"]) != {k: n[k] for k in ("name", "uid", "failure_domain")} for n in nodes): raise ValueError("OSD hosts must retain matching identities in the CSI access inventory")
    csi = plan["csi"]
    if not isinstance(csi, dict) or set(csi) != {"rbd", "cephfs"}: raise ValueError("Require both storage access paths")
    namespace_uids = {p["namespace"]: p["namespace_uid"], p["operator"]["namespace"]: p["operator"]["namespace_uid"]}
    for kind, item in csi.items():
        if (not isinstance(item, dict) or set(item) != {"namespace", "namespace_uid", "driver", "controller", "node_plugin", "driver_uid"}
                or not name(item["namespace"]) or not uid(item["namespace_uid"]) or not uid(item["driver_uid"]) or not resource(item["driver"])
                or not item["driver"].endswith("." + kind + ".csi.ceph.com")): raise ValueError("Pin each CSI namespace and driver")
        if item["namespace"] in namespace_uids and namespace_uids[item["namespace"]] != item["namespace_uid"]: raise ValueError("One CSI namespace cannot have conflicting UIDs")
        namespace_uids[item["namespace"]] = item["namespace_uid"]
        for workload in (item["controller"], item["node_plugin"]):
            if (not isinstance(workload, dict) or set(workload) != {"name", "uid", "images"} or not resource(workload["name"]) or not uid(workload["uid"])
                    or not isinstance(workload["images"], list) or not 1 <= len(workload["images"]) <= 12 or len(set(workload["images"])) != len(workload["images"])
                    or any(not image(v) for v in workload["images"])): raise ValueError("Pin all CSI workload images and handles")
    capacity = plan["capacity"]
    if (not isinstance(capacity, dict) or set(capacity) != {"rbd_bytes", "cephfs_data_bytes", "cephfs_metadata_bytes", "recovery_reserve_percent"}
            or any(not integer(capacity[k], 1024 ** 3, 1024 ** 5) for k in ("rbd_bytes", "cephfs_data_bytes", "cephfs_metadata_bytes"))
            or not integer(capacity["recovery_reserve_percent"], 20, 50)): raise ValueError("Set finite pool quotas and deliberate recovery headroom")
    names = plan["names"]
    if (not isinstance(names, dict) or set(names) != {"block_pool", "filesystem", "rbd_class", "cephfs_class"}
            or any(not name(v) for v in names.values()) or len(set(names.values())) != 4
            or plan["admitted_sha256"] is not None and not sha(plan["admitted_sha256"])): raise ValueError("Separate the new resource names and reviewed admission pin")


def schemas():
    raw = read_bounded_bytes(ROOT / "schemas/rook/v1.21.2/storage.json", max_bytes=128 * 1024)
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256: raise ValueError("The reviewed Rook API schemas changed")
    return loads_strict_json(raw)


def pool(quota):
    return {"failureDomain": "host", "crushRoot": "default", "replicated": {"size": 3, "requireSafeReplicaSize": True},
        "parameters": {"min_size": "2", "pg_autoscale_mode": "on", "compression_mode": "none"}, "quotas": {"maxBytes": quota}}


def objects(plan, runtime):
    validate(plan, runtime)
    namespace, names, capacity = plan["provider"]["namespace"], plan["names"], plan["capacity"]
    def metadata(value, namespaced=True): return {"name": value, **({"namespace": namespace} if namespaced else {}), "labels": {OWNER: names["block_pool"]}}
    block = {"apiVersion": "ceph.rook.io/v1", "kind": "CephBlockPool", "metadata": metadata(names["block_pool"]), "spec": {**pool(capacity["rbd_bytes"]), "application": "rbd"}}
    fs = {"apiVersion": "ceph.rook.io/v1", "kind": "CephFilesystem", "metadata": metadata(names["filesystem"]), "spec": {
        "metadataPool": pool(capacity["cephfs_metadata_bytes"]), "dataPools": [{"name": "data", **pool(capacity["cephfs_data_bytes"])}],
        "preserveFilesystemOnDelete": True, "preservePoolsOnDelete": True,
        "metadataServer": {"activeCount": 1, "activeStandby": True, "resources": plan["mds_resources"],
            "placement": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": sorted(n["name"] for n in plan["nodes"])}]}]}},
                "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": plan["provider"]["domain_label"], "labelSelector": {"matchLabels": {"app": "rook-ceph-mds", "rook_file_system": names["filesystem"]}}}]}}}}}
    classes = []
    for kind in ("rbd", "cephfs"):
        parameters = {"clusterID": namespace}
        for action, role in (("provisioner", "provisioner"), ("controller-expand", "provisioner"), ("controller-publish", "provisioner"), ("node-stage", "node")):
            parameters["csi.storage.k8s.io/" + action + "-secret-name"] = "rook-csi-" + kind + "-" + role
            parameters["csi.storage.k8s.io/" + action + "-secret-namespace"] = namespace
        parameters.update({"pool": names["block_pool"], "imageFormat": "2", "imageFeatures": "layering", "csi.storage.k8s.io/fstype": "ext4",
            "mapOptions": "krbd:ms_mode=secure,fsid=" + plan["provider"]["fsid"], "tryOtherMounters": "false"} if kind == "rbd"
            else {"fsName": names["filesystem"], "pool": names["filesystem"] + "-data", "mounter": "kernel",
                "kernelMountOptions": "ms_mode=secure,fsid=" + plan["provider"]["fsid"]})
        classes.append({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": metadata(names[kind + "_class"], False),
            "provisioner": plan["csi"][kind]["driver"], "parameters": parameters, "reclaimPolicy": "Retain", "allowVolumeExpansion": True,
            "volumeBindingMode": "WaitForFirstConsumer"})
    return [block, fs, *classes]


def render(plan, runtime):
    if yaml.__version__ != "6.0.3": raise ValueError("Use the reviewed PyYAML 6.0.3 renderer")
    plan = loads_strict_json(canonical(plan))
    resources = objects(plan, runtime); schemas()
    payload = yaml.safe_dump_all(resources, sort_keys=False)
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "schemas_sha256": SCHEMA_SHA256,
        "resources_sha256": hashlib.sha256(payload.encode()).hexdigest()}
    return {"ceph-storage.yaml": payload, "storage-contract.json": canonical(contract) + "\n",
        "render-status.json": canonical({"rendered": True, "consumer_runtime": runtime, "provider": "prepared-rook-kubernetes-cluster", **UNVERIFIED}) + "\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=RUNTIMES, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, **UNVERIFIED})); return 0
    except Exception:
        print("Ceph storage rendering refused; private inputs were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
