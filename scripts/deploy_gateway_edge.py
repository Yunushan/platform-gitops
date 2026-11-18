#!/usr/bin/env python3
"""Check the reviewed Kubernetes edge pair, or explicitly roll it out one node at a time."""

from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, inventory, resource_name, verify_target
import deploy_cilium_gateway as gateway
from deploy_cilium_installation import includes
import render_gateway_edge as renderer
from render_gerrit_router import ssh_nodeport_service
from start_gateway_edge import load_bundle
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]


def load_package(root, digest, gateway_root):
    payload = read_bounded_bytes(root / "edge-contract.json", max_bytes=256 * 1024)
    from strict_json import loads_strict_json
    contract = loads_strict_json(payload)
    host = contract["plan"]["entry_hosts"][0]["name"]
    load_bundle(root, digest, host)
    reviewed, _ = gateway.load_package(gateway_root, contract["plan"]["gateway_contract_sha256"])
    if reviewed["plan"] != contract["gateway_plan"]:
        raise ValueError("Gateway reference differs from the prepared external entry")
    regenerated = renderer.render(contract["plan"], reviewed, contract["runtime"])
    if regenerated["edge-contract.json"].encode() != payload:
        raise ValueError("External entry differs from generated policy")
    if contract["runtime"] not in {"rke2", "k3s", "k8s"}:
        return contract, None, reviewed
    workload = read_bounded_bytes(root / "edge-workloads.yaml", max_bytes=2 * 1024 ** 2)
    if workload != regenerated["edge-workloads.yaml"].encode():
        raise ValueError("External entry workload changed")
    return contract, loads_strict_yaml_all(workload.decode()), reviewed


def owned(client, obj, plan):
    item = inventory([obj])[0]
    current = client.get(resource_name(item), item.get("namespace"), optional=True)
    if current and (current.get("metadata", {}).get("labels", {}).get(renderer.OWNER) != plan["name"] or current.get("metadata", {}).get("deletionTimestamp")):
        raise ValueError("External entry cannot adopt an unowned/deleting resource")
    return current


def verify_gerrit_ssh(client, plan, selected, serving_domains):
    ssh = plan["gerrit_ssh"]
    route = renderer.gerrit_route(selected, ssh["hostname"])
    namespace = route["namespace"]
    expected = ssh_nodeport_service(namespace, ssh["node_port"])
    name = expected["metadata"]["name"]
    service = client.get("service/" + name, namespace, optional=True) or {}
    meta, spec = service.get("metadata", {}), service.get("spec", {})
    port, = expected["spec"]["ports"]
    if (not meta.get("uid") or meta.get("deletionTimestamp") or spec.get("selector") != route["pod_selector"]
            or not includes(expected["spec"], spec) or spec.get("externalIPs")):
        raise ValueError("Apply the Gerrit router SSH Service with the reviewed Local NodePort")
    endpoints = gateway.backend_endpoints(client, {**route, "service": name, "target_port": 29418}, service, port)
    if len({serving_domains[node] for node in endpoints & serving_domains.keys()}) < 2:
        raise ValueError("Gerrit SSH needs ready router endpoints on two selected serving failure domains")


