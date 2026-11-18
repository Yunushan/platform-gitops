#!/usr/bin/env python3
"""Render SourceHut accounts and Git hosting with an external database and cache."""

import argparse
import sys
from pathlib import Path

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, write_bundle
from render_gerrit_sites import dns_name, label
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CLUSTER = {"rke2", "k3s", "k8s"}
PORT = 8080
SSH_PORT = 2222
STATE = "/var/lib/git"
CONFIG_DIR = "/etc/sr.ht"
NGINX_CONFIG = "/etc/sourcehut-proxy/nginx.conf"
SSH_CONFIG = "/etc/sourcehut-ssh/sshd_config"
SSH_KEY = "/etc/sourcehut-ssh/ssh_host_ed25519_key"
SSH_COMMAND = ["/usr/sbin/sshd", "-D", "-e", "-f", SSH_CONFIG]
SSH_CAPABILITIES = ["SETUID", "SETGID", "SYS_CHROOT"]
ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
# IDs come from the pinned image's Alpine installation.
ROLES = {
    "proxy": (103, ["/usr/sbin/nginx", "-e", "/dev/stderr", "-c", NGINX_CONFIG, "-g", "daemon off;"], False),
    "meta-web": (102, ["/usr/bin/gunicorn", "metasrht.app:app", "-b", "127.0.0.1:5000", "-w", "2"], False),
    "meta-api": (102, ["/usr/bin/meta.sr.ht-api", "-b", "127.0.0.1:5100", "-m", "127.0.0.1:0"], False),
    "meta-webhooks": (102, ["/usr/bin/celery", "-A", "metasrht.webhooks", "worker", "--concurrency=2"], False),
    "git-web": (101, ["/usr/bin/gunicorn", "gitsrht.app:app", "-b", "127.0.0.1:5001", "-w", "2"], True),
    "git-api": (101, ["/usr/bin/git.sr.ht-api", "-b", "127.0.0.1:5101", "-m", "127.0.0.1:0"], True),
    "git-http": (101, ["/usr/bin/fcgiwrap", "-s", "tcp:127.0.0.1:9100"], True),
}


def validate(plan, runtime):
    fields = {"schema_version", "name", "meta_host", "git_host"}
    if runtime in CLUSTER:
        fields |= {"namespace", "state_claim", "config_secret", "image"}
    elif runtime == "docker":
        fields |= {"state_path", "config_dir", "http_port", "image"}
    elif runtime == "raw":
        fields |= {"http_port"}
    if isinstance(plan, dict):
        if runtime in CLUSTER and "ssh_host_key_secret" in plan:
            fields.add("ssh_host_key_secret")
        elif runtime in {"docker", "raw"} and "ssh_port" in plan:
            fields.add("ssh_port")
            if runtime == "docker":
                fields.add("ssh_host_key_file")
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not label(plan["name"]) or len(plan["name"]) > 56):
        raise ValueError("Invalid SourceHut deployment plan")
    if (any(not dns_name(plan[key]) or "." not in plan[key] for key in ("meta_host", "git_host"))
            or plan["meta_host"].casefold() == plan["git_host"].casefold()):
        raise ValueError("Provide distinct DNS hostnames for accounts and Git")
    if runtime != "raw" and (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"])):
        raise ValueError("Provide the built SourceHut image by registry digest")
    if runtime in CLUSTER:
        if (any(not label(plan[key]) for key in ("namespace", "state_claim", "config_secret"))
                or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}):
            raise ValueError("Use a dedicated namespace, existing claim and configuration Secret")
        if "ssh_host_key_secret" in plan and (not label(plan["ssh_host_key_secret"])
                or plan["ssh_host_key_secret"] == plan["config_secret"]):
            raise ValueError("Use a separate SSH host key Secret")
    else:
        port = plan["http_port"]
        if type(port) is not int or not 1024 <= port <= 65535 or port in {5000, 5001, 5100, 5101, 9100}:
            raise ValueError("Use a distinct unprivileged proxy port")
    if runtime == "docker" and (
            not host_path(plan["state_path"]) or not plan["state_path"].startswith(("/srv/", "/var/lib/"))
            or not host_path(plan["config_dir"]) or not plan["config_dir"].startswith("/etc/")):
        raise ValueError("Use prepared state under /srv or /var/lib and private config under /etc")
    if "ssh_port" in plan:
        port = plan["ssh_port"]
        if type(port) is not int or not 1024 <= port <= 65535 or port in {plan["http_port"], 5000, 5001, 5100, 5101, 9100}:
            raise ValueError("Use a distinct unprivileged SSH port")
        if runtime == "docker" and (not host_path(plan["ssh_host_key_file"]) or not plan["ssh_host_key_file"].startswith("/etc/")):
            raise ValueError("Use an existing private SSH host key under /etc")


