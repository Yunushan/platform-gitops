#!/usr/bin/env python3
"""Opt-in initialization of NEW native Gerrit sites; never upgrade existing data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from bounded_file import read_bounded_text
from bounded_subprocess import run_bounded
import render_gerrit_sites as sites
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json


# Resolve the scripts directory without following the projected file's symlink
# into a Kubernetes ConfigMap revision directory. Reports are mounted beside scripts.
ROOT = Path(__file__).parent.resolve().parent
CONFIRMATION = "INITIALIZE_NEW_GERRIT_SITES"
PRIVATE_INPUTS = ("etc/secure.config", "etc/keystore", "etc/ssh_host_ed25519_key")


class InitializationError(ValueError):
    pass


def file_hash(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 ** 3:
            raise InitializationError("Input is not a bounded regular file")
        digest = hashlib.sha256()
        remaining = 1024 ** 3 + 1
        while chunk := stream.read(min(1024 ** 2, remaining)):
            remaining -= len(chunk)
            if remaining <= 0:
                raise InitializationError("Input grew beyond the limit")
            digest.update(chunk)
    return digest.hexdigest()


def config_value(config: Path, key: str) -> str:
    result = run_bounded(["git", "config", "--no-includes", "--file", str(config), "--get-all", key],
                         timeout=15, text=True, output_max_bytes=8192, check=False)
    if result.returncode or len(result.stdout.splitlines()) != 1 or not result.stdout.strip():
        raise InitializationError("A required configuration field is missing or ambiguous")
    return result.stdout.strip()


def check_config(config: Path, shared: Path, instance: str) -> None:
    secure = config.with_name("secure.config")
    includes = run_bounded(["git", "config", "--no-includes", "--file", str(config),
                            "--name-only", "--get-regexp", "^include"],
                           timeout=15, text=True, output_max_bytes=8192, check=False)
    if includes.returncode != 1:
        raise InitializationError("External configuration includes are not accepted during initialization")
    overrides = run_bounded(["git", "config", "--no-includes", "--file", str(secure),
                             "--name-only", "--get-regexp", "^(include|gerrit[.]|auth[.]type$)"],
                            timeout=15, text=True, output_max_bytes=8192, check=False)
    if overrides.returncode != 1:
        raise InitializationError("Private configuration must not override initialization targets or authentication mode")
    # Gerrit init generates this key when absent, changing the reviewed file
    # after the irreversible shared repository creation has begun.
    try:
        config_value(secure, "auth.registerEmailPrivateKey")
    except InitializationError:
        raise InitializationError("Set exactly one Gerrit email signing key in each private secure.config before initialization") from None
    if config_value(config, "gerrit.basePath") != str(shared / "git"):
        raise InitializationError("Git basePath must be the selected new shared Git directory")
    if config_value(config, "gerrit.instanceId") != instance:
        raise InitializationError("Each primary must have its own reviewed instance ID")
    if config_value(config, "auth.type").upper() == "DEVELOPMENT_BECOME_ANY_ACCOUNT":
        raise InitializationError("Development authentication is not accepted")


def check_server_id_pair(first: Path, second: Path) -> str:
    server_ids = [config_value(site / "etc/gerrit.config", "gerrit.serverId") for site in (first, second)]
    if server_ids[0] != server_ids[1]:
        raise InitializationError("Both Gerrit primaries must use the same explicit server ID")
    return server_ids[0]


def targets(plan: dict) -> tuple[list[Path], Path]:
    return [Path(site["host_path"]) for site in plan["sites"]], Path(plan["shared_host_path"])


def preflight(plan: dict, inputs: Path) -> tuple[list[Path], Path]:
    sites.validate(plan)
    directories, shared = targets(plan)
    if "shared_storage" in plan: sites.storage_guard.check(plan["shared_storage"], shared)
    if not shared.is_dir() or shared.is_symlink() or any(shared.iterdir()):
        raise InitializationError("The independently mounted shared directory must exist and be empty")
    for directory in directories:
        if directory.exists() or directory.is_symlink() or not directory.parent.is_dir():
            raise InitializationError("Site targets must be new directories with existing parents")
        if directory.resolve() != directory.absolute():
            raise InitializationError("Symlinked site target ancestors are not accepted")
    if shared.resolve() != shared.absolute():
        raise InitializationError("Symlinked shared target ancestors are not accepted")
    for site in plan["sites"]:
        source = inputs / site["name"]
        sites.verify_site(plan, site["name"], source)
        for relative in PRIVATE_INPUTS:
            path = source / relative
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(source.resolve()):
                raise InitializationError("Private input escapes its site directory")
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4 * 1024 ** 2:
                raise InitializationError("Required private input is absent or not a bounded regular file")
        check_config(source / "etc/gerrit.config", shared, site["name"])
        if "shared_storage" in plan: sites.storage_guard.check_configuration(source, shared)
        if plan["schema_version"] == 2: sites.storage_guard.check_health_configuration(source)
    if "shared_storage" in plan:
        check_server_id_pair(*(inputs / site["name"] for site in plan["sites"]))
    if file_hash(inputs / plan["sites"][0]["name"] / "etc/ssh_host_ed25519_key") != file_hash(inputs / plan["sites"][1]["name"] / "etc/ssh_host_ed25519_key"):
        raise InitializationError("Both primary SSH private-key inputs must match")
    return directories, shared


def private_copy(source: Path, destination: Path) -> None:
    # A new target only. Inputs must be regular files; never follow final symlinks.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as incoming:
        metadata = os.fstat(incoming.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 ** 3:
            raise InitializationError("Copy source is not a bounded regular file")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(output, "wb") as outgoing:
            remaining = 1024 ** 3 + 1
            while chunk := incoming.read(min(1024 ** 2, remaining)):
                remaining -= len(chunk)
                if remaining <= 0:
                    raise InitializationError("Copy source grew beyond its limit")
                outgoing.write(chunk)


def initialize(plan: dict, inputs: Path, *, java: str) -> dict:
    directories, shared = preflight(plan, inputs)
    original_mount = sites.storage_guard.check(plan["shared_storage"], shared) if "shared_storage" in plan else None
    # Reserve both destinations exclusively before any shared Git initialization.
    # Failures leave all new files for private inspection; no rollback deletes data.
    for directory in directories:
        directory.mkdir(mode=0o700, exist_ok=False)
    for site, destination in zip(plan["sites"], directories):
        for relative in (*sites.ARTIFACTS, *sites.site_locks(plan), "etc/ssh_host_ed25519_key.pub", *PRIVATE_INPUTS):
            private_copy(inputs / site["name"] / relative, destination / relative)
        sites.verify_site(plan, site["name"], destination)
        check_config(destination / "etc/gerrit.config", shared, site["name"])
        if "shared_storage" in plan: sites.storage_guard.check_configuration(destination, shared)
        if plan["schema_version"] == 2: sites.storage_guard.check_health_configuration(destination)
    server_id = check_server_id_pair(*directories) if "shared_storage" in plan else None
    if any(shared.iterdir()):
        raise InitializationError("Shared storage changed during preparation; initialization stopped")
    if original_mount is not None and sites.storage_guard.check(plan["shared_storage"], shared) != original_mount:
        raise InitializationError("The original shared CephFS mount changed during preparation")
    marker = os.open(shared / ".platform-gerrit-init", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(marker, "w", encoding="utf-8") as stream:
        stream.write("New-store initialization reserved; preserve for recovery review.\n")
    first, second = directories
    # Version 2 health configuration contains an authentication-check password.
    # Initialization must preserve it just like the other private bootstrap files.
    private_names = (*PRIVATE_INPUTS, "etc/healthcheck.config") if plan["schema_version"] == 2 else PRIVATE_INPUTS
    private_before = {name: file_hash(first / name) for name in private_names}
    result = run_bounded([java, "-jar", str(first / "bin/gerrit.war"), "init", "--batch",
                          "--no-auto-start", "--skip-all-downloads", "-d", str(first)],
                         timeout=600, text=True, output_max_bytes=1024 ** 2, check=False)
    if result.returncode:
        raise InitializationError("Initialization failed; new private state was retained")
    if any(file_hash(first / name) != digest for name, digest in private_before.items()):
        raise InitializationError("Initialization changed private bootstrap state; inspect before preparing the second primary")
    if server_id is not None and check_server_id_pair(first, second) != server_id:
        raise InitializationError("Initialization changed the original Gerrit server ID")
    if not all((shared / "git" / name).is_dir() for name in ("All-Projects.git", "All-Users.git")):
        raise InitializationError("Initialization did not create the required shared repositories")
    # Clone initial local state, not shared Git, before applying the second
    # primary's reviewed configuration. Never run init on shared data twice.
    # The second reserved destination contains only the validated inputs above.
    for path in first.rglob("*"):
        if path.is_symlink():
            raise InitializationError("Initialized site contains a symlink; inspect manually")
        relative = path.relative_to(first)
        target = second / relative
        if path.is_dir():
            target.mkdir(mode=0o700, exist_ok=True)
        elif not target.exists():
            private_copy(path, target)
    draft = copy.deepcopy(plan)
    for site, directory in zip(draft["sites"], directories):
        check_config(directory / "etc/gerrit.config", shared, site["name"])
        if "shared_storage" in plan: sites.storage_guard.check_configuration(directory, shared)
        if plan["schema_version"] == 2: sites.storage_guard.check_health_configuration(directory)
        site["config_hashes"] = {name: file_hash(directory / name) for name in sites.site_locks(plan)}
        # Keep the originally approved artifact locks; fail if init altered them.
        sites.verify_site(draft, site["name"], directory)
    return draft


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        preflight(plan, args.inputs)
        if args.apply:
            if sys.platform != "linux" or os.geteuid() == 0 or args.confirm != CONFIRMATION or not args.output_dir:
                raise InitializationError("Apply requires a Linux preparation host, confirmation and private draft output")
            java = shutil.which("java")
            if not java:
                raise InitializationError("An approved compatible Java runtime is required")
            # Check private output location BEFORE starting any initialization.
            relative = args.output_dir.resolve().relative_to((ROOT / "private").resolve())
            if not relative.parts or args.output_dir.exists() or args.output_dir.is_symlink():
                raise InitializationError("Draft output must be a new ignored private directory")
            draft = initialize(plan, args.inputs, java=java)
            write_bundle({"gerrit-sites-draft.json": json.dumps(draft, indent=2) + "\n"},
                         args.output_dir, private_root=ROOT / "private")
        elif args.confirm or args.output_dir:
            raise InitializationError("Apply-only arguments are not accepted during preflight")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print("Gerrit initialization stopped. Review private inputs and new targets; any created state was retained. No daemon was started by this helper.", file=sys.stderr)
        return 1
    print(json.dumps({"preflight_passed": True, "initialization_performed": args.apply,
                      "read_only": not args.apply, "ha_verified": False, "deployed": False,
                      "draft_requires_review": args.apply}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
