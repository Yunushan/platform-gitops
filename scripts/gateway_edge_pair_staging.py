#!/usr/bin/env python3
"""Bounded, standard-library-only SSH input staging; never starts host services."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tempfile

from strict_json import loads_strict_json


HEADER_LIMIT = 128 * 1024
TOTAL_LIMIT = 160 * 1024 ** 2
FILE_LIMIT = 64 * 1024 ** 2
SHA = re.compile(r"[0-9a-f]{64}")


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


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value): return False
    parts = PurePosixPath(value).parts
    return (len(parts) >= 4 and parts[1] in {"opt", "srv", "var", "run"}
            and (parts[1] != "var" or parts[2] == "lib") and not any(p in {".", ".."} for p in value.split("/"))
            and not (value == "/var/lib/platform-gateway-edge" or value.startswith("/var/lib/platform-gateway-edge/")))


def validate(header):
    if (not isinstance(header, dict) or set(header) != {"schema_version", "machine_sha256", "roots"}
            or type(header["schema_version"]) is not int or header["schema_version"] != 1
            or not isinstance(header["machine_sha256"], str) or not SHA.fullmatch(header["machine_sha256"])
            or not isinstance(header["roots"], dict) or set(header["roots"]) not in ({"tools", "bundle", "gateway", "ca"}, {"tools", "bundle", "gateway", "ca", "artifacts"})):
        raise ValueError("Invalid input staging scope")
    paths, total, count = [], 0, 0
    for label, root in header["roots"].items():
        if (not isinstance(root, dict) or set(root) != {"path", "kind", "files"} or not safe_path(root["path"])
                or root["kind"] != ("file" if label == "ca" else "directory") or not isinstance(root["files"], dict) or not root["files"]):
            raise ValueError("Invalid isolated staging destination")
        paths.append(PurePosixPath(root["path"]))
        for name, item in root["files"].items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
                    or any(p in {".", ".."} for p in name.split("/"))
                    or not isinstance(item, dict) or set(item) != {"size", "sha256", "mode"}
                    or type(item["size"]) is not int or not 0 <= item["size"] <= FILE_LIMIT
                    or not isinstance(item["sha256"], str) or not SHA.fullmatch(item["sha256"])
                    or type(item["mode"]) is not int or item["mode"] not in {0o644, 0o555}):
                raise ValueError("Invalid bounded input file")
            total += item["size"]; count += 1
        if label == "ca" and (set(root["files"]) != {"payload"} or root["files"]["payload"]["size"] > 256 * 1024):
            raise ValueError("Invalid CA payload")
        names = [PurePosixPath(n) for n in root["files"]]
        if any(a in b.parents for a in names for b in names if a != b): raise ValueError("File and directory paths overlap")
    if len(set(paths)) != len(paths) or any(a in b.parents for a in paths for b in paths if a != b):
        raise ValueError("Staged roots must be separate immutable paths")
    if total > TOTAL_LIMIT or count > 128: raise ValueError("Excessive input staging request")
    if len(canonical(header)) > HEADER_LIMIT: raise ValueError("Excessive staging header")


def exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(min(size - len(result), 1024 * 1024))
        if not chunk: raise ValueError("Incomplete staging stream; retain inputs for retry")
        result.extend(chunk)
    return bytes(result)


def trusted(path, *, directory=False):
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Staging path must be root-owned without symlinks or public writes")
        if part != path and not stat.S_ISDIR(info.st_mode): raise ValueError("Invalid staging ancestor")
    info = path.lstat()
    if directory and not stat.S_ISDIR(info.st_mode): raise ValueError("Expected staging directory")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1): raise ValueError("Expected an unlinked regular input file")
    return info


def mkdir(path, mode=0o755):
    if not path.exists() and not path.is_symlink():
        mkdir(path.parent)
        path.mkdir(mode=mode)
        sync(path.parent)
    trusted(path, directory=True)


def sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def digest_file(path, limit=FILE_LIMIT):
    trusted(path)
    value = read_bootstrap_bytes(path, limit, follow_symlinks=False)
    return len(value), hashlib.sha256(value).hexdigest()


def verify_file(path, item):
    info = trusted(path)
    if stat.S_IMODE(info.st_mode) != item["mode"] or digest_file(path) != (item["size"], item["sha256"]):
        raise ValueError("Existing staged input differs; use a new reviewed path")


def allowed_directories(files):
    return {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}


def inventory(directory, files, *, complete):
    trusted(directory, directory=True)
    allowed = allowed_directories(files)
    actual = set()
    for path in directory.rglob("*"):
        name = path.relative_to(directory).as_posix()
        if path.is_symlink(): raise ValueError("Symlink in isolated input directory")
        if path.is_dir():
            trusted(path, directory=True)
            if name not in allowed: raise ValueError("Unexpected staged directory")
        else:
            trusted(path)
            if name not in files: raise ValueError("Unexpected staged file")
            actual.add(name)
            if complete: verify_file(path, files[name])
    if complete and actual != set(files): raise ValueError("Staged directory is incomplete")


def stage(stream, expected):
    import fcntl
    if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Staging requires root Linux")
    length = struct.unpack(">I", exact(stream, 4))[0]
    if length > HEADER_LIMIT: raise ValueError("Excessive staging header")
    header = loads_strict_json(exact(stream, length))
    validate(header)
    if fingerprint(header) != expected: raise ValueError("Input staging fingerprint changed")
    with Path("/etc/machine-id").open("rb") as machine:
        machine_id = machine.read(257).strip()
    if not re.fullmatch(rb"[0-9a-f]{32}", machine_id) or hashlib.sha256(machine_id).hexdigest() != header["machine_sha256"]:
        raise ValueError("Remote physical-host identity differs; nothing staged")
    with ExitStack() as locks:
        caches, existing = {}, set()
        # All peer input destinations are checked before any final input is published.
        for label, root in sorted(header["roots"].items(), key=lambda item: item[1]["path"]):
            target = Path(root["path"]); mkdir(target.parent)
            key = hashlib.sha256(str(target).encode()).hexdigest()
            lock_path = target.parent / (".platform-entry-stage-" + key + ".lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            locks.callback(os.close, descriptor)
            if stat.S_IMODE(trusted(lock_path).st_mode) != 0o600: raise ValueError("Untrusted staging lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if target.exists() or target.is_symlink():
                if root["kind"] == "directory": inventory(target, root["files"], complete=True)
                else: verify_file(target, root["files"]["payload"])
                existing.add(label)
        for label, root in sorted(header["roots"].items()):
            if label in existing: continue
            target = Path(root["path"])
            claim = {"schema_version": 1, "root": root, "machine_sha256": header["machine_sha256"]}
            cache = target.parent / (".platform-entry-inputs-" + fingerprint(claim))
            if not cache.exists() and not cache.is_symlink():
                new = Path(tempfile.mkdtemp(prefix=".platform-entry-new-", dir=target.parent))
                descriptor = os.open(new / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    os.chmod(output.fileno(), 0o600); output.write(canonical(claim)); output.flush(); os.fsync(output.fileno())
                sync(new); os.rename(new, cache); sync(target.parent)
            if stat.S_IMODE(trusted(cache, directory=True).st_mode) != 0o700: raise ValueError("Untrusted transfer cache")
            claim_file = cache / "manifest.json"
            if stat.S_IMODE(trusted(claim_file).st_mode) != 0o600: raise ValueError("Untrusted transfer claim")
            if read_bootstrap_bytes(claim_file, HEADER_LIMIT, follow_symlinks=False) != canonical(claim): raise ValueError("Transfer cache scope differs")
            if {p.name for p in cache.iterdir()} - {"manifest.json", "files", "incoming"}: raise ValueError("Unexpected transfer cache entry")
            mkdir(cache / "files")
            inventory(cache / "files", root["files"], complete=False)
            caches[label] = cache
        for label, root in sorted(header["roots"].items()):
            for name, item in sorted(root["files"].items()):
                cache = caches.get(label)
                if cache:
                    incoming = cache / "incoming"
                    if incoming.exists() or incoming.is_symlink():
                        if stat.S_IMODE(trusted(incoming).st_mode) not in {0o600, 0o644, 0o555}: raise ValueError("Untrusted incomplete input")
                    descriptor = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    os.fchmod(descriptor, 0o600)
                    output = locks.enter_context(os.fdopen(descriptor, "wb"))
                digest = hashlib.sha256(); remaining = item["size"]
                while remaining:
                    chunk = exact(stream, min(remaining, 1024 * 1024)); remaining -= len(chunk); digest.update(chunk)
                    if cache: output.write(chunk)
                if digest.hexdigest() != item["sha256"]: raise ValueError("Transferred input fingerprint differs")
                if cache:
                    output.flush(); os.fchmod(output.fileno(), item["mode"]); os.fsync(output.fileno()); output.close()
                    destination = cache / "files" / name
                    mkdir(destination.parent)
                    os.replace(incoming, destination); sync(destination.parent)
        if stream.read(1): raise ValueError("Unexpected trailing staging input")
        for label, cache in sorted(caches.items()):
            root = header["roots"][label]; target = Path(root["path"])
            inventory(cache / "files", root["files"], complete=True)
            if target.exists() or target.is_symlink(): raise ValueError("Staged target appeared during transfer")
            source = cache / "files" if root["kind"] == "directory" else cache / "files/payload"
            os.rename(source, target); sync(target.parent)
        return {"staged": True, "manifest_sha256": expected, "machine_sha256": header["machine_sha256"]}


if __name__ == "__main__":
    try: print(canonical(stage(sys.stdin.buffer, sys.argv[1])).decode())
    except Exception:
        print('{"error":"input-staging-incomplete"}'); raise SystemExit(1)
