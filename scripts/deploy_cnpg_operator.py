#!/usr/bin/env python3
"""Create one fresh reviewed CNPG operator; retain original intents on uncertain writes."""

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from atomic_file import atomic_write_text
from bootstrap_keycloak_realm import sources
from bounded_file import read_bounded_bytes
from kubernetes_management import Client, resource_name, verify_target
import deploy_postgres_ha as postgres
import render_cnpg_operator as renderer
from render_rook_bootstrap import explicit_pod_defaults
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
JOURNAL = "cnpg-operator-proof.json"


def key(obj):
    return obj["kind"] + "/" + obj["metadata"]["name"]


def get(client, obj):
    return client.get(resource_name({**obj["metadata"], "apiVersion": obj["apiVersion"], "kind": obj["kind"]}), obj["metadata"].get("namespace"), optional=True)


def body(obj):
    value = copy.deepcopy({name: item for name, item in obj.items() if name not in {"metadata", "status"}})
    kind = obj["kind"]
    metadata = obj.get("metadata", {})
    annotations = copy.deepcopy(metadata.get("annotations", {}))
    if kind == "Deployment":
        revision = annotations.pop("deployment.kubernetes.io/revision", None)
        if revision is not None and (not isinstance(revision, str) or not revision.isdecimal()):
            raise ValueError("The operator deployment revision is malformed")
    finalizers = metadata.get("finalizers", [])
    if kind == "CustomResourceDefinition":
        finalizers = [item for item in finalizers if item != "customresourcecleanup.apiextensions.k8s.io"]
    value["metadata"] = {"labels": copy.deepcopy(metadata.get("labels", {})), "annotations": annotations, "finalizers": finalizers}
    if kind == "CustomResourceDefinition":
        value["spec"].setdefault("conversion", {"strategy": "None"})
        value["spec"].setdefault("preserveUnknownFields", False)
    elif kind == "Deployment":
        spec = value["spec"]
        spec.setdefault("progressDeadlineSeconds", 600)
        spec.setdefault("revisionHistoryLimit", 10)
        spec["template"]["metadata"].pop("creationTimestamp", None)
        pod = spec["template"]["spec"]
        explicit_pod_defaults(pod)
        if pod.get("serviceAccount") == pod.get("serviceAccountName"):
            pod.pop("serviceAccount", None)
    elif kind == "Service":
        spec = value["spec"]
        for name in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy"):
            spec.pop(name, None)
        for name, default in (("type", "ClusterIP"), ("sessionAffinity", "None"), ("internalTrafficPolicy", "Cluster")):
            spec.setdefault(name, default)
        for port in spec.get("ports", []):
            port.setdefault("protocol", "TCP")
    elif kind == "ClusterRoleBinding":
        for subject in value.get("subjects", []):
            if subject.get("kind") == "ServiceAccount":
                subject.setdefault("apiGroup", "")
    elif kind == "PodDisruptionBudget":
        value["spec"].setdefault("unhealthyPodEvictionPolicy", "IfHealthyBudget")
    elif kind.endswith("WebhookConfiguration"):
        for webhook in value["webhooks"]:
            for rule in webhook.get("rules", []):
                rule.setdefault("scope", "*")
            webhook["clientConfig"].pop("caBundle", None)
            webhook["clientConfig"]["service"].setdefault("port", 443)
            for name, default in (("matchPolicy", "Equivalent"), ("timeoutSeconds", 10), ("namespaceSelector", {}), ("objectSelector", {})):
                webhook.setdefault(name, default)
            if kind == "MutatingWebhookConfiguration":
                webhook.setdefault("reinvocationPolicy", "Never")
    return value


def admitted(expected, current):
    metadata = current.get("metadata", {})
    if (current.get("apiVersion") != expected["apiVersion"] or current.get("kind") != expected["kind"]
            or metadata.get("name") != expected["metadata"]["name"] or metadata.get("namespace") != expected["metadata"].get("namespace")
            or metadata.get("deletionTimestamp") or metadata.get("ownerReferences")
            or any(metadata.get("annotations", {}).get(name) != value for name, value in expected["metadata"]["annotations"].items())
            or any(metadata.get("labels", {}).get(name) != value for name, value in expected["metadata"].get("labels", {}).items())
            or body(expected) != body(current)):
        raise ValueError("Admission or a live operator resource changed the finite reviewed policy")
    return renderer.fingerprint(body(current))


