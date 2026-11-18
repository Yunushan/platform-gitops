#!/usr/bin/env python3
"""Inspect a reviewed edge package, or explicitly start one local daemon."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys

from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json


BUNDLE = Path("/etc/platform/gateway-edge")
INPUTS = Path("/run/platform/gateway-edge")
NATIVE_TRUST = Path("/var/lib/platform-gateway-edge/trust")
PROGRAMS = {"start_gateway_edge.py", "gateway_edge_health.py", "bounded_file.py", "bounded_subprocess.py", "subprocess_timeout.py", "strict_json.py"}


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def reviewed_version(component, value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return False
    version = tuple(int(part) for part in value.split("."))
    # Older 3.2 builds ignore init-state during process startup, admitting
    # unchecked backends. The upstream startup fix is present from 3.2.20.
    return (component == "haproxy" and version[:2] == (3, 2) and version[2] >= 20
            or component == "keepalived" and version[:2] == (2, 3) and version[2] >= 4)


def verify_daemon_version(path, component, expected, environment):
    if not reviewed_version(component, expected):
        raise ValueError("Unsupported edge daemon version or known unsafe startup behavior")
    result = run_bounded([str(path), "-v" if component == "haproxy" else "--version"], env=environment, text=True,
        input="", timeout=10, output_max_bytes=256 * 1024, check=False)
    prefix = "HAProxy version " if component == "haproxy" else "Keepalived v"
    match = re.search(r"^" + re.escape(prefix) + r"([0-9]+\.[0-9]+\.[0-9]+)(?=[\s(-]|$)([^\r\n]*)", result.stdout + "\n" + result.stderr, re.MULTILINE)
    if (result.returncode or match is None or match.group(1) != expected or re.search(r"-(?:dev|rc)", match.group(2), re.IGNORECASE)):
        raise ValueError("Pinned daemon does not report its reviewed stable version")


def load_bundle(root, digest, host, *, check_executing=True):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Reviewed edge contract digest required")
    payload = read_bounded_bytes(root / "edge-contract.json", max_bytes=256 * 1024)
    if sha(payload) != digest:
        raise ValueError("Edge contract changed")
    contract = loads_strict_json(payload)
    if (not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "gateway_plan", "files_sha256"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1
            or contract["runtime"] not in {"rke2", "k3s", "k8s", "docker", "raw"}):
        raise ValueError("Invalid edge contract")
    hosts = contract["plan"]["entry_hosts"]
    if host not in {item["name"] for item in hosts}:
        raise ValueError("Unknown edge host identity")
    expected = PROGRAMS | {"haproxy.cfg"} | {"keepalived-" + item["name"] + ".conf" for item in hosts}
    if set(contract["files_sha256"]) != expected:
        raise ValueError("Incomplete edge program/configuration inventory")
    for name, digest in contract["files_sha256"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) or sha(read_bounded_bytes(root / name, max_bytes=1024 * 1024)) != digest:
            raise ValueError("Edge program/configuration content changed")
    # Verify the executing helper and imported local dependencies, not merely
    # different copied files bearing their names.
    # Verify the readiness hook as a standalone file, separately from the imports above.
    for name in PROGRAMS - {"gateway_edge_health.py"} if check_executing else ():
        module = sys.modules.get(name.removesuffix(".py"))
        path = Path(__file__) if name == "start_gateway_edge.py" else Path(module.__file__) if module and getattr(module, "__file__", None) else None
        if path is not None:
            normalized = read_bounded_text(path, encoding="utf-8", max_bytes=1024 * 1024).encode()
            if sha(normalized) != contract["files_sha256"][name]:
                raise ValueError("Executing edge helper differs from the reviewed package")
    return contract


def interface_address(interface):
    import fcntl
    if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", interface):
        raise ValueError("Invalid interface identity")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as descriptor:
        request = struct.pack("256s", interface.encode())
        flags = fcntl.ioctl(descriptor.fileno(), 0x8913, request)
        if not struct.unpack_from("H", flags, 16)[0] & 1:
            raise ValueError("Entry interface is down")
        return socket.inet_ntoa(fcntl.ioctl(descriptor.fileno(), 0x8915, request)[20:24])


def host_inputs(root, contract, host, component, *, inputs=INPUTS):
    if sys.platform != "linux":
        raise ValueError("Edge host checks require Linux")
    selected = next(item for item in contract["plan"]["entry_hosts"] if item["name"] == host)
    if interface_address(selected["interface"]) != selected["address"]:
        raise ValueError("This is not the declared edge interface/address")
    ca = inputs / "ca.crt"
    if sha(read_bounded_bytes(ca, max_bytes=256 * 1024)) != contract["plan"]["ca_sha256"]:
        raise ValueError("Edge backend CA differs from the reviewed trust input")
    binary = contract["plan"]["binaries"][component]
    path = root / "bin" / component if contract["runtime"] == "raw" else Path(binary["path"])
    stat = path.stat()
    if (not path.is_file() or stat.st_uid != 0 or stat.st_mode & 0o022
            or not os.access(path, os.X_OK) or sha(read_bounded_bytes(path, max_bytes=64 * 1024 ** 2)) != binary["sha256"]):
        raise ValueError("Edge daemon binary is not the reviewed root-owned artifact")
    if contract["runtime"] == "raw" and component == "keepalived" and binary.get("config_opts_sha256"):
        support = root / "bin" / "keepalived.config-opts"
        if sha(read_bounded_bytes(support, max_bytes=256 * 1024)) != binary["config_opts_sha256"]:
            raise ValueError("Keepalived build metadata changed")
        trusted_paths([support])
    return path


def daemon_arguments(path, config, component, plan):
    state = "/run/platform-gateway-edge/" + plan["name"]
    # Only the VRRP subsystem belongs to this adapter. Default checker/BFD
    # PID files live outside its writable runtime directory and can cause a
    # misleading "already running" exit under the real service sandbox.
    return [str(path), "-W", "-db", "-f", str(config)] if component == "haproxy" else [str(path), "--dont-fork", "--log-console", "--vrrp", "--no_bfd", "--use-file", str(config),
        "--pid=" + state + ".pid", "--vrrp_pid=" + state + "-vrrp.pid"]


def trusted_paths(paths):
    for path in paths:
        resolved = path.resolve(strict=True)
        for item in (resolved, *resolved.parents):
            stat = item.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o022:
                raise ValueError("Privileged edge code/configuration must have a root-owned non-writable path")


def require_keepalived_capabilities():
    # Keepalived resets supplementary groups before executing every tracked
    # script, even when script_user is root. Its failure path can report a
    # successful script exit, so reject a missing SETGID before starting it.
    status = read_bounded_text(Path("/proc/self/status"), encoding="utf-8", max_bytes=64 * 1024)
    match = re.search(r"^CapEff:\s*([0-9a-fA-F]+)$", status, re.MULTILINE)
    required = (1 << 6) | (1 << 12) | (1 << 13)  # SETGID, NET_ADMIN, NET_RAW
    if match is None or int(match.group(1), 16) & required != required:
        raise ValueError("Keepalived needs NET_ADMIN, NET_RAW and SETGID for VIP and tracked-script execution")


def operate(root, digest, host, component, *, check_host=False, start=False, inputs=None):
    root = root.resolve(strict=True)
    contract = load_bundle(root, digest, host)
    if inputs is None: inputs = root if contract["runtime"] == "raw" else INPUTS
    if component not in {"haproxy", "keepalived"} or check_host and start:
        raise ValueError("Select a single daemon and host mode")
    if not check_host and not start:
        return {"read_only": True, "local_inputs_verified": True, "started": False, "ha_verified": False}
    path = host_inputs(root, contract, host, component, inputs=inputs)
    if contract["runtime"] == "raw":
        trust = NATIVE_TRUST / contract["plan"]["ca_sha256"] / "ca.crt"
        if sha(read_bounded_bytes(trust, max_bytes=256 * 1024)) != contract["plan"]["ca_sha256"]:
            raise ValueError("Native CA store differs from the reviewed release")
        trusted_paths([trust])
    trusted_paths([root / name for name in contract["files_sha256"]] + [inputs / "ca.crt", path, Path(sys.executable), Path("/usr/bin/python3")])
    clean = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "HOME": "/tmp"}
    verify_daemon_version(path, component, contract["plan"]["binaries"][component]["version"], clean)
    config = root / ("haproxy.cfg" if component == "haproxy" else "keepalived-" + host + ".conf")
    check = [str(path), "-c", "-f", str(config)] if component == "haproxy" else [str(path), "--config-test", "--use-file", str(config)]
    result = run_bounded(check, env=clean, text=True, input="", timeout=15, output_max_bytes=256 * 1024, check=False)
    if result.returncode:
        raise ValueError("Edge daemon rejected its reviewed configuration")
    if not start:
        return {"read_only": True, "local_inputs_verified": True, "host_checked": True, "started": False, "ha_verified": False}
    if component == "keepalived" and os.geteuid() != 0 or component == "haproxy" and os.geteuid() == 0:
        raise ValueError("Keepalived requires its root capability boundary; HAProxy must use a separate non-root identity")
    if component == "keepalived":
        require_keepalived_capabilities()
    args = daemon_arguments(path, config, component, contract["plan"])
    os.execve(path, args, clean)
    raise ValueError("Daemon execution unexpectedly returned")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, default=BUNDLE)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--component", choices=("haproxy", "keepalived"), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-host", action="store_true")
    mode.add_argument("--start", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, args.host, args.component, check_host=args.check_host, start=args.start)))
        return 0
    except Exception:
        print("Gateway edge stopped; inspect the private contract, host identity, CA and pinned binaries.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
