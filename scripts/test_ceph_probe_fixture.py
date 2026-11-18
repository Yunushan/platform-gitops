#!/usr/bin/env python3
"""Exercise block ioctls and full blank scans on an explicitly owned temporary loop device."""

import hashlib
import errno
import json
import os
from pathlib import Path
import re
import tempfile
import time

from bounded_subprocess import run_bounded
import ceph_device_probe as probe


def exercise(test):
    if os.name != "posix" or os.geteuid() != 0: raise ValueError("This opt-in fixture requires a disposable root Linux environment")
    def command(args):
        result = run_bounded(["/usr/sbin/losetup", *args], env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}, input="", text=True, check=False, timeout=15, output_max_bytes=16384)
        if result.returncode: raise ValueError("Disposable loop-device fixture failed; device/system diagnostics suppressed")
        return result.stdout
    with tempfile.TemporaryDirectory(prefix="ceph-probe-fixture-", dir="/run") as temporary:
        root = Path(temporary).resolve()
        if root.parent != Path("/run") or not root.name.startswith("ceph-probe-fixture-"): raise ValueError("Unexpected fixture location")
        backing = root / "disk"; size = 8 * 1024 ** 2
        with backing.open("wb") as stream: stream.truncate(size)
        def mapped(loop):
            rows = json.loads(command(["--json", "--list", "--output", "NAME,BACK-FILE", loop]))["loopdevices"]
            return len(rows) == 1 and rows[0].get("name") == loop and Path(rows[0].get("back-file", "")).resolve() == backing
        for corrupted in (False, True):
            if corrupted:
                with backing.open("r+b") as stream: stream.seek(size - 1); stream.write(b"x"); stream.flush(); os.fsync(stream.fileno())
            loop = command(["--find", "--show", str(backing)]).strip()
            if not re.fullmatch(r"/dev/loop[0-9]+", loop) or not mapped(loop): raise ValueError("The loop fixture did not return its original backing-file handle")
            descriptor = None
            try:
                initial = hashlib.sha256(backing.read_bytes()).hexdigest()
                descriptor = os.open(loop, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
                test.assertEqual(probe.block_size(descriptor), size)
                if corrupted:
                    with test.assertRaises(ValueError): probe.scan_zero(descriptor, size, time.monotonic() + 10)
                else:
                    test.assertEqual(probe.scan_zero(descriptor, size, time.monotonic() + 10), hashlib.sha256(bytes(size)).hexdigest())
                    with test.assertRaises(OSError) as collision:
                        second = os.open(loop, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
                        os.close(second)
                    test.assertEqual(collision.exception.errno, errno.EBUSY)
                test.assertEqual(hashlib.sha256(backing.read_bytes()).hexdigest(), initial)
            finally:
                if descriptor is not None: os.close(descriptor)
                if not mapped(loop): raise ValueError("The original loop association changed; refuse to detach a foreign device")
                command(["--detach", loop])
