#!/usr/bin/env python3
"""Render a single-active GitBucket deployment for a prepared runtime."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, write_bundle
from render_gerrit_sites import dns_name, label
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("rke2", "docker", "raw", "k3s", "k8s")
WAR_SHA256 = "bbd08775a5a19d51a4358f481fb16eceecb992b2b0c83bfac414e54717310359"
UPSTREAM_IMAGE = "ghcr.io/gitbucket/gitbucket@sha256:4383274e7b3bc089633d2626167f3365d16e90d00b49d9c60dce2948a9aab230"
CONTAINER_WAR = "/opt/platform-gitbucket.war"
HTTP_PORT = 8080
SSH_PORT = 29418
STATE = "/gitbucket"


def unprivileged_port(value):
    return type(value) is int and 1024 <= value <= 65535


def render(plan, runtime):
    if runtime not in RUNTIMES:
        raise ValueError("Unsupported runtime")
    kubernetes = runtime in {"rke2", "k3s", "k8s"}
    common = {"schema_version", "name", "https_url", "ssh_host", "ssh_public_port"}
    fields = ({"namespace", "image", "state_claim"} if kubernetes else
              {"state_path", "http_port", "ssh_port", "image"} if runtime == "docker" else
              {"state_path", "http_port", "ssh_port", "java", "war", "user", "group"})
    if (not isinstance(plan, dict) or set(plan) != common | fields or
            type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or
            not label(plan["name"])):
        raise ValueError("Invalid GitBucket plan")
    url = plan["https_url"]
    if not isinstance(url, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", url):
        raise ValueError("An HTTPS origin is required")
    origin = urlsplit(url)
    if not dns_name(origin.hostname) or origin.port == 0:
        raise ValueError("Invalid HTTPS origin")
    ssh_host, ssh_port = plan["ssh_host"], plan["ssh_public_port"]
    if (not dns_name(ssh_host) or type(ssh_port) is not int or not 1 <= ssh_port <= 65535):
        raise ValueError("Invalid advertised SSH endpoint")
    if kubernetes:
        if (not label(plan["namespace"]) or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}
                or not label(plan["state_claim"])):
            raise ValueError("A dedicated namespace and prepared state claim are required")
    else:
        state = plan["state_path"]
        if (not host_path(state) or not state.startswith(("/srv/", "/var/lib/")) or
                not unprivileged_port(plan["http_port"]) or not unprivileged_port(plan["ssh_port"]) or
                plan["http_port"] == plan["ssh_port"]):
            raise ValueError("A dedicated state directory and distinct unprivileged ports are required")
    if runtime != "raw" and (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"])):
        raise ValueError("A reviewed image digest is required")
    if runtime != "raw" and plan["image"] == UPSTREAM_IMAGE:
        raise ValueError("Use the derived image with a WAR readable by the service account")

    ssh_bind = "127.0.0.1" if runtime == "raw" else "0.0.0.0"
    ssh_bind_port = plan["ssh_port"] if runtime == "raw" else SSH_PORT
    properties = (
        f"base_url={url}\nallow_account_registration=false\nallow_anonymous_access=false\n"
        "is_create_repository_option_public=false\nallow_create_repository_by_clone=false\n"
        "webhook.block_private_address=true\nssh=true\n"
        f"ssh.bindAddress.host={ssh_bind}\nssh.bindAddress.port={ssh_bind_port}\n"
        f"ssh.publicAddress.host={ssh_host}\nssh.publicAddress.port={ssh_port}\n"
    )
    files = {"gitbucket.conf": properties}
    if runtime != "raw":
        files["containerfile"] = containerfile()
    if kubernetes:
        files["gitbucket.yaml"] = yaml.safe_dump_all(kubernetes_objects(plan), sort_keys=False)
    elif runtime == "docker":
        files["compose.yaml"] = yaml.safe_dump(compose(plan), sort_keys=False)
    else:
        files.update(native_files(plan))
    return files


def java_command(java, war, home, host, port):
    return [java, "-Djava.awt.headless=true", "-Djava.io.tmpdir=/tmp", f"-Duser.home={home}", "-XX:MaxRAMPercentage=65",
            "-jar", war, f"--gitbucket.home={home}", f"--host={host}", f"--port={port}"]


def container_command():
    return java_command("java", CONTAINER_WAR, STATE, "0.0.0.0", HTTP_PORT)


def containerfile():
    return (f"FROM {UPSTREAM_IMAGE}\nUSER 0:0\n"
            f"RUN install -m 0444 /opt/gitbucket.war {CONTAINER_WAR} && "
            f"echo '{WAR_SHA256}  {CONTAINER_WAR}' | sha256sum --check --status\n"
            "USER 1000:1000\n")


def kubernetes_objects(plan):
    name = plan["name"]
    metadata = {"name": name, "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "gitbucket", "app.kubernetes.io/instance": name}
    selector = {"matchLabels": labels}
    ports = [{"name": "http", "containerPort": HTTP_PORT}, {"name": "ssh", "containerPort": SSH_PORT}]
    pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "gitbucket", "image": plan["image"], "command": container_command(), "ports": ports,
            "env": [{"name": "HOME", "value": STATE}, {"name": "XDG_CONFIG_HOME", "value": STATE + "/.config"}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "3Gi"}},
            "startupProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 5, "failureThreshold": 60},
            "readinessProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 5},
            "volumeMounts": [{"name": "state", "mountPath": STATE}, {"name": "tmp", "mountPath": "/tmp"}],
        }],
        "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}}],
    }
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"type": "ClusterIP", "selector": labels,
                  "ports": [{"name": port["name"], "port": port["containerPort"], "targetPort": port["name"]}
                            for port in ports]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    return {"services": {plan["name"]: {
        "image": plan["image"], "entrypoint": container_command(), "command": [], "user": "1000:1000",
        "environment": {"HOME": STATE, "XDG_CONFIG_HOME": STATE + "/.config"},
        "read_only": True, "restart": "unless-stopped", "pids_limit": 512, "mem_limit": "3g",
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "ports": [f"127.0.0.1:{plan['http_port']}:{HTTP_PORT}", f"127.0.0.1:{plan['ssh_port']}:{SSH_PORT}"],
        "tmpfs": ["/tmp:rw,nosuid,nodev,size=2g,mode=1777"],
        "volumes": [{"type": "bind", "source": plan["state_path"], "target": STATE,
                     "bind": {"create_host_path": False}}],
    }}}


def native_files(plan):
    state, java, war = plan["state_path"], plan["java"], plan["war"]
    if any(not host_path(path) or path == state or path.startswith(state + "/") for path in (java, war)):
        raise ValueError("Reviewed runtime files must be outside the writable state directory")
    if any(not isinstance(plan[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", plan[key])
           or plan[key] == "root" for key in ("user", "group")):
        raise ValueError("A dedicated unprivileged user and group are required")
    command = " ".join(java_command(java, war, state, "127.0.0.1", plan["http_port"]))
    service = (
        "[Unit]\nDescription=GitBucket\nAfter=network.target\n\n[Service]\n"
        f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={state}\n"
        f"Environment=HOME={state} XDG_CONFIG_HOME={state}/.config\n"
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
        f"ReadWritePaths={state}\nExecStartPre=/usr/bin/sha256sum --check --status {war}.sha256\n"
        f"ExecStart={command}\nRestart=on-failure\nRestartSec=5s\nMemoryMax=3G\nTasksMax=512\n"
        "\n[Install]\nWantedBy=multi-user.target\n"
    )
    return {plan["name"] + ".service": service, "gitbucket.war.sha256": f"{WAR_SHA256}  {war}\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=RUNTIMES)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--containerfile", action="store_true", help="Print the pinned image build recipe")
    args = parser.parse_args(argv)
    if args.containerfile:
        if args.runtime or args.plan or args.output_dir:
            parser.error("--containerfile is a standalone action")
        print(containerfile(), end="")
        return 0
    if not all((args.runtime, args.plan, args.output_dir)):
        parser.error("rendering requires --runtime, --plan and --output-dir")
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=16384, encoding="utf-8"))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (OSError, ValueError):
        print("GitBucket rendering failed. Check the runtime settings and choose a new private output directory.",
              file=sys.stderr)
        return 1
    print(json.dumps({"component": "gitbucket", "runtime": args.runtime, "rendered": True, "deployed": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