def owned(current, expected, entry):
    if (not current or not renderer.uid(current.get("metadata", {}).get("uid"))
            or entry["uid"] is not None and current["metadata"]["uid"] != entry["uid"]
            or admitted(expected, current) != entry["admitted_sha256"]):
        raise ValueError("An original operator resource disappeared or changed")


def prerequisites(client, plan):
    verify_target(client, plan["target"])
    version = loads_strict_json(client.call(["version", "-o", "json"]))["serverVersion"]
    if str(version.get("major")) != "1" or not re.fullmatch(r"3[4-6][+]?", str(version.get("minor"))):
        raise ValueError("Review Kubernetes outside the selected CNPG 1.30 supported 1.34-1.36 lane")
    namespace = client.get("namespace/" + renderer.NAMESPACE) or {}
    metadata = namespace.get("metadata", {})
    if (metadata.get("uid") != plan["namespace_uid"] or metadata.get("deletionTimestamp")
            or metadata.get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted"):
        raise ValueError("Require the original explicitly restricted operator namespace")
    for node in plan["nodes"]:
        current = client.get("node/" + node["name"]) or {}
        metadata, spec = current.get("metadata", {}), current.get("spec", {})
        labels = metadata.get("labels", {})
        if (metadata.get("uid") != node["uid"] or metadata.get("deletionTimestamp") or not postgres.condition(current, "Ready")
                or labels.get("kubernetes.io/hostname") != node["name"] or labels.get(plan["domain_label"]) != node["domain"]
                or current.get("status", {}).get("nodeInfo", {}).get("operatingSystem") != "linux" or spec.get("unschedulable")
                or any(taint.get("effect") in {"NoSchedule", "NoExecute"} for taint in spec.get("taints", []))):
            raise ValueError("A reviewed operator host or failure domain changed")
    # These upstream optional configuration inputs are intentionally absent.
    # Never let an unrelated object change startup policy during bootstrap.
    for kind in ("configmap", "secret"):
        if client.get(kind + "/cnpg-controller-manager-config", renderer.NAMESPACE, optional=True) is not None:
            raise ValueError("Unreviewed ambient operator configuration is present")


def certificates_absent(client):
    for name in ("cnpg-ca-secret", "cnpg-webhook-cert"):
        if client.call(["get", "secret/" + name, "--namespace", renderer.NAMESPACE, "--ignore-not-found=true", "-o", "name"]).strip():
            raise ValueError("Fresh operator bootstrap cannot adopt existing certificate secrets")


def ready(client, plan, phases, state):
    for obj in sum(phases.values(), []):
        current = get(client, obj)
        owned(current, obj, state["objects"][key(obj)])
        if obj["kind"] == "CustomResourceDefinition" and not all(postgres.condition(current, name) for name in ("Established", "NamesAccepted")):
            return None
    deployment = client.get("deployment/" + renderer.DEPLOYMENT, renderer.NAMESPACE)
    metadata, status = deployment["metadata"], deployment.get("status", {})
    if status.get("observedGeneration") != metadata.get("generation") or any(status.get(name) != 2 for name in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")):
        return None
    revision = metadata.get("annotations", {}).get("deployment.kubernetes.io/revision")
    if not isinstance(revision, str) or not revision.isdecimal():
        return None
    sets = {item["metadata"]["uid"] for item in postgres.collection(client, "replicasets", renderer.NAMESPACE)
            if postgres.controlled(item, "Deployment", metadata["uid"]) and item["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") == revision}
    pods = [item for item in postgres.collection(client, "pods", renderer.NAMESPACE)
            if any(postgres.controlled(item, "ReplicaSet", parent) for parent in sets)]
    image = renderer.cnpg_release.RELEASES[plan["version"]]["image"]
    expected_container = body(phases["workload"][0])["spec"]["template"]["spec"]["containers"][0]
    domains = {node["name"]: node["domain"] for node in plan["nodes"]}
    if len(pods) != 2 or any(not postgres.running_pod(pod, {image}) for pod in pods):
        return None
    for pod in pods:
        spec = pod["spec"]
        if (spec.get("nodeName") not in domains or len(spec.get("containers", [])) != 1 or spec.get("initContainers")
                or spec.get("serviceAccountName") != "cnpg-manager"):
            raise ValueError("An original operator pod has unexpected placement or execution policy")
        container = copy.deepcopy(spec["containers"][0])
        explicit_pod_defaults({"containers": [container]})
        for name in ("name", "image", "command", "args", "env", "resources", "securityContext"):
            if container.get(name) != expected_container.get(name):
                raise ValueError("An original operator pod no longer runs the reviewed container")
    if len({domains[pod["spec"]["nodeName"]] for pod in pods}) != 2:
        raise ValueError("Operator candidates must span two reviewed failure domains")
    ca_values = set()
    for obj in phases["webhooks"]:
        for webhook in get(client, obj)["webhooks"]:
            value = webhook.get("clientConfig", {}).get("caBundle")
            if not value:
                return None
            raw = base64.b64decode(value, validate=True)
            if not 128 <= len(raw) <= 65536 or not raw.startswith(b"-----BEGIN CERTIFICATE-----"):
                raise ValueError("The operator webhook has no bounded PEM trust bundle")
            ca_values.add(hashlib.sha256(raw).hexdigest())
    if len(ca_values) != 1 or state.get("webhook_ca_sha256") not in {None, next(iter(ca_values))}:
        raise ValueError("The original webhook trust changed during bootstrap")
    return {"namespace": renderer.NAMESPACE, "namespace_uid": plan["namespace_uid"], "deployment": renderer.DEPLOYMENT,
            "uid": metadata["uid"], "image": image, "crd_uid": state["objects"]["CustomResourceDefinition/clusters.postgresql.cnpg.io"]["uid"],
            "webhook_ca_sha256": next(iter(ca_values))}


def operate(plan, runtime, release, *, mode="local", output=None, wait_seconds=600, client=None):
    phases = renderer.objects(plan, runtime, release)
    source = sources(Path(__file__).stem)
    result = {"local_inputs_verified": True, "server_checked": False, "operator_resources_ready": False, "webhook_admission_checked": False, **renderer.UNVERIFIED}
    if mode == "local":
        return result
    if mode not in {"check-server", "create", "resume"} or output is None or not renderer.integer(wait_seconds, 5, 1800):
        raise ValueError("Select one bounded explicit operator operation")
    if mode != "check-server" and plan["admitted_sha256"] is None:
        raise ValueError("Review and pin server admission before creating an operator")
    directory = Path(output).absolute()
    if directory.resolve() != directory or not directory.relative_to((ROOT / "private").resolve()).parts or directory.is_symlink():
        raise ValueError("Use an original canonical private operation directory")
    if mode != "resume":
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("Operator journals require owner-only private directories")
    receipt = directory / JOURNAL
    expected = sum(phases.values(), [])
    with postgres.lock(directory):
        if mode == "resume":
            info = receipt.lstat()
            if not stat.S_ISREG(info.st_mode) or receipt.is_symlink() or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise ValueError("The original operator journal is no longer private")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (type(state.get("schema_version")) is not int or state.get("schema_version") != 1 or state.get("action") != "create" or state.get("runtime") != runtime
                    or state.get("plan_sha256") != renderer.fingerprint(plan) or state.get("source_sha256") != source
                    or state.get("release_sha256") != hashlib.sha256(release).hexdigest() or not isinstance(state.get("objects"), dict)
                    or not set(state["objects"]) <= {key(obj) for obj in expected}
                    or any(state.get(name) is not False for name in renderer.UNVERIFIED)):
                raise ValueError("Resume only the original implementation, release, plan and object intents")
            for entry in state["objects"].values():
                if (not isinstance(entry, dict) or set(entry) != {"uid", "admitted_sha256"} or not renderer.sha(entry["admitted_sha256"])
                        or entry["uid"] is not None and not renderer.uid(entry["uid"])):
                    raise ValueError("An original operator object intent is invalid")
        else:
            state = {"schema_version": 1, "action": mode, "runtime": runtime, "plan_sha256": renderer.fingerprint(plan),
                     "source_sha256": source, "release_sha256": hashlib.sha256(release).hexdigest(), "objects": {}, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
            atomic_write_text(receipt, renderer.canonical(state) + "\n")
        record("started")
        network = client or Client(plan["target"])
        try:
            with network.frozen():
                prerequisites(network, plan)
                if "Deployment/" + renderer.DEPLOYMENT not in state["objects"]:
                    certificates_absent(network)
                hashes = {}
                for obj in expected:
                    name = key(obj)
                    current = get(network, obj)
                    if name in state["objects"]:
                        owned(current, obj, state["objects"][name])
                        state["objects"][name]["uid"] = current["metadata"]["uid"]
                        hashes[name] = state["objects"][name]["admitted_sha256"]
                    else:
                        if current is not None:
                            raise ValueError("Fresh operator bootstrap cannot adopt existing resources")
                        preview = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=renderer.canonical(obj).encode()))
                        hashes[name] = admitted(obj, preview)
                        atomic_write_text(directory / (hashlib.sha256(name.encode()).hexdigest() + ".admitted.json"), renderer.canonical(body(preview)))
                pin = renderer.fingerprint(hashes)
                if plan["admitted_sha256"] is not None and plan["admitted_sha256"] != pin:
                    raise ValueError("Concrete operator admission differs from the reviewed hash")
                record("admission-checked", observed_admitted_sha256=pin, server_checked=True)
                if mode == "check-server":
                    record("check-completed")
                    return {**result, "server_checked": True, "admitted_sha256": pin}
                for obj in expected:
                    name = key(obj)
                    if name in state["objects"]:
                        continue
                    prerequisites(network, plan)
                    if obj["kind"] == "Deployment":
                        certificates_absent(network)
                    if sources(Path(__file__).stem) != source or get(network, obj) is not None:
                        raise ValueError("Operator source or a pending object name changed before creation")
                    state["objects"][name] = {"uid": None, "admitted_sha256": hashes[name]}
                    record("before-create", pending=name)
                    current = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=renderer.canonical(obj).encode()))
                    if renderer.uid(current.get("metadata", {}).get("uid")):
                        state["objects"][name]["uid"] = current["metadata"]["uid"]
                        record("create-response-observed")
                    owned(current, obj, state["objects"][name])
                    record("created", pending=None)
                deadline = time.monotonic() + wait_seconds
                while True:
                    prerequisites(network, plan)
                    handoff = ready(network, plan, phases, state)
                    if handoff:
                        break
                    record("waiting-for-original-operator")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Observe the same original operator resources again")
                    time.sleep(2)
                state["webhook_ca_sha256"] = handoff.pop("webhook_ca_sha256")
                # Server dry-run exercises the real webhook without creating a
                # PostgreSQL cluster, credentials, volumes or an instance pod.
                probe = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {
                    "name": "bootstrap-probe-" + plan["operation_id"][:8], "namespace": renderer.NAMESPACE},
                    "spec": {"instances": 1, "storage": {"size": "1Gi"}, "bootstrap": {"initdb": {"database": "operator_probe", "owner": "operator_probe"}}}}
                network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=renderer.canonical(probe).encode())
                if not ready(network, plan, phases, state) or sources(Path(__file__).stem) != source:
                    raise ValueError("Final original operator readiness or implementation changed")
                result.update(server_checked=True, operator_resources_ready=True, webhook_admission_checked=True)
                record("completed", operator=handoff, **result)
                return result
        except BaseException:
            record("failed", operator_resources_ready=False, error_code="cnpg-operator-unverified")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime", choices=renderer.RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=600)
    modes = parser.add_mutually_exclusive_group()
    for mode in ("check-server", "create", "resume"):
        modes.add_argument("--" + mode, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        release = read_bounded_bytes(args.release, max_bytes=2 * 1024 ** 2)
        mode = "check-server" if args.check_server else "create" if args.create else "resume" if args.resume else "local"
        print(renderer.canonical(operate(plan, args.runtime, release, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds)))
        return 0
    except Exception:
        print("Operator bootstrap stopped; preserve the private journal and original resource handles. Details suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
