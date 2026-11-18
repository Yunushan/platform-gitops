#!/usr/bin/env python3
"""Render a single-active Forgejo or Gitea host with a private backend."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from materialize_patroni import read_contract as read_postgres_contract
from postgres_ha_policy import application_databases
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from render_jenkins_host import host_path
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CONTAINER_STATE = "/var/lib/gitea"
CONTAINER_INPUTS = "/run/platform/forge"
HTTP_PORT, SSH_PORT = 3000, 2222
PROVIDERS = ("forgejo", "gitea")
RUNTIMES = ("docker", "raw")
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
SQL_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def port(value, minimum=1):
    return type(value) is int and minimum <= value <= 65535


def private_path(value):
    return host_path(value) and value.startswith(("/etc/", "/run/", "/srv/", "/var/lib/", "/var/tmp/", "/opt/"))


def webhook_host(value, provider):
    if not isinstance(value, str):
        return False
    host, separator, number = value.partition(":")
    if not dns_name(host) or "." not in host:
        return False
    return not separator or (
        provider == "gitea" and bool(re.fullmatch(r"[1-9][0-9]{0,4}", number)) and port(int(number))
    )


def validate(plan):
    common = {"schema_version", "provider", "runtime", "name", "state_path", "inputs_path", "https_url",
              "ssh_host", "ssh_public_port", "http_port", "ssh_port", "database", "webhook_hosts", "custom_https_ca"}
    if not isinstance(plan, dict) or plan.get("runtime") not in RUNTIMES or plan.get("provider") not in PROVIDERS:
        raise ValueError("Select a host runtime and supported forge")
    native = plan["runtime"] == "raw"
    implementation = {"binary", "binary_sha256", "user", "group"} if native else {"image"}
    if (set(plan) != common | implementation or type(plan["schema_version"]) is not int
            or plan["schema_version"] != 1 or not label(plan["name"])):
        raise ValueError("Invalid host plan fields")
    state, inputs = (plan[key] for key in ("state_path", "inputs_path"))
    if (not host_path(state) or not state.startswith(("/srv/", "/var/lib/"))
            or not private_path(inputs) or state == inputs
            or state.startswith(inputs + "/") or inputs.startswith(state + "/")):
        raise ValueError("Use distinct private inputs and persistent state paths")
    https_url = plan["https_url"]
    if not isinstance(https_url, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", https_url):
        raise ValueError("Supply a canonical HTTPS origin")
    url = urlsplit(https_url)
    if not dns_name(url.hostname) or url.port == 0:
        raise ValueError("Invalid HTTPS origin")
    if (not dns_name(plan["ssh_host"]) or not port(plan["ssh_public_port"])
            or not all(port(plan[key], 1024) for key in ("http_port", "ssh_port"))
            or plan["http_port"] == plan["ssh_port"]):
        raise ValueError("Select separate unprivileged listeners and an advertised SSH endpoint")
    hooks = plan["webhook_hosts"]
    if (not isinstance(hooks, list) or len(hooks) > 32
            or any(not webhook_host(host, plan["provider"]) for host in hooks) or len(set(hooks)) != len(hooks)
            or type(plan["custom_https_ca"]) is not bool):
        raise ValueError("List exact webhook hosts (optionally with a single port for Gitea) and select HTTPS trust")
    database = plan["database"]
    if database != {"type": "sqlite3"}:
        if (not isinstance(database, dict) or set(database) != {"type", "host", "port", "name", "user"}
                or database["type"] != "postgres" or not dns_name(database["host"]) or not port(database["port"])
                or any(not isinstance(database[key], str) or not SQL_NAME.fullmatch(database[key])
                       for key in ("name", "user"))):
            raise ValueError("Select SQLite or a specific PostgreSQL endpoint and identity")
    if native:
        binary = plan["binary"]
        if (not private_path(binary) or any(value.startswith("/var/tmp/") for value in (binary, inputs))
                or any(binary == path or binary.startswith(path + "/") for path in (state, inputs))
                or not isinstance(plan["binary_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["binary_sha256"])
                or any(not isinstance(plan[key], str) or not ACCOUNT.fullmatch(plan[key]) or plan[key] == "root"
                       for key in ("user", "group"))):
            raise ValueError("Use a reviewed executable and a dedicated non-root identity")
    elif not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("Use a digest-pinned rootless image")


def runtime_paths(plan):
    if plan["runtime"] == "raw":
        return plan["state_path"], plan["inputs_path"]
    return CONTAINER_STATE, CONTAINER_INPUTS


def configuration(plan):
    state, inputs = runtime_paths(plan)
    native = plan["runtime"] == "raw"
    http_port = plan["http_port"] if native else HTTP_PORT
    ssh_port = plan["ssh_port"] if native else SSH_PORT
    address = "127.0.0.1" if native else "0.0.0.0"
    sections = {
        "server": {
            "DOMAIN": urlsplit(plan["https_url"]).hostname,
            "ROOT_URL": plan["https_url"] + "/",
            "PROTOCOL": "http",
            "HTTP_ADDR": address,
            "HTTP_PORT": http_port,
            "LOCAL_ROOT_URL": f"http://127.0.0.1:{http_port}/",
            "APP_DATA_PATH": state + "/data",
            "DISABLE_SSH": "false",
            "START_SSH_SERVER": "true",
            "BUILTIN_SSH_SERVER_USER": "git",
            "SSH_USER": "git",
            "SSH_DOMAIN": plan["ssh_host"],
            "SSH_PORT": plan["ssh_public_port"],
            "SSH_LISTEN_HOST": address,
            "SSH_LISTEN_PORT": ssh_port,
            "SSH_SERVER_HOST_KEYS": state + "/data/ssh/gitea.ed25519",
            "SSH_CREATE_AUTHORIZED_KEYS_FILE": "false",
            "OFFLINE_MODE": "true",
        },
        "repository": {"ROOT": state + "/repositories"},
        "security": {
            "INSTALL_LOCK": "true", "DISABLE_GIT_HOOKS": "true",
            "SECRET_KEY_URI": "file:" + inputs + "/secret-key",
            "INTERNAL_TOKEN_URI": "file:" + inputs + "/internal-token",
        },
        "session": {"COOKIE_SECURE": "true"},
        "service": {"DISABLE_REGISTRATION": "true", "REQUIRE_SIGNIN_VIEW": "true"},
        "log": {"MODE": "console", "LEVEL": "Info", "ROOT_PATH": state + "/log"},
    }
    database = plan["database"]
    if database["type"] == "postgres":
        sections["database"] = {
            "DB_TYPE": "postgres", "HOST": f"{database['host']}:{database['port']}",
            "NAME": database["name"], "USER": database["user"], "SSL_MODE": "verify-full",
        }
    else:
        sections["database"] = {"DB_TYPE": "sqlite3", "PATH": state + "/data/forge.db"}
    if plan["provider"] == "forgejo":
        sections["webhook"] = {"ALLOWED_HOST_LIST": ",".join(plan["webhook_hosts"]), "SKIP_TLS_VERIFY": "false"}
    else:
        sections["server"]["PUBLIC_URL_DETECTION"] = "never"
        sections["security"].update(EGRESS_MODE="strict", ALLOWED_HOST_LIST=",".join(plan["webhook_hosts"]))
        sections["webhook"] = {"SKIP_TLS_VERIFY": "false"}
    pairs = [f"RUN_USER = {plan['user'] if native else 'git'}\nRUN_MODE = prod"]
    for section, options in sections.items():
        pairs.append(f"[{section}]\n" + "\n".join(f"{key} = {value}" for key, value in options.items()))
    return "\n\n".join(pairs) + "\n"


def launch(plan):
    state, inputs = runtime_paths(plan)
    binary = plan.get("binary", "/usr/local/bin/" + plan["provider"])
    checks = [state + "/config/app.ini", inputs + "/secret-key", inputs + "/internal-token"]
    environment = {"HOME": state}
    if plan["database"]["type"] == "postgres":
        environment.update(PGSSLROOTCERT=inputs + "/postgres-ca.crt", PGPASSFILE=inputs + "/pgpass")
        checks.extend((environment["PGSSLROOTCERT"], environment["PGPASSFILE"]))
    if plan["custom_https_ca"]:
        environment["SSL_CERT_FILE"] = inputs + "/https-ca-bundle.crt"
        checks.append(environment["SSL_CERT_FILE"])
    command = [binary, "--config", checks[0], "--work-path", state, "--custom-path", state + "/custom", "web"]
    return checks, environment, command


def compose(plan, postgres_ca=None):
    checks, environment, command = launch(plan)
    guard = " && ".join("test -s " + file for file in checks)
    if postgres_ca:
        guard += f" && printf '%s  %s\\n' {postgres_ca} {environment['PGSSLROOTCERT']} | sha256sum -c -"
    service = {
        "image": plan["image"], "user": "1000:1000", "read_only": True,
        "init": True,
        # The rootless images can set TMPDIR below /tmp. The tmpfs hides
        # image-layer directories, so choose its writable root explicitly.
        "entrypoint": ["/bin/sh", "-ec", guard + ' && exec "$$@"', "forge", *command],
        "command": [], "restart": "unless-stopped", "environment": {**environment, "TMPDIR": "/tmp"},
        "ports": [f"127.0.0.1:{plan['http_port']}:{HTTP_PORT}", f"127.0.0.1:{plan['ssh_port']}:{SSH_PORT}"],
        "volumes": [
            {"type": "bind", "source": plan["state_path"], "target": CONTAINER_STATE, "bind": {"create_host_path": False}},
            {"type": "bind", "source": plan["inputs_path"], "target": CONTAINER_INPUTS,
             "read_only": True, "bind": {"create_host_path": False}},
        ],
        "tmpfs": ["/tmp:rw,nosuid,nodev,size=256m,mode=1777"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 256, "mem_limit": "2g",
    }
    if plan["provider"] == "gitea":
        # The upstream rootless image declares /etc/gitea as a VOLUME.
        # Keep it explicitly read-only so Docker cannot attach writable storage.
        service["volumes"].append({"type": "bind", "source": plan["inputs_path"], "target": "/etc/gitea",
                                   "read_only": True, "bind": {"create_host_path": False}})
    return yaml.safe_dump({"services": {plan["name"]: service}}, sort_keys=False)


def native_files(plan, postgres_ca=None):
    checks, environment, command = launch(plan)
    state, binary = (plan[key] for key in ("state_path", "binary"))
    files = {"binary.sha256": f"{plan['binary_sha256']}  {binary}\n"}
    verify_ca = []
    if postgres_ca:
        files["postgres-ca.sha256"] = f"{postgres_ca}  {environment['PGSSLROOTCERT']}\n"
        verify_ca = [f"ExecStartPre=/usr/bin/sha256sum --check --status {plan['inputs_path']}/postgres-ca.sha256"]
    service = [
        "[Unit]", "Description=Single-active " + plan["provider"], "Wants=network-online.target", "After=network-online.target",
        "", "[Service]", f"User={plan['user']}", f"Group={plan['group']}", f"WorkingDirectory={state}",
        *[f"Environment={key}={value}" for key, value in environment.items()],
        f"ExecStartPre=/usr/bin/sha256sum --check --status {plan['inputs_path']}/binary.sha256",
        *["ExecStartPre=/usr/bin/test -s " + file for file in checks],
        *verify_ca,
        "ExecStart=" + " ".join(command), "UMask=0077", "NoNewPrivileges=yes", "PrivateTmp=yes", "PrivateDevices=yes",
        "ProtectSystem=strict", "ProtectHome=yes", f"ReadWritePaths={state}", "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", "Restart=on-failure", "RestartSec=5s",
        "MemoryMax=2G", "TasksMax=256", "", "[Install]", "WantedBy=multi-user.target", "",
    ]
    files[plan["name"] + ".service"] = "\n".join(service)
    return files


def reviewed_postgres_ca(plan, bundle, digest):
    database = plan["database"]
    contract = read_postgres_contract(bundle, digest)["plan"]
    endpoint = contract.get("write_endpoint")
    if database["type"] != "postgres" or endpoint is None:
        raise ValueError("Select the reviewed Patroni write endpoint for this forge")
    if (database["host"], database["port"]) != (endpoint["host"], endpoint["port"]):
        raise ValueError("Forge PostgreSQL address differs from the reviewed write endpoint")
    if not any((app["database"], app["owner"]) == (database["name"], database["user"])
               for app in application_databases(contract)):
        raise ValueError("Forge PostgreSQL identity is absent from the reviewed applications")
    return contract["tls"]["ca_sha256"]["postgres"]


def verify_upgrade_plan(previous, target):
    validate(previous)
    executable = {"binary", "binary_sha256"} if target["runtime"] == "raw" else {"image"}
    if any(previous.get(key) != target[key] for key in target.keys() - executable):
        raise ValueError("A host forge upgrade must preserve the deployment settings")
    if target["runtime"] == "raw":
        if any(previous[key] == target[key] for key in executable):
            raise ValueError("Stage the new forge executable at a separate path with its own digest")
    elif previous["image"].rsplit("@", 1)[-1] == target["image"].rsplit("@", 1)[-1]:
        raise ValueError("Use a new reviewed image digest for the forge upgrade")


def render(plan, *, postgres_bundle=None, postgres_contract_sha256=None, upgrade_from=None):
    validate(plan)
    if upgrade_from is not None:
        verify_upgrade_plan(upgrade_from, plan)
    if (postgres_bundle is None) != (postgres_contract_sha256 is None):
        raise ValueError("Supply both the PostgreSQL bundle and its reviewed contract digest")
    ca = reviewed_postgres_ca(plan, postgres_bundle, postgres_contract_sha256) if postgres_bundle else None
    files = {"app.ini": configuration(plan)} if upgrade_from is None else {}
    if plan["runtime"] == "raw":
        files.update(native_files(plan, ca))
    else:
        files["compose.yaml"] = compose(plan, ca)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--postgres-bundle", type=Path)
    parser.add_argument("--postgres-contract-sha256")
    parser.add_argument("--upgrade-from", type=Path, help="Render only replacement runtime files against the current plan")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=32768))
        previous = None
        if args.upgrade_from is not None:
            previous = loads_strict_json(read_bounded_text(args.upgrade_from, max_bytes=32768))
            validate(previous)
        files = render(plan, postgres_bundle=args.postgres_bundle,
                       postgres_contract_sha256=args.postgres_contract_sha256, upgrade_from=previous)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("Forge host rendering failed. Review the private plan and select a new private output directory.")
        return 1
    print(json.dumps({"rendered": True, "provider": plan["provider"], "runtime": plan["runtime"], "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
