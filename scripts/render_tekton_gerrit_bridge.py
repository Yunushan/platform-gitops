#!/usr/bin/env python3
"""Package current Gerrit patch-set discovery and fixed Tekton submission."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import write_bundle
import render_tekton_forge_bridge as pipeline
from strict_json import loads_strict_json
import tekton_gerrit_bridge as bridge


ROOT = Path(__file__).resolve().parents[1]


def run_template(plan, checkout):
    if plan["schema_version"] == 2:
        from gerrit_artifact_contract import run_template as artifact_template
        return artifact_template(plan)
    result = pipeline.run_template(plan, checkout)
    params = [{"name": "revision", "type": "string"}, {"name": "patchset-ref", "type": "string"}]
    result["spec"]["pipelineSpec"]["params"] = params
    task = result["spec"]["pipelineSpec"]["tasks"][0]
    task["params"].append({"name": "patchset-ref", "value": "$(params.patchset-ref)"})
    task["taskSpec"]["params"] = params
    task["taskSpec"]["steps"][0]["args"].append("$(params.patchset-ref)")
    return result


def render(plan, runtime):
    bridge.validate(plan)
    if runtime not in bridge.RUNTIMES: raise ValueError("Select an explicit application-hosting runtime")
    files = {name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in bridge.programs(plan)}
    files["run-template.json"] = bridge.common.canonical(run_template(plan, files["tekton_checkout.py"])).decode() + "\n"
    contract = {"schema_version": 1, "runtime": runtime, "plan": plan,
                "files_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}}
    files["gerrit-event-contract.json"] = bridge.common.canonical(contract).decode() + "\n"
    digest = hashlib.sha256(files["gerrit-event-contract.json"].encode()).hexdigest()
    files["bridge-rbac.yaml"] = yaml.safe_dump_all(pipeline.rbac(plan), sort_keys=False)
    base = "/opt/platform/gerrit-tekton"
    command = ["python3", "-B", base + "/tekton_gerrit_bridge.py", "--bundle-dir", base, "--contract-sha256", digest, "--watch"]
    environment = {plan["management_target"]["kubeconfig_env"]: str(bridge.INPUTS / "kubeconfig")}
    if runtime in {"rke2", "k3s", "k8s"}:
        name, namespace = plan["name"], plan["submitter_namespace"]
        labels = {"app.kubernetes.io/name": name}
        config = name + "-" + digest[:12]
        objects = [
            {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True, "metadata": {"name": config, "namespace": namespace},
             "data": {k: v for k, v in files.items() if k in {*bridge.programs(plan), "run-template.json", "gerrit-event-contract.json"}}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace}, "spec": {
                "replicas": 2, "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                "selector": {"matchLabels": labels.copy()}, "template": {"metadata": {"labels": labels.copy()}, "spec": {
                    "serviceAccountName": plan["submitter_account"], "automountServiceAccountToken": False,
                    "nodeSelector": plan["controller_selector"],
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                    "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": labels}}]}},
                    "containers": [{"name": "discovery", "image": plan["bridge_image"], "command": command,
                        "env": [{"name": k, "value": v} for k, v in environment.items()],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                        "volumeMounts": [{"name": "program", "mountPath": base, "readOnly": True}, {"name": "inputs", "mountPath": str(bridge.INPUTS), "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}]}],
                    "volumes": [{"name": "program", "configMap": {"name": config}}, {"name": "inputs", "secret": {"secretName": plan["bridge_secret"], "defaultMode": 0o440,
                        "items": [{"key": k, "path": k} for k in ("api-token", "ca.crt", "kubeconfig")]}}, {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}}},
            {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": name, "namespace": namespace}, "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}}]
        files["bridge-workload.yaml"] = yaml.safe_dump_all(objects, sort_keys=False)
    elif runtime == "docker":
        files["compose.yaml"] = yaml.safe_dump({"services": {plan["name"]: {
            "image": plan["bridge_image"], "command": command, "environment": environment,
            "user": "1000:1000", "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m,uid=1000,gid=1000"], "restart": "unless-stopped",
            "volumes": [{"type": "bind", "source": "${PLATFORM_GERRIT_TEKTON_BUNDLE:?Set the exact reviewed package}", "target": base, "read_only": True},
                {"type": "bind", "source": "${PLATFORM_GERRIT_TEKTON_INPUTS:?Set the private bridge inputs}", "target": str(bridge.INPUTS), "read_only": True}],
            "mem_limit": "512m", "cpus": "1", "pids_limit": 64}}}, sort_keys=False)
    else:
        files["platform-gerrit-tekton.service"] = "\n".join([
            "[Unit]", "Description=Current Gerrit patch sets to fixed Tekton jobs", "After=network-online.target", "Wants=network-online.target", "",
            "[Service]", "Type=simple", "User=platform-ci-bridge", "Group=platform-ci-bridge", "UMask=0077",
            "Environment=" + next(iter(environment)) + "=" + next(iter(environment.values())),
            "ExecStart=/usr/bin/" + " ".join(command), "Restart=on-failure", "RestartSec=10",
            "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
            "RestrictSUIDSGID=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "MemoryMax=512M", "TasksMax=64", "",
            "[Install]", "WantedBy=multi-user.target", ""])
    files["render-status.json"] = json.dumps({"rendered": True, "runtime": runtime, "contract_sha256": digest,
        "current_patchset_polling_implemented": True, "manual_patchset_event_submission_implemented": True,
        "remaining": ["live-Gerrit-and-Tekton-controller-worker-acceptance", "guarded-workload-installation", "verified-result-votes", "CI-to-CD-handoff", "failure-load-upgrade-and-restore"],
        **{k: False for k in bridge.FALSE_FLAGS}}, sort_keys=True, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--runtime", choices=bridge.RUNTIMES, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, **{k: False for k in bridge.FALSE_FLAGS}}, sort_keys=True)); return 0
    except Exception:
        print("Gerrit Tekton package failed; private inputs and details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
