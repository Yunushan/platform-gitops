#!/usr/bin/env python3
"""Prepare pinned OpenSSH files for a disposable, isolated Zuul worker test."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "openssh-server_1%3a10.2p1-2ubuntu3.8_amd64.deb": "d14910ed045bd4a2a4c9a4eeb6b3edf08d527fb4cdee5cf527fe66b8bb364c31",
    "openssh-sftp-server_1%3a10.2p1-2ubuntu3.8_amd64.deb": "8093b5bbc911802f946069953640796ecc40234cf25aea3892f0b05304908ed4",
    "libwrap0_7.6.q-36build2_amd64.deb": "a3784ca083c626c3ae41cdd32d4c5898554650dc77d2b54dbb1197a38198adf8",
}


def prepare(source: Path, output: Path) -> dict:
    if sys.platform != "linux":
        raise ValueError("The reviewed package fixture requires Linux")
    private = (ROOT / "private").resolve()
    target = output.resolve()
    if not target.is_relative_to(private) or target == private or output.exists() or output.is_symlink():
        raise ValueError("Output must be a new private directory")
    for name, expected in PACKAGES.items():
        path = source / name
        if path.is_symlink() or hashlib.sha256(read_bounded_bytes(path, max_bytes=4 * 1024 * 1024)).hexdigest() != expected:
            raise ValueError("A reviewed fixture package is missing or changed")
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in PACKAGES:
        result = run_bounded(["dpkg-deb", "--extract", str(source / name), str(target)],
                             timeout=30, text=True, input="", output_max_bytes=4096, check=False)
        if result.returncode:
            raise ValueError("Fixture package extraction failed")
    required = ("usr/sbin/sshd", "usr/lib/openssh/sshd-session", "usr/lib/openssh/sshd-auth",
                "usr/lib/openssh/sftp-server", "usr/lib/x86_64-linux-gnu/libwrap.so.0")
    if any(not (target / name).is_file() for name in required):
        raise ValueError("Fixture package components are incomplete")
    return {"prepared": True, "installed": False, "service_started": False,
            "network_isolation_verified": False, "job_execution_verified": False}


def tree_identity(root: Path) -> dict[str, tuple[str, str]]:
    """Record names and contents without following extracted symlinks."""
    entries: dict[str, tuple[str, str]] = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[relative] = ("symlink", os.readlink(path))
            elif path.is_dir():
                entries[relative] = ("directory", "")
            elif path.is_file():
                entries[relative] = ("file", hashlib.sha256(read_bounded_bytes(path, max_bytes=16 * 1024 * 1024)).hexdigest())
            else:
                raise ValueError("Unsupported worker fixture artifact type")
    return entries


def verify(source: Path, existing: Path) -> dict:
    private = (ROOT / "private").resolve()
    target = existing.resolve(strict=True)
    if existing.is_symlink() or not target.is_relative_to(private) or target == private or not target.is_dir():
        raise ValueError("Worker artifact must be a private directory")
    with tempfile.TemporaryDirectory(prefix="zuul-worker-verify-", dir=private) as directory:
        expected = Path(directory) / "expected"
        prepare(source, expected)
        expected_identity = tree_identity(expected)
        if tree_identity(target) != expected_identity:
            raise ValueError("Worker artifact does not match the reviewed packages")
    return {"package_tree_verified": True, "entries": len(expected_identity),
            "permissions_verified": False, "service_started": False, "job_execution_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-dir", type=Path)
    mode.add_argument("--verify-existing", type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.packages, args.output_dir) if args.output_dir else verify(args.packages, args.verify_existing)
    except (ValueError, OSError, RuntimeError):
        print("Worker fixture preparation failed; inspect the private input packages and output path.", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
