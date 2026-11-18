#!/usr/bin/env python3
"""Render a single-active Gogs deployment for a prepared runtime."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import RUNTIMES, dns_name, write_bundle
from render_gerrit_sites import label
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "docker.io/gogs/gogs@sha256:95473d5a859ea3c0db1db10e853b1f3324ec8e95450e9be8a8c1bd42f1ccde59"
VERSION = "0.14.4"
BINARY_SHA256 = "0eab3a3848af833cef38d9e5e886a862acb7b0a7c15d4cac90db5bc3e735901b"
HTTP_PORT = 3000
SSH_PORT = 2222
STATE = "/data"
CA_PATH = "/etc/gogs/postgres-ca.crt"
SECRET_ENV = "GOGS_SECURITY_SECRET_KEY"
DATABASE_ENV = "GOGS_DATABASE_PASSWORD"


def port(value, minimum=1):
    return type(value) is int and minimum <= value <= 65535


def validate(plan, runtime):
    if runtime not in RUNTIMES:
        raise ValueError("Unsupported runtime")
    common = {"schema_version", "name", "https_url", "ssh_host", "ssh_public_port", "database"}
    container_cluster = runtime in {"rke2", "k3s", "k8s"}
    if container_cluster:
        fields = {"namespace", "state_claim", "credentials_secret"}
    else:
        fields = {"state_path", "http_port", "ssh_port", "environment_file"}
        if runtime == "raw":
            fields |= {"binary", "user", "group"}
    if (not isinstance(plan, dict) or set(plan) != common | fields or
            type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not label(plan["name"])):
        raise ValueError("Invalid Gogs plan fields")
    url = plan["https_url"]
    if not isinstance(url, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", url):
        raise ValueError("Set a trusted HTTPS origin")
    origin = urlsplit(url)
    if not dns_name(origin.hostname) or origin.port == 0:
        raise ValueError("Invalid HTTPS origin")
    if not dns_name(plan["ssh_host"]) or not port(plan["ssh_public_port"]):
        raise ValueError("Invalid advertised SSH endpoint")

    database = plan["database"]
    if not isinstance(database, dict):
        raise ValueError("Select a database backend")
    if database == {"type": "sqlite3"}:
        pass
    elif set(database) != {"type", "host", "port", "name", "user", "ca"} or database["type"] != "postgres":
        raise ValueError("Use SQLite or an explicitly configured PostgreSQL database")
    elif (not dns_name(database["host"]) or not port(database["port"]) or
          any(not isinstance(database[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database[key])
              for key in ("name", "user")) or
          not (label(database["ca"]) if container_cluster else host_path(database["ca"]))):
        raise ValueError("Invalid PostgreSQL connection or CA reference")

    if container_cluster:
        if (any(not label(plan[key]) for key in ("namespace", "state_claim", "credentials_secret")) or
                plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}):
            raise ValueError("Use a dedicated namespace, PVC and credentials Secret")
        return

    state = plan["state_path"]
    if (not host_path(state) or not state.startswith(("/srv/", "/var/lib/")) or
            not port(plan["http_port"], 1024) or not port(plan["ssh_port"], 1024) or
            plan["http_port"] == plan["ssh_port"]):
        raise ValueError("Use a dedicated state directory and separate unprivileged ports")
    protected = [plan["environment_file"]]
    if database["type"] == "postgres":
        protected.append(database["ca"])
    if runtime == "raw":
        protected.append(plan["binary"])
        if any(not isinstance(plan[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", plan[key])
               or plan[key] == "root" for key in ("user", "group")):
            raise ValueError("Use a dedicated non-root account")
    if any(not host_path(path) or path == state or path.startswith(state + "/") for path in protected):
        raise ValueError("Keep runtime artifacts and credential files outside the state directory")


def configuration(plan, runtime):
    native = runtime == "raw"
    home = plan["state_path"] if native else STATE
    bind = "127.0.0.1" if native else "0.0.0.0"
    http = plan["http_port"] if native else HTTP_PORT
    ssh = plan["ssh_port"] if native else SSH_PORT
    user = plan["user"] if native else "git"
    database = plan["database"]
    if database["type"] == "postgres":
        backend = (f"TYPE = postgres\nHOST = {database['host']}:{database['port']}\n"
                   f"NAME = {database['name']}\nUSER = {database['user']}\n"
                   f"PASSWORD = ${{{DATABASE_ENV}}}\nSSL_MODE = verify-full\n")
    else:
        backend = f"TYPE = sqlite3\nPATH = {home}/data/gogs.db\n"
    return (
        f"RUN_USER = {user}\nRUN_MODE = prod\n\n"
        f"[server]\nPROTOCOL = http\nHTTP_ADDR = {bind}\nHTTP_PORT = {http}\n"
        f"EXTERNAL_URL = {plan['https_url']}/\nAPP_DATA_PATH = {home}/data\n"
        f"SSH_DOMAIN = {plan['ssh_host']}\nSSH_PORT = {plan['ssh_public_port']}\n"
        f"START_SSH_SERVER = true\nSSH_LISTEN_HOST = {bind}\nSSH_LISTEN_PORT = {ssh}\n"
        f"\n[repository]\nROOT = {home}/repositories\nFORCE_PRIVATE = true\n"
        f"\n[database]\n{backend}"
        f"\n[security]\nINSTALL_LOCK = true\nSECRET_KEY = ${{{SECRET_ENV}}}\nCOOKIE_SECURE = true\n"
        "\n[session]\nCOOKIE_SECURE = true\n"
        f"\n[auth]\nDISABLE_REGISTRATION = true\nREQUIRE_SIGNIN_VIEW = true\n"
        f"\n[log]\nMODE = console\nLEVEL = Info\nROOT_PATH = {home}/log\n"
    )


def launch(plan, binary):
    required = [SECRET_ENV]
    if plan["database"]["type"] == "postgres":
        required.append(DATABASE_ENV)
    checks = ['test -s "$GOGS_CUSTOM/conf/app.ini"']
    checks.extend(f'test -n "${{{name}:-}}"' for name in required)
    guards = " && ".join(checks)
    arguments = [binary, "web"]
    if binary == "/app/gogs/gogs":
        arguments.insert(0, "/app/gogs/start.sh")
    return ["/bin/sh", "-ec", guards + ' && exec "$@"', "gogs", *arguments]


def kubernetes_objects(plan):
    name = plan["name"]
    metadata = {"name": name, "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "gogs", "app.kubernetes.io/instance": name}
    selector = {"matchLabels": labels}
    ports = [{"name": "http", "containerPort": HTTP_PORT}, {"name": "ssh", "containerPort": SSH_PORT}]
    env = [{"name": "GOGS_CUSTOM", "value": STATE + "/gogs"}]
    volumes = [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
               {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]
    mounts = [{"name": "state", "mountPath": STATE}, {"name": "tmp", "mountPath": "/tmp"}]
    credentials = [(SECRET_ENV, "secret-key")]
    database = plan["database"]
    if database["type"] == "postgres":
        credentials.append((DATABASE_ENV, "database-password"))
        env.append({"name": "PGSSLROOTCERT", "value": CA_PATH})
        volumes.append({"name": "postgres-ca", "secret": {"secretName": database["ca"],
                        "items": [{"key": "ca.crt", "path": "postgres-ca.crt"}]}})
        mounts.append({"name": "postgres-ca", "mountPath": "/etc/gogs", "readOnly": True})
    for variable, key in credentials:
        env.append({"name": variable, "valueFrom": {"secretKeyRef": {"name": plan["credentials_secret"], "key": key}}})
    pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "gogs", "image": IMAGE, "command": launch(plan, "/app/gogs/gogs"),
                        "env": env, "ports": ports, "volumeMounts": mounts,
                        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                        "startupProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 3, "failureThreshold": 60},
                        "readinessProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 5},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}}}],
        "volumes": volumes,
    }
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"type": "ClusterIP", "selector": labels,
                  "ports": [{"name": item["name"], "port": item["containerPort"], "targetPort": item["name"]}
                            for item in ports]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    bind = {"create_host_path": False}
    volumes = [{"type": "bind", "source": plan["state_path"], "target": STATE, "bind": bind}]
    env = {"GOGS_CUSTOM": STATE + "/gogs"}
    if plan["database"]["type"] == "postgres":
        env["PGSSLROOTCERT"] = CA_PATH
        volumes.append({"type": "bind", "source": plan["database"]["ca"], "target": CA_PATH,
                        "read_only": True, "bind": bind})
    return {"services": {plan["name"]: {
        "image": IMAGE, "entrypoint": [part.replace("$", "$$") for part in launch(plan, "/app/gogs/gogs")],
        "command": [], "user": "1000:1000", "read_only": True, "restart": "unless-stopped",
        "env_file": [{"path": plan["environment_file"], "format": "raw"}], "environment": env,
        "ports": [f"127.0.0.1:{plan['http_port']}:{HTTP_PORT}", f"127.0.0.1:{plan['ssh_port']}:{SSH_PORT}"],
        "volumes": volumes, "tmpfs": ["/tmp:rw,nosuid,nodev,size=256m,mode=1777"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "mem_limit": "1g", "pids_limit": 256,
    }}}


def native_files(plan):
    state, binary = plan["state_path"], plan["binary"]
    shell, flag, guard, *arguments = launch(plan, binary)
    exec_start = f"{shell} {flag} '{guard.replace('$', '$$')}' {' '.join(arguments)}"
    ca = f"Environment=PGSSLROOTCERT={plan['database']['ca']}\n" if plan["database"]["type"] == "postgres" else ""
    unit = (
        "[Unit]\nDescription=Gogs\nAfter=network.target\n\n[Service]\n"
        f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={state}\n"
        f"Environment=HOME={state} GOGS_CUSTOM={state}/gogs GOGS_WORK_DIR={state}\n{ca}"
        f"EnvironmentFile={plan['environment_file']}\n"
        f"ExecStartPre=/usr/bin/sha256sum --check --status {binary}.sha256\n"
        f"ExecStart={exec_start}\n"
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
        f"ReadWritePaths={state}\nRestart=on-failure\nRestartSec=5s\nMemoryMax=1G\nTasksMax=256\n"
        "\n[Install]\nWantedBy=multi-user.target\n"
    )
    return {plan["name"] + ".service": unit, "gogs.sha256": f"{BINARY_SHA256}  {binary}\n"}


def render(plan, runtime):
    validate(plan, runtime)
    files = {"app.ini": configuration(plan, runtime)}
    if runtime == "raw":
        files.update(native_files(plan))
    elif runtime == "docker":
        files["compose.yaml"] = yaml.safe_dump(compose(plan), sort_keys=False)
    else:
        files["gogs.yaml"] = yaml.safe_dump_all(kubernetes_objects(plan), sort_keys=False)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=16384, encoding="utf-8"))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (OSError, ValueError):
        print("Gogs rendering failed. Check the runtime plan and select a new private output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"component": "gogs", "version": VERSION, "runtime": args.runtime, "rendered": True, "deployed": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
