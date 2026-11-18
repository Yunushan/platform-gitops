#!/usr/bin/env python3
"""Create fresh pinned Rook/CSI bootstrap resources; never adopts, upgrades or deletes storage."""

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
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, verify_target
import deploy_ceph_provider as provider
import provision_ceph_storage as shared
import render_rook_bootstrap as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/rook-bootstrap-operation"
CONTRACT = "platform.gitops/rook-bootstrap-plan"


def toolset():
    return renderer.storage.fingerprint({"provider": provider.toolset(), **{n: hashlib.sha256(read_bounded_bytes(Path(__file__).parent / n, max_bytes=256 * 1024)).hexdigest()
        for n in ("render_rook_bootstrap.py", "deploy_rook_bootstrap.py")}, "catalog": hashlib.sha256(read_bounded_bytes(renderer.ROOT / "schemas/rook/v1.21.2/bootstrap.json", max_bytes=2 * 1024 ** 2)).hexdigest()})


def prerequisites(client, plan):
    verify_target(client, plan["target"])
    version = loads_strict_json(client.call(["version", "-o", "json"]))
    value = version.get("serverVersion", {})
    if str(value.get("major")) != "1" or not re.fullmatch(r"(?:3[2-7])[+]?$", str(value.get("minor"))): raise ValueError("Review the Kubernetes version outside Rook v1.21's 1.32-1.37 compatibility lane")
    namespace = client.get("namespace/" + plan["namespace"]) or {}; meta = namespace.get("metadata", {})
    if meta.get("uid") != plan["namespace_uid"] or meta.get("deletionTimestamp") or meta.get("labels", {}).get("pod-security.kubernetes.io/enforce") != "privileged": raise ValueError("Require the original explicitly privileged operator namespace")
    nodes = {}
    for node in plan["nodes"]:
        current = client.get("node/" + node["name"]) or {}; labels = current.get("metadata", {}).get("labels", {})
        if (current.get("metadata", {}).get("uid") != node["uid"] or current.get("metadata", {}).get("deletionTimestamp") or not shared.condition(current, "Ready")
                or labels.get("kubernetes.io/hostname") != node["name"] or labels.get(plan["domain_label"]) != node["failure_domain"]
                or current.get("status", {}).get("nodeInfo", {}).get("operatingSystem") != "linux" or current.get("spec", {}).get("unschedulable")
                or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in current.get("spec", {}).get("taints", []))): raise ValueError("A reviewed client/controller node or failure domain changed")
        nodes[node["name"]] = node["failure_domain"]
    return nodes


def admitted(expected, actual):
    identity = shared.admitted(expected, actual)
    if expected["apiVersion"].startswith("csi.ceph.io/"):
        schema = renderer.schema_map()[expected["apiVersion"] + "/" + expected["kind"]]
        if not provider.finite_defaults(expected["spec"], actual["spec"], schema["properties"]["spec"]): raise ValueError("Admission injected unreviewed CSI driver policy")
    elif expected["kind"] in {"ClusterRole", "Role", "ClusterRoleBinding", "RoleBinding", "ConfigMap"}:
        # RBAC and immutable configuration are finite source policy, never an
        # approval token for additional subjects/rules or fallback images.
        expected_body, actual_body = shared.material(expected), shared.material(actual)
        for key in ("rules", "subjects", "roleRef", "data", "immutable"):
            if expected_body.get(key) != actual_body.get(key): raise ValueError("Admission changed finite bootstrap access or configuration")
        if actual_body.get("aggregationRule") or actual_body.get("binaryData"): raise ValueError("Unreviewed bootstrap aggregation/configuration")
        if any(k.startswith("rbac.authorization.k8s.io/aggregate-to-") for k in actual.get("metadata", {}).get("labels", {})): raise ValueError("Runtime bootstrap roles cannot join convenience aggregation")
    elif expected["kind"] == "Deployment":
        if operator_pod_policy(expected["spec"]["template"]["spec"]) != operator_pod_policy(actual["spec"]["template"]["spec"]): raise ValueError("Admission changed an operator's finite runtime policy")
    return identity


def operator_pod_policy(spec):
    value = provider.pod_policy(spec)
    if value.get("serviceAccount") == value.get("serviceAccountName"): value.pop("serviceAccount", None)
    return value


def owned(current, expected, state, entry):
    meta = current.get("metadata", {}); annotations = meta.get("annotations", {})
    if (not renderer.storage.uid(meta.get("uid")) or meta.get("deletionTimestamp") or entry["uid"] is not None and meta["uid"] != entry["uid"]
            or annotations.get(OPERATION) != state["operation_id"] or annotations.get(CONTRACT) != state["plan_sha256"] or admitted(expected, current) != entry["admitted_sha256"]): raise ValueError("An original bootstrap resource changed")


