#!/usr/bin/env python3
"""Assemble or verify a Flux offline bootstrap cache against a reviewed plan."""

import argparse
import json
from pathlib import Path
import sys
import tarfile

from atomic_file import atomic_write_bytes
from recovery_artifacts import copy_archive, new_recovery_directory, read_pinned_manifest, read_regular
from render_flux import export_objects, validate
from strict_json import loads_strict_json
from verify_oci_archive import verify_archive


EXPORT = "install.yaml"
ARTIFACT_SERVER = "nginx-unprivileged.oci.tar"


def process(directory, plan, *, source=None):
    export = read_pinned_manifest((source or directory) / EXPORT, plan["bundle_sha256"], 16 * 1024 ** 2)
    export_objects(plan, export)
    if source is not None:
        atomic_write_bytes(directory / EXPORT, export, replace=False)
    images = {name + ".oci.tar": ref for name, ref in plan["images"].items()}
    images[ARTIFACT_SERVER] = plan["artifact_server_image"]
    files = {EXPORT: plan["bundle_sha256"]}
    for name, image in sorted(images.items()):
        path = directory / name
        if source is not None:
            copy_archive(source / name, path)
        files[name] = verify_archive(path, image)
    if {path.name for path in directory.iterdir()} != files.keys():
        raise ValueError("Recovery directory contains unexpected files")
    return {
        "flux_version": plan["flux_version"],
        "image_platform": "linux/amd64",
        "image_count": len(images),
        "files_sha256": files,
        "verified": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Copy artifacts into a new verified recovery directory")
    create.add_argument("--source", type=Path, required=True, help="Source cache with install.yaml and named OCI archives")
    verify = commands.add_parser("verify", help="Verify the transferred recovery directory")
    for command in (create, verify):
        command.add_argument("--directory", type=Path, required=True)
        command.add_argument("--reviewed-plan", type=Path, required=True, help="Trusted Flux recovery plan outside the cache")
    args = parser.parse_args()
    try:
        plan = validate(loads_strict_json(read_regular(args.reviewed_plan, 256 * 1024)))
        if plan.get("offline_images") is not True or "commit" not in plan["sync"]:
            raise ValueError("Recovery plan requires offline_images and a commit pin")
        if args.command == "create":
            with new_recovery_directory(args.directory) as stage:
                result = process(stage, plan, source=args.source)
        else:
            result = process(args.directory, plan)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError):
        print("Flux cache verification failed. Check the reviewed plan, export, named Linux amd64 OCI archives, "
              "and recovery directory.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
