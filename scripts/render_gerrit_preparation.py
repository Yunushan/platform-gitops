#!/usr/bin/env python3
"""Render explicit, guarded Gerrit preparation Jobs or one-shot Compose.

Rendering does not run init. Applying the generated workload DOES initialize
new sites, and therefore requires a separate operator decision after review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, write_bundle
import render_gerrit_sites as sites
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("initialize_gerrit_sites.py", "render_gerrit_sites.py", "render_gerrit_router.py", "gateway_edge_health.py",
           "gerrit_storage.py", "gerrit_artifacts.py", "bounded_file.py", "bounded_subprocess.py",
           "http_transport.py", "standalone_python.py", "strict_json.py")
SECRET_FILES = ("gerrit.config", "high-availability.config", "secure.config", "keystore",
                "ssh_host_ed25519_key", "ssh_host_ed25519_key.pub")
CONTAINER_RUNTIMES = ("rke2", "k3s", "k8s", "docker")


def secret_files(plan):
    return (*SECRET_FILES, "healthcheck.config") if plan["schema_version"] == 2 else SECRET_FILES


class PreparationError(ValueError):
    pass


def validate_layout(plan: dict) -> None:
    sites.validate(plan)
    if plan["shared_host_path"] != "/shared":
        raise PreparationError("Container preparation requires the explicit /shared mount layout")
    for site in plan["sites"]:
        if site["host_path"] != f"/volumes/{site['name']}/site" or site.get("claim_subpath") != "site":
            raise PreparationError("Container preparation requires a new site subdirectory per mounted claim")


def code_bundle(plan: dict) -> dict[str, str]:
    files = {name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in MODULES}
    files["site-plan.json"] = json.dumps(plan, indent=2) + "\n"
    if sum(len(value.encode()) for value in files.values()) > 400 * 1024:
        raise PreparationError("Preparation code bundle exceeds its Kubernetes payload limit")
    return files


def initializer_command() -> list[str]:
    return ["python3", "/opt/platform/scripts/initialize_gerrit_sites.py",
            "--plan", "/opt/platform/scripts/site-plan.json", "--inputs", "/bootstrap",
            "--apply", "--confirm", "INITIALIZE_NEW_GERRIT_SITES",
            "--output-dir", "/opt/platform/private/initialization-draft"]


def stage_script(plan: dict) -> str:
    lines = ["#!/bin/sh", "set -eu", "umask 077"]
    for site in plan["sites"]:
        name = site["name"]
        for directory in ("bin", "plugins", "lib", "etc"):
            lines.append(f'mkdir -p /bootstrap/{name}/{directory}')
        for relative in sites.ARTIFACTS:
            lines.append(f'cp /release/{relative} /bootstrap/{name}/{relative}')
        for filename in secret_files(plan):
            lines.append(f'cp /private-inputs/{name}/{filename} /bootstrap/{name}/etc/{filename}')
    # Literal arguments only; all plan-derived site names are DNS labels.
    lines.append("exec " + " ".join(initializer_command()))
    return "\n".join(lines) + "\n"


def kubernetes_bundle(plan: dict, image: str, artifact_claim: str, report_claim: str) -> dict[str, str]:
    if not sites.label(artifact_claim) or not sites.label(report_claim):
        raise PreparationError("Artifact and report claim names must be DNS labels")
    claims = [artifact_claim, report_claim, plan["shared_claim"], *(s["claim"] for s in plan["sites"])]
    if len(set(claims)) != len(claims):
        raise PreparationError("Artifacts, reports, shared Git and local site claims must be distinct")
    files = code_bundle(plan)
    files["stage.sh"] = stage_script(plan)
    revision = hashlib.sha256(json.dumps({"files": files, "image": image, "claims": claims}, sort_keys=True).encode()).hexdigest()[:12]
    name = "gerrit-prepare-" + revision
    meta = {"name": name, "namespace": plan["namespace"], "labels": {"app.kubernetes.io/name": "gerrit-preparation"}}
    mounts = [{"name": "code", "mountPath": "/opt/platform/scripts", "readOnly": True},
              {"name": "release", "mountPath": "/release", "readOnly": True},
              {"name": "report", "mountPath": "/opt/platform/private"},
              {"name": "shared", "mountPath": "/shared"},
              {"name": "bootstrap", "mountPath": "/bootstrap"}, {"name": "tmp", "mountPath": "/tmp"}]
    volumes = [{"name": "code", "configMap": {"name": name}},
               {"name": "release", "persistentVolumeClaim": {"claimName": artifact_claim, "readOnly": True}},
               {"name": "report", "persistentVolumeClaim": {"claimName": report_claim}},
               {"name": "shared", "persistentVolumeClaim": {"claimName": plan["shared_claim"]}},
               {"name": "bootstrap", "emptyDir": {"sizeLimit": "4Gi"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    for index, site in enumerate(plan["sites"]):
        mounts += [{"name": f"site-{index}", "mountPath": f"/volumes/{site['name']}"},
                   {"name": f"private-{index}", "mountPath": f"/private-inputs/{site['name']}", "readOnly": True}]
        volumes += [{"name": f"site-{index}", "persistentVolumeClaim": {"claimName": site["claim"]}},
                    {"name": f"private-{index}", "secret": {"secretName": site["name"] + "-bootstrap", "defaultMode": 0o440,
                                                            "items": [{"key": f, "path": f} for f in secret_files(plan)]}}]
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "immutable": True, "data": files}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta, "spec": {
        "backoffLimit": 0, "activeDeadlineSeconds": 900,
        "template": {"metadata": {"labels": meta["labels"]}, "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "prepare", "image": image,
                "command": ["/bin/sh", "/opt/platform/scripts/stage.sh"],
                "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "4Gi"}},
                "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}}, "volumeMounts": mounts}],
            "volumes": volumes,
        }}}}
    # No automatic TTL cleanup: retain failed/completed Job and persistent draft
    # for independent review. Never create a Secret, PVC, service or daemon here.
    return {"preparation.yaml": yaml.safe_dump_all([configmap, job], sort_keys=False)}


def docker_bundle(plan: dict, image: str, bootstrap_path: str, report_path: str) -> dict[str, str]:
    for path in (bootstrap_path, report_path):
        if not isinstance(path, str) or not sites.LINUX_PATH.fullmatch(path):
            raise PreparationError("Docker preparation needs explicit absolute Linux bootstrap/report paths")
    paths = [bootstrap_path, report_path, "/shared", *(f"/volumes/{site['name']}" for site in plan["sites"])]
    for index, one in enumerate(paths):
        if any(one == two or one.startswith(two + "/") or two.startswith(one + "/") for two in paths[index + 1:]):
            raise PreparationError("Bootstrap, report, site and shared mounts must not overlap")
    files = code_bundle(plan)
    mounts = [{"type": "bind", "source": source, "target": target, "read_only": read_only,
               "bind": {"create_host_path": False}} for source, target, read_only in (
        (".", "/opt/platform/scripts", True), (bootstrap_path, "/bootstrap", True),
        (report_path, "/opt/platform/private", False), ("/shared", "/shared", False),
        *((f"/volumes/{site['name']}", f"/volumes/{site['name']}", False) for site in plan["sites"]))]
    files["preparation-compose.yaml"] = yaml.safe_dump({"services": {"gerrit-prepare": {
        "image": image, "user": "1000:1000", "restart": "no", "read_only": True,
        "entrypoint": initializer_command(), "command": [], "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "volumes": mounts, "tmpfs": ["/tmp:rw,nosuid,nodev,size=1g,mode=1777"],
        "mem_limit": "4g", "cpus": 4.0,
    }}}, sort_keys=False)
    return files


def render(plan: dict, runtime: str, image: str, *, artifact_claim: str = "", report_claim: str = "",
           bootstrap_path: str = "", report_path: str = "") -> dict[str, str]:
    validate_layout(plan)
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise PreparationError("Pin the reviewed preparation toolchain image by digest")
    if runtime in {"rke2", "k3s", "k8s"}:
        if bootstrap_path or report_path:
            raise PreparationError("Host-path arguments are not accepted for Kubernetes preparation")
        files = kubernetes_bundle(plan, image, artifact_claim, report_claim)
    elif runtime == "docker":
        if artifact_claim or report_claim:
            raise PreparationError("PVC arguments are not accepted for Docker preparation")
        files = docker_bundle(plan, image, bootstrap_path, report_path)
    else:
        raise PreparationError("Use the native initializer directly for raw deployments")
    files["render-status.json"] = json.dumps({"component": "gerrit-preparation", "runtime": runtime,
        "rendered": True, "executed": False, "ha_verified": False,
        "warning": "Applying the workload explicitly initializes new sites; review before running.",
        "required_tools": ["python3", "PyYAML", "java", "git", "POSIX shell", "cp"],
    }, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=CONTAINER_RUNTIMES, required=True)
    parser.add_argument("--preparation-image", required=True)
    parser.add_argument("--artifact-claim", default="")
    parser.add_argument("--report-claim", default="")
    parser.add_argument("--bootstrap-host-path", default="")
    parser.add_argument("--report-host-path", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        files = render(plan, args.runtime, args.preparation_image, artifact_claim=args.artifact_claim,
                       report_claim=args.report_claim, bootstrap_path=args.bootstrap_host_path, report_path=args.report_host_path)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Gerrit preparation render failed; inspect private layout, image pin, input references and new output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "executed": False, "ha_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