def apis_ready(client, objects, state):
    for obj in objects:
        current = shared.get(client, obj)
        if current is None: raise ValueError("An original API is missing; never recreate it")
        owned(current, obj, state, state["objects"][shared.key(obj)])
        if not shared.condition(current, "Established") or not shared.condition(current, "NamesAccepted"): return False
    return True


def driver_ready(client, plan, expected, state, nodes):
    current = shared.get(client, expected)
    if current is None: raise ValueError("An original CSI Driver configuration is missing")
    entry = state["objects"][shared.key(expected)]; owned(current, expected, state, entry)
    name, namespace = expected["metadata"]["name"], plan["namespace"]
    allowed = [value for key, value in plan["images"].items() if key not in {"rook", "csi_operator"}]
    for kind, suffix, minimum in (("deployment", "ctrlplugin", 2), ("daemonset", "nodeplugin", 1)):
        workload = client.get(kind + "/" + name + "-" + suffix, namespace, optional=True)
        if workload is None: return False
        if not shared.controlled(workload, "Driver", entry["uid"]): raise ValueError("A CSI workload is owned by another driver")
        generated_handle(workload, kind + "/" + name + "-" + suffix, state)
        status = workload.get("status", {})
        if status.get("observedGeneration") != workload.get("metadata", {}).get("generation"): return False
        if kind == "deployment":
            desired = workload.get("spec", {}).get("replicas", 1)
            if desired != 2: raise ValueError("The CSI controller replica policy changed")
            if any(status.get(k) != desired for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")): return False
        else:
            desired = status.get("desiredNumberScheduled")
            if desired != len(nodes) or any(status.get(k) != desired for k in ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")): return False
        config = {"name": name + "-" + suffix, "uid": workload["metadata"]["uid"], "images": allowed}
        shared.workload(client, namespace, kind, config, nodes, minimum=minimum, spread=kind == "deployment")
    driver = client.get("csidriver/" + name, optional=True)
    if driver is None: return False
    # A namespaced Driver cannot own a cluster-scoped object through an ownerRef.
    # The pinned operator uses this JSON object-key annotation instead.
    annotation = driver.get("metadata", {}).get("annotations", {}).get("csi.ceph.io/ownerref", "")
    if (not isinstance(annotation, str) or len(annotation) > 512 or loads_strict_json(annotation) != {"Namespace": namespace, "Name": name}
            or driver.get("metadata", {}).get("ownerReferences") or driver.get("spec", {}).get("attachRequired") != expected["spec"]["attachRequired"]
            or driver.get("spec", {}).get("fsGroupPolicy") != "File" or driver.get("spec", {}).get("podInfoOnMount") is not True or driver.get("spec", {}).get("seLinuxMount") is not True): raise ValueError("CSI registration identity or policy differs")
    generated_handle(driver, "csidriver/" + name, state)
    return True


def generated_handle(current, name, state):
    meta = current.get("metadata", {})
    if not renderer.storage.uid(meta.get("uid")) or meta.get("deletionTimestamp"): raise ValueError("A generated CSI handle is missing or terminating")
    entry = {"uid": meta["uid"], "material_sha256": renderer.storage.fingerprint(shared.material(current))}
    previous = state["generated"].get(name)
    if previous is not None and previous != entry: raise ValueError("An original generated CSI resource was replaced or changed")
    state["generated"][name] = entry


def handoff(client, plan):
    namespace = plan["namespace"]
    operator = client.get("deployment/rook-ceph-operator", namespace)
    crd = client.get("customresourcedefinition/cephclusters.ceph.rook.io")
    configs = {}
    for name in ("rook-ceph-operator-config", "rook-csi-operator-image-set-configmap"):
        obj = client.get("configmap/" + name, namespace)
        configs[name] = {"uid": obj["metadata"]["uid"], "data_sha256": renderer.storage.fingerprint(obj["data"])}
    return {"namespace": namespace, "namespace_uid": plan["namespace_uid"], "deployment": "rook-ceph-operator", "uid": operator["metadata"]["uid"],
        "image": plan["images"]["rook"], "cluster_crd_uid": crd["metadata"]["uid"], "configmaps": configs}


def csi_handoff(client, plan, phases, state):
    result = {}
    for obj in phases["drivers"]:
        if obj["kind"] != "Driver": continue
        name = obj["metadata"]["name"]; kind = "rbd" if ".rbd." in name else "cephfs"; components = {}
        for role, resource, suffix in (("controller", "deployment", "-ctrlplugin"), ("node_plugin", "daemonset", "-nodeplugin")):
            current = client.get(resource + "/" + name + suffix, plan["namespace"])
            generated_handle(current, resource + "/" + name + suffix, state)
            spec = current["spec"]["template"]["spec"]
            components[role] = {"name": name + suffix, "uid": current["metadata"]["uid"], "images": sorted({c["image"] for c in spec.get("containers", []) + spec.get("initContainers", [])})}
        driver = client.get("csidriver/" + name); generated_handle(driver, "csidriver/" + name, state)
        result[kind] = {"namespace": plan["namespace"], "namespace_uid": plan["namespace_uid"], "driver": name, "driver_uid": driver["metadata"]["uid"], **components}
    return result


def core_ready(client, plan, phases, state, nodes):
    for obj in phases["core"]:
        current = shared.get(client, obj)
        if current is None: raise ValueError("An original bootstrap object disappeared")
        entry = state["objects"][shared.key(obj)]; owned(current, obj, state, entry)
        if obj["kind"] == "Deployment":
            csi = obj["metadata"]["name"] == "ceph-csi-controller-manager"
            status = current.get("status", {}); desired = current["spec"].get("replicas", 1)
            if status.get("observedGeneration") != current["metadata"].get("generation") or any(status.get(k) != desired for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")): return False
            config = {"name": obj["metadata"]["name"], "uid": entry["uid"], "images": [plan["images"]["csi_operator" if csi else "rook"]]}
            shared.workload(client, plan["namespace"], "deployment", config, nodes, minimum=2 if csi else 1, spread=csi)
    return True


def api_exists(client, obj):
    plural = "drivers" if obj["kind"] == "Driver" else "operatorconfigs"
    return client.get("customresourcedefinition/" + plural + ".csi.ceph.io", optional=True) is not None


def fresh_driver_names(client, obj):
    name, namespace = obj["metadata"]["name"], obj["metadata"]["namespace"]
    for resource, scope in (("deployment/" + name + "-ctrlplugin", namespace), ("daemonset/" + name + "-nodeplugin", namespace), ("csidriver/" + name, None)):
        if client.get(resource, scope, optional=True) is not None: raise ValueError("Fresh CSI bootstrap cannot adopt or overwrite existing generated names")


def operate(plan, runtime, *, mode="local", output=None, wait_seconds=600, client=None):
    phases = renderer.objects(plan, runtime); source = toolset()
    result = {"local_inputs_verified": True, "server_checked": False, "bootstrap_resources_ready": False, **renderer.UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-server", "create", "resume"} or output is None or not renderer.storage.integer(wait_seconds, 5, 3600): raise ValueError("Select an explicit bounded bootstrap operation")
    if mode != "check-server" and plan["admitted_core_sha256"] is None: raise ValueError("First review and pin server admission of APIs/RBAC/controllers")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private operation directory")
    if mode != "resume": resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = resolved / "rook-bootstrap-proof.json"; all_objects = sum(phases.values(), [])
    with shared.lock(resolved):
        if mode == "resume":
            info, directory = receipt.lstat(), resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077 or directory.st_uid != os.geteuid() or directory.st_mode & 0o077): raise ValueError("Original private journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=512 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "create" or state.get("runtime") != runtime
                    or state.get("plan_sha256") != renderer.storage.fingerprint(plan) or state.get("toolset_sha256") != source or not isinstance(state.get("operation_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"]) or not isinstance(state.get("objects"), dict)
                    or not set(state["objects"]).issubset({shared.key(o) for o in all_objects}) or not isinstance(state.get("generated"), dict)
                    or not set(state["generated"]).issubset({kind + "/" + o["metadata"]["name"] + suffix for o in phases["drivers"] if o["kind"] == "Driver" for kind, suffix in (("deployment", "-ctrlplugin"), ("daemonset", "-nodeplugin"), ("csidriver", ""))})
                    or any(state.get(k) is not False for k in renderer.UNVERIFIED)): raise ValueError("Resume only matching original bootstrap handles")
            for entry in state["objects"].values():
                if not isinstance(entry, dict) or set(entry) != {"admitted_sha256", "uid"} or not renderer.storage.sha(entry["admitted_sha256"]) or entry["uid"] is not None and not renderer.storage.uid(entry["uid"]): raise ValueError("Invalid original bootstrap handle")
            for entry in state["generated"].values():
                if not isinstance(entry, dict) or set(entry) != {"material_sha256", "uid"} or not renderer.storage.sha(entry["material_sha256"]) or not renderer.storage.uid(entry["uid"]): raise ValueError("Invalid original CSI handle")
        else:
            state = {"schema_version": 1, "action": mode, "runtime": runtime, "operation_id": uuid4().hex, "plan_sha256": renderer.storage.fingerprint(plan), "toolset_sha256": source, "objects": {}, "generated": {}, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, renderer.storage.canonical(state))
            if os.name == "posix":
                for directory in (resolved, resolved.parent):
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
        record("started")
        for obj in all_objects: obj["metadata"]["annotations"] = {**obj["metadata"].get("annotations", {}), OPERATION: state["operation_id"], CONTRACT: state["plan_sha256"]}
        try:
            network = client or Client(plan["target"])
            with network.frozen():
                nodes = prerequisites(network, plan); hashes = {}
                for obj in phases["drivers"]:
                    if obj["kind"] == "Driver" and shared.key(obj) not in state["objects"]: fresh_driver_names(network, obj)
                for obj in all_objects:
                    custom = obj in phases["drivers"]; identity = shared.key(obj)
                    current = shared.get(network, obj) if not custom or api_exists(network, obj) else None
                    if identity in state["objects"]:
                        if current is None: raise ValueError("A missing uncertain bootstrap request cannot be repeated")
                        owned(current, obj, state, state["objects"][identity]); state["objects"][identity]["uid"] = current["metadata"]["uid"]
                        hashes[identity] = state["objects"][identity]["admitted_sha256"]
                    else:
                        if current is not None: raise ValueError("Fresh bootstrap cannot adopt existing APIs, access grants or operators")
                        if custom: continue
                        admitted_obj = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                        hashes[identity] = admitted(obj, admitted_obj)
                        atomic_write_text(resolved / (hashlib.sha256(identity.encode()).hexdigest()[:16] + ".admitted.json"), renderer.storage.canonical(shared.material(admitted_obj)))
                core_pin = renderer.storage.fingerprint({shared.key(o): hashes[shared.key(o)] for o in phases["apis"] + phases["core"]})
                record("core-admission-checked", observed_admitted_core_sha256=core_pin, server_checked=True)
                if plan["admitted_core_sha256"] is not None and plan["admitted_core_sha256"] != core_pin: raise ValueError("Concrete bootstrap admission changed")
                if mode == "check-server":
                    record("server-check-completed"); return {**result, "server_checked": True, "admitted_core_sha256": core_pin, "custom_api_dry_run_pending": True}
                def create(obj):
                    identity = shared.key(obj)
                    if identity in state["objects"]: return
                    if toolset() != source or shared.get(network, obj) is not None: raise ValueError("Bootstrap source or a pending resource name changed")
                    if obj["kind"] == "Driver": fresh_driver_names(network, obj)
                    state["objects"][identity] = {"uid": None, "admitted_sha256": hashes[identity]}; record("before-create", pending=identity)
                    current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                    if renderer.storage.uid(current.get("metadata", {}).get("uid")): state["objects"][identity]["uid"] = current["metadata"]["uid"]; record("create-response-observed")
                    owned(current, obj, state, state["objects"][identity]); record("created", pending=None)
                prerequisites(network, plan)
                for obj in phases["apis"]: create(obj)
                deadline = time.monotonic() + wait_seconds
                while not apis_ready(network, phases["apis"], state):
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original API definitions again")
                    time.sleep(2)
                prerequisites(network, plan)
                for obj in phases["core"]: create(obj)
                prerequisites(network, plan)
                for obj in phases["drivers"]:
                    identity = shared.key(obj)
                    if identity not in state["objects"]:
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
                        hashes[identity] = admitted(obj, response)
                        atomic_write_text(resolved / (hashlib.sha256(identity.encode()).hexdigest()[:16] + ".admitted.json"), renderer.storage.canonical(shared.material(response)))
                    create(obj)
                while True:
                    if core_ready(network, plan, phases, state, nodes) and all(driver_ready(network, plan, obj, state, nodes) for obj in phases["drivers"] if obj["kind"] == "Driver"): break
                    record("waiting-for-original-controllers")
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original operators and driver handles again")
                    time.sleep(2)
                prerequisites(network, plan)
                if not core_ready(network, plan, phases, state, nodes) or not apis_ready(network, phases["apis"], state) or toolset() != source or not all(driver_ready(network, plan, obj, state, nodes) for obj in phases["drivers"] if obj["kind"] == "Driver"): raise ValueError("Final bootstrap evidence changed")
                result.update(server_checked=True, bootstrap_resources_ready=True)
                record("completed", provider_operator=handoff(network, plan), storage_access_nodes=plan["nodes"], storage_csi=csi_handoff(network, plan, phases, state), **result)
                return result
        except BaseException:
            record("failed", bootstrap_resources_ready=False, error_code="rook-bootstrap-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=renderer.storage.RUNTIMES, required=True)
    modes = parser.add_mutually_exclusive_group()
    for action in ("check-server", "create", "resume"): modes.add_argument("--" + action, action="store_true")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024)); mode = "check-server" if args.check_server else "create" if args.create else "resume" if args.resume else "local"
        print(json.dumps(operate(plan, args.runtime, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Rook bootstrap refused; preserve private evidence and original resource handles. Private details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
