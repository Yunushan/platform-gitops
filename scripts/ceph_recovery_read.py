"""Read persisted bytes from the original client canary on a read-only mount."""

import hashlib
import os
from pathlib import Path
import runpy
import stat
import sys
import uuid


FILES = {"rbd": ("rbd",), "cephfs": ("cephfs-left", "cephfs-right")}


def read_payloads(program, scope, kind, root):
    name = ".platform-canary-" + uuid.UUID(scope["operation_id"]).hex
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(root, flags)
    try:
        directory = os.open(name, flags, dir_fd=root_fd)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("The original canary directory changed")
            hashes = {}
            for identity in FILES[kind]:
                data = program["file_read"](directory, identity + ".bin", scope["payload_bytes"])
                if data != program["payload"](scope, identity):
                    raise ValueError("Persisted canary bytes differ")
                hashes[identity] = hashlib.sha256(data).hexdigest()
            return hashes
        finally:
            os.close(directory)
    finally:
        os.close(root_fd)


def main():
    kind, phase = sys.argv[1:]
    program = runpy.run_path("/program/client.py")
    scope = program["load"](Path("/program/scope.json"))
    node = scope["nodes"]["rbd-read"]
    if sys.platform != "linux" or os.geteuid() == 0 or os.environ.get("NODE_NAME") != node:
        raise ValueError("Require the original non-root reader node")
    filesystem = program["mounted"](kind, True)
    hashes = read_payloads(program, scope, kind, Path("/volume"))
    print(program["canonical"]({"phase": phase, "node": node, "scope_sha256": program["digest"](scope),
                                "filesystem": filesystem, "payloads_sha256": hashes}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("The original Ceph canary read failed.", file=sys.stderr)
        raise SystemExit(1)
