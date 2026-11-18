#!/usr/bin/env python3
"""Prepare an offline build context for the non-root Woodpecker publisher image."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from atomic_file import atomic_write_bytes, atomic_write_text
from bounded_file import read_bootstrap_bytes
from verify_image_signature import IMAGE, https_origin


ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = (
    "atomic_file.py", "bounded_file.py", "http_transport.py", "publish_harbor_static_image.py",
    "strict_json.py", "tekton_checkout.py", "woodpecker_static_image.py",
)
PROGRAM_DIR = "/opt/platform/publisher"
ENTRYPOINT = ["python3", "-EsB", PROGRAM_DIR + "/woodpecker_static_image.py", "--plugin"]
UID = "1000:1000"
ENV = ["HOME=/tmp", "TMPDIR=/tmp"]


def program_files(root=ROOT):
    return {name: read_bootstrap_bytes(root / "scripts" / name, 256 * 1024, follow_symlinks=False) for name in PROGRAMS}


def prepare(base_image, output, *, root=ROOT):
    selected = IMAGE.fullmatch(base_image) if isinstance(base_image, str) else None
    if selected is None or not any(marker in selected[1] for marker in (".", ":")):
        raise ValueError("use-a-fully-qualified-digest-pinned-publisher-base")
    https_origin("https://" + selected[1])
    target = Path(output).resolve()
    private_root = (root / "private").resolve()
    if not target.is_relative_to(private_root) or target == private_root or Path(output).is_symlink():
        raise ValueError("publisher-context-must-be-a-new-directory-under-private")
    files = program_files(root)
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in files.items():
        atomic_write_bytes(target / name, data, mode=0o644, replace=False)
    dockerfile = (f"FROM {base_image}\n"
                  f"COPY {' '.join(PROGRAMS)} {PROGRAM_DIR}/\n"
                  f"ENV {' '.join(ENV)}\n"
                  f"USER {UID}\n"
                  f"ENTRYPOINT {json.dumps(ENTRYPOINT)}\n"
                  "CMD []\n")
    atomic_write_text(target / "Dockerfile", dockerfile, mode=0o644, replace=False)
    manifest = {"schema_version": 1, "base_image": base_image, "image_built": False,
                "source_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest()}
    atomic_write_text(target / "context-manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n", replace=False)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.base_image, args.output)
    except (OSError, ValueError):
        print("Woodpecker publisher context preparation failed. Check the base digest and private output path.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
