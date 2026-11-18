#!/usr/bin/env python3
"""Create fresh CSI canaries and verify RBD remount and concurrent CephFS exchange."""

import argparse
from datetime import datetime, timedelta, timezone
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
from kubernetes_management import Client
import ceph_client_io as program
import deploy_ceph_provider as provider
import provision_ceph_storage as shared
import render_ceph_client_check as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/ceph-client-operation"
CONTRACT = "platform.gitops/ceph-client-plan"


def toolset():
    return renderer.storage.fingerprint({"provider": provider.toolset(), **{n: hashlib.sha256(read_bounded_bytes(Path(__file__).parent / n, max_bytes=256 * 1024)).hexdigest()
        for n in ("verify_ceph_clients.py", "render_ceph_client_check.py", "render_rook_bootstrap.py", "ceph_client_io.py")}})


def storage_receipt(plan, storage_plan, runtime, path):
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Require the original private storage receipt")
    raw = read_bounded_bytes(path, max_bytes=256 * 1024); value = loads_strict_json(raw)
    objects = renderer.storage.objects(storage_plan, runtime)
    if (hashlib.sha256(raw).hexdigest() != plan["storage_proof_sha256"] or not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("phase") != "completed" or value.get("action") != "create" or value.get("runtime") != runtime
            or value.get("plan_sha256") != plan["storage_plan_sha256"] or value.get("toolset_sha256") != shared.toolset()
            or value.get("storage_resources_provisioned") is not True or value.get("server_checked") is not True or value.get("local_inputs_verified") is not True
            or not renderer.storage.sha(storage_plan["admitted_sha256"]) or value.get("observed_admitted_sha256") != storage_plan["admitted_sha256"]
            or not isinstance(value.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["operation_id"])
            or not isinstance(value.get("objects"), dict) or set(value["objects"]) != {shared.key(o) for o in objects}
            or any(value.get(k) is not False for k in renderer.storage.UNVERIFIED)): raise ValueError("Require the matching completed original storage operation")
    for entry in value["objects"].values():
        if not isinstance(entry, dict) or set(entry) != {"uid", "admitted_sha256"} or not renderer.storage.uid(entry["uid"]) or not renderer.storage.sha(entry["admitted_sha256"]): raise ValueError("Invalid original storage UID")
    for obj in objects: obj["metadata"]["annotations"] = {shared.OPERATION: value["operation_id"], shared.CONTRACT: value["plan_sha256"]}
    return value, objects


def prerequisites(client, plan, storage_plan, receipt, storage_objects):
    shared.prerequisites(client, storage_plan)
    nodes = {n["name"]: n["failure_domain"] for n in storage_plan["access_nodes"]}
    if not shared.ready(client, storage_plan, storage_objects, receipt): raise ValueError("The original storage resources are not ready")
    namespace = client.get("namespace/" + plan["namespace"]) or {}; meta = namespace.get("metadata", {})
    if meta.get("uid") != plan["namespace_uid"] or meta.get("deletionTimestamp") or meta.get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted": raise ValueError("Require the original dedicated restricted client namespace")
    for node in plan["nodes"]:
        current = client.get("node/" + node["name"]) or {}; meta, spec, status = (current.get(k, {}) for k in ("metadata", "spec", "status"))
        if (meta.get("uid") != node["uid"] or meta.get("deletionTimestamp") or not shared.condition(current, "Ready") or spec.get("unschedulable")
                or any(t.get("effect") in {"NoExecute", "NoSchedule"} for t in spec.get("taints", [])) or status.get("nodeInfo", {}).get("operatingSystem") != "linux"
                or meta.get("labels", {}).get("kubernetes.io/hostname") != node["name"] or meta.get("labels", {}).get(storage_plan["provider"]["domain_label"]) != node["failure_domain"]): raise ValueError("A client node or failure domain changed")
        nodes[node["name"]] = node["failure_domain"]
    for config in storage_plan["csi"].values(): shared.workload(client, config["namespace"], "daemonset", config["node_plugin"], nodes, reviewed_nodes_only=True)


