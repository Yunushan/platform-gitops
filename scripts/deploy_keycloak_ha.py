#!/usr/bin/env python3
"""Explicit-target fresh Keycloak workload installation; never imports realm/DB data."""

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
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
import render_keycloak_ha as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/keycloak-operation"
CONTRACT = "platform.gitops/keycloak-contract"
SHA = re.compile(r"[0-9a-f]{64}")
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
UNVERIFIED = ("mfa_verified", "database_ha_verified", "network_datapath_verified", "application_integration_verified", "session_failover_verified", "ha_verified", "full_stack_verified")


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def uid(value): return isinstance(value, str) and bool(UID.fullmatch(value))
def sha(value): return isinstance(value, str) and bool(SHA.fullmatch(value))
def key(obj): return obj["kind"].lower() + "/" + obj["metadata"]["name"]
def condition(obj, name): return any(c.get("type") == name and c.get("status") == "True" for c in obj.get("status", {}).get("conditions", []))


def includes(expected, actual):
    if isinstance(expected, dict): return isinstance(actual, dict) and all(k in actual and includes(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list): return isinstance(actual, list) and len(expected) == len(actual) and all(includes(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def controlled(obj, kind, identity):
    refs = [r for r in obj.get("metadata", {}).get("ownerReferences", []) if r.get("controller") is True]
    return len(refs) == 1 and refs[0].get("kind") == kind and refs[0].get("uid") == identity


def toolset():
    directory = Path(__file__).resolve().parent; found = {}; pending = [Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_bytes(directory / (name + ".py"), max_bytes=2 * 1024 ** 2); found[name] = hashlib.sha256(raw).hexdigest()
        for item in ast.walk(ast.parse(raw)):
            modules = [n.name for n in item.names] if isinstance(item, ast.Import) else [item.module] if isinstance(item, ast.ImportFrom) and item.module else []
            for module in modules:
                first = module.split(".")[0]
                if (directory / (first + ".py")).is_file() and first not in found: pending.append(first)
    return fingerprint(found)


def load_package(bundle, digest):
    if not sha(digest): raise ValueError("Pin the original Keycloak contract")
    raw = read_bounded_bytes(
        bundle / "keycloak-contract.json",
        max_bytes=256 * 1024,
    )
    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError("Keycloak contract changed")
    contract = loads_strict_json(raw)
    renderer.exact(contract, "schema_version runtime plan files_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in {"rke2", "k3s", "k8s"}: raise ValueError("This installer requires a Kubernetes Keycloak package")
    files = renderer.render(contract["plan"], contract["runtime"])
    if files["keycloak-contract.json"].encode() != raw: raise ValueError("The contract differs from current reviewed policy")
    for name, value in files.items():
        if read_bounded_bytes(bundle / name, max_bytes=2 * 1024 ** 2) != value.encode(): raise ValueError("A Keycloak package file changed")
    # Network isolation and stable references precede executable workloads.
    objects = sorted(renderer.kubernetes(contract["plan"]), key=lambda o: ("NetworkPolicy", "Service", "PodDisruptionBudget", "Deployment").index(o["kind"]))
    return contract, objects


def validate(plan, contract):
    renderer.exact(plan, "schema_version contract_sha256 target namespace_uid node_uids secrets cilium admitted_sha256")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not sha(plan["contract_sha256"]) or not uid(plan["namespace_uid"]) or plan["admitted_sha256"] is not None and not sha(plan["admitted_sha256"]): raise ValueError("Use an explicit original installation scope")
    validate_target(plan["target"]); selected = contract["plan"]
    if not isinstance(plan["node_uids"], dict) or set(plan["node_uids"]) != {m["node"] for m in selected["members"]} or any(not uid(value) for value in plan["node_uids"].values()) or len(set(plan["node_uids"].values())) != len(selected["members"]): raise ValueError("Pin every selected node UID")
    names = {selected["tls_secret"], selected["database_ca_secret"], selected["database"]["secret"]}
    if len(names) != 3 or not isinstance(plan["secrets"], dict) or set(plan["secrets"]) != names: raise ValueError("Use three distinct private Secret snapshots")
    for name, value in plan["secrets"].items():
        renderer.exact(value, "uid type")
        allowed = {"kubernetes.io/tls"} if name == selected["tls_secret"] else {"Opaque", "kubernetes.io/basic-auth"} if name == selected["database"]["secret"] else {"Opaque"}
        if not uid(value["uid"]) or value["type"] not in allowed or value["type"] == "kubernetes.io/basic-auth" and selected["database"]["password_key"] != "password": raise ValueError("Pin the expected Secret identity and type")
    c = plan["cilium"]; renderer.exact(c, "namespace namespace_uid daemonset uid image pod_template_sha256 configmap config_uid config_data_sha256")
    if any(not renderer.label(c[k]) for k in ("namespace", "daemonset", "configmap")) or any(not uid(c[k]) for k in ("namespace_uid", "uid", "config_uid")) or any(not sha(c[k]) for k in ("pod_template_sha256", "config_data_sha256")) or not isinstance(c["image"], str) or not renderer.IMAGE.fullmatch(c["image"]): raise ValueError("Pin the prepared Cilium agent and policy configuration")


def items(client, resource, namespace=None, selector=None):
    args = ["get", resource, "-o", "json"]
    if namespace: args += ["--namespace", namespace]
    if selector: args += ["--selector", selector]
    value = loads_strict_json(client.call(args))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > 4096: raise ValueError("Invalid or excessive Kubernetes collection")
    return value["items"]


def pod_ready(pod):
    spec, status = pod.get("spec", {}), pod.get("status", {}); containers = spec.get("containers", []); states = status.get("containerStatuses", [])
    return (uid(pod.get("metadata", {}).get("uid")) and not pod["metadata"].get("deletionTimestamp") and status.get("phase") == "Running" and condition(pod, "Ready")
        and len(states) == len(containers) > 0 and {s.get("name") for s in states} == {c.get("name") for c in containers}
        and all(s.get("ready") is True and "running" in s.get("state", {}) and s.get("containerID") and s.get("imageID") for s in states)
        and len(spec.get("initContainers", [])) == len(status.get("initContainerStatuses", []))
        and all(s.get("state", {}).get("terminated", {}).get("exitCode") == 0 for s in status.get("initContainerStatuses", [])))


def prerequisites(client, plan, selected):
    verify_target(client, plan["target"])
    ns = client.get("namespace/" + selected["namespace"]) or {}; meta = ns.get("metadata", {})
    if meta.get("uid") != plan["namespace_uid"] or meta.get("deletionTimestamp") or meta.get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted": raise ValueError("The reviewed restricted namespace is unavailable")
    for member in selected["members"]:
        node = client.get("node/" + member["node"]) or {}; m, spec = node.get("metadata", {}), node.get("spec", {})
        if (m.get("uid") != plan["node_uids"][member["node"]] or m.get("deletionTimestamp") or not condition(node, "Ready") or spec.get("unschedulable")
            or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in spec.get("taints", [])) or node.get("status", {}).get("nodeInfo", {}).get("operatingSystem") != "linux"
            or m.get("labels", {}).get("kubernetes.io/hostname") != member["node"] or m.get("labels", {}).get("topology.kubernetes.io/zone") != member["failure_domain"]
            or member["address"] not in {a.get("address") for a in node.get("status", {}).get("addresses", []) if a.get("type") == "InternalIP"}): raise ValueError("A selected node or failure-domain identity changed")
    # Format only identity, immutability and key-presence booleans. Never get a
    # Secret as JSON or capture its credential, certificate or private key data.
    required = {selected["tls_secret"]: ("tls.crt", "tls.key"), selected["database_ca_secret"]: ("ca.crt",), selected["database"]["secret"]: (selected["database"]["password_key"],)}
    if plan["secrets"][selected["database"]["secret"]]["type"] == "kubernetes.io/basic-auth": required[selected["database"]["secret"]] = ("password", "username")
    for name, keys in required.items():
        template = '{{.metadata.uid}}{{"\\n"}}{{.type}}{{"\\n"}}{{.immutable}}{{"\\n"}}{{if .metadata.deletionTimestamp}}true{{else}}false{{end}}{{"\\n"}}' + ''.join('{{if index .data ' + json.dumps(k) + '}}true{{else}}false{{end}}{{"\\n"}}' for k in keys)
        actual = client.call(["get", "secret/" + name, "--namespace", selected["namespace"], "-o", "go-template=" + template]).splitlines()
        if actual != [plan["secrets"][name]["uid"], plan["secrets"][name]["type"], "true", "false", *("true" for _ in keys)]: raise ValueError("Provision the original immutable private Secret snapshots")
    c = plan["cilium"]; namespace = client.get("namespace/" + c["namespace"]) or {}
    if namespace.get("metadata", {}).get("uid") != c["namespace_uid"] or namespace["metadata"].get("deletionTimestamp"): raise ValueError("Cilium namespace changed")
    config = client.get("configmap/" + c["configmap"], c["namespace"]) or {}
    if config.get("metadata", {}).get("uid") != c["config_uid"] or config["metadata"].get("deletionTimestamp") or config.get("binaryData") or fingerprint(config.get("data", {})) != c["config_data_sha256"] or config.get("data", {}).get("enable-policy") not in {"default", "always"} or config["data"].get("enable-k8s-networkpolicy") != "true": raise ValueError("The reviewed Kubernetes policy enforcement configuration changed")
    ds = client.get("daemonset/" + c["daemonset"], c["namespace"]) or {}; metadata, status = ds.get("metadata", {}), ds.get("status", {}); template = ds.get("spec", {}).get("template", {})
    desired = status.get("desiredNumberScheduled")
    if (metadata.get("uid") != c["uid"] or metadata.get("deletionTimestamp") or type(metadata.get("generation")) is not int or status.get("observedGeneration") != metadata["generation"]
        or type(desired) is not int or desired < len(selected["members"]) or any(status.get(k) != desired for k in ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable"))
        or status.get("numberMisscheduled", 0) != 0 or fingerprint(template) != c["pod_template_sha256"]): raise ValueError("The original Cilium agent rollout is unavailable")
    images = {x.get("name"): x.get("image") for x in template.get("spec", {}).get("containers", [])}
    init = {x.get("name"): x.get("image") for x in template.get("spec", {}).get("initContainers", [])}
    if c["image"] not in images.values() or any(not isinstance(v, str) or not renderer.IMAGE.fullmatch(v) for v in [*images.values(), *init.values()]): raise ValueError("Pin every Cilium agent/init image")
    pods = items(client, "pods", c["namespace"])
    for member in selected["members"]:
        found = [p for p in pods if p.get("spec", {}).get("nodeName") == member["node"] and controlled(p, "DaemonSet", c["uid"]) and pod_ready(p)
            and {x.get("name"): x.get("image") for x in p["spec"].get("containers", [])} == images and {x.get("name"): x.get("image") for x in p["spec"].get("initContainers", [])} == init]
        if len(found) != 1: raise ValueError("Each selected host needs its original ready Cilium agent")


def material(obj):
    meta = obj["metadata"]; spec = dict(obj.get("spec", {}))
    if obj["kind"] == "Service": spec = {k: v for k, v in spec.items() if k not in {"clusterIP", "clusterIPs"}}
    return {"apiVersion": obj["apiVersion"], "kind": obj["kind"], "metadata": {"name": meta["name"], "namespace": meta["namespace"], "labels": meta.get("labels", {}),
        "annotations": {k: v for k, v in meta.get("annotations", {}).items() if k not in {OPERATION, CONTRACT}}}, "spec": spec}


def safe_pod(pod):
    if any(pod.get(k) for k in ("hostPID", "hostIPC", "hostNetwork", "shareProcessNamespace", "initContainers", "ephemeralContainers", "runtimeClassName")): return False
    context = pod.get("securityContext", {})
    if context.get("sysctls") or context.get("supplementalGroups"): return False
    if pod.get("schedulerName") not in {None, "default-scheduler"}: return False
    for c in pod.get("containers", []):
        if any(c.get(k) for k in ("lifecycle", "envFrom", "livenessProbe", "stdin", "stdinOnce", "tty")): return False
        for name in ("startupProbe", "readinessProbe"):
            probe = c.get(name, {})
            if any(probe.get(k) for k in ("exec", "tcpSocket", "grpc")) or probe.get("httpGet", {}).get("host") or probe.get("httpGet", {}).get("httpHeaders"): return False
        context = c.get("securityContext", {})
        if (context.get("privileged") or context.get("capabilities", {}).get("add") or context.get("procMount") not in {None, "Default"}
            or context.get("runAsUser", 1000) != 1000 or context.get("runAsGroup", 1000) != 1000 or context.get("runAsNonRoot") is False
            or context.get("seccompProfile", {"type": "RuntimeDefault"}) != {"type": "RuntimeDefault"}): return False
    return True


def admitted(expected, actual):
    if (not isinstance(actual, dict) or actual.get("apiVersion") != expected["apiVersion"] or actual.get("kind") != expected["kind"] or not includes(expected["metadata"], actual.get("metadata", {}))
        or not includes(expected["spec"], actual.get("spec", {})) or actual["metadata"].get("ownerReferences") or actual["metadata"].get("finalizers") or actual["metadata"].get("deletionTimestamp")): raise ValueError("Admission changed the reviewed Keycloak policy or ownership")
    if actual["kind"] == "Service":
        spec = actual["spec"]
        if spec.get("type", "ClusterIP") != "ClusterIP" or spec.get("externalIPs") or spec.get("externalName") or spec.get("clusterIP") == "None" or any(p.get("nodePort") for p in spec["ports"]): raise ValueError("Keep Keycloak behind its internal HTTPS Service")
    if actual["kind"] == "Deployment":
        pod = actual["spec"]["template"]["spec"]
        if pod.get("nodeName") or not safe_pod(pod): raise ValueError("Admission added an executable, placement or privileged path")
    return fingerprint(material(actual))


def owned(expected, current, entry, state):
    if not current or not uid(current.get("metadata", {}).get("uid")) or current["metadata"].get("annotations", {}).get(OPERATION) != state["operation_id"] or current["metadata"].get("annotations", {}).get(CONTRACT) != state["contract_sha256"] or entry["uid"] is not None and current["metadata"]["uid"] != entry["uid"] or admitted(expected, current) != entry["admitted_sha256"]: raise ValueError("An original Keycloak resource was replaced or changed")
    return current


def fresh_children(client, selected, *, workloads_only=False):
    wanted = renderer.LABELS
    for resource in (("deployments", "replicasets", "pods") if workloads_only else ("deployments", "replicasets", "pods", "services")):
        for obj in items(client, resource, selected["namespace"]):
            labels = obj.get("metadata", {}).get("labels", {}); spec = obj.get("spec", {})
            if includes(wanted, labels) or includes(wanted, spec.get("selector", {})) or includes(wanted, spec.get("selector", {}).get("matchLabels", {})) or includes(wanted, spec.get("template", {}).get("metadata", {}).get("labels", {})):
                raise ValueError("Existing Keycloak workloads require recovery rather than fresh installation")


def ready(client, selected, current):
    deployment = current["deployment/platform-keycloak"]; status, meta = deployment.get("status", {}), deployment["metadata"]; count = len(selected["members"]); namespace = selected["namespace"]
    if type(meta.get("generation")) is not int or status.get("observedGeneration") != meta["generation"] or not condition(deployment, "Available") or any(status.get(k) != count for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")) or status.get("unavailableReplicas", 0) != 0: return None
    selector = ",".join(k + "=" + v for k, v in renderer.LABELS.items())
    sets = items(client, "replicasets", namespace, selector)
    if len(sets) != 1 or not uid(sets[0].get("metadata", {}).get("uid")) or not controlled(sets[0], "Deployment", meta["uid"]) or sets[0]["metadata"].get("deletionTimestamp"): return None
    rs = sets[0]; rs_status = rs.get("status", {})
    if (rs.get("spec", {}).get("replicas") != count or not includes(deployment["spec"]["template"]["spec"], rs.get("spec", {}).get("template", {}).get("spec", {}))
        or type(rs["metadata"].get("generation")) is not int or rs_status.get("observedGeneration") != rs["metadata"]["generation"] or any(rs_status.get(k) != count for k in ("replicas", "readyReplicas", "availableReplicas"))): return None
    pods = items(client, "pods", namespace, selector); template = deployment["spec"]["template"]["spec"]
    if len(pods) != count or any(not controlled(p, "ReplicaSet", sets[0]["metadata"]["uid"]) or not pod_ready(p) or not includes(template, p.get("spec", {})) for p in pods): return None
    if {p["spec"].get("nodeName") for p in pods} != {m["node"] for m in selected["members"]}: return None
    if any(not safe_pod(p["spec"]) for p in pods): return None
    disruption = current["poddisruptionbudget/platform-keycloak"]
    if disruption.get("status", {}).get("observedGeneration") != disruption["metadata"].get("generation") or disruption.get("status", {}).get("currentHealthy") != count or disruption.get("status", {}).get("expectedPods") != count or disruption.get("status", {}).get("disruptionsAllowed", 0) < 1: return None
    service = current["service/platform-keycloak"]
    try: ipaddress.ip_address(service["spec"]["clusterIP"])
    except (KeyError, ValueError): return None
    by_uid = {p["metadata"]["uid"]: p for p in pods}; routed = set()
    for item in items(client, "endpointslices.discovery.k8s.io", namespace, "kubernetes.io/service-name=platform-keycloak"):
        if not controlled(item, "Service", service["metadata"]["uid"]) or item.get("metadata", {}).get("deletionTimestamp") or item.get("addressType") not in {"IPv4", "IPv6"} or not any(p.get("name") == "https" and p.get("port") == 8443 and p.get("protocol", "TCP") == "TCP" for p in item.get("ports", [])): return None
        for endpoint in item.get("endpoints", []):
            target = endpoint.get("targetRef", {}); pod = by_uid.get(target.get("uid"))
            if not pod or target.get("kind") != "Pod" or target.get("namespace") != namespace or target.get("name") != pod["metadata"]["name"] or endpoint.get("nodeName") != pod["spec"]["nodeName"] or endpoint.get("conditions", {}).get("ready") is not True or endpoint.get("conditions", {}).get("terminating") or not endpoint.get("addresses") or set(endpoint["addresses"]) != {pod.get("status", {}).get("podIP")}: return None
            try: address = ipaddress.ip_address(endpoint["addresses"][0])
            except ValueError: return None
            if item["addressType"] != "IPv" + str(address.version): return None
            routed.add(target["uid"])
    if routed != set(by_uid): return None
    return {p["metadata"]["uid"]: {"node": p["spec"]["nodeName"], "container_id": p["status"]["containerStatuses"][0]["containerID"], "image_id": p["status"]["containerStatuses"][0]["imageID"]} for p in pods}


@contextmanager
def lock(directory):
    path = directory / "operation.lock"
    if path.is_symlink(): raise ValueError("Operation lock cannot be a symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Untrusted private lock")
        if os.name == "nt":
            import msvcrt
            if info.st_size == 0: os.write(fd, b"0")
            os.lseek(fd, 0, 0); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def operate(bundle, plan, *, mode="local", output=None, wait_seconds=180, client=None):
    contract, objects = load_package(bundle, plan["contract_sha256"]); validate(plan, contract); tools = toolset()
    result = {"local_inputs_verified": True, "server_checked": False, "resources_observed": False, "resources_ready": False, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 1800: raise ValueError("Select one bounded server action and private journal")
    if mode != "check-server" and plan["admitted_sha256"] is None: raise ValueError("Inspect and pin server admission before creation")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use an original private operation directory")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume needs its original journal directory")
    receipt = directory / "keycloak-install-proof.json"; selected = contract["plan"]; namespace = selected["namespace"]
    with lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("Journal cannot redirect elsewhere")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply" or state.get("plan_sha256") != fingerprint(plan) or state.get("contract_sha256") != plan["contract_sha256"] or state.get("toolset_sha256") != tools
                or not isinstance(state.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"]) or not isinstance(state.get("objects"), dict) or set(state["objects"]) != {key(o) for o in objects} or any(state.get(k) is not False for k in UNVERIFIED)): raise ValueError("Resume only the matching original installation")
            for entry in state["objects"].values():
                renderer.exact(entry, "attempted uid admitted_sha256")
                if type(entry["attempted"]) is not bool or entry["uid"] is not None and (not uid(entry["uid"]) or not entry["attempted"]) or entry["admitted_sha256"] is not None and not sha(entry["admitted_sha256"]): raise ValueError("Invalid original object handle")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": uuid4().hex, "plan_sha256": fingerprint(plan), "contract_sha256": plan["contract_sha256"], "toolset_sha256": tools,
                "objects": {key(o): {"attempted": False, "uid": None, "admitted_sha256": None} for o in objects}, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        record("local-validated", **result)
        for obj in objects: obj["metadata"]["annotations"] = {OPERATION: state["operation_id"], CONTRACT: plan["contract_sha256"]}
        wrote = False
        try:
            network = client or Client(plan["target"])
            with network.frozen():
                prerequisites(network, plan, selected)
                if not any(entry["attempted"] for entry in state["objects"].values()):
                    fresh_children(network, selected)
                    for obj in objects:
                        if network.get(key(obj), namespace, optional=True): raise ValueError("A target resource already exists")
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=canonical(obj).encode(), seconds=60))
                        state["objects"][key(obj)]["admitted_sha256"] = admitted(obj, response)
                        atomic_write_text(directory / (obj["kind"].lower() + "-admitted.json"), canonical(material(response)))
                    aggregate = fingerprint({k: v["admitted_sha256"] for k, v in state["objects"].items()}); record("admission-reviewed", server_checked=True, admitted_sha256=aggregate)
                    if plan["admitted_sha256"] is not None and aggregate != plan["admitted_sha256"]: raise ValueError("Server admission differs from the reviewed policy")
                    if mode == "check-server": return {**result, "server_checked": True, "admitted_sha256": aggregate}
                if fingerprint({k: v["admitted_sha256"] for k, v in state["objects"].items()}) != plan["admitted_sha256"]: raise ValueError("The original admitted policy changed")
                for obj in objects:
                    entry = state["objects"][key(obj)]; current = network.get(key(obj), namespace, optional=True)
                    if not entry["attempted"]:
                        if current: raise ValueError("A new foreign object appeared")
                        prerequisites(network, plan, selected)
                        if obj["kind"] == "Deployment": fresh_children(network, selected, workloads_only=True)
                        if toolset() != tools: raise ValueError("Installation sources changed")
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=canonical(obj).encode(), seconds=60))
                        if admitted(obj, response) != entry["admitted_sha256"]: raise ValueError("Admission changed before creation")
                        entry["attempted"] = True; record("before-" + key(obj), read_only=False); wrote = True
                        current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=canonical(obj).encode(), seconds=60))
                        if uid(current.get("metadata", {}).get("uid")): entry["uid"] = current["metadata"]["uid"]; record("create-response-observed")
                    deadline = time.monotonic() + wait_seconds
                    while not current:
                        if entry["uid"] is not None: raise ValueError("An original resource is missing; never recreate automatically")
                        if time.monotonic() >= deadline: raise TimeoutError("Unknown create outcome; observe this same handle again")
                        time.sleep(1); current = network.get(key(obj), namespace, optional=True)
                    owned(obj, current, entry, state); entry["uid"] = current["metadata"]["uid"]; record("observed-" + key(obj))
                deadline = time.monotonic() + wait_seconds
                while True:
                    current = {key(obj): owned(obj, network.get(key(obj), namespace, optional=True), state["objects"][key(obj)], state) for obj in objects}
                    snapshot = ready(network, selected, current)
                    if snapshot:
                        prerequisites(network, plan, selected)
                        final = {key(obj): owned(obj, network.get(key(obj), namespace, optional=True), state["objects"][key(obj)], state) for obj in objects}
                        if ready(network, selected, final) != snapshot: raise ValueError("Readiness identities changed during final observation")
                        if toolset() != tools: raise ValueError("Installation sources changed")
                        result.update(server_checked=True, resources_observed=True, resources_ready=True, read_only=not wrote); record("completed", observed_pods=snapshot, **result); return result
                    if time.monotonic() >= deadline: raise TimeoutError("Original resources remain unready; preserve the journal")
                    record("waiting-for-original-workloads", server_checked=True, resources_observed=True); time.sleep(2)
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="keycloak-installation-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--deployment-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=180)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("check-server", "apply", "resume"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.deployment_plan, max_bytes=128 * 1024))
        mode = next((name.replace("_", "-") for name in ("check_server", "apply", "resume") if getattr(args, name)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Keycloak installation stopped; preserve the private journal and original resource handles. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
