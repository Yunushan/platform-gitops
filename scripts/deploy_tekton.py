#!/usr/bin/env python3
"""Inspect Tekton locally by default; install and run a canary only explicitly."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

from bounded_file import read_bounded_bytes
from kubernetes_management import Client, inventory, resource_name, verify_owned, verify_target
from render_tekton import CONTROLLERS, NAMESPACE, OWNER_LABEL, RUNTIMES, validate
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


def load_package(root: Path, digest: str) -> tuple[dict, dict[str, bytes]]:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Supply the separately reviewed contract digest")
    payload = read_bounded_bytes(root / "deployment-contract.json", max_bytes=256 * 1024)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Tekton deployment contract changed")
    contract = loads_strict_json(payload.decode("utf-8"))
    if (not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "inventory", "crds", "webhooks", "files_sha256"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in RUNTIMES):
        raise ValueError("Invalid Tekton deployment contract")
    validate(contract["plan"])
    if set(contract["files_sha256"]) != {"tekton-components.yaml", "tekton-canary.yaml", "tekton-canary-run.yaml"}:
        raise ValueError("Unexpected Tekton package files")
    files, objects = {}, []
    for name, expected in contract["files_sha256"].items():
        payload = read_bounded_bytes(root / name, max_bytes=16 * 1024 ** 2)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("Tekton deployment content changed")
        files[name] = payload
    for name in ("tekton-components.yaml", "tekton-canary.yaml"):
        objects.extend(loads_strict_yaml_all(files[name].decode("utf-8")))
    if inventory(objects) != contract["inventory"] or len(objects) > 256:
        raise ValueError("Tekton inventory differs from reviewed bytes")
    crds = sorted(obj["metadata"]["name"] for obj in objects if obj["kind"] == "CustomResourceDefinition")
    webhooks = inventory([obj for obj in objects if obj["kind"] in {"MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}])
    if crds != contract["crds"] or webhooks != contract["webhooks"] or len(webhooks) != 3:
        raise ValueError("CRD or admission inventory is incomplete")
    return contract, files


def succeeded(obj: dict) -> None:
    conditions = obj.get("status", {}).get("conditions", [])
    if len([c for c in conditions if c.get("type") == "Succeeded" and c.get("status") == "True"]) != 1:
        raise ValueError("The observed pipeline or task did not succeed")


def verify_canary(client: Client, plan: dict, run: dict, *, wait_worker: bool = False) -> None:
    canary = plan["canary"]
    succeeded(run)
    run_uid = run["metadata"].get("uid")
    if (run.get("spec", {}).get("pipelineRef") != {"name": canary["name"]}
            or run.get("spec", {}).get("taskRunTemplate", {}).get("serviceAccountName") != canary["name"]):
        raise ValueError("Observed PipelineRun selected an unexpected pipeline or identity")
    children = run.get("status", {}).get("childReferences", [])
    if (not run_uid or len(children) != 1 or children[0].get("kind") != "TaskRun" or children[0].get("pipelineTaskName") != "probe"
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", children[0].get("name", ""))):
        raise ValueError("PipelineRun has no unique expected child TaskRun")
    task = client.get("taskrun.tekton.dev/" + children[0]["name"], canary["namespace"])
    if not task:
        raise ValueError("Canary TaskRun is missing")
    succeeded(task)
    if (not any(owner.get("kind") == "PipelineRun" and owner.get("uid") == run_uid and owner.get("controller") is True
                for owner in task["metadata"].get("ownerReferences", []))
            or task.get("spec", {}).get("taskRef", {}).get("name") != canary["name"]
            or task.get("spec", {}).get("serviceAccountName") != canary["name"]):
        raise ValueError("TaskRun ownership or execution identity differs from the canary")
    markers = [result.get("value") for result in task["status"].get("results", []) if result.get("name") == "marker"]
    if len(markers) != 1 or not isinstance(markers[0], str):
        raise ValueError("TaskRun marker is absent or ambiguous")
    marker = loads_strict_json(markers[0])
    if (not isinstance(marker, dict) or set(marker) != {"schema_version", "component", "uid", "unprivileged"}
            or type(marker["schema_version"]) is not int or marker["schema_version"] != 1
            or marker["component"] != "platform-tekton-canary" or marker["uid"] != task["metadata"].get("uid")
            or marker["unprivileged"] is not True):
        raise ValueError("Probe result is not bound to this successful unprivileged TaskRun")
    pod_name = task["status"].get("podName", "")
    if not isinstance(pod_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,252}", pod_name):
        raise ValueError("TaskRun has no valid worker Pod reference")
    pod = client.get("pod/" + pod_name, canary["namespace"])
    def original_worker(value):
        return (value and value.get("spec", {}).get("serviceAccountName") == canary["name"]
            and any(owner.get("kind") == "TaskRun" and owner.get("uid") == task["metadata"].get("uid") and owner.get("controller") is True
                    for owner in value.get("metadata", {}).get("ownerReferences", [])))
    if not original_worker(pod): raise ValueError("Canary worker ownership or identity changed")
    pod_uid = pod["metadata"].get("uid")
    if not isinstance(pod_uid, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", pod_uid): raise ValueError("Canary worker has no original UID")
    if wait_worker and pod.get("status", {}).get("phase") in {"Pending", "Running"}:
        client.call(["wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/" + pod_name, "--namespace", canary["namespace"], "--timeout=60s"], seconds=60)
        pod = client.get("pod/" + pod_name, canary["namespace"])
        current_task = client.get("taskrun.tekton.dev/" + children[0]["name"], canary["namespace"])
        if not current_task or current_task.get("metadata", {}).get("uid") != task["metadata"].get("uid"):
            raise ValueError("Original canary TaskRun was replaced while waiting for its worker")
    if (not original_worker(pod) or pod.get("metadata", {}).get("uid") != pod_uid or pod.get("status", {}).get("phase") != "Succeeded"
            or pod.get("spec", {}).get("serviceAccountName") != canary["name"]
            or not any(owner.get("kind") == "TaskRun" and owner.get("uid") == task["metadata"].get("uid") and owner.get("controller") is True
                       for owner in pod.get("metadata", {}).get("ownerReferences", []))):
        raise ValueError("Canary worker Pod is not the completed child of this TaskRun")
    if not any(c.get("name") == "step-probe" and c.get("image") == canary["image"] for c in pod["spec"].get("containers", [])):
        raise ValueError("Canary Pod did not select the reviewed probe image")
    for volume in pod["spec"].get("volumes", []):
        if "secret" in volume or any("secret" in source or "serviceAccountToken" in source for source in volume.get("projected", {}).get("sources", [])):
            raise ValueError("Canary Pod unexpectedly mounts credentials")
    node_name = pod["spec"].get("nodeName", "")
    if not isinstance(node_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node_name):
        raise ValueError("Canary Pod was not assigned to a worker")
    node = client.get("node/" + node_name)
    labels = (node or {}).get("metadata", {}).get("labels", {})
    if (any(labels.get(key) != value for key, value in canary["node_selector"].items())
            or any(key in labels for key in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))):
        raise ValueError("Observed worker placement differs from the dedicated canary policy")


def operate(root: Path, digest: str, *, apply: bool = False, check_server: bool = False, run_canary: bool = False) -> dict:
    if run_canary and not apply:
        raise ValueError("Canary creation requires explicit apply authorization")
    contract, files = load_package(root, digest)
    plan = contract["plan"]
    report = {"read_only": not apply, "local_inputs_verified": True, "server_checked": False, "deployed": False,
              "canary_completed": False, "event_integration_verified": False, "application_ci_verified": False, "ha_verified": False}
    if not apply and not check_server:
        return report
    client = Client(plan["management_target"])
    verify_target(client, plan["management_target"])
    canary_namespace = client.get("namespace/" + plan["canary"]["namespace"])
    if not canary_namespace or canary_namespace.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
        raise ValueError("Preprovision the separate restricted canary namespace")
    for item in contract["inventory"]:
        if item["kind"] not in {"Task", "Pipeline"}:
            verify_owned(client, item, OWNER_LABEL, NAMESPACE)
    args = ["apply", "--server-side", "--field-manager=platform-tekton", "-f", "-"]
    # Dry-run Namespace objects are not persisted for subsequent namespaced
    # objects. Review cluster-scoped bootstrap first; only explicit apply may
    # create the missing namespaces before full admission validation.
    components = loads_strict_yaml_all(files["tekton-components.yaml"].decode("utf-8"))
    namespaces = [obj for obj in components if obj["kind"] == "Namespace"]
    missing = [obj for obj in namespaces if not client.get("namespace/" + obj["metadata"]["name"], optional=True)]
    report["namespace_bootstrap_required"] = sorted(obj["metadata"]["name"] for obj in missing)
    if missing:
        cluster_objects = [obj for obj in components if not obj["metadata"].get("namespace")]
        client.call([*args, "--dry-run=server"], payload=yaml.safe_dump_all(cluster_objects, sort_keys=False).encode(), seconds=60)
        report["namespace_bootstrap_checked"] = True
        if not apply:
            return report
        client.call(args, payload=yaml.safe_dump_all(missing, sort_keys=False).encode(), seconds=60)
    client.call([*args, "--dry-run=server"], payload=files["tekton-components.yaml"], seconds=60)
    report["server_checked"] = True
    if not apply:
        return report
    client.call(args, payload=files["tekton-components.yaml"], seconds=60)
    for crd in contract["crds"]:
        client.call(["wait", "--for=condition=Established", "customresourcedefinition/" + crd, "--timeout=120s"], seconds=120)
    for name in sorted(CONTROLLERS):
        namespace = "tekton-pipelines-resolvers" if name.endswith("remote-resolvers") else NAMESPACE
        client.call(["rollout", "status", "deployment/" + name, "--namespace", namespace, "--timeout=180s"], seconds=180)
    for item in contract["webhooks"]:
        resource = resource_name(item)
        for expression in ("{.webhooks[0].clientConfig.caBundle}", "{.webhooks[0].rules}"):
            client.call(["wait", "--for=jsonpath=" + expression, resource, "--timeout=120s"], seconds=120)
        registration = client.get(resource)
        entries = (registration or {}).get("webhooks", [])
        if not entries or any(not entry.get("rules") or not entry.get("clientConfig", {}).get("caBundle") or entry.get("failurePolicy") != "Fail"
                              or entry.get("clientConfig", {}).get("service", {}).get("namespace") != NAMESPACE
                              or entry.get("clientConfig", {}).get("service", {}).get("name") != "tekton-pipelines-webhook"
                              or entry.get("clientConfig", {}).get("url") for entry in entries):
            raise ValueError("Admission registrations have no active fail-closed rules and CA trust")
    for item in contract["inventory"]:
        if item["kind"] in {"Task", "Pipeline"}:
            verify_owned(client, item, OWNER_LABEL, NAMESPACE)
    client.call([*args, "--dry-run=server"], payload=files["tekton-canary.yaml"], seconds=60)
    client.call(args, payload=files["tekton-canary.yaml"], seconds=60)
    report["deployed"] = True
    if run_canary:
        response = loads_strict_json(client.call(["create", "-f", "-", "-o", "json"], payload=files["tekton-canary-run.yaml"], seconds=60))
        name = response.get("metadata", {}).get("name", "")
        uid = response.get("metadata", {}).get("uid")
        if not uid or not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
            raise ValueError("New canary PipelineRun has no complete identity")
        namespace = plan["canary"]["namespace"]
        client.call(["wait", "--for=condition=Succeeded", "pipelinerun.tekton.dev/" + name, "--namespace", namespace, "--timeout=300s"], seconds=300)
        run = client.get("pipelinerun.tekton.dev/" + name, namespace)
        if not run or run.get("metadata", {}).get("uid") != uid:
            raise ValueError("Observed canary PipelineRun differs from the created run")
        verify_canary(client, plan, run, wait_worker=True)
        report["canary_completed"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--run-canary", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, apply=args.apply, check_server=args.check_server, run_canary=args.run_canary)))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        print("Tekton deployment stopped; inspect private target, ownership, webhook health, controller and worker diagnostics. Partial resources are retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
