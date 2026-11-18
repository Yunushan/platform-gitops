#!/usr/bin/env python3
"""Render pinned Tekton Pipelines controllers and a manual infrastructure canary."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_bytes, read_bounded_text
from kubernetes_management import inventory, validate_target
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "tekton-pipelines"
RESOLVERS_NAMESPACE = "tekton-pipelines-resolvers"
CONTROLLERS = {"tekton-pipelines-controller", "tekton-events-controller", "tekton-pipelines-webhook", "tekton-pipelines-remote-resolvers"}
KINDS = {"Namespace", "ConfigMap", "Secret", "ServiceAccount", "Service", "Deployment", "Role", "RoleBinding",
         "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition", "HorizontalPodAutoscaler",
         "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}
CLUSTER_KINDS = {"Namespace", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition",
                 "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}
OWNER_LABEL = "platform.gitops/tekton-package"
RESOLVERS = {"git", "hub", "bundles", "cluster", "http"}
PROBE = """import json, os, sys, uuid
uid = str(uuid.UUID(sys.argv[1]))
if uid != sys.argv[1] or os.geteuid() == 0:
    raise SystemExit("Canary requires an actual TaskRun UID and an unprivileged worker")
marker = {"schema_version": 1, "component": "platform-tekton-canary", "uid": uid, "unprivileged": True}
with open(sys.argv[2], "x", encoding="utf-8") as result:
    result.write(json.dumps(marker, sort_keys=True))
