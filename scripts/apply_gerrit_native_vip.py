#!/usr/bin/env python3
"""Apply a reviewed Gerrit VIP plan to one declared native entry host."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time

from atomic_file import _fsync_directory, atomic_write_text
from bounded_file import read_bounded_bytes
from check_gerrit_native_vip import check, check_interface, command, host_files
from manage_gateway_edge_host import host_lock, secure_directory
from render_gerrit_router import CA_FILE, NATIVE_CONFIG, READINESS_PORT, ROUTE_PATH, VIP_BOOT_GUARD, VIP_PENDING, route_config_id
from start_gateway_edge import trusted_paths
from http_transport import HttpTransportPolicyError, read_bounded_response
from strict_json import loads_strict_json


CONFIG = Path(NATIVE_CONFIG)
UNITS = Path("/etc/systemd/system")
LOCK = Path("/run/gerrit-router-apply.lock")
PENDING = Path(VIP_PENDING)
ACTIVATING = PENDING.with_name("activating.json")
SERVICES = ("gerrit-router.service", "gerrit-router-vrrp.service")


def apply(plan, previous, host, ca, *, rollback_pending=False, execute=command):
    target = host_files(plan, host, CONFIG, UNITS)
    prior = host_files(previous, host, CONFIG, UNITS) if previous is not None else {}
    if previous is not None and previous["native_vip"] != plan["native_vip"]:
        raise ValueError("Keep the VIP and host inventory fixed for this transition")
    check_interface(plan, host, execute)
    trust = ca.decode("ascii")
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=trust)
    target[Path(CA_FILE)] = trust
    if prior:
        prior[Path(CA_FILE)] = trust
    operation = {"schema_version": 1, "host": host,
                 "previous": {path.name: hashlib.sha256(value.encode()).hexdigest() for path, value in prior.items()},
                 "target": {path.name: hashlib.sha256(value.encode()).hexdigest() for path, value in target.items()}}
    if rollback_pending and previous is None:
        raise ValueError("A pending rollback requires the original previous plan")
    rollback_operation = {**operation, "rollback_pending": True}
    if rollback_pending:
        plan, target, prior = previous, prior, target
    desired = rollback_operation if rollback_pending else operation

    def systemctl(*args):
        result = execute(["systemctl", *args])
        if result.returncode:
            raise ValueError("Native Gerrit service operation failed")
        return result.stdout.decode("utf-8")

    identity = route_config_id(plan)
    role = "backup" if plan.get("hold_primary", False) else "primary"
    expected_route = f"{identity} {role}".encode("ascii")

    def ready():
        connection = http.client.HTTPConnection("127.0.0.1", READINESS_PORT, timeout=1)
        try:
            connection.request("GET", ROUTE_PATH, headers={"Connection": "close"})
            with connection.getresponse() as response:
                return response.status == 200 and read_bounded_response(response, max_bytes=len(expected_route)) == expected_route
        except (OSError, http.client.HTTPException, HttpTransportPolicyError):
            return False
        finally:
            connection.close()

    with host_lock(LOCK):
        journals = [path for path in (PENDING, ACTIVATING) if path.exists() or path.is_symlink()]
        if len(journals) > 1:
            raise ValueError("Conflicting native Gerrit transition records")
        journal = journals[0] if journals else None
        if rollback_pending and journal is None:
            raise ValueError("There is no pending transition to roll back")
        recorded = None
        if journal is not None:
            if journal.is_symlink():
                raise ValueError("Native Gerrit transition state cannot be a symlink")
            trusted_paths([journal])
            recorded = loads_strict_json(read_bounded_bytes(journal, max_bytes=16384))
            if recorded != desired and (not rollback_pending or recorded != operation):
                raise ValueError("Retry the pending transition with its original host, plans and CA")

        # Reject foreign fragments and local overrides before stopping services.
        for name in SERVICES:
            output = systemctl("show", name, "-p", "FragmentPath", "-p", "DropInPaths", "-p", "LoadState")
            info = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            if info.get("LoadState") not in {"loaded", "not-found"} or info.get("DropInPaths") != "" or (
                info.get("FragmentPath") not in {"", str(UNITS / name)}
            ):
                raise ValueError("Unexpected native Gerrit unit or override")

        for path, content in target.items():
            secure_directory(path.parent)
            if path.is_symlink():
                raise ValueError("Native Gerrit managed files cannot be symlinks")
            if path.exists():
                trusted_paths([path])
                installed = read_bounded_bytes(path, max_bytes=128 * 1024)
                allowed = [content.encode("utf-8")]
                if path in prior:
                    allowed.append(prior[path].encode("utf-8"))
                    if path.parent == UNITS:
                        allowed.append(prior[path].replace(VIP_BOOT_GUARD, "").encode("utf-8"))
                if installed not in allowed:
                    raise ValueError("Native Gerrit file differs from the expected previous or target bundle")
            elif path in prior:
                raise ValueError("The expected previous bundle is incomplete")

        # The exact same release may be resumed after a lost acknowledgment.
        already_ready = all(path.exists() and read_bounded_bytes(path, max_bytes=128 * 1024) == value.encode("utf-8")
                            for path, value in target.items())
        if journal != PENDING and recorded in (None, desired) and already_ready and ready() and all(
            execute(["systemctl", "is-active", name]).returncode == 0 for name in SERVICES
        ):
            if journal is not None:
                journal.unlink()
                _fsync_directory(PENDING.parent)
            return {"host_applied": True, "changed": False, "route_config_id": identity}

        if journal == ACTIVATING:
            os.replace(ACTIVATING, PENDING)
            _fsync_directory(PENDING.parent)
        if recorded != desired:
            secure_directory(PENDING.parent)
            atomic_write_text(PENDING, json.dumps(desired, sort_keys=True) + "\n")
        # A previously absent unit has nothing to stop on a fresh install.
        for name in reversed(SERVICES):
            if systemctl("show", name, "-p", "LoadState", "--value").strip() != "not-found":
                systemctl("stop", name)
        # Install the boot guards before replacing any daemon configuration.
        for path, value in sorted(target.items(), key=lambda item: item[0].parent != UNITS):
            atomic_write_text(path, value, mode=0o644)
        report = check(plan, host, ca, execute=execute)
        if not report["host_preflight_passed"]:
            raise ValueError("Installed target failed daemon configuration validation")
        systemctl("daemon-reload")
        # Both configurations and units now match. Allow starts while keeping
        # the transition bound to the same inputs through activation.
        os.replace(PENDING, ACTIVATING)
        _fsync_directory(PENDING.parent)
        systemctl("start", SERVICES[0])
        deadline = time.monotonic() + 30
        while not ready():
            if time.monotonic() >= deadline:
                raise TimeoutError("HAProxy did not report the target configuration")
            time.sleep(0.25)
        systemctl("start", SERVICES[1])
        for name in SERVICES:
            systemctl("is-active", name)
        if not ready():
            raise ValueError("HAProxy lost the expected route during activation")
        ACTIVATING.unlink()
        _fsync_directory(PENDING.parent)
        return {"host_applied": True, "changed": True, "route_config_id": identity}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--backend-ca", type=Path, required=True)
    current = parser.add_mutually_exclusive_group(required=True)
    current.add_argument("--previous-plan", type=Path)
    current.add_argument("--fresh", action="store_true")
    parser.add_argument("--rollback-pending", action="store_true",
                        help="Restore the previous release using the original pending transition inputs")
    args = parser.parse_args(argv)
    if args.rollback_pending and not args.previous_plan:
        parser.error("--rollback-pending requires --previous-plan")
    try:
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise ValueError("Run as root on the selected native entry host")
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=65536))
        previous = loads_strict_json(read_bounded_bytes(args.previous_plan, max_bytes=65536)) if args.previous_plan else None
        result = apply(plan, previous, args.host, read_bounded_bytes(args.backend_ca, max_bytes=128 * 1024),
                       rollback_pending=args.rollback_pending)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Gerrit native VIP apply failed. Check the installed release, plan and local services before retrying.",
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
