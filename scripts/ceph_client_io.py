#!/usr/bin/env python3
"""Finite non-root client canary for fresh CSI claims; no credentials or host mounts."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from strict_json import loads_strict_json


ROLES = ("rbd-write", "rbd-read", "cephfs-left", "cephfs-right")
UNVERIFIED = {k: False for k in ("node_loss_verified", "recovery_verified", "ha_verified", "native_client_mounts_verified", "full_stack_verified")}


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(scope):
    if (not isinstance(scope, dict) or set(scope) != {"schema_version", "operation_id", "payload_bytes", "timeout_seconds", "nodes"}
            or type(scope["schema_version"]) is not int or scope["schema_version"] != 1
            or type(scope["payload_bytes"]) is not int or not 65536 <= scope["payload_bytes"] <= 4 * 1024 ** 2
            or type(scope["timeout_seconds"]) is not int or not 10 <= scope["timeout_seconds"] <= 300): raise ValueError("Use a finite canary scope")
    if not isinstance(scope["operation_id"], str) or str(uuid.UUID(scope["operation_id"])) != scope["operation_id"]: raise ValueError("Use the original canary UUID")
    if not isinstance(scope["nodes"], dict) or set(scope["nodes"]) != set(ROLES): raise ValueError("Bind all four client roles to their reviewed nodes")
    for value in scope["nodes"].values():
        if not isinstance(value, str) or len(value) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in value.split(".")): raise ValueError("Invalid client node")
    if (scope["nodes"]["rbd-write"] != scope["nodes"]["cephfs-left"] or scope["nodes"]["rbd-read"] != scope["nodes"]["cephfs-right"]
            or scope["nodes"]["rbd-write"] == scope["nodes"]["rbd-read"]): raise ValueError("Require distinct writer/reader and concurrent peer nodes")


def read_bootstrap_bytes(path, max_bytes, *, follow_symlinks=True):
    """Self-contained primitive mirrored by the pre-import SSH staging agent."""
    import os
    import stat
    if type(max_bytes) is not int or not 0 < max_bytes <= 512 * 1024 * 1024:
        raise ValueError("Invalid bootstrap file limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow_symlinks: flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("Invalid or excessive bootstrap file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise ValueError("Bootstrap file grew beyond its limit")
        return value
    finally:
        os.close(descriptor)


def load(path):
    raw = read_bootstrap_bytes(path, 16384)
    value = loads_strict_json(raw)
    validate(value)
    return value


def payload(scope, identity):
    return hashlib.shake_256((scope["operation_id"] + ":" + identity + ":ceph-client-v1").encode()).digest(scope["payload_bytes"])


def expected(scope, identity): return hashlib.sha256(payload(scope, identity)).hexdigest()


def mounted(kind, readonly, *, inventory=Path("/proc/self/mountinfo")):
    raw = read_bootstrap_bytes(inventory, 1024 ** 2)
    found = []
    for line in raw.decode().splitlines():
        before, separator, after = line.partition(" - "); fields, tail = before.split(), after.split()
        if not separator or len(fields) < 6 or len(tail) < 3: raise ValueError("Invalid mount inventory")
        if fields[4] == "/volume": found.append((fields[2], set(fields[5].split(",")), tail[0]))
    if len(found) != 1 or found[0][2] != ("ext4" if kind == "rbd" else "ceph") or ("ro" in found[0][1]) != readonly: raise ValueError("Require the expected CSI filesystem mount and write policy")
    return found[0][2]


def file_write(directory, name, value):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        view = memoryview(value)
        while view:
            count = os.write(fd, view)
            if count <= 0: raise ValueError("A canary write did not advance")
            view = view[count:]
        os.fsync(fd)
    finally: os.close(fd)
    os.fsync(directory)


def file_read(directory, name, length):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != length or info.st_uid != os.geteuid(): raise ValueError("Canary file identity or length differs")
        result = bytearray()
        while len(result) <= length:
            chunk = os.read(fd, min(65536, length + 1 - len(result)))
            if not chunk: break
            result.extend(chunk)
        if len(result) != length: raise ValueError("Canary file changed while reading")
        return bytes(result)
    finally: os.close(fd)


def exchange(scope, role, root, *, deadline=None, root_descriptor=None):
    """Operate only in this new operation's directory on an already checked mount."""
    validate(scope)
    if role not in ROLES: raise ValueError("Unknown canary role")
    deadline = deadline if deadline is not None else time.monotonic() + scope["timeout_seconds"]
    name = ".platform-canary-" + uuid.UUID(scope["operation_id"]).hex
    directory = None
    if root_descriptor is not None and (type(root_descriptor) is not int or root_descriptor < 0): raise ValueError("Invalid checked root descriptor")
    root_fd = os.dup(root_descriptor) if root_descriptor is not None else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode): raise ValueError("The checked root is not a directory")
        if role != "rbd-read":
            try: os.mkdir(name, 0o700, dir_fd=root_fd); os.fsync(root_fd)
            except FileExistsError:
                if role == "rbd-write": raise ValueError("An original writer cannot be retried on existing data")
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077: raise ValueError("The original private canary directory changed")
        identity = "rbd" if role.startswith("rbd-") else role
        peer = "cephfs-right" if role == "cephfs-left" else "cephfs-left"
        if role != "rbd-read": file_write(directory, identity + ".bin", payload(scope, identity))
        def verify(name, identity, size):
            while True:
                if time.monotonic() >= deadline: raise TimeoutError("The canary peer or persisted file is unavailable")
                try:
                    value = file_read(directory, name, size)
                    if value != (payload(scope, identity) if size else b""): raise ValueError("Persisted canary bytes differ")
                    return
                except FileNotFoundError: time.sleep(0.1)
        verify(identity + ".bin", identity, scope["payload_bytes"])
        if role.startswith("cephfs-"):
            # The payload name is visible as soon as O_EXCL creates it.
            # Publish readiness only after the writer has synced and read it.
            file_write(directory, role + ".ready", b"")
            verify(peer + ".ready", peer, 0)
            verify(peer + ".bin", peer, scope["payload_bytes"])
            file_write(directory, role + ".seen", b"")
            verify(peer + ".seen", peer, 0)
        return {"payload_sha256": expected(scope, identity), "peer_sha256": expected(scope, peer) if role.startswith("cephfs-") else None,
            "peer_barrier_verified": role.startswith("cephfs-")}
    finally:
        if directory is not None: os.close(directory)
        os.close(root_fd)


def execute(scope, role):
    validate(scope)
    if role not in ROLES or sys.platform != "linux" or os.geteuid() == 0 or os.environ.get("NODE_NAME") != scope["nodes"].get(role): raise ValueError("Require the reviewed non-root client node")
    kind = "rbd" if role.startswith("rbd-") else "cephfs"
    filesystem = mounted(kind, role == "rbd-read")
    result = exchange(scope, role, Path("/volume"))
    raw = read_bootstrap_bytes(Path(__file__), 128 * 1024)
    return {"schema_version": 1, "operation_id": scope["operation_id"], "scope_sha256": digest(scope), "program_sha256": hashlib.sha256(raw).hexdigest(),
        "role": role, "node": scope["nodes"][role], "payload_bytes": scope["payload_bytes"], "filesystem": filesystem, "volume_verified": True,
        "observed_at": datetime.now(timezone.utc).isoformat(), **result, **UNVERIFIED}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True); parser.add_argument("--role", choices=ROLES, required=True); parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        scope = load(args.scope)
        print(canonical(execute(scope, args.role) if args.execute else {"local_inputs_verified": True, "volume_verified": False, **UNVERIFIED})); return 0
    except Exception:
        print("CSI client canary refused; private scope and file contents were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
