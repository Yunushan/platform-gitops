#!/usr/bin/env python3
"""Render guarded Zuul core services and native executor units, without applying."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
import render_zuul_config as config
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CORE_ROLES = ("scheduler", "merger", "web")
CODE_FILES = ("start_zuul.py", "zuul_runtime_preflight.py", "atomic_file.py", "bounded_file.py", "bounded_subprocess.py")
SECRET_KEYS = {"ZUUL_DATABASE_URI": "database-uri", "ZUUL_KEYSTORE_PASSWORD": "keystore-password",
               "ZUUL_GERRIT_HTTP_PASSWORD": "gerrit-http-password"}


def command(role: str, version: str) -> list[str]:
    return ["python3", "/etc/zuul/start_zuul.py", "--role", role, "--start", "--expected-version", version]


def service_roles(plan: dict) -> tuple[str, ...]:
    return CORE_ROLES + (("launcher",) if plan.get("integrated_launcher", {}).get("enabled", False) else ())


def bundle(plan: dict) -> dict[str, str]:
    files = {"zuul.conf": config.configuration(plan), "tenants.yaml": config.tenant_configuration(plan)}
    files.update({name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in CODE_FILES})
    if sum(len(text.encode()) for text in files.values()) > 400 * 1024:
        raise ValueError("Zuul runtime bundle exceeds its payload limit")
    return files


def kubernetes(plan: dict, files: dict[str, str]) -> str:
    namespace = plan["namespace"]
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    name = "zuul-runtime-" + revision
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace},
                "immutable": True, "data": files}]
    for role in service_roles(plan):
        labels = {"app.kubernetes.io/name": "zuul", "app.kubernetes.io/component": role}
        meta = {"name": "zuul-" + role, "namespace": namespace, "labels": labels}
        container = {"name": role, "image": plan["image"], "command": command(role, plan["zuul_version"]),
            "env": [{"name": key, "valueFrom": {"secretKeyRef": {"name": f"zuul-{role}-runtime", "key": value}}}
                    for key, value in SECRET_KEYS.items()] + [
                        {"name": "REQUESTS_CA_BUNDLE", "value": "/etc/zuul/private/gerrit-ca.pem"},
                        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
            "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "1Gi"},
                          "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "22Gi"}},
            "volumeMounts": [{"name": "config", "mountPath": "/etc/zuul", "readOnly": True},
                             {"name": "private", "mountPath": "/etc/zuul/private", "readOnly": True},
                             {"name": "state", "mountPath": "/var/lib/zuul"}, {"name": "tmp", "mountPath": "/tmp"}]}
        if role == "web":
            container["ports"] = [{"name": "http", "containerPort": 9000}]
            container["readinessProbe"] = {"tcpSocket": {"port": "http"}, "periodSeconds": 5}
        else:
            container["readinessProbe"] = {"exec": {"command": ["python3", "-c",
                f"import os,stat,sys; p='/var/lib/zuul/{role}.socket'; sys.exit(0 if os.path.exists(p) and stat.S_ISSOCK(os.stat(p).st_mode) else 1)"]}, "periodSeconds": 5}
        objects += [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {
            "replicas": 2, "selector": {"matchLabels": labels},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
            "template": {"metadata": {"labels": labels}, "spec": {
                "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 120,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                    {"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}]}},
                "containers": [container], "volumes": [
                    {"name": "config", "configMap": {"name": name}},
                    {"name": "private", "secret": {"secretName": f"zuul-{role}-files", "defaultMode": 0o440,
                        "items": [{"key": filename, "path": filename} for filename in config.PRIVATE_FILES]}},
                    {"name": "state", "emptyDir": {"sizeLimit": "20Gi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                ],
            }}}},
            {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta,
             "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}}]
        if role == "web":
            objects.append({"apiVersion": "v1", "kind": "Service", "metadata": meta,
                            "spec": {"type": "ClusterIP", "selector": labels,
                                     "ports": [{"name": "http", "port": 9000, "targetPort": "http"}]}})
    return yaml.safe_dump_all(objects, sort_keys=False)


def docker(plan: dict) -> str:
    services = {}
    for role in service_roles(plan):
        mounts = [{"type": "bind", "source": source, "target": target, "read_only": read_only,
                   "bind": {"create_host_path": False}} for source, target, read_only in (
            (".", "/etc/zuul", True), (f"./private/{role}", "/etc/zuul/private", True),
            (f"./state/{role}", "/var/lib/zuul", False))]
        service = {"image": plan["image"], "user": "1000:1000", "read_only": True,
            "entrypoint": command(role, plan["zuul_version"]), "command": [], "restart": "unless-stopped",
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "environment": {key: "${" + key + ":?Supply the private runtime value}" for key in SECRET_KEYS} | {
                "REQUESTS_CA_BUNDLE": "/etc/zuul/private/gerrit-ca.pem", "PYTHONDONTWRITEBYTECODE": "1"},
            "volumes": mounts, "tmpfs": ["/tmp:rw,nosuid,nodev,size=1g,mode=1777"], "mem_limit": "2g", "cpus": 2.0}
        if role == "web":
            service["ports"] = ["127.0.0.1:9000:9000"]
        services["zuul-" + role] = service
    return yaml.safe_dump({"services": services}, sort_keys=False)


def kubernetes_executor(plan: dict, files: dict[str, str]) -> str:
    executor = plan["executor"]
    namespace = plan["namespace"]
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    config_name = "zuul-executor-config-" + revision
    labels = {"app.kubernetes.io/name": "zuul", "app.kubernetes.io/component": "executor"}
    meta = {"name": "zuul-executor", "namespace": namespace, "labels": labels}
    security = {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    if executor["kubernetes_seccomp"] != "runtime-default":
        security["seccompProfile"] = {"type": "Localhost", "localhostProfile": executor["kubernetes_seccomp"]}
    if executor["kubernetes_apparmor"] != "runtime-default":
        security["appArmorProfile"] = {"type": "Localhost", "localhostProfile": executor["kubernetes_apparmor"]}
    env = [{"name": key, "valueFrom": {"secretKeyRef": {"name": "zuul-executor-runtime", "key": value}}}
           for key, value in SECRET_KEYS.items()]
    env += [{"name": "ZUUL_EXECUTOR_HOSTNAME", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
            {"name": "REQUESTS_CA_BUNDLE", "value": "/etc/zuul/private/gerrit-ca.pem"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
    objects = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": namespace},
         "immutable": True, "data": files},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {
            "replicas": executor["replicas"], "selector": {"matchLabels": labels},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
            "template": {"metadata": {"labels": labels}, "spec": {
                "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 600,
                "nodeSelector": {"platform.gitops/ci-executor": "true"},
                "tolerations": [{"key": "platform.gitops/ci-executor", "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {
                    "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
                        {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"},
                        {"key": "node-role.kubernetes.io/master", "operator": "DoesNotExist"}]}]}},
                    "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": labels},
                                                                                             "topologyKey": "kubernetes.io/hostname"}]}},
                "containers": [{"name": "executor", "image": executor["image"],
                    "command": command("executor", plan["zuul_version"]), "env": env, "securityContext": security,
                    "ports": [{"name": "finger", "containerPort": 7900}],
                    "resources": {"requests": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "10Gi"},
                                  "limits": {"cpu": "4", "memory": "4Gi", "ephemeral-storage": "66Gi"}},
                    "readinessProbe": {"exec": {"command": ["python3", "-c",
                        "import os,stat,sys; p='/var/lib/zuul/executor.socket'; sys.exit(0 if os.path.exists(p) and stat.S_ISSOCK(os.stat(p).st_mode) else 1)"]}, "periodSeconds": 5},
                    "volumeMounts": [{"name": "config", "mountPath": "/etc/zuul", "readOnly": True},
                                     {"name": "private", "mountPath": "/etc/zuul/private", "readOnly": True},
                                     {"name": "state", "mountPath": "/var/lib/zuul"}, {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "config", "configMap": {"name": config_name}},
                            {"name": "private", "secret": {"secretName": "zuul-executor-files", "defaultMode": 0o440,
                                "items": [{"key": f, "path": f} for f in (*config.PRIVATE_FILES, "build-ssh.key")]}},
                            {"name": "state", "emptyDir": {"sizeLimit": "64Gi"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
            }}}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta,
         "spec": {"minAvailable": executor["replicas"] - 1, "selector": {"matchLabels": labels}}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta,
         "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress"], "ingress": [
             {"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "zuul", "app.kubernetes.io/component": "web"}}}],
              "ports": [{"protocol": "TCP", "port": 7900}]}]}},
    ]
    return yaml.safe_dump_all(objects, sort_keys=False)


def docker_executors(plan: dict) -> str:
    executor = plan["executor"]
    services = {}
    for index in range(1, executor["replicas"] + 1):
        name = "zuul-executor-" + str(index)
        security = ["no-new-privileges:true"]
        if executor["docker_seccomp"] != "runtime-default":
            security.append("seccomp=" + executor["docker_seccomp"])
        if executor["docker_apparmor"] != "runtime-default":
            security.append("apparmor=" + executor["docker_apparmor"])
        env = {key: "${" + key + ":?Supply the private runtime value}" for key in SECRET_KEYS}
        env.update({"ZUUL_EXECUTOR_HOSTNAME": "${ZUUL_EXECUTOR_HOSTNAME_" + str(index) + ":-" + name + "}",
                    "REQUESTS_CA_BUNDLE": "/etc/zuul/private/gerrit-ca.pem", "PYTHONDONTWRITEBYTECODE": "1"})
        services[name] = {"image": executor["image"], "hostname": name, "user": "1000:1000",
            "entrypoint": command("executor", plan["zuul_version"]), "command": [], "restart": "unless-stopped",
            "read_only": True, "cap_drop": ["ALL"], "security_opt": security, "environment": env,
            "expose": ["7900"], "mem_limit": "4g", "cpus": 4.0, "stop_grace_period": "10m",
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=1g,mode=1777"],
            "volumes": [{"type": "bind", "source": source, "target": target, "read_only": read_only,
                         "bind": {"create_host_path": False}} for source, target, read_only in (
                (".", "/etc/zuul", True), (f"./private/{name}", "/etc/zuul/private", True),
                (f"./state/{name}", "/var/lib/zuul", False))]}
    return yaml.safe_dump({"services": services}, sort_keys=False)


def native_unit(role: str, version: str) -> str:
    return f"""[Unit]