"""


def selector(value: object) -> dict:
    if (not isinstance(value, dict) or not value or len(value) > 8
            or any(not isinstance(key, str) or len(key.split("/")) > 2
                   or ("/" in key and not dns_name(key.split("/")[0]))
                   or not label(key.split("/")[-1]) or not label(item) for key, item in value.items())):
        raise ValueError("Select explicitly provisioned Linux nodes")
    if value.get("kubernetes.io/os") != "linux":
        raise ValueError("Tekton presets require explicit Linux placement")
    return value


def validate(plan: object) -> dict:
    fields = {"schema_version", "pipelines_version", "bundle_sha256", "management_target", "replicas",
              "leader_buckets", "webhook_max_replicas", "node_selector", "failure_domain_key", "image_overrides", "api_fields", "resolvers", "canary"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Missing or unsupported Tekton plan fields")
    if (not isinstance(plan["pipelines_version"], str) or not re.fullmatch(r"1[.][0-9]+[.][0-9]+", plan["pipelines_version"])
            or not isinstance(plan["bundle_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["bundle_sha256"])):
        raise ValueError("Select a reviewed stable Tekton Pipelines 1.x release and export digest")
    validate_target(plan["management_target"])
    if (type(plan["replicas"]) is not int or not 2 <= plan["replicas"] <= 5
            or type(plan["leader_buckets"]) is not int or not plan["replicas"] <= plan["leader_buckets"] <= 10
            or type(plan["webhook_max_replicas"]) is not int or not plan["replicas"] <= plan["webhook_max_replicas"] <= 10):
        raise ValueError("Declare bounded replica, bucket and webhook scaling capacity")
    selector(plan["node_selector"])
    if not isinstance(plan["failure_domain_key"], str) or plan["failure_domain_key"] not in {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}:
        raise ValueError("Select the controller failure domain")
    overrides = plan["image_overrides"]
    if (not isinstance(overrides, dict) or len(overrides) > 40
            or any(not isinstance(key, str) or not IMAGE.fullmatch(key) or not isinstance(value, str) or not IMAGE.fullmatch(value)
                   for key, value in overrides.items())):
        raise ValueError("Image overrides must map complete digest-pinned references")
    if (not isinstance(plan["resolvers"], dict) or set(plan["resolvers"]) != RESOLVERS
            or any(type(enabled) is not bool for enabled in plan["resolvers"].values())):
        raise ValueError("Declare each remote resolver policy explicitly")
    if (plan["api_fields"] not in {"stable", "beta"}
            or (any(plan["resolvers"].values()) and plan["api_fields"] != "beta")):
        raise ValueError("Remote resolver use requires explicit beta API fields approval")
    canary = plan["canary"]
    if (not isinstance(canary, dict) or set(canary) != {"name", "namespace", "image", "node_selector"}
            or not label(canary["name"]) or len(canary["name"]) > 40 or not label(canary["namespace"])
            or canary["namespace"] in {NAMESPACE, RESOLVERS_NAMESPACE, "default", "kube-system", "kube-public", "kube-node-lease"}
            or not isinstance(canary["image"], str) or not IMAGE.fullmatch(canary["image"])):
        raise ValueError("Select a separate canary namespace, short name and pinned Python step image")
    selector(canary["node_selector"])
    return plan


def release_objects(plan: dict, payload: bytes) -> list[dict]:
    if len(payload) > 16 * 1024 ** 2 or hashlib.sha256(payload).hexdigest() != plan["bundle_sha256"]:
        raise ValueError("Tekton release differs from the reviewed artifact")
    objects = [obj for obj in loads_strict_yaml_all(payload.decode("utf-8")) if obj is not None]
    if not objects or len(objects) > 240:
        raise ValueError("Tekton release resource inventory is missing or too large")
    seen = set()
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("kind") not in KINDS or not isinstance(obj.get("metadata"), dict):
            raise ValueError("Unexpected resource in Tekton release")
        kind, meta = obj["kind"], obj["metadata"]
        if not dns_name(meta.get("name")):
            raise ValueError("Invalid release resource name")
        if kind not in CLUSTER_KINDS and meta.get("namespace") not in {NAMESPACE, RESOLVERS_NAMESPACE}:
            raise ValueError("Unexpected release namespace")
        key = (obj.get("apiVersion"), kind, meta.get("namespace"), meta["name"])
        if key in seen:
            raise ValueError("Duplicate release resource")
        seen.add(key)
        if kind == "Secret":
            if meta.get("namespace") != NAMESPACE or meta["name"] != "webhook-certs" or obj.get("data") or obj.get("stringData"):
                raise ValueError("Only the empty runtime-managed webhook certificate Secret is allowed")
            obj.pop("data", None)
            obj.pop("stringData", None)
        if kind == "CustomResourceDefinition" and obj.get("spec", {}).get("group") not in {"tekton.dev", "resolution.tekton.dev"}:
            raise ValueError("Unexpected custom API group")
        for webhook in obj.get("webhooks", []):
            connection = webhook.get("clientConfig", {})
            service = connection.get("service", {})
            if (webhook.get("failurePolicy") != "Fail" or connection.get("url")
                    or service.get("namespace") != NAMESPACE or service.get("name") != "tekton-pipelines-webhook"):
                raise ValueError("Webhook TLS routing and fail-closed admission must be preserved")
        for subject in obj.get("subjects", []):
            if subject.get("kind") == "ServiceAccount" and subject.get("namespace") not in {NAMESPACE, RESOLVERS_NAMESPACE}:
                raise ValueError("Cross-namespace release identity is not supported")
    if {obj["metadata"]["name"] for obj in objects if obj["kind"] == "Namespace"} != {NAMESPACE, RESOLVERS_NAMESPACE}:
        raise ValueError("Release namespace inventory differs from supported layout")
    if {obj["metadata"]["name"] for obj in objects if obj["kind"] == "Deployment"} != CONTROLLERS:
        raise ValueError("Release controller inventory differs from supported layout")
    info = [obj for obj in objects if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "pipelines-info"]
    if len(info) != 1 or info[0].get("data", {}).get("version") != "v" + plan["pipelines_version"]:
        raise ValueError("Declared release version differs from pipelines-info")
    crds = {obj["metadata"]["name"] for obj in objects if obj["kind"] == "CustomResourceDefinition"}
    if not {"tasks.tekton.dev", "taskruns.tekton.dev", "pipelines.tekton.dev", "pipelineruns.tekton.dev"} <= crds:
        raise ValueError("Required pipeline/task APIs are missing")
    return objects


def pin_images(pod: dict, overrides: dict, found: set) -> None:
    def pin(value):
        if not isinstance(value, str) or not IMAGE.fullmatch(value):
            raise ValueError("Every controller and injected helper image must be digest-pinned")
        found.add(value)
        return overrides.get(value, value)
    for container in pod.get("containers", []) + pod.get("initContainers", []):
        container["image"] = pin(container["image"])
        args = container.get("args", [])
        for index, arg in enumerate(args):
            flag = arg.split("=", 1)[0]
            if re.fullmatch(r"--?[a-z0-9-]*image(?:-win)?", flag):
                if "=" in arg:
                    args[index] = flag + "=" + pin(arg.split("=", 1)[1])
                elif index + 1 < len(args):
                    args[index + 1] = pin(args[index + 1])
                else:
                    raise ValueError("Injected image flag has no value")
            if flag in {"-disable-ha", "--disable-ha"}:
                raise ValueError("Release must use its supported default HA implementation")


def canary_objects(plan: dict) -> tuple[list[dict], dict]:
    canary = plan["canary"]
    meta = {"name": canary["name"], "namespace": canary["namespace"], "labels": {OWNER_LABEL: NAMESPACE}}
    security = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
    objects = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": copy.deepcopy(meta), "automountServiceAccountToken": False},
        {"apiVersion": "tekton.dev/v1", "kind": "Task", "metadata": copy.deepcopy(meta), "spec": {
            "description": "Infrastructure canary only; does not checkout, test, vote on or deploy application code.",
            "results": [{"name": "marker", "type": "string", "description": "Probe bound to this TaskRun UID"}],
            "steps": [{"name": "probe", "image": canary["image"], "securityContext": security,
                "command": ["python3", "-I", "-B", "-c", PROBE], "args": ["$(context.taskRun.uid)", "$(results.marker.path)"],
                "computeResources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "128Mi"}}}]}},
        {"apiVersion": "tekton.dev/v1", "kind": "Pipeline", "metadata": copy.deepcopy(meta), "spec": {
            "description": "Manual infrastructure canary; no source events or application CI.",
            "tasks": [{"name": "probe", "taskRef": {"name": canary["name"]}}]}},
    ]
    run = {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun", "metadata": {
        "generateName": canary["name"] + "-", "namespace": canary["namespace"], "labels": {OWNER_LABEL: NAMESPACE}},
        "spec": {"pipelineRef": {"name": canary["name"]}, "timeouts": {"pipeline": "5m", "tasks": "3m", "finally": "1m"},
            "taskRunTemplate": {"serviceAccountName": canary["name"], "podTemplate": {
                "nodeSelector": canary["node_selector"], "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                    "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"},
                    {"key": "node-role.kubernetes.io/master", "operator": "DoesNotExist"}]}]}}}}}}}
    return objects, run


def render(plan: dict, payload: bytes, runtime: str) -> dict[str, str]:
    validate(plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown application hosting runtime")
    objects = copy.deepcopy(release_objects(plan, payload))
    found, configured, config_maps = set(), set(), {}
    for obj in list(objects):
        meta, kind = obj["metadata"], obj["kind"]
        meta.setdefault("labels", {})[OWNER_LABEL] = NAMESPACE
        if kind == "ConfigMap":
            config_maps[(meta.get("namespace"), meta["name"])] = obj
        if kind == "HorizontalPodAutoscaler":
            spec = obj["spec"]
            if spec.get("scaleTargetRef", {}).get("name") != "tekton-pipelines-webhook":
                raise ValueError("Unexpected autoscaling target")
            spec.update(minReplicas=plan["replicas"], maxReplicas=plan["webhook_max_replicas"])
            configured.add("webhook-hpa")
        if kind != "Deployment":
            continue
        spec = obj["spec"]
        pod = spec["template"]["spec"]
        pin_images(pod, plan["image_overrides"], found)
        spec.update(replicas=plan["replicas"], strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}})
        pod["nodeSelector"] = plan["node_selector"]
        pod.setdefault("affinity", {})["podAntiAffinity"] = {"requiredDuringSchedulingIgnoredDuringExecution": [{
            "labelSelector": copy.deepcopy(spec["selector"]), "topologyKey": plan["failure_domain_key"]}]}
        objects.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {
            "name": meta["name"], "namespace": meta["namespace"], "labels": {OWNER_LABEL: NAMESPACE}},
            "spec": {"minAvailable": plan["replicas"] - 1, "selector": copy.deepcopy(spec["selector"])}})
    if set(plan["image_overrides"]) - found:
        raise ValueError("Unused image override; reviewed release image inventory does not match")
    required = {(NAMESPACE, name) for name in ("feature-flags", "config-defaults", "config-leader-election-controller", "config-leader-election-events", "config-leader-election-webhook")}
    required |= {(RESOLVERS_NAMESPACE, name) for name in ("resolvers-feature-flags", "config-leader-election-resolvers")}
    if not required <= set(config_maps) or "webhook-hpa" not in configured:
        raise ValueError("Required election, security or webhook scaling configuration is missing")
    for namespace, name in sorted(required):
        data = config_maps[(namespace, name)].setdefault("data", {})
        if name.startswith("config-leader-election-"):
            data.update({"buckets": str(1 if name.endswith("webhook") else plan["leader_buckets"]),
                         "lease-duration": "60s", "renew-deadline": "40s", "retry-period": "10s"})
    config_maps[(NAMESPACE, "feature-flags")]["data"].update({"disable-creds-init": "true", "enable-api-fields": plan["api_fields"],
        "set-security-context": "true", "set-security-context-read-only-root-filesystem": "true", "require-git-ssh-secret-known-hosts": "true"})
    config_maps[(NAMESPACE, "config-defaults")]["data"]["default-service-account"] = "tekton-unconfigured"
    data = config_maps[(RESOLVERS_NAMESPACE, "resolvers-feature-flags")]["data"]
    data.update({"enable-" + name + "-resolver": str(enabled).lower() for name, enabled in plan["resolvers"].items()})
    canary, run = canary_objects(plan)
    files = {"tekton-components.yaml": yaml.safe_dump_all(objects, sort_keys=False),
             "tekton-canary.yaml": yaml.safe_dump_all(canary, sort_keys=False), "tekton-canary-run.yaml": yaml.safe_dump(run, sort_keys=False)}
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "inventory": inventory(objects + canary),
        "crds": sorted(obj["metadata"]["name"] for obj in objects if obj["kind"] == "CustomResourceDefinition"),
        "webhooks": inventory([obj for obj in objects if obj["kind"] in {"MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}]),
        "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
    files["deployment-contract.json"] = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    files["render-status.json"] = json.dumps({"component": "tekton-pipelines-management-target", "runtime": runtime,
        "rendered": True, "deployed": False, "canary_completed": False, "event_integration_verified": False, "application_ci_verified": False,
        "ha_verified": False, "release_images": sorted(found), "resolved_images": sorted({plan["image_overrides"].get(image, image) for image in found}),
        "contract_sha256": hashlib.sha256(files["deployment-contract.json"].encode()).hexdigest(),
        "remaining": ["release-image-provenance-and-license-acceptance", "real-admission-ca-and-controller-election",
            "source-event-authentication-replay-deduplication-and-review-reporting", "application-ci-workspaces-registry-and-signing",
            "tenant-rbac-network-policy-and-adversarial-job-tests", "controller-worker-loss-upgrade-log-backup-and-restore-tests"]}, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        files = render(plan, read_bounded_bytes(args.bundle, max_bytes=16 * 1024 ** 2), args.runtime)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError):
        print("Tekton render stopped; inspect private target, reviewed release, image pins, placement and new output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False, "canary_completed": False, "ha_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