def material(obj):
    value = loads_strict_json(renderer.storage.canonical(shared.material(obj)))
    if obj["kind"] == "PersistentVolumeClaim": value["spec"].pop("volumeName", None)
    return value


def admitted(expected, actual, *, bound=False):
    shared.admitted(expected, actual)
    if expected["kind"] == "PersistentVolumeClaim":
        allowed = set(expected["spec"]) | ({"volumeName"} if bound else set())
        if set(actual["spec"]) != allowed and not (bound and set(actual["spec"]) == set(expected["spec"])): raise ValueError("Fresh claims cannot adopt selectors, data sources or prebound data")
        if any(actual["spec"].get(k) != v for k, v in expected["spec"].items()): raise ValueError("Claim policy changed")
    elif expected["kind"] == "Job":
        if set(actual["spec"]) - set(expected["spec"]) - {"suspend"} or actual["spec"].get("suspend", False) is not False or actual["spec"].get("selector") != expected["spec"]["selector"]: raise ValueError("Admission changed canary retry, completion or selector policy")
        if provider.pod_policy(expected["spec"]["template"]["spec"]) != provider.pod_policy(actual["spec"]["template"]["spec"]): raise ValueError("Admission changed finite non-root client policy")
    elif expected.get("data") != actual.get("data") or actual.get("binaryData"): raise ValueError("The immutable canary program changed")
    return renderer.storage.fingerprint(material(actual))


def owned(current, expected, state, entry):
    meta = current.get("metadata", {})
    if (meta.get("uid") != entry["uid"] and entry["uid"] is not None or not renderer.storage.uid(meta.get("uid")) or meta.get("deletionTimestamp")
            or meta.get("annotations", {}).get(OPERATION) != state["operation_id"] or meta.get("annotations", {}).get(CONTRACT) != state["plan_sha256"]
            or admitted(expected, current, bound=True) != entry["admitted_sha256"]): raise ValueError("An original canary resource changed")


def binding(client, plan, storage_plan, obj, state):
    claim = shared.get(client, obj)
    if claim is None: raise ValueError("The original canary claim disappeared")
    entry = state["objects"][shared.key(obj)]; owned(claim, obj, state, entry)
    if claim.get("status", {}).get("phase") != "Bound": return False
    kind = "rbd" if obj["metadata"]["name"].endswith("-rbd") else "cephfs"
    name = claim["spec"].get("volumeName")
    if not renderer.storage.resource(name): raise ValueError("Invalid original canary volume")
    pv = client.get("persistentvolume/" + name) or {}; spec = pv.get("spec", {}); csi = spec.get("csi", {}); ref = spec.get("claimRef", {})
    if pv.get("metadata", {}).get("uid") in state["preexisting_pvs"]: raise ValueError("Canary clients cannot use pre-existing PV data")
    created = datetime.fromisoformat(pv.get("metadata", {}).get("creationTimestamp", "")); started = datetime.fromisoformat(state["started_at"])
    if created.tzinfo is None or created < started - timedelta(seconds=30) or (datetime.now(timezone.utc) - created).total_seconds() < -30: raise ValueError("The canary PV predates its original operation")
    identity = csi.get("volumeAttributes", {}).get("storage.kubernetes.io/csiProvisionerIdentity")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 1024: raise ValueError("Require a newly dynamically provisioned CSI volume")
    if (not renderer.storage.uid(pv.get("metadata", {}).get("uid")) or pv.get("metadata", {}).get("deletionTimestamp") or pv.get("status", {}).get("phase") != "Bound"
            or ref.get("uid") != entry["uid"] or ref.get("namespace") != plan["namespace"] or ref.get("name") != obj["metadata"]["name"] or ref.get("kind") != "PersistentVolumeClaim"
            or spec.get("storageClassName") != obj["spec"]["storageClassName"] or spec.get("accessModes") != obj["spec"]["accessModes"] or spec.get("volumeMode") != "Filesystem"
            or spec.get("persistentVolumeReclaimPolicy") != "Retain" or spec.get("capacity", {}).get("storage") != "1Gi" or claim.get("status", {}).get("capacity", {}).get("storage") != "1Gi"
            or csi.get("driver") != storage_plan["csi"][kind]["driver"] or not isinstance(csi.get("volumeHandle"), str) or not 1 <= len(csi["volumeHandle"]) <= 1024
            or csi.get("readOnly", False) is not False or csi.get("volumeAttributes", {}).get("clusterID") != storage_plan["provider"]["namespace"]
            or csi.get("nodeStageSecretRef") != {"name": "rook-csi-" + kind + "-node", "namespace": storage_plan["provider"]["namespace"]}): raise ValueError("The claim is not bound to the expected original CSI provider")
    attribute = "pool" if kind == "rbd" else "fsName"; wanted = storage_plan["names"]["block_pool" if kind == "rbd" else "filesystem"]
    if csi.get("volumeAttributes", {}).get(attribute) != wanted or kind == "rbd" and csi.get("fsType") != "ext4": raise ValueError("The canary backend differs from the reviewed pool/filesystem")
    observation = {"name": name, "uid": pv["metadata"]["uid"], "spec_sha256": renderer.storage.fingerprint(spec)}
    previous = state["volumes"].get(kind)
    if previous is not None and previous != observation: raise ValueError("An original canary PV was replaced or rebound")
    state["volumes"][kind] = observation; return True