def preflight(client, contract, objects, reviewed):
    plan, selected = contract["plan"], contract["gateway_plan"]
    verify_target(client, selected["management_target"])
    gateway.preflight(client, reviewed)
    if not gateway.ready(client, selected):
        raise ValueError("Gateway routes must be programmed before exposing the external entry")
    if objects is not None:
        namespace = client.get("namespace/" + plan["namespace"]) or {}
        if namespace.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") != "privileged":
            raise ValueError("Preprovision the dedicated privileged infrastructure namespace")
        ca = client.call(["get", "secret/" + plan["ca_secret"], "--namespace", plan["namespace"], "-o", 'jsonpath={.immutable}{"\\n"}{.data.ca\\.crt}'])
        immutable, _, encoded = ca.partition("\n")
        payload = base64.b64decode(encoded.strip(), validate=True)
        if immutable != "true" or len(payload) > 256 * 1024 or hashlib.sha256(payload).hexdigest() != plan["ca_sha256"]:
            raise ValueError("Provision the immutable reviewed backend CA Secret")
    domains = set()
    for host in plan["entry_hosts"]:
        if host["node_name"] is None:
            continue
        node = client.get("node/" + host["node_name"]) or {}
        meta = node.get("metadata", {})
        addresses = {item.get("address") for item in node.get("status", {}).get("addresses", []) if item.get("type") == "InternalIP"}
        domain = meta.get("labels", {}).get(selected["failure_domain_key"])
        if (meta.get("uid") != host["node_uid"] or host["address"] not in addresses or meta.get("deletionTimestamp")
                or domain != host["failure_domain"] or not gateway.condition(node.get("status", {}).get("conditions", []), "Ready")):
            raise ValueError("Entry Node identity, interface address or availability differs")
        domains.add(domain)
    if objects is not None and len(domains) != 2:
        raise ValueError("Entry peers must span the selected Kubernetes failure domains")
    serving_domains = {}
    for backend in plan["backends"]:
        node = client.get("node/" + backend["node_name"]) or {}
        meta = node.get("metadata", {})
        addresses = {item.get("address") for item in node.get("status", {}).get("addresses", []) if item.get("type") == "InternalIP"}
        domain = meta.get("labels", {}).get(selected["failure_domain_key"])
        if (meta.get("uid") != backend["node_uid"] or backend["address"] not in addresses or not domain
                or not gateway.matches(meta.get("labels", {}), selected["serving_selector"])
                or not gateway.condition(node.get("status", {}).get("conditions", []), "Ready")):
            raise ValueError("Gateway serving-node identity/address differs")
        serving_domains[backend["node_name"]] = domain
    if len(set(serving_domains.values())) < 2:
        raise ValueError("External entry requires serving nodes in two failure domains")
    if ssh := plan.get("gitlab_ssh"):
        service = client.get("service/gitlab-gitlab-shell", "gitlab") or {}
        spec = service.get("spec", {})
        port = {"name": "ssh", "protocol": "TCP", "port": ssh["port"],
                "nodePort": ssh["node_port"], "targetPort": 2222}
        if (service.get("metadata", {}).get("deletionTimestamp")
                or spec.get("type") != "NodePort" or spec.get("externalTrafficPolicy") != "Cluster"
                or spec.get("selector") != {"app": "gitlab-shell", "release": "gitlab"}
                or not any(includes(port, candidate) for candidate in spec.get("ports", []))):
            raise ValueError("Apply the GitLab Shell Service rendered from this same edge plan")
    if "gerrit_ssh" in plan:
        verify_gerrit_ssh(client, plan, selected, serving_domains)
    for obj in objects or (): owned(client, obj, plan)


