#!/usr/bin/env python3
"""Stage and install a pinned RKE2 tar release from a local cache."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
CNI = {"canal", "cilium", "calico", "flannel", "none"}


def open_regular(path, maximum):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    stream = os.fdopen(os.open(path, flags), "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
        stream.close()
        raise ValueError("Unsafe cache file")
    return stream


def copy_verified(root, stage, name, expected, maximum):
    digest = hashlib.sha256()
    with open_regular(root / name, maximum) as src, create_staged_file(stage / name) as dst:
        remaining = maximum
        while chunk := src.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("Cache file exceeded bound")
            digest.update(chunk)
            dst.write(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("Cache digest mismatch")


def create_staged_file(path):
    # This standalone bootstrap program owns a private temporary directory.
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")


def stage_release(args, stage, arch):
    checksum_name = f"sha256sum-{arch}.txt"
    with open_regular(args.cache / checksum_name, 1024 ** 2) as stream:
        checksums = stream.read(1024 ** 2 + 1)
    if hashlib.sha256(checksums).hexdigest() != args.checksums_sha256:
        raise ValueError("Review the exact release checksum file")
    names = {
        f"rke2.linux-{arch}.tar.gz": 2 * 1024 ** 3,
        f"rke2-images.linux-{arch}.tar.zst": 8 * 1024 ** 3,
    }
    for cni in args.cni.split(","):
        if cni in CNI - {"none", "canal"}:
            names[f"rke2-images-{cni}.linux-{arch}.tar.zst"] = 4 * 1024 ** 3
    selected = []
    for name, maximum in names.items():
        lines = re.findall(rb"(?m)^([0-9a-f]{64}) [ *]" + re.escape(name.encode()) + rb"\r?$", checksums)
        if len(lines) != 1:
            raise ValueError("Missing or ambiguous release checksum")
        digest = lines[0].decode()
        copy_verified(args.cache, stage, name, digest, maximum)
        selected.append(f"{digest}  {name}\n")
    with create_staged_file(stage / checksum_name) as stream:
        stream.write("".join(selected).encode("ascii"))
    copy_verified(args.cache, stage, "install.sh", args.installer_sha256, 2 * 1024 ** 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--checksums-sha256", required=True)
    parser.add_argument("--cni", required=True)
    parser.add_argument("--role", choices=("server", "agent"), default="server")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        arch = ARCHITECTURES[os.uname().machine]
        cnis = args.cni.split(",")
        if (not args.cache.is_absolute() or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+\+rke2r[0-9]+", args.version)
                or any(not re.fullmatch(r"[0-9a-f]{64}", pin) for pin in (args.installer_sha256, args.checksums_sha256))
                or not set(cnis) <= CNI or len(cnis) != len(set(cnis)) or ("none" in cnis and len(cnis) > 1)):
            raise ValueError("Use an absolute cache, exact release, supported CNI and reviewed digests")
        with tempfile.TemporaryDirectory(prefix="rke2-offline-") as temporary:
            stage = Path(temporary)
            stage_release(args, stage, arch)
            if not args.verify_only:
                env = {key: value for key, value in os.environ.items() if not key.startswith("INSTALL_RKE2_")}
                env.update(INSTALL_RKE2_TYPE=args.role, INSTALL_RKE2_VERSION=args.version,
                           INSTALL_RKE2_METHOD="tar", INSTALL_RKE2_ARTIFACT_PATH=str(stage))
                subprocess.run(["/bin/sh", str(stage / "install.sh")], env=env, check=True)
        print(f"Verified offline RKE2 artifacts for {args.version} ({arch}).")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Offline RKE2 install stopped. Check the reviewed installer, checksum file, and required release archives.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
