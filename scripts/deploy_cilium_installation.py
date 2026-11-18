#!/usr/bin/env python3
"""Explicitly install, patch-upgrade, resume or patch-rollback an owned Cilium package."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sys
import uuid

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, inventory, resource_name, verify_target
import deploy_cilium_gateway as gateway
import render_cilium_installation as renderer
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
FILES = {"cilium-values.yaml", "cilium-components.yaml", "cilium-preflight.yaml", "gateway-crds.yaml"}
PLURALS = {"ServiceAccount": "serviceaccounts", "ClusterRole": "clusterroles", "ClusterRoleBinding": "clusterrolebindings", "DaemonSet": "daemonsets", "Deployment": "deployments"}


def key(item):
    return item["apiVersion"] + "/" + item["kind"] + "/" + str(item.get("namespace") or "") + "/" + item["name"]


def load_package(root, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Declare the reviewed CNI contract digest")
    payload = read_bounded_bytes(root / "installation-contract.json", max_bytes=256 * 1024)
    if renderer.sha(payload) != digest:
        raise ValueError("CNI installation contract changed")
    contract = loads_strict_json(payload)
    if (not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "inventory", "preflight_inventory", "files_sha256"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in renderer.RUNTIMES
            or set(contract["files_sha256"]) != FILES):
        raise ValueError("Invalid CNI installation contract")
    plan = renderer.validate(contract["plan"])
    files, resources = {}, {}
    for name, digest in contract["files_sha256"].items():
        data = read_bounded_bytes(root / name, max_bytes=renderer.MAX_ARTIFACT)
        if renderer.sha(data) != digest:
            raise ValueError("CNI installation content changed")
        files[name] = data
        resources[name] = [obj for obj in loads_strict_yaml_all(data.decode()) if obj is not None]
    if resources["cilium-values.yaml"] != [renderer.values(plan)]:
        raise ValueError("CNI values differ from the selected topology")
    for name in FILES - {"cilium-values.yaml"}:
        if any(obj.get("metadata", {}).get("labels", {}).get(renderer.OWNER) != plan["name"] for obj in resources[name]):
            raise ValueError("CNI resource ownership label differs")
    renderer.parse_components(plan, files["cilium-components.yaml"])
    renderer.parse_components(plan, files["cilium-preflight.yaml"], preflight=True)
    definitions = resources["gateway-crds.yaml"]
    renderer.validate_gateway_definitions(plan, definitions)
    if (inventory(definitions + resources["cilium-components.yaml"]) != contract["inventory"]
            or inventory(resources["cilium-preflight.yaml"]) != contract["preflight_inventory"] or len(contract["inventory"]) > 128):
        raise ValueError("CNI inventory differs from reviewed content")
    return contract, files


def includes(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(v is None or k in actual and includes(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(includes(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def transition(target, previous, *, rollback):
    if previous is None:
        if rollback: raise ValueError("Rollback needs the currently installed package")
        return
    old, new = previous["plan"], target["plan"]
    mutable = {"cilium_version", "chart_sha256", "helm_sha256", "images"}
    if (target["runtime"] != previous["runtime"] or {k: v for k, v in old.items() if k not in mutable} != {k: v for k, v in new.items() if k not in mutable}
            or target["inventory"] != previous["inventory"]):
        raise ValueError("Patch lifecycle cannot change network topology, target or resource inventory")
    old_version, new_version = renderer.version(old["cilium_version"]), renderer.version(new["cilium_version"])
    if old_version[:2] != new_version[:2]:
        raise ValueError("Minor Cilium transitions need a separately implemented migration")
    if old_version == new_version or (new_version < old_version) != rollback:
        raise ValueError("Version direction differs from the explicit upgrade/rollback operation")


def topology_plan(plan):
    return {"cilium": {"namespace": plan["namespace"], "images": plan["images"]},
            "serving_selector": plan["serving_selector"], "failure_domain_key": plan["failure_domain_key"]}


def owned(client, item, plan, *, expected_uid=None):
    obj = client.get(resource_name(item), item.get("namespace"), optional=True)
    if obj and (obj.get("metadata", {}).get("labels", {}).get(renderer.OWNER) != plan["name"] or obj.get("metadata", {}).get("deletionTimestamp")):
        raise ValueError("CNI resources cannot be adopted or changed while deleting")
    if expected_uid and (not obj or obj.get("metadata", {}).get("uid") != expected_uid):
        raise ValueError("An installed CNI resource was replaced or is missing")
    return obj


def ledger(client, plan):
    obj = client.get("configmap/" + plan["name"] + "-installation", plan["namespace"], optional=True)
    if obj is None: return None, None
    if obj.get("metadata", {}).get("labels", {}).get(renderer.OWNER) != plan["name"] or obj.get("metadata", {}).get("deletionTimestamp"):
        raise ValueError("CNI lifecycle record is not owned by this package")
    state = loads_strict_json(obj.get("data", {}).get("state", ""))
    if (not isinstance(state, dict) or set(state) != {"phase", "current", "desired", "attempt_uid", "resource_uids"}
            or state["phase"] not in {"stable", "pending"} or not isinstance(state["resource_uids"], dict)
            or any(not isinstance(value, str) or not value for value in state["resource_uids"].values())):
        raise ValueError("CNI lifecycle record is malformed")
    return obj, state


def state_preflight(client, contract, digest, previous, previous_digest, *, resume):
    plan = contract["plan"]
    obj, state = ledger(client, plan)
    if state:
        if state["phase"] == "pending":
            if not resume or state["desired"] != digest or state["current"] != previous_digest:
                raise ValueError("Pending CNI operation needs the identical package and explicit resume")
        elif state["current"] != (previous_digest or digest) or resume:
            raise ValueError("Installed CNI revision differs from the reviewed transition")
    elif previous is not None or resume:
        raise ValueError("No owned installation exists for this upgrade or resume")
    all_items = {key(i): i for i in contract["inventory"] + contract["preflight_inventory"]}
    for identity, item in all_items.items():
        current = owned(client, item, plan, expected_uid=(state or {}).get("resource_uids", {}).get(identity))
        if not state and current:
            raise ValueError("Fresh installation requires absent CNI and Gateway resources")
    return obj, state


def cluster_preflight(client, plan, *, fresh, require_ready):
    verify_target(client, plan["management_target"])
    server = loads_strict_json(client.call(["version", "-o", "json"])).get("serverVersion", {}).get("gitVersion", "")
    if server.removeprefix("v").split("+", 1)[0] != plan["kubernetes_version"]:
        raise ValueError("Kubernetes release differs from the rendering target")
    nodes = gateway.items(client, "nodes")
    pool, controls, workers, serving_domains = ipaddress.ip_network(plan["pod_cidr"]), 0, 0, set()
    if len(nodes) > 2 ** (plan["node_cidr_mask"] - pool.prefixlen):
        raise ValueError("Pod pool lacks a per-node allocation for the current cluster")
    for node in nodes:
        meta, info = node.get("metadata", {}), node.get("status", {}).get("nodeInfo", {})
        labels = meta.get("labels", {})
        if (meta.get("deletionTimestamp") or info.get("operatingSystem") != "linux" or info.get("architecture") not in {"amd64", "arm64"}
                or require_ready and not gateway.condition(node.get("status", {}).get("conditions", []), "Ready")):
            raise ValueError("CNI nodes are unsupported, deleting or unavailable for this operation")
        kernel = re.match(r"([0-9]+)[.]([0-9]+)", info.get("kernelVersion", ""))
        if not kernel or tuple(map(int, kernel.groups())) < (5, 10):
            raise ValueError("Cilium requires the reviewed kernel baseline")
        control = any(k in labels for k in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))
        controls += int(control)
        workers += int(not control)
        if gateway.matches(labels, plan["serving_selector"]):
            domain = labels.get(plan["failure_domain_key"])
            if not domain: raise ValueError("Gateway serving node has no declared failure domain")
            serving_domains.add(domain)
        for address in node.get("status", {}).get("addresses", []):
            if address.get("type") == "InternalIP" and ipaddress.ip_address(address["address"]) in pool:
                raise ValueError("Pod CIDR overlaps a node address")
    if controls < 3 or workers < 2 or len(serving_domains) < 2:
        raise ValueError("The foundation needs three control-plane nodes and two workers with redundant gateway placement")
    service = client.get("service/kubernetes", "default") or {}
    service_ips = service.get("spec", {}).get("clusterIPs", [])
    if plan["bootstrap_api"]["host"] in service_ips or any(ipaddress.ip_address(value) in pool for value in service_ips):
        raise ValueError("Pod CIDR overlaps the API Service address")
    pods = gateway.items(client, "pods", plan["namespace"])
    if any(p.get("metadata", {}).get("name", "").startswith(("kube-proxy-", "rke2-kube-proxy-")) for p in pods):
        raise ValueError("Bootstrap must disable kube-proxy before this kube-proxy-free installation")
    if fresh:
        if any(ds.get("metadata", {}).get("name", "").startswith(("cilium", "calico", "canal", "kube-flannel", "weave", "antrea", "kube-router"))
               for ds in gateway.items(client, "daemonsets", plan["namespace"])):
            raise ValueError("Fresh installation cannot replace another CNI")
        if client.get("customresourcedefinition/ciliumnodes.cilium.io", optional=True):
            raise ValueError("Existing Cilium state requires an explicit recovery or migration")
    # Never capture Helm release Secret contents.
    if client.call(["get", "secrets", "--namespace", plan["namespace"], "--selector", "owner=helm,name=cilium", "-o", "name"]).strip():
        raise ValueError("Helm-managed Cilium must use its own lifecycle")
    if client.call(["api-resources", "--api-group=helm.cattle.io", "-o", "name"]).strip():
        if client.get("helmchart.helm.cattle.io/rke2-cilium", plan["namespace"], optional=True):
            raise ValueError("RKE2-managed Cilium must not be adopted by the standalone installer")


def write_ledger(client, plan, prior, state):
    metadata = {"name": plan["name"] + "-installation", "namespace": plan["namespace"], "labels": {renderer.OWNER: plan["name"]}}
    if prior:
        metadata.update(uid=prior["metadata"]["uid"], resourceVersion=prior["metadata"]["resourceVersion"])
    obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": {"state": renderer.canonical(state).decode()}}
    try:
        client.call(["replace" if prior else "create", "-f", "-", "-o", "json"], payload=renderer.canonical(obj))
    except Exception:
        # A timed-out acknowledgment is resolved by reading this exact attempt.
        pass
    actual, observed = ledger(client, plan)
    if not actual or observed != state:
        raise ValueError("CNI lifecycle reservation/update was not verified")
    return actual


def apply_file(client, payload, *, dry_run=False):
    args = ["apply", "--server-side", "--field-manager=platform-cilium-installation", "-f", "-"]
    if dry_run: args += ["--dry-run=server"]
    client.call(args, payload=payload, seconds=90)


def bootstrap_namespaces(client, plan, components, reserved, reservation, prior_state, record):
    allowed_attempts = {reservation["attempt_uid"]}
    if prior_state and prior_state["phase"] == "pending": allowed_attempts.add(prior_state["attempt_uid"])
    for desired in (obj for obj in components if obj["kind"] == "Namespace"):
        item = inventory([desired])[0]; identity = key(item)
        current = owned(client, item, plan, expected_uid=reservation["resource_uids"].get(identity))
        if current and identity in reservation["resource_uids"]: continue
        annotation = "platform.gitops/cilium-bootstrap-attempt"
        if not current:
            obj = copy.deepcopy(desired)
            obj["metadata"]["annotations"] = {**(obj["metadata"].get("annotations") or {}), annotation: reservation["attempt_uid"]}
            record(phase="before-namespace-bootstrap", namespace=desired["metadata"]["name"], write_outcome="unverified")
            try: client.call(["create", "-f", "-", "-o", "json"], payload=renderer.canonical(obj), seconds=60)
            except Exception: pass  # Resolve only this exact attempt's possible acknowledgment loss.
            current = owned(client, item, plan)
        if (not current or not current.get("metadata", {}).get("uid")
                or (current["metadata"].get("annotations") or {}).get(annotation) not in allowed_attempts):
            raise ValueError("Namespace bootstrap was not created by this exact owned operation")
        reservation = {**reservation, "resource_uids": {**reservation["resource_uids"], identity: current["metadata"]["uid"]}}
        reserved = write_ledger(client, plan, reserved, reservation)
        client.call(["wait", "--for=jsonpath={.status.phase}=Active", "namespace/" + desired["metadata"]["name"], "--timeout=30s"], seconds=30)
        record(phase="namespace-bootstrapped", namespace_uid=current["metadata"]["uid"], write_outcome="acknowledged")
    return reserved, reservation


def preflight_resources(contract, files):
    shared = {key(item) for item in contract["inventory"]}
    objects = [obj for obj in loads_strict_yaml_all(files["cilium-preflight.yaml"].decode()) if key(inventory([obj])[0]) not in shared]
    return objects


def admitted_workload_spec(spec):
    """Normalize only observed typed API representations, preserving drift checks."""
    result = copy.deepcopy(spec)
    if not isinstance(result, dict): return result
    pod = result.get("template", {}).get("spec", {})
    for container in pod.get("containers", []) + pod.get("initContainers", []):
        # Kubernetes omits these zero-valued fields on read-back. Fill both
        # expected and actual defaults so true/nonzero changes remain visible.
        for mount in container.get("volumeMounts") or []: mount.setdefault("readOnly", False)
        for field in ("readinessProbe", "livenessProbe", "startupProbe"):
            if container.get(field) is not None: container[field].setdefault("initialDelaySeconds", 0)
        # resource.Quantity accepts integer JSON values but returns strings.
        for field in ("requests", "limits"):
            values = (container.get("resources") or {}).get(field) or {}
            for name, value in values.items():
                if type(value) is int: values[name] = str(value)
    return result


def verify_fields(client, contract, files, expected_uids=None):
    uids = {}
    for name in ("gateway-crds.yaml", "cilium-components.yaml"):
        for desired in loads_strict_yaml_all(files[name].decode()):
            item = inventory([desired])[0]
            obj = owned(client, item, contract["plan"], expected_uid=(expected_uids or {}).get(key(item)))
            if not obj or not obj.get("metadata", {}).get("uid"):
                raise ValueError("Applied CNI resource has no matching identity")
            for field in ("spec", "data", "rules", "roleRef", "subjects"):
                if field in desired:
                    expected, actual = desired[field], obj.get(field)
                    if field == "spec" and desired["kind"] in {"DaemonSet", "Deployment"}:
                        expected, actual = admitted_workload_spec(expected), admitted_workload_spec(actual)
                    if not includes(expected, actual):
                        raise ValueError("Installed CNI resource fields drifted")
            uids[key(item)] = obj["metadata"]["uid"]
    return uids


def preflight_ready(client, contract, objects):
    plan = contract["plan"]
    for desired in (o for o in objects if o["kind"] in {"DaemonSet", "Deployment"}):
        obj = owned(client, inventory([desired])[0], plan) or {}
        generation = obj.get("metadata", {}).get("generation")
        status = obj.get("status", {})
        if not includes(desired["spec"], obj.get("spec")) or type(generation) is not int or generation < 1 or status.get("observedGeneration") != generation:
            raise ValueError("Image preflight is stale or differs from the reviewed workload")
        if desired["kind"] == "DaemonSet":
            gateway.daemonset_current(obj, plan["images"]["agent"], set(plan["images"].values()))
        else:
            replicas = obj.get("spec", {}).get("replicas", 1)
            if any(status.get(k) != replicas for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")):
                raise ValueError("Cilium CRD image preflight is not available")


def agent_coverage(client, plan):
    nodes = gateway.items(client, "nodes")
    pods = gateway.items(client, "pods", plan["namespace"])
    ds = client.get("daemonset/cilium", plan["namespace"]) or {}
    gateway.daemonset_current(ds, plan["images"]["agent"], set(plan["images"].values()))
    if ds.get("status", {}).get("desiredNumberScheduled") != len(nodes):
        raise ValueError("Cilium does not cover every declared Linux cluster node")
    for node in nodes:
        name = node.get("metadata", {}).get("name")
        found = [pod for pod in pods if pod.get("spec", {}).get("nodeName") == name and gateway.controlled(pod, "DaemonSet", ds["metadata"]["uid"])
                 and gateway.ready_pod(pod) and gateway.pinned_pod(pod, set(plan["images"].values()))
                 and any(c.get("image") == plan["images"]["agent"] for c in pod["spec"]["containers"])]
        if len(found) != 1:
            raise ValueError("A cluster node has no current ready pinned Cilium agent")


def remove_preflight(client, contract, objects):
    for item in inventory(objects):
        obj = owned(client, item, contract["plan"])
        if obj is None: continue
        meta = obj["metadata"]
        group = "/apis/" + item["apiVersion"] if "/" in item["apiVersion"] else "/api/" + item["apiVersion"]
        namespace = "/namespaces/" + item["namespace"] if item.get("namespace") else ""
        path = group + namespace + "/" + PLURALS[item["kind"]] + "/" + item["name"]
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}, "propagationPolicy": "Foreground"}
        client.call(["delete", "--raw", path, "-f", "-"], payload=renderer.canonical(options))
        args = ["wait", "--for=delete", resource_name(item), "--timeout=120s"]
        if item.get("namespace"): args += ["--namespace", item["namespace"]]
        client.call(args, seconds=120)


def journal(output, initial):
    path = output.resolve()
    if not path.relative_to((ROOT / "private").resolve()).parts or output.exists() or output.is_symlink():
        raise ValueError("Use a new private proof directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = dict(initial)
    def record(**values):
        state.update(values, recorded_at=datetime.now(timezone.utc).isoformat())
        atomic_write_text(path / "cilium-proof.json", json.dumps(state, sort_keys=True, indent=2) + "\n")
    record(phase="local-validated")
    return record, state


def server_operation(client, contract, files, digest, previous, previous_files, previous_digest, *, apply, resume, verify, record):
    plan = contract["plan"]
    current, state = state_preflight(client, contract, digest, previous, previous_digest, resume=resume)
    pending = state and state["phase"] == "pending"
    cluster_preflight(client, plan, fresh=state is None, require_ready=bool(state) and not pending)
    original_nodes = {node["metadata"]["name"]: node["metadata"]["uid"] for node in gateway.items(client, "nodes")}
    if previous and not pending:
        for obj in loads_strict_yaml_all(previous_files["cilium-components.yaml"].decode()):
            live = owned(client, inventory([obj])[0], plan)
            if obj["kind"] == "ConfigMap" and (live or {}).get("data") != obj.get("data"):
                raise ValueError("Installed Cilium configuration drifted from the reviewed previous package")
        gateway.topology(client, topology_plan(previous["plan"]))
    record(phase="prerequisites-verified")
    already_current = state and state["phase"] == "stable" and state["current"] == digest and previous is None
    if verify or apply and already_current:
        if not state or state["phase"] != "stable" or state["current"] != digest:
            raise ValueError("Target package is not recorded as installed")
        verify_fields(client, contract, files, state["resource_uids"])
        agent_coverage(client, plan)
        evidence = gateway.topology(client, topology_plan(plan))
        record(phase="verified", bootstrap_ready=True, **evidence)
        return {"read_only": True, "bootstrap_ready": True, "idempotent": bool(already_current), "ha_verified": False, "full_stack_verified": False}
    components = loads_strict_yaml_all(files["cilium-components.yaml"].decode())
    missing_namespaces = {obj["metadata"]["name"] for obj in components if obj["kind"] == "Namespace"
        and not client.get("namespace/" + obj["metadata"]["name"], optional=True)}
    apply_file(client, files["gateway-crds.yaml"], dry_run=True)
    preview = [obj for obj in components if obj["metadata"].get("namespace") not in missing_namespaces]
    apply_file(client, yaml.safe_dump_all(preview, sort_keys=False).encode(), dry_run=True)
    if not apply:
        record(phase="server-checked")
        return {"read_only": True, "server_checked": not missing_namespaces, "namespace_bootstrap_checked": bool(missing_namespaces),
            "namespace_bootstrap_required": sorted(missing_namespaces), "bootstrap_ready": False, "full_stack_verified": False}
    # Re-read the reservation after dry-run, so a concurrent different target
    # cannot be mistaken for this reviewed transition.
    current, state = state_preflight(client, contract, digest, previous, previous_digest, resume=resume)
    reservation = {"phase": "pending", "current": previous_digest, "desired": digest, "attempt_uid": str(uuid.uuid4()), "resource_uids": (state or {}).get("resource_uids", {})}
    record(phase="before-reservation", write_attempted=True, write_outcome="unverified")
    reserved = write_ledger(client, plan, current, reservation)
    record(phase="reserved", write_outcome="acknowledged", attempt_uid=reservation["attempt_uid"])
    reserved, reservation = bootstrap_namespaces(client, plan, components, reserved, reservation, state, record)
    apply_file(client, files["cilium-components.yaml"], dry_run=True)
    if previous:
        preflight = preflight_resources(contract, files)
        payload = yaml.safe_dump_all(preflight, sort_keys=False).encode()
        record(phase="before-image-preflight", write_outcome="unverified")
        apply_file(client, payload)
        for kind in ("daemonset", "deployment"):
            client.call(["rollout", "status", kind + "/cilium-pre-flight-check", "--namespace", plan["namespace"], "--timeout=180s"], seconds=180)
        preflight_ready(client, contract, preflight)
        record(phase="image-preflight-ready")
        remove_preflight(client, contract, preflight)
        record(phase="image-preflight-removed")
    record(phase="before-gateway-crds", write_outcome="unverified")
    apply_file(client, files["gateway-crds.yaml"])
    for name in renderer.gateway.required_crds(plan.get("gateway_api_version", "1.2.0"),
                                              tls_passthrough=plan.get("tls_passthrough", False)):
        client.call(["wait", "--for=condition=Established", "customresourcedefinition/" + name, "--timeout=120s"], seconds=120)
    record(phase="before-cni-components", write_outcome="unverified")
    for desired in (obj for obj in components if obj["kind"] == "Namespace"):
        item = inventory([desired])[0]
        owned(client, item, plan, expected_uid=reservation["resource_uids"].get(key(item)))
    apply_file(client, files["cilium-components.yaml"])
    record(phase="cni-components-applied", write_outcome="acknowledged")
    for kind, name in (("daemonset", "cilium"), ("daemonset", "cilium-envoy"), ("deployment", "cilium-operator")):
        client.call(["rollout", "status", kind + "/" + name, "--namespace", plan["namespace"], "--timeout=300s"], seconds=300)
    # Host-network controller readiness can precede kubelet's CNI readiness
    # update. Wait on the original inventory; replacement Nodes cannot satisfy
    # this operation merely by reusing names.
    record(phase="waiting-for-node-readiness")
    client.call(["wait", "--for=condition=Ready", *["node/" + name for name in sorted(original_nodes)], "--timeout=180s"], seconds=185)
    if {node["metadata"]["name"]: node["metadata"]["uid"] for node in gateway.items(client, "nodes")} != original_nodes:
        raise ValueError("Cluster node identities changed during CNI installation")
    cluster_preflight(client, plan, fresh=False, require_ready=True)
    evidence = gateway.topology(client, topology_plan(plan))
    agent_coverage(client, plan)
    uids = verify_fields(client, contract, files, reservation["resource_uids"])
    stable = {**reservation, "phase": "stable", "current": digest, "desired": digest, "resource_uids": uids}
    write_ledger(client, plan, reserved, stable)
    record(phase="bootstrap-ready", bootstrap_ready=True, **evidence)
    return {"read_only": False, "bootstrap_ready": True, "upgrade_performed": previous is not None, "ha_verified": False, "full_stack_verified": False}


def operate(root, digest, *, previous_root=None, previous_digest=None, apply=False, check_server=False, verify=False, rollback=False, resume=False, output=None):
    if (sum(bool(v) for v in (apply, check_server, verify)) > 1 or bool(previous_root) != bool(previous_digest)
            or verify and any((previous_root, resume, rollback))):
        raise ValueError("Invalid CNI operation modes or previous package inputs")
    contract, files = load_package(root, digest)
    previous, previous_files = load_package(previous_root, previous_digest) if previous_root else (None, None)
    transition(contract, previous, rollback=rollback)
    if not any((apply, check_server, verify)):
        return {"read_only": True, "local_inputs_verified": True, "bootstrap_ready": False, "full_stack_verified": False}
    if output is None:
        raise ValueError("Server modes need a new private proof directory")
    record, state = journal(output, {"schema_version": 1, "contract_sha256": digest, "previous_contract_sha256": previous_digest,
        "runtime": contract["runtime"], "bootstrap_ready": False, "ha_verified": False, "full_stack_verified": False})
    try:
        client = Client(contract["plan"]["management_target"])
        with client.frozen():
            return server_operation(client, contract, files, digest, previous, previous_files, previous_digest, apply=apply, resume=resume, verify=verify, record=record)
    except Exception:
        record(phase="failed", failed_at=state["phase"], error_code="cilium-validation-or-operation-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--previous-bundle-dir", type=Path)
    parser.add_argument("--previous-contract-sha256")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--resume", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, previous_root=args.previous_bundle_dir, previous_digest=args.previous_contract_sha256,
            apply=args.apply, check_server=args.check_server, verify=args.verify, rollback=args.rollback, resume=args.resume, output=args.output_dir)))
        return 0
    except Exception:
        print("Cilium lifecycle stopped; retain the private proof, operation record and previous package. No automatic rollback was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
