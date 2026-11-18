#!/usr/bin/env python3
"""Render a single-active SCM-Manager deployment for a prepared runtime."""

import argparse
import re
import sys
from pathlib import Path

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, write_bundle
from render_gerrit_sites import label
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.12.2"
IMAGE = "docker.io/scmmanager/scm-manager@sha256:9e89d2459c2b9e41634106e4da49670c97fbbaf34bf90c3bc2d4dabfc758e478"
ARCHIVE_SHA256 = "44ce0bae26385ad9781aa722f76f0a880aaccc862872e666f6da4019723988ea"
STATE = "/var/lib/scm"
WORK = "/var/cache/scm/work"
PORT = 8080
CONTEXT = "/scm"
PASSWORD_ENV = "SCM_WEBAPP_INITIALPASSWORD"
CLUSTER_RUNTIMES = {"rke2", "k3s", "k8s"}


def validate(plan, runtime):
    common = {"schema_version", "name"}
    cluster = runtime in CLUSTER_RUNTIMES
    fields = {"namespace", "state_claim", "bootstrap_secret"} if cluster else {
        "state_path", "http_port", "bootstrap_env_file",
    }
    if runtime == "raw":
        fields |= {"install_dir", "java_home", "user", "group"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) != common | fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not label(plan["name"])):
        raise ValueError("Invalid SCM-Manager plan fields")
    if cluster:
        if (not label(plan["namespace"]) or not label(plan["state_claim"])
                or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}
                or (plan["bootstrap_secret"] is not None and not label(plan["bootstrap_secret"]))):
            raise ValueError("Use a dedicated namespace and valid PVC and optional bootstrap Secret references")
        return

    state, port = plan["state_path"], plan["http_port"]
    if (not host_path(state) or not state.startswith(("/srv/", "/var/lib/"))
            or type(port) is not int or not 1024 <= port <= 65535):
        raise ValueError("Use a dedicated state directory and an unprivileged HTTP port")
    env_file = plan["bootstrap_env_file"]
    if env_file is not None and (not host_path(env_file) or not env_file.startswith("/etc/")):
        raise ValueError("Keep the private bootstrap environment file under /etc")
    if runtime == "raw":
        for field in ("install_dir", "java_home"):
            path = plan[field]
            if (not host_path(path) or not path.startswith(("/opt/", "/usr/"))):
                raise ValueError("Keep the installation and Java under /opt or /usr")
        for field in ("user", "group"):
            value = plan[field]
            if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", value) or value == "root":
                raise ValueError("Use a dedicated non-root account")


def environment(state=STATE, work=WORK, port=PORT, address="0.0.0.0"):
    return {
        "HOME": state,
        "JAVA_OPTS": f"-Duser.home={state} -Dorg.sqlite.tmpdir={work}",
        "SCM_ADDRESS_BINDING": address,
        "SCM_PORT": str(port),
        "SCM_CONTEXT_PATH": CONTEXT,
        "SCM_TEMP_DIR": "/tmp/scm-jetty",
        "SCM_WEBAPP_HOMEDIR": state,
        "SCM_WEBAPP_WORKDIR": work,
    }


def kubernetes_objects(plan):
    name = plan["name"]
    metadata = {"name": name, "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "scm-manager", "app.kubernetes.io/instance": name}
    selector = {"matchLabels": labels}
    env = [{"name": key, "value": value} for key, value in environment().items()]
    if plan["bootstrap_secret"] is not None:
        env.append({"name": PASSWORD_ENV, "valueFrom": {"secretKeyRef": {
            "name": plan["bootstrap_secret"], "key": "initial-password",
        }}})
    container = {
        "name": "scm-manager", "image": IMAGE, "env": env,
        "ports": [{"name": "http", "containerPort": PORT}],
        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "startupProbe": {"httpGet": {"path": CONTEXT + "/", "port": "http"},
                         "periodSeconds": 5, "failureThreshold": 60},
        "readinessProbe": {"httpGet": {"path": CONTEXT + "/", "port": "http"}, "periodSeconds": 5},
        "volumeMounts": [{"name": name, "mountPath": path} for name, path in
                         (("state", STATE), ("work", WORK), ("tmp", "/tmp"))],
    }
    pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
        "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
                    {"name": "work", "emptyDir": {"sizeLimit": "2Gi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
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
    service = {
        "image": IMAGE, "user": "1000:1000", "read_only": True, "restart": "unless-stopped",
        "environment": environment(), "ports": [f"127.0.0.1:{plan['http_port']}:{PORT}"],
        "volumes": [{"type": "bind", "source": plan["state_path"], "target": STATE,
                     "bind": {"create_host_path": False}}],
        "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=1g,mode=1777",
                  f"{WORK}:rw,exec,nosuid,nodev,size=2g,uid=1000,gid=1000,mode=0700"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "mem_limit": "2g", "pids_limit": 512,
    }
    if plan["bootstrap_env_file"] is not None:
        service["env_file"] = [{"path": plan["bootstrap_env_file"], "format": "raw"}]
    return {"services": {plan["name"]: service}}


def native_files(plan):
    state, install = plan["state_path"], plan["install_dir"]
    env = environment(state, state + "/work", plan["http_port"], "127.0.0.1")
    env["JAVA_HOME"] = plan["java_home"]
    env_lines = "".join(f'Environment="{key}={value}"\n' for key, value in env.items())
    bootstrap = plan["bootstrap_env_file"]
    unit = (
        "[Unit]\nDescription=SCM-Manager\nAfter=network.target\n\n[Service]\n"
        f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={install}\n"
        + env_lines
        + (f"EnvironmentFile={bootstrap}\n" if bootstrap is not None else "")
        + f"ExecStart={install}/bin/scm-server\n"
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
        f"ReadWritePaths={state}\nRestart=on-failure\nRestartSec=5s\nMemoryMax=2G\nTasksMax=512\n"
        "\n[Install]\nWantedBy=multi-user.target\n"
    )
    return {plan["name"] + ".service": unit, "scm-manager.sha256": f"{ARCHIVE_SHA256}  scm-manager-{VERSION}.tar.gz\n"}


def render(plan, runtime):
    validate(plan, runtime)
    if runtime == "raw":
        return native_files(plan)
    if runtime == "docker":
        return {"compose.yaml": yaml.safe_dump(compose(plan), sort_keys=False)}
    return {"scm-manager.yaml": yaml.safe_dump_all(kubernetes_objects(plan), sort_keys=False)}


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
        print(f"SCM-Manager render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
