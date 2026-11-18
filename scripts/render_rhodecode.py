#!/usr/bin/env python3
"""Render single-active RhodeCode CE with a VCS server and Celery worker."""

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
INSTALL = "/usr/local/bin/rhodecode_bin"
CONFIG = "/etc/rhodecode/conf"
DATA = "/var/opt/rhodecode_data"
REPOS = "/var/opt/rhodecode_repo_store"
HTTP_PORT = 10020
VCS_PORT = 10010
UID = 999
ROLES = {
    "web": ("bin", "rhodecode.ini", "gunicorn_conf_rc.py"),
    "vcs": ("vcs_bin", "vcsserver.ini", "gunicorn_conf_vcs.py"),
}
PROCESSES = (*ROLES, "worker")


def validate(plan, runtime):
    fields = {"schema_version", "name"}
    if runtime in CLUSTER:
        fields |= {"namespace", "state_claim", "config_secret", "image"}
    else:
        fields |= {"state_path", "config_dir", "http_port"}
        fields |= {"install_path", "user", "group"} if runtime == "raw" else {"image"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict)
            or set(plan) - {"trusted_proxy_ips"} != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not label(plan["name"]) or len(plan["name"]) > 53):
        raise ValueError("Invalid RhodeCode CE plan fields")
    proxies = plan.get("trusted_proxy_ips", [])
    if not isinstance(proxies, list) or len(proxies) > 32:
        raise ValueError("Provide at most 32 trusted web proxy IP addresses")
    for proxy in proxies:
        if not isinstance(proxy, str) or not re.fullmatch(r"[0-9a-fA-F:.]+", proxy):
            raise ValueError("Use individual web proxy IP addresses")
        ipaddress.ip_address(proxy)
    if runtime != "raw" and (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"])):
        raise ValueError("Provide the Community Edition image by registry digest")
    if runtime in CLUSTER:
        if (any(not label(plan[key]) for key in ("namespace", "state_claim", "config_secret"))
                or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}):
            raise ValueError("Use a dedicated namespace, existing claim and configuration Secret")
        return
    port = plan["http_port"]
    if type(port) is not int or not 1024 <= port <= 65535 or port == VCS_PORT:
        raise ValueError("Use an unprivileged web port distinct from the VCS port")
    if (not host_path(plan["state_path"]) or not plan["state_path"].startswith(("/srv/", "/var/lib/"))
            or not host_path(plan["config_dir"]) or not plan["config_dir"].startswith("/etc/")):
        raise ValueError("Keep configuration under /etc and state under /srv or /var/lib")
    if runtime == "raw":
        if not host_path(plan["install_path"]) or not plan["install_path"].startswith("/opt/"):
            raise ValueError("Specify the prepared RhodeCode installation under /opt")
        for key in ("user", "group"):
            value = plan[key]
            if not isinstance(value, str) or value == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", value):
                raise ValueError("Use a dedicated non-root service account")


def command(plan, role, runtime):
    native = runtime == "raw"
    root = plan["install_path"] if native else INSTALL
    config = plan["config_dir"] if native else CONFIG
    if role == "worker":
        return [f"{root}/bin/celery", "--no-color", "--app=rhodecode.lib.celerylib.loader", "worker",
                "--concurrency=2", "--max-tasks-per-child=150", "--loglevel=WARNING", f"--ini={config}/rhodecode.ini"]
    executable, ini, gunicorn = ROLES[role]
    host, port, proxies = "127.0.0.1", VCS_PORT, ()
    if role == "web":
        host = "127.0.0.1" if native else "0.0.0.0"
        port = plan["http_port"] if native else HTTP_PORT
        proxies = plan.get("trusted_proxy_ips", ())
    return [f"{root}/{executable}/gunicorn", f"--paster={config}/{ini}", f"--config={config}/{gunicorn}",
            f"--bind={host}:{port}", "--forwarded-allow-ips=" + ",".join(proxies),
            "--no-control-socket", "--error-logfile=-"]


