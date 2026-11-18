#!/usr/bin/env python3
"""Read-only Linux fresh-device inspection for a reviewed Rook initialization scope."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time
import uuid

from strict_json import loads_strict_json


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def sha(value): return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
def identifier(value):
    try: return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError): return False
def integer(value, low, high): return type(value) is int and low <= value <= high


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


def read(path, maximum=4096):
    return read_bootstrap_bytes(path, maximum)


def load(path):
    return loads_strict_json(read(path, 32768))


def sysfs_attribute(path, limit):
    # stat() reports the kernel attribute buffer size, often a full page.
    value = read_bootstrap_bytes(path, max(limit, os.sysconf("SC_PAGE_SIZE")), follow_symlinks=False)
    if len(value) > limit: raise ValueError("A sysfs attribute exceeded its byte limit")
    return value.strip()


def validate(scope):
    fields = {"schema_version", "cluster_id", "node_name", "machine_id_sha256", "boot_id", "timeout_seconds", "devices"}
    if (not isinstance(scope, dict) or set(scope) != fields or type(scope["schema_version"]) is not int or scope["schema_version"] != 1
            or not identifier(scope["cluster_id"]) or not identifier(scope["boot_id"]) or not sha(scope["machine_id_sha256"])
            or not isinstance(scope["node_name"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", scope["node_name"])
            or not integer(scope["timeout_seconds"], 30, 86400) or not isinstance(scope["devices"], list) or not 1 <= len(scope["devices"]) <= 8):
        raise ValueError("Use a complete reviewed node/device scope")
    for i, device in enumerate(scope["devices"]):
        if (not isinstance(device, dict) or set(device) != {"path", "major", "minor", "bytes", "identity_sha256"}
                or device["path"] != "/devices/osd-" + str(i) or not integer(device["major"], 1, 2 ** 20 - 1)
                or not integer(device["minor"], 0, 2 ** 20 - 1) or not integer(device["bytes"], 64 * 1024 ** 3, 128 * 1024 ** 4)
                or not sha(device["identity_sha256"])): raise ValueError("Pin each whole device, size and hardware-identity digest")
    if len({(d["major"], d["minor"]) for d in scope["devices"]}) != len(scope["devices"]) or len({d["identity_sha256"] for d in scope["devices"]}) != len(scope["devices"]):
        raise ValueError("Device aliases cannot count as independent OSDs")


def hardware(device, *, sysfs=Path("/host/sys")):
    root = sysfs.resolve(strict=True)
    path = (root / "dev/block" / (str(device["major"]) + ":" + str(device["minor"]))).resolve(strict=True)
    if not path.is_relative_to(root) or (path / "partition").exists() or "virtual" in path.relative_to(root).parts: raise ValueError("Use physical whole devices, not partitions or virtual stacks")
    if sysfs_attribute(path / "dev", 32) != (str(device["major"]) + ":" + str(device["minor"])).encode(): raise ValueError("The kernel device identity changed")
    if any((child / "partition").exists() for child in path.iterdir()) or any((path / "holders").iterdir()) or any((path / "slaves").iterdir()):
        raise ValueError("An existing partition, holder or lower device requires manual recovery")
    identity = {}
    for key in ("wwid", "serial"):
        candidates = (path / key, path / "device" / key)
        value = ""
        for candidate in candidates:
            if not candidate.exists(): continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root): raise ValueError("Hardware identity escaped the read-only sysfs mount")
            value = sysfs_attribute(resolved, 512).decode("ascii")
            if value: break
        if len(value) > 256 or any(ord(c) < 32 or ord(c) > 126 for c in value): raise ValueError("Unsupported hardware identity")
        identity[key] = value
    if not any(identity.values()) or fingerprint(identity) != device["identity_sha256"]: raise ValueError("The reviewed hardware identity changed")
    return fingerprint(identity)


def mounted(device, *, mountinfo=Path("/host/mountinfo")):
    expected = str(device["major"]) + ":" + str(device["minor"])
    for line in read(mountinfo, 4 * 1024 ** 2).decode("utf-8").splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields: raise ValueError("Cannot establish the host mount inventory")
        if fields[2] == expected: raise ValueError("A selected device is mounted on the host")


def block_size(descriptor):
    import fcntl
    size, readonly = bytearray(8), bytearray(4)
    fcntl.ioctl(descriptor, 0x80081272, size, True)  # BLKGETSIZE64
    fcntl.ioctl(descriptor, 0x125E, readonly, True)  # BLKROGET
    if struct.unpack("=I", readonly)[0] != 0: raise ValueError("A read-only device cannot become a new OSD")
    return struct.unpack("=Q", size)[0]


def scan_zero(descriptor, size, deadline):
    digest = hashlib.sha256(); remaining = size
    while remaining:
        if time.monotonic() >= deadline: raise TimeoutError("The complete blank-device scan exceeded its deadline")
        value = os.read(descriptor, min(4 * 1024 ** 2, remaining))
        if not value or value != bytes(len(value)): raise ValueError("The selected device is not completely blank")
        digest.update(value); remaining -= len(value)
    if os.read(descriptor, 1): raise ValueError("The selected device size changed")
    return digest.hexdigest()


def probe(scope):
    validate(scope)
    if sys.platform != "linux" or os.geteuid() != 0 or os.environ.get("PROBE_NODE_NAME") != scope["node_name"]: raise ValueError("Require the reviewed root Linux node-probe container")
    def host():
        machine = read(Path("/host/machine-id"), 128).strip()
        boot = read(Path("/host/boot-id"), 128).strip().decode("ascii")
        if not re.fullmatch(b"[0-9a-f]{32}", machine) or hashlib.sha256(machine).hexdigest() != scope["machine_id_sha256"] or boot != scope["boot_id"]:
            raise ValueError("The reviewed host or boot identity changed")
        if os.path.lexists(Path("/host/rook") / scope["cluster_id"]): raise ValueError("Existing Rook state cannot be adopted")
    host(); deadline = time.monotonic() + scope["timeout_seconds"]; observations = []
    with ExitStack() as opened:
        for device in scope["devices"]:
            hardware(device); mounted(device)
            # Linux block-device O_EXCL refuses a busy exclusive claimant.
            # It is not fencing against a privileged writer that bypasses it.
            descriptor = os.open(device["path"], os.O_RDONLY | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
            opened.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISBLK(info.st_mode) or (os.major(info.st_rdev), os.minor(info.st_rdev)) != (device["major"], device["minor"]) or block_size(descriptor) != device["bytes"]:
                raise ValueError("The selected device handle, size or type changed")
            checksum = scan_zero(descriptor, device["bytes"], deadline)
            if block_size(descriptor) != device["bytes"] or os.fstat(descriptor).st_rdev != info.st_rdev: raise ValueError("The original device handle changed")
            hardware(device); mounted(device)
            observations.append({"major": device["major"], "minor": device["minor"], "bytes": device["bytes"], "identity_sha256": device["identity_sha256"], "zero_sha256": checksum})
        host()
    return {"schema_version": 1, "scope_sha256": fingerprint(scope), "probe_sha256": hashlib.sha256(read(Path(__file__), 128 * 1024)).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat(), "fresh_devices_observed": True, "devices": observations,
        "fencing_verified": False, "provider_installed": False, "ha_verified": False, "full_stack_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True); parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    try:
        scope = load(args.scope); validate(scope)
        result = probe(scope) if args.probe else {"local_inputs_verified": True, "fresh_devices_observed": False, "provider_installed": False, "ha_verified": False, "full_stack_verified": False}
        print(json.dumps(result)); return 0
    except Exception:
        print("Ceph device probe refused; private inputs and device contents were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
