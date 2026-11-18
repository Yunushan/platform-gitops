#!/usr/bin/env python3
"""Render selected GitOps profiles and validate Kubernetes object schemas."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from atomic_file import atomic_write_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from reconcile_image_inventory import manifest_image_references
from rendered_crd_schemas import build_crd_schema_catalog, crd_schema_location, is_native_api_group
from render_deployable_gitops_apps import (
    APPLICATION_NAME_RE,
    APPLICATION_PATH_RE,
    scan_path,
    selected_application_documents,
)
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds, timeout_stream_text


ROOT = Path(__file__).resolve().parents[1]
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
API_VERSION_RE = re.compile(r"(?:[a-z0-9.-]+/)?[a-z0-9]+")
KIND_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
TRUE_VALUES = {"1", "true", "yes", "on"}
RENDER_TIMEOUT_SECONDS = 900
DEFAULT_KUBERNETES_SCHEMA_VERSION = "1.36.2"
NATIVE_SCHEMA_LOCATION = str(
    ROOT / "schemas/kubernetes/{{.NormalizedKubernetesVersion}}/{{.Group}}/{{.ResourceAPIVersion}}/{{.ResourceKind}}.json"
)
SCHEMA_STATUSES = {
    "statusValid": "valid", "statusInvalid": "invalid",
    "statusError": "errors", "statusSkipped": "skipped",
}


def summarize_schema_report(output: str, custom_schemas: dict | None = None) -> dict:
    """Reconcile verbose per-object outcomes without retaining successful object identities."""
    data = loads_strict_json(output)
    if not isinstance(data, dict) or not isinstance(data.get("resources"), list):
        raise ValueError("missing kubeconform resource results")
    totals = {status: 0 for status in SCHEMA_STATUSES.values()}
    skipped = Counter()
    custom_valid = Counter()
    diagnostics = []
    for resource in data["resources"]:
        if not isinstance(resource, dict):
            raise ValueError("invalid kubeconform resource result")
        status = resource.get("status")
        if status == "" and not resource.get("kind") and not resource.get("version"):
            continue  # Kubeconform emits empty YAML documents in verbose mode.
        if not isinstance(status, str) or status not in SCHEMA_STATUSES:
            raise ValueError("unknown kubeconform resource status")
        totals[SCHEMA_STATUSES[status]] += 1
        if status in {"statusSkipped", "statusValid"}:
            version, kind = resource.get("version"), resource.get("kind")
            if not isinstance(version, str) or not API_VERSION_RE.fullmatch(version):
                raise ValueError("missing or malformed API version in resource result")
            if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
                raise ValueError("missing or malformed kind in resource result")
            gvk = f"{version}/{kind}"
            if status == "statusSkipped":
                skipped[gvk] += 1
            elif custom_schemas is not None and not is_native_api_group(version.partition("/")[0]):
                if (version, kind) not in custom_schemas:
                    raise ValueError("a custom resource matched a schema outside its exact group, version and kind")
                custom_valid[gvk] += 1
        else:
            # Validator error messages can quote a rejected Secret value.
            detail = {"status": status}
            for key, pattern in (("version", API_VERSION_RE), ("kind", KIND_RE)):
                value = resource.get(key)
                if isinstance(value, str) and pattern.fullmatch(value):
                    detail[key] = value
            diagnostics.append(detail)
    counts = data.get("summary")
    if not isinstance(counts, dict) or not any(totals.values()) or any(
        type(counts.get(key)) is not int or counts[key] != n for key, n in totals.items()
    ):
        raise ValueError("kubeconform resource outcomes disagree with the summary or are empty")
    return {"resources": diagnostics, "summary": totals, "skippedByGVK": dict(sorted(skipped.items())),
            "validatedCustomByGVK": dict(sorted(custom_valid.items()))}


def missing_native_schemas(skipped: dict[str, int]) -> list[str]:
    return [gvk for gvk in skipped if is_native_api_group(gvk.partition("/")[0])]


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
    if not PROFILE_NAME_RE.fullmatch(profile):
        raise ValueError(f"invalid rendered schema profile name {profile!r}")
    selection = "default" if profile == "base" else profile
    # The local schema build uses only Application names and source paths.
    try:
        documents = selected_application_documents(
            root, selection, "https://example.invalid/platform.git",
        )
    except OSError as exc:
        raise ValueError(f"could not read applications for profile {profile!r}: {exc}") from exc
    if not documents:
        raise ValueError(f"rendered schema profile {profile!r} has no applications")

    sources: list[tuple[str, Path]] = []
    for document in documents:
        name_match = APPLICATION_NAME_RE.search(document)
        path_match = APPLICATION_PATH_RE.search(document)
        if not name_match or not path_match:
            raise ValueError(
                f"profile {profile!r} contains an Application without name/path"
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
    atomic_write_text(
        path,
        f"command_rc={result.returncode}\n"
        f"stdout_bytes={len(stdout_bytes)}\n"
        f"stdout_sha256={hashlib.sha256(stdout_bytes).hexdigest()}\n\n"
        f"stderr:\n{result.stderr}\n",
    )


def validate(args: argparse.Namespace, *, root: Path = ROOT, include_images: bool = False) -> int:
    root = root.resolve()
    if not (root / "gitops").is_dir():
        print(f"rendered schema repository root has no gitops directory: {root}", file=sys.stderr)
        return 2
    profiles = list(dict.fromkeys(profile_names(args.profile)))
    allow_incomplete = False
    if not getattr(args, "require_complete", False):
        allow_incomplete = args.allow_incomplete or env_flag(
            "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE"
        )
    kubernetes_version = (
        args.kubernetes_version
        or os.environ.get(
            "PLATFORM_RENDERED_SCHEMA_KUBERNETES_VERSION",
            DEFAULT_KUBERNETES_SCHEMA_VERSION,
        )
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

    profile_skips: dict[str, Counter[str]] = {profile: Counter() for profile in profiles}
    profile_custom: dict[str, Counter[str]] = {profile: Counter() for profile in profiles}
    summary: dict[str, object] = {
        "kubernetesVersion": kubernetes_version,
        "profiles": profiles,
        "allowIncomplete": allow_incomplete,
        "skippedSchemasByProfile": profile_skips,
        "validatedCustomSchemasByProfile": profile_custom,
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
            pending: list[tuple[str, Path, int, str]] = []
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
                atomic_write_text(manifest, render_result.stdout)
                manifest_bytes = render_result.stdout.encode("utf-8")
                manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
                pending.append((app_name, manifest, len(manifest_bytes), manifest_sha256))

            # A controller can install CRDs consumed by a different application.
            # Build the catalog from this profile's exact rendered artifacts.
            catalog_dir = temporary_root / "crd-schemas" / profile
            try:
                catalog = build_crd_schema_catalog([manifest for _, manifest, _, _ in pending], catalog_dir)
            except (OSError, ValueError) as exc:
                failures.append({"profile": profile, "error": f"could not build rendered CRD schema catalog: {exc}"})
                continue
            require_all_schemas = not allow_incomplete and profile in {"premium-3node", "premium-3node-gitlab-ce"}
            for app_name, manifest, manifest_size, manifest_sha256 in pending:
                safe_name = manifest.stem
                schema_report = reports_dir / f"{safe_name}.kubeconform.json"
                schema_log = reports_dir / f"{safe_name}.kubeconform.stderr.log"
                schema_result = run(
                    [
                        kubeconform,
                        "-strict",
                        "-verbose",
                        "-summary",
                        "-ignore-missing-schemas",
                        "-schema-location", NATIVE_SCHEMA_LOCATION,
                        "-schema-location", crd_schema_location(catalog_dir),
                        "-schema-location", "default",
                        "-kubernetes-version",
                        kubernetes_version,
                        "-output",
                        "json",
                        str(manifest),
                    ],
                    env=command_env,
                    root=root,
                )
                stderr_bytes = schema_result.stderr.encode("utf-8")
                atomic_write_text(schema_log, f"command_rc={schema_result.returncode}\n"
                                  f"stderr_bytes={len(stderr_bytes)}\n"
                                  f"stderr_sha256={hashlib.sha256(stderr_bytes).hexdigest()}\n")
                record: dict[str, object] = {
                    "profile": profile,
                    "application": app_name,
                    "manifestBytes": manifest_size,
                    "manifestSha256": manifest_sha256,
                    "report": str(schema_report.relative_to(root)),
                }
                try:
                    report = summarize_schema_report(schema_result.stdout, catalog)
                except ValueError:
                    # Tool startup/decode failures have no trustworthy per-object results.
                    record["error"] = "kubeconform produced no usable detailed schema report"
                    record.pop("report")
                    record["log"] = str(schema_log.relative_to(root))
                    failures.append(record)
                    continue
                atomic_write_text(schema_report, json.dumps(report, indent=2) + "\n")
                record["schemaCounts"] = report["summary"]
                record["skippedByGVK"] = report["skippedByGVK"]
                record["validatedCustomByGVK"] = report["validatedCustomByGVK"]
                profile_skips[profile].update(report["skippedByGVK"])
                profile_custom[profile].update(report["validatedCustomByGVK"])
                missing = (sorted(report["skippedByGVK"]) if require_all_schemas
                           else missing_native_schemas(report["skippedByGVK"]))
                if missing or schema_result.returncode or report["summary"]["errors"] or report["summary"]["invalid"]:
                    record["error"] = ("missing required Kubernetes schemas: " + ", ".join(missing)
                                       if missing else "kubeconform schema validation failed")
                    failures.append(record)
                    continue
                if include_images:
                    try:
                        record["imageReferences"] = manifest_image_references(manifest)
                    except (OSError, ValueError):
                        failures.append(
                            {
                                "profile": profile,
                                "application": app_name,
                                "error": "could not extract rendered image references",
                            }
                        )
                        continue
                rendered.append(record)
                print(f"schema_validation=passed profile={profile} app={app_name} "
                      f"valid_objects={report['summary']['valid']} skipped_objects={report['summary']['skipped']}")

    atomic_write_text(
        output_dir / "summary.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
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
    parser.add_argument(
        "--profile", action="append",
        help="GitOps profile name, or base for the default applications; repeatable",
    )
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
        help="repository root containing gitops/ and profiles/; defaults to this checkout",
    )
    args = parser.parse_args()
    return validate(
        args, root=args.repo_root or ROOT,
        include_images=env_flag("PLATFORM_RENDERED_SCHEMA_INCLUDE_IMAGE_REFERENCES"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
