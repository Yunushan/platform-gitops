#!/usr/bin/env python3
"""Render single-active Jenkins host artifacts; never start or install them."""
import argparse
from pathlib import Path
import re
import sys

import yaml
from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE
from render_gerrit_sites import label
from strict_json import loads_strict_json


def host_path(value):
    return (isinstance(value, str) and len(value) <= 240
            and re.fullmatch(r"/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*", value) is not None
            and all(part not in {".", ".."} for part in value.split("/")))


def render(plan):
    fields = {"schema_version", "runtime", "name", "home", "port", "image", "java", "war", "war_sha256", "checksum_file", "user", "group"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-host-plan")
    if plan["runtime"] not in {"raw", "docker"} or not label(plan["name"]) or len(plan["name"]) > 48:
        raise ValueError("invalid-host-identity")
    if (not host_path(plan["home"]) or plan["home"].split("/")[1] in {"home", "root", "proc", "sys", "dev"}
            or type(plan["port"]) is not int or not 1024 <= plan["port"] <= 65535):
        raise ValueError("invalid-home-or-port")
    if plan["runtime"] == "docker":
        if any(plan[key] is not None for key in ("java", "war", "war_sha256", "checksum_file", "user", "group")):
            raise ValueError("docker-plan-has-native-settings")
        if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]): raise ValueError("digest-pinned-image-required")
        service = {"image": plan["image"], "user": "1000:1000", "read_only": True, "restart": "unless-stopped",
                   "ports": [f"127.0.0.1:{plan['port']}:8080"], "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                   "pids_limit": 512, "mem_limit": "3g", "cpus": 2.0,
                   "tmpfs": ["/tmp:rw,nosuid,nodev,size=2g"],
                   "volumes": [{"type": "bind", "source": plan["home"], "target": "/var/jenkins_home", "bind": {"create_host_path": False}}]}
        return {"compose.yaml": yaml.safe_dump({"services": {plan["name"]: service}}, sort_keys=False)}
    if plan["image"] is not None or any(not host_path(plan[key]) for key in ("java", "war", "checksum_file")):
        raise ValueError("invalid-native-paths")
    if not isinstance(plan["war_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["war_sha256"]):
        raise ValueError("war-digest-required")
    if any(not isinstance(plan[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", plan[key]) or plan[key] == "root" for key in ("user", "group")):
        raise ValueError("dedicated-nonroot-account-required")
    for key in ("java", "war", "checksum_file"):
        if plan[key] == plan["home"] or plan[key].startswith(plan["home"] + "/"):
            raise ValueError("runtime-artifacts-must-be-outside-writable-home")
    service = ("[Unit]\nDescription=Single-active Jenkins controller\nAfter=network.target\n\n[Service]\n"
               f"User={plan['user']}\nGroup={plan['group']}\nEnvironment=JENKINS_HOME={plan['home']}\n"
               "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
               f"ReadWritePaths={plan['home']}\nExecStartPre=/usr/bin/sha256sum --check --status {plan['checksum_file']}\n"
               f"ExecStart={plan['java']} -Djava.awt.headless=true -jar {plan['war']} --httpListenAddress=127.0.0.1 --httpPort={plan['port']}\n"
               "Restart=on-failure\nRestartSec=10s\nTimeoutStopSec=120s\nTasksMax=512\n\n[Install]\nWantedBy=multi-user.target\n")
    return {plan["name"] + ".service": service, "jenkins.war.sha256": plan["war_sha256"] + "  " + plan["war"] + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try: result = render(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
    except Exception:
        print("Jenkins host rendering failed; review the plan.", file=sys.stderr); return 1
    for name, content in result.items():
        print("### " + name); print(content, end="")
    return 0


if __name__ == "__main__": sys.exit(main())
