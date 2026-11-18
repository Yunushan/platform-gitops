#!/usr/bin/env python3
"""Render an optional fresh CloudNativePG operator on an explicit Kubernetes target."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_bytes
import cnpg_release
from kubernetes_management import validate_target
from postgres_ha_policy import canonical_capacity, canonical_cpu
from render_gerrit_router import write_bundle
from render_ceph_storage import canonical, fingerprint, integer, resource, sha, uid
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("rke2", "k3s", "k8s")
NAMESPACE = "cnpg-system"
DEPLOYMENT = "cnpg-controller-manager"
OPERATION = "platform.gitops/cnpg-bootstrap-operation"
POLICY = "platform.gitops/cnpg-bootstrap-policy"
UNVERIFIED = dict.fromkeys(("database_created", "database_io_verified", "operator_failover_verified", "ha_verified", "full_stack_verified"), False)


def validate(plan, runtime):
    fields = {"schema_version", "operation_id", "version", "target", "namespace_uid", "nodes", "domain_label", "resources", "admitted_sha256"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not uid(plan["operation_id"]) or plan["version"] not in cnpg_release.RELEASES
            or not uid(plan["namespace_uid"]) or plan["domain_label"] not in {"topology.kubernetes.io/zone", "topology.rook.io/rack"}
            or plan["admitted_sha256"] is not None and not sha(plan["admitted_sha256"])):
        raise ValueError("Select a reviewed fresh operator operation, release and target scope")
    validate_target(plan["target"])
    nodes = plan["nodes"]
    if (not isinstance(nodes, list) or not 2 <= len(nodes) <= 32
            or any(not isinstance(node, dict) or set(node) != {"name", "uid", "domain"}
                   or not resource(node["name"]) or not uid(node["uid"]) or not resource(node["domain"]) for node in nodes)
            or any(len({node[key] for node in nodes}) != len(nodes) for key in ("name", "uid"))
            or len({node["domain"] for node in nodes}) < 2):
        raise ValueError("Select distinct original controller nodes spanning at least two domains")
    budgets = plan["resources"]
    if not isinstance(budgets, dict) or set(budgets) != {"requests", "limits"}:
        raise ValueError("Declare finite operator requests and limits")
    observed = {}
    for key, value in budgets.items():
        if (not isinstance(value, dict) or set(value) != {"cpu", "memory"}
                or not isinstance(value["cpu"], str) or not re.fullmatch(r"[1-9][0-9]{0,5}m?", value["cpu"])
                or not isinstance(value["memory"], str) or not re.fullmatch(r"[1-9][0-9]{0,4}(?:Mi|Gi)", value["memory"])):
            raise ValueError("Use positive finite operator CPU and binary memory quantities")
        cpu = int(value["cpu"][:-1]) if value["cpu"].endswith("m") else int(value["cpu"]) * 1000
        memory = int(value["memory"][:-2]) * (1024 if value["memory"].endswith("Gi") else 1)
        if not 50 <= cpu <= 8000 or not 128 <= memory <= 8192:
            raise ValueError("Operator budgets are outside the reviewed bounds")
        observed[key] = (cpu, memory)
    if any(request > limit for request, limit in zip(observed["requests"], observed["limits"])):
        raise ValueError("Operator limits cannot be below requests")


def objects(plan, runtime, release):
    validate(plan, runtime)
    # Exclude only the preprovisioned Namespace and upstream convenience roles.
    # The runtime manager's upstream access policy remains visible for review.
    values = [value for value in cnpg_release.objects(plan["version"], release)
              if value["kind"] != "Namespace" and not (value["kind"] == "ClusterRole" and value["metadata"]["name"] != "cnpg-manager")]
    phases = {"apis": [], "support": [], "webhooks": [], "workload": []}
    identity = fingerprint({key: value for key, value in plan.items() if key != "admitted_sha256"})
    for value in values:
        metadata = value["metadata"]
        metadata.setdefault("annotations", {}).update({OPERATION: plan["operation_id"], POLICY: identity})
        metadata.setdefault("labels", {})["app.kubernetes.io/version"] = plan["version"]
        kind = value["kind"]
        if kind == "Deployment":
            if metadata["name"] != DEPLOYMENT:
                raise ValueError("An unexpected operator workload was selected")
            spec, pod = value["spec"], value["spec"]["template"]["spec"]
            spec.update(replicas=2, strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}})
            spec["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = plan["version"]
            pod.update(enableServiceLinks=False, automountServiceAccountToken=True,
                       affinity={"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                           {"key": "kubernetes.io/hostname", "operator": "In", "values": sorted(node["name"] for node in plan["nodes"])}]}]}},
                           "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": plan["domain_label"],
                               "labelSelector": {"matchLabels": spec["selector"]["matchLabels"]}}]}})
            if len(pod["containers"]) != 1 or pod.get("initContainers"):
                raise ValueError("Review the changed upstream operator container inventory")
            container = pod["containers"][0]
            container["image"] = cnpg_release.RELEASES[plan["version"]]["image"]
            container["imagePullPolicy"] = "IfNotPresent"
            container["resources"] = {key: {"cpu": canonical_cpu(row["cpu"]), "memory": canonical_capacity(row["memory"])} for key, row in plan["resources"].items()}
            image_vars = [entry for entry in container["env"] if entry["name"] == "OPERATOR_IMAGE_NAME"]
            if len(image_vars) != 1 or container["command"] != ["/manager"] or "--leader-elect" not in container["args"]:
                raise ValueError("Review the changed upstream operator startup contract")
            image_vars[0]["value"] = container["image"]
        phase = "apis" if kind == "CustomResourceDefinition" else "webhooks" if kind.endswith("WebhookConfiguration") else "workload" if kind == "Deployment" else "support"
        phases[phase].append(value)
    if len(phases["apis"]) != 11 or len(phases["workload"]) != 1 or len(phases["webhooks"]) != 2:
        raise ValueError("Review the changed pinned operator API/workload inventory")
    phases["support"].append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
        "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE, "annotations": {OPERATION: plan["operation_id"], POLICY: identity}},
        "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "cloudnative-pg"}}}})
    return phases


def render(plan, runtime, release):
    phases = objects(plan, runtime, release)
    files = {name + ".yaml": yaml.safe_dump_all(values, sort_keys=True) for name, values in phases.items()}
    contract = {"schema_version": 1, "runtime": runtime, "plan": copy.deepcopy(plan),
                "release_sha256": hashlib.sha256(release).hexdigest(),
                "files_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}}
    files["cnpg-operator-contract.json"] = canonical(contract) + "\n"
    files["render-status.json"] = canonical({"rendered": True, "contract_sha256": hashlib.sha256(files["cnpg-operator-contract.json"].encode()).hexdigest(),
        "operator_resources_ready": False, "publisher_signature_verified": False, **UNVERIFIED}) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        release = read_bounded_bytes(args.release, max_bytes=2 * 1024 ** 2)
        write_bundle(render(plan, args.runtime, release), args.output_dir, private_root=ROOT / "private")
        print(canonical({"rendered": True, "operator_resources_ready": False, **UNVERIFIED}))
        return 0
    except Exception:
        print("Operator rendering refused; review the private release, scope and finite policy. Details suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
