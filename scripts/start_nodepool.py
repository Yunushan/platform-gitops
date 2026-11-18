#!/usr/bin/env python3
"""Check local Nodepool inputs by default; launch only with explicit --start."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded


TLS_FILES = ("zk-client.pem", "zk-client.key", "zk-ca.pem")


def local_inputs(expected_hash: str, *, root: Path = Path("/etc/nodepool")) -> dict:
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("An exact reviewed configuration digest is required")
    payload = read_bounded_bytes(root / "nodepool.yaml", max_bytes=4 * 1024 ** 2)
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("Nodepool configuration differs from the rendered contract")
    for filename in TLS_FILES:
        metadata = (root / "private" / filename).stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4 * 1024 ** 2:
            raise ValueError("Provision bounded regular ZooKeeper TLS input files")
    return {"read_only": True, "local_inputs_verified": True, "connections_verified": False,
            "registered": False, "allocation_verified": False, "ha_verified": False}


def check_release(binary: str, expected: str) -> None:
    if not isinstance(expected, str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", expected):
        raise ValueError("Declare the exact reviewed stable Nodepool release")
    result = run_bounded([binary, "--version"], timeout=20, text=True, input="", output_max_bytes=4096, check=False)
    versions = set(re.findall(r"(?<![\w.+-])[0-9]+\.[0-9]+\.[0-9]+(?![\w.+-])", result.stdout))
    if result.returncode or versions != {expected}:
        raise ValueError("Installed Nodepool tools do not match the reviewed release")


def launch(version: str, digest: str, *, root: Path = Path("/etc/nodepool"), state: Path = Path("/var/lib/nodepool")) -> None:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("Nodepool requires a dedicated non-root Linux service identity")
    local_inputs(digest, root=root)
    if not state.is_dir() or state.is_symlink():
        raise ValueError("Provision the dedicated writable launcher state directory")
    launcher, cli = shutil.which("nodepool-launcher"), shutil.which("nodepool")
    if not launcher or not cli:
        raise ValueError("Reviewed Nodepool launcher and validation tools are required")
    check_release(launcher, version)
    check_release(cli, version)
    validation = run_bounded([cli, "-c", str(root / "nodepool.yaml"), "config-validate"],
                             timeout=60, text=True, input="", output_max_bytes=65536, check=False)
    if validation.returncode:
        raise ValueError("Pinned Nodepool configuration validation failed")
    # Re-check after invoking the external validator; a mutable host file must
    # not change unnoticed before the launch handoff.
    local_inputs(digest, root=root)
    os.environ["HOME"] = str(state)
    os.execv(launcher, [launcher, "-f", "-c", str(root / "nodepool.yaml"), "-p", str(state / "launcher.pid"), "--no-webapp"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--expected-version")
    args = parser.parse_args(argv)
    try:
        if args.start:
            if not args.expected_version:
                raise ValueError("Explicit launch requires a selected release")
            launch(args.expected_version, args.config_sha256)
            raise ValueError("Launcher exec returned unexpectedly")
        if args.expected_version:
            raise ValueError("Release selection is only used for explicit startup")
        print(json.dumps(local_inputs(args.config_sha256)))
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print("Nodepool input/launch check stopped; inspect private configuration, TLS provisioning and pinned tool versions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