def sshd_config(port, listen, host_key=SSH_KEY):
    return f"""Port {port}
ListenAddress {listen}
HostKey {host_key}
PidFile /tmp/sourcehut-sshd.pid
AllowUsers git
PermitRootLogin no
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthorizedKeysFile none
AuthorizedKeysCommand /usr/bin/sourcehut-ssh %u /var/empty %t %k
AuthorizedKeysCommandUser git
PermitUserEnvironment SRHT_PUSH
PermitUserRC no
PermitTTY no
DisableForwarding yes
MaxAuthTries 3
LoginGraceTime 30
"""


def nginx_config(plan, listen):
    blocks = []
    for name, web, api in (("meta", 5000, 5100), ("git", 5001, 5101)):
        block = f"""    server {{
        listen {listen};
        server_name {plan[name + '_host']};
        if ($http_authorization ~* \"^internal([[:space:]]|$)\") {{ return 403; }}
        location = /query {{
            proxy_pass http://127.0.0.1:{api};
        }}
        location / {{ proxy_pass http://127.0.0.1:{web}; }}
"""
        if name == "git":
            block += """        location ^~ /query/blob/ { proxy_pass http://127.0.0.1:5101; }
        location ~ ^/~[^/]+/[^/]+/(HEAD|info/refs|objects/info/.*|git-upload-pack)$ {
            if ($arg_service = git-receive-pack) { return 405; }
            fastcgi_pass 127.0.0.1:9100;
            include /etc/nginx/fastcgi_params;
            fastcgi_param SCRIPT_FILENAME /usr/bin/git.sr.ht-http-clone;
            fastcgi_param PATH_INFO $uri;
            fastcgi_param GIT_PROJECT_ROOT /var/lib/git;
            gzip off;
        }
        location ~ /git-receive-pack$ { return 405; }
"""
        blocks.append(block + "    }\n")
    return f"""worker_processes 1;
pid /tmp/sourcehut-nginx.pid;
error_log /dev/stderr warn;
events {{ worker_connections 256; }}
http {{
    access_log off;
    client_body_temp_path /tmp/client;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    client_max_body_size 100m;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    server {{
        listen {listen} default_server;
        location = /healthz {{ return 200; }}
        location / {{ return 404; }}
    }}
{''.join(blocks)}}}
"""


