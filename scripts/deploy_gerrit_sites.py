#!/usr/bin/env python3
"""Explicit fresh Kubernetes Gerrit pair activation; never init, upgrade or adopt sites."""

from __future__ import annotations

import argparse
import ast
import copy
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
from kubernetes_management import Client, validate_target, verify_target
import deploy_ceph_provider as provider
import provision_ceph_storage as shared
import render_ceph_storage as values
import render_gerrit_sites as sites
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/gerrit-operation"
CONTRACT = "platform.gitops/gerrit-contract"
UNVERIFIED = {key: False for key in ("live_plugin_compatibility_verified", "peer_forwarding_verified",
    "git_writes_verified", "failover_verified", "ha_verified", "full_stack_verified")}


def validate(plan):
    fields = {"schema_version", "target", "namespace_uid", "site_plan", "nodes", "domain_label", "claims", "admitted_sha256"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Use the finite Gerrit activation plan")
    validate_target(plan["target"]); sites.validate(plan["site_plan"])
    if plan["site_plan"]["schema_version"] != 2 or not values.uid(plan["namespace_uid"]):
        raise ValueError("Activation requires version 2 guarded sites and an original namespace UID")
    if not isinstance(plan["domain_label"], str) or not re.fullmatch(r"[a-z0-9.-]+/[A-Za-z0-9_.-]+", plan["domain_label"]):
        raise ValueError("Declare the serving failure-domain label")
    names = {s["name"] for s in plan["site_plan"]["sites"]}
    if not isinstance(plan["nodes"], dict) or set(plan["nodes"]) != names: raise ValueError("Pin one node per primary")
    for node in plan["nodes"].values():
        if (not isinstance(node, dict) or set(node) != {"name", "uid", "failure_domain"} or not values.resource(node["name"])
                or not values.uid(node["uid"]) or not isinstance(node["failure_domain"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,61}[A-Za-z0-9]|[A-Za-z0-9]", node["failure_domain"])):
            raise ValueError("Pin the primary node UID and failure domain")
    if any(len({n[key] for n in plan["nodes"].values()}) != 2 for key in ("name", "uid", "failure_domain")):
        raise ValueError("Primaries must use separate nodes and failure domains")
    claims = {plan["site_plan"]["shared_claim"], *(s["claim"] for s in plan["site_plan"]["sites"])}
    if not isinstance(plan["claims"], dict) or set(plan["claims"]) != claims: raise ValueError("Pin all three existing claims")
    for claim in plan["claims"].values():
        if (not isinstance(claim, dict) or set(claim) != {"uid", "pv", "pv_uid", "csi_sha256"}
                or not values.uid(claim["uid"]) or not values.resource(claim["pv"]) or not values.uid(claim["pv_uid"])
                or not values.sha(claim["csi_sha256"])): raise ValueError("Pin original PVC/PV and CSI identities")
    for field in ("uid", "pv", "pv_uid"):
        if len({c[field] for c in plan["claims"].values()}) != 3: raise ValueError("All claim/backend identities must be separate")
    if plan["admitted_sha256"] is not None and not values.sha(plan["admitted_sha256"]): raise ValueError("Pin reviewed server admission")
    return plan


def toolset():
    root = Path(__file__).parent; found, pending = {}, [Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_bytes(root / (name + ".py"), max_bytes=2 * 1024 ** 2)
        found[name] = hashlib.sha256(raw).hexdigest()
        for node in ast.walk(ast.parse(raw)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for module in modules:
                first = module.split(".")[0]
                if (root / (first + ".py")).is_file() and first not in found: pending.append(first)
    return values.fingerprint(found)


def objects(plan, runtime):
    validate(plan)
    if runtime not in {"rke2", "k3s", "k8s"}: raise ValueError("Use the separate native/Docker lifecycle for those runtimes")
    phases = {"guards": [], "daemons": []}
    for site in plan["site_plan"]["sites"]:
        config, deployment, service = sites.deployment(plan["site_plan"], site)
        pod = deployment["spec"]["template"]["spec"]
        node = plan["nodes"][site["name"]]
        pod["nodeSelector"] = {"kubernetes.io/hostname": node["name"], plan["domain_label"]: node["failure_domain"]}
        pod["restartPolicy"] = "Always"
        pod["enableServiceLinks"] = False
        # Ownership/permissions are prepared separately, never recursively repaired by activation.
        pod["securityContext"].pop("fsGroup", None)
        container = pod["containers"][0]; container["imagePullPolicy"] = "IfNotPresent"
        for port in container["ports"]: port["protocol"] = "TCP"
        for key in ("startupProbe", "readinessProbe"):
            container[key].update(timeoutSeconds=1, successThreshold=1)
            container[key].setdefault("failureThreshold", 3)
        for volume in pod["volumes"]:
            if "configMap" in volume: volume["configMap"]["defaultMode"] = 0o644
        deployment["spec"].update(revisionHistoryLimit=10, progressDeadlineSeconds=600)
        guard_pod = copy.deepcopy(pod); guard_pod["restartPolicy"] = "Never"
        guard_pod.pop("affinity", None)
        guard = guard_pod["containers"][0]; guard.pop("ports"); guard.pop("startupProbe"); guard.pop("readinessProbe")
        # Retain the exact storage/metadata/health checks, removing only the final daemon exec.
        startup = config["data"]["start.sh"]
        prefix, marker, _ = startup.rpartition("exec java ")
        if not marker: raise ValueError("Unexpected startup program")
        config["data"]["verify.sh"] = prefix
        guard["command"] = ["/bin/sh", "/opt/gerrit-startup/verify.sh"]
        for volume in guard_pod["volumes"]:
            if volume["name"] == "site": volume["persistentVolumeClaim"]["readOnly"] = True
        for mount in guard["volumeMounts"]:
            if mount["name"] == "site": mount["readOnly"] = True
        # Shared mount remains RW: the existing guard must verify a writable mount.
        # The check program itself does not write, initialize or execute Java.
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": site["name"] + "-verify", "namespace": plan["site_plan"]["namespace"]},
            "spec": {"parallelism": 1, "completions": 1, "backoffLimit": 0, "activeDeadlineSeconds": 600,
                "completionMode": "NonIndexed", "suspend": False, "manualSelector": False,
                "template": {"metadata": {"labels": {"platform.gitops/gerrit-site-check": site["name"]}}, "spec": guard_pod}}}
        phases["guards"].extend((config, job)); phases["daemons"].extend((service, deployment))
    return phases


def prerequisites(client, plan, state):
    verify_target(client, plan["target"])
    namespace = plan["site_plan"]["namespace"]
    ns = client.get("namespace/" + namespace) or {}
    if ns.get("metadata", {}).get("uid") != plan["namespace_uid"] or ns.get("metadata", {}).get("deletionTimestamp"):
        raise ValueError("Original Gerrit namespace changed")
    for node in plan["nodes"].values():
        actual = client.get("node/" + node["name"]) or {}; meta, spec = actual.get("metadata", {}), actual.get("spec", {})
        if (meta.get("uid") != node["uid"] or meta.get("deletionTimestamp") or spec.get("unschedulable")
                or not shared.condition(actual, "Ready") or actual.get("status", {}).get("nodeInfo", {}).get("operatingSystem") != "linux"
                or meta.get("labels", {}).get("kubernetes.io/hostname") != node["name"] or meta.get("labels", {}).get(plan["domain_label"]) != node["failure_domain"]
                or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in spec.get("taints", []))): raise ValueError("A primary node or failure domain changed")
    backends = set()
    for name, expected in plan["claims"].items():
        claim = client.get("persistentvolumeclaim/" + name, namespace) or {}
        pv = client.get("persistentvolume/" + expected["pv"]) or {}
        spec, pvspec = claim.get("spec", {}), pv.get("spec", {})
        ref, csi = pvspec.get("claimRef", {}), pvspec.get("csi", {})
        modes = spec.get("accessModes", [])
        if (claim.get("metadata", {}).get("uid") != expected["uid"] or claim.get("metadata", {}).get("deletionTimestamp")
                or pv.get("metadata", {}).get("uid") != expected["pv_uid"] or pv.get("metadata", {}).get("deletionTimestamp")
                or claim.get("status", {}).get("phase") != "Bound" or pv.get("status", {}).get("phase") != "Bound"
                or spec.get("volumeName") != expected["pv"] or spec.get("volumeMode", "Filesystem") != "Filesystem"
                or pvspec.get("volumeMode", "Filesystem") != "Filesystem" or ref.get("uid") != expected["uid"]
                or ref.get("namespace") != namespace or ref.get("name") != name or values.fingerprint(csi) != expected["csi_sha256"]
                or csi.get("readOnly") or not csi.get("volumeHandle") or not csi.get("driver")
                or not set(modes).issubset(set(pvspec.get("accessModes", [])))): raise ValueError("A prepared claim or original CSI volume changed")
        identity = (csi["driver"], csi["volumeHandle"])
        if identity in backends: raise ValueError("Distinct PV names cannot alias the same CSI backend")
        backends.add(identity)
        if name == plan["site_plan"]["shared_claim"]:
            if "ReadWriteMany" not in modes or not csi["driver"].endswith(".cephfs.csi.ceph.com"): raise ValueError("Shared Git requires the prepared RWX CephFS claim")
        elif modes not in (["ReadWriteOnce"], ["ReadWriteOncePod"]): raise ValueError("Use a separate single-writer site claim")
    allowed_jobs = {entry["uid"] for key, entry in state["objects"].items() if key.startswith("Job/") and entry["uid"]}
    allowed_deployments = {entry["uid"] for key, entry in state["objects"].items() if key.startswith("Deployment/") and entry["uid"]}
    sets = {r.get("metadata", {}).get("uid") for r in shared.items(client, "replicasets", namespace)
        if any(shared.controlled(r, "Deployment", uid) for uid in allowed_deployments)}
    for pod in shared.items(client, "pods", namespace):
        if not any(v.get("persistentVolumeClaim", {}).get("claimName") in plan["claims"] for v in pod.get("spec", {}).get("volumes", [])): continue
        if not (any(shared.controlled(pod, "Job", uid) for uid in allowed_jobs)
                or any(shared.controlled(pod, "ReplicaSet", uid) for uid in sets)):
            raise ValueError("A prepared claim is in use by an unrelated Pod; release it explicitly first")


def admitted(expected, actual):
    shared.admitted(expected, actual)
    normalized = copy.deepcopy(actual)
    if expected["kind"] in {"Job", "Deployment"}:
        wanted, observed = expected["spec"]["template"]["spec"], normalized["spec"]["template"]["spec"]
        if provider.pod_policy(wanted) != provider.pod_policy(observed): raise ValueError("Admission injected unreviewed Gerrit runtime policy")
        if expected["kind"] == "Job":
            uid = actual.get("metadata", {}).get("uid")
            labels = normalized["spec"]["template"]["metadata"].get("labels", {})
            generated = {"controller-uid": uid, "batch.kubernetes.io/controller-uid": uid,
                "job-name": expected["metadata"]["name"], "batch.kubernetes.io/job-name": expected["metadata"]["name"]}
            selector = normalized["spec"].pop("selector", {}).get("matchLabels", {})
            for key, value in selector.items():
                if key not in {"controller-uid", "batch.kubernetes.io/controller-uid"} or not uid or value != uid:
                    raise ValueError("Unreviewed Job selector")
            for key, value in generated.items():
                if key in labels:
                    if not value or labels.pop(key) != value: raise ValueError("Unreviewed Job controller labels")
    if expected["kind"] == "Service":
        for key in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy"): normalized["spec"].pop(key, None)
    return values.fingerprint(shared.material(normalized))


def owned(client, obj, state):
    current = shared.get(client, obj)
    entry = state["objects"][shared.key(obj)]
    meta = (current or {}).get("metadata", {}); annotations = meta.get("annotations", {})
    if (not values.uid(meta.get("uid")) or meta.get("deletionTimestamp")
            or entry["uid"] is not None and meta["uid"] != entry["uid"]
            or annotations.get(OPERATION) != state["operation_id"] or annotations.get(CONTRACT) != state["plan_sha256"]
            or admitted(obj, current) != entry["admitted_sha256"]): raise ValueError("Original Gerrit resource disappeared or changed; never recreate it")
    entry["uid"] = meta["uid"]
    return current


def original_pod(expected, actual, node):
    observed = copy.deepcopy(actual.get("spec", {}))
    if observed.pop("nodeName", None) != node: raise ValueError("Original Pod ran on another node")
    if provider.pod_policy(expected) != provider.pod_policy(observed): raise ValueError("Pod admission changed the reviewed Gerrit runtime")


def checks_complete(client, plan, phases, state):
    namespace = plan["site_plan"]["namespace"]; complete = True
    pods = shared.items(client, "pods", namespace)
    for obj in phases["guards"]:
        actual = owned(client, obj, state)
        if obj["kind"] != "Job": continue
        if shared.condition(actual, "Failed") or actual.get("status", {}).get("failed", 0): raise ValueError("An original site check failed; do not rerun it automatically")
        candidates = [p for p in pods if shared.controlled(p, "Job", actual["metadata"]["uid"])]
        if len(candidates) > 1: raise ValueError("More than one original site check Pod; no repeated checks accepted")
        if not candidates: complete = False; continue
        pod = candidates[0]; key = shared.key(obj); meta = pod.get("metadata", {}); status = pod.get("status", {})
        if not values.uid(meta.get("uid")) or meta.get("deletionTimestamp"): raise ValueError("A site check Pod disappeared")
        previous = state["pods"].setdefault(key, meta["uid"])
        if previous != meta["uid"]: raise ValueError("Original site check Pod was replaced")
        site_name = obj["metadata"]["name"].removesuffix("-verify")
        if not pod.get("spec", {}).get("nodeName") and status.get("phase") == "Pending": complete = False; continue
        original_pod(obj["spec"]["template"]["spec"], pod, plan["nodes"][site_name]["name"])
        if status.get("phase") == "Failed": raise ValueError("An original site check failed")
        if not shared.condition(actual, "Complete") or actual.get("status", {}).get("succeeded") != 1 or status.get("phase") != "Succeeded": complete = False; continue
        states = status.get("containerStatuses", [])
        if (len(states) != 1 or states[0].get("name") != "gerrit" or states[0].get("restartCount") != 0
                or states[0].get("state", {}).get("terminated", {}).get("exitCode") != 0
                or not re.search(r"sha256:[0-9a-f]{64}$", str(states[0].get("imageID", "")))): raise ValueError("Original check did not exit successfully once")
    return complete


def daemons_ready(client, plan, phases, state):
    nodes = {n["name"]: n["failure_domain"] for n in plan["nodes"].values()}
    ready = True
    for obj in phases["daemons"]:
        actual = owned(client, obj, state)
        if obj["kind"] != "Deployment": continue
        status = actual.get("status", {})
        if status.get("observedGeneration") != actual["metadata"].get("generation") or any(status.get(k) != 1 for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")):
            ready = False; continue
        pods = shared.workload(client, plan["site_plan"]["namespace"], "deployment",
            {"name": obj["metadata"]["name"], "uid": actual["metadata"]["uid"], "images": [plan["site_plan"]["java_image"]]}, nodes, reviewed_nodes_only=True)
        if len(pods) != 1 or pods[0]["spec"]["nodeName"] != plan["nodes"][obj["metadata"]["name"]]["name"]:
            raise ValueError("Primary placement differs from its reviewed node")
        original_pod(obj["spec"]["template"]["spec"], pods[0], plan["nodes"][obj["metadata"]["name"]]["name"])
        key = shared.key(obj); identity = {"uid": pods[0]["metadata"]["uid"], "containers": pods[0]["status"]["containerStatuses"]}
        # Store only original process identities; status timestamps/addresses are not a contract.
        identity["containers"] = [{k: c.get(k) for k in ("name", "containerID", "imageID", "restartCount")} for c in identity["containers"]]
        if any(not c["containerID"] or c["restartCount"] != 0 for c in identity["containers"]): raise ValueError("Primary restarted before initial acceptance")
        if state["pods"].setdefault(key, identity) != identity: raise ValueError("Original primary process was replaced")
    return ready


def operate(plan, runtime, *, mode="local", output=None, wait_seconds=900, client=None):
    phases = objects(plan, runtime); source = toolset(); all_objects = sum(phases.values(), [])
    result = {"local_inputs_verified": True, "server_checked": False, "site_checks_completed": False, "daemons_ready": False, **UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-server", "create", "resume"} or output is None or not values.integer(wait_seconds, 5, 3600): raise ValueError("Select an explicit bounded activation")
    if mode != "check-server" and plan["admitted_sha256"] is None: raise ValueError("Review/pin server admission before activation")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private operation directory")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = directory / "gerrit-activation.json"
    with shared.lock(directory):
        if mode == "resume":
            info, parent = receipt.lstat(), directory.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and
                    (info.st_uid != os.geteuid() or info.st_mode & 0o077 or parent.st_uid != os.geteuid() or parent.st_mode & 0o077)):
                raise ValueError("Original journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=512 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state.get("schema_version") != 1 or state.get("action") != "create" or state.get("runtime") != runtime
                    or state.get("plan_sha256") != values.fingerprint(plan) or state.get("toolset_sha256") != source
                    or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("operation_id")))
                    or not isinstance(state.get("objects"), dict) or not set(state["objects"]).issubset({shared.key(o) for o in all_objects})
                    or not isinstance(state.get("pods"), dict) or not set(state["pods"]).issubset({shared.key(o) for o in all_objects if o["kind"] in {"Job", "Deployment"}})
                    or any(state.get(k) is not False for k in UNVERIFIED)):
                raise ValueError("Resume only the original matching Gerrit activation")
            for entry in state["objects"].values():
                if (not isinstance(entry, dict) or set(entry) != {"uid", "admitted_sha256"} or not values.sha(entry["admitted_sha256"])
                        or entry["uid"] is not None and not values.uid(entry["uid"])): raise ValueError("Invalid original resource handle")
        else:
            state = {"schema_version": 1, "action": mode, "runtime": runtime, "operation_id": uuid4().hex,
                "plan_sha256": values.fingerprint(plan), "toolset_sha256": source, "objects": {}, "pods": {}, **result}
        def record(phase, **changes):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **changes)
            atomic_write_text(receipt, values.canonical(state))
        record("started", server_checked=False, site_checks_completed=False, daemons_ready=False)
        for obj in all_objects:
            obj["metadata"].setdefault("annotations", {}).update({OPERATION: state["operation_id"], CONTRACT: state["plan_sha256"]})
        try:
            network = client or Client(plan["target"])
            with network.frozen():
                # Resolve uncertain create responses before checking consumers of the claims.
                for obj in all_objects:
                    if shared.key(obj) in state["objects"]: owned(network, obj, state)
                prerequisites(network, plan, state)
                hashes = {}
                for obj in all_objects:
                    key = shared.key(obj)
                    if key in state["objects"]: hashes[key] = state["objects"][key]["admitted_sha256"]; continue
                    if shared.get(network, obj) is not None: raise ValueError("Fresh activation cannot adopt existing names")
                    response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=values.canonical(obj).encode()))
                    hashes[key] = admitted(obj, response)
                    atomic_write_text(directory / (hashlib.sha256(key.encode()).hexdigest()[:16] + ".admitted.json"), values.canonical(shared.material(response)))
                observed = values.fingerprint(hashes)
                record("admission-checked", observed_admitted_sha256=observed, server_checked=True)
                if plan["admitted_sha256"] is not None and observed != plan["admitted_sha256"]: raise ValueError("Reviewed Gerrit admission changed")
                if mode == "check-server": return {**result, "server_checked": True, "admitted_sha256": observed}
                def create(obj):
                    key = shared.key(obj)
                    if key in state["objects"]: owned(network, obj, state); return
                    if toolset() != source or shared.get(network, obj) is not None: raise ValueError("Source or a pending resource name changed")
                    prerequisites(network, plan, state)
                    state["objects"][key] = {"uid": None, "admitted_sha256": hashes[key]}; record("before-create", pending=key)
                    response = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=values.canonical(obj).encode()))
                    uid = response.get("metadata", {}).get("uid")
                    if values.uid(uid): state["objects"][key]["uid"] = uid; record("create-response-observed")
                    owned(network, obj, state); record("created", pending=None)
                deadline = time.monotonic() + wait_seconds
                for obj in phases["guards"]: create(obj)
                while not checks_complete(network, plan, phases, state):
                    record("waiting-for-original-site-checks")
                    prerequisites(network, plan, state)
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original site checks again")
                    time.sleep(2)
                record("site-checks-completed", site_checks_completed=True)
                for obj in phases["daemons"]: create(obj)
                while not daemons_ready(network, plan, phases, state):
                    record("waiting-for-original-daemons")
                    prerequisites(network, plan, state)
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original daemons again")
                    time.sleep(2)
                prerequisites(network, plan, state)
                record("activation-ready", daemons_ready=True)
                return {**result, "server_checked": True, "site_checks_completed": True, "daemons_ready": True}
        except Exception:
            record("stopped-for-review")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=("rke2", "k3s", "k8s"), required=True)
    parser.add_argument("--mode", choices=("local", "check-server", "create", "resume"), default="local")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=900); parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        if args.mode in {"create", "resume"} and args.confirm != "ACTIVATE_PREPARED_GERRIT_PAIR": raise ValueError("Confirm prepared pair activation")
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024))
        result = operate(plan, args.runtime, mode=args.mode, output=args.output_dir, wait_seconds=args.wait_seconds)
        print(json.dumps(result)); return 0
    except (ValueError, OSError, RuntimeError, TypeError, KeyError):
        print("Gerrit activation stopped; retain the original journal and inspect private diagnostics.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