def verify_daemonset(client, desired, plan):
    obj = owned(client, desired, plan) or {}
    status, meta = obj.get("status", {}), obj.get("metadata", {})
    generation = meta.get("generation")
    expected = copy.deepcopy(desired["spec"])
    actual_pod = obj.get("spec", {}).get("template", {}).get("spec", {})
    for field in ("hostPID", "hostIPC"):
        expected["template"]["spec"].pop(field, None)
    if (type(generation) is not int or generation < 1 or status.get("observedGeneration") != generation or not includes(expected, obj.get("spec"))
            or any(actual_pod.get(field, False) is not False for field in ("hostPID", "hostIPC")) or actual_pod.get("initContainers")
            or any(status.get(key) != 1 for key in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable"))
            or status.get("numberMisscheduled", 0) != 0):
        raise ValueError("Edge DaemonSet is not the current ready reviewed workload")
    return meta.get("uid")


def journal(output, initial):
    resolved = output.resolve()
    if not resolved.relative_to((ROOT / "private").resolve()).parts or output.exists() or output.is_symlink():
        raise ValueError("Use a new private edge proof directory")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = dict(initial)
    def record(**values):
        state.update(values, recorded_at=datetime.now(timezone.utc).isoformat())
        atomic_write_text(resolved / "edge-proof.json", json.dumps(state, sort_keys=True, indent=2) + "\n")
    record(phase="local-validated")
    return record, state


def operate(root, digest, gateway_root, *, check_server=False, apply=False, verify=False, output=None):
    if sum(bool(v) for v in (check_server, apply, verify)) > 1:
        raise ValueError("Select one edge server mode")
    contract, objects, reviewed = load_package(root, digest, gateway_root)
    result = {"read_only": not apply, "local_inputs_verified": True, "deployed": False, "pair_ready": False, "vip_failover_verified": False, "application_health_verified": False,
              "ha_verified": False, "full_stack_verified": False}
    if not any((check_server, apply, verify)): return result
    if output is None or objects is None and not check_server:
        raise ValueError("Server checks require a new private proof directory, and rollout modes require a Kubernetes edge package")
    record, state = journal(output, {"contract_sha256": digest, "runtime": contract["runtime"], **result})
    try:
        plan = contract["plan"]
        client = Client(contract["gateway_plan"]["management_target"])
        with client.frozen():
            preflight(client, contract, objects, reviewed)
            record(phase="prerequisites-verified")
            if objects is None:
                result["prerequisites_verified"] = True
                record(phase="completed", **result)
                return result
            args = ["apply", "--server-side", "--field-manager=platform-gateway-edge", "-f", "-"]
            if not verify:
                client.call([*args, "--dry-run=server"], payload=yaml.safe_dump_all(objects, sort_keys=False).encode(), seconds=60)
            if apply:
                preflight(client, contract, objects, reviewed)
                support = [obj for obj in objects if obj["kind"] != "DaemonSet"]
                record(phase="before-support-apply", write_attempted=True, write_outcome="unverified")
                client.call(args, payload=yaml.safe_dump_all(support, sort_keys=False).encode(), seconds=60)
                # Never update both VIP peers concurrently. Each must become
                # current/ready before the other node's workload is touched.
                for desired in reversed([obj for obj in objects if obj["kind"] == "DaemonSet"]):
                    name = desired["metadata"]["name"]
                    record(phase="before-peer-apply", peer=name, write_outcome="unverified")
                    client.call(args, payload=yaml.safe_dump(desired, sort_keys=False).encode(), seconds=60)
                    client.call(["rollout", "status", "daemonset/" + name, "--namespace", plan["namespace"], "--timeout=180s"], seconds=180)
                    uid = verify_daemonset(client, desired, plan)
                    record(phase="peer-ready", peer=name, peer_uid=uid, write_outcome="acknowledged")
                result["deployed"] = True
            if apply or verify:
                for obj in objects:
                    current = owned(client, obj, plan)
                    if obj["kind"] == "ConfigMap" and (not current or current.get("immutable") is not True or current.get("data") != obj["data"]):
                        raise ValueError("Immutable edge program/configuration differs")
                    if obj["kind"] == "DaemonSet": verify_daemonset(client, obj, plan)
                    if obj["kind"] == "PodDisruptionBudget" and not includes(obj["spec"], (current or {}).get("spec")):
                        raise ValueError("Edge disruption policy differs")
                preflight(client, contract, objects, reviewed)
                result["pair_ready"] = True
            record(phase="completed", **result)
            return result
    except Exception:
        record(phase="failed", failed_at=state["phase"], error_code="edge-validation-or-operation-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--gateway-bundle-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, args.gateway_bundle_dir, check_server=args.check_server, apply=args.apply, verify=args.verify, output=args.output_dir)))
        return 0
    except Exception:
        print("Gateway edge deployment stopped; retain the private proof and inspect host/Gateway readiness. No automatic rollback was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