def kubernetes_objects(plan, nginx):
    name = plan["name"]
    namespace = plan["namespace"]
    metadata = {"name": name, "namespace": namespace}
    labels = {"app.kubernetes.io/name": "sourcehut", "app.kubernetes.io/instance": name}
    selector = {"matchLabels": labels}
    containers = []
    for role, (uid, command, repository_access) in ROLES.items():
        container = {
            "name": role, "image": plan["image"], "command": command[:1], "args": command[1:],
            "securityContext": {"runAsUser": uid, "runAsGroup": uid, "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "100m", "memory": "96Mi"}, "limits": {"memory": "512Mi"}},
            "env": [{"name": key, "value": value} for key, value in ENV.items()],
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        }
        if role == "proxy":
            container["volumeMounts"].append({"name": "proxy", "mountPath": "/etc/sourcehut-proxy", "readOnly": True})
            container["ports"] = [{"name": "http", "containerPort": PORT}]
            container["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}}
        else:
            container["volumeMounts"].append({"name": "config", "mountPath": CONFIG_DIR, "readOnly": True})
        if repository_access:
            container["volumeMounts"].append({"name": "state", "mountPath": STATE, "readOnly": role == "git-http"})
        containers.append(container)
    pod = {
        "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 90,
        "securityContext": {"runAsNonRoot": True, "fsGroup": 101, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": containers,
        "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
                    {"name": "config", "secret": {"secretName": plan["config_secret"], "defaultMode": 0o440}},
                    {"name": "proxy", "configMap": {"name": name + "-proxy"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
    }
    proxy_files = {"nginx.conf": nginx}
    ports = [{"name": "http", "port": PORT, "targetPort": "http"}]
    if "ssh_host_key_secret" in plan:
        proxy_files["sshd_config"] = sshd_config(SSH_PORT, "0.0.0.0", "/tmp/ssh-host/ed25519")
        pod["volumes"].extend([
            {"name": "host-key", "secret": {"secretName": plan["ssh_host_key_secret"], "defaultMode": 0o400,
                                            "items": [{"key": "ssh_host_ed25519_key", "path": "ed25519"}]}},
            {"name": "ssh-logs", "emptyDir": {"sizeLimit": "32Mi"}},
            {"name": "ssh-tmp", "emptyDir": {"sizeLimit": "128Mi"}},
        ])
        # The pod fsGroup makes Secret files group-readable. OpenSSH requires
        # its copy to be root-owned and mode 0600.
        pod["containers"].append({
            "name": "git-ssh", "image": plan["image"], "command": ["/bin/sh", "-ec"],
            "args": ["umask 077; mkdir -p /tmp/ssh-host; cat /run/sourcehut-host-key/ed25519 > /tmp/ssh-host/ed25519; "
                     "exec /usr/sbin/sshd -D -e -f /etc/sourcehut-proxy/sshd_config"],
            "securityContext": {"runAsNonRoot": False, "runAsUser": 0, "runAsGroup": 0,
                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"], "add": SSH_CAPABILITIES}},
            "resources": {"requests": {"cpu": "100m", "memory": "96Mi"}, "limits": {"memory": "512Mi"}},
            "ports": [{"name": "ssh", "containerPort": SSH_PORT}],
            "readinessProbe": {"tcpSocket": {"port": "ssh"}},
            "volumeMounts": [
                {"name": "ssh-tmp", "mountPath": "/tmp"},
                {"name": "ssh-logs", "mountPath": "/var/log/git.sr.ht"},
                {"name": "state", "mountPath": STATE},
                {"name": "config", "mountPath": CONFIG_DIR, "readOnly": True},
                {"name": "proxy", "mountPath": "/etc/sourcehut-proxy", "readOnly": True},
                {"name": "host-key", "mountPath": "/run/sourcehut-host-key", "readOnly": True},
            ],
        })
        ports.append({"name": "ssh", "port": SSH_PORT, "targetPort": "ssh"})
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {**metadata, "name": name + "-proxy"},
         "data": proxy_files},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"selector": labels, "ports": ports}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    services = {}
    for role, (uid, command, repository_access) in ROLES.items():
        mounts = [
            {"type": "bind", "source": "./nginx.conf" if role == "proxy" else plan["config_dir"],
             "target": NGINX_CONFIG if role == "proxy" else CONFIG_DIR, "read_only": True,
             "bind": {"create_host_path": False}},
        ]
        if repository_access:
            mounts.append({"type": "bind", "source": plan["state_path"], "target": STATE,
                           "read_only": role == "git-http", "bind": {"create_host_path": False}})
        settings = {"ports": [f"127.0.0.1:{plan['http_port']}:{PORT}"]} if role == "proxy" else {
            "network_mode": "service:proxy",
        }
        services[role] = {
            "image": plan["image"], "entrypoint": command[:1], "command": command[1:], "user": f"{uid}:{uid}",
            "group_add": ["101"], "environment": ENV, "read_only": True, "restart": "unless-stopped",
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 128, "mem_limit": "512m",
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=512m,mode=1777"], "volumes": mounts, **settings,
        }
    if "ssh_port" in plan:
        services["proxy"]["ports"].append(f"127.0.0.1:{plan['ssh_port']}:{SSH_PORT}")
        services["git-ssh"] = {
            **services["git-api"], "entrypoint": SSH_COMMAND[:1], "command": SSH_COMMAND[1:], "user": "0:0",
            "cap_add": SSH_CAPABILITIES, "volumes": [
                *services["git-api"]["volumes"],
                {"type": "bind", "source": "./sshd_config", "target": SSH_CONFIG, "read_only": True,
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": plan["ssh_host_key_file"], "target": SSH_KEY, "read_only": True,
                 "bind": {"create_host_path": False}},
            ],
            "tmpfs": [*services["git-api"]["tmpfs"], "/var/log/git.sr.ht:rw,nosuid,nodev,noexec,size=16m,uid=101,gid=101,mode=0750"],
        }
    return {"name": plan["name"], "services": services}


def render(plan, runtime):
    validate(plan, runtime)
    listener = f"127.0.0.1:{plan['http_port']}" if runtime == "raw" else str(PORT)
    nginx = nginx_config(plan, listener)
    if runtime == "raw":
        files = {"nginx.conf": nginx, "meta.sr.ht-api.conf": "META_BIND=127.0.0.1:5100\nMETA_ARGS='-m 127.0.0.1:0'\n",
                 "git.sr.ht-api.conf": "GIT_BIND=127.0.0.1:5101\nGIT_ARGS='-m 127.0.0.1:0'\n",
                 "sourcehut-fcgiwrap": "#!/sbin/openrc-run\ncommand=/usr/bin/fcgiwrap\n"
                 "command_args='-s tcp:127.0.0.1:9100'\ncommand_user=git:git\nsupervisor=supervise-daemon\n"}
        if "ssh_port" in plan:
            files["sshd_config"] = sshd_config(plan["ssh_port"], "127.0.0.1")
            files["sourcehut-sshd"] = ("#!/sbin/openrc-run\ncommand=/usr/sbin/sshd\n"
                                        f"command_args='-D -e -f {SSH_CONFIG}'\nsupervisor=supervise-daemon\n")
        return files
    if runtime == "docker":
        files = {"nginx.conf": nginx, "compose.yaml": yaml.safe_dump(compose(plan), sort_keys=False)}
        if "ssh_port" in plan:
            files["sshd_config"] = sshd_config(SSH_PORT, "0.0.0.0")
        return files
    return {"sourcehut.yaml": yaml.safe_dump_all(kubernetes_objects(plan, nginx), sort_keys=False)}


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
        print(f"SourceHut render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