def initial_volumes(client, storage_plan):
    classes = {storage_plan["names"][k + "_class"] for k in ("rbd", "cephfs")}
    selected = [pv for pv in shared.items(client, "persistentvolumes") if pv.get("spec", {}).get("storageClassName") in classes]
    if any(not renderer.storage.uid(pv.get("metadata", {}).get("uid")) or pv.get("metadata", {}).get("deletionTimestamp") or pv.get("status", {}).get("phase") != "Bound" for pv in selected): raise ValueError("Unbound or terminating PVs on canary classes require separate review")
    return sorted({pv["metadata"]["uid"] for pv in selected})


def client_pods_owned(client, plan, objects, state):
    claims = {o["metadata"]["name"] for o in objects if o["kind"] == "PersistentVolumeClaim"}
    jobs = {o["metadata"]["name"] for o in objects if o["kind"] == "Job"}
    handles = [state["objects"][shared.key(o)]["uid"] for o in objects if o["kind"] == "Job" and shared.key(o) in state["objects"] and state["objects"][shared.key(o)]["uid"] is not None]
    for pod in shared.items(client, "pods", plan["namespace"]):
        uses_claim = any(v.get("persistentVolumeClaim", {}).get("claimName") in claims for v in pod.get("spec", {}).get("volumes", []))
        role = pod.get("metadata", {}).get("labels", {}).get("platform.gitops/client-role")
        if (uses_claim or role in jobs) and not any(shared.controlled(pod, "Job", uid) for uid in handles): raise ValueError("An unowned pod occupies a canary selector or claim")


