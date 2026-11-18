#!/usr/bin/env python3
"""Render a single-active OneDev Community server for a prepared runtime."""

import argparse
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
VERSION = "17.2.0"
IMAGE = "docker.io/1dev/server@sha256:beab585e0a3efd74f35052c46732d876181313f692340e498f7eab754b0c3615"
CLUSTER = {"rke2", "k3s", "k8s"}
STATE = "/opt/onedev"
SECRETS = "/run/onedev-secrets"
PORTS = {"http": 6610, "ssh": 6611}
# The upstream wrapper writes its PID and logs beside the source distribution.
# Use a disposable writable copy when the application version changes.
LAUNCH = ["/bin/sh", "-ec", """if ! cmp -s /app/release.properties /opt/onedev/release.properties; then
    source=$(mktemp -d /tmp/onedev-release.XXXXXXXX)
    cp -R --no-preserve=ownership /app/. "$source"
    "$source/bin/upgrade.sh" /opt/onedev
    rm -rf -- "$source"
fi
touch /opt/onedev/IN_DOCKER
exec /opt/onedev/bin/server.sh console
"""]


def valid_port(value, minimum=1):
    return type(value) is int and minimum <= value <= 65535


def validate(plan, runtime):
    cluster = runtime in CLUSTER
    fields = {"namespace", "state_claim", "credentials_secret"} if cluster else {"state_path", "credentials_path", "http_port", "ssh_port"}
    if runtime == "raw":
        fields |= {"user", "group", "java_home"}
    fields |= {"schema_version", "name", "https_url", "ssh_host", "ssh_public_port", "database", "bootstrap"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not label(plan["name"])):
        raise ValueError("Invalid OneDev plan fields")
    url = plan["https_url"]
    if (not isinstance(url, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", url)
            or not dns_name(urlsplit(url).hostname) or urlsplit(url).port == 0):
        raise ValueError("Provide a canonical HTTPS origin")
    if not dns_name(plan["ssh_host"]) or not valid_port(plan["ssh_public_port"]):
        raise ValueError("Provide the advertised SSH host and port")

    database = plan["database"]
    if database != {"type": "embedded"}:
        if (not isinstance(database, dict) or set(database) != {"type", "host", "port", "name", "user"}
                or database["type"] != "postgres" or not dns_name(database["host"]) or not valid_port(database["port"])
                or any(not isinstance(database[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database[key])
                       for key in ("name", "user"))):
            raise ValueError("Select embedded HSQLDB or a prepared PostgreSQL database")

    bootstrap = plan["bootstrap"]
    if bootstrap is not None:
        if (not isinstance(bootstrap, dict) or set(bootstrap) != {"user", "email"}
                or not label(bootstrap["user"]) or not isinstance(bootstrap["email"], str)
                or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}@[a-z0-9.-]{1,190}", bootstrap["email"])
                or not dns_name(bootstrap["email"].split("@", 1)[1])):
            raise ValueError("Set the bootstrap user and email or use the interactive setup")

    credentials = plan["credentials_secret" if cluster else "credentials_path"]
    if database["type"] == "postgres" or bootstrap is not None:
        valid = label(credentials) if cluster else host_path(credentials) and credentials.startswith("/etc/")
        if not valid:
            raise ValueError("Provide the external credential and CA reference")
    elif credentials is not None:
        raise ValueError("Use null credentials for interactive setup with the embedded database")
    if cluster:
        if (not label(plan["namespace"]) or not label(plan["state_claim"])
                or plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}):
            raise ValueError("Use a dedicated namespace and PVC")
    else:
        state = plan["state_path"]
        if (not host_path(state) or not state.startswith(("/srv/", "/var/lib/"))
                or any(not valid_port(plan[key + "_port"], 1024) for key in PORTS)
                or plan["http_port"] == plan["ssh_port"]):
            raise ValueError("Use a dedicated state directory and distinct unprivileged ports")
    if runtime == "raw":
        if not host_path(plan["java_home"]) or not plan["java_home"].startswith(("/opt/", "/usr/")):
            raise ValueError("Specify a Java installation under /opt or /usr")
        for key in ("user", "group"):
            account = plan[key]
            if not isinstance(account, str) or account == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", account):
                raise ValueError("Use a dedicated non-root account")


