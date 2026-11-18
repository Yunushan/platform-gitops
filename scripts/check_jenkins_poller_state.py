#!/usr/bin/env python3
"""Check existing private poller directories before starting either poller."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def check_local_lock(directory):
    import fcntl
    descriptor, name = tempfile.mkstemp(prefix=".lock-check-", dir=directory)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = os.fork()
        if child == 0:
            try:
                os.close(descriptor)
                other = os.open(name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                try:
                    try: fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError: os._exit(0)
                    os._exit(1)
                finally: os.close(other)
            except BaseException: os._exit(2)
        _, status = os.waitpid(child, 0)
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0: raise ValueError("local-lock-exclusion-failed")
    finally:
        os.close(descriptor)
        os.unlink(name)


def check(root):
    if os.name != "posix" or os.geteuid() == 0: raise ValueError("nonroot-posix-required")
    root = Path(root)
    private = root / "private"
    if private.is_symlink() or not private.is_dir(): raise ValueError("private-state-required")
    observed = []
    for name in ("queue", "reviews"):
        path = private / name
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("private-directory-owner-or-mode-invalid")
        # Verify effective write access with an exclusively created temporary
        # file, then remove it; never inspect or change existing reservations.
        with tempfile.TemporaryFile(dir=path) as test_file:
            test_file.write(b"poller-state-check"); test_file.flush(); os.fsync(test_file.fileno())
        observed.append((info.st_dev, info.st_ino))
    if observed[0][0] != observed[1][0] or observed[0][1] == observed[1][1]:
        raise ValueError("distinct-directories-on-same-filesystem-required")
    check_local_lock(private / "queue")
    return {"state_directories_ready": True, "local_process_lock_verified": True,
            "cross_node_locking_verified": False, "storage_ha_verified": False}


def main():
    try: result = check(Path(__file__).resolve().parents[1])
    except Exception:
        print("Poller state check failed; inspect private directory ownership, permissions and storage.", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
