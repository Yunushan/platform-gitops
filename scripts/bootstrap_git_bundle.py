#!/usr/bin/env python3
"""Capture and recover a reviewed GitOps branch without contacting a forge."""

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from atomic_file import atomic_binary_output
from bounded_file import open_bounded_file
from bounded_subprocess import run_bounded


BUNDLE = "gitops.bundle"
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")


def git(repository, *args):
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL="file",
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-C", repository, *args]
    return run_bounded(command, env=env, timeout=900, check=True, text=True,
                       encoding="utf-8", errors="replace", output_max_bytes=4 * 1024 * 1024).stdout.strip()


def branch_ref(directory, branch, commit):
    if not isinstance(commit, str) or not OID.fullmatch(commit):
        raise ValueError("Use a full reviewed Git commit ID")
    if not isinstance(branch, str) or not branch or len(branch) > 240:
        raise ValueError("Select an explicit Git branch")
    ref = "refs/heads/" + branch
    git(directory, "check-ref-format", ref)
    return ref


def copy_and_hash(source, destination=None):
    source = source.resolve(strict=True)
    expected_size = source.stat().st_size
    digest = hashlib.sha256()
    staged_output = atomic_binary_output(destination, replace=False) if destination else nullcontext()
    with open_bounded_file(source, max_bytes=expected_size) as stream, staged_output as output:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if stream.tell() != expected_size:
            raise ValueError("Git bundle size changed during the copy")
    return digest.hexdigest()


def import_bundle(bundle, repository, ref, commit):
    object_format = "sha1" if len(commit) == 40 else "sha256"
    git(repository, "init", "--bare", "--template=", "--object-format=" + object_format)
    if git(repository, "bundle", "list-heads", bundle) != f"{commit} {ref}":
        raise ValueError("Bundle must contain exactly the reviewed branch and tip")
    # Unbundle into an empty object database so prerequisites and promisor objects cannot be satisfied externally.
    git(repository, "bundle", "unbundle", bundle)
    if git(repository, "cat-file", "-t", commit) != "commit":
        raise ValueError("The recovered tip must be a Git commit")
    git(repository, "update-ref", ref, commit)
    git(repository, "symbolic-ref", "HEAD", ref)
    git(repository, "fsck", "--full", "--strict", "--no-reflogs")


def create(source, branch, commit, output):
    source = source.resolve(strict=True)
    ref = branch_ref(source, branch, commit)
    if git(source, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Capture requires complete branch history")
    if git(source, "show-ref", "--verify", "--hash", ref) != commit:
        raise ValueError("The source branch has moved")
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix=".bundle-capture-", dir=output) as directory:
            stage = Path(directory)
            temporary = stage / BUNDLE
            git(source, "bundle", "create", temporary, ref)
            check = stage / "verify.git"
            check.mkdir(mode=0o700)
            import_bundle(temporary, check, ref, commit)
            temporary.chmod(0o600)
            digest = copy_and_hash(temporary)
            temporary.rename(output / BUNDLE)
    except Exception:
        shutil.rmtree(output)
        raise
    return digest


def restore(bundle, branch, commit, digest, output):
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ValueError("Supply the recorded SHA-256 from a trusted copy")
    if not bundle.is_file():
        raise ValueError("Supply a regular Git bundle file")
    ref = branch_ref(Path.cwd(), branch, commit)
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix=".bundle-restore-", dir=output.parent) as directory:
            local = Path(directory) / BUNDLE
            if copy_and_hash(bundle, local) != digest:
                raise ValueError("Bundle digest mismatch")
            import_bundle(local, output, ref, commit)
    except Exception:
        shutil.rmtree(output)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("create", help="Capture and test a standalone bundle from a local repository")
    capture.add_argument("--source", type=Path, required=True)
    recovery = commands.add_parser("restore", help="Recover the bundle into a new bare repository")
    recovery.add_argument("--bundle", type=Path, required=True)
    recovery.add_argument("--sha256", required=True)
    for command in (capture, recovery):
        command.add_argument("--branch", required=True)
        command.add_argument("--commit", required=True)
        command.add_argument("--output-dir", type=Path, required=True, help="New directory on private storage")
    args = parser.parse_args(argv)
    try:
        output = args.output_dir.parent.resolve(strict=True) / args.output_dir.name
        if args.command == "create":
            digest = create(args.source, args.branch, args.commit, output)
        else:
            digest = args.sha256
            restore(args.bundle, args.branch, args.commit, digest, output)
    except (ValueError, OSError, subprocess.SubprocessError):
        print("Git bootstrap bundle operation failed. Check the reviewed branch, commit, digest, "
              "complete local history, and fresh private output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"command": args.command, "commit": args.commit, "bundle_sha256": digest, "verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
