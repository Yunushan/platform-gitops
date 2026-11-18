#!/usr/bin/env python3
"""Validate an optional gateway package, or explicitly apply its owned routes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, resource_name, verify_owned, verify_target
import render_cilium_gateway as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]


def load_package(root, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("A reviewed contract digest is required")
    content = read_bounded_bytes(root / "deployment-contract.json", max_bytes=256 * 1024)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Gateway contract changed")
    contract = loads_strict_json(content)
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "inventory", "files_sha256"}:
        raise ValueError("Invalid gateway contract")
    # Rebuild this finite resource set from the validated plan. A manifest hash
    # must not turn an arbitrary Namespace, Secret or CNI patch into a route.
    regenerated = renderer.render(contract["plan"], contract["runtime"])
    if regenerated["deployment-contract.json"].encode() != content:
        raise ValueError("Gateway contract differs from generated policy")
    files = {}
    for name in contract["files_sha256"]:
        payload = read_bounded_bytes(root / name, max_bytes=1024 * 1024)
        if payload != regenerated[name].encode():
            raise ValueError("Gateway content changed")
        files[name] = payload
    return contract, files


def items(client, resource, namespace=None, selector=None, *, all_namespaces=False):
    args = ["get", resource, "-o", "json"]
    if all_namespaces:
        args.append("--all-namespaces")
    elif namespace:
        args += ["--namespace", namespace]
    if selector:
        args += ["--selector", ",".join(key + "=" + value for key, value in sorted(selector.items()))]
    result = loads_strict_json(client.call(args))
    if not isinstance(result, dict) or not isinstance(result.get("items"), list) or len(result["items"]) > 4096:
        raise ValueError("Invalid or excessive Kubernetes collection")
    return result["items"]


def matches(labels, selector):
    return all(labels.get(key) == value for key, value in selector.items())


def condition(conditions, kind, generation=None):
    found = [item for item in conditions if item.get("type") == kind]
    return len(found) == 1 and found[0].get("status") == "True" and (generation is None or found[0].get("observedGeneration") == generation)


def controlled(obj, kind, uid):
    owners = [ref for ref in obj.get("metadata", {}).get("ownerReferences", []) if ref.get("controller") is True]
    return len(owners) == 1 and owners[0].get("kind") == kind and owners[0].get("uid") == uid


def ready_pod(pod):
    status, meta = pod.get("status", {}), pod.get("metadata", {})
    containers = pod.get("spec", {}).get("containers", [])
    states = status.get("containerStatuses", [])
    return (not meta.get("deletionTimestamp") and status.get("phase") == "Running" and condition(status.get("conditions", []), "Ready")
            and len(states) == len(containers) > 0 and {s.get("name") for s in states} == {c.get("name") for c in containers}
            and all(s.get("ready") is True and "running" in s.get("state", {}) for s in states))


def pinned_pod(pod, images):
    spec = pod.get("spec", {})
    return all(container.get("image") in images for container in spec.get("containers", []) + spec.get("initContainers", []))


def daemonset_current(obj, image, allowed_images):
    status, meta = obj.get("status", {}), obj.get("metadata", {})
    pod = obj.get("spec", {}).get("template", {}).get("spec", {})
    desired = status.get("desiredNumberScheduled")
    containers = pod.get("containers", [])
    images = [c.get("image") for c in containers + pod.get("initContainers", [])]
    if (meta.get("deletionTimestamp") or not meta.get("uid") or type(meta.get("generation")) is not int
            or status.get("observedGeneration") != meta["generation"] or type(desired) is not int or desired < 2
            or any(status.get(key) != desired for key in ("currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable"))
            or status.get("numberMisscheduled", 0) != 0 or pod.get("hostNetwork") is not True
            or not any(c.get("image") == image for c in containers) or any(value not in allowed_images for value in images)):
        raise ValueError("Cilium/Envoy DaemonSet is unpinned, stale or unavailable")


def topology(client, plan):
    cilium, selector = plan["cilium"], plan["serving_selector"]
    namespace = cilium["namespace"]
    config = (client.get("configmap/cilium-config", namespace) or {}).get("data", {})
    flags = ("kube-proxy-replacement", "enable-l7-proxy", "enable-gateway-api", "enable-envoy-config", "external-envoy-proxy",
             "gateway-api-hostnetwork-enabled", "enable-gateway-api-alpn", "enable-gateway-api-app-protocol")
    selector_string = ",".join(key + "=" + value for key, value in sorted(selector.items()))
    # These names/serialization are checked against the pinned Cilium chart.
    configured_selector = config.get("gateway-api-hostnetwork-nodelabelselector", "")
    if (any(config.get(key) != "true" for key in flags) or config.get("enable-gateway-api-proxy-protocol") != "false"
            or sorted(configured_selector.split(",")) != sorted(selector_string.split(","))):
        raise ValueError("Cilium Gateway API prerequisite settings differ; no CNI migration is performed")
    nodes = items(client, "nodes", selector=selector)
    selected = {}
    domains = set()
    for node in nodes:
        meta = node.get("metadata", {})
        labels = meta.get("labels", {})
        domain = labels.get(plan["failure_domain_key"])
        if (not meta.get("uid") or meta.get("deletionTimestamp") or node.get("spec", {}).get("unschedulable")
                or not matches(labels, selector) or not condition(node.get("status", {}).get("conditions", []), "Ready") or not domain):
            raise ValueError("Every selected serving node must be ready and have its failure-domain label")
        selected[meta["name"]] = meta["uid"]
        domains.add(domain)
    if len(selected) < 2 or len(domains) < 2:
        raise ValueError("At least two ready serving nodes in two failure domains are required")
    pods = items(client, "pods", namespace)
    images = set(cilium["images"].values())
    for role, name in (("agent", "cilium"), ("envoy", "cilium-envoy")):
        ds = client.get("daemonset/" + name, namespace) or {}
        daemonset_current(ds, cilium["images"][role], images)
        for node in selected:
            found = [pod for pod in pods if pod.get("spec", {}).get("nodeName") == node and controlled(pod, "DaemonSet", ds["metadata"]["uid"])
                     and ready_pod(pod) and pinned_pod(pod, images) and any(c.get("image") == cilium["images"][role] for c in pod["spec"]["containers"])]
            if len(found) != 1:
                raise ValueError("Each serving node needs its ready pinned Cilium and Envoy pods")
    operator = client.get("deployment/cilium-operator", namespace) or {}
    status, meta = operator.get("status", {}), operator.get("metadata", {})
    replicas = operator.get("spec", {}).get("replicas")
    revision = meta.get("annotations", {}).get("deployment.kubernetes.io/revision")
    if (not meta.get("uid") or meta.get("deletionTimestamp") or not revision or type(replicas) is not int or replicas < 2
            or status.get("observedGeneration") != meta.get("generation") or type(meta.get("generation")) is not int
            or any(status.get(key) != replicas for key in ("updatedReplicas", "readyReplicas", "availableReplicas", "replicas"))
            or not pinned_pod(operator.get("spec", {}).get("template", {}), images)
            or not any(c.get("image") == cilium["images"]["operator"] for c in operator.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []))):
        raise ValueError("Cilium operators are not fully rolled out")
    replica_sets = {rs["metadata"]["uid"] for rs in items(client, "replicasets", namespace)
                    if controlled(rs, "Deployment", meta["uid"]) and rs.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision") == revision}
    operator_nodes = {pod.get("spec", {}).get("nodeName") for pod in pods if ready_pod(pod) and pinned_pod(pod, images)
                      and any(controlled(pod, "ReplicaSet", uid) for uid in replica_sets)
                      and any(c.get("image") == cilium["images"]["operator"] for c in pod["spec"]["containers"])}
    operator_domains = set()
    for node in operator_nodes:
        obj = client.get("node/" + node) or {}
        domain = obj.get("metadata", {}).get("labels", {}).get(plan["failure_domain_key"])
        if not domain or not condition(obj.get("status", {}).get("conditions", []), "Ready"):
            raise ValueError("Operator node identity is unavailable")
        operator_domains.add(domain)
    if len(operator_nodes) < 2 or len(operator_domains) < 2:
        raise ValueError("Two current operator pods must span nodes and failure domains")
    return {"serving_node_uids": sorted(selected.values()), "serving_domains": len(domains), "operator_domains": len(operator_domains)}


def backend_endpoints(client, route, service, port):
    target = port.get("targetPort", port["port"])
    if not (type(target) is int and target == route["target_port"] or isinstance(target, str) and target):
        raise ValueError("Backend Service target port differs")
    slices = items(client, "endpointslices.discovery.k8s.io", route["namespace"], {"kubernetes.io/service-name": route["service"]})
    found = False
    verified_nodes = set()
    for item in slices:
        if not controlled(item, "Service", service["metadata"]["uid"]) or item.get("metadata", {}).get("deletionTimestamp"): continue
        endpoints = [e for e in item.get("endpoints", []) if e.get("conditions", {}).get("ready") is True and not e.get("conditions", {}).get("terminating")]
        if not endpoints: continue
        resolved = [p for p in item.get("ports", []) if (p.get("name") or "") == (port.get("name") or "") and p.get("protocol", "TCP") == "TCP"]
        if len(resolved) != 1 or resolved[0].get("port") != route["target_port"]:
            raise ValueError("Ready backend endpoint ports differ from the declared Service port")
        if isinstance(target, str):
            for endpoint in endpoints:
                ref = endpoint.get("targetRef", {})
                if (ref.get("kind") != "Pod" or not ref.get("name") or not ref.get("uid")
                        or ref.get("namespace", route["namespace"]) != route["namespace"]):
                    raise ValueError("Named target ports require original backend Pod references")
                pod = client.get("pod/" + ref["name"], route["namespace"], optional=True) or {}
                if (pod.get("metadata", {}).get("uid") != ref["uid"] or pod.get("metadata", {}).get("deletionTimestamp")
                        or not ready_pod(pod) or not matches(pod.get("metadata", {}).get("labels", {}), route["pod_selector"])):
                    raise ValueError("Named target port backend Pod identity or readiness differs")
                ports = [p for c in pod.get("spec", {}).get("containers", []) for p in c.get("ports", [])
                         if p.get("name") == target and p.get("protocol", "TCP") == "TCP"]
                if len(ports) != 1 or ports[0].get("containerPort") != route["target_port"]:
                    raise ValueError("Named target port does not resolve to the reviewed backend port")
                if node := pod.get("spec", {}).get("nodeName"):
                    verified_nodes.add(node)
        found = True
    if not found: raise ValueError("Backend Service has no ready endpoint on the declared port")
    return verified_nodes


def preflight(client, contract):
    plan = contract["plan"]
    verify_target(client, plan["management_target"])
    required = renderer.required_crds(plan["cilium"]["gateway_api_version"], tls_passthrough="tls_passthrough_port" in plan)
    for crd, version in {**required, "ciliumnetworkpolicies.cilium.io": "v2"}.items():
        obj = client.get("customresourcedefinition/" + crd) or {}
        if (not condition(obj.get("status", {}).get("conditions", []), "Established")
                or not any(v.get("name") == version and v.get("served") is True for v in obj.get("spec", {}).get("versions", []))
                or crd in required and obj.get("metadata", {}).get("annotations", {}).get("gateway.networking.k8s.io/bundle-version") != "v" + plan["cilium"]["gateway_api_version"]):
            raise ValueError("A required pinned, established and served CRD is missing")
    evidence = topology(client, plan)
    if not client.get("namespace/" + plan["namespace"]):
        raise ValueError("Provision the gateway namespace")
    # Never capture private keys or serialize the Secret in this process.
    if "tls_secret" in plan:
        secret_type = client.call(["get", "secret/" + plan["tls_secret"], "--namespace", plan["namespace"], "-o", "jsonpath={.type}"]).strip()
        if secret_type != "kubernetes.io/tls":
            raise ValueError("Provision the gateway TLS Secret")
    protocols = {"http": {None, "http"}, "h2c": {"kubernetes.io/h2c"}, "tls": {None, "https", "tls"}}
    for route in plan["routes"]:
        namespace = route["namespace"]
        ns = client.get("namespace/" + namespace) or {}
        if ns.get("metadata", {}).get("labels", {}).get(renderer.ACCESS) != plan["name"]:
            raise ValueError("Authorize route namespaces with the explicit gateway-access label")
        service = client.get("service/" + route["service"], namespace) or {}
        spec = service.get("spec", {})
        ports = [p for p in spec.get("ports", []) if p.get("port") == route["port"] and p.get("protocol", "TCP") == "TCP"]
        if (not service.get("metadata", {}).get("uid") or service.get("metadata", {}).get("deletionTimestamp")
                or spec.get("type", "ClusterIP") == "ExternalName" or spec.get("selector") != route["pod_selector"] or len(ports) != 1
                or ports[0].get("appProtocol") not in protocols[route["protocol"]]):
            raise ValueError("Backend Service selector, target port or application protocol differs")
        backend_endpoints(client, route, service, ports[0])
    # A second host-network Gateway on the same port cannot share this wildcard
    # listener. Check all namespaces; don't delete or adopt the conflicting one.
    ports = set(renderer.listener_ports(plan).values())
    for gateway in items(client, "gateways.gateway.networking.k8s.io", all_namespaces=True):
        meta = gateway.get("metadata", {})
        ours = meta.get("namespace") == plan["namespace"] and meta.get("name") == plan["name"]
        if not ours and any(listener.get("port") in ports for listener in gateway.get("spec", {}).get("listeners", [])):
            raise ValueError("Another Gateway already declares the host listener port")
    for item in contract["inventory"]:
        verify_owned(client, item, renderer.OWNER, plan["name"])
    return evidence


def ready(client, plan):
    for desired in renderer.objects(plan):
        current = client.get(resource_name({"apiVersion": desired["apiVersion"], "kind": desired["kind"], "name": desired["metadata"]["name"]}), desired["metadata"].get("namespace")) or {}
        if (current.get("spec") != desired["spec"] or current.get("metadata", {}).get("labels", {}).get(renderer.OWNER) != plan["name"]
                or current.get("metadata", {}).get("deletionTimestamp")):
            return False
    cls = client.get("gatewayclass.gateway.networking.k8s.io/" + plan["class_name"]) or {}
    gateway = client.get("gateway.gateway.networking.k8s.io/" + plan["name"], plan["namespace"]) or {}
    for obj, required in ((cls, ("Accepted",)), (gateway, ("Accepted", "Programmed"))):
        generation = obj.get("metadata", {}).get("generation")
        if (obj.get("metadata", {}).get("deletionTimestamp") or type(generation) is not int or generation < 1
                or not all(condition(obj.get("status", {}).get("conditions", []), key, generation) for key in required)):
            return False
    listeners = gateway.get("status", {}).get("listeners", [])
    expected = {name: 0 for name in renderer.listener_ports(plan)}
    for route in plan["routes"]:
        expected[renderer.listener_name(route)] += 1
    if len(listeners) != len(expected) or {item.get("name"): item.get("attachedRoutes") for item in listeners} != expected:
        return False
    for listener in listeners:
        if not all(condition(listener.get("conditions", []), key, gateway["metadata"]["generation"])
                   for key in ("Accepted", "ResolvedRefs", "Programmed")):
            return False
    for route in plan["routes"]:
        resource = renderer.ROUTE_KINDS[route["protocol"]].lower() + "." + renderer.GROUP + "/" + route["name"]
        obj = client.get(resource, route["namespace"]) or {}
        generation = obj.get("metadata", {}).get("generation")
        parents = [p for p in obj.get("status", {}).get("parents", []) if p.get("controllerName") == renderer.CONTROLLER
                   and p.get("parentRef", {}).get("name") == plan["name"]
                   and p.get("parentRef", {}).get("namespace", route["namespace"]) == plan["namespace"]
                   and p.get("parentRef", {}).get("sectionName") == renderer.listener_name(route)]
        if (obj.get("metadata", {}).get("deletionTimestamp") or type(generation) is not int or generation < 1 or len(parents) != 1
                or not all(condition(parents[0].get("conditions", []), key, generation) for key in ("Accepted", "ResolvedRefs"))):
            return False
    return True


def journal(output, initial):
    resolved = output.resolve()
    relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.exists() or output.is_symlink():
        raise ValueError("Use a new private proof directory")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = dict(initial)
    def record(**values):
        state.update(values, recorded_at=datetime.now(timezone.utc).isoformat())
        atomic_write_text(resolved / "gateway-proof.json", json.dumps(state, sort_keys=True, indent=2) + "\n")
    record(phase="local-validated")
    return record, state


def server_operation(client, contract, files, result, record, *, verify, apply):
    plan = contract["plan"]
    evidence = preflight(client, contract)
    record(phase="prerequisites-verified", **evidence)
    args = ["apply", "--server-side", "--field-manager=platform-cilium-gateway", "-f", "-"]
    if not verify:
        client.call([*args, "--dry-run=server"], payload=files["gateway-resources.yaml"], seconds=60)
    result["server_checked"] = True
    if apply:
        # Recheck ownership/topology after dry-run, then submit only the
        # verified in-memory resource bytes; no force-conflicts or pruning.
        preflight(client, contract)
        record(phase="before-apply", apply_attempted=True, apply_outcome="unverified")
        client.call(args, payload=files["gateway-resources.yaml"], seconds=60)
        record(phase="resources-applied", deployed=True, apply_outcome="acknowledged")
        deadline = time.monotonic() + 180
        while not ready(client, plan):
            if time.monotonic() >= deadline:
                raise ValueError("Gateway programming was not verified within the bounded wait")
            time.sleep(3)
        result.update(deployed=True, gateway_programmed=True)
    elif verify:
        if not ready(client, plan):
            raise ValueError("Current gateway/route conditions are not programmed")
        result["gateway_programmed"] = True
    if apply or verify:
        evidence = preflight(client, contract)
    record(phase="completed", **result, **evidence)
    return result


def operate(bundle, digest, *, check_server=False, apply=False, verify=False, output=None):
    if sum(bool(value) for value in (check_server, apply, verify)) > 1:
        raise ValueError("Select a single server mode")
    contract, files = load_package(bundle, digest)
    result = {"local_inputs_verified": True, "read_only": not apply, "server_checked": False, "deployed": False,
              "gateway_programmed": False, "cni_installed": False, "ha_verified": False, "full_stack_verified": False}
    if not any((check_server, apply, verify)):
        return result
    if output is None:
        raise ValueError("Server modes require a new private proof directory")
    record, state = journal(output, {"schema_version": 1, "contract_sha256": digest, "runtime": contract["runtime"], **result})
    try:
        client = Client(contract["plan"]["management_target"])
        with client.frozen():
            return server_operation(client, contract, files, result, record, verify=verify, apply=apply)
    except Exception:
        record(phase="failed", failed_at=state["phase"], error_code="gateway-validation-or-operation-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--output-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, check_server=args.check_server, apply=args.apply, verify=args.verify, output=args.output_dir)))
        return 0
    except Exception:
        print("Cilium gateway operation stopped; retain the private proof and inspect prerequisites or controller diagnostics.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
