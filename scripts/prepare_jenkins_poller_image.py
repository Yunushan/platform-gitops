#!/usr/bin/env python3
"""Prepare a minimal offline poller image context; never build or publish it."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

from atomic_file import atomic_write_bytes, atomic_write_text
from bounded_file import read_bootstrap_bytes, read_bounded_text
from render_gerrit_router import IMAGE
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("atomic_file", "bounded_file", "bounded_subprocess", "check_jenkins_poller_state", "gateway_edge_health",
           "gerrit_artifact_contract", "http_transport",
           "kubernetes_management", "publish_harbor_static_image", "render_gerrit_harbor_static_task", "render_gerrit_router",
           "render_harbor_static_task", "render_tekton_forge_bridge", "report_jenkins_gerrit", "resolve_jenkins_gerrit_build",
           "scan_jenkins_gerrit", "scan_jenkins_gerrit_results", "source_bound_static_image", "standalone_python", "strict_json", "tekton_checkout",
           "tekton_forge_bridge", "tekton_forge_reporter", "tekton_gerrit_bridge", "tekton_gerrit_reporter",
           "trigger_jenkins_gerrit", "verify_jenkins_gerrit")


def validate_wheel(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= 500 or sum(item.file_size for item in entries) > 32 * 1024 * 1024:
            raise ValueError("invalid-wheel-size")
        names = [item.filename for item in entries]
        if len(set(names)) != len(names) or any(name.startswith("/") or "\\" in name or ".." in PurePosixPath(name).parts for name in names):
            raise ValueError("invalid-wheel-paths")
        metadata = [name for name in names if re.fullmatch(r"pyyaml-[A-Za-z0-9_.+-]+\.dist-info/METADATA", name, re.IGNORECASE)]
        if len(metadata) != 1: raise ValueError("invalid-wheel-metadata")
        prefix = metadata[0].rsplit("/", 1)[0]
        if prefix + "/WHEEL" not in names or prefix + "/RECORD" not in names:
            raise ValueError("incomplete-wheel-metadata")
        info = archive.read(metadata[0])
        wheel_info = archive.read(prefix + "/WHEEL")
        if len(info) > 256 * 1024 or len(wheel_info) > 65536 or not re.search(rb"(?im)^Name:\s*PyYAML\s*$", info) or b"Wheel-Version:" not in wheel_info:
            raise ValueError("unexpected-wheel-package")


def prepare(plan, wheel, output, *, root=ROOT):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "base_image", "wheel_sha256"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-image-plan")
    if not isinstance(plan["base_image"], str) or not IMAGE.fullmatch(plan["base_image"]): raise ValueError("base-image-digest-required")
    if not isinstance(plan["wheel_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["wheel_sha256"]): raise ValueError("wheel-digest-required")
    wheel = Path(wheel)
    if not re.fullmatch(r"pyyaml-[A-Za-z0-9_.+-]+\.whl", wheel.name, flags=re.IGNORECASE): raise ValueError("pyyaml-wheel-required")
    wheel_bytes = read_bootstrap_bytes(wheel, 8 * 1024 * 1024, follow_symlinks=False)
    if hashlib.sha256(wheel_bytes).hexdigest() != plan["wheel_sha256"]: raise ValueError("wheel-digest-mismatch")
    validate_wheel(wheel_bytes)
    target = Path(output)
    if not target.is_absolute() or target.is_symlink() or not target.resolve().is_relative_to((root / "private").resolve()):
        raise ValueError("private-output-required")
    scripts = {name + ".py": read_bootstrap_bytes(root / "scripts" / (name + ".py"), 1024 * 1024, follow_symlinks=False) for name in MODULES}
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    (target / "scripts").mkdir(mode=0o755)
    (target / "scripts").chmod(0o755)
    for name, data in scripts.items():
        atomic_write_bytes(target / "scripts" / name, data, mode=0o644)
    atomic_write_bytes(target / wheel.name, wheel_bytes)
    dockerfile = (f"FROM {plan['base_image']}\nCOPY {wheel.name} /tmp/{wheel.name}\n"
                  f"RUN echo '{plan['wheel_sha256']}  /tmp/{wheel.name}' | sha256sum -c - && "
                  f"/usr/local/bin/python3 -m pip install --no-cache-dir --no-index --no-deps /tmp/{wheel.name} && rm /tmp/{wheel.name}\n"
                  "COPY scripts/ /opt/platform/scripts/\nUSER 1000:1000\nWORKDIR /opt/platform\n")
    atomic_write_text(target / "Dockerfile", dockerfile)
    manifest = {"schema_version": 1, "base_image": plan["base_image"], "wheel_sha256": plan["wheel_sha256"],
                "scripts_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(scripts.items())},
                "image_built": False, "image_published": False}
    atomic_write_text(target / "context-manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True); args = parser.parse_args()
    try: result = prepare(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)), args.wheel, args.output)
    except Exception:
        print("Poller build context preparation failed; inspect private inputs.", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
