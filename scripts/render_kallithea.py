#!/usr/bin/env python3
"""Render a single-active Kallithea server from a prepared configuration."""

import argparse
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
PORT = 5000
STATE = "/var/lib/kallithea"
CONFIG = "/run/kallithea/kallithea.ini"
LAUNCHER = "/opt/kallithea/bin/python"
START_SERVER = (
    "import sys; from logging.config import fileConfig; from pathlib import Path; "
    "from paste.deploy import loadapp; from waitress import serve; "
    "fileConfig(sys.argv[1], {'__file__': sys.argv[1], 'here': str(Path(sys.argv[1]).parent)}); "
    "serve(loadapp('config:' + sys.argv[1]), listen=sys.argv[2], threads=1)"
)


def validate(plan, runtime):
    fields = {"schema_version", "name"}
    if runtime in CLUSTER:
        fields |= {"namespace", "state_claim", "config_secret", "image"}
    else:
        fields |= {"state_path", "config_file", "http_port"}
        fields |= {"venv", "user", "group"} if runtime == "raw" else {"image"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not label(plan["name"])):
        raise ValueError("Invalid Kallithea plan fields")
    if runtime != "raw" and (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"])):
        raise ValueError("Provide the built Kallithea image by registry digest")
    if runtime in CLUSTER:
        if (any(not label(plan[key]) for key in ("namespace", "state_claim", "config_secret"))
                or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}):
            raise ValueError("Use a dedicated namespace, state claim, and configuration Secret")
        return

    port = plan["http_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Use an unprivileged HTTP port")
    if (not host_path(plan["state_path"]) or not plan["state_path"].startswith(("/srv/", "/var/lib/"))
            or not host_path(plan["config_file"]) or not plan["config_file"].startswith("/etc/")):
        raise ValueError("Keep the private configuration in /etc and state under /srv or /var/lib")
    if runtime == "raw":
        if not host_path(plan["venv"]) or not plan["venv"].startswith("/opt/"):
            raise ValueError("Specify the prepared Python virtual environment under /opt")
        for key in ("user", "group"):
            value = plan[key]
            if not isinstance(value, str) or value == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", value):
                raise ValueError("Use a dedicated non-root service account")


def serve(config, host, port):
    return ["-c", START_SERVER, config, f"{host}:{port}"]


def kubernetes_objects(plan):
    metadata = {"name": plan["name"], "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "kallithea", "app.kubernetes.io/instance": plan["name"]}
    selector = {"matchLabels": labels}
    container = {
        "name": "kallithea", "image": plan["image"], "command": [LAUNCHER], "args": serve(CONFIG, "0.0.0.0", PORT),
        "env": [{"name": "HOME", "value": STATE}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
        "ports": [{"name": "http", "containerPort": PORT}],
        "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "startupProbe": {"httpGet": {"path": "/_admin/login", "port": "http"},
                         "periodSeconds": 5, "failureThreshold": 60},
        "readinessProbe": {"httpGet": {"path": "/_admin/login", "port": "http"}, "periodSeconds": 5},
        "volumeMounts": [{"name": "state", "mountPath": STATE}, {"name": "tmp", "mountPath": "/tmp"},
                         {"name": "config", "mountPath": "/run/kallithea", "readOnly": True}],
    }
    pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
        "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                    {"name": "config", "secret": {"secretName": plan["config_secret"], "defaultMode": 0o440}}],
    }
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"type": "ClusterIP", "selector": labels,
                  "ports": [{"name": "http", "port": PORT, "targetPort": "http"}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    return {"services": {plan["name"]: {
        "image": plan["image"], "entrypoint": [LAUNCHER], "command": serve(CONFIG, "0.0.0.0", PORT),
        "user": "1000:1000", "read_only": True, "restart": "unless-stopped",
        "environment": {"HOME": STATE, "PYTHONDONTWRITEBYTECODE": "1"},
        "ports": [f"127.0.0.1:{plan['http_port']}:{PORT}"],
        "volumes": [{"type": "bind", "source": plan["state_path"], "target": STATE, "bind": {"create_host_path": False}},
                    {"type": "bind", "source": plan["config_file"], "target": CONFIG, "read_only": True,
                     "bind": {"create_host_path": False}}],
        "tmpfs": ["/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "mem_limit": "1g", "pids_limit": 256,
    }}}


def native_unit(plan):
    state, venv = plan["state_path"], plan["venv"]
    config = plan["config_file"]
    listen = f"127.0.0.1:{plan['http_port']}"
    return (
        "[Unit]\nDescription=Kallithea\nAfter=network.target\n[Service]\n"
        f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={state}\n"
        f"Environment=HOME={state} PATH={venv}/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1\n"
        f'ExecStart={venv}/bin/python -c "{START_SERVER}" {config} {listen}\n'
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
        f"ReadWritePaths={state}\nRestart=on-failure\nRestartSec=5s\nMemoryMax=1G\nTasksMax=256\n"
        "\n[Install]\nWantedBy=multi-user.target\n"
    )


def render(plan, runtime):
    validate(plan, runtime)
    if runtime == "raw":
        return {plan["name"] + ".service": native_unit(plan)}
    if runtime == "docker":
        return {"compose.yaml": yaml.safe_dump(compose(plan), sort_keys=False)}
    return {"kallithea.yaml": yaml.safe_dump_all(kubernetes_objects(plan), sort_keys=False)}


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
        print(f"Kallithea render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
