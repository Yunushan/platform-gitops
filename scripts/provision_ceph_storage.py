#!/usr/bin/env python3
"""Create new replicated pools, CephFS and Retain classes on a pinned prepared provider."""

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, resource_name, verify_target
import render_ceph_storage as policy
import render_ceph_provider as cluster_policy
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/ceph-operation"
CONTRACT = "platform.gitops/ceph-contract"


def toolset():
    root = Path(__file__).resolve().parent; found, pending = {}, [Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_bytes(root / (name + ".py"), max_bytes=2 * 1024 ** 2); found[name] = hashlib.sha256(raw).hexdigest()
        for node in ast.walk(ast.parse(raw)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for module in modules:
                first = module.split(".")[0]
                if (root / (first + ".py")).is_file() and first not in found: pending.append(first)
    return policy.fingerprint(found)


def items(client, resource, namespace=None):
    value = client.get(resource, namespace)
    if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > 4096: raise ValueError("Invalid or excessive provider collection")
    return value["items"]


def controlled(obj, kind, uid):
    owners = [o for o in obj.get("metadata", {}).get("ownerReferences", []) if o.get("controller") is True]
    return len(owners) == 1 and owners[0].get("kind") == kind and owners[0].get("uid") == uid


def condition(obj, kind): return any(c.get("type") == kind and c.get("status") == "True" for c in obj.get("status", {}).get("conditions", []))


def pod_ready(pod, images):
    spec, state = pod.get("spec", {}), pod.get("status", {})
    containers = spec.get("containers", []) + spec.get("initContainers", [])
    statuses = state.get("containerStatuses", [])
    expected = {c.get("name"): c.get("image") for c in spec.get("containers", [])}
    return (not pod.get("metadata", {}).get("deletionTimestamp") and condition(pod, "Ready") and state.get("phase") == "Running" and containers
        and all(c.get("image") in images for c in containers) and len(statuses) == len(expected) > 0 and {s.get("name") for s in statuses} == set(expected)
        and all(s.get("ready") is True and "running" in s.get("state", {}) and s.get("name") in expected
            and isinstance(s.get("imageID"), str) and re.search(r"sha256:[0-9a-f]{64}$", s["imageID"]) for s in statuses))


def workload(client, namespace, kind, config, nodes, *, minimum=1, spread=False, reviewed_nodes_only=False):
    current = client.get(kind + "/" + config["name"], namespace) or {}
    meta, spec, state = (current.get(k, {}) for k in ("metadata", "spec", "status"))
    images = config["images"]
    pod_spec = spec.get("template", {}).get("spec", {})
    if (not policy.uid(meta.get("uid")) or meta.get("uid") != config["uid"] or meta.get("deletionTimestamp") or type(meta.get("generation")) is not int or state.get("observedGeneration") != meta["generation"]
            or any(c.get("image") not in images for c in pod_spec.get("containers", []) + pod_spec.get("initContainers", []))): raise ValueError("The original provider workload changed")
    if kind == "deployment":
        desired = spec.get("replicas", 1)
        if not policy.integer(desired, minimum, 32) or any(state.get(k) != desired for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")): raise ValueError("The provider deployment is not current and ready")
        revision = meta.get("annotations", {}).get("deployment.kubernetes.io/revision")
        if not isinstance(revision, str) or not revision.isdecimal(): raise ValueError("The current provider revision is missing")
        parents = {r.get("metadata", {}).get("uid") for r in items(client, "replicasets", namespace) if controlled(r, "Deployment", config["uid"])
            and r.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision") == revision}
        pods = [p for p in items(client, "pods", namespace) if any(controlled(p, "ReplicaSet", uid) for uid in parents)]
    else:
        desired = state.get("desiredNumberScheduled")
        if (not policy.integer(desired, len(nodes), 4096) or state.get("numberMisscheduled", 0) != 0
                or any(state.get(k) != desired for k in ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable"))): raise ValueError("The CSI node service is not current and ready")
        pods = [p for p in items(client, "pods", namespace) if controlled(p, "DaemonSet", config["uid"])]
    if len(pods) != desired or any(not policy.uid(p.get("metadata", {}).get("uid")) or not pod_ready(p, images) for p in pods): raise ValueError("Provider pods are unavailable or have unreviewed images")
    if reviewed_nodes_only and (any(p.get("spec", {}).get("nodeName") not in nodes for p in pods) or kind == "daemonset" and desired != len(nodes)): raise ValueError("CSI placement extends beyond the reviewed access-node set")
    domains = {nodes[p["spec"]["nodeName"]] for p in pods if p.get("spec", {}).get("nodeName") in nodes}
    if spread and len(domains) < minimum: raise ValueError("Redundant access services must span reviewed failure domains")
    if kind == "daemonset" and not set(nodes).issubset({p["spec"].get("nodeName") for p in pods}): raise ValueError("CSI is missing a reviewed storage node")
    return pods


def node_domains(client, selected, label):
    domains = {}
    for expected in selected:
        node = client.get("node/" + expected["name"]) or {}; meta, spec, status = (node.get(k, {}) for k in ("metadata", "spec", "status"))
        kernel = re.match(r"([0-9]+)[.]([0-9]+)", status.get("nodeInfo", {}).get("kernelVersion", ""))
        if (meta.get("uid") != expected["uid"] or meta.get("deletionTimestamp") or not condition(node, "Ready") or spec.get("unschedulable")
                or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in spec.get("taints", [])) or status.get("nodeInfo", {}).get("operatingSystem") != "linux"
                or not kernel or tuple(map(int, kernel.groups())) < (5, 11)
                or meta.get("labels", {}).get(label) != expected["failure_domain"] or meta.get("labels", {}).get("kubernetes.io/hostname") != expected["name"]): raise ValueError("A reviewed node identity, domain or scheduling policy changed")
        domains[expected["name"]] = expected["failure_domain"]
    return domains


def operator_ready(client, operator, provider_namespace, domains):
    namespace = operator["namespace"]; current = client.get("namespace/" + namespace) or {}; meta = current.get("metadata", {})
    if meta.get("uid") != operator["namespace_uid"] or meta.get("deletionTimestamp"): raise ValueError("The original operator namespace identity changed")
    crd = client.get("customresourcedefinition/cephclusters.ceph.rook.io") or {}
    versions = [v for v in crd.get("spec", {}).get("versions", []) if v.get("name") == "v1" and v.get("served") is True and v.get("storage") is True]
    if (crd.get("metadata", {}).get("uid") != operator["cluster_crd_uid"] or not condition(crd, "Established") or len(versions) != 1
            or policy.fingerprint(versions[0].get("schema", {}).get("openAPIV3Schema")) != policy.fingerprint(cluster_policy.schema())): raise ValueError("The original Rook cluster API identity or schema changed")
    workload(client, namespace, "deployment", {"name": operator["deployment"], "uid": operator["uid"], "images": [operator["image"]]}, domains)
    deployment = client.get("deployment/" + operator["deployment"], namespace)
    for name, expected in operator["configmaps"].items():
        obj = client.get("configmap/" + name, namespace) or {}; meta = obj.get("metadata", {})
        if (meta.get("uid") != expected["uid"] or meta.get("deletionTimestamp") or obj.get("immutable") is not True or obj.get("binaryData")
                or policy.fingerprint(obj.get("data", {})) != expected["data_sha256"]): raise ValueError("The original immutable operator/CSI configuration changed")
        if name == "rook-ceph-operator-config":
            required = {"ROOK_ENABLE_DISCOVERY_DAEMON": "false", "ROOK_DISABLE_DEVICE_HOTPLUG": "true", "ROOK_CEPH_ALLOW_LOOP_DEVICES": "false", "ROOK_DELETE_UNUSED_CRUSH_RULES": "false",
                "ROOK_CURRENT_NAMESPACE_ONLY": str(namespace == provider_namespace).lower()}
            if any(obj.get("data", {}).get(k) != v for k, v in required.items()): raise ValueError("Operator discovery or namespace watch policy changed")
    if namespace != provider_namespace:
        variables = [e for c in deployment["spec"]["template"]["spec"]["containers"] for e in c.get("env", []) if e.get("name") == "ROOK_CURRENT_NAMESPACE_ONLY"]
        if variables != [{"name": "ROOK_CURRENT_NAMESPACE_ONLY", "value": "false"}]: raise ValueError("Explicitly allow the reviewed operator to watch the provider namespace")


def prerequisites(client, plan):
    verify_target(client, plan["target"]); p = plan["provider"]; namespace = p["namespace"]
    ns = client.get("namespace/" + namespace) or {}
    if ns.get("metadata", {}).get("uid") != p["namespace_uid"] or ns.get("metadata", {}).get("deletionTimestamp"): raise ValueError("Provider namespace identity changed")
    cluster = client.get("cephcluster.ceph.rook.io/" + p["cluster"], namespace) or {}
    spec, status = cluster.get("spec", {}), cluster.get("status", {})
    if (cluster.get("metadata", {}).get("uid") != p["cluster_uid"] or cluster.get("metadata", {}).get("deletionTimestamp") or policy.fingerprint(spec) != p["spec_sha256"]
            or type(cluster.get("metadata", {}).get("generation")) is not int or status.get("observedGeneration") != cluster["metadata"]["generation"]
            or spec.get("cephVersion", {}).get("image") != p["image"] or status.get("phase") != "Ready" or status.get("ceph", {}).get("fsid") != p["fsid"]): raise ValueError("The prepared Ceph provider changed or is not ready")
    definitions = policy.schemas()
    for name, expected in definitions.items():
        crd = client.get("customresourcedefinition/" + name) or {}
        versions = [v for v in crd.get("spec", {}).get("versions", []) if v.get("name") == "v1" and v.get("served") is True and v.get("storage") is True]
        if crd.get("metadata", {}).get("uid") != p["crds"][name] or not condition(crd, "Established") or len(versions) != 1 or policy.fingerprint(versions[0].get("schema", {}).get("openAPIV3Schema")) != policy.fingerprint(expected): raise ValueError("The Rook storage API identity or schema changed")
    nodes = node_domains(client, plan["nodes"], p["domain_label"])
    access = node_domains(client, plan["access_nodes"], p["domain_label"])
    operator = p["operator"]
    operator_ready(client, operator, namespace, access)
    provider_deployments = items(client, "deployments", namespace)
    for role, count in (("mon", 3), ("mgr", 2)):
        selected = [d for d in provider_deployments if controlled(d, "CephCluster", p["cluster_uid"]) and d.get("metadata", {}).get("labels", {}).get("app") == "rook-ceph-" + role]
        if len(selected) != count: raise ValueError("Require exactly three monitors and two managers")
        domains = set()
        for deployment in selected:
            config = {"name": deployment["metadata"]["name"], "uid": deployment["metadata"]["uid"], "images": [p["image"], operator["image"]]}
            pods = workload(client, namespace, "deployment", config, nodes)
            if len(pods) != 1 or pods[0]["spec"]["nodeName"] not in nodes: raise ValueError("Invalid provider control process placement")
            domains.add(nodes[pods[0]["spec"]["nodeName"]])
        if len(domains) != count: raise ValueError("Provider control processes must span distinct failure domains")
    for kind, config in plan["csi"].items():
        csi_namespace = client.get("namespace/" + config["namespace"]) or {}
        if csi_namespace.get("metadata", {}).get("uid") != config["namespace_uid"] or csi_namespace.get("metadata", {}).get("deletionTimestamp"): raise ValueError("The CSI namespace identity changed")
        driver = client.get("csidriver/" + config["driver"]) or {}
        if (driver.get("metadata", {}).get("uid") != config["driver_uid"] or driver.get("metadata", {}).get("deletionTimestamp") or "Persistent" not in driver.get("spec", {}).get("volumeLifecycleModes", [])
                or kind == "rbd" and driver.get("spec", {}).get("attachRequired") is not True): raise ValueError("CSI driver identity or attachment policy changed")
        workload(client, config["namespace"], "deployment", config["controller"], access, minimum=2, spread=True, reviewed_nodes_only=True)
        workload(client, config["namespace"], "daemonset", config["node_plugin"], access, reviewed_nodes_only=True)
    tools = p["toolbox"]
    pods = workload(client, namespace, "deployment", {"name": tools["deployment"], "uid": tools["uid"], "images": [tools["image"]]}, nodes)
    found = [pod for pod in pods if pod.get("metadata", {}).get("name") == tools["pod"] and pod.get("metadata", {}).get("uid") == tools["pod_uid"]]
    if len(found) != 1 or {c.get("name") for c in found[0]["spec"].get("containers", [])} != {tools["container"]}: raise ValueError("The original toolbox container changed")
    return nodes


READS = {("status",), ("versions",), ("mon", "dump"), ("osd", "dump"), ("osd", "df", "tree"), ("osd", "crush", "dump"), ("fs", "dump")}


def ceph(client, plan, command):
    if tuple(command) not in READS: raise ValueError("Only fixed read-only Ceph observations are supported")
    p, tools = plan["provider"], plan["provider"]["toolbox"]
    def handle():
        obj = client.get("pod/" + tools["pod"], p["namespace"]) or {}
        if obj.get("metadata", {}).get("uid") != tools["pod_uid"] or not pod_ready(obj, [tools["image"]]): raise ValueError("The original toolbox process is unavailable")
        states = obj["status"]["containerStatuses"]
        if any(not isinstance(s.get("containerID"), str) or not s["containerID"] or not policy.integer(s.get("restartCount"), 0, 1000000) for s in states): raise ValueError("The toolbox process identity is missing")
        return [(s["containerID"], s["restartCount"], s["imageID"]) for s in states]
    before = handle()
    raw = client.call(["exec", "pod/" + tools["pod"], "--namespace", p["namespace"], "--container", tools["container"], "--",
        "env", "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", "ceph", "--conf=/etc/ceph/ceph.conf", *command, "--format=json"], seconds=30)
    if handle() != before: raise ValueError("The toolbox process changed during observation")
    value = loads_strict_json(raw)
    if not isinstance(value, dict): raise ValueError("Invalid Ceph observation")
    return value


def observe(client, plan):
    status = ceph(client, plan, ["status"]); p = plan["provider"]
    monmap = ceph(client, plan, ["mon", "dump"]); mon = monmap.get("mons", []); mgr = status.get("mgrmap", {})
    if (status.get("fsid") != p["fsid"] or status.get("health", {}).get("status") != "HEALTH_OK" or len(mon) != 3
            or monmap.get("fsid") != p["fsid"] or monmap.get("epoch") != status.get("monmap", {}).get("epoch")
            or set(status.get("quorum_names", [])) != {m.get("name") for m in mon} or mgr.get("available") is not True or not mgr.get("active_name") or len(mgr.get("standbys", [])) < 1):
        raise ValueError("Require healthy original Ceph identity, monitor quorum and redundant managers")
    versions = ceph(client, plan, ["versions"])
    for role in ("mon", "mgr", "osd"):
        values = versions.get(role, {})
        if not isinstance(values, dict) or not values or any(not key.startswith("ceph version " + p["ceph_version"] + " ") or not policy.integer(count, 1, 4096) for key, count in values.items()): raise ValueError("Mixed or unreviewed daemon versions")
    dump, crush, usage = (ceph(client, plan, args) for args in (["osd", "dump"], ["osd", "crush", "dump"], ["osd", "df", "tree"]))
    osds = dump.get("osds", []); buckets = crush.get("buckets", []); rows = usage.get("nodes", [])
    if not 4 <= len(osds) <= 4096 or len(rows) > 8192 or len(buckets) > 8192: raise ValueError("Invalid Ceph storage inventory")
    identities = {row.get("osd") for row in osds}
    if len(identities) != len(osds) or any(type(i) is not int or i < 0 for i in identities) or any(row.get("up") != 1 or row.get("in") != 1 for row in osds): raise ValueError("All declared OSDs must be up and in")
    hosts = {}; covered = set()
    for node in plan["nodes"]:
        found = [b for b in buckets if b.get("type_name") == "host" and b.get("name") == node["crush_host"]]
        if len(found) != 1: raise ValueError("The CRUSH host map differs")
        members = {v.get("id") for v in found[0].get("items", [])}
        if not members or not members.issubset(identities) or members & covered: raise ValueError("Storage hosts must own distinct active OSDs")
        covered |= members; total = used = 0
        for identity in members:
            matched = [r for r in rows if r.get("id") == identity and r.get("type") == "osd"]
            if len(matched) != 1: raise ValueError("The live capacity map is incomplete")
            row = matched[0]
            if not policy.integer(row.get("kb"), 1, 2 ** 60) or not policy.integer(row.get("kb_used"), 0, row["kb"]) or not policy.integer(row.get("kb_avail"), 0, row["kb"]): raise ValueError("Invalid observed OSD capacity")
            # Use the greater usage observation, preserving a conservative bound.
            raw, consumed = row["kb"] * 1024, max(row["kb_used"], row["kb"] - row["kb_avail"]) * 1024
            if consumed * 100 >= raw * (100 - plan["capacity"]["recovery_reserve_percent"]): raise ValueError("An OSD lacks the reviewed free-space reserve")
            total += raw; used += consumed
        hosts[node["crush_host"]] = (total, used)
    if covered != identities: raise ValueError("The plan must account for the complete active storage cluster")
    root = [b for b in buckets if b.get("name") == "default" and b.get("type_name") == "root"]
    host_ids = {b["id"] for b in buckets if b.get("type_name") == "host" and b.get("name") in hosts}
    if len(root) != 1 or {v.get("id") for v in root[0].get("items", [])} != host_ids: raise ValueError("Review alternate or hierarchical CRUSH roots separately")
    requested = sum(plan["capacity"][k] for k in ("rbd_bytes", "cephfs_data_bytes", "cephfs_metadata_bytes"))
    reserve = plan["capacity"]["recovery_reserve_percent"]
    total, used = sum(v[0] for v in hosts.values()), sum(v[1] for v in hosts.values())
    largest_raw, largest_used = max(v[0] for v in hosts.values()), max(v[1] for v in hosts.values())
    if ((used + 3 * requested) * 100 > (total - largest_raw) * (100 - reserve)
            or any((v[1] + requested + largest_used) * 100 > v[0] * (100 - reserve) for v in hosts.values())):
        raise ValueError("Insufficient worst-host-loss recovery and quota headroom")
    return {"status": status, "osd": dump, "crush": crush, "filesystem": ceph(client, plan, ["fs", "dump"]),
        "capacity": {"raw_bytes": total, "used_bytes": used, "planned_logical_bytes": requested, "largest_host_bytes": largest_raw}}


def material(obj): return {k: v for k, v in obj.items() if k not in {"metadata", "status"}}


def admitted(expected, actual):
    def includes(a, b):
        if isinstance(a, dict): return isinstance(b, dict) and all(k in b and includes(v, b[k]) for k, v in a.items())
        if isinstance(a, list): return isinstance(b, list) and len(a) == len(b) and all(includes(x, y) for x, y in zip(a, b))
        return type(a) is type(b) and a == b
    if not includes(expected, actual): raise ValueError("Admission weakened or changed the reviewed storage policy")
    return policy.fingerprint(material(actual))


def key(obj): return obj["kind"] + "/" + obj["metadata"]["name"]
def get(client, obj): return client.get(resource_name({**obj["metadata"], "kind": obj["kind"], "apiVersion": obj["apiVersion"]}), obj["metadata"].get("namespace"), optional=True)


def fresh(client, plan, objects, view):
    if any(get(client, obj) is not None for obj in objects): raise ValueError("New storage cannot adopt existing Kubernetes resources")
    for obj in objects: unused_backend(plan, obj, view)


def unused_backend(plan, obj, view):
    names = plan["names"]
    wanted = {names["block_pool"]} if obj["kind"] == "CephBlockPool" else {names["filesystem"] + "-metadata", names["filesystem"] + "-data"} if obj["kind"] == "CephFilesystem" else set()
    if wanted & {p.get("pool_name") for p in view["osd"].get("pools", [])} or obj["kind"] == "CephFilesystem" and any(f.get("mdsmap", {}).get("fs_name") == names["filesystem"] for f in view["filesystem"].get("filesystems", [])):
        raise ValueError("Existing Ceph data identities require explicit recovery")


def owned(obj, expected, state, entry):
    annotations = obj.get("metadata", {}).get("annotations", {})
    if (not policy.uid(obj.get("metadata", {}).get("uid")) or obj.get("metadata", {}).get("deletionTimestamp") or admitted(expected, obj) != entry["admitted_sha256"]
            or annotations.get(OPERATION) != state["operation_id"] or annotations.get(CONTRACT) != state["plan_sha256"]
            or entry.get("uid") is not None and obj["metadata"]["uid"] != entry["uid"]): raise ValueError("The original storage object handle or policy changed")


def pool_ready(row, expected, crush):
    rules = [rule for rule in crush.get("rules", []) if rule.get("rule_id") == row.get("crush_rule")]
    if len(rules) != 1: return False
    steps = rules[0].get("steps", [])
    return (row.get("type") == 1 and row.get("size") == 3 and row.get("min_size") == 2 and row.get("quota_max_bytes") == expected["quotas"]["maxBytes"] and len(rules) == 1
        and len(steps) == 3 and steps[0].get("op") == "take" and steps[0].get("item_name") == "default" and steps[0].get("item") == next(b["id"] for b in crush["buckets"] if b.get("name") == "default")
        and steps[1].get("op") in {"chooseleaf_firstn", "chooseleaf_indep"} and steps[1].get("type") == "host" and steps[1].get("num") == 0 and steps[2] == {"op": "emit"})


def ready(client, plan, objects, state):
    for obj in objects:
        current = get(client, obj)
        if current is None: raise ValueError("An original storage object disappeared; never recreate it")
        owned(current, obj, state, state["objects"][key(obj)])
        if obj["kind"] != "StorageClass" and current.get("status", {}).get("phase") != "Ready": return False
    view = observe(client, plan); names = plan["names"]
    expected_pools = {names["block_pool"]: objects[0]["spec"], names["filesystem"] + "-metadata": objects[1]["spec"]["metadataPool"], names["filesystem"] + "-data": objects[1]["spec"]["dataPools"][0]}
    pools = {}
    for name, expected in expected_pools.items():
        matches = [p for p in view["osd"].get("pools", []) if p.get("pool_name") == name]
        if len(matches) != 1 or not pool_ready(matches[0], expected, view["crush"]): raise ValueError("The runtime pool quota, replication or CRUSH policy differs")
        pools[name] = matches[0]["pool"]
    filesystems = [f for f in view["filesystem"].get("filesystems", []) if f.get("mdsmap", {}).get("fs_name") == names["filesystem"]]
    if len(filesystems) != 1: raise ValueError("The new filesystem identity is missing or duplicated")
    mds = filesystems[0]["mdsmap"]
    if mds.get("metadata_pool") != pools[names["filesystem"] + "-metadata"] or mds.get("data_pools") != [pools[names["filesystem"] + "-data"]] or mds.get("max_mds") != 1: raise ValueError("The filesystem uses unexpected pools or active count")
    nodes = {n["name"]: n["failure_domain"] for n in plan["nodes"]}; namespace = plan["provider"]["namespace"]
    fs_uid = state["objects"][key(objects[1])]["uid"]
    deployments = [d for d in items(client, "deployments", namespace) if controlled(d, "CephFilesystem", fs_uid)]
    if len(deployments) != 2: return False
    serving, domains = set(), set()
    for deployment in deployments:
        meta = deployment["metadata"]; config = {"name": meta["name"], "uid": meta["uid"], "images": [plan["provider"]["image"]]}
        pods = workload(client, namespace, "deployment", config, nodes)
        if len(pods) != 1: raise ValueError("Expected one process per metadata-service deployment")
        pod = pods[0]; identity = pod.get("metadata", {}).get("labels", {}).get("ceph_daemon_id")
        if identity not in {names["filesystem"] + "-a", names["filesystem"] + "-b"} or pod["spec"]["nodeName"] not in nodes: raise ValueError("Unexpected metadata process identity or placement")
        serving.add(identity); domains.add(nodes[pod["spec"]["nodeName"]])
    states = [v for v in mds.get("info", {}).values() if v.get("name") in serving] + [v for v in view["filesystem"].get("standbys", []) if v.get("name") in serving]
    if (len(serving) != 2 or len(domains) != 2 or len(states) != 2 or {s.get("name") for s in states} != serving or sum(s.get("state") == "up:active" for s in states) != 1
            or sum(s.get("state", "up:standby") in {"up:standby", "up:standby-replay"} for s in states) != 1): raise ValueError("The active and standby metadata services are not established across failure domains")
    return True


@contextmanager
def lock(directory):
    path = directory / "operation.lock"
    if path.is_symlink(): raise ValueError("Untrusted operation lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Untrusted operation lock")
        if os.name == "nt":
            import msvcrt
            if info.st_size == 0: os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def operate(plan, runtime, *, mode="local", output=None, wait_seconds=120, client=None):
    objects = policy.objects(plan, runtime); policy.schemas(); source = toolset()
    result = {"local_inputs_verified": True, "server_checked": False, "storage_resources_provisioned": False, **policy.UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-server", "create", "resume"} or output is None or not policy.integer(wait_seconds, 5, 1800): raise ValueError("Select an explicit bounded provider operation")
    if mode != "check-server" and plan["admitted_sha256"] is None: raise ValueError("Review and pin server admission before creation")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private operation directory")
    if mode != "resume": resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = resolved / "ceph-storage-proof.json"
    with lock(resolved):
        if mode == "resume":
            info = receipt.lstat(); directory_info = resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077 or directory_info.st_uid != os.geteuid() or directory_info.st_mode & 0o077): raise ValueError("The original private journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "create" or state.get("plan_sha256") != policy.fingerprint(plan)
                    or state.get("runtime") != runtime or state.get("toolset_sha256") != source or not isinstance(state.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"])
                    or not isinstance(state.get("objects"), dict) or not set(state["objects"]).issubset({key(o) for o in objects})
                    or any(state.get(k) is not False for k in policy.UNVERIFIED)): raise ValueError("Resume only the original matching private operation")
            for entry in state["objects"].values():
                if not isinstance(entry, dict) or set(entry) != {"admitted_sha256", "uid"} or not policy.sha(entry["admitted_sha256"]) or entry["uid"] is not None and not policy.uid(entry["uid"]): raise ValueError("Invalid original resource handle")
        else:
            state = {"schema_version": 1, "action": mode, "runtime": runtime, "operation_id": uuid4().hex, "plan_sha256": policy.fingerprint(plan), "toolset_sha256": source, "objects": {}, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
            atomic_write_text(receipt, policy.canonical(state))
            if os.name == "posix":
                for directory in (resolved, resolved.parent):
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
        record("started")
        for obj in objects: obj["metadata"]["annotations"] = {OPERATION: state["operation_id"], CONTRACT: state["plan_sha256"]}
        try:
            network = client or Client(plan["target"])
            with network.frozen():
                prerequisites(network, plan); view = observe(network, plan)
                record("provider-observed", capacity=view["capacity"])
                if not state["objects"]: fresh(network, plan, objects, view)
                dry = {}
                for obj in objects:
                    identity = key(obj)
                    if identity in state["objects"]:
                        existing = get(network, obj)
                        if existing is None: raise ValueError("An uncertain or missing original create cannot be repeated")
                        owned(existing, obj, state, state["objects"][identity]); state["objects"][identity]["uid"] = existing["metadata"]["uid"]
                        dry[identity] = state["objects"][identity]["admitted_sha256"]
                    else:
                        if get(network, obj) is not None: raise ValueError("A pending resource name became occupied")
                        unused_backend(plan, obj, view)
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=policy.canonical(obj).encode()))
                        dry[identity] = admitted(obj, response)
                        atomic_write_text(resolved / (obj["kind"].lower() + "-" + obj["metadata"]["name"] + ".admitted.json"), policy.canonical(material(response)))
                observed = policy.fingerprint(dry)
                record("admission-checked", observed_admitted_sha256=observed, server_checked=True)
                if plan["admitted_sha256"] is not None and plan["admitted_sha256"] != observed: raise ValueError("The server-admitted policy changed")
                if mode == "check-server":
                    record("server-check-completed"); return {**result, "server_checked": True, "admitted_sha256": observed}
                prerequisites(network, plan); observe(network, plan)
                for obj in objects:
                    identity = key(obj)
                    if identity in state["objects"]: continue
                    if get(network, obj) is not None: raise ValueError("A new storage name became occupied before creation")
                    if obj["kind"] in {"CephBlockPool", "CephFilesystem"}: unused_backend(plan, obj, observe(network, plan))
                    state["objects"][identity] = {"admitted_sha256": dry[identity], "uid": None}
                    record("before-create", pending=identity)
                    current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=policy.canonical(obj).encode()))
                    if policy.uid(current.get("metadata", {}).get("uid")):
                        state["objects"][identity]["uid"] = current["metadata"]["uid"]; record("create-response-observed", pending=identity)
                    owned(current, obj, state, state["objects"][identity]); state["objects"][identity]["uid"] = current["metadata"]["uid"]
                    record("created", pending=None)
                deadline = time.monotonic() + wait_seconds
                while not ready(network, plan, objects, state):
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original resources again; do not repeat creation")
                    time.sleep(2)
                prerequisites(network, plan)
                if not ready(network, plan, objects, state) or toolset() != source: raise ValueError("Final storage evidence changed")
                result.update(server_checked=True, storage_resources_provisioned=True); record("completed", **result); return result
        except BaseException:
            record("failed", storage_resources_provisioned=False, error_code="ceph-storage-provisioning-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=policy.RUNTIMES, required=True)
    actions = parser.add_mutually_exclusive_group()
    for action in ("check-server", "create", "resume"): actions.add_argument("--" + action, action="store_true")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024))
        mode = "check-server" if args.check_server else "create" if args.create else "resume" if args.resume else "local"
        print(json.dumps(operate(plan, args.runtime, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Ceph storage operation refused; retain private evidence and the original resource handles. Private details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
