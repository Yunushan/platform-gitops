#!/usr/bin/env python3
"""Scoped CephFS exchange on an existing Linux mount; default validation is local only."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types
import uuid

from strict_json import loads_strict_json


ROLES = ("cephfs-left", "cephfs-right")
UNVERIFIED = {k: False for k in ("client_caps_verified", "docker_runtime_verified", "rbd_mounts_verified", "native_client_mounts_verified",
    "node_loss_verified", "recovery_verified", "ha_verified", "full_stack_verified")}


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def sha(value): return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
def path(value): return isinstance(value, str) and len(value) <= 240 and bool(re.fullmatch(r"/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+", value))
def label(value): return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9-]{1,47}", value))
def identifier(value):
    try: return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError): return False


def read(path, limit):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit: raise ValueError("Excessive native canary input")
    return raw


def load(path):
    return loads_strict_json(read(path, 32768))


def validate(scope):
    if (not isinstance(scope, dict) or set(scope) != {"schema_version", "operation_id", "runtime", "storage_runtime", "storage_plan_sha256", "storage_proof_sha256", "fsid", "filesystem", "cephfs_path",
            "hosts", "uid", "gid", "payload_bytes", "timeout_seconds", "agent_sha256", "engine_sha256"}
            or type(scope["schema_version"]) is not int or scope["schema_version"] != 1 or not identifier(scope["operation_id"]) or scope["runtime"] not in {"raw", "docker"}
            or scope["storage_runtime"] not in {"rke2", "k3s", "k8s", "raw", "docker"}
            or any(not sha(scope[k]) for k in ("storage_plan_sha256", "storage_proof_sha256", "agent_sha256", "engine_sha256"))
            or not identifier(scope["fsid"]) or not label(scope["filesystem"]) or not path(scope["cephfs_path"])
            or any(type(scope[k]) is not int or not 1000 <= scope[k] <= 60000 for k in ("uid", "gid"))
            or type(scope["payload_bytes"]) is not int or not 65536 <= scope["payload_bytes"] <= 4 * 1024 ** 2
            or type(scope["timeout_seconds"]) is not int or not 10 <= scope["timeout_seconds"] <= 300): raise ValueError("Use a finite reviewed native CephFS scope")
    hosts = scope["hosts"]
    if not isinstance(hosts, list) or len(hosts) != 2: raise ValueError("Require two reviewed client hosts")
    for host in hosts:
        if (not isinstance(host, dict) or set(host) != {"name", "machine_id_sha256", "boot_id", "failure_domain", "mount_path", "package_path", "client"}
                or any(not label(host[k]) for k in ("name", "failure_domain", "client")) or host["client"] in {"admin", "bootstrap"}
                or not sha(host["machine_id_sha256"]) or not identifier(host["boot_id"]) or any(not path(host[k]) for k in ("mount_path", "package_path"))
                or host["mount_path"].startswith(host["package_path"] + "/") or host["package_path"].startswith(host["mount_path"] + "/")
                or host["mount_path"] == host["package_path"]): raise ValueError("Pin each separate client host and mount")
    if any(len({h[k] for h in hosts}) != 2 for k in ("name", "machine_id_sha256", "boot_id", "failure_domain", "client")): raise ValueError("Client identities and failure domains must be distinct")


def engine_scope(scope):
    return {"schema_version": 1, "operation_id": scope["operation_id"], "payload_bytes": scope["payload_bytes"], "timeout_seconds": scope["timeout_seconds"],
        "nodes": {r: scope["hosts"][0 if r in {"cephfs-left", "rbd-write"} else 1]["name"] for r in ("cephfs-left", "cephfs-right", "rbd-write", "rbd-read")}}


def engine(scope):
    raw = read(Path(__file__).with_name("ceph_client_io.py"), 128 * 1024)
    if hashlib.sha256(raw).hexdigest() != scope["engine_sha256"]: raise ValueError("The original exchange program changed")
    module = types.ModuleType("native_ceph_exchange")
    module.__file__ = str(Path(__file__).with_name("ceph_client_io.py"))
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


def identity(scope, host, *, container=False):
    if (sys.platform != "linux" or sys.version_info < (3, 12) or os.geteuid() != scope["uid"] or os.getegid() != scope["gid"]
            or container != (scope["runtime"] == "docker")): raise ValueError("Require the reviewed non-root Linux client")
    machine = read(Path("/identity/machine-id" if container else "/etc/machine-id"), 128).decode().strip()
    boot = read(Path("/identity/boot-id" if container else "/proc/sys/kernel/random/boot_id"), 128).decode().strip()
    if (not re.fullmatch(r"[0-9a-f]{32}", machine) or hashlib.sha256(machine.encode()).hexdigest() != host["machine_id_sha256"] or boot != host["boot_id"]): raise ValueError("The client machine or boot changed")


def mount_identity(scope, host, target, *, inventory=Path("/proc/self/mountinfo")):
    found = []
    for line in read(inventory, 1024 ** 2).decode().splitlines():
        before, separator, after = line.partition(" - "); fields, tail = before.split(), after.split()
        if not separator or len(fields) < 6 or len(tail) != 3: raise ValueError("Invalid native mount inventory")
        if fields[4].startswith(target + "/"): raise ValueError("Nested mounts are outside the canary scope")
        if fields[4] != target: continue
        options = set(fields[5].split(",")); filesystem_options = tail[2].split(",")
        expected_source = host["client"] + "@" + scope["fsid"] + "." + scope["filesystem"] + "=" + scope["cephfs_path"]
        if (not fields[0].isdecimal() or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]) or fields[3] != "/" or tail[0] != "ceph" or tail[1] != expected_source
                or not {"rw", "nosuid", "nodev", "noexec"}.issubset(options) or "ro" in options or "ro" in filesystem_options
                or [v for v in filesystem_options if v.startswith("ms_mode=")] != ["ms_mode=secure"]
                or "recover_session=clean" in filesystem_options): raise ValueError("Require the original encrypted CephFS subtree mount")
        found.append({"id": int(fields[0]), "device": fields[2], "mount_sha256": hashlib.sha256(line.encode()).hexdigest()})
    if len(found) != 1: raise ValueError("Require one exact CephFS mount; ordinary directories and stacked mounts are refused")
    return found[0]


def descriptor_identity(descriptor, mounted):
    info = os.fstat(descriptor)
    values = [line.split(":", 1)[1].strip() for line in read(Path("/proc/self/fdinfo") / str(descriptor), 4096).decode().splitlines() if line.startswith("mnt_id:")]
    if (not stat.S_ISDIR(info.st_mode) or values != [str(mounted["id"])] or f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}" != mounted["device"]): raise ValueError("The open directory is not the checked mount")
    return {"device": info.st_dev, "inode": info.st_ino}


def execute(scope, role, *, container=False):
    validate(scope)
    if role not in ROLES: raise ValueError("Unknown native client role")
    host = scope["hosts"][ROLES.index(role)]; identity(scope, host, container=container)
    if hashlib.sha256(read(Path(__file__), 128 * 1024)).hexdigest() != scope["agent_sha256"]: raise ValueError("The original native agent changed")
    exchange = engine(scope); target = "/volume" if container else host["mount_path"]
    # Keep the checked directory open throughout I/O; do not resolve its path again.
    mounted = mount_identity(scope, host, target)
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    started = datetime.now(timezone.utc).isoformat()
    try:
        original = descriptor_identity(descriptor, mounted)
        result = exchange.exchange(engine_scope(scope), role, None, root_descriptor=descriptor)
        identity(scope, host, container=container)
        if mount_identity(scope, host, target) != mounted or descriptor_identity(descriptor, mounted) != original: raise ValueError("The mounted client changed during exchange")
        if hashlib.sha256(read(Path(__file__), 128 * 1024)).hexdigest() != scope["agent_sha256"]: raise ValueError("The native agent changed during exchange")
        engine(scope)
    finally: os.close(descriptor)
    return {"schema_version": 1, "operation_id": scope["operation_id"], "scope_sha256": digest(scope), "role": role, "host": host["name"],
        "runtime": scope["runtime"], "machine_id_sha256": host["machine_id_sha256"], "boot_id": host["boot_id"], "payload_bytes": scope["payload_bytes"],
        "agent_sha256": scope["agent_sha256"], "engine_sha256": scope["engine_sha256"], "mount": mounted, "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(), "cephfs_mount_observed": True, **result, **UNVERIFIED}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True); parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--container", action="store_true"); parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        scope = load(args.scope); validate(scope)
        result = execute(scope, args.role, container=args.container) if args.execute else {"local_inputs_verified": True, "cephfs_mount_observed": False, **UNVERIFIED}
        print(canonical(result)); return 0
    except Exception:
        print("Native CephFS check refused; private host details and data were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
