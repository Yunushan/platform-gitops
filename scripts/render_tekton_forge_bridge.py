#!/usr/bin/env python3
"""Package a reviewed Forgejo/Gitea push-to-Tekton application pipeline."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
from strict_json import loads_strict_json
from tekton_forge_bridge import canonical, programs, reporting_settings, validate


ROOT = Path(__file__).resolve().parents[1]
def run_template(plan: dict, checkout: str) -> dict:
    security = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
    resources = {"requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"},
                 "limits": {"cpu": "2", "memory": "1Gi", "ephemeral-storage": "3Gi"}}
    steps = [
        {"name": "checkout", "image": plan["checkout_image"], "command": ["python3", "-I", "-B", "-c", checkout],
         "args": [plan["source"]["clone_url"], "$(params.revision)", "/workspace/repository", plan["source"]["read_username"]],
         "env": [{"name": "TMPDIR", "value": "/tmp"}],
         "volumeMounts": [{"name": "source", "mountPath": "/run/source", "readOnly": True},
                          {"name": "workspace", "mountPath": "/workspace/repository"}, {"name": "checkout-tmp", "mountPath": "/tmp"}]},
        {"name": "build", "image": plan["build_image"], "command": plan["build_argv"], "workingDir": "/workspace/repository",
         "env": [{"name": "HOME", "value": "/tmp"}, {"name": "TMPDIR", "value": "/tmp"},
                 {"name": "GIT_CONFIG_COUNT", "value": "1"}, {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
                 {"name": "GIT_CONFIG_VALUE_0", "value": "/workspace/repository"}],
         "volumeMounts": [{"name": "workspace", "mountPath": "/workspace/repository"}, {"name": "build-tmp", "mountPath": "/tmp"}]},
    ]
    for step in steps:
        step.update(securityContext=security, computeResources=resources)
    # Tekton's EmbeddedTask serializes its zero-value metadata and custom-task
    # RawExtension fields even for a regular inline TaskSpec. Account for those
    # exact empty values in the reviewed spec rather than accepting mutations.
    task = {"metadata": {}, "spec": None, "params": [{"name": "revision", "type": "string"}], "steps": steps,
            "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "2Gi"}},
                        {"name": "checkout-tmp", "emptyDir": {"sizeLimit": "64Mi"}}, {"name": "build-tmp", "emptyDir": {"sizeLimit": "256Mi"}},
                        {"name": "source", "secret": {"secretName": plan["read_secret"], "defaultMode": 0o440,
                            "items": [{"key": "token", "path": "token"}, {"key": "ca.crt", "path": "ca.crt"}]}}]}
    return {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun", "metadata": {"namespace": plan["namespace"]}, "spec": {
        "pipelineSpec": {"params": [{"name": "revision", "type": "string"}],
                         "tasks": [{"name": "build", "params": [{"name": "revision", "value": "$(params.revision)"}], "taskSpec": task}]},
        "timeouts": {"pipeline": ("1h0m0s" if plan["timeout_minutes"] == 60 else str(plan["timeout_minutes"]) + "m0s")},
        "taskRunTemplate": {"serviceAccountName": plan["service_account"], "podTemplate": {
            "nodeSelector": plan["node_selector"], "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"},
                {"key": "node-role.kubernetes.io/master", "operator": "DoesNotExist"}]}]}}}}}}}


def rbac(plan: dict) -> list[dict]:
    name, jobs, control = plan["name"], plan["namespace"], plan["submitter_namespace"]
    subject = {"kind": "ServiceAccount", "name": plan["submitter_account"], "namespace": control}
    def metadata(namespace):
        return {"name": name, **({"namespace": namespace} if namespace else {})}
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": plan["service_account"], "namespace": jobs}, "automountServiceAccountToken": False},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": plan["submitter_account"], "namespace": control}, "automountServiceAccountToken": False},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": metadata(jobs), "rules": [
            {"apiGroups": ["tekton.dev"], "resources": ["pipelineruns"], "verbs": ["get", "create"]},
            {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "create"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": metadata(jobs), "subjects": [subject],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": metadata(None), "rules": [
            {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system", jobs], "verbs": ["get"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": metadata(None), "subjects": [subject],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": name}},
    ]


def reporter_rbac(plan: dict) -> list[dict]:
    settings = reporting_settings(plan)
    objects = copy.deepcopy(rbac(plan))
    objects = [obj for obj in objects if not (obj["kind"] == "ServiceAccount" and obj["metadata"]["namespace"] == plan["namespace"])]
    name = plan["name"] + "-reporter"
    for obj in objects:
        obj["metadata"]["name"] = settings["account"] if obj["kind"] == "ServiceAccount" else name
        for subject in obj.get("subjects", []):
            subject["name"] = settings["account"]
        if "roleRef" in obj:
            obj["roleRef"]["name"] = name
        if obj["kind"] == "Role":
            obj["rules"] = [
                {"apiGroups": ["tekton.dev"], "resources": ["pipelineruns"], "verbs": ["get", "list"]},
                {"apiGroups": ["tekton.dev"], "resources": ["taskruns"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "create"]}]
        if obj["kind"] == "ClusterRole":
            obj["rules"].append({"apiGroups": [""], "resources": ["nodes"], "verbs": ["get"]})
    return objects


def render(plan: dict, runtime: str) -> dict[str, str]:
    validate(plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown application-hosting runtime")
    helpers = programs(plan)
    files = {name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in helpers}
    if plan["schema_version"] == 2:
        from gerrit_artifact_contract import run_template as publication_template
        template = publication_template(plan)
    else:
        template = run_template(plan, files["tekton_checkout.py"])
    files["run-template.json"] = canonical(template).decode() + "\n"
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan,
                "files_sha256": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}}
    files["event-contract.json"] = canonical(contract).decode() + "\n"
    digest = hashlib.sha256(files["event-contract.json"].encode()).hexdigest()
    files["bridge-rbac.yaml"] = yaml.safe_dump_all(rbac(plan), sort_keys=False)
    reporting = reporting_settings(plan)
    if reporting["enabled"]:
        files["reporter-rbac.yaml"] = yaml.safe_dump_all(reporter_rbac(plan), sort_keys=False)
    name = plan["name"]
    base = "/opt/platform/bridge"
    command = ["python3", "-B", base + "/tekton_forge_bridge.py", "--bundle-dir", base, "--contract-sha256", digest, "--serve"]
    if runtime in {"rke2", "k3s", "k8s"}:
        namespace = plan["submitter_namespace"]
        labels = {"app.kubernetes.io/name": name}
        meta = {"name": name, "namespace": namespace}
        data = {key: value for key, value in files.items() if key in {*helpers, "run-template.json", "event-contract.json"}}
        objects = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {**meta, "name": name + "-" + digest[:12]}, "immutable": True, "data": data},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {"replicas": 2,
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
                    "serviceAccountName": plan["submitter_account"], "automountServiceAccountToken": False,
                    "nodeSelector": plan["controller_selector"], "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                    "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                        "labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}]}},
                    "containers": [{"name": "receiver", "image": plan["bridge_image"], "command": command,
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
                        "ports": [{"name": "https", "containerPort": 8443}], "readinessProbe": {"httpGet": {"scheme": "HTTPS", "path": "/healthz", "port": "https"}, "timeoutSeconds": 3},
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                        "volumeMounts": [{"name": "program", "mountPath": base, "readOnly": True},
                            {"name": "inputs", "mountPath": "/run/platform/bridge", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}]}],
                    "volumes": [{"name": "program", "configMap": {"name": name + "-" + digest[:12]}},
                        {"name": "inputs", "secret": {"secretName": plan["bridge_secret"], "defaultMode": 0o440}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": meta, "spec": {"selector": labels, "ports": [{"name": "https", "port": 8443, "targetPort": "https"}]}},
            {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta, "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}},
        ]
        files["bridge-workload.yaml"] = yaml.safe_dump_all(objects, sort_keys=False)
        if reporting["enabled"]:
            reporter = copy.deepcopy(objects[1])
            reporter["metadata"]["name"] = name + "-reporter"
            pod = reporter["spec"]["template"]
            labels = {"app.kubernetes.io/name": name + "-reporter"}
            reporter["spec"]["selector"]["matchLabels"] = labels
            pod["metadata"]["labels"] = labels
            pod["spec"]["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["labelSelector"]["matchLabels"] = labels
            pod["spec"]["serviceAccountName"] = reporting["account"]
            container = pod["spec"]["containers"][0]
            container["name"] = "reporter"
            container["command"] = ["python3", "-B", base + "/tekton_forge_reporter.py", "--bundle-dir", base, "--contract-sha256", digest, "--watch"]
            container.pop("ports")
            container.pop("readinessProbe")
            next(v for v in container["volumeMounts"] if v["name"] == "inputs")["mountPath"] = "/run/platform/reporter"
            next(v for v in pod["spec"]["volumes"] if v["name"] == "inputs")["secret"]["secretName"] = reporting["secret"]
            budget = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": name + "-reporter", "namespace": namespace},
                      "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}}
            files["reporter-workload.yaml"] = yaml.safe_dump_all([objects[0], reporter, budget], sort_keys=False)
    elif runtime == "docker":
        compose = {"services": {name: {"image": plan["bridge_image"], "command": command,
            "user": "1000:1000", "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m,uid=1000,gid=1000"], "restart": "unless-stopped",
            "ports": [plan["publish_address"] + ":8443:8443"],
            "volumes": [{"type": "bind", "source": "${PLATFORM_TEKTON_BRIDGE_BUNDLE:?Set the reviewed bundle path}", "target": base, "read_only": True},
                        {"type": "bind", "source": "${PLATFORM_TEKTON_BRIDGE_INPUTS:?Set the private inputs path}", "target": "/run/platform/bridge", "read_only": True}],
            "mem_limit": "512m", "cpus": "1", "pids_limit": 64}}}
        if reporting["enabled"]:
            service = copy.deepcopy(compose["services"][name])
            service["command"] = ["python3", "-B", base + "/tekton_forge_reporter.py", "--bundle-dir", base, "--contract-sha256", digest, "--watch"]
            service.pop("ports")
            service["volumes"][1].update(source="${PLATFORM_TEKTON_REPORTER_INPUTS:?Set the separate private reporter inputs}", target="/run/platform/reporter")
            compose["services"][name + "-reporter"] = service
        files["compose.yaml"] = yaml.safe_dump(compose, sort_keys=False)
    else:
        native = "/etc/platform/tekton-bridge"
        files["platform-tekton-bridge.service"] = "\n".join([
            "[Unit]", "Description=Authenticated forge push to fixed Tekton job", "After=network-online.target", "Wants=network-online.target", "",
            "[Service]", "Type=simple", "User=platform-ci-bridge", "Group=platform-ci-bridge", "UMask=0077",
            "ExecStart=/usr/bin/python3 -B " + native + "/tekton_forge_bridge.py --bundle-dir " + native + " --contract-sha256 " + digest + " --serve",
            "Restart=on-failure", "RestartSec=5", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
            "RestrictSUIDSGID=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "MemoryMax=512M", "TasksMax=64", "",
            "[Install]", "WantedBy=multi-user.target", ""])
        if reporting["enabled"]:
            files["platform-tekton-reporter.service"] = files["platform-tekton-bridge.service"].replace(
                "Authenticated forge push to fixed Tekton job", "Verified terminal Tekton status reporter").replace(
                "platform-ci-bridge", "platform-ci-reporter").replace("tekton_forge_bridge.py", "tekton_forge_reporter.py").replace(" --serve", " --watch")
    files["render-status.json"] = json.dumps({"component": "forge-tekton-push-bridge", "runtime": runtime,
        "contract_sha256": digest, "rendered": True, "deployed": False, "event_integration_verified": False,
        "application_ci_verified": False, "ha_verified": False, "result_reporting_enabled": reporting["enabled"], "result_reporting_verified": False,
        "remaining": ["private-TLS-source-and-submit-credentials", "live-forge-push-checkout-build-and-result-report",
                      "worker-network-tenant-and-artifact-boundaries", "gerrit-review-events-and-reporting", "argocd-handoff-upgrade-backup-and-restore"]}, indent=2) + "\n"
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, "deployed": False, "event_integration_verified": False}))
        return 0
    except Exception:
        print("Forge Tekton bridge render stopped; inspect the private plan and new output directory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
