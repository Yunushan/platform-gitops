#!/usr/bin/env python3
"""Linux exec guard: bound a foreground restore postmaster to its original parent."""

import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import signal
import stat
import sys

from bounded_file import read_bounded_bytes


def execute(parent, uid, executable, digest, memory, arguments):
    if sys.platform != "linux" or os.geteuid() != uid or uid == 0 or os.getppid() != parent:
        raise ValueError("The original non-root restore parent is missing")
    info = executable.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o6022 or info.st_nlink != 1 or executable.name != "postgres"
            or hashlib.sha256(read_bounded_bytes(executable, max_bytes=64 * 1024 ** 2)).hexdigest() != digest):
        raise ValueError("The restore executable changed")
    try: capabilities = os.getxattr(executable, "security.capability")
    except OSError as error:
        import errno
        if error.errno not in {errno.ENODATA, errno.ENOTSUP}: raise
        capabilities = b""
    if capabilities: raise ValueError("The restore executable must not change credentials")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError("Restore parent-death/no-new-privileges guard is unavailable")
    # Close the race between Popen and arming PR_SET_PDEATHSIG.
    if os.getppid() != parent: raise ValueError("The restore parent exited before exec")
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    os.execve(executable, [str(executable), *arguments], dict(os.environ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True); parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--executable", type=Path, required=True); parser.add_argument("--sha256", required=True)
    parser.add_argument("--memory-bytes", type=int, required=True); parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        execute(args.parent, args.uid, args.executable, args.sha256, args.memory_bytes, args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments)
    except Exception:
        print("Restore postmaster launch refused; private details were suppressed.", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__": raise SystemExit(main())
