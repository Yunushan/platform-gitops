#!/usr/bin/env python3
"""Render a pinned Cilium CNI installation and its upgrade image preflight."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

import yaml

from atomic_file import atomic_write_bytes, atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
from kubernetes_management import inventory, validate_target
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
import render_cilium_gateway as gateway
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
OWNER = "platform.gitops/cilium-installation"
RELEASE = "cilium"
KINDS = {"Namespace", "ServiceAccount", "ConfigMap", "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding", "Service", "DaemonSet", "Deployment", "PodDisruptionBudget"}
NAMESPACE_KINDS = KINDS - {"Namespace", "ClusterRole", "ClusterRoleBinding"}
MAX_ARTIFACT = 16 * 1024 ** 2


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"1[.](?:18|19)[.](?:0|[1-9][0-9]{0,2})", value):
        raise ValueError("Select an exact reviewed Cilium 1.18 or 1.19 patch release")
    return tuple(int(part) for part in value.split("."))


def validate(plan):
    fields = {"schema_version", "name", "namespace", "management_target", "cilium_version", "kubernetes_version", "chart_sha256", "helm_sha256",
              "gateway_crds_sha256", "images", "bootstrap_api", "cni_paths", "pod_cidr", "node_cidr_mask", "mtu", "serving_selector", "failure_domain_key"}
    if (not isinstance(plan, dict) or set(plan) - {"gateway_api_version", "tls_passthrough"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or type(plan.get("tls_passthrough", False)) is not bool):
        raise ValueError("Invalid Cilium installation plan")
    if not gateway.name(plan["name"]) or len(plan["name"]) > 48 or plan["namespace"] != "kube-system":
        raise ValueError("Select a dedicated package name and the infrastructure namespace")
    validate_target(plan["management_target"])
    gateway.compatibility(plan["cilium_version"], plan.get("gateway_api_version", "1.2.0"), plan["kubernetes_version"])
    for key in ("chart_sha256", "helm_sha256", "gateway_crds_sha256"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]):
            raise ValueError("Pin every local tool and release artifact")
    if (not isinstance(plan["images"], dict) or set(plan["images"]) != {"agent", "envoy", "operator"}
            or any(not isinstance(image, str) or not IMAGE.fullmatch(image) for image in plan["images"].values())
            or not gateway.selector(plan["serving_selector"]) or plan["failure_domain_key"] not in {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}):
        raise ValueError("Pin Cilium images and serving/failure-domain placement")
    api = plan["bootstrap_api"]
    if not isinstance(api, dict) or set(api) != {"host", "port"} or not isinstance(api["host"], str):
        raise ValueError("Declare the independently reachable bootstrap API")
    try:
        host = ipaddress.ip_address(api["host"])
        valid_host = not (host.is_loopback or host.is_unspecified or host.is_link_local or host.is_multicast)
    except ValueError:
        valid_host = dns_name(api["host"]) and "." in api["host"] and not api["host"].endswith((".svc", ".cluster.local"))
    if not valid_host or type(api["port"]) is not int or not 1 <= api["port"] <= 65535:
        raise ValueError("Bootstrap API must work without cluster DNS or the Service datapath")
    paths = plan["cni_paths"]
    if not isinstance(paths, dict) or set(paths) != {"bin", "config"} or paths["bin"] == paths["config"]:
        raise ValueError("Declare distinct CNI binary and configuration host paths")
    for path in paths.values():
        if (not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", path) or len(PurePosixPath(path).parts) < 3
                or ".." in PurePosixPath(path).parts or str(PurePosixPath(path)) != path):
            raise ValueError("CNI host directories must be explicit normalized absolute paths")
    network = ipaddress.ip_network(plan["pod_cidr"], strict=True)
    if (network.version != 4 or network.is_multicast or network.is_loopback or network.is_link_local or not 8 <= network.prefixlen <= 28
            or type(plan["node_cidr_mask"]) is not int or not network.prefixlen < plan["node_cidr_mask"] <= 28
            or type(plan["mtu"]) is not int or not 1280 <= plan["mtu"] <= 9000):
        raise ValueError("Declare a nonoverlapping IPv4 pod pool, per-node mask and underlay MTU")
    return plan


def values(plan):
    result = gateway.helm_overlay({"cilium": {"images": plan["images"]}, "serving_selector": plan["serving_selector"], "failure_domain_key": plan["failure_domain_key"]})
    result.update(agent=True, k8sServiceHost=plan["bootstrap_api"]["host"], k8sServicePort=plan["bootstrap_api"]["port"],
        cni={"binPath": plan["cni_paths"]["bin"], "confPath": plan["cni_paths"]["config"]},
        ipv4={"enabled": True}, ipv6={"enabled": False}, routingMode="tunnel", tunnelProtocol="vxlan", MTU=plan["mtu"],
        ipam={"mode": "cluster-pool", "operator": {"clusterPoolIPv4PodCIDRList": [plan["pod_cidr"]], "clusterPoolIPv4MaskSize": plan["node_cidr_mask"]}},
        rollOutCiliumPods=True, nodeinit={"enabled": False}, hubble={"enabled": False},
        ingressController={"enabled": False}, upgradeCompatibility=".".join(plan["cilium_version"].split(".")[:2]),
        resources={"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}})
    result["operator"].update(enabled=True, rollOutPods=True, resources={"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}})
    result["envoy"].update(resources={"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}})
    result["gatewayAPI"]["secretsNamespace"] = {"create": True, "name": plan["name"] + "-secrets", "sync": True}
    result["tls"] = {"secretsNamespace": {"create": True, "name": plan["name"] + "-secrets", "sync": False}}
    return result


def checked_chart(plan, payload):
    if len(payload) > MAX_ARTIFACT or sha(payload) != plan["chart_sha256"]:
        raise ValueError("Cilium chart differs from the reviewed archive")
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
        expanded = compressed.read(MAX_ARTIFACT + 1)
    if len(expanded) > MAX_ARTIFACT:
        raise ValueError("Expanded chart exceeds the artifact limit")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        total = 0
        paths = set()
        documents = []
        for index, member in enumerate(archive):
            if index >= 2048:
                raise ValueError("Excessive chart archive")
            path = PurePosixPath(member.name)
            total += member.size
            if (member.name in paths or str(path) != member.name.rstrip("/") or member.isfile() and member.name.endswith("/")
                    or not path.parts or path.parts[0] != "cilium" or path.is_absolute() or ".." in path.parts
                    or "\\" in member.name or not (member.isfile() or member.isdir()) or member.size > MAX_ARTIFACT or total > MAX_ARTIFACT):
                raise ValueError("Unsafe or excessive chart archive")
            paths.add(member.name)
            if member.name == "cilium/Chart.yaml":
                if not member.isfile() or member.size > 256 * 1024:
                    raise ValueError("Invalid chart metadata")
                documents = loads_strict_yaml_all(archive.extractfile(member).read().decode())
        chart = documents[0] if len(documents) == 1 and isinstance(documents[0], dict) else {}
    if chart.get("name") != "cilium" or str(chart.get("version")) != plan["cilium_version"] or str(chart.get("appVersion", "")).removeprefix("v") != plan["cilium_version"]:
        raise ValueError("Chart release metadata differs from the plan")


def gateway_crds(plan, payload):
    if len(payload) > MAX_ARTIFACT or sha(payload) != plan["gateway_crds_sha256"]:
        raise ValueError("Gateway API release artifact changed")
    objects = [obj for obj in loads_strict_yaml_all(payload.decode()) if obj is not None]
    validate_gateway_definitions(plan, objects)
    return objects


def validate_gateway_definitions(plan, objects):
    selected_version = plan.get("gateway_api_version", "1.2.0")
    required = gateway.required_crds(selected_version, tls_passthrough=plan.get("tls_passthrough", False))
    names = set()
    for obj in objects:
        meta = obj.get("metadata", {}) if isinstance(obj, dict) else {}
        name = meta.get("name")
        if (obj.get("kind") != "CustomResourceDefinition" or obj.get("apiVersion") != "apiextensions.k8s.io/v1" or name not in required or name in names
                or meta.get("annotations", {}).get("gateway.networking.k8s.io/bundle-version") != "v" + selected_version
                or not any(v.get("name") == required[name] and v.get("served") is True for v in obj.get("spec", {}).get("versions", []))):
            raise ValueError("Unexpected or incompatible Gateway API CRD")
        names.add(name)
    if names != set(required):
        raise ValueError("Incomplete Gateway API release")


def mark(objects, plan):
    for obj in objects:
        obj["metadata"]["labels"] = {**(obj["metadata"].get("labels") or {}), OWNER: plan["name"]}
    return objects


def parse_components(plan, payload, *, preflight=False):
    if len(payload) > MAX_ARTIFACT:
        raise ValueError("Rendered Cilium content exceeds the limit")
    objects = [obj for obj in loads_strict_yaml_all(payload.decode()) if obj is not None]
    identities, workloads, images = set(), set(), set(plan["images"].values())
    for obj in objects:
        kind, meta = obj.get("kind"), obj.get("metadata", {})
        if (kind not in KINDS or (meta.get("annotations") or {}).get("helm.sh/hook") or not dns_name(meta.get("name"))
                or kind in NAMESPACE_KINDS and meta.get("namespace") not in {plan["namespace"], plan["name"] + "-secrets"}
                or kind == "Namespace" and meta.get("name") != plan["name"] + "-secrets"):
            raise ValueError("Unexpected, hooked or cross-namespace Cilium resource")
        identity = (kind, meta.get("namespace"), meta["name"])
        if identity in identities:
            raise ValueError("Duplicate Cilium resource")
        identities.add(identity)
        if kind in {"DaemonSet", "Deployment"}:
            workloads.add((kind, meta["name"]))
            pod = obj.get("spec", {}).get("template", {}).get("spec", {})
            containers = pod.get("containers", []) + pod.get("initContainers", [])
            if not containers or any(c.get("image") not in images for c in containers) or pod.get("hostNetwork") is not True:
                raise ValueError("Every CNI workload image must be pinned and use the reviewed host network")
    expected = {("DaemonSet", "cilium-pre-flight-check")} if preflight else {("DaemonSet", "cilium"), ("DaemonSet", "cilium-envoy"), ("Deployment", "cilium-operator")}
    if not expected <= workloads or workloads - expected - ({("Deployment", "cilium-pre-flight-check")} if preflight else set()):
        raise ValueError("Cilium controller/preflight inventory changed")
    return mark(objects, plan)


def render(plan, chart, crds, runtime, helm, *, runner=run_bounded):
    validate(plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown application-host runtime")
    checked_chart(plan, chart)
    definitions = gateway_crds(plan, crds)
    if sha(read_bounded_bytes(helm, max_bytes=128 * 1024 ** 2)) != plan["helm_sha256"]:
        raise ValueError("Helm binary differs from its reviewed digest")
    selected = values(plan)
    files = {"cilium-values.yaml": yaml.safe_dump(selected, sort_keys=False)}
    with tempfile.TemporaryDirectory(prefix="cilium-render-") as directory:
        root = Path(directory)
        archive, value_file = root / "cilium.tgz", root / "values.yaml"
        atomic_write_bytes(archive, chart, replace=False)
        for preflight, name in ((False, "cilium-components.yaml"), (True, "cilium-preflight.yaml")):
            configured = copy.deepcopy(selected)
            if preflight:
                configured.update(preflight={"enabled": True, "image": {"override": plan["images"]["agent"]}, "envoy": {"image": {"override": plan["images"]["envoy"]}}}, agent=False)
                configured["operator"]["enabled"] = False
            atomic_write_text(value_file, yaml.safe_dump(configured))
            result = runner([str(Path(helm).resolve()), "template", RELEASE, str(archive), "--namespace", plan["namespace"], "--kube-version", plan["kubernetes_version"],
                             "--skip-tests", "--include-crds", "--values", str(value_file)], input="", text=True, timeout=90, output_max_bytes=MAX_ARTIFACT, check=False)
            if result.returncode:
                raise ValueError("Offline Helm rendering failed; private diagnostics suppressed")
            files[name] = yaml.safe_dump_all(parse_components(plan, result.stdout.encode(), preflight=preflight), sort_keys=False)
    files["gateway-crds.yaml"] = yaml.safe_dump_all(mark(definitions, plan), sort_keys=False)
    resources = [obj for name in ("gateway-crds.yaml", "cilium-components.yaml") for obj in loads_strict_yaml_all(files[name])]
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "inventory": inventory(resources),
                "preflight_inventory": inventory(loads_strict_yaml_all(files["cilium-preflight.yaml"])),
                "files_sha256": {name: sha(value.encode()) for name, value in files.items()}}
    files["installation-contract.json"] = json.dumps(contract, sort_keys=True, indent=2) + "\n"
    files["render-status.json"] = json.dumps({"component": "cilium-installation", "runtime": runtime, "rendered": True,
        "contract_sha256": sha(files["installation-contract.json"].encode()), "installed": False, "upgrade_verified": False,
        "ha_verified": False, "full_stack_verified": False}, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--chart", type=Path, required=True)
    parser.add_argument("--gateway-crds", type=Path, required=True)
    parser.add_argument("--helm", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        files = render(plan, read_bounded_bytes(args.chart, max_bytes=MAX_ARTIFACT), read_bounded_bytes(args.gateway_crds, max_bytes=MAX_ARTIFACT), args.runtime, args.helm)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, "installed": False, "upgrade_verified": False}))
        return 0
    except Exception:
        print("Cilium installation render stopped; inspect private artifact pins, plan and output directory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
