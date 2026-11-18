#!/usr/bin/env python3
"""Guard fresh Rook creation with pinned target, admitted policy and complete blank-device probes."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, verify_target
import provision_ceph_storage as shared
import render_ceph_provider as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/ceph-provider-operation"
CONTRACT = "platform.gitops/ceph-provider-contract"


def toolset():
    return renderer.storage.fingerprint({"shared": shared.toolset(), **{name: hashlib.sha256(read_bounded_bytes(Path(__file__).parent / name, max_bytes=256 * 1024)).hexdigest()
        for name in ("deploy_ceph_provider.py", "render_ceph_provider.py", "ceph_device_probe.py",
                     "standalone_python.py", "strict_json.py")}})


def prerequisites(client, plan):
    verify_target(client, plan["target"])
    operator = plan["operator"]
    for namespace, identity in ((plan["namespace"], plan["namespace_uid"]), (operator["namespace"], operator["namespace_uid"])):
        obj = client.get("namespace/" + namespace) or {}; meta = obj.get("metadata", {})
        if meta.get("uid") != identity or meta.get("deletionTimestamp"): raise ValueError("The original namespace identity changed")
        if namespace == plan["namespace"] and meta.get("labels", {}).get("pod-security.kubernetes.io/enforce") != "privileged": raise ValueError("Explicit privileged provider admission is required for reviewed host-device probes")
    domains = {}
    for node in plan["nodes"]:
        obj = client.get("node/" + node["name"]) or {}; meta, spec, status = (obj.get(k, {}) for k in ("metadata", "spec", "status")); info = status.get("nodeInfo", {})
        kernel = re.match(r"([0-9]+)[.]([0-9]+)", info.get("kernelVersion", ""))
        if (meta.get("uid") != node["uid"] or meta.get("deletionTimestamp") or not shared.condition(obj, "Ready") or spec.get("unschedulable")
                or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in spec.get("taints", [])) or meta.get("labels", {}).get("kubernetes.io/hostname") != node["name"]
                or meta.get("labels", {}).get(plan["domain_label"]) != node["failure_domain"] or info.get("operatingSystem") != "linux"
                or info.get("bootID") != node["boot_id"] or not isinstance(info.get("machineID"), str) or not re.fullmatch(r"[0-9a-f]{32}", info["machineID"])
                or hashlib.sha256(info["machineID"].encode()).hexdigest() != node["machine_id_sha256"] or not kernel or tuple(map(int, kernel.groups())) < (5, 11)):
            raise ValueError("The reviewed node, boot, machine identity, kernel or placement changed")
        domains[node["name"]] = node["failure_domain"]
    shared.operator_ready(client, operator, plan["namespace"], domains)
    return domains


POD_DEFAULTS = {"dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler", "terminationGracePeriodSeconds": 30, "serviceAccountName": "default", "serviceAccount": "default", "priority": 0, "preemptionPolicy": "PreemptLowerPriority"}
CONTAINER_DEFAULTS = {"terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File"}


def pod_policy(spec):
    value = loads_strict_json(renderer.storage.canonical(spec))
    for key, default in POD_DEFAULTS.items():
        if value.get(key) == default: value.pop(key)
    for container in value.get("containers", []):
        for key, default in CONTAINER_DEFAULTS.items():
            if container.get(key) == default: container.pop(key)
    defaults = [{"key": "node.kubernetes.io/" + key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300} for key in ("not-ready", "unreachable")]
    if "tolerations" in value:
        value["tolerations"] = [t for t in value["tolerations"] if t not in defaults]
        if not value["tolerations"]: value.pop("tolerations")
    return value


def finite_defaults(expected, actual, schema):
    if isinstance(expected, dict):
        if not isinstance(actual, dict): return False
        properties = schema.get("properties", {})
        for key, value in actual.items():
            if key in expected:
                if not finite_defaults(expected[key], value, properties.get(key, schema.get("additionalProperties", {}) if isinstance(schema.get("additionalProperties"), dict) else {})): return False
            elif "default" not in properties.get(key, {}) or type(value) is not type(properties[key]["default"]) or value != properties[key]["default"]: return False
        return set(expected).issubset(actual)
    if isinstance(expected, list): return isinstance(actual, list) and len(expected) == len(actual) and all(finite_defaults(a, b, schema.get("items", {})) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def admitted(expected, actual):
    identity = shared.admitted(expected, actual)
    if expected["kind"] == "Job":
        if pod_policy(expected["spec"]["template"]["spec"]) != pod_policy(actual["spec"]["template"]["spec"]): raise ValueError("Admission changed a root probe's finite device policy")
    elif expected["kind"] == "CephCluster":
        if not finite_defaults(expected["spec"], actual["spec"], renderer.schema()["properties"]["spec"]): raise ValueError("Admission injected unreviewed Ceph bootstrap or storage policy")
    elif expected.get("data") != actual.get("data") or actual.get("binaryData"): raise ValueError("The immutable probe program or scope changed")
    return identity


def fresh(client, plan, state, objects):
    clusters = shared.items(client, "cephclusters.ceph.rook.io", plan["namespace"])
    cluster = objects[-1]; known = state["objects"].get(shared.key(cluster))
    if clusters and (known is None or len(clusters) != 1 or clusters[0].get("metadata", {}).get("name") != plan["name"]): raise ValueError("A fresh provider cannot adopt an existing cluster")
    if known is not None: return
    expected_config = objects[0]["metadata"]["name"]
    approved_configs = set()
    if plan["operator"]["namespace"] == plan["namespace"]:
        approved_configs.update(plan["operator"]["configmaps"])
        # Rook v1.21 creates an empty CSI cluster list owned by the operator
        # Deployment during startup, before any CephCluster exists.
        name = "rook-ceph-csi-config"
        csi_config = client.get("configmap/" + name, plan["namespace"], optional=True)
        if (csi_config and not csi_config.get("metadata", {}).get("deletionTimestamp")
                and shared.controlled(csi_config, "Deployment", plan["operator"]["uid"])
                and not csi_config.get("binaryData") and csi_config.get("data") == {"csi-cluster-config-json": "[]"}):
            approved_configs.add(name)
    for resource in ("secrets", "configmaps"):
        rows = client.call(["get", resource, "--namespace", plan["namespace"], "-o", 'go-template={{range .items}}{{.metadata.name}}{{"\\n"}}{{end}}']).splitlines()
        if len(rows) > 4096 or any(name.startswith(("rook-ceph", "rook-csi")) and not (resource == "configmaps" and (name in approved_configs or name == expected_config and shared.key(objects[0]) in state["objects"])) for name in rows):
            raise ValueError("Existing provider identities or state require explicit recovery")


def owned(current, expected, state, entry):
    meta = current.get("metadata", {}); annotations = meta.get("annotations", {})
    if (not renderer.storage.uid(meta.get("uid")) or meta.get("deletionTimestamp") or entry.get("uid") is not None and meta["uid"] != entry["uid"]
            or annotations.get(OPERATION) != state["operation_id"] or annotations.get(CONTRACT) != state["plan_sha256"]
            or admitted(expected, current) != entry["admitted_sha256"]): raise ValueError("The original provider resource changed")


def probe_result(client, plan, job, state, index, *, program_sha256, fresh_required):
    current = shared.get(client, job)
    if current is None: raise ValueError("The original probe job disappeared")
    entry = state["objects"][shared.key(job)]; owned(current, job, state, entry)
    if current.get("status", {}).get("failed", 0) or shared.condition(current, "Failed"): raise ValueError("The original device probe failed; never retry its scope automatically")
    pods = [p for p in shared.items(client, "pods", plan["namespace"]) if shared.controlled(p, "Job", entry["uid"])]
    if len(pods) > 1: raise ValueError("The no-retry probe acquired multiple pod handles")
    if not pods: return None
    pod = pods[0]; meta, status = pod.get("metadata", {}), pod.get("status", {})
    if not renderer.storage.uid(meta.get("uid")) or meta.get("deletionTimestamp") or pod_policy(pod.get("spec", {})) != pod_policy(current["spec"]["template"]["spec"]): raise ValueError("The original device-probe pod policy changed")
    observed = state["probe_pods"].get(str(index))
    if observed is not None and observed != meta["uid"]: raise ValueError("The original device-probe pod was replaced")
    state["probe_pods"][str(index)] = meta["uid"]
    if not shared.condition(current, "Complete") or current.get("status", {}).get("succeeded") != 1: return None
    containers = status.get("containerStatuses", [])
    if status.get("phase") != "Succeeded" or len(containers) != 1 or containers[0].get("name") != "probe" or containers[0].get("restartCount") != 0 or containers[0].get("state", {}).get("terminated", {}).get("exitCode") != 0: raise ValueError("The original probe did not complete exactly once")
    if not isinstance(containers[0].get("imageID"), str) or not re.search(r"sha256:[0-9a-f]{64}$", containers[0]["imageID"]): raise ValueError("The probe runtime image identity is missing")
    raw = client.call(["logs", "pod/" + meta["name"], "--namespace", plan["namespace"], "--container", "probe", "--limit-bytes=32768"], seconds=30)
    value = loads_strict_json(raw)
    selected = renderer.scope(plan, plan["nodes"][index])
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1 or value.get("scope_sha256") != renderer.storage.fingerprint(selected) or value.get("probe_sha256") != program_sha256
            or value.get("fresh_devices_observed") is not True or any(value.get(k) is not False for k in ("fencing_verified", "provider_installed", "ha_verified", "full_stack_verified"))
            or not isinstance(value.get("devices"), list) or len(value["devices"]) != len(selected["devices"])): raise ValueError("The original probe evidence differs from its reviewed program/scope")
    for expected, result in zip(selected["devices"], value["devices"]):
        if not isinstance(result, dict) or set(result) != {"major", "minor", "bytes", "identity_sha256", "zero_sha256"} or any(type(result.get(k)) is not type(v) or result[k] != v for k, v in expected.items() if k != "path") or not renderer.storage.sha(result["zero_sha256"]): raise ValueError("The original device result differs")
    observed_at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    if observed_at.tzinfo is None or fresh_required and not -30 <= (datetime.now(timezone.utc) - observed_at).total_seconds() <= 300: raise ValueError("Fresh node probes expired before provider creation")
    again = client.get("pod/" + meta["name"], plan["namespace"])
    if again is None or again.get("metadata", {}).get("uid") != meta["uid"] or again.get("status", {}).get("containerStatuses") != containers: raise ValueError("The probe process changed while reading evidence")
    digest = renderer.storage.fingerprint(value)
    previous = state["probe_results"].get(str(index))
    if previous is not None and previous != digest: raise ValueError("Original probe evidence changed")
    state["probe_results"][str(index)] = digest
    return digest


def ready(client, plan, expected, state):
    current = shared.get(client, expected)
    if current is None: raise ValueError("The original provider is missing; never repeat an uncertain creation")
    entry = state["objects"][shared.key(expected)]; owned(current, expected, state, entry)
    status = current.get("status", {}); ceph = status.get("ceph", {})
    if status.get("phase") != "Ready" or status.get("observedGeneration") != current["metadata"].get("generation") or ceph.get("health") != "HEALTH_OK": return None
    fsid = ceph.get("fsid")
    if not renderer.storage.uid(fsid) or state.get("fsid") is not None and state["fsid"] != fsid: raise ValueError("The original provider FSID changed")
    nodes = {n["name"]: n["failure_domain"] for n in plan["nodes"]}
    deployments = shared.items(client, "deployments", plan["namespace"])
    for role, count in (("mon", 3), ("mgr", 2), ("osd", sum(len(n["devices"]) for n in plan["nodes"]))):
        versions = ceph.get("versions", {}).get(role, {})
        if not isinstance(versions, dict) or not versions or sum(versions.values()) != count or any(not k.startswith("ceph version " + plan["ceph_version"] + " ") or not renderer.storage.integer(v, 1, 256) for k, v in versions.items()): return None
        selected = [d for d in deployments if shared.controlled(d, "CephCluster", entry["uid"]) and d.get("metadata", {}).get("labels", {}).get("app") == "rook-ceph-" + role]
        if len(selected) != count: return None
        placements = []
        for deployment in selected:
            config = {"name": deployment["metadata"]["name"], "uid": deployment["metadata"]["uid"], "images": [plan["images"]["ceph"], plan["operator"]["image"]]}
            pods = shared.workload(client, plan["namespace"], "deployment", config, nodes)
            if len(pods) != 1 or pods[0]["spec"]["nodeName"] not in nodes: raise ValueError("Provider processes escaped their original placement")
            placements.append(pods[0]["spec"]["nodeName"])
        if role in {"mon", "mgr"} and len({nodes[n] for n in placements}) != count or role == "osd" and any(placements.count(n["name"]) != len(n["devices"]) for n in plan["nodes"]): raise ValueError("Provider process redundancy or OSD placement differs")
    return fsid


def operate(plan, runtime, *, mode="local", output=None, wait_seconds=600, client=None):
    renderer.validate(plan, runtime); renderer.schema(); source = toolset(); operation = plan["cluster_id"].replace("-", "")
    objects = [*renderer.probe_objects(plan, operation), renderer.cluster(plan)]
    # The isolated interpreter hashes the executable ConfigMap file, including
    # its embedded dependency. Its source template has a different digest.
    program_sha256 = hashlib.sha256(objects[0]["data"]["probe.py"].encode("utf-8")).hexdigest()
    result = {"local_inputs_verified": True, "server_checked": False, "provider_resource_ready": False, **renderer.UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-server", "create", "resume"} or output is None or not renderer.storage.integer(wait_seconds, 5, 86400): raise ValueError("Select a bounded explicit provider operation")
    if mode != "check-server" and plan["admitted_sha256"] is None: raise ValueError("First review and pin actual admission")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a new private operation directory")
    if mode != "resume": resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = resolved / "ceph-provider-proof.json"
    with shared.lock(resolved):
        if mode == "resume":
            info, directory_info = receipt.lstat(), resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077 or directory_info.st_uid != os.geteuid() or directory_info.st_mode & 0o077): raise ValueError("The original private journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "create"
                    or state.get("plan_sha256") != renderer.storage.fingerprint(plan) or state.get("runtime") != runtime or state.get("operation_id") != operation or state.get("toolset_sha256") != source
                    or not isinstance(state.get("objects"), dict) or not set(state["objects"]).issubset({shared.key(o) for o in objects}) or type(state.get("probes_verified")) is not bool
                    or not isinstance(state.get("probe_pods"), dict) or not isinstance(state.get("probe_results"), dict) or any(state.get(k) is not False for k in renderer.UNVERIFIED if k != "devices_observed")):
                raise ValueError("Resume only the original matching provider operation")
            for entry in state["objects"].values():
                if not isinstance(entry, dict) or set(entry) != {"admitted_sha256", "uid"} or not renderer.storage.sha(entry["admitted_sha256"]) or entry["uid"] is not None and not renderer.storage.uid(entry["uid"]): raise ValueError("Invalid original provider handle")
        else:
            state = {"schema_version": 1, "action": mode, "runtime": runtime, "plan_sha256": renderer.storage.fingerprint(plan), "operation_id": operation, "toolset_sha256": source,
                "objects": {}, "probe_pods": {}, "probe_results": {}, "probes_verified": False, "fsid": None, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
            atomic_write_text(receipt, renderer.storage.canonical(state))
            if sys.platform == "linux":
                for directory in (resolved, resolved.parent):
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
        record("started")
        for obj in objects: obj["metadata"]["annotations"] = {OPERATION: operation, CONTRACT: state["plan_sha256"]}
        try:
            network = client or Client(plan["target"])
            with network.frozen():
                prerequisites(network, plan); fresh(network, plan, state, objects)
                hashes = {}
                for obj in objects:
                    identity = shared.key(obj); current = shared.get(network, obj)
                    if identity in state["objects"]:
                        if current is None: raise ValueError("A missing uncertain provider request cannot be repeated")
                        owned(current, obj, state, state["objects"][identity]); state["objects"][identity]["uid"] = current["metadata"]["uid"]; hashes[identity] = state["objects"][identity]["admitted_sha256"]
                    else:
                        if current is not None: raise ValueError("A new provider resource name became occupied")
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                        hashes[identity] = admitted(obj, response)
                        atomic_write_text(resolved / (obj["kind"].lower() + "-" + obj["metadata"]["name"] + ".admitted.json"), renderer.storage.canonical(shared.material(response)))
                observed = renderer.storage.fingerprint(hashes); record("admission-checked", observed_admitted_sha256=observed, server_checked=True)
                if plan["admitted_sha256"] is not None and observed != plan["admitted_sha256"]: raise ValueError("Provider admission changed from the reviewed snapshot")
                if mode == "check-server":
                    record("server-check-completed"); return {**result, "server_checked": True, "admitted_sha256": observed}
                def create(obj):
                    identity = shared.key(obj)
                    if identity in state["objects"]: return
                    if toolset() != source: raise ValueError("Provider/probe source changed before a privileged operation")
                    if shared.get(network, obj) is not None: raise ValueError("A pending provider name became occupied")
                    state["objects"][identity] = {"admitted_sha256": hashes[identity], "uid": None}; record("before-create", pending=identity)
                    current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                    if renderer.storage.uid(current.get("metadata", {}).get("uid")): state["objects"][identity]["uid"] = current["metadata"]["uid"]; record("create-response-observed")
                    owned(current, obj, state, state["objects"][identity]); record("created", pending=None)
                for obj in objects[:-1]: create(obj)
                cluster = objects[-1]; attempted = shared.key(cluster) in state["objects"]
                if attempted and not state["probes_verified"]: raise ValueError("An original cluster intent is missing its prior probe evidence")
                deadline = time.monotonic() + wait_seconds
                while True:
                    results = [probe_result(network, plan, job, state, i, program_sha256=program_sha256,
                                            fresh_required=not attempted) for i, job in enumerate(objects[1:-1])]
                    record("observing-original-probes")
                    if all(value is not None for value in results): break
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original probes again; never create replacement probes automatically")
                    time.sleep(2)
                if not attempted:
                    prerequisites(network, plan); fresh(network, plan, state, objects)
                    if toolset() != source: raise ValueError("Provider source changed before disk initialization")
                    record("probes-verified", probes_verified=True, devices_observed=True)
                    create(cluster)
                while True:
                    fsid = ready(network, plan, cluster, state)
                    if fsid is not None: break
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original provider; never repeat uncertain initialization")
                    time.sleep(2)
                prerequisites(network, plan)
                if ready(network, plan, cluster, state) != fsid or toolset() != source: raise ValueError("Final provider readiness or source identity changed")
                result.update(server_checked=True, devices_observed=True, provider_resource_ready=True)
                record("completed", fsid=fsid, **result); return result
        except BaseException:
            record("failed", provider_resource_ready=False, error_code="ceph-provider-installation-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=renderer.storage.RUNTIMES, required=True)
    actions = parser.add_mutually_exclusive_group()
    for mode in ("check-server", "create", "resume"): actions.add_argument("--" + mode, action="store_true")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024))
        mode = "check-server" if args.check_server else "create" if args.create else "resume" if args.resume else "local"
        print(json.dumps(operate(plan, args.runtime, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Ceph provider creation refused; preserve private probe evidence and original resource handles. Private details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
