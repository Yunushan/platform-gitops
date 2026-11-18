#!/usr/bin/env python3
"""Render opt-in static Nodepool launcher packages; never apply or register."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
import render_nodepool_static as workers
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("start_nodepool.py", "bounded_file.py", "bounded_subprocess.py")


def command(version: str, digest: str) -> list[str]:
    return ["python3", "/etc/nodepool/start_nodepool.py", "--start", "--expected-version", version, "--config-sha256", digest]


def render(plan: dict, zuul_plan: dict, runtime: str) -> dict[str, str]:
    workers.validate(plan, zuul_plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown Nodepool runtime")
    files = {"nodepool.yaml": workers.configuration(plan, zuul_plan)}
    digest = hashlib.sha256(files["nodepool.yaml"].encode()).hexdigest()
    files.update({name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in MODULES})
    if sum(len(text.encode()) for text in files.values()) > 400 * 1024:
        raise ValueError("Static launcher configuration exceeds the payload limit")
    if runtime in {"rke2", "k3s", "k8s"}:
        files = {"nodepool-services.yaml": kubernetes(plan, files, digest)}
    elif runtime == "docker":
        files["nodepool-compose.yaml"] = yaml.safe_dump({"services": {"nodepool-launcher": {
            "image": plan["image"], "user": "1000:1000", "entrypoint": command(plan["nodepool_version"], digest),
            "command": [], "restart": "unless-stopped", "read_only": True, "stop_signal": "SIGINT", "stop_grace_period": "2m",
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "mem_limit": "2g", "cpus": 2.0,
            "environment": {"PYTHONDONTWRITEBYTECODE": "1"}, "tmpfs": ["/tmp:rw,nosuid,nodev,size=256m,mode=1777"],
            "volumes": [{"type": "bind", "source": source, "target": target, "read_only": readonly,
                         "bind": {"create_host_path": False}} for source, target, readonly in (
                (".", "/etc/nodepool", True), ("./private", "/etc/nodepool/private", True),
                ("./state", "/var/lib/nodepool", False))],
        }}}, sort_keys=False)
    else:
        files["nodepool-launcher.service"] = native_unit(plan["nodepool_version"], digest)
    files["render-status.json"] = json.dumps({"component": "nodepool-static-launcher", "runtime": runtime,
        "rendered": True, "deployed": False, "registered": False, "allocation_verified": False, "ha_verified": False,
        "config_sha256": digest, "remaining": ["pinned-release-pair-acceptance", "zookeeper-acls-and-connectivity",
            "duplicate-allocation-and-launcher-failover-tests", "worker-reset-and-job-isolation",
            "real-job-allocation-release-node-loss-and-restore-tests"]}, indent=2) + "\n"
    return files


def kubernetes(plan: dict, files: dict[str, str], digest: str) -> str:
    namespace = plan["namespace"]
    name = "nodepool-config-" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    labels = {"app.kubernetes.io/name": "nodepool", "app.kubernetes.io/component": "launcher"}
    meta = {"name": "nodepool-launcher", "namespace": namespace, "labels": labels}
    stop = "import os,signal,time\nos.kill(1,signal.SIGINT)\nfor _ in range(100):\n try: os.kill(1,0)\n except ProcessLookupError: break\n time.sleep(1)\n"
    objects = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace}, "immutable": True, "data": files},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {
            "replicas": 2, "selector": {"matchLabels": labels},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
            "template": {"metadata": {"labels": labels}, "spec": {
                "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 120,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                    {"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}]}},
                "containers": [{"name": "launcher", "image": plan["image"], "command": command(plan["nodepool_version"], digest),
                    "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                    "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
                    "lifecycle": {"preStop": {"exec": {"command": ["python3", "-c", stop]}}},
                    "volumeMounts": [{"name": "config", "mountPath": "/etc/nodepool", "readOnly": True},
                                     {"name": "private", "mountPath": "/etc/nodepool/private", "readOnly": True},
                                     {"name": "state", "mountPath": "/var/lib/nodepool"}, {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "config", "configMap": {"name": name}},
                            {"name": "private", "secret": {"secretName": "nodepool-zookeeper", "defaultMode": 0o440,
                                "items": [{"key": filename, "path": filename} for filename in workers.TLS_FILES]}},
                            {"name": "state", "emptyDir": {"sizeLimit": "1Gi"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}],
            }}}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta,
         "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}},
    ]
    return yaml.safe_dump_all(objects, sort_keys=False)


def native_unit(version: str, digest: str) -> str:
    return f"""[Unit]
Description=Guarded static Nodepool launcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nodepool
Group=nodepool
Environment=PATH=/opt/nodepool/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
StateDirectory=nodepool
StateDirectoryMode=0700
WorkingDirectory=/var/lib/nodepool
ExecStart=/opt/nodepool/bin/python3 /etc/nodepool/start_nodepool.py --start --expected-version {version} --config-sha256 {digest}
KillSignal=SIGINT
TimeoutStopSec=120
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/nodepool
CapabilityBoundingSet=
UMask=0077
MemoryMax=2G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--zuul-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        zuul_plan = loads_strict_json(read_bounded_text(args.zuul_plan, encoding="utf-8"))
        write_bundle(render(plan, zuul_plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Nodepool launcher render failed; inspect private source plans and new output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False, "registered": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
