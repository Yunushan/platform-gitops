#!/usr/bin/env python3
"""Create only a fresh explicitly scoped OpenBao workload; never initialize/unseal."""

import argparse
import ast
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, validate_target, verify_target
import deploy_keycloak_ha as mechanics
import render_openbao_ha as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/openbao-operation"
CONTRACT = "platform.gitops/openbao-contract"
UNVERIFIED = ("initialized", "unsealed", "raft_membership_verified", "tls_verified", "restricted_identities_verified", "audit_verified",
              "storage_encryption_verified", "network_datapath_verified", "off_platform_restore_verified", "ha_verified", "full_stack_verified")
canonical = mechanics.canonical
fingerprint = mechanics.fingerprint
uid = mechanics.uid
sha = mechanics.sha
includes = mechanics.includes
condition = mechanics.condition
controlled = mechanics.controlled
items = mechanics.items
key = mechanics.key


def toolset():
    directory = Path(__file__).resolve().parent; found = {}; pending = [Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_bytes(directory / (name + ".py"), max_bytes=2 * 1024 ** 2)
        found[name] = hashlib.sha256(raw).hexdigest()
        for node in ast.walk(ast.parse(raw)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for module in modules:
                first = module.split(".")[0]
                if (directory / (first + ".py")).is_file() and first not in found: pending.append(first)
    return fingerprint(found)


def load_package(bundle, digest):
    if not sha(digest): raise ValueError("Pin the original OpenBao contract")
    raw = read_bounded_bytes(bundle / "openbao-contract.json", max_bytes=256 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError("OpenBao contract changed")
    contract = loads_strict_json(raw); renderer.exact(contract, "schema_version runtime plan files_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in {"rke2", "k3s", "k8s"}:
        raise ValueError("This installer requires a Kubernetes OpenBao package")
    files = renderer.render(contract["plan"], contract["runtime"])
    if files["openbao-contract.json"].encode() != raw: raise ValueError("Package differs from current policy")
    for name, value in files.items():
        if read_bounded_bytes(bundle / name, max_bytes=2 * 1024 ** 2) != value.encode(): raise ValueError("A package artifact changed")
    configs = {m["name"] + ".json": json.dumps(renderer.configuration(contract["plan"], m, contract["runtime"]), sort_keys=True, indent=2) + "\n" for m in contract["plan"]["members"]}
    objects = sorted(renderer.kubernetes(contract["plan"], configs), key=lambda o: ("NetworkPolicy", "ConfigMap", "Service", "PodDisruptionBudget", "Deployment").index(o["kind"]))
    return contract, objects


def validate(plan, contract):
    renderer.exact(plan, "schema_version contract_sha256 target namespace_uid node_uids secrets claims storage_classes cilium admitted_sha256")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not sha(plan["contract_sha256"]) or not uid(plan["namespace_uid"]) or plan["admitted_sha256"] is not None and not sha(plan["admitted_sha256"]):
        raise ValueError("Pin a complete original deployment scope")
    validate_target(plan["target"]); selected = contract["plan"]
    if not isinstance(plan["node_uids"], dict) or set(plan["node_uids"]) != {m["node"] for m in selected["members"]} or any(not uid(v) for v in plan["node_uids"].values()) or len(set(plan["node_uids"].values())) != 3:
        raise ValueError("Pin all three different Linux node identities")
    if not isinstance(plan["secrets"], dict) or set(plan["secrets"]) != {m["tls_secret"] for m in selected["members"]} or any(not uid(v) for v in plan["secrets"].values()) or len(set(plan["secrets"].values())) != 3:
        raise ValueError("Pin three different immutable TLS Secret identities")
    names = {m[k] for m in selected["members"] for k in ("data_claim", "audit_claim")}
    if not isinstance(plan["claims"], dict) or set(plan["claims"]) != names: raise ValueError("Pin six existing independent claims")
    for value in plan["claims"].values():
        renderer.exact(value, "uid spec_sha256 volume volume_uid volume_spec_sha256 storage_class")
        if not uid(value["uid"]) or not uid(value["volume_uid"]) or not sha(value["spec_sha256"]) or not sha(value["volume_spec_sha256"]) or not renderer.label(value["volume"]) or not renderer.label(value["storage_class"]):
            raise ValueError("Pin claim and volume identities and specifications")
    for field in ("uid", "volume", "volume_uid"):
        if len({v[field] for v in plan["claims"].values()}) != 6: raise ValueError("Claims and volumes must not alias")
    if not isinstance(plan["storage_classes"], dict) or set(plan["storage_classes"]) != {v["storage_class"] for v in plan["claims"].values()}:
        raise ValueError("Pin every prepared Ceph storage class")
    for value in plan["storage_classes"].values():
        renderer.exact(value, "uid configuration_sha256")
        if not uid(value["uid"]) or not sha(value["configuration_sha256"]): raise ValueError("Pin storage class policy")
    c = plan["cilium"]; renderer.exact(c, "namespace namespace_uid daemonset uid image pod_template_sha256 configmap config_uid config_data_sha256")
    if (any(not renderer.label(c[k]) for k in ("namespace", "daemonset", "configmap")) or any(not uid(c[k]) for k in ("namespace_uid", "uid", "config_uid"))
            or any(not sha(c[k]) for k in ("pod_template_sha256", "config_data_sha256")) or not isinstance(c["image"], str) or not renderer.IMAGE.fullmatch(c["image"])):
        raise ValueError("Pin the original Cilium rollout and policy configuration")


def storage_configuration(obj):
    return {k: v for k, v in obj.items() if k not in {"apiVersion", "kind", "metadata"}}


def quantity(value):
    match = re.fullmatch(r"([1-9][0-9]*)(Ki|Mi|Gi|Ti)?", value) if isinstance(value, str) else None
    if not match: raise ValueError("Use explicit integral storage capacity")
    return int(match[1]) * {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}[match[2]]


def prerequisites(client, plan, selected):
    verify_target(client, plan["target"])
    namespace = selected["namespace"]; ns = client.get("namespace/" + namespace) or {}; meta = ns.get("metadata", {})
    if meta.get("uid") != plan["namespace_uid"] or meta.get("deletionTimestamp") or meta.get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
        raise ValueError("The original restricted namespace is unavailable")
    for member in selected["members"]:
        node = client.get("node/" + member["node"]) or {}; m, spec = node.get("metadata", {}), node.get("spec", {})
        if (m.get("uid") != plan["node_uids"][member["node"]] or m.get("deletionTimestamp") or not condition(node, "Ready") or spec.get("unschedulable")
                or any(t.get("effect") in {"NoSchedule", "NoExecute"} for t in spec.get("taints", [])) or node.get("status", {}).get("nodeInfo", {}).get("operatingSystem") != "linux"
                or m.get("labels", {}).get("kubernetes.io/hostname") != member["node"] or m.get("labels", {}).get("topology.kubernetes.io/zone") != member["failure_domain"]
                or member["address"] not in {a.get("address") for a in node.get("status", {}).get("addresses", []) if a.get("type") == "InternalIP"}):
            raise ValueError("A reviewed node or failure domain changed")
    template = '{{.metadata.uid}}{{"\\n"}}{{.type}}{{"\\n"}}{{.immutable}}{{"\\n"}}{{if .metadata.deletionTimestamp}}true{{else}}false{{end}}{{"\\n"}}' + ''.join(
        '{{if index .data ' + json.dumps(k) + '}}true{{else}}false{{end}}{{"\\n"}}' for k in ("tls.crt", "tls.key", "ca.crt"))
    for name, identity in plan["secrets"].items():
        rows = client.call(["get", "secret/" + name, "--namespace", namespace, "-o", "go-template=" + template]).splitlines()
        if rows != [identity, "kubernetes.io/tls", "true", "false", "true", "true", "true"]: raise ValueError("The immutable TLS Secret identity or key presence changed")
    for name, pin in plan["storage_classes"].items():
        obj = client.get("storageclass/" + name) or {}; m = obj.get("metadata", {})
        if (m.get("uid") != pin["uid"] or m.get("deletionTimestamp") or fingerprint(storage_configuration(obj)) != pin["configuration_sha256"]
                or not re.fullmatch(r"[a-z0-9-]+[.]rbd[.]csi[.]ceph[.]com", obj.get("provisioner", "")) or obj.get("reclaimPolicy") != "Retain"):
            raise ValueError("The reviewed retained Ceph RBD storage policy changed")
    handles = set()
    for name, pin in plan["claims"].items():
        claim = client.get("persistentvolumeclaim/" + name, namespace) or {}; m, spec, status = claim.get("metadata", {}), claim.get("spec", {}), claim.get("status", {})
        if (m.get("uid") != pin["uid"] or m.get("deletionTimestamp") or fingerprint(spec) != pin["spec_sha256"] or spec.get("volumeName") != pin["volume"]
                or spec.get("storageClassName") != pin["storage_class"] or spec.get("volumeMode", "Filesystem") != "Filesystem" or spec.get("accessModes") != ["ReadWriteOnce"]
                or spec.get("dataSource") or spec.get("dataSourceRef") or status.get("phase") != "Bound" or any(c.get("status") == "True" for c in status.get("conditions", []))
                or quantity(status.get("capacity", {}).get("storage")) < quantity(spec.get("resources", {}).get("requests", {}).get("storage"))):
            raise ValueError("An original claim is not independently bound and ready")
        volume = client.get("persistentvolume/" + pin["volume"]) or {}; m, spec = volume.get("metadata", {}), volume.get("spec", {})
        ref = spec.get("claimRef", {}); csi = spec.get("csi", {}); storage_class = client.get("storageclass/" + pin["storage_class"])
        if (m.get("uid") != pin["volume_uid"] or m.get("deletionTimestamp") or fingerprint(spec) != pin["volume_spec_sha256"] or volume.get("status", {}).get("phase") != "Bound"
                or ref.get("uid") != pin["uid"] or ref.get("name") != name or ref.get("namespace") != namespace or spec.get("storageClassName") != pin["storage_class"]
                or spec.get("persistentVolumeReclaimPolicy") != "Retain" or spec.get("accessModes") != ["ReadWriteOnce"] or spec.get("volumeMode", "Filesystem") != "Filesystem"
                or not csi.get("volumeHandle") or csi.get("driver") != storage_class["provisioner"] or csi.get("fsType") not in {"ext4", "xfs"} or csi.get("readOnly")
                or quantity(spec.get("capacity", {}).get("storage")) < quantity(claim["spec"]["resources"]["requests"]["storage"])):
            raise ValueError("An original retained Ceph volume or claim binding changed")
        handle = (csi["driver"], csi["volumeHandle"])
        if handle in handles: raise ValueError("Distinct claims must not alias one physical CSI volume")
        handles.add(handle)
    c = plan["cilium"]; ns = client.get("namespace/" + c["namespace"]) or {}; config = client.get("configmap/" + c["configmap"], c["namespace"]) or {}
    if (ns.get("metadata", {}).get("uid") != c["namespace_uid"] or ns["metadata"].get("deletionTimestamp") or config.get("metadata", {}).get("uid") != c["config_uid"]
            or config["metadata"].get("deletionTimestamp") or config.get("binaryData") or fingerprint(config.get("data", {})) != c["config_data_sha256"]
            or config.get("data", {}).get("enable-policy") not in {"default", "always"} or config["data"].get("enable-k8s-networkpolicy") != "true"):
        raise ValueError("The original Cilium policy enforcement configuration changed")
    ds = client.get("daemonset/" + c["daemonset"], c["namespace"]) or {}; metadata, status = ds.get("metadata", {}), ds.get("status", {}); pod_template = ds.get("spec", {}).get("template", {})
    count = status.get("desiredNumberScheduled")
    if (metadata.get("uid") != c["uid"] or metadata.get("deletionTimestamp") or type(metadata.get("generation")) is not int or status.get("observedGeneration") != metadata["generation"]
            or type(count) is not int or count < 3 or any(status.get(k) != count for k in ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable"))
            or status.get("numberMisscheduled", 0) != 0 or fingerprint(pod_template) != c["pod_template_sha256"]): raise ValueError("The reviewed Cilium rollout is unavailable")
    images = {x.get("name"): x.get("image") for x in pod_template.get("spec", {}).get("containers", [])}
    init = {x.get("name"): x.get("image") for x in pod_template.get("spec", {}).get("initContainers", [])}
    if c["image"] not in images.values() or any(not isinstance(v, str) or not renderer.IMAGE.fullmatch(v) for v in [*images.values(), *init.values()]):
        raise ValueError("Pin every Cilium agent and init image")
    pods = items(client, "pods", c["namespace"])
    for member in selected["members"]:
        found = [p for p in pods if p.get("spec", {}).get("nodeName") == member["node"] and controlled(p, "DaemonSet", c["uid"]) and mechanics.pod_ready(p)
            and {x.get("name"): x.get("image") for x in p["spec"].get("containers", [])} == images and {x.get("name"): x.get("image") for x in p["spec"].get("initContainers", [])} == init]
        if len(found) != 1: raise ValueError("Each reviewed node needs its original Ready Cilium agent")


def safe_pod(pod, *, allow_node=False):
    if any(pod.get(k) for k in ("hostPID", "hostIPC", "hostNetwork", "shareProcessNamespace", "initContainers", "ephemeralContainers", "runtimeClassName", "dnsConfig", "hostAliases", "priorityClassName")):
        return False
    if not allow_node and pod.get("nodeName") or pod.get("schedulerName") not in {None, "default-scheduler"}: return False
    if pod.get("serviceAccountName") not in {None, "default"} or pod.get("automountServiceAccountToken") is not False: return False
    context = pod.get("securityContext", {})
    if context.get("sysctls") or context.get("supplementalGroups") or context.get("runAsUser") != 1000 or context.get("runAsGroup") != 1000 or context.get("runAsNonRoot") is not True or context.get("seccompProfile") != {"type": "RuntimeDefault"}: return False
    for container in pod.get("containers", []):
        if any(container.get(k) for k in ("env", "envFrom", "args", "lifecycle", "stdin", "stdinOnce", "tty", "workingDir")): return False
        ctx = container.get("securityContext", {})
        if (ctx.get("privileged") or ctx.get("allowPrivilegeEscalation") is not False or ctx.get("readOnlyRootFilesystem") is not True or ctx.get("capabilities") != {"drop": ["ALL"]}
                or ctx.get("procMount") not in {None, "Default"} or ctx.get("runAsUser", 1000) != 1000 or ctx.get("runAsGroup", 1000) != 1000 or ctx.get("runAsNonRoot") is False
                or ctx.get("seccompProfile", {"type": "RuntimeDefault"}) != {"type": "RuntimeDefault"}): return False
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe = container.get(name, {}); handlers = [k for k in ("exec", "httpGet", "tcpSocket", "grpc") if k in probe]
            if handlers != (["httpGet"] if name == "readinessProbe" else ["tcpSocket"]): return False
            if probe[handlers[0]].get("host") or probe[handlers[0]].get("httpHeaders"): return False
    return True


def material(obj):
    meta = obj["metadata"]
    value = {"apiVersion": obj["apiVersion"], "kind": obj["kind"], "metadata": {"name": meta["name"], "namespace": meta["namespace"], "labels": meta.get("labels", {}),
        "annotations": {k: v for k, v in meta.get("annotations", {}).items() if k not in {OPERATION, CONTRACT} and not (obj["kind"] == "Deployment" and k == "deployment.kubernetes.io/revision")}}}
    if obj["kind"] == "ConfigMap": value.update(data=obj.get("data", {}), immutable=obj.get("immutable"))
    else:
        spec = dict(obj["spec"])
        if obj["kind"] == "Service": spec = {k: v for k, v in spec.items() if k not in {"clusterIP", "clusterIPs"}}
        value["spec"] = spec
    return value


def admitted(expected, actual):
    if (not isinstance(actual, dict) or actual.get("apiVersion") != expected["apiVersion"] or actual.get("kind") != expected["kind"]
            or not includes(expected["metadata"], actual.get("metadata", {})) or actual["metadata"].get("ownerReferences") or actual["metadata"].get("finalizers") or actual["metadata"].get("deletionTimestamp")):
        raise ValueError("Admission changed original policy or ownership")
    if actual["kind"] == "ConfigMap":
        if actual.get("data") != expected["data"] or actual.get("immutable") is not True or actual.get("binaryData"): raise ValueError("Keep the exact immutable server configuration")
    elif not includes(expected["spec"], actual.get("spec", {})): raise ValueError("Admission changed the reviewed workload policy")
    if actual["kind"] == "Service":
        spec = actual["spec"]
        if (spec.get("type", "ClusterIP") != "ClusterIP" or spec.get("externalIPs") or spec.get("externalName") or spec.get("clusterIP") == "None"
                or spec.get("sessionAffinity", "None") != "None" or spec.get("internalTrafficPolicy", "Cluster") != "Cluster" or any(p.get("nodePort") for p in spec["ports"])):
            raise ValueError("Keep internal per-member API and Raft services")
    if actual["kind"] == "Deployment" and (actual["spec"].get("paused") or not safe_pod(actual["spec"]["template"]["spec"])):
        raise ValueError("Admission added an executable, privilege or placement path")
    if actual["kind"] == "Deployment" and actual["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") not in {None, "1"}:
        raise ValueError("An original fresh Deployment has another rollout revision")
    return fingerprint(material(actual))


def owned(expected, current, entry, state):
    if (not current or not uid(current.get("metadata", {}).get("uid")) or current["metadata"].get("annotations", {}).get(OPERATION) != state["operation_id"]
            or current["metadata"].get("annotations", {}).get(CONTRACT) != state["contract_sha256"] or entry["uid"] is not None and current["metadata"]["uid"] != entry["uid"]
            or admitted(expected, current) != entry["admitted_sha256"]): raise ValueError("An original resource was replaced or changed")
    return current


def fresh_scope(client, selected, *, member=None):
    wanted = renderer.LABELS if member is None else {**renderer.LABELS, "platform.gitops/member": member}
    claims = {m[k] for m in selected["members"] if member is None or m["name"] == member for k in ("data_claim", "audit_claim")}
    for pod in items(client, "pods", selected["namespace"]):
        if any(v.get("persistentVolumeClaim", {}).get("claimName") in claims for v in pod.get("spec", {}).get("volumes", [])):
            raise ValueError("A reviewed claim is already attached to another workload scope")
    for resource in ("deployments", "replicasets", "pods", "services") if member is None else ("deployments", "replicasets", "pods"):
        for obj in items(client, resource, selected["namespace"]):
            spec = obj.get("spec", {})
            if any(includes(wanted, labels) for labels in (obj.get("metadata", {}).get("labels", {}), spec.get("selector", {}), spec.get("selector", {}).get("matchLabels", {}), spec.get("template", {}).get("metadata", {}).get("labels", {}))):
                raise ValueError("Existing OpenBao resources require recovery rather than fresh installation")


def observe_started(client, selected, current):
    namespace = selected["namespace"]; selector = ",".join(k + "=" + v for k, v in renderer.LABELS.items())
    replicasets = items(client, "replicasets", namespace, selector); pods = items(client, "pods", namespace, selector)
    if len(replicasets) != 3 or len(pods) != 3: return None
    result = {}
    for member in selected["members"]:
        deployment = current["deployment/" + member["name"]]; meta, status = deployment["metadata"], deployment.get("status", {})
        if type(meta.get("generation")) is not int or status.get("observedGeneration") != meta["generation"] or any(status.get(k) != 1 for k in ("replicas", "updatedReplicas")): return None
        found = [r for r in replicasets if controlled(r, "Deployment", meta["uid"])]
        if len(found) != 1: return None
        rs = found[0]; rm, spec = rs["metadata"], rs.get("spec", {})
        if (not uid(rm.get("uid")) or rm.get("deletionTimestamp") or spec.get("replicas") != 1 or type(rm.get("generation")) is not int
                or rs.get("status", {}).get("observedGeneration") != rm["generation"] or rs.get("status", {}).get("replicas") != 1
                or not includes(deployment["spec"]["template"]["spec"], spec.get("template", {}).get("spec", {}))): return None
        found = [p for p in pods if controlled(p, "ReplicaSet", rm["uid"])]
        if len(found) != 1: return None
        pod = found[0]; pm, ps, state = pod["metadata"], pod.get("spec", {}), pod.get("status", {})
        containers = state.get("containerStatuses", [])
        if (not uid(pm.get("uid")) or pm.get("deletionTimestamp") or state.get("phase") != "Running" or not condition(pod, "PodScheduled") or ps.get("nodeName") != member["node"]
                or not includes(deployment["spec"]["template"]["spec"], ps) or not safe_pod(ps, allow_node=True)
                or len(containers) != 1 or containers[0].get("name") != "openbao" or "running" not in containers[0].get("state", {})
                or containers[0].get("started") is not True or type(containers[0].get("ready")) is not bool or not containers[0].get("containerID") or not containers[0].get("imageID") or ps.get("initContainers") or state.get("initContainerStatuses")): return None
        try: address = ipaddress.ip_address(state.get("podIP", ""))
        except ValueError: return None
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast: return None
        result[member["name"]] = {"deployment_uid": meta["uid"], "replicaset_uid": rm["uid"], "pod_uid": pm["uid"], "node": member["node"],
            "pod_ip": str(address), "container_id": containers[0]["containerID"], "image_id": containers[0]["imageID"]}
    if len({v["pod_ip"] for v in result.values()}) != 3: return None
    return result


def operate(bundle, plan, *, mode="local", output=None, wait_seconds=180, client=None):
    contract, objects = load_package(bundle, plan["contract_sha256"]); validate(plan, contract); tools = toolset()
    result = {"local_inputs_verified": True, "server_checked": False, "resources_observed": False, "member_processes_observed": False,
        "requires_external_ceremony": True, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 1800:
        raise ValueError("Select a bounded explicit server action")
    if mode != "check-server" and plan["admitted_sha256"] is None: raise ValueError("Review and pin dry-run admission before creating resources")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use an original private operation directory")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume needs its original journal")
    receipt = directory / "openbao-install-proof.json"; selected = contract["plan"]; namespace = selected["namespace"]
    with mechanics.lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("The journal cannot redirect elsewhere")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply" or state.get("plan_sha256") != fingerprint(plan)
                    or state.get("contract_sha256") != plan["contract_sha256"] or state.get("toolset_sha256") != tools or not isinstance(state.get("operation_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"]) or not isinstance(state.get("objects"), dict) or set(state["objects"]) != {key(o) for o in objects}
                    or any(state.get(k) is not False for k in UNVERIFIED)): raise ValueError("Resume only the original matching installation")
            for entry in state["objects"].values():
                renderer.exact(entry, "attempted uid admitted_sha256")
                if type(entry["attempted"]) is not bool or entry["uid"] is not None and (not uid(entry["uid"]) or not entry["attempted"]) or entry["admitted_sha256"] is not None and not sha(entry["admitted_sha256"]):
                    raise ValueError("Invalid original resource handle")
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
                if not any(e["attempted"] for e in state["objects"].values()):
                    fresh_scope(network, selected)
                    for obj in objects:
                        if network.get(key(obj), namespace, optional=True): raise ValueError("A target resource already exists")
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=canonical(obj).encode(), seconds=60))
                        state["objects"][key(obj)]["admitted_sha256"] = admitted(obj, response)
                        atomic_write_text(directory / (key(obj).replace("/", "-") + "-admitted.json"), canonical(material(response)))
                    aggregate = fingerprint({k: v["admitted_sha256"] for k, v in state["objects"].items()}); record("admission-reviewed", server_checked=True, admitted_sha256=aggregate)
                    if plan["admitted_sha256"] is not None and aggregate != plan["admitted_sha256"]: raise ValueError("Server admission differs from the reviewed policy")
                    if mode == "check-server": return {**result, "server_checked": True, "admitted_sha256": aggregate}
                if fingerprint({k: v["admitted_sha256"] for k, v in state["objects"].items()}) != plan["admitted_sha256"]: raise ValueError("Original admission commitments changed")
                for obj in objects:
                    entry = state["objects"][key(obj)]; current = network.get(key(obj), namespace, optional=True)
                    if not entry["attempted"]:
                        if current: raise ValueError("A new foreign resource appeared")
                        prerequisites(network, plan, selected)
                        if obj["kind"] == "Deployment": fresh_scope(network, selected, member=obj["metadata"]["name"])
                        if toolset() != tools: raise ValueError("Installer sources changed")
                        response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=canonical(obj).encode(), seconds=60))
                        if admitted(obj, response) != entry["admitted_sha256"]: raise ValueError("Admission changed before creation")
                        entry["attempted"] = True; record("before-" + key(obj), read_only=False); wrote = True
                        current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=canonical(obj).encode(), seconds=60))
                        if uid(current.get("metadata", {}).get("uid")): entry["uid"] = current["metadata"]["uid"]; record("create-response-observed")
                    deadline = time.monotonic() + wait_seconds
                    while not current:
                        if entry["uid"] is not None: raise ValueError("An original resource is missing; never recreate it")
                        if time.monotonic() >= deadline: raise TimeoutError("Unknown create outcome; observe the same handle later")
                        time.sleep(1); current = network.get(key(obj), namespace, optional=True)
                    owned(obj, current, entry, state); entry["uid"] = current["metadata"]["uid"]; record("observed-" + key(obj))
                deadline = time.monotonic() + wait_seconds
                while True:
                    current = {key(o): owned(o, network.get(key(o), namespace, optional=True), state["objects"][key(o)], state) for o in objects}
                    snapshot = observe_started(network, selected, current)
                    if snapshot:
                        if state.get("observed_members") is not None and state["observed_members"] != snapshot: raise ValueError("An original member process or Pod was replaced; use explicit recovery")
                        prerequisites(network, plan, selected)
                        final = {key(o): owned(o, network.get(key(o), namespace, optional=True), state["objects"][key(o)], state) for o in objects}
                        if observe_started(network, selected, final) != snapshot or toolset() != tools: raise ValueError("Final original process or source observation changed")
                        result.update(server_checked=True, resources_observed=True, member_processes_observed=True, read_only=not wrote)
                        record("awaiting-external-ceremony", observed_members=snapshot, **result); return result
                    if time.monotonic() >= deadline: raise TimeoutError("Original member processes are not running; preserve the journal")
                    record("waiting-for-original-workloads", server_checked=True, resources_observed=True); time.sleep(2)
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="openbao-installation-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--deployment-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=180)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("check-server", "apply", "resume"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.deployment_plan, max_bytes=128 * 1024))
        mode = next((v.replace("_", "-") for v in ("check_server", "apply", "resume") if getattr(args, v)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("OpenBao installation stopped; preserve the private journal and original handles. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
