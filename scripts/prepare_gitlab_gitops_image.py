#!/usr/bin/env python3
"""Prepare an offline, digest-pinned scheduler image context without secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from atomic_file import atomic_write_bytes, atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
from render_gerrit_router import IMAGE
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = ("atomic_file", "bounded_file", "bounded_subprocess", "ci_gitops_handoff", "gateway_edge_health", "gerrit_artifact_contract",
            "gitlab_gitops_handoff", "gitlab_gitops_schedule", "http_transport", "kubernetes_management",
            "publish_harbor_static_image", "render_gerrit_harbor_static_task", "render_gerrit_router",
            "render_harbor_static_task", "render_tekton_forge_bridge", "source_bound_static_image", "standalone_python", "strict_json",
            "subprocess_timeout", "tekton_checkout", "tekton_forge_bridge", "tekton_forge_reporter", "verify_gitlab_pipeline")


def prepare(plan, kubectl_path, output, *, root=ROOT):
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "base_image", "kubectl_sha256", "architecture"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-image-plan")
    if not isinstance(plan["base_image"], str) or not IMAGE.fullmatch(plan["base_image"]):
        raise ValueError("digest-pinned-base-required")
    if not isinstance(plan["kubectl_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["kubectl_sha256"]):
        raise ValueError("kubectl-digest-required")
    if plan["architecture"] not in {"amd64", "arm64"}:
        raise ValueError("unsupported-architecture")
    binary = read_bounded_bytes(kubectl_path, max_bytes=256 * 1024 * 1024)
    machine = {"amd64": b"\x3e\x00", "arm64": b"\xb7\x00"}[plan["architecture"]]
    if (len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01" or binary[18:20] != machine
            or hashlib.sha256(binary).hexdigest() != plan["kubectl_sha256"]):
        raise ValueError("kubectl-artifact-mismatch")
    files = {}
    for name in PROGRAMS:
        path = root / "scripts" / (name + ".py")
        if path.is_symlink() or not path.is_file(): raise ValueError("program-file-missing")
        files[name + ".py"] = read_bounded_bytes(path, max_bytes=4 * 1024 * 1024)
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("fresh-absolute-output-required")
    output.mkdir(mode=0o700)
    (output / "scripts").mkdir(mode=0o700)
    for name, data in files.items():
        atomic_write_bytes(output / "scripts" / name, data)
    atomic_write_bytes(output / "kubectl", binary)
    dockerfile = (f"FROM --platform=linux/{plan['architecture']} {plan['base_image']}\n"
        "COPY scripts/ /opt/platform/platform-gitops/scripts/\n"
        "COPY --chmod=0755 kubectl /usr/local/bin/kubectl\n"
        "WORKDIR /opt/platform/platform-gitops\n"
        "USER 0:0\n"
        "RUN chmod 0755 scripts && chmod 0644 scripts/*.py\n"
        "USER 65532:65532\n"
        "RUN /usr/bin/python3 -B -c 'import yaml' && git --version && kubectl version --client && "
        "/usr/bin/python3 -B scripts/gitlab_gitops_schedule.py --help\n")
    atomic_write_text(output / "Dockerfile", dockerfile)
    manifest = {"schema_version": 1, "base_image": plan["base_image"], "architecture": plan["architecture"],
                "kubectl_sha256": plan["kubectl_sha256"], "program_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(), "image_built": False}
    atomic_write_text(output / "context-manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--kubectl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)), args.kubectl, args.output)
    except Exception:
        print("Image context preparation failed; review the artifact inputs.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
