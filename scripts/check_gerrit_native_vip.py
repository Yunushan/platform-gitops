#!/usr/bin/env python3
"""Check a native Gerrit entry host before starting HAProxy and Keepalived."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import ssl
import sys

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from render_gerrit_router import CA_FILE, NATIVE_CONFIG, render
from start_gateway_edge import trusted_paths
from strict_json import loads_strict_json


def command(argv, *, environment=None):
    return run_bounded(argv, env=environment or {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                       cwd="/", timeout=15, output_max_bytes=65536)


def selected_interface(host, vip, inventory):
    if not isinstance(inventory, list):
        return False
    matches = [row for row in inventory if isinstance(row, dict)
               and row.get("ifname") == host["interface"] and "UP" in row.get("flags", [])]
    subnet = ipaddress.IPv4Interface(vip).network
    return len(matches) == 1 and any(
        entry.get("family") == "inet" and entry.get("local") == host["address"]
        and entry.get("prefixlen") == subnet.prefixlen
        and subnet == ipaddress.IPv4Interface(f"{host['address']}/{entry['prefixlen']}").network
        for entry in matches[0].get("addr_info", [])
    )


def daemon_configs(haproxy, keepalived, hp_config, vrrp_config, *, haproxy_user, execute):
    commands = {
        "haproxy_config_valid": ["setpriv", "--reuid", str(haproxy_user.pw_uid),
                                  "--regid", str(haproxy_user.pw_gid), "--clear-groups",
                                  "--no-new-privs", str(haproxy), "-c", "-f", str(hp_config)],
        "keepalived_config_valid": [str(keepalived), "--config-test", "--vrrp", "--use-file", str(vrrp_config)],
    }
    return {key: execute(argv).returncode == 0 for key, argv in commands.items()}


def host_files(plan, host_name, config, units):
    files = render(plan, "raw")
    if f"keepalived-{host_name}.conf" not in files:
        raise ValueError("Select the declared native VIP host")
    return {
        config / "haproxy.cfg": files["haproxy.cfg"],
        config / "gateway_edge_health.py": files["gateway_edge_health.py"],
        config / "gerrit-router-vrrp.conf": files[f"keepalived-{host_name}.conf"],
        units / "gerrit-router.service": files["gerrit-router.service"],
        units / "gerrit-router-vrrp.service": files["gerrit-router-vrrp.service"],
    }


def check_interface(plan, host_name, execute):
    hosts = plan.get("native_vip", {}).get("entry_hosts", [])
    host = next((item for item in hosts if item["name"] == host_name), None)
    if host is None:
        raise ValueError("Select the declared native VIP host")
    ip = execute(["ip", "-j", "-4", "addr", "show", "dev", host["interface"]])
    if ip.returncode or not selected_interface(host, plan["native_vip"]["vip"], loads_strict_json(ip.stdout)):
        raise ValueError("Host interface does not match the plan")


def check(plan, host_name, ca, *,
          config=Path(NATIVE_CONFIG), units=Path("/etc/systemd/system"),
          haproxy=Path("/usr/sbin/haproxy"), keepalived=Path("/usr/sbin/keepalived"),
          haproxy_user=None, execute=command):
    installed = host_files(plan, host_name, config, units)
    check_interface(plan, host_name, execute)

    if haproxy_user is None:
        import pwd

        haproxy_user = pwd.getpwnam("haproxy")
    if haproxy_user.pw_uid == 0 or haproxy_user.pw_gid == 0:
        raise ValueError("Expected an unprivileged HAProxy identity")
    ca_path = config / Path(CA_FILE).relative_to(NATIVE_CONFIG)
    trusted_paths([*installed, ca_path, haproxy, keepalived])
    for path, content in installed.items():
        if path.is_symlink() or read_bounded_bytes(path, max_bytes=65536) != content.encode("utf-8"):
            raise ValueError("Installed file differs from the rendered host bundle")
    if ca_path.is_symlink() or read_bounded_bytes(ca_path, max_bytes=128 * 1024) != ca:
        raise ValueError("Installed backend trust does not match the reviewed CA bundle")
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=ca.decode("ascii"))
    for path in (haproxy, keepalived):
        if not stat.S_ISREG(path.stat().st_mode) or not os.access(path, os.X_OK):
            raise ValueError("Expected the installed daemon binary")
    parsers = daemon_configs(haproxy, keepalived, config / "haproxy.cfg", config / "gerrit-router-vrrp.conf",
                              haproxy_user=haproxy_user, execute=execute)
    return {"host_preflight_passed": all(parsers.values()), "interface_verified": True,
            "installed_bundle_verified": True, "installed_backend_ca_verified": True, **parsers}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--backend-ca", required=True, type=Path, help="Reviewed CA bundle used for rendering and preflight")
    args = parser.parse_args(argv)
    try:
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise ValueError("Run as root on the selected native entry host")
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=65536))
        result = check(plan, args.host, read_bounded_bytes(args.backend_ca, max_bytes=128 * 1024))
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        print("Gerrit native VIP preflight failed. Check the host address, installed bundle and daemon packages.",
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["host_preflight_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
