#!/usr/bin/env python3
"""Explicit-target fresh CNPG creation and observation; never adopts, upgrades or deletes data."""

import argparse
import ast
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
from kubernetes_management import Client, validate_target, verify_target
import postgres_ha_policy as policy
import render_postgres_ha as renderer
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SHA = re.compile(r"[0-9a-f]{64}")
OPERATION = "platform.gitops/postgres-operation"
CONTRACT = "platform.gitops/postgres-contract"
CLUSTER = "clusters.postgresql.cnpg.io/"


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def uid(value): return isinstance(value, str) and bool(UID.fullmatch(value))


def includes(expected, actual):
    if isinstance(expected, dict): return isinstance(actual, dict) and all(k in actual and includes(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list): return isinstance(actual, list) and len(expected) == len(actual) and all(includes(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def toolset():
    root = Path(__file__).resolve().parent
    found, pending = {}, [Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name + ".py" in found: continue
        source = read_bounded_text(root / (name + ".py"), encoding="utf-8", max_bytes=2 * 1024 ** 2)
        found[name + ".py"] = hashlib.sha256(source.encode()).hexdigest()
        for item in ast.walk(ast.parse(source)):
            modules = [entry.name for entry in item.names] if isinstance(item, ast.Import) else [item.module] if isinstance(item, ast.ImportFrom) and item.module else []
            for module in modules:
                dependency = module.split(".")[0]
                if (root / (dependency + ".py")).is_file() and dependency + ".py" not in found: pending.append(dependency)
    return fingerprint(found)


def load_package(bundle, digest):
    raw = read_bounded_bytes(bundle / "postgres-contract.json", max_bytes=256 * 1024)
    if not isinstance(digest, str) or not SHA.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest: raise ValueError("PostgreSQL contract differs")
    contract = loads_strict_json(raw)
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "files_sha256"} or type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise ValueError("Invalid PostgreSQL contract")
    if contract["runtime"] not in {"rke2", "k3s", "k8s"}: raise ValueError("This installer requires a Kubernetes CNPG package")
    files = renderer.render(contract["plan"], contract["runtime"])
    if files["postgres-contract.json"].encode() != raw: raise ValueError("PostgreSQL package differs from regenerated policy")
    for name, value in files.items():
        if read_bounded_bytes(bundle / name, max_bytes=4 * 1024 ** 2) != value.encode(): raise ValueError("PostgreSQL package artifact changed")
    return contract, loads_strict_yaml_all(files["postgres-resources.yaml"])[0], loads_strict_json(files["cnpg-cluster-schema.json"])


def validate(plan, contract, digest):
    required = {"schema_version", "contract_sha256", "target", "namespace_uid", "operator", "storage_class_uid", "storage_parameters_sha256", "secrets", "admitted_spec_sha256"}
    if (not isinstance(plan, dict) or set(plan) != required or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or plan["contract_sha256"] != digest or not uid(plan["namespace_uid"]) or not uid(plan["storage_class_uid"])
            or not isinstance(plan["storage_parameters_sha256"], str) or not SHA.fullmatch(plan["storage_parameters_sha256"])
            or plan["admitted_spec_sha256"] is not None and (not isinstance(plan["admitted_spec_sha256"], str) or not SHA.fullmatch(plan["admitted_spec_sha256"]))):
        raise ValueError("Use the original reviewed deployment scope and admission fingerprint")
    validate_target(plan["target"])
    operator = plan["operator"]
    if (not isinstance(operator, dict) or set(operator) != {"namespace", "namespace_uid", "deployment", "uid", "image", "crd_uid"}
            or any(not uid(operator[k]) for k in ("namespace_uid", "uid", "crd_uid")) or any(not policy.label(operator[k]) for k in ("namespace", "deployment"))
            or not isinstance(operator["image"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", operator["image"])):
        raise ValueError("Pin the operator deployment, namespace, image and API identity")
    selected = contract["plan"]
    if selected["bootstrap"]["secret"] in {selected["tls"]["server_ca_secret"], selected["tls"]["server_secret"]}: raise ValueError("Separate application credentials from TLS trust snapshots")
    names = {selected["tls"]["server_ca_secret"], selected["tls"]["server_secret"], selected["bootstrap"]["secret"]}
    if (not isinstance(plan["secrets"], dict) or set(plan["secrets"]) != names
            or any(not isinstance(item, dict) or set(item) != {"uid", "type"} or not uid(item["uid"]) or item["type"] not in {"Opaque", "kubernetes.io/tls", "kubernetes.io/basic-auth"} for item in plan["secrets"].values())):
        raise ValueError("Bind every private Secret reference by UID and type")
    if (plan["secrets"][selected["tls"]["server_secret"]]["type"] != "kubernetes.io/tls"
            or plan["secrets"][selected["bootstrap"]["secret"]]["type"] != "kubernetes.io/basic-auth"
            or names & {selected["name"] + suffix for suffix in ("-ca", "-replication", "-superuser")}):
        raise ValueError("Keep external Secret snapshots separate from controller-generated identities")


def collection(client, resource, namespace=None, selector=None):
    args = ["get", resource, "-o", "json"]
    if namespace: args += ["--namespace", namespace]
    if selector: args += ["--selector", selector]
    value = loads_strict_json(client.call(args))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > 4096: raise ValueError("Invalid bounded Kubernetes collection")
    return value["items"]


def condition(obj, name): return any(c.get("type") == name and c.get("status") == "True" for c in obj.get("status", {}).get("conditions", []))
def controlled(obj, kind, owner): return any(r.get("controller") is True and r.get("kind") == kind and r.get("uid") == owner for r in obj.get("metadata", {}).get("ownerReferences", []))


def node(client, name, *, expected=None, domain_key=None):
    obj = client.get("node/" + name) or {}; meta, spec = obj.get("metadata", {}), obj.get("spec", {})
    if not uid(meta.get("uid")) or meta.get("deletionTimestamp") or not condition(obj, "Ready"): raise ValueError("A selected node is unavailable")
    if expected and (spec.get("unschedulable") or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in spec.get("taints", []))): raise ValueError("A selected database node cannot schedule the new workload")
    if expected and (meta["uid"] != expected["node_uid"] or meta.get("labels", {}).get("kubernetes.io/hostname") != name
            or meta.get("labels", {}).get(domain_key) != expected["failure_domain"]
            or expected["address"] not in {a.get("address") for a in obj.get("status", {}).get("addresses", []) if a.get("type") == "InternalIP"}):
        raise ValueError("Database node identity, address or failure domain changed")
    return obj


def running_pod(pod, allowed):
    spec, status = pod.get("spec", {}), pod.get("status", {})
    containers = spec.get("containers", []); statuses = status.get("containerStatuses", [])
    return (uid(pod.get("metadata", {}).get("uid")) and not pod["metadata"].get("deletionTimestamp") and status.get("phase") == "Running" and condition(pod, "Ready")
        and bool(containers) and all(c.get("image") in allowed for c in containers + spec.get("initContainers", []))
        and {c.get("name") for c in containers} == {c.get("name") for c in statuses}
        and all(c.get("ready") is True and c.get("state", {}).get("running") is not None for c in statuses)
        and len(spec.get("initContainers", [])) == len(status.get("initContainerStatuses", []))
        and all(c.get("state", {}).get("terminated", {}).get("exitCode") == 0 for c in status.get("initContainerStatuses", [])))


def private_references(client, plan, selected):
    namespace = selected["namespace"]
    keys = ("ca.crt", "tls.crt", "tls.key", "username", "password")
    template = '{{.metadata.uid}}{{"\\n"}}{{.type}}{{"\\n"}}{{.immutable}}{{"\\n"}}' + ''.join('{{if index .data ' + json.dumps(k) + '}}true{{else}}false{{end}}{{"\\n"}}' for k in keys)
    for name, pin in plan["secrets"].items():
        output = client.call(["get", "secret/" + name, "--namespace", namespace, "-o", "go-template=" + template]).splitlines()
        if len(output) != 8 or output[:3] != [pin["uid"], pin["type"], "true"] or any(v not in {"true", "false"} for v in output[3:]): raise ValueError("Provision matching immutable Secret snapshots")
        present = dict(zip(keys, (value == "true" for value in output[3:])))
        required = {"ca.crt"} if name == selected["tls"]["server_ca_secret"] else set()
        if name == selected["tls"]["server_secret"]: required |= {"tls.crt", "tls.key"}
        if name == selected["bootstrap"]["secret"]: required |= {"username", "password"}
        if any(not present[k] for k in required): raise ValueError("Required private input key is missing")
    def public(name, key, limit):
        value = client.call(["get", "secret/" + name, "--namespace", namespace, "-o", "go-template={{index .data " + json.dumps(key) + "}}"]).strip()
        raw = base64.b64decode(value, validate=True)
        if len(raw) > limit: raise ValueError("Excessive public Secret field")
        return raw
    ca = public(selected["tls"]["server_ca_secret"], "ca.crt", 256 * 1024)
    if hashlib.sha256(ca).hexdigest() != selected["tls"]["ca_sha256"]["postgres"]: raise ValueError("Database trust anchor changed")
    if b"PRIVATE KEY" in ca: raise ValueError("The public trust field must not contain private key material")
    trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    trust.load_verify_locations(cadata=ca.decode("ascii"))
    if public(selected["bootstrap"]["secret"], "username", 128).decode() != selected["bootstrap"]["owner"]: raise ValueError("Bootstrap Secret does not identify the reviewed application owner")


def prerequisites(client, plan, contract, schema):
    selected, operator = contract["plan"], plan["operator"]
    verify_target(client, plan["target"])
    for name, expected in ((selected["namespace"], plan["namespace_uid"]), (operator["namespace"], operator["namespace_uid"])):
        ns = client.get("namespace/" + name) or {}
        if ns.get("metadata", {}).get("uid") != expected or ns.get("metadata", {}).get("deletionTimestamp"): raise ValueError("Namespace identity changed")
        if name == selected["namespace"] and ns.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted": raise ValueError("Require restricted workload admission in the database namespace")
    definition = client.get("customresourcedefinition/clusters.postgresql.cnpg.io") or {}
    versions = [v for v in definition.get("spec", {}).get("versions", []) if v.get("name") == "v1" and v.get("served") is True and v.get("storage") is True]
    if definition.get("metadata", {}).get("uid") != operator["crd_uid"] or not condition(definition, "Established") or len(versions) != 1 or fingerprint(versions[0].get("schema", {}).get("openAPIV3Schema")) != fingerprint(schema):
        raise ValueError("The established CNPG API differs from its reviewed schema/identity")
    for member in selected["members"]: node(client, member["node_name"], expected=member, domain_key=selected["failure_domain_key"])
    deployment = client.get("deployment/" + operator["deployment"], operator["namespace"]) or {}
    meta, spec, status = deployment.get("metadata", {}), deployment.get("spec", {}), deployment.get("status", {})
    replicas = spec.get("replicas"); template = spec.get("template", {}).get("spec", {})
    containers = template.get("containers", []); revision = meta.get("annotations", {}).get("deployment.kubernetes.io/revision")
    if (meta.get("uid") != operator["uid"] or meta.get("deletionTimestamp") or meta.get("labels", {}).get("app.kubernetes.io/version") != selected["cnpg_version"]
            or type(replicas) is not int or replicas < 2 or type(meta.get("generation")) is not int or status.get("observedGeneration") != meta["generation"] or not revision
            or any(status.get(k) != replicas for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
            or len(containers) != 1 or containers[0].get("image") != operator["image"] or containers[0].get("command") != ["/manager"]): raise ValueError("The pinned HA operator is not fully rolled out")
    args = containers[0].get("args", [])
    if not args or args[0] != "controller" or [a for a in args if a.startswith("--leader-elect")] not in (["--leader-elect"], ["--leader-elect=true"]): raise ValueError("Require operator leader election")
    image_vars = [v for v in containers[0].get("env", []) if v.get("name") == "OPERATOR_IMAGE_NAME"]
    if image_vars != [{"name": "OPERATOR_IMAGE_NAME", "value": operator["image"]}]: raise ValueError("Pin the operator's injected instance-manager image")
    sets = {rs.get("metadata", {}).get("uid") for rs in collection(client, "replicasets", operator["namespace"]) if controlled(rs, "Deployment", operator["uid"]) and rs.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision") == revision}
    pods = [p for p in collection(client, "pods", operator["namespace"]) if any(controlled(p, "ReplicaSet", rs) for rs in sets) and running_pod(p, {operator["image"]})]
    domains = {node(client, p["spec"]["nodeName"])["metadata"].get("labels", {}).get(selected["failure_domain_key"]) for p in pods}
    if len(pods) != replicas or len({p["spec"]["nodeName"] for p in pods}) < 2 or None in domains or len(domains) < 2: raise ValueError("Current operator pods must span nodes and failure domains")
    storage = client.get("storageclass/" + selected["storage"]["class"]) or {}
    if (storage.get("metadata", {}).get("uid") != plan["storage_class_uid"] or storage.get("metadata", {}).get("deletionTimestamp")
            or not str(storage.get("provisioner", "")).endswith("rbd.csi.ceph.com") or storage.get("reclaimPolicy") != "Retain" or storage.get("allowVolumeExpansion") is not True
            or fingerprint(storage.get("parameters", {})) != plan["storage_parameters_sha256"]): raise ValueError("Require the reviewed expandable Ceph RBD Retain storage class")
    private_references(client, plan, selected)


def fresh(client, selected):
    namespace, name = selected["namespace"], selected["name"]
    if client.get(CLUSTER + name, namespace, optional=True): raise ValueError("Fresh installation cannot adopt an existing database")
    for resource in ("pods", "persistentvolumeclaims", "services"):
        for item in collection(client, resource, namespace):
            meta = item.get("metadata", {})
            if meta.get("labels", {}).get("cnpg.io/cluster") == name or meta.get("name") == name or str(meta.get("name", "")).startswith(name + "-"):
                raise ValueError("Existing database children require explicit recovery instead of adoption")
    for suffix in ("-ca", "-replication", "-superuser"):
        if client.call(["get", "secret/" + name + suffix, "--namespace", namespace, "--ignore-not-found=true", "-o", "go-template={{.metadata.uid}}"]).strip():
            raise ValueError("A controller-generated database identity already exists")
    for volume in collection(client, "persistentvolumes"):
        spec = volume.get("spec", {}); claim = spec.get("claimRef", {})
        if spec.get("storageClassName") == selected["storage"]["class"] and (volume.get("status", {}).get("phase") == "Available" or claim.get("namespace") == namespace and str(claim.get("name", "")).startswith(name + "-")):
            raise ValueError("A retained/pre-existing volume could bind old database data")


def admitted(expected, actual):
    if (not isinstance(actual, dict) or actual.get("apiVersion") != expected["apiVersion"] or actual.get("kind") != "Cluster"
            or not includes(expected["metadata"], actual.get("metadata", {})) or not includes(expected["spec"], actual.get("spec", {}))): raise ValueError("Admission weakened the reviewed database policy")
    spec = actual["spec"]
    if any(spec.get(k) for k in ("replica", "externalClusters", "managed", "plugins", "walStorage", "tablespaces", "imageCatalogRef")) or set(spec.get("bootstrap", {})) != {"initdb"}:
        raise ValueError("Review alternate bootstrap, database roles or external sources separately")
    initdb = spec["bootstrap"]["initdb"]
    if spec.get("storage", {}).get("pvcTemplate") or any(initdb.get(k) for k in initdb if k == "import" or k == "options" or k.startswith("postInit")):
        raise ValueError("Fresh installation cannot import data, run SQL hooks or adopt a PVC template")
    return fingerprint(spec)


def owned(current, expected, state):
    if (not current or not uid(current.get("metadata", {}).get("uid")) or current["metadata"].get("deletionTimestamp")
            or state["cluster_uid"] is not None and state["cluster_uid"] != current["metadata"]["uid"] or admitted(expected, current) != state["admitted_spec_sha256"]): raise ValueError("The original database UID, ownership or admitted policy changed")
    return current


def ready(client, selected, current, allowed):
    namespace, name = selected["namespace"], selected["name"]
    status = current.get("status", {})
    if not condition(current, "Ready") or status.get("readyInstances") != 3 or status.get("instances") != 3 or status.get("targetPrimary") != status.get("currentPrimary"): return False
    pods = collection(client, "pods", namespace, "cnpg.io/cluster=" + name)
    if len(pods) != 3 or any(not controlled(p, "Cluster", current["metadata"]["uid"]) or not running_pod(p, allowed) for p in pods): return False
    if {p["spec"].get("nodeName") for p in pods} != {m["node_name"] for m in selected["members"]}: return False
    reports = status.get("instancesReportedState", {})
    primary = [p for p in pods if reports.get(p["metadata"]["name"], {}).get("isPrimary") is True]
    if set(reports) != {p["metadata"]["name"] for p in pods} or len(primary) != 1 or primary[0]["metadata"]["name"] != status["currentPrimary"]: return False
    claims, handles = set(), set()
    for pod in pods:
        pod_spec = pod["spec"]
        if (any(pod_spec.get(key) for key in ("hostPID", "hostIPC", "hostNetwork")) or any("hostPath" in volume for volume in pod_spec.get("volumes", []))
                or any(c.get("securityContext", {}).get("privileged") is True or c.get("securityContext", {}).get("allowPrivilegeEscalation") is True for c in pod_spec["containers"] + pod_spec.get("initContainers", []))): return False
        references = [v["persistentVolumeClaim"]["claimName"] for v in pod["spec"].get("volumes", []) if "persistentVolumeClaim" in v]
        if len(references) != 1: return False
        claim = client.get("persistentvolumeclaim/" + references[0], namespace) or {}; spec = claim.get("spec", {})
        if (not uid(claim.get("metadata", {}).get("uid")) or claim["metadata"].get("deletionTimestamp") or claim.get("status", {}).get("phase") != "Bound"
                or spec.get("storageClassName") != selected["storage"]["class"] or not spec.get("volumeName")
                or spec.get("accessModes") != ["ReadWriteOnce"]
                or not (controlled(claim, "Cluster", current["metadata"]["uid"]) or controlled(claim, "Pod", pod["metadata"]["uid"]))): return False
        volume = client.get("persistentvolume/" + spec["volumeName"]) or {}; vspec = volume.get("spec", {}); binding = vspec.get("claimRef", {})
        if (not uid(volume.get("metadata", {}).get("uid")) or volume["metadata"].get("deletionTimestamp") or volume.get("status", {}).get("phase") != "Bound"
                or vspec.get("persistentVolumeReclaimPolicy") != "Retain" or vspec.get("storageClassName") != selected["storage"]["class"]
                or not str(vspec.get("csi", {}).get("driver", "")).endswith("rbd.csi.ceph.com") or binding.get("uid") != claim["metadata"]["uid"] or binding.get("namespace") != namespace or binding.get("name") != references[0]): return False
        handle = vspec.get("csi", {}).get("volumeHandle")
        if not isinstance(handle, str) or not handle: return False
        handles.add(handle)
        claims.add(claim["metadata"]["uid"])
    if len(claims) != 3 or len(handles) != 3: return False
    service = client.get("service/" + name + "-rw", namespace) or {}
    if (not controlled(service, "Cluster", current["metadata"]["uid"]) or service.get("metadata", {}).get("deletionTimestamp") or service.get("spec", {}).get("type", "ClusterIP") != "ClusterIP"
            or len([p for p in service.get("spec", {}).get("ports", []) if p.get("port") == 5432 and p.get("protocol", "TCP") == "TCP"]) != 1): return False
    endpoints = [e for item in collection(client, "endpointslices.discovery.k8s.io", namespace, "kubernetes.io/service-name=" + name + "-rw")
        if controlled(item, "Service", service.get("metadata", {}).get("uid")) and not item.get("metadata", {}).get("deletionTimestamp") and any(p.get("port") == 5432 and p.get("protocol", "TCP") == "TCP" for p in item.get("ports", [])) for e in item.get("endpoints", []) if e.get("conditions", {}).get("ready") is True and not e.get("conditions", {}).get("terminating")]
    primary_ips = {item["ip"] for item in primary[0].get("status", {}).get("podIPs", []) if item.get("ip")} | {primary[0].get("status", {}).get("podIP")}
    return bool(endpoints) and all(e.get("targetRef", {}).get("uid") == primary[0]["metadata"]["uid"] and e.get("targetRef", {}).get("kind") == "Pod"
        and e.get("targetRef", {}).get("name") == primary[0]["metadata"]["name"] and e.get("targetRef", {}).get("namespace") == namespace
        and e.get("addresses") and set(e["addresses"]) <= primary_ips for e in endpoints)


@contextmanager
def lock(directory):
    path = directory / "operation.lock"
    if path.is_symlink(): raise ValueError("Operation lock cannot be a symlink")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Untrusted private operation lock")
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0: os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET); msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(descriptor)


def operate(bundle, plan, *, mode="local", output=None, wait_seconds=180, client=None):
    contract, expected, schema = load_package(bundle, plan["contract_sha256"]); validate(plan, contract, plan["contract_sha256"])
    tools_sha = toolset()
    result = {"local_inputs_verified": True, "toolset_sha256": tools_sha, "read_only": True, "server_checked": False, "resource_observed": False, "resources_ready": False,
              "synchronous_replication_verified": False, "promotion_verified": False, "restore_verified": False, "ha_verified": False, "full_stack_verified": False}
    if mode == "local": return result
    if mode not in {"check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 1800: raise ValueError("Choose one explicit bounded server operation and private journal")
    if mode != "check-server" and plan["admitted_spec_sha256"] is None: raise ValueError("First inspect and pin the concrete server-admitted specification")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private operation directory")
    if mode != "resume": resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = resolved / "postgres-install-proof.json"
    with lock(resolved):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("Journal cannot be a symlink")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply" or state.get("deployment_plan_sha256") != fingerprint(plan)
                    or state.get("contract_sha256") != plan["contract_sha256"] or state.get("cluster_uid") is not None and state.get("creation_attempted") is not True
                    or state.get("toolset_sha256") != tools_sha
                    or not isinstance(state.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"]) or type(state.get("creation_attempted")) is not bool
                    or state.get("cluster_uid") is not None and not uid(state["cluster_uid"]) or state.get("admitted_spec_sha256") != plan["admitted_spec_sha256"]
                    or any(state.get(k) is not False for k in ("synchronous_replication_verified", "promotion_verified", "restore_verified", "ha_verified", "full_stack_verified"))): raise ValueError("Resume only the matching original installation journal")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": uuid4().hex, "deployment_plan_sha256": fingerprint(plan), "contract_sha256": plan["contract_sha256"],
                     "cluster_uid": None, "creation_attempted": False, "admitted_spec_sha256": plan["admitted_spec_sha256"], "phase": "local-validated", **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        record("resume-started" if mode == "resume" else state["phase"], **result)
        expected["metadata"]["annotations"] = {OPERATION: state["operation_id"], CONTRACT: plan["contract_sha256"]}
        selected = contract["plan"]; namespace, name = selected["namespace"], selected["name"]
        wrote = False
        try:
            network = client or Client(plan["target"])
            with network.frozen():
                prerequisites(network, plan, contract, schema); record("prerequisites-verified")
                if not state["creation_attempted"]:
                    fresh(network, selected)
                    response = loads_strict_json(network.call(["create", "-f", "-", "--dry-run=server", "-o", "json"], payload=canonical(expected).encode(), seconds=60))
                    spec_hash = admitted(expected, response)
                    atomic_write_text(resolved / "admitted-spec.json", canonical(response["spec"]))
                    record("admission-reviewed", observed_admitted_spec_sha256=spec_hash, server_checked=True)
                    if plan["admitted_spec_sha256"] is not None and plan["admitted_spec_sha256"] != spec_hash: raise ValueError("Server admission changed from the reviewed snapshot")
                    if mode == "check-server":
                        record("server-check-completed", admitted_spec_sha256=spec_hash)
                        return {**result, "server_checked": True, "admitted_spec_sha256": spec_hash}
                    prerequisites(network, plan, contract, schema); fresh(network, selected)
                    record("before-cluster-create", creation_attempted=True, read_only=False)
                    wrote = True
                    current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=canonical(expected).encode(), seconds=60))
                    if uid(current.get("metadata", {}).get("uid")):
                        record("create-response-observed", cluster_uid=current["metadata"]["uid"], resource_observed=True)
                    owned(current, expected, state); record("cluster-created", cluster_uid=current["metadata"]["uid"], resource_observed=True)
                deadline = time.monotonic() + wait_seconds
                while True:
                    current = network.get(CLUSTER + name, namespace, optional=True)
                    if current:
                        owned(current, expected, state); record("observing-cluster", cluster_uid=current["metadata"]["uid"], resource_observed=True)
                        if ready(network, selected, current, {selected["images"]["postgres"], plan["operator"]["image"]}): break
                    elif state["cluster_uid"] is not None: raise ValueError("The original database object is missing; never recreate its data automatically")
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the same database handle again; do not repeat an uncertain create")
                    record("waiting-for-original-cluster"); time.sleep(2)
                prerequisites(network, plan, contract, schema)
                current = network.get(CLUSTER + name, namespace, optional=True)
                owned(current, expected, state)
                if not ready(network, selected, current, {selected["images"]["postgres"], plan["operator"]["image"]}): raise ValueError("Resource readiness changed during final verification")
                result.update(server_checked=True, resource_observed=True, resources_ready=True, read_only=not wrote)
                record("completed", **result)
                return result
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="postgres-installation-unverified")
            raise


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
        print("PostgreSQL installation stopped. Preserve the private journal and original database handle; private details were suppressed."); return 1


if __name__ == "__main__": raise SystemExit(main())
