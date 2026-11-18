#!/usr/bin/env python3
"""Render the Allura web application and task worker from prepared state."""

import argparse
import ipaddress
import re
import sys
from pathlib import Path

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, write_bundle
from render_gerrit_sites import label
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CLUSTER = {"rke2", "k3s", "k8s"}
PORT = 8080
INSTALL = "/opt/allura"
STATE = "/var/lib/allura"
CONFIG = "/run/allura/allura.ini"
WEB = "gunicorn"
TASK = "paster"
HEALTH_PATH = "/auth/"


def validate(plan, runtime):
    fields = {"schema_version", "name"}
    if runtime in CLUSTER:
        fields |= {"namespace", "state_claim", "config_secret", "image"}
    else:
        fields |= {"state_path", "config_dir", "http_port"}
        fields |= {"user", "group"} if runtime == "raw" else {"image"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict)
            or set(plan) - {"trusted_proxy_cidrs"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not label(plan["name"]) or len(plan["name"]) > 53):
        raise ValueError("Invalid Allura deployment plan")
    proxies = plan.get("trusted_proxy_cidrs", [])
    if not isinstance(proxies, list) or len(proxies) > 32:
        raise ValueError("Provide a list of trusted Allura proxy IPs or CIDRs")
    for proxy in proxies:
        if not isinstance(proxy, str) or not re.fullmatch(r"[0-9a-fA-F:./]+", proxy):
            raise ValueError("Provide explicit Allura proxy IPs or CIDRs")
        if ipaddress.ip_network(proxy).prefixlen == 0:
            raise ValueError("Allura cannot trust forwarded headers from every IP")
    if runtime != "raw" and (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"])):
        raise ValueError("Provide a registry digest for the Allura image")
    if runtime in CLUSTER:
        if (any(not label(plan[key]) for key in ("namespace", "state_claim", "config_secret"))
                or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}):
            raise ValueError("Use a dedicated namespace, existing claim and configuration Secret")
        return

    port = plan["http_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Use an unprivileged HTTP port")
    if (not host_path(plan["state_path"]) or not plan["state_path"].startswith(("/srv/", "/var/lib/"))
            or not host_path(plan["config_dir"]) or not plan["config_dir"].startswith("/etc/")):
        raise ValueError("Keep private configuration under /etc and state under /srv or /var/lib")
    if runtime == "raw":
        for key in ("user", "group"):
            value = plan[key]
            if not isinstance(value, str) or value == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", value):
                raise ValueError("Use a dedicated non-root service account")


def web_args(config, address, trusted_proxy_cidrs=()):
    return ["--bind", address, "--workers", "2", "--timeout", "90",
            "--forwarded-allow-ips=" + ",".join(trusted_proxy_cidrs),
            f"allura_git_http:make_app('{config}')"]


def kubernetes_objects(plan):
    metadata = {"name": plan["name"], "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "allura", "app.kubernetes.io/instance": plan["name"]}
    selector = {"matchLabels": labels}
    containers = []
    for role, program, args in (
        ("web", WEB, web_args(CONFIG, f"0.0.0.0:{PORT}", plan.get("trusted_proxy_cidrs", ()))),
        ("task", TASK, ["--plugin=Allura", "taskd", CONFIG]),
    ):
        containers.append({
            "name": role, "image": plan["image"], "command": [f"{INSTALL}/venv/bin/{program}"],
            "args": args, "workingDir": f"{INSTALL}/source/Allura",
            "env": [{"name": "HOME", "value": STATE}],
            "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [
                {"name": "state", "mountPath": STATE},
                {"name": "config", "mountPath": "/run/allura", "readOnly": True},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        })
    web = containers[0]
    web.update(
        ports=[{"name": "http", "containerPort": PORT}],
        startupProbe={"httpGet": {"path": HEALTH_PATH, "port": "http"}, "periodSeconds": 5, "failureThreshold": 60},
        readinessProbe={"httpGet": {"path": HEALTH_PATH, "port": "http"}, "periodSeconds": 5},
    )
    pod = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 120,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": containers,
        "volumes": [
            {"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
            {"name": "config", "secret": {"secretName": plan["config_secret"], "defaultMode": 0o440}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        ],
    }
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"selector": labels, "ports": [{"name": "http", "port": PORT, "targetPort": "http"}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    services = {}
    for role, program, args in (
        ("web", WEB, web_args(CONFIG, f"0.0.0.0:{PORT}", plan.get("trusted_proxy_cidrs", ()))),
        ("task", TASK, ["--plugin=Allura", "taskd", CONFIG]),
    ):
        services[role] = {
            "image": plan["image"], "entrypoint": [f"{INSTALL}/venv/bin/{program}"], "command": args,
            "working_dir": f"{INSTALL}/source/Allura", "user": "1000:1000", "read_only": True,
            "restart": "unless-stopped", "stop_grace_period": "120s",
            "environment": {"HOME": STATE}, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"], "pids_limit": 256, "mem_limit": "1g",
            "volumes": [
                {"type": "bind", "source": plan["state_path"], "target": STATE,
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": plan["config_dir"], "target": "/run/allura", "read_only": True,
                 "bind": {"create_host_path": False}},
            ],
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=1g,mode=1777"],
        }
    services["web"]["ports"] = [f"127.0.0.1:{plan['http_port']}:{PORT}"]
    return {"name": plan["name"], "services": services}


def native_units(plan):
    state = plan["state_path"]
    config = f"{plan['config_dir']}/allura.ini"
    files = {}
    for role, program, args in (
        ("web", WEB, web_args(config, f"127.0.0.1:{plan['http_port']}", plan.get("trusted_proxy_cidrs", ()))),
        ("task", TASK, ["--plugin=Allura", "taskd", config]),
    ):
        files[f"{plan['name']}-{role}.service"] = (
            f"[Unit]\nDescription=Apache Allura {role}\nAfter=network-online.target\n"
            f"Wants=network-online.target\nRequiresMountsFor={state}\n[Service]\n"
            f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={INSTALL}/source/Allura\n"
            f"Environment=HOME={state} PATH={INSTALL}/venv/bin:/usr/bin:/bin "
            "PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            f"ExecStart={INSTALL}/venv/bin/{program} "
            f"{' '.join(args[:-1])} \"{args[-1]}\"\n"
            "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
            f"ReadWritePaths={state}\nRestart=on-failure\nRestartSec=5s\nTimeoutStopSec=120s\n"
            "MemoryMax=1G\nTasksMax=256\n\n[Install]\nWantedBy=multi-user.target\n"
        )
    return files


def render(plan, runtime):
    validate(plan, runtime)
    if runtime == "raw":
        return native_units(plan)
    if runtime == "docker":
        return {"compose.yaml": yaml.safe_dump(compose(plan), sort_keys=False)}
    return {"allura.yaml": yaml.safe_dump_all(kubernetes_objects(plan), sort_keys=False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=16384, encoding="utf-8"))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Allura render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
