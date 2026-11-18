#!/usr/bin/env python3
"""Assemble or verify a portable offline Argo CD bootstrap directory."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile

from atomic_file import atomic_write_bytes
from bounded_file import read_bounded_bytes
from recovery_artifacts import copy_archive, new_recovery_directory, read_pinned_manifest, read_regular
from render_argocd_offline_images import parse_lock, pod_containers, workload_documents
from strict_yaml import loads_strict_yaml_all
from verify_oci_archive import verify_archive
import yaml


LOCK_NAME = "images.json"


def manifest_pins(repo):
    payload = read_bounded_bytes(repo / "ansible/playbooks/bootstrap-argocd.yml", max_bytes=1024 ** 2)
    playbook, = loads_strict_yaml_all(payload)
    play, = playbook
    values = play["vars"]
    return {
        "install.yaml": values["platform_argocd_core_manifest_sha256"],
        "install-ha.yaml": values["platform_argocd_ha_manifest_sha256"],
    }


def required_tags(manifests):
    tags = set()
    for data in manifests.values():
        current = {container["image"] for _, _, spec in workload_documents(data) if spec is not None
                   for container in pod_containers(spec)}
        if not current:
            raise ValueError("A reviewed manifest must include workload images")
        tags.update(current)
    return tags


def read_manifests(directory, pins, mode, fallback):
    names = ["install-ha.yaml"] if mode == "ha" else ["install.yaml"]
    if fallback:
        names.append("install.yaml")
    return {name: read_pinned_manifest(directory / name, pins[name], 4 * 1024 ** 2) for name in names}


def process(directory, pins, lock, mode, fallback, *, source=None):
    manifests = read_manifests(source or directory, pins, mode, fallback)
    tags = required_tags(manifests)
    if not tags <= lock.keys():
        raise ValueError("Image lock is incomplete for the selected manifests")
    selected = {tag: lock[tag] for tag in sorted(tags)}
    if source is None:
        lock_data = read_regular(directory / LOCK_NAME, 64 * 1024)
        if parse_lock(lock_data) != selected:
            raise ValueError("The copied image lock differs from the reviewed image lock")
    else:
        for name, data in manifests.items():
            atomic_write_bytes(directory / name, data, replace=False)
        lock_data = (json.dumps(selected, indent=2) + "\n").encode()
        atomic_write_bytes(directory / LOCK_NAME, lock_data, replace=False)
    files = {name: pins[name] for name in manifests}
    files[LOCK_NAME] = hashlib.sha256(lock_data).hexdigest()
    for tag, digest in selected.items():
        name = digest[7:] + ".oci.tar"
        destination = directory / name
        if source is not None and not destination.exists():
            copy_archive(source / name, destination)
        ref = tag.rsplit(":", 1)[0] + "@" + digest
        files[name] = verify_archive(destination, ref)
    if {path.name for path in directory.iterdir()} != files.keys():
        raise ValueError("Recovery directory contains unexpected files")
    return {"mode": mode, "core_fallback": fallback, "image_count": len(selected),
            "files_sha256": files, "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    create = modes.add_parser("create", help="Copy the reviewed cache into a new directory")
    create.add_argument("--source", type=Path, required=True, help="Directory with cached manifests and <digest>.oci.tar files")
    verify = modes.add_parser("verify", help="Verify the transferred directory against the reviewed source pins")
    for command in (create, verify):
        command.add_argument("--directory", type=Path, required=True, help="Argo CD recovery directory")
        command.add_argument("--reviewed-lock", type=Path, required=True, help="Trusted image lock from outside the recovery directory")
        command.add_argument("--mode", choices=("core", "ha"), required=True)
        command.add_argument("--core-fallback", action="store_true", help="Include the core manifest for HA fallback")
    args = parser.parse_args()
    if args.core_fallback and args.mode != "ha":
        parser.error("--core-fallback requires --mode ha")
    try:
        pins = manifest_pins(Path(__file__).resolve().parents[1])
        lock = parse_lock(read_bounded_bytes(args.reviewed_lock, max_bytes=64 * 1024))
        directory = args.directory
        if args.command == "create":
            with new_recovery_directory(directory) as stage:
                result = process(stage, pins, lock, args.mode, args.core_fallback, source=args.source)
        else:
            if args.reviewed_lock.samefile(directory / LOCK_NAME):
                raise ValueError("Use the reviewed lock from outside the transferred directory")
            result = process(directory, pins, lock, args.mode, args.core_fallback)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError, yaml.YAMLError):
        print("Argo CD recovery directory could not be verified. Check the mode, pinned manifests, "
              "trusted image lock, archive names and architecture.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
