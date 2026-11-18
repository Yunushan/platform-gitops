#!/usr/bin/env python3
"""Mount one reviewed CephFS subtree on a prepared Linux client, or observe its original attempt."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import stat
import sys

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import ceph_native_io as agent
import render_ceph_native_check as renderer
from start_gateway_edge import trusted_paths
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CLEAN = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"}
UNVERIFIED = {**agent.UNVERIFIED, "native_cephfs_mounts_verified": False, "native_cephfs_data_roundtrip_verified": False}


def toolset():
    return renderer.storage.fingerprint({"provider_tools": renderer.csi.toolset(), **{name: hashlib.sha256(read_bounded_bytes(Path(__file__).parent / name, max_bytes=256 * 1024)).hexdigest()
        for name in ("mount_cephfs_client.py", "render_ceph_native_check.py", "ceph_native_io.py", "start_gateway_edge.py")}})


def validate(plan, native, storage, runtime, host):
    renderer.validate(native, storage, runtime)
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "operation_id", "native_plan_sha256", "monitors", "hosts"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not agent.identifier(plan["operation_id"])
            or plan["operation_id"] == native["operation_id"] or plan["native_plan_sha256"] != renderer.storage.fingerprint(native)
            or not isinstance(plan["hosts"], dict) or set(plan["hosts"]) != {h["name"] for h in native["hosts"]} or host not in plan["hosts"]): raise ValueError("Use a separate mount operation bound to the original native scope")
    for item in plan["hosts"].values():
        if (not isinstance(item, dict) or set(item) != {"helper_sha256", "kernel_release", "credential_file_env"} or not renderer.storage.sha(item["helper_sha256"])
                or not isinstance(item["kernel_release"], str) or not re.fullmatch(r"[0-9]+[.][0-9]+[.][A-Za-z0-9._+-]{1,110}", item["kernel_release"])
                or not isinstance(item["credential_file_env"], str) or not re.fullmatch(r"CEPH_[A-Z0-9_]{3,80}_FILE", item["credential_file_env"])): raise ValueError("Pin each helper, kernel and private credential-file reference")
    rows = plan["monitors"]
    if not isinstance(rows, list) or len(rows) != 3: raise ValueError("Supply three reviewed numeric monitor endpoints")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"address", "port"} or not isinstance(row["address"], str) or not renderer.storage.integer(row["port"], 1, 65535): raise ValueError("Use fixed numeric monitor endpoints")
        address = ipaddress.ip_address(row["address"])
        if str(address) != row["address"] or address.is_unspecified or address.is_multicast or address.is_loopback: raise ValueError("Use canonical non-loopback monitor addresses")
    if len({r["address"] for r in rows}) != 3: raise ValueError("Monitor addresses must be distinct")


def arguments(plan, scope, selected, helper, descriptor):
    monitors = "/".join(("[" + r["address"] + "]" if ":" in r["address"] else r["address"]) + ":" + str(r["port"]) for r in plan["monitors"])
    options = "rw,nosuid,nodev,noexec,ms_mode=secure,noshare,recover_session=no,nofallback,mount_timeout=60,conf=/dev/null,mon_addr=" + monitors
    options += ",secretfile=/proc/" + str(os.getpid()) + "/fd/" + str(descriptor)
    source = selected["client"] + "@" + scope["fsid"] + "." + scope["filesystem"] + "=" + scope["cephfs_path"]
    return [str(helper), source, selected["mount_path"], "-n", "-o", options]


class Host:
    def __init__(self, *, helper=Path("/usr/sbin/mount.ceph"), inventory=Path("/proc/self/mountinfo"), lock_root=Path("/run/platform-ceph-mount-locks")):
        self.helper, self.inventory, self.lock_root = helper, inventory, lock_root

    @contextmanager
    def target_lock(self, selected):
        self.lock_root.mkdir(mode=0o700, exist_ok=True)
        if self.lock_root.is_symlink(): raise ValueError("Untrusted host mount lock directory")
        trusted_paths([self.lock_root])
        rootinfo = self.lock_root.stat()
        if not stat.S_ISDIR(rootinfo.st_mode) or rootinfo.st_mode & 0o077: raise ValueError("Use private root-owned host mount locks")
        directory = self.lock_root / hashlib.sha256(selected["mount_path"].encode()).hexdigest()
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077: raise ValueError("Untrusted per-target mount lock")
        with renderer.csi.shared.lock(directory): yield

    @contextmanager
    def credential(self, pins, selected):
        value = os.environ.get(pins["credential_file_env"], "")
        if not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value) or any(p in {".", ".."} for p in value.split("/")): raise ValueError("Set a safe absolute private credential-file path")
        path = Path(value)
        if path.resolve(strict=True) != path: raise ValueError("Use a direct credential path without symlinked ancestors")
        if path == Path(selected["mount_path"]) or Path(selected["mount_path"]) in path.parents: raise ValueError("Keep credentials outside the mount target")
        trusted_paths([path.parent])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600: raise ValueError("Require a root-owned private single-link credential file")
            raw = os.read(fd, 257)
            if len(raw) > 256 or not re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}\n?", raw) or not 16 <= len(base64.b64decode(raw.strip(), validate=True)) <= 64: raise ValueError("Use a bounded CephX secret file")
            os.lseek(fd, 0, os.SEEK_SET)
            metadata = {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                "path_sha256": hashlib.sha256(value.encode()).hexdigest()}
            yield fd, metadata
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns): raise ValueError("The original credential file changed during observation")
        finally: os.close(fd)

    def prerequisites(self, pins, selected):
        if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Mount preparation requires an explicit root Linux client session")
        machine = agent.read(Path("/etc/machine-id"), 128).decode().strip(); boot = agent.read(Path("/proc/sys/kernel/random/boot_id"), 128).decode().strip()
        if (not re.fullmatch(r"[0-9a-f]{32}", machine) or hashlib.sha256(machine.encode()).hexdigest() != selected["machine_id_sha256"] or boot != selected["boot_id"]
                or os.uname().release != pins["kernel_release"]): raise ValueError("The reviewed client machine, boot or kernel changed")
        trusted_paths([self.helper, Path(selected["mount_path"]).parent])
        if not os.access(self.helper, os.X_OK) or hashlib.sha256(read_bounded_bytes(self.helper, max_bytes=128 * 1024 ** 2)).hexdigest() != pins["helper_sha256"]: raise ValueError("The reviewed mount helper changed")
        if agent.read(Path("/sys/module/ceph/parameters/mount_syntax_v2"), 16).strip() not in {b"Y", b"1"}: raise ValueError("Load the reviewed kernel Ceph module with v2 mount syntax support first")
        own, initial = (Path(p).stat() for p in ("/proc/self/ns/mnt", "/proc/1/ns/mnt"))
        if (own.st_dev, own.st_ino) != (initial.st_dev, initial.st_ino): raise ValueError("Use the host mount namespace")
        with self.credential(pins, selected) as (_, metadata):
            return {"namespace": {"device": own.st_dev, "inode": own.st_ino}, "credential": metadata}

    def inventory_ids(self):
        ids = []
        for line in agent.read(self.inventory, 1024 ** 2).decode().splitlines():
            before, separator, after = line.partition(" - "); fields = before.split()
            if not separator or len(fields) < 6 or not fields[0].isdecimal(): raise ValueError("Invalid host mount inventory")
            ids.append(int(fields[0]))
        if len(set(ids)) != len(ids): raise ValueError("Duplicate mount IDs in the host inventory")
        return sorted(ids)

    def mounted(self, scope, selected):
        target = selected["mount_path"]; present = False
        for line in agent.read(self.inventory, 1024 ** 2).decode().splitlines():
            fields = line.partition(" - ")[0].split()
            if len(fields) < 6: raise ValueError("Invalid host mount inventory")
            if fields[4].startswith(target + "/"): raise ValueError("Nested mounts are outside the preparation scope")
            if fields[4] == target: present = True
        return agent.mount_identity(scope, selected, target, inventory=self.inventory) if present else None

    def empty(self, selected):
        path = Path(selected["mount_path"])
        if path.resolve(strict=True) != path or path.is_symlink(): raise ValueError("Use the original direct mountpoint path")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or any(path.iterdir()): raise ValueError("Prepare a root-owned empty private mountpoint")
        return {"device": info.st_dev, "inode": info.st_ino}

    def mount(self, plan, scope, selected, expected):
        with self.credential(plan["hosts"][selected["name"]], selected) as (descriptor, metadata):
            if metadata != expected["credential"]: raise ValueError("The original credential-file handle changed before mounting")
            result = run_bounded(arguments(plan, scope, selected, self.helper, descriptor), input="", text=True, env=CLEAN, timeout=75, output_max_bytes=65536, check=False)
            if result.returncode: raise ValueError("The mount helper failed; observe retained state before retry")

    def descriptor(self, scope, selected, mounted):
        fd = os.open(selected["mount_path"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try: return agent.descriptor_identity(fd, mounted)
        finally: os.close(fd)


def operate(plan, native, storage, runtime, host_name, *, mode="local", output=None, host=None):
    validate(plan, native, storage, runtime, host_name)
    if mode not in {"local", "check-host", "create", "resume"}: raise ValueError("Select an explicit mount preparation mode")
    result = {"local_inputs_verified": True, "cephfs_mount_prepared": False, **UNVERIFIED}
    if mode == "local": return result
    host = host or Host(); selected = next(h for h in native["hosts"] if h["name"] == host_name); pins = plan["hosts"][host_name]
    scope = renderer.scope(native, storage, runtime); current = host.prerequisites(pins, selected); mounted = host.mounted(scope, selected)
    if mode == "check-host":
        if mounted is None: host.empty(selected)
        return {**result, "host_checked": True, "existing_matching_mount": mounted is not None}
    if output is None: raise ValueError("Use an original private mount attempt directory")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if (not relative.parts or output.is_symlink() or any(resolved == Path(selected[k]) or resolved in Path(selected[k]).parents or Path(selected[k]) in resolved.parents for k in ("mount_path", "package_path"))): raise ValueError("Keep the private journal outside mount and package paths")
    exists = resolved.exists()
    if mode == "create" and exists or mode == "resume" and not exists: raise ValueError("Use a new create directory or the original resume journal")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=mode == "resume")
    info = resolved.stat()
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Untrusted mount attempt directory")
    source = toolset(); plan_hash = renderer.storage.fingerprint(plan); scope_hash = renderer.storage.fingerprint(scope); path = resolved / "mount-journal.json"
    with host.target_lock(selected), renderer.csi.shared.lock(resolved):
        mounted = host.mounted(scope, selected)
        if mode == "resume":
            state = renderer.private_receipt(path)
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("plan_sha256") != plan_hash
                    or state.get("scope_sha256") != scope_hash or state.get("host") != host_name or state.get("runtime") != runtime or state.get("toolset_sha256") != source
                    or state.get("host_snapshot") != current or type(state.get("mount_intent")) is not bool
                    or not isinstance(state.get("initial_mount_ids"), list) or any(not renderer.storage.integer(v, 1, 2 ** 63 - 1) for v in state["initial_mount_ids"])): raise ValueError("The original mount attempt changed")
        else:
            if mounted is not None: raise ValueError("A new mount operation cannot adopt an existing mount")
            state = {"schema_version": 1, "plan_sha256": plan_hash, "scope_sha256": scope_hash, "host": host_name, "runtime": runtime,
                "toolset_sha256": source, "host_snapshot": current, "directory": host.empty(selected), "initial_mount_ids": host.inventory_ids(), "mount_intent": False, "mount": None}
        def record(phase):
            state["phase"] = phase; atomic_write_text(path, renderer.storage.canonical(state) + "\n")
        try:
            record("prepared")
            if state["mount_intent"]:
                if mounted is None: raise ValueError("An uncertain or removed mount is never requested again")
            else:
                if mounted is not None or host.empty(selected) != state["directory"] or host.prerequisites(pins, selected) != current or toolset() != source: raise ValueError("Mount prerequisites changed before the request")
                state["mount_intent"] = True; record("mount-intent")
                host.mount(plan, scope, selected, current)
                mounted = host.mounted(scope, selected)
            if mounted is None or mounted["id"] in state["initial_mount_ids"] or state["mount"] is not None and state["mount"] != mounted: raise ValueError("The observed mount does not match the original new mount scope")
            descriptor = host.descriptor(scope, selected, mounted)
            state["mount"] = mounted; record("mount-observed")
            if host.prerequisites(pins, selected) != current or host.mounted(scope, selected) != mounted or host.descriptor(scope, selected, mounted) != descriptor or toolset() != source: raise ValueError("Mount evidence changed before completion")
            proof = {"schema_version": 1, "operation_id": plan["operation_id"], "plan_sha256": plan_hash, "native_plan_sha256": plan["native_plan_sha256"], "scope_sha256": scope_hash,
                "host": host_name, "runtime": runtime, "toolset_sha256": source, "mount": mounted, "directory": descriptor, "observed_at": datetime.now(timezone.utc).isoformat(),
                "cephfs_mount_prepared": True, **UNVERIFIED}
            atomic_write_text(resolved / "cephfs-mount-proof.json", renderer.storage.canonical(proof) + "\n"); record("completed")
            return {**result, "cephfs_mount_prepared": True}
        except BaseException:
            record("failed-observe-original-mount"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--native-plan", type=Path, required=True); parser.add_argument("--storage-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=("raw", "docker"), required=True); parser.add_argument("--host", required=True); parser.add_argument("--output-dir", type=Path)
    modes = parser.add_mutually_exclusive_group(); modes.add_argument("--check-host", action="store_true"); modes.add_argument("--create", action="store_true"); modes.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = [loads_strict_json(read_bounded_bytes(p, max_bytes=256 * 1024)) for p in (args.plan, args.native_plan, args.storage_plan)]
        mode = "create" if args.create else "resume" if args.resume else "check-host" if args.check_host else "local"
        print(renderer.storage.canonical(operate(*values, args.runtime, args.host, mode=mode, output=args.output_dir))); return 0
    except Exception:
        print("CephFS mount preparation stopped; retain the original private journal and inspect the host mount state. No credentials were printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
