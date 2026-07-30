#!/usr/bin/env python3
"""Render selected GitOps profiles and validate Kubernetes object schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from bounded_subprocess import BoundedSubprocessError, run_bounded
from render_deployable_gitops_apps import (
    APPLICATION_NAME_RE,
    APPLICATION_PATH_RE,
    application_documents_from_file,
    scan_path,
)
from subprocess_timeout import bounded_timeout_seconds, timeout_stream_text


ROOT = Path(__file__).resolve().parents[1]
PROFILE_APPLICATION_FILES = {
    "base": Path("gitops/clusters/rke2-main/platform-apps.yaml"),
    "premium-3node": Path("gitops/clusters/rke2-main/premium-3node/platform-apps.yaml"),
}
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
TRUE_VALUES = {"1", "true", "yes", "on"}
RENDER_TIMEOUT_SECONDS = 900


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def profile_names(cli_profiles: list[str] | None) -> list[str]:
    if cli_profiles:
        return cli_profiles
    configured = os.environ.get("PLATFORM_RENDERED_SCHEMA_PROFILES")
    if configured:
        return [part.strip() for part in configured.split(",") if part.strip()]
    return [os.environ.get("PLATFORM_PROFILE", "premium-3node").strip() or "premium-3node"]


def output_path(value: str, root: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    rendered_root = (root / "rendered").resolve()
    if path != rendered_root and rendered_root not in path.parents:
        raise ValueError("rendered schema output must remain under rendered/")
    return path


def required_tool(env_name: str, default: str) -> str:
    command = os.environ.get(env_name, default).strip()
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"required command is unavailable: {command} ({env_name})")
    return resolved


def application_sources(profile: str, root: Path) -> list[tuple[str, Path]]:
    applications_relative = PROFILE_APPLICATION_FILES.get(profile)
    if applications_relative is None:
        supported = ", ".join(sorted(PROFILE_APPLICATION_FILES))
        raise ValueError(f"unsupported rendered schema profile {profile!r}; choose {supported}")
    applications_file = root / applications_relative
    if not applications_file.is_file():
        raise ValueError(
            f"profile applications file is missing: {applications_file.relative_to(root)}"
        )

    sources: list[tuple[str, Path]] = []
    for document in application_documents_from_file(applications_file):
        name_match = APPLICATION_NAME_RE.search(document)
        path_match = APPLICATION_PATH_RE.search(document)
        if not name_match or not path_match:
            raise ValueError(
                f"{applications_file.relative_to(root)} contains an Application without name/path"
            )
        sources.append((name_match.group("name"), root / path_match.group("path")))
    return sources


def run(
    command: list[str], *, env: dict[str, str], root: Path
) -> subprocess.CompletedProcess[str]:
    try:
        timeout = bounded_timeout_seconds(
            RENDER_TIMEOUT_SECONDS,
            "PLATFORM_RENDER_COMMAND_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        return subprocess.CompletedProcess(command, 2, "", str(exc))
    try:
        return run_bounded(
            command,
            cwd=root,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = timeout_stream_text(exc.stdout)
        stderr = timeout_stream_text(exc.stderr)
        detail = f"command timed out after {timeout:g} seconds"
        stderr = f"{stderr.rstrip()}\n{detail}" if stderr else detail
        return subprocess.CompletedProcess(command, 124, stdout, stderr)
    except BoundedSubprocessError as exc:
        stdout = getattr(exc, "stdout", "")
        stderr = getattr(exc, "stderr", "")
        detail = f"command output rejected: {exc}"
        stderr = f"{stderr.rstrip()}\n{detail}" if stderr else detail
        return subprocess.CompletedProcess(command, 125, stdout, stderr)
    except ValueError as exc:
        return subprocess.CompletedProcess(command, 2, "", str(exc))


def write_log(path: Path, result: subprocess.CompletedProcess[str]) -> None:
    stdout_bytes = result.stdout.encode("utf-8")
    path.write_text(
        f"command_rc={result.returncode}\n"
        f"stdout_bytes={len(stdout_bytes)}\n"
        f"stdout_sha256={hashlib.sha256(stdout_bytes).hexdigest()}\n\n"
        f"stderr:\n{result.stderr}\n",
        encoding="utf-8",
    )


def validate(args: argparse.Namespace, *, root: Path = ROOT) -> int:
    root = root.resolve()
    if not (root / "gitops").is_dir():
        print(f"rendered schema repository root has no gitops directory: {root}", file=sys.stderr)
        return 2
    profiles = profile_names(args.profile)
    allow_incomplete = False
    if not getattr(args, "require_complete", False):
        allow_incomplete = args.allow_incomplete or env_flag(
            "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE"
        )
    kubernetes_version = (
        args.kubernetes_version
        or os.environ.get("PLATFORM_RENDERED_SCHEMA_KUBERNETES_VERSION", "1.35.0")
    ).strip()
    output_dir = output_path(
        args.output_dir
        or os.environ.get(
            "PLATFORM_RENDERED_SCHEMA_OUTPUT_DIR", "rendered/schema-validation"
        ),
        root,
    )

    try:
        kustomize = required_tool("KUSTOMIZE_BIN", "kustomize")
        kubeconform = required_tool("KUBECONFORM_BIN", "kubeconform")
        helm = required_tool("HELM_BIN", "helm")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if output_dir.exists():
        shutil.rmtree(output_dir)
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True)

    summary: dict[str, object] = {
        "kubernetesVersion": kubernetes_version,
        "profiles": profiles,
        "allowIncomplete": allow_incomplete,
        "rendered": [],
        "skipped": [],
        "failures": [],
    }
    rendered = summary["rendered"]
    skipped = summary["skipped"]
    failures = summary["failures"]
    assert isinstance(rendered, list)
    assert isinstance(skipped, list)
    assert isinstance(failures, list)

    with tempfile.TemporaryDirectory(prefix="platform-rendered-schema-") as temporary:
        temporary_root = Path(temporary)
        work_root = temporary_root / "repo"
        shutil.copytree(root / "gitops", work_root / "gitops")
        manifests_dir = temporary_root / "manifests"
        manifests_dir.mkdir()
        tool_home = temporary_root / "tools"
        tool_home.mkdir()
        command_env = os.environ.copy()
        command_env.update(
            {
                "HELM_CACHE_HOME": str(tool_home / "helm-cache"),
                "HELM_CONFIG_HOME": str(tool_home / "helm-config"),
                "HELM_DATA_HOME": str(tool_home / "helm-data"),
            }
        )

        for profile in profiles:
            try:
                sources = application_sources(profile, root)
            except ValueError as exc:
                failures.append({"profile": profile, "error": str(exc)})
                continue

            for app_name, source in sources:
                findings = scan_path(source, root)
                if findings:
                    record = {
                        "profile": profile,
                        "application": app_name,
                        "reason": "unresolved-placeholders",
                        "findings": findings[:20],
                    }
                    if allow_incomplete:
                        skipped.append(record)
                        print(
                            f"schema_validation=skipped profile={profile} app={app_name} "
                            f"reason=unresolved-placeholders count={len(findings)}"
                        )
                        continue
                    failures.append(record)
                    continue

                try:
                    relative_source = source.resolve().relative_to(root)
                except ValueError:
                    failures.append(
                        {
                            "profile": profile,
                            "application": app_name,
                            "error": "application source escapes repository root",
                        }
                    )
                    continue
                copied_source = work_root / relative_source
                safe_name = SAFE_NAME_RE.sub("-", f"{profile}-{app_name}").strip("-")
                manifest = manifests_dir / f"{safe_name}.yaml"
                render_log = reports_dir / f"{safe_name}.render.log"
                schema_report = reports_dir / f"{safe_name}.kubeconform.json"

                render_result = run(
                    [
                        kustomize,
                        "build",
                        "--enable-helm",
                        "--helm-command",
                        helm,
                        "--helm-kube-version",
                        kubernetes_version,
                        "--load-restrictor",
                        "LoadRestrictionsNone",
                        str(copied_source),
                    ],
                    env=command_env,
                    root=root,
                )
                write_log(render_log, render_result)
                if render_result.returncode != 0:
                    failures.append(
                        {
                            "profile": profile,
                            "application": app_name,
                            "error": "kustomize build failed",
                            "log": str(render_log.relative_to(root)),
                        }
                    )
                    continue
                if "apiVersion:" not in render_result.stdout or "kind:" not in render_result.stdout:
                    failures.append(
                        {
                            "profile": profile,
                            "application": app_name,
                            "error": "kustomize produced no Kubernetes objects",
                        }
                    )
                    continue
                manifest.write_text(render_result.stdout, encoding="utf-8")
                manifest_bytes = render_result.stdout.encode("utf-8")
                manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

                schema_result = run(
                    [
                        kubeconform,
                        "-strict",
                        "-summary",
                        "-ignore-missing-schemas",
                        "-kubernetes-version",
                        kubernetes_version,
                        "-output",
                        "json",
                        str(manifest),
                    ],
                    env=command_env,
                    root=root,
                )
                schema_report.write_text(schema_result.stdout, encoding="utf-8")
                (reports_dir / f"{safe_name}.kubeconform.stderr.log").write_text(
                    schema_result.stderr, encoding="utf-8"
                )
                if schema_result.returncode != 0:
                    failures.append(
                        {
                            "profile": profile,
                            "application": app_name,
                            "error": "kubeconform schema validation failed",
                            "report": str(schema_report.relative_to(root)),
                        }
                    )
                    continue

                rendered.append(
                    {
                        "profile": profile,
                        "application": app_name,
                        "manifestBytes": len(manifest_bytes),
                        "manifestSha256": manifest_sha256,
                        "report": str(schema_report.relative_to(root)),
                    }
                )
                print(f"schema_validation=passed profile={profile} app={app_name}")

    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"rendered_schema_profiles={len(profiles)} rendered_apps={len(rendered)} "
        f"skipped_apps={len(skipped)} failures={len(failures)}"
    )
    if failures:
        for failure in failures:
            print(f"rendered_schema_failure={json.dumps(failure, sort_keys=True)}", file=sys.stderr)
        return 1
    if not rendered:
        print("rendered schema verification produced no validated applications", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", help="base or premium-3node; repeatable")
    completeness = parser.add_mutually_exclusive_group()
    completeness.add_argument("--allow-incomplete", action="store_true")
    completeness.add_argument(
        "--require-complete",
        action="store_true",
        help="reject every unresolved application even when the environment allows skips",
    )
    parser.add_argument("--kubernetes-version")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="repository root containing gitops/; defaults to this checkout",
    )
    args = parser.parse_args()
    return validate(args, root=args.repo_root or ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
