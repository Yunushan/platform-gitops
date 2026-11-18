#!/usr/bin/env python3
"""Package optional Cilium host-network Gateway routes, without changing the CNI."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_text
from kubernetes_management import inventory, validate_target
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OWNER = "platform.gitops/cilium-gateway-package"
ACCESS = "platform.gitops/gateway-access"
CONTROLLER = "io.cilium/gateway-controller"
GROUP = "gateway.networking.k8s.io"
TLSROUTE_CRD = "tlsroutes." + GROUP
ROUTE_KINDS = {"http": "HTTPRoute", "h2c": "GRPCRoute", "tls": "TLSRoute"}
CRDS = {name + ".gateway.networking.k8s.io": version for name, version in (
    ("gatewayclasses", "v1"), ("gateways", "v1"), ("httproutes", "v1"),
    ("grpcroutes", "v1"), ("referencegrants", "v1beta1"))}


def compatibility(cilium_version, gateway_version, kubernetes_version=None):
    if not isinstance(cilium_version, str) or not re.fullmatch(r"1[.](?:18|19)[.](?:0|[1-9][0-9]{0,2})", cilium_version):
        raise ValueError("Select an exact reviewed Cilium 1.18 or 1.19 patch release")
    lane = tuple(int(part) for part in cilium_version.split("."))
    gateway, minors = {18: ("1.2.0", {31, 32, 33}), 19: ("1.4.1", {32, 33, 34, 35})}[lane[1]]
    if gateway_version != gateway:
        raise ValueError("Cilium and Gateway API releases are outside the reviewed compatibility pair")
    if kubernetes_version is not None and (not isinstance(kubernetes_version, str)
            or not re.fullmatch(r"1[.][0-9]{2}[.][0-9]{1,3}", kubernetes_version) or int(kubernetes_version.split(".")[1]) not in minors):
        raise ValueError("Kubernetes release is outside the reviewed Cilium lane")
    return lane


def required_crds(gateway_version, *, tls_passthrough=False):
    required = dict(CRDS)
    if gateway_version == "1.4.1":
        required["backendtlspolicies." + GROUP] = "v1"
    elif gateway_version != "1.2.0":
        raise ValueError("Unsupported Gateway API release")
    if tls_passthrough:
        required[TLSROUTE_CRD] = "v1alpha2"
    return required


def listener_name(route):
    return "tls-passthrough" if route["protocol"] == "tls" else "https"


def listener_ports(plan):
    return {name: plan[key] for name, key in (("https", "https_port"), ("tls-passthrough", "tls_passthrough_port"))
            if key in plan}


def name(value):
    return isinstance(value, str) and len(value) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value) is not None


def selector(value):
    if not isinstance(value, dict) or not value or len(value) > 8:
        return False
    for key, item in value.items():
        if not isinstance(key, str) or len(key.split("/")) > 2:
            return False
        prefix, _, suffix = key.rpartition("/")
        if ("/" in key and not prefix) or prefix and not dns_name(prefix):
            return False
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", suffix):
            return False
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", item):
            return False
    return True


def validate(plan):
    fields = {"schema_version", "name", "namespace", "class_name", "management_target", "cilium", "serving_selector",
              "failure_domain_key", "routes"}
    if (not isinstance(plan, dict) or set(plan) - {"https_port", "tls_secret", "tls_passthrough_port"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1):
        raise ValueError("Invalid gateway plan fields")
    if any(not name(plan[key]) for key in ("name", "namespace", "class_name")):
        raise ValueError("Invalid gateway names")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("Use a dedicated gateway namespace")
    validate_target(plan["management_target"])
    cilium = plan["cilium"]
    if (not isinstance(cilium, dict) or set(cilium) != {"version", "gateway_api_version", "namespace", "images"}
            or not name(cilium["namespace"])
            or not isinstance(cilium["images"], dict) or set(cilium["images"]) != {"agent", "envoy", "operator"}
            or any(not isinstance(image, str) or not IMAGE.fullmatch(image) for image in cilium["images"].values())):
        raise ValueError("Declare the reviewed Cilium/Gateway API compatibility pair and every image digest")
    compatibility(cilium["version"], cilium["gateway_api_version"])
    if not selector(plan["serving_selector"]) or plan["failure_domain_key"] not in {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}:
        raise ValueError("Select serving nodes and failure domains")
    routes = plan["routes"]
    if not isinstance(routes, list) or not 1 <= len(routes) <= 32:
        raise ValueError("Select between one and 32 explicit routes")
    identities, hosts, services = set(), set(), {}
    for route in routes:
        if (not isinstance(route, dict) or set(route) != {"name", "namespace", "hostname", "protocol", "service", "port", "target_port", "pod_selector"}
                or any(not name(route[key]) for key in ("name", "namespace", "service"))
                or len(route["name"]) > 55
                or route["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease", plan["namespace"]}
                or not dns_name(route["hostname"]) or "." not in route["hostname"]
                or not isinstance(route["protocol"], str) or route["protocol"] not in ROUTE_KINDS or not selector(route["pod_selector"])
                or any(type(route[key]) is not int or not 1 <= route[key] <= 65535 for key in ("port", "target_port"))):
            raise ValueError("Invalid explicit backend route")
        identity, host = (route["namespace"], route["name"]), route["hostname"]
        service = (route["namespace"], route["service"], route["port"])
        backend = (route["target_port"], route["protocol"], route["pod_selector"])
        if identity in identities or host in hosts or service in services and services[service] != backend:
            raise ValueError("Duplicate routes or conflicting service declarations")
        identities.add(identity)
        hosts.add(host)
        services[service] = backend
    listeners = {listener_name(route) for route in routes}
    ports = listener_ports(plan)
    if (set(ports) != listeners or any(type(port) is not int or not 1024 <= port <= 65535 for port in ports.values())
            or len(set(ports.values())) != len(ports)):
        raise ValueError("Declare a distinct unprivileged port for each active listener")
    if ("tls_secret" in plan) != ("https" in listeners) or "tls_secret" in plan and not name(plan["tls_secret"]):
        raise ValueError("Declare a TLS Secret for the terminating listener only")
    return plan


def objects(plan):
    meta = {"name": plan["name"], "namespace": plan["namespace"]}
    listeners = []
    for listener, port in listener_ports(plan).items():
        passthrough = listener == "tls-passthrough"
        tls = {"mode": "Passthrough"} if passthrough else {
            "mode": "Terminate", "certificateRefs": [{"group": "", "kind": "Secret", "name": plan["tls_secret"]}]}
        kinds = ("TLSRoute",) if passthrough else ("HTTPRoute", "GRPCRoute")
        listeners.append({"name": listener, "protocol": "TLS" if passthrough else "HTTPS", "port": port, "tls": tls,
            "allowedRoutes": {"namespaces": {"from": "Selector", "selector": {"matchLabels": {ACCESS: plan["name"]}}},
                              "kinds": [{"group": GROUP, "kind": kind} for kind in kinds]}})
    result = [
        {"apiVersion": GROUP + "/v1", "kind": "GatewayClass", "metadata": {"name": plan["class_name"]},
         "spec": {"controllerName": CONTROLLER}},
        {"apiVersion": GROUP + "/v1", "kind": "Gateway", "metadata": meta,
         "spec": {"gatewayClassName": plan["class_name"], "listeners": listeners}}]
    for route in plan["routes"]:
        kind = ROUTE_KINDS[route["protocol"]]
        namespace = route["namespace"]
        rule = {"backendRefs": [{"group": "", "kind": "Service", "name": route["service"], "port": route["port"], "weight": 1}]}
        if kind == "HTTPRoute":
            rule["matches"] = [{"path": {"type": "PathPrefix", "value": "/"}}]
        result.extend([
            {"apiVersion": GROUP + ("/v1alpha2" if kind == "TLSRoute" else "/v1"), "kind": kind,
             "metadata": {"name": route["name"], "namespace": namespace},
             "spec": {"parentRefs": [{"group": GROUP, "kind": "Gateway", "namespace": plan["namespace"], "name": plan["name"],
                                      "sectionName": listener_name(route)}],
                      "hostnames": [route["hostname"]], "rules": [rule]}},
            {"apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy", "metadata": {"name": route["name"] + "-gateway", "namespace": namespace},
             "spec": {"endpointSelector": {"matchLabels": {"k8s:" + key: value for key, value in route["pod_selector"].items()}},
                      "ingress": [{"fromEntities": ["ingress"], "toPorts": [{"ports": [{"port": str(route["target_port"]), "protocol": "TCP"}]}]}]}}])
    for obj in result:
        obj["metadata"]["labels"] = {OWNER: plan["name"]}
    return result


def helm_overlay(plan):
    # This is a merge fragment for a separately reviewed CNI installation. The
    # deploy command never applies it or changes kube-proxy replacement.
    return {"kubeProxyReplacement": True, "l7Proxy": True, "image": {"override": plan["cilium"]["images"]["agent"]},
        "envoy": {"enabled": True, "image": {"override": plan["cilium"]["images"]["envoy"]}},
        "gatewayAPI": {"enabled": True, "enableAlpn": True, "enableAppProtocol": True, "enableProxyProtocol": False,
            "gatewayClass": {"create": "false"}, "hostNetwork": {"enabled": True, "nodes": {"matchLabels": plan["serving_selector"]}}},
        "operator": {"replicas": 2, "image": {"override": plan["cilium"]["images"]["operator"]},
            "podDisruptionBudget": {"enabled": True, "maxUnavailable": 1},
            "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": plan["failure_domain_key"],
                "labelSelector": {"matchLabels": {"io.cilium/app": "operator"}}}]}}}}


def rke2_helmchartconfig(plan):
    validate(plan)
    if plan["cilium"]["namespace"] != "kube-system":
        raise ValueError("Bundled RKE2 Cilium uses the kube-system namespace")
    if compatibility(plan["cilium"]["version"], plan["cilium"]["gateway_api_version"]) < (1, 19, 6):
        raise ValueError("Bundled RKE2 host-network Gateway status requires Cilium 1.19.6 or newer in the reviewed lane")
    values = helm_overlay(plan)
    values.update(k8sServiceHost="127.0.0.1", k8sServicePort=6443)
    values["cni"] = {"chainingMode": "none"}
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChartConfig",
            "metadata": {"name": "rke2-cilium", "namespace": "kube-system"},
            "spec": {"valuesContent": yaml.safe_dump(values, sort_keys=False)}}


def render(plan, runtime):
    validate(plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown application-host runtime")
    resources = objects(plan)
    files = {"gateway-resources.yaml": yaml.safe_dump_all(resources, sort_keys=False),
             "cilium-values-fragment.yaml": yaml.safe_dump(helm_overlay(plan), sort_keys=False)}
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "inventory": inventory(resources),
                "files_sha256": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in files.items()}}
    files["deployment-contract.json"] = json.dumps(contract, sort_keys=True, indent=2) + "\n"
    files["render-status.json"] = json.dumps({"component": "cilium-gateway", "runtime": runtime,
        "contract_sha256": hashlib.sha256(files["deployment-contract.json"].encode()).hexdigest(),
        "rendered": True, "deployed": False, "cni_installed": False, "ha_verified": False, "full_stack_verified": False,
        "remaining": ["cilium-cni-installation-and-upgrade", "redundant-external-entry-and-network-paths", "tls-and-backend-live-probes",
                      "network-policy-positive-and-negative-tests", "node-loss-upgrade-restore-and-load-tests"]}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rke2-bundled", action="store_true",
                        help="Also render bootstrap HelmChartConfig for a fresh bundled RKE2 Cilium cluster")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        files = render(plan, args.runtime)
        if args.rke2_bundled:
            if args.runtime != "rke2":
                raise ValueError("Use --rke2-bundled with --runtime rke2")
            files["rke2-cilium-config.yaml"] = yaml.safe_dump(rke2_helmchartconfig(plan), sort_keys=False)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, "deployed": False, "ha_verified": False}))
        return 0
    except Exception:
        print("Cilium gateway render stopped; inspect the private plan and new output directory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
