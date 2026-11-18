#!/usr/bin/env python3
"""Render a single-active Woodpecker server with separately hosted agents."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

from bounded_file import read_bounded_text
from render_forge_host import ACCOUNT, port, private_path
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CONTAINER_STATE = "/var/lib/woodpecker"
CONTAINER_INPUTS = "/run/platform/woodpecker"


def https_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[1-9][0-9]{0,4})?", value):
        return False
    url = urlsplit(value)
    return dns_name(url.hostname) and (url.port is None or port(url.port))


def validate(plan):
    common = {"schema_version", "runtime", "name", "provider", "forge_url", "https_url", "admins",
              "state_path", "inputs_path", "http_port", "grpc_port", "database", "custom_https_ca", "clone_image"}
    if not isinstance(plan, dict) or plan.get("runtime") not in ("docker", "raw"):
        raise ValueError("Select Docker or native Linux")
    native = plan["runtime"] == "raw"
    specific = {"binary", "binary_sha256", "user", "group"} if native else {"image"}
    if (set(plan) != common | specific or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not label(plan["name"]) or plan["provider"] not in ("forgejo", "gitea")):
        raise ValueError("Invalid server plan")
    if (not all(https_origin(plan[key]) for key in ("forge_url", "https_url"))
            or plan["https_url"] == plan["forge_url"]):
        raise ValueError("Supply distinct HTTPS origins")
    admins = plan["admins"]
    if (not isinstance(admins, list) or not 1 <= len(admins) <= 32
            or any(not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", login)
                   for login in admins) or len(set(admins)) != len(admins)):
        raise ValueError("List explicit Woodpecker administrator logins")
    state, inputs = plan["state_path"], plan["inputs_path"]
    if (not private_path(state) or not state.startswith(("/srv/", "/var/lib/"))
            or not private_path(inputs) or state == inputs or state.startswith(inputs + "/")
            or inputs.startswith(state + "/") or (native and inputs.startswith("/var/tmp/"))):
        raise ValueError("Supply distinct state and input paths")
    if (not all(port(plan[key], 1024) for key in ("http_port", "grpc_port"))
            or plan["http_port"] == plan["grpc_port"]):
        raise ValueError("Select separate unprivileged listener ports")
    if plan["database"] not in ("sqlite3", "postgres") or type(plan["custom_https_ca"]) is not bool:
        raise ValueError("Select database and trust settings")
    clone = plan["clone_image"]
    if clone is not None and (not isinstance(clone, str) or not IMAGE.fullmatch(clone)):
        raise ValueError("Pin a custom clone image by digest")
    if native:
        binary = plan["binary"]
        if (not private_path(binary) or binary.startswith("/var/tmp/") or any(
                binary == path or binary.startswith(path + "/") for path in (state, inputs))
                or not isinstance(plan["binary_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", plan["binary_sha256"])
                or any(not isinstance(plan[key], str) or not ACCOUNT.fullmatch(plan[key]) or plan[key] == "root"
                       for key in ("user", "group"))):
            raise ValueError("Use a checksum-pinned executable and a dedicated account")
    elif not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("Pin the server image by digest")


def server_environment(plan, state, inputs, http, grpc):
    provider = "WOODPECKER_" + plan["provider"].upper()
    settings = {
        "HOME": state, "XDG_DATA_HOME": state, "XDG_CACHE_HOME": state,
        "WOODPECKER_HOST": plan["https_url"], "WOODPECKER_ADMIN": ",".join(plan["admins"]),
        "WOODPECKER_OPEN": "false", "WOODPECKER_PLUGINS_PRIVILEGED": "",
        "WOODPECKER_SERVER_ADDR": http, "WOODPECKER_GRPC_ADDR": grpc,
        "WOODPECKER_DATABASE_DRIVER": plan["database"],
        "WOODPECKER_AGENT_SECRET_FILE": inputs + "/agent-secret",
        provider: "true", provider + "_URL": plan["forge_url"],
        provider + "_CLIENT_FILE": inputs + "/oauth-client-id",
        provider + "_SECRET_FILE": inputs + "/oauth-client-secret",
    }
    if plan["database"] == "postgres":
        settings["WOODPECKER_DATABASE_DATASOURCE_FILE"] = inputs + "/database-datasource"
    else:
        settings["WOODPECKER_DATABASE_DATASOURCE"] = state + "/woodpecker.sqlite"
    if plan["custom_https_ca"]:
        settings["SSL_CERT_FILE"] = inputs + "/https-ca-bundle.crt"
    if plan["clone_image"] is not None:
        settings["WOODPECKER_DEFAULT_CLONE_PLUGIN"] = plan["clone_image"]
        settings["WOODPECKER_PLUGINS_TRUSTED_CLONE"] = plan["clone_image"]
    return settings


def render(plan):
    validate(plan)
    state, inputs = plan["state_path"], plan["inputs_path"]
    if plan["runtime"] == "docker":
        environment = server_environment(plan, CONTAINER_STATE, CONTAINER_INPUTS, ":8000", ":9000")
        service = {
            "image": plan["image"], "user": "1000:1000", "init": True,
            "read_only": True, "restart": "unless-stopped",
            "environment": environment,
            "ports": [f"127.0.0.1:{plan['http_port']}:8000", f"127.0.0.1:{plan['grpc_port']}:9000"],
            "volumes": [
                {"type": "bind", "source": state, "target": CONTAINER_STATE, "bind": {"create_host_path": False}},
                {"type": "bind", "source": inputs, "target": CONTAINER_INPUTS,
                 "read_only": True, "bind": {"create_host_path": False}},
            ],
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=128m,mode=1777"],
            "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"], "mem_limit": "1g", "pids_limit": 256,
        }
        return {"compose.yaml": yaml.safe_dump({"services": {plan["name"]: service}}, sort_keys=False)}

    settings = server_environment(plan, state, inputs, f"127.0.0.1:{plan['http_port']}", f"127.0.0.1:{plan['grpc_port']}")
    checks = [settings[key] for key in settings if key.endswith("_FILE")]
    binary = plan["binary"]
    service = [
        "[Unit]", "Description=Single-active Woodpecker server", "Wants=network-online.target", "After=network-online.target",
        "", "[Service]", f"User={plan['user']}", f"Group={plan['group']}", f"WorkingDirectory={state}",
        *[f"Environment={key}={value}" for key, value in settings.items()],
        f"ExecStartPre=/usr/bin/sha256sum --check --status {inputs}/binary.sha256",
        *["ExecStartPre=/usr/bin/test -s " + file for file in checks],
        "ExecStart=" + binary, "UMask=0077", "NoNewPrivileges=yes", "PrivateTmp=yes", "PrivateDevices=yes",
        "ProtectSystem=strict", "ProtectHome=yes", f"ReadWritePaths={state}", "CapabilityBoundingSet=",
        "Restart=on-failure", "RestartSec=5s", "MemoryMax=1G", "TasksMax=256",
        "", "[Install]", "WantedBy=multi-user.target", "",
    ]
    return {plan["name"] + ".service": "\n".join(service),
            "binary.sha256": f"{plan['binary_sha256']}  {binary}\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=32768))
        write_bundle(render(plan), args.output_dir, private_root=ROOT / "private")
    except (OSError, ValueError):
        print("Woodpecker host rendering failed. Review the private plan and select a new private output directory.")
        return 1
    print(json.dumps({"rendered": True, "runtime": plan["runtime"], "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