def completed(client, plan, obj, state, role):
    job = shared.get(client, obj)
    if job is None: raise ValueError("An original client Job disappeared")
    entry = state["objects"][shared.key(obj)]; owned(job, obj, state, entry)
    if job.get("status", {}).get("failed", 0) or shared.condition(job, "Failed"): raise ValueError("An original no-retry client failed")
    pods = [p for p in shared.items(client, "pods", plan["namespace"]) if shared.controlled(p, "Job", entry["uid"])]
    if len(pods) > 1: raise ValueError("An original canary acquired multiple pod handles")
    if not pods: return None
    pod = pods[0]; meta, status = pod.get("metadata", {}), pod.get("status", {})
    expected_node = renderer.scope(plan)["nodes"][role]; spec = dict(pod.get("spec", {})); node = spec.pop("nodeName", None)
    if (not renderer.storage.uid(meta.get("uid")) or meta.get("deletionTimestamp") or node is not None and node != expected_node
            or provider.pod_policy(spec) != provider.pod_policy(job["spec"]["template"]["spec"])): raise ValueError("The original client pod policy or placement changed")
    previous = state["pods"].get(role)
    if previous is not None and previous != meta["uid"]: raise ValueError("An original client pod was replaced")
    state["pods"][role] = meta["uid"]
    if not shared.condition(job, "Complete") or job.get("status", {}).get("succeeded") != 1: return None
    statuses = status.get("containerStatuses", [])
    if (node != expected_node or status.get("phase") != "Succeeded" or len(statuses) != 1 or statuses[0].get("name") != "canary" or statuses[0].get("restartCount") != 0
            or statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0 or not isinstance(statuses[0].get("imageID"), str)
            or not re.search(r"sha256:[0-9a-f]{64}$", statuses[0]["imageID"])): raise ValueError("The original client did not complete exactly once")
    raw = client.call(["logs", "pod/" + meta["name"], "--namespace", plan["namespace"], "--container", "canary", "--limit-bytes=16384"])
    value = loads_strict_json(raw); scope = renderer.scope(plan); identity = "rbd" if role.startswith("rbd-") else role; peer = "cephfs-right" if role == "cephfs-left" else "cephfs-left"
    expected = {"schema_version": 1, "operation_id": plan["operation_id"], "scope_sha256": program.digest(scope), "program_sha256": hashlib.sha256(read_bounded_bytes(Path(program.__file__), max_bytes=128 * 1024)).hexdigest(),
        "role": role, "node": expected_node, "payload_bytes": plan["payload_bytes"], "filesystem": "ext4" if role.startswith("rbd-") else "ceph", "volume_verified": True,
        "payload_sha256": program.expected(scope, identity), "peer_sha256": program.expected(scope, peer) if role.startswith("cephfs-") else None, "peer_barrier_verified": role.startswith("cephfs-"), **program.UNVERIFIED}
    if not isinstance(value, dict) or set(value) != set(expected) | {"observed_at"} or any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in expected.items()): raise ValueError("Client bytes, barrier or program proof differs")
    observed = datetime.fromisoformat(value["observed_at"])
    if observed.tzinfo is None or not -30 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 3600: raise ValueError("Client evidence is stale or future dated")
    return value


def writer_released(client, plan, storage_plan, state):
    pv = state["volumes"]["rbd"]["name"]; writer = plan["nodes"][0]["name"]
    rows = [r for r in shared.items(client, "volumeattachments.storage.k8s.io") if r.get("spec", {}).get("source", {}).get("persistentVolumeName") == pv]
    for row in rows:
        if not renderer.storage.uid(row.get("metadata", {}).get("uid")) or row.get("spec", {}).get("attacher") != storage_plan["csi"]["rbd"]["driver"] or row.get("spec", {}).get("nodeName") != writer: raise ValueError("An unexpected node holds the canary RBD attachment")
        if row.get("status", {}).get("attached") is not False: return False
    return True