Description=Guarded Zuul {role}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zuul
Group=zuul
EnvironmentFile=/etc/zuul/{role}.env
Environment=PATH=/opt/zuul/bin:/usr/bin:/bin
Environment=REQUESTS_CA_BUNDLE=/etc/zuul/private/gerrit-ca.pem
Environment=PYTHONDONTWRITEBYTECODE=1
StateDirectory=zuul
StateDirectoryMode=0700
WorkingDirectory=/var/lib/zuul
ExecStart=/opt/zuul/bin/python3 /etc/zuul/start_zuul.py --role {role} --start --expected-version {version}
Restart=on-failure
RestartSec=10
TimeoutStopSec=600
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/zuul
CapabilityBoundingSet=
UMask=0077
MemoryMax=4G
CPUQuota=400%
TasksMax=4096

[Install]
WantedBy=multi-user.target
"""


def render(plan: dict, runtime: str) -> dict[str, str]:
    config.validate(plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unsupported Zuul service runtime")
    files = bundle(plan)
    executor = plan.get("executor", {}).get("enabled", False)
    if runtime in {"rke2", "k3s", "k8s"}:
        executor_files = {"zuul-executors.yaml": kubernetes_executor(plan, files)} if executor else {}
        files = {"zuul-services.yaml": kubernetes(plan, files), **executor_files}
    elif runtime == "docker":
        files["zuul-compose.yaml"] = docker(plan)
        if executor:
            files["zuul-executors-compose.yaml"] = docker_executors(plan)
    else:
        for role in (*service_roles(plan), "executor"):
            files[f"zuul-{role}.service"] = native_unit(role, plan["zuul_version"])
    files["render-status.json"] = json.dumps({"component": "zuul-service-packaging", "runtime": runtime,
        "core_roles": list(CORE_ROLES), "integrated_launcher_selected": "launcher" in service_roles(plan), "executor_packaging": "guarded-optional-containers-and-native",
        "container_executor_selected": executor and runtime != "raw",
        "rendered": True, "deployed": False, "ci_verified": False, "ha_verified": False,
        "remaining": ["container-executor-isolation-acceptance", "node-supply", "trusted-pipelines", "zookeeper-and-database",
                      "web-sso-and-gateway", "event-recovery", "live-job-node-loss-and-restore-acceptance"]}, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Zuul service render failed; review the private plan and new private output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False, "ci_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
