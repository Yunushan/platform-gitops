#!/usr/bin/env python3
"""Render a dedicated Woodpecker Docker executor as Compose or a native service."""

import argparse
import json
from pathlib import Path
import re

import yaml

from bounded_file import read_bounded_text
from render_forge_host import ACCOUNT, port, private_path
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
COMMAND = ["agent", "--grpc-secure=true", "--grpc-skip-insecure=false"]
CONTAINER_STATE = "/var/lib/woodpecker-agent"
CONTAINER_INPUTS = "/run/platform/woodpecker-agent"
CONTAINER_SOCKET = "/run/woodpecker-engine.sock"


def disjoint(left, right):
    return left != right and not left.startswith(right + "/") and not right.startswith(left + "/")


def validate(plan):
    if not isinstance(plan, dict) or plan.get("runtime") not in ("docker", "raw"):
        raise ValueError("Select Compose or native Linux hosting")
    raw = plan["runtime"] == "raw"
    common = {
        "schema_version", "runtime", "name", "server", "state_path", "inputs_path", "docker_socket",
        "custom_https_ca",
    }
    specific = {"binary", "binary_sha256", "user", "group", "socket_group"} if raw else {"image", "socket_gid"}
    if (set(plan) != common | specific or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not label(plan["name"]) or type(plan["custom_https_ca"]) is not bool):
        raise ValueError("Use an explicit agent plan")
    server = plan["server"]
    if not isinstance(server, str):
        raise ValueError("Use a TLS server name and port")
    host, separator, number = server.partition(":")
    if not separator or not dns_name(host) or not re.fullmatch(r"[1-9][0-9]{0,4}", number) or not port(int(number)):
        raise ValueError("Use a TLS server name and port")
    state, inputs, socket = (plan[key] for key in ("state_path", "inputs_path", "docker_socket"))
    if (not all(private_path(path) for path in (state, inputs, socket))
            or not state.startswith(("/srv/", "/var/lib/")) or not inputs.startswith(("/etc/", "/run/"))
            or not socket.startswith("/run/") or not socket.endswith(".sock")
            or not all(disjoint(left, right) for left, right in ((state, inputs), (state, socket), (inputs, socket)))):
        raise ValueError("Use independent state, credential, and executor socket paths")
    if raw:
        binary = plan["binary"]
        if (not private_path(binary) or not binary.startswith("/opt/")
                or not all(disjoint(binary, path) for path in (state, inputs, socket))
                or not isinstance(plan["binary_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["binary_sha256"])
                or any(not isinstance(plan[key], str) or not ACCOUNT.fullmatch(plan[key]) or plan[key] == "root"
                       for key in ("user", "group", "socket_group"))):
            raise ValueError("Use a checksum-pinned agent and dedicated accounts")
    elif (not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"])
          or type(plan["socket_gid"]) is not int or not 1 <= plan["socket_gid"] <= 2147483647):
        raise ValueError("Use a digest-pinned agent and explicit non-root executor socket group")


def environment(plan, state, inputs, socket):
    settings = {
        "HOME": state,
        "WOODPECKER_HOSTNAME": plan["name"],
        "WOODPECKER_SERVER": plan["server"],
        "WOODPECKER_AGENT_CONFIG_FILE": state + "/agent.conf",
        "WOODPECKER_AGENT_SECRET_FILE": inputs + "/agent-secret",
        "WOODPECKER_BACKEND": "docker",
        "WOODPECKER_BACKEND_DOCKER_HOST": "unix://" + socket,
        "WOODPECKER_HEALTHCHECK": "false",
        "WOODPECKER_MAX_WORKFLOWS": "1",
    }
    if plan["custom_https_ca"]:
        settings["SSL_CERT_FILE"] = inputs + "/https-ca-bundle.crt"
    return settings


def render(plan):
    validate(plan)
    name, state, inputs, socket = (plan[key] for key in ("name", "state_path", "inputs_path", "docker_socket"))
    if plan["runtime"] == "docker":
        settings = environment(plan, CONTAINER_STATE, CONTAINER_INPUTS, CONTAINER_SOCKET)
        volumes = [
            {"type": "bind", "source": source, "target": target, "read_only": read_only,
             "bind": {"create_host_path": False}}
            for source, target, read_only in (
                (state, CONTAINER_STATE, False), (inputs, CONTAINER_INPUTS, True), (socket, CONTAINER_SOCKET, True)
            )
        ]
        service = {
            "image": plan["image"], "command": COMMAND, "user": "1000:1000", "group_add": [str(plan["socket_gid"])],
            "init": True, "read_only": True, "restart": "unless-stopped", "environment": settings, "volumes": volumes,
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=128m,mode=1777"], "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"], "mem_limit": "1g", "pids_limit": 256,
        }
        return {"compose.yaml": yaml.safe_dump({"services": {name: service}}, sort_keys=False)}

    settings = environment(plan, state, inputs, socket)
    binary = plan["binary"]
    checks = [settings["WOODPECKER_AGENT_SECRET_FILE"]]
    if plan["custom_https_ca"]:
        checks.append(settings["SSL_CERT_FILE"])
    service = [
        "[Unit]", "Description=Woodpecker job agent", "Wants=network-online.target", "After=network-online.target",
        "", "[Service]", f"User={plan['user']}", f"Group={plan['group']}", f"SupplementaryGroups={plan['socket_group']}",
        f"WorkingDirectory={state}", *[f"Environment={key}={value}" for key, value in settings.items()],
        f"ExecStartPre=/usr/bin/sha256sum --check --status {inputs}/binary.sha256",
        *["ExecStartPre=/usr/bin/test -s " + path for path in checks], f"ExecStartPre=/usr/bin/test -S {socket}",
        "ExecStart=" + " ".join([binary, *COMMAND]), "UMask=0077", "NoNewPrivileges=yes", "PrivateTmp=yes",
        "PrivateDevices=yes", "ProtectSystem=strict", "ProtectHome=yes", f"ReadWritePaths={state}",
        "CapabilityBoundingSet=", "Restart=on-failure", "RestartSec=5s", "MemoryMax=1G", "TasksMax=256",
        "", "[Install]", "WantedBy=multi-user.target", "",
    ]
    return {name + ".service": "\n".join(service), "binary.sha256": f"{plan['binary_sha256']}  {binary}\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=32768))
        write_bundle(render(plan), args.output_dir, private_root=ROOT / "private")
    except (OSError, ValueError):
        print("Woodpecker agent rendering failed. Review the plan and select a new private output directory.")
        return 1
    print(json.dumps({"rendered": True, "runtime": plan["runtime"], "backend": "docker", "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