def operate(plan, storage_plan, runtime, proof_path, *, mode="local", output=None, wait_seconds=900, client=None):
    objects = renderer.objects(plan, storage_plan, runtime); source = toolset(); receipt, storage_objects = storage_receipt(plan, storage_plan, runtime, proof_path)
    result = {"local_inputs_verified": True, "server_checked": False, **renderer.UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-server", "create", "resume"} or output is None or not renderer.storage.integer(wait_seconds, 5, 1800): raise ValueError("Select an explicit bounded canary operation")
    if mode != "check-server" and plan["admitted_sha256"] is None: raise ValueError("Review concrete canary admission first")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private canary operation directory")
    if mode != "resume": resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    journal = resolved / "ceph-client-proof.json"
    with shared.lock(resolved):
        if mode == "resume":
            info = journal.lstat(); directory = resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077 or directory.st_uid != os.geteuid() or directory.st_mode & 0o077): raise ValueError("Original canary journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(journal, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "create" or state.get("operation_id") != plan["operation_id"]
                    or state.get("plan_sha256") != renderer.storage.fingerprint(plan) or state.get("runtime") != runtime or state.get("toolset_sha256") != source
                    or not isinstance(state.get("objects"), dict) or not set(state["objects"]).issubset({shared.key(o) for o in objects})
                    or any(not isinstance(state.get(k), dict) for k in ("pods", "volumes", "receipts")) or any(state.get(k) is not False for k in program.UNVERIFIED)): raise ValueError("Resume only the original matching canary handles")
            if (not isinstance(state.get("started_at"), str) or datetime.fromisoformat(state["started_at"]).tzinfo is None
                    or state.get("preexisting_pvs") is None and state["objects"] or state.get("preexisting_pvs") is not None and (not isinstance(state["preexisting_pvs"], list)
                    or len(state["preexisting_pvs"]) > 4096 or any(not renderer.storage.uid(v) for v in state["preexisting_pvs"]) or len(set(state["preexisting_pvs"])) != len(state["preexisting_pvs"]))): raise ValueError("Original PV inventory or operation time is invalid")
            for entry in state["objects"].values():
                if not isinstance(entry, dict) or set(entry) != {"uid", "admitted_sha256"} or not renderer.storage.sha(entry["admitted_sha256"]) or entry["uid"] is not None and not renderer.storage.uid(entry["uid"]): raise ValueError("Invalid canary object handle")
            if not set(state["pods"]).issubset(program.ROLES) or any(not renderer.storage.uid(v) for v in state["pods"].values()) or not set(state["volumes"]).issubset({"rbd", "cephfs"}) or not set(state["receipts"]).issubset(program.ROLES): raise ValueError("Unexpected original client handles")
            for entry in state["volumes"].values():
                if not isinstance(entry, dict) or set(entry) != {"name", "uid", "spec_sha256"} or not renderer.storage.resource(entry["name"]) or not renderer.storage.uid(entry["uid"]) or not renderer.storage.sha(entry["spec_sha256"]): raise ValueError("Invalid original CSI volume identity")
        else:
            state = {"schema_version": 1, "action": mode, "runtime": runtime, "operation_id": plan["operation_id"], "plan_sha256": renderer.storage.fingerprint(plan), "toolset_sha256": source,
                "objects": {}, "pods": {}, "volumes": {}, "receipts": {}, "preexisting_pvs": None, "started_at": datetime.now(timezone.utc).isoformat(), **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(journal, renderer.storage.canonical(state))
            if os.name == "posix":
                for path in (resolved, resolved.parent):
                    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
        record("started")
        for obj in objects: obj["metadata"]["annotations"] = {OPERATION: state["operation_id"], CONTRACT: state["plan_sha256"]}
        try:
            network = client or Client(storage_plan["target"])
            with network.frozen():
                prerequisites(network, plan, storage_plan, receipt, storage_objects); hashes = {}
                if state["preexisting_pvs"] is None:
                    state["preexisting_pvs"] = initial_volumes(network, storage_plan); record("original-pv-inventory-recorded")
                for obj in objects:
                    identity = shared.key(obj); current = shared.get(network, obj)
                    if identity in state["objects"]:
                        if current is None: raise ValueError("An absent uncertain original request cannot be repeated")
                        owned(current, obj, state, state["objects"][identity]); state["objects"][identity]["uid"] = current["metadata"]["uid"]
                        hashes[identity] = state["objects"][identity]["admitted_sha256"]
                    else:
                        if current is not None: raise ValueError("A canary cannot adopt existing names or volumes")
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                        hashes[identity] = admitted(obj, response)
                        atomic_write_text(resolved / (obj["metadata"]["name"] + ".admitted.json"), renderer.storage.canonical(material(response)))
                client_pods_owned(network, plan, objects, state)
                pin = renderer.storage.fingerprint(hashes); record("admission-checked", admitted_sha256=pin, server_checked=True)
                if plan["admitted_sha256"] is not None and pin != plan["admitted_sha256"]: raise ValueError("Concrete client admission changed")
                if mode == "check-server": record("server-check-completed"); return {**result, "server_checked": True, "admitted_sha256": pin}
                def create(obj):
                    identity = shared.key(obj)
                    if identity in state["objects"]: return
                    if toolset() != source or shared.get(network, obj) is not None: raise ValueError("Canary source or pending resource name changed")
                    state["objects"][identity] = {"uid": None, "admitted_sha256": hashes[identity]}; record("before-create", pending=identity)
                    response = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                    if renderer.storage.uid(response.get("metadata", {}).get("uid")): state["objects"][identity]["uid"] = response["metadata"]["uid"]; record("create-response-observed")
                    owned(response, obj, state, state["objects"][identity]); record("created", pending=None)
                for obj in objects[:3]: create(obj)
                jobs = dict(zip(program.ROLES, objects[3:])); claims = objects[1:3]
                if not any(shared.key(job) in state["objects"] for job in jobs.values()):
                    initial_volumes(network, storage_plan)
                    for obj in claims:
                        claim = shared.get(network, obj)
                        if claim and claim.get("spec", {}).get("volumeName"): binding(network, plan, storage_plan, obj, state)
                for role in ("rbd-write", "cephfs-left", "cephfs-right"): create(jobs[role])
                deadline = time.monotonic() + wait_seconds
                def wait(roles):
                    while True:
                        bound = all(binding(network, plan, storage_plan, obj, state) for obj in claims)
                        ready = {}
                        if bound:
                            for role in roles:
                                value = completed(network, plan, jobs[role], state, role)
                                if value is not None: ready[role] = value
                        state["receipts"].update(ready); record("waiting-for-original-clients")
                        if len(ready) == len(roles): return
                        if time.monotonic() >= deadline: raise TimeoutError("Observe the original clients; never repeat uncertain creates")
                        time.sleep(2)
                wait(("rbd-write", "cephfs-left", "cephfs-right"))
                if shared.key(jobs["rbd-read"]) not in state["objects"]:
                    while not writer_released(network, plan, storage_plan, state):
                        if time.monotonic() >= deadline: raise TimeoutError("The original writer RBD attachment is not released")
                        record("waiting-for-writer-detach"); time.sleep(2)
                    prerequisites(network, plan, storage_plan, receipt, storage_objects); create(jobs["rbd-read"])
                wait(program.ROLES)
                prerequisites(network, plan, storage_plan, receipt, storage_objects)
                storage_receipt(plan, storage_plan, runtime, proof_path)
                client_pods_owned(network, plan, objects, state)
                for obj in objects:
                    current = shared.get(network, obj)
                    if current is None: raise ValueError("An original client resource disappeared before final verification")
                    owned(current, obj, state, state["objects"][shared.key(obj)])
                for role in program.ROLES:
                    value = completed(network, plan, jobs[role], state, role)
                    if value is None: raise ValueError("An original client completion disappeared")
                    state["receipts"][role] = value
                if toolset() != source or not all(binding(network, plan, storage_plan, obj, state) for obj in claims): raise ValueError("Final client/provider evidence changed")
                result.update(server_checked=True, kubernetes_client_mounts_verified=True, kubernetes_data_roundtrip_verified=True); record("completed", **result); return result
        except BaseException:
            record("failed", failure_phase=state["phase"], kubernetes_client_mounts_verified=False, kubernetes_data_roundtrip_verified=False, error_code="ceph-client-check-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "storage-plan", "storage-proof"): parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--runtime", choices=renderer.storage.RUNTIMES, required=True); parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=900); actions = parser.add_mutually_exclusive_group()
    for name in ("check-server", "create", "resume"): actions.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan, storage_plan = [loads_strict_json(read_bounded_bytes(p, max_bytes=256 * 1024)) for p in (args.plan, args.storage_plan)]
        mode = "check-server" if args.check_server else "create" if args.create else "resume" if args.resume else "local"
        print(json.dumps(operate(plan, storage_plan, args.runtime, args.storage_proof, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Ceph client verification refused; preserve private receipts and original handles. Private values were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
