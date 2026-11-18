#!/usr/bin/env python3
"""Render a pinned Flux export with shared artifact serving and scoped Git sync."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_bytes, read_bounded_text
from kubernetes_management import validate_target
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
CORE = {"source-controller", "kustomize-controller", "helm-controller", "notification-controller"}
OPTIONAL = {"image-automation-controller", "image-reflector-controller"}
KINDS = {"Namespace", "CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding",
         "ServiceAccount", "Service", "NetworkPolicy", "Deployment", "ResourceQuota"}
CLUSTER_KINDS = {"Namespace", "CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding"}
OWNER_LABEL = "platform.gitops/flux-package"
NGINX = """worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 1024; }
http {
  access_log off;
  server_tokens off;
  client_body_temp_path /tmp/client-body;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  server {
    listen 9090;
    root /data;
    autoindex off;
    location = /healthz { return 200 'ready\\n'; }
    location / { limit_except GET { deny all; } try_files $uri =404; }
  }
}
"""


def validate(plan: object) -> dict:
    fields = {"schema_version", "flux_version", "namespace", "bundle_sha256", "images", "artifact_server_image",
              "artifact_claim", "failure_domain_key", "node_selector", "management_target", "sync"}
    if (not isinstance(plan, dict) or not fields <= set(plan) <= fields | {"offline_images"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1):
        raise ValueError("Missing or unsupported Flux plan fields")
    if "offline_images" in plan and type(plan["offline_images"]) is not bool:
        raise ValueError("Set offline_images to a boolean")
    if (not isinstance(plan["flux_version"], str) or not re.fullmatch(r"2[.][0-9]+[.][0-9]+", plan["flux_version"])
            or not label(plan["namespace"]) or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}
            or not isinstance(plan["bundle_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["bundle_sha256"])):
        raise ValueError("Declare a stable exact Flux release, dedicated namespace and export digest")
    images = plan["images"]
    if (not isinstance(images, dict) or not CORE <= set(images) <= CORE | OPTIONAL
            or any(not isinstance(value, str) or not IMAGE.fullmatch(value) for value in images.values())
            or not isinstance(plan["artifact_server_image"], str) or not IMAGE.fullmatch(plan["artifact_server_image"])
            or not label(plan["artifact_claim"])):
        raise ValueError("Pin every selected controller and artifact-server image, and select a shared claim")
    if not isinstance(plan["failure_domain_key"], str) or plan["failure_domain_key"] not in {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}:
        raise ValueError("Declare the required placement failure domain")
    selector = plan["node_selector"]
    if (not isinstance(selector, dict) or not selector or len(selector) > 8
            or any(not isinstance(key, str) or len(key.split("/")) > 2
                   or ("/" in key and not dns_name(key.split("/")[0]))
                   or not label(key.split("/")[-1]) or not label(value) for key, value in selector.items())):
        raise ValueError("Select explicitly provisioned controller nodes")
    target = plan["management_target"]
    validate_target(target)
    sync = plan["sync"]
    required_sync = {"name", "namespace", "url", "branch", "path", "read_secret", "service_account", "prune"}
    if (not isinstance(sync, dict) or not required_sync <= set(sync) <= required_sync | {"commit"}
            or any(not label(sync[field]) for field in ("name", "namespace", "read_secret", "service_account"))
            or sync["namespace"] == plan["namespace"] or sync["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}
            or type(sync["prune"]) is not bool):
        raise ValueError("Declare a separate reconciliation namespace, read credential reference and scoped service account")
    branch, path = sync["branch"], sync["path"]
    if (not isinstance(branch, str) or len(branch) > 200
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*", branch)
            or ".." in branch or any(part.endswith((".", ".lock")) for part in branch.split("/"))):
        raise ValueError("Select an exact simple Git branch")
    if "commit" in sync and (not isinstance(sync["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", sync["commit"])):
        raise ValueError("Select a full lowercase SHA-1 commit ID")
    if (not isinstance(path, str) or not path.startswith("./") or not re.fullmatch(r"[A-Za-z0-9_./-]{3,300}", path)
            or ".." in PurePosixPath(path).parts or path == "./" or "//" in path):
        raise ValueError("Reconcile an explicit relative subdirectory")
    try:
        remote = urlsplit(sync["url"])
        if (not isinstance(sync["url"], str) or any(char.isspace() for char in sync["url"])
                or remote.scheme not in {"https", "ssh"} or not dns_name(remote.hostname)
                or remote.password is not None or remote.query or remote.fragment or remote.path in {"", "/"}
                or (remote.scheme == "https" and remote.username is not None)
                or (remote.scheme == "ssh" and (not remote.username or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", remote.username)))):
            raise ValueError("Use a credential-free HTTPS or SSH Git URL")
        if remote.port is not None and not 1 <= remote.port <= 65535:
            raise ValueError("Git URL port is outside the valid range")
    except (TypeError, ValueError):
        raise ValueError("Use a credential-free HTTPS or SSH Git URL") from None
    return plan


def export_objects(plan: dict, payload: bytes) -> list[dict]:
    if len(payload) > 16 * 1024 ** 2 or hashlib.sha256(payload).hexdigest() != plan["bundle_sha256"]:
        raise ValueError("Flux export differs from the reviewed release artifact")
    objects = loads_strict_yaml_all(payload.decode("utf-8"))
    identities = set()
    deployments = set()
    crds = set()
    namespaces = []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("kind") not in KINDS or not isinstance(obj.get("metadata"), dict):
            raise ValueError("Unexpected resource in the Flux export")
        kind, meta = obj["kind"], obj["metadata"]
        name = meta.get("name")
        if not isinstance(name, str) or not dns_name(name) and not label(name):
            raise ValueError("Unnamed or invalid export resource")
        if kind not in CLUSTER_KINDS and meta.get("namespace") != plan["namespace"]:
            raise ValueError("Flux export namespace does not match the plan")
        key = (obj.get("apiVersion"), kind, meta.get("namespace"), name)
        if key in identities:
            raise ValueError("Duplicate export resource")
        identities.add(key)
        if kind == "Namespace":
            namespaces.append(name)
        if kind == "CustomResourceDefinition":
            group = obj.get("spec", {}).get("group", "")
            if not isinstance(group, str) or not group.endswith(".toolkit.fluxcd.io"):
                raise ValueError("The export contains an unrelated CRD")
            crds.add(name)
        if kind == "Deployment":
            if name not in plan["images"]:
                raise ValueError("Every exported controller must be explicitly selected and pinned")
            deployments.add(name)
        # Namespace references in RBAC must agree with the selected export.
        for subject in obj.get("subjects", []):
            if subject.get("kind") == "ServiceAccount" and subject.get("namespace") != plan["namespace"]:
                raise ValueError("Cross-namespace controller identity in export")
    if namespaces != [plan["namespace"]] or deployments != set(plan["images"]):
        raise ValueError("Flux namespace or controller inventory is incomplete")
    if not {"gitrepositories.source.toolkit.fluxcd.io", "kustomizations.kustomize.toolkit.fluxcd.io"} <= crds:
        raise ValueError("Required Git source and reconciliation CRDs are missing")
    return objects


def source_serving(plan: dict, pod: dict, manager: dict, config_name: str) -> None:
    args = manager["args"]
    if "--storage-path=/data" not in args or not any(arg.startswith("--storage-adv-addr=source-controller.") for arg in args):
        raise ValueError("Unsupported source-controller storage layout")
    manager["args"] = [arg for arg in args if not arg.startswith("--storage-addr=")] + ["--storage-addr=127.0.0.1:9091"]
    ports = manager.get("ports", [])
    if not any(port.get("name") == "http" and port.get("containerPort") == 9090 for port in ports):
        raise ValueError("Unsupported source-controller artifact port")
    for port in ports:
        if port.get("name") == "http":
            port.update(name="artifact-local", containerPort=9091)
    manager["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": "healthz"}, "periodSeconds": 5}
    volumes = pod.get("volumes", [])
    if len([volume for volume in volumes if volume.get("name") == "data" and "emptyDir" in volume]) != 1:
        raise ValueError("Unsupported source-controller artifact volume")
    for index, volume in enumerate(volumes):
        if volume.get("name") == "data":
            volumes[index] = {"name": "data", "persistentVolumeClaim": {"claimName": plan["artifact_claim"]}}
    if not any(mount.get("name") == "data" and mount.get("mountPath") == "/data" for mount in manager.get("volumeMounts", [])):
        raise ValueError("Unsupported source-controller artifact mount")
    pod["automountServiceAccountToken"] = False
    volumes += [{"name": "artifact-config", "configMap": {"name": config_name}},
        {"name": "artifact-tmp", "emptyDir": {"sizeLimit": "256Mi"}},
        {"name": "platform-api", "projected": {"defaultMode": 0o440, "sources": [
            {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
            {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
            {"downwardAPI": {"items": [{"path": "namespace", "fieldRef": {"fieldPath": "metadata.namespace"}}]}},
        ]}}]
    manager["volumeMounts"].append({"name": "platform-api", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount", "readOnly": True})
    pod["securityContext"] = {**pod.get("securityContext", {}), "runAsNonRoot": True, "runAsUser": 1337, "runAsGroup": 1337, "fsGroup": 1337}
    pod["containers"].append({"name": "artifact-server", "image": plan["artifact_server_image"],
        "command": ["nginx", "-c", "/etc/platform-artifacts/nginx.conf", "-g", "daemon off;"],
        "ports": [{"name": "http", "containerPort": 9090}],
        "securityContext": {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}},
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 5},
        "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True},
                         {"name": "artifact-config", "mountPath": "/etc/platform-artifacts", "readOnly": True},
                         {"name": "artifact-tmp", "mountPath": "/tmp"}],
    })


def render(plan: dict, payload: bytes, runtime: str) -> dict[str, str]:
    validate(plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown application runtime")
    objects = copy.deepcopy(export_objects(plan, payload))
    config_name = "flux-artifact-server-" + hashlib.sha256(NGINX.encode()).hexdigest()[:12]
    objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": plan["namespace"]},
                    "immutable": True, "data": {"nginx.conf": NGINX}})
    crds = []
    for obj in list(objects):
        kind, meta = obj["kind"], obj["metadata"]
        meta.setdefault("labels", {})[OWNER_LABEL] = plan["namespace"]
        if kind == "CustomResourceDefinition":
            crds.append(meta["name"])
        if kind != "Deployment":
            continue
        name, spec = meta["name"], obj["spec"]
        pod = spec["template"]["spec"]
        if len(pod.get("containers", [])) != 1 or pod.get("initContainers") or pod.get("hostNetwork"):
            raise ValueError("Unsupported controller pod layout")
        manager = pod["containers"][0]
        if "--enable-leader-election" not in manager.get("args", []):
            raise ValueError("Controller export must explicitly enable leader election")
        manager["image"] = plan["images"][name]
        spec.update(replicas=2, strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}})
        pod["nodeSelector"] = plan["node_selector"]
        pod.setdefault("affinity", {})["podAntiAffinity"] = {"requiredDuringSchedulingIgnoredDuringExecution": [
            {"labelSelector": {"matchLabels": copy.deepcopy(spec["selector"]["matchLabels"])}, "topologyKey": plan["failure_domain_key"]}]}
        if name == "source-controller":
            source_serving(plan, pod, manager, config_name)
        if plan.get("offline_images", False):
            for container in pod["containers"]:
                container["imagePullPolicy"] = "Never"
        objects.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {
            "name": name, "namespace": plan["namespace"], "labels": {OWNER_LABEL: plan["namespace"]}},
            "spec": {"minAvailable": 1, "selector": copy.deepcopy(spec["selector"])}})
    source_service = [obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "source-controller"]
    if len(source_service) != 1 or not any(port.get("targetPort") == "http" for port in source_service[0]["spec"]["ports"]):
        raise ValueError("Missing source artifact Service or incompatible target port")
    sync = plan["sync"]
    metadata = {"name": sync["name"], "namespace": sync["namespace"], "labels": {OWNER_LABEL: plan["namespace"]}}
    git_ref = {"branch": sync["branch"]}
    if "commit" in sync:
        git_ref["commit"] = sync["commit"]
    sync_objects = [
        {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository", "metadata": metadata,
         "spec": {"interval": "1m", "url": sync["url"], "ref": git_ref, "secretRef": {"name": sync["read_secret"]}}},
        {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization", "metadata": copy.deepcopy(metadata),
         "spec": {"interval": "5m", "retryInterval": "1m", "timeout": "5m", "wait": True, "force": False,
                  "path": sync["path"], "prune": sync["prune"], "serviceAccountName": sync["service_account"],
                  "sourceRef": {"kind": "GitRepository", "name": sync["name"]}}},
    ]
    files = {"flux-components.yaml": yaml.safe_dump_all(objects, sort_keys=False), "flux-sync.yaml": yaml.safe_dump_all(sync_objects, sort_keys=False)}
    inventory = [{"apiVersion": obj["apiVersion"], "kind": obj["kind"], "name": obj["metadata"]["name"], "namespace": obj["metadata"].get("namespace")}
                 for obj in objects + sync_objects]
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan, "crds": sorted(crds), "inventory": inventory,
        "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
    files["deployment-contract.json"] = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    files["render-status.json"] = json.dumps({"component": "flux-management-target", "runtime": runtime, "rendered": True,
        "deployed": False, "reconciliation_verified": False, "ha_verified": False,
        "contract_sha256": hashlib.sha256(files["deployment-contract.json"].encode()).hexdigest(),
        "remaining": ["selected-release-image-provenance-and-runtime-acceptance", "rwx-storage-health-and-artifact-permissions",
                      "scoped-reconciliation-rbac-and-private-git-trust", "network-isolation-and-transport-encryption",
                      "leader-loss-artifact-read-upgrade-and-restore-acceptance"]}, indent=2) + "\n"
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
        print("Flux render stopped; inspect private target, image pins, reviewed export and new output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False, "ha_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