def kubernetes_objects(plan, runtime):
    metadata = {"name": plan["name"], "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "rhodecode", "app.kubernetes.io/instance": plan["name"]}
    selector = {"matchLabels": labels}
    containers = []
    for role in PROCESSES:
        binary, *args = command(plan, role, runtime)
        container = {
            "name": role, "image": plan["image"], "command": [binary], "args": args,
            "env": [{"name": "HOME", "value": DATA}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "200m", "memory": "384Mi"}, "limits": {"memory": "2Gi"}},
            "volumeMounts": [{"name": "state", "mountPath": DATA, "subPath": "data"},
                             {"name": "state", "mountPath": REPOS, "subPath": "repos"},
                             {"name": "config", "mountPath": CONFIG, "readOnly": True},
                             {"name": "tmp", "mountPath": "/tmp"},
                             {"name": "ramdisk", "mountPath": "/data_ramdisk"}],
        }
        if role == "web":
            container["ports"] = [{"name": "http", "containerPort": HTTP_PORT}]
            check = {"httpGet": {"path": "/_admin/ops/ping", "port": "http",
                                 "httpHeaders": [{"name": "User-Agent", "value": "RhodeCode-Healthcheck"}]}}
        elif role == "vcs":
            check = {"exec": {"command": ["/usr/bin/curl", "--noproxy", "*", "--fail", "--silent", "--max-time", "3",
                                         f"http://127.0.0.1:{VCS_PORT}/status"]}}
        if role in ROLES:
            container["readinessProbe"] = {**check, "periodSeconds": 5, "timeoutSeconds": 4}
            container["startupProbe"] = {**container["readinessProbe"], "failureThreshold": 60}
        containers.append(container)
    pod = {
        "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 120,
        "securityContext": {"runAsNonRoot": True, "runAsUser": UID, "runAsGroup": UID, "fsGroup": UID,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": containers,
        "volumes": [
            {"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
            {"name": "config", "secret": {"secretName": plan["config_secret"], "defaultMode": 0o440}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
            {"name": "ramdisk", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
        ],
    }
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"selector": labels, "ports": [{"name": "http", "port": HTTP_PORT, "targetPort": "http"}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    services = {}
    for role in PROCESSES:
        binary, *args = command(plan, role, "docker")
        services[role] = {
            "image": plan["image"], "entrypoint": [binary], "command": args,
            "user": f"{UID}:{UID}", "read_only": True, "restart": "unless-stopped", "stop_grace_period": "120s",
            "environment": {"HOME": DATA, "PYTHONDONTWRITEBYTECODE": "1"},
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 256,
            "mem_limit": "2g", "tmpfs": ["/tmp:rw,nosuid,nodev,size=1g,mode=1777",
                                         f"/data_ramdisk:rw,nosuid,nodev,size=256m,uid={UID},gid={UID},mode=0700"],
            "volumes": [{"type": "bind", "source": source, "target": target, "read_only": read_only,
                         "bind": {"create_host_path": False}} for source, target, read_only in (
                (plan["state_path"] + "/data", DATA, False), (plan["state_path"] + "/repos", REPOS, False),
                (plan["config_dir"], CONFIG, True),
            )],
        }
    services["web"]["ports"] = [f"127.0.0.1:{plan['http_port']}:{HTTP_PORT}"]
    services["vcs"]["network_mode"] = "service:web"
    services["worker"]["network_mode"] = "service:web"
    return {"name": plan["name"], "services": services}


def native_units(plan):
    files = {}
    data = plan["state_path"] + "/data"
    vcs = plan["name"] + "-vcs.service"
    worker = plan["name"] + "-worker.service"
    for role in PROCESSES:
        dependency = f"Requires={vcs} {worker}\nAfter={vcs} {worker}\n" if role == "web" else ""
        files[f"{plan['name']}-{role}.service"] = (
            f"[Unit]\nDescription=RhodeCode CE {role}\nAfter=network-online.target\nWants=network-online.target\n"
            f"{dependency}RequiresMountsFor={plan['state_path']}\n[Service]\n"
            f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={data}\n"
            f"Environment=HOME={data} PATH={plan['install_path']}/vcs_bin:{plan['install_path']}/bin:/usr/bin:/bin "
            "PYTHONDONTWRITEBYTECODE=1\n"
            f"ExecStart={' '.join(command(plan, role, 'raw'))}\n"
            "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
            f"ReadWritePaths={data} {plan['state_path']}/repos\nRestart=on-failure\nRestartSec=5s\nTimeoutStopSec=120s\n"
            "MemoryMax=2G\nTasksMax=256\n\n[Install]\nWantedBy=multi-user.target\n"
        )
    return files


def render(plan, runtime):
    validate(plan, runtime)
    if runtime == "raw":
        return native_units(plan)
    if runtime == "docker":
        return {"compose.yaml": yaml.safe_dump(compose(plan), sort_keys=False)}
    return {"rhodecode.yaml": yaml.safe_dump_all(kubernetes_objects(plan, runtime), sort_keys=False)}


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
        print(f"RhodeCode CE render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