def environment(plan, runtime):
    native = runtime == "raw"
    home = plan["state_path"] if native else STATE
    credentials = plan["credentials_path"] if native else SECRETS
    settings = {"HOME": home, "http_host": "127.0.0.1" if native else "0.0.0.0", "cluster_ip": "127.0.0.1"}
    settings.update({key + "_port": str(plan[key + "_port"] if native else value) for key, value in PORTS.items()})
    if native:
        java = plan["java_home"]
        settings.update(JAVA_HOME=java, PATH=java + "/bin:/usr/bin:/bin")
    database = plan["database"]
    if database["type"] == "postgres":
        settings.update(
            hibernate_dialect="io.onedev.server.persistence.PostgreSQLDialect",
            hibernate_connection_driver_class="org.postgresql.Driver",
            hibernate_connection_url=(f"jdbc:postgresql://{database['host']}:{database['port']}/{database['name']}"
                                      f"?sslmode=verify-full&sslrootcert={credentials}/ca.crt"),
            hibernate_connection_username=database["user"],
            hibernate_connection_password_file=credentials + "/database-password",
        )
    if plan["bootstrap"] is not None:
        settings.update(initial_user=plan["bootstrap"]["user"], initial_email=plan["bootstrap"]["email"],
                        initial_password_file=credentials + "/initial-password", initial_server_url=plan["https_url"],
                        initial_ssh_root_url=f"ssh://{plan['ssh_host']}:{plan['ssh_public_port']}")
    return settings


def kubernetes_objects(plan):
    metadata = {"name": plan["name"], "namespace": plan["namespace"]}
    labels = {"app.kubernetes.io/name": "onedev", "app.kubernetes.io/instance": plan["name"]}
    selector = {"matchLabels": labels}
    mounts = [{"name": "state", "mountPath": STATE}, {"name": "tmp", "mountPath": "/tmp"}]
    volumes = [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_claim"]}},
               {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    if plan["credentials_secret"] is not None:
        mounts.append({"name": "credentials", "mountPath": SECRETS, "readOnly": True})
        keys = ["ca.crt", "database-password"] if plan["database"]["type"] == "postgres" else []
        if plan["bootstrap"] is not None:
            keys.append("initial-password")
        volumes.append({"name": "credentials", "secret": {"secretName": plan["credentials_secret"], "defaultMode": 0o440,
                        "items": [{"key": key, "path": key} for key in keys]}})
    container = {
        "name": "onedev", "image": IMAGE, "command": LAUNCH, "volumeMounts": mounts,
        "env": [{"name": key, "value": value} for key, value in environment(plan, "k8s").items()],
        "ports": [{"name": key, "containerPort": value} for key, value in PORTS.items()],
        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "3Gi"}},
        "startupProbe": {"httpGet": {"path": "/healthz", "port": "http"},
                         "periodSeconds": 5, "timeoutSeconds": 3, "failureThreshold": 90},
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"},
                           "periodSeconds": 5, "timeoutSeconds": 3},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
    }
    pod = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 90,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container], "volumes": volumes,
    }
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": []}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
         "spec": {"type": "ClusterIP", "selector": labels,
                  "ports": [{"name": key, "port": value, "targetPort": key} for key, value in PORTS.items()]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": selector,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]


def compose(plan):
    volumes = [{"type": "bind", "source": plan["state_path"], "target": STATE, "bind": {"create_host_path": False}}]
    if plan["credentials_path"] is not None:
        volumes.append({"type": "bind", "source": plan["credentials_path"], "target": SECRETS,
                        "read_only": True, "bind": {"create_host_path": False}})
    return {"services": {plan["name"]: {
        "image": IMAGE, "entrypoint": [item.replace("$", "$$") for item in LAUNCH], "command": [],
        "user": "1000:1000", "read_only": True, "restart": "unless-stopped", "stop_grace_period": "90s",
        "environment": environment(plan, "docker"), "volumes": volumes,
        "ports": [f"127.0.0.1:{plan[key + '_port']}:{port}" for key, port in PORTS.items()],
        "tmpfs": ["/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "mem_limit": "3g", "pids_limit": 512,
    }}}


def native_unit(plan):
    state = plan["state_path"]
    env = "".join(f'Environment="{key}={value}"\n' for key, value in environment(plan, "raw").items())
    return (
        "[Unit]\nDescription=OneDev Community\nAfter=network.target\n\n[Service]\n"
        f"User={plan['user']}\nGroup={plan['group']}\nWorkingDirectory={state}\n{env}"
        f"ExecStart={state}/bin/server.sh console\n"
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
        f"ReadWritePaths={state}\nRestart=on-failure\nRestartSec=5s\nTimeoutStopSec=90s\nMemoryMax=3G\nTasksMax=512\n"
        "\n[Install]\nWantedBy=multi-user.target\n"
    )


def render(plan, runtime):
    validate(plan, runtime)
    if runtime == "raw":
        return {plan["name"] + ".service": native_unit(plan)}
    if runtime == "docker":
        return {"compose.yaml": yaml.safe_dump(compose(plan), sort_keys=False)}
    return {"onedev.yaml": yaml.safe_dump_all(kubernetes_objects(plan), sort_keys=False)}


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
        print(f"OneDev render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
