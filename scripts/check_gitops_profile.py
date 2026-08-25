#!/usr/bin/env python3
"""Check that a GitOps profile is complete enough for production registration."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from bounded_file import read_bounded_text


PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
APPLICATION_PATH_RE = re.compile(
    r"""(?m)^\s+path:\s*(?P<quote>['"]?)(?P<path>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)
APPLICATION_NAME_RE = re.compile(
    r"""(?m)^\s{2}name:\s*(?P<quote>['"]?)(?P<name>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)
VENDORED_PATH_PARTS = {"charts", "crds"}
EXAMPLE_SUFFIXES = (".example.yaml", ".example.yml")
KUSTOMIZATION_NAMES = {"kustomization.yaml", "kustomization.yml", "Kustomization"}
LOCAL_PATH_SECTIONS = {"resources", "components", "patchesStrategicMerge"}
TOP_LEVEL_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z][A-Za-z0-9_-]*):\s*(?:#.*)?$")
LIST_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s*(?P<value>[^#]+?)(?:\s+#.*)?$")
VALUES_FILE_RE = re.compile(r"(?m)^\s+valuesFile:\s*(?P<value>[^#\s]+)")
PATCH_ITEM_PATH_RE = re.compile(r"^\s*-\s+path:\s*(?P<value>[^#\s]+)")
REQUIRED_PREMIUM_SUPPORT_FILES = (
    "gitops/clusters/rke2-main/premium-3node/apps/cert-manager/internal-ca.yaml",
    "gitops/clusters/rke2-main/premium-3node/apps/trust-manager/bundles.yaml",
    "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/server-certificate.yaml",
    "gitops/clusters/rke2-main/premium-3node/apps/harbor/ca-bundle-configmap-patch.yaml",
    "gitops/clusters/rke2-main/premium-3node/apps/harbor/ca-bundle-configmap-statefulset-patch.yaml",
)
PROFILE_APP_FILES = {
    "default": "gitops/clusters/rke2-main/platform-apps.yaml",
    "premium-3node": "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml",
}


def fail(message: str) -> int:
    print(f"GitOps profile check failed: {message}", file=sys.stderr)
    return 1


def unresolved_in_text(text: str, allow_repo_url: bool = False) -> list[str]:
    placeholders = PLACEHOLDER_RE.findall(text)
    if allow_repo_url:
        placeholders = [item for item in placeholders if item != "<THIS_REPO_URL>"]
    return placeholders


def display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def scan_file(path: Path, repo_root: Path, allow_repo_url: bool = False) -> list[str]:
    findings: list[str] = []
    try:
        lines = read_bounded_text(path, encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return findings

    for line_number, line in enumerate(lines, start=1):
        placeholders = unresolved_in_text(line, allow_repo_url=allow_repo_url)
        if placeholders:
            findings.append(f"{display_path(path, repo_root)}:{line_number}: {line.strip()}")
    return findings


def scan_path(path: Path, repo_root: Path, allow_repo_url: bool = False) -> list[str]:
    if not path.exists():
        return [f"{display_path(path, repo_root)}: missing path"]

    if path.is_file():
        return scan_file(path, repo_root, allow_repo_url=allow_repo_url)

    findings: list[str] = []
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel_parts = set(file_path.relative_to(path).parts)
        if rel_parts & VENDORED_PATH_PARTS:
            continue
        if file_path.suffix not in {".yaml", ".yml"}:
            continue
        if file_path.name.endswith(EXAMPLE_SUFFIXES):
            continue
        findings.extend(scan_file(file_path, repo_root, allow_repo_url=allow_repo_url))
    return findings


def parse_simple_profile(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    current_list = ""

    for raw_line in read_bounded_text(path, encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        scalar_match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", stripped)
        if scalar_match:
            key, value = scalar_match.groups()
            current_list = ""
            if value:
                scalars[key] = value
            else:
                lists.setdefault(key, [])
                current_list = key
            continue
        list_match = re.match(r"^-\s+(.+?)\s*$", stripped)
        if list_match and current_list:
            lists[current_list].append(list_match.group(1))

    return scalars, lists


def append_unique(values: list[str], additions: list[str]) -> list[str]:
    result = list(values)
    for item in additions:
        if item not in result:
            result.append(item)
    return result


def application_source_paths(applications_file: Path, repo_root: Path) -> list[Path]:
    text = read_bounded_text(applications_file, encoding="utf-8")
    paths: list[Path] = []
    for match in APPLICATION_PATH_RE.finditer(text):
        paths.append(repo_root / match.group("path"))
    return paths


def optional_application_names(repo_root: Path, profile: str, seen: set[str] | None = None) -> set[str]:
    """Return optional applications unless their feature is explicitly enabled."""
    mode = os.environ.get("STEP_CA_MODE", "disabled").strip().lower()
    if mode not in {"", "disabled", "skip", "false", "none", "bootstrap"}:
        raise ValueError("STEP_CA_MODE currently supports disabled or bootstrap")
    if mode == "bootstrap":
        return set()

    seen = seen or set()
    if profile in seen:
        raise ValueError(f"profile {profile!r} has an inheritance cycle")
    seen.add(profile)

    profile_file = repo_root / "profiles" / f"{profile}.yaml"
    if not profile_file.exists():
        return set()

    scalars, lists = parse_simple_profile(profile_file)
    optional = {
        value.strip()
        for value in [scalars.get("internal_ca_optional", ""), *lists.get("optional_applications", [])]
        if value.strip()
    }
    inherited = scalars.get("inherits", "")
    if inherited:
        optional.update(optional_application_names(repo_root, inherited, seen))
    return optional


def scan_applications_file(
    path: Path,
    repo_root: Path,
    optional_apps: set[str],
) -> list[str]:
    """Scan selected Application documents while excluding disabled optional apps."""
    text = read_bounded_text(path, encoding="utf-8")
    findings: list[str] = []
    documents = [document for document in re.split(r"(?m)^---\s*$", text) if document.strip()]
    for document in documents:
        name_match = APPLICATION_NAME_RE.search(document)
        if name_match and name_match.group("name") in optional_apps:
            continue
        for line_number, line in enumerate(document.splitlines(), start=1):
            placeholders = unresolved_in_text(line, allow_repo_url=True)
            if placeholders:
                findings.append(f"{display_path(path, repo_root)}:{line_number}: {line.strip()}")
    return findings


def resolve_profile_entries(repo_root: Path, profile: str, seen: set[str] | None = None) -> tuple[list[str], list[str]]:
    if profile in PROFILE_APP_FILES:
        app_file = repo_root / PROFILE_APP_FILES[profile]
        return [str(path.relative_to(repo_root)).replace("\\", "/") for path in application_source_paths(app_file, repo_root)], []

    seen = seen or set()
    if profile in seen:
        raise ValueError(f"profile {profile!r} has an inheritance cycle")
    seen.add(profile)

    profile_file = repo_root / "profiles" / f"{profile}.yaml"
    if not profile_file.exists():
        raise ValueError(f"unsupported profile {profile!r}")

    scalars, lists = parse_simple_profile(profile_file)
    includes: list[str] = []
    removes: list[str] = []
    inherited = scalars.get("inherits", "")
    if inherited:
        includes, removes = resolve_profile_entries(repo_root, inherited, seen)

    local_includes = lists.get("includes", [])
    local_removes = lists.get("remove", [])
    missing_entries = [
        entry
        for entry in local_includes + local_removes
        if not (repo_root / entry).exists()
    ]
    if missing_entries:
        raise ValueError(
            f"profile {profile!r} references missing path(s): {', '.join(sorted(missing_entries))}"
        )

    includes = append_unique(includes, local_includes)
    removes = append_unique(removes, local_removes)
    includes = [entry for entry in includes if entry not in set(removes)]
    return includes, removes


def profile_dependency_files(repo_root: Path, profile: str, seen: set[str] | None = None) -> list[Path]:
    if profile in PROFILE_APP_FILES:
        return [repo_root / PROFILE_APP_FILES[profile]]

    seen = seen or set()
    if profile in seen:
        raise ValueError(f"profile {profile!r} has an inheritance cycle")
    seen.add(profile)

    profile_file = repo_root / "profiles" / f"{profile}.yaml"
    if not profile_file.exists():
        raise ValueError(f"unsupported profile {profile!r}")

    scalars, _ = parse_simple_profile(profile_file)
    inherited = scalars.get("inherits", "")
    files = profile_dependency_files(repo_root, inherited, seen) if inherited else []
    files.append(profile_file)
    return files


def is_application_source(path: Path) -> bool:
    kustomization = path / "kustomization.yaml"
    if not kustomization.exists():
        return False
    text = read_bounded_text(kustomization, encoding="utf-8")
    return "helmCharts:" in text


def profile_source_paths(repo_root: Path, profile: str) -> list[Path]:
    includes, _ = resolve_profile_entries(repo_root, profile)
    return [repo_root / entry for entry in includes if is_application_source(repo_root / entry)]


def strip_scalar(value: str) -> str:
    return value.strip().strip("'\"")


def is_static_local_ref(value: str) -> bool:
    return bool(value) and "{{" not in value and "}}" not in value and "://" not in value


def list_items_for_section(text: str, section: str) -> list[str]:
    lines = text.splitlines()
    items: list[str] = []
    for index, line in enumerate(lines):
        match = TOP_LEVEL_KEY_RE.match(line)
        if not match or match.group("key") != section:
            continue
        section_indent = len(match.group("indent"))
        for item_line in lines[index + 1 :]:
            if not item_line.strip():
                continue
            item_indent = len(item_line) - len(item_line.lstrip())
            if item_indent <= section_indent:
                break
            item_match = LIST_ITEM_RE.match(item_line)
            if item_match:
                items.append(strip_scalar(item_match.group("value")))
        break
    return items


def helm_chart_home(lines: list[str]) -> str:
    in_helm_globals = False
    globals_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not in_helm_globals:
            if stripped == "helmGlobals:":
                in_helm_globals = True
                globals_indent = indent
            continue
        if indent <= globals_indent:
            return ""
        if stripped.startswith("chartHome:"):
            return strip_scalar(stripped.split(":", 1)[1])
    return ""


def parse_helm_charts(lines: list[str]) -> list[dict[str, str]]:
    charts: list[dict[str, str]] = []
    in_helm_charts = False
    helm_indent = 0
    current: dict[str, str] | None = None

    def finish() -> None:
        nonlocal current
        if current is not None:
            charts.append(current)
        current = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not in_helm_charts:
            if stripped == "helmCharts:":
                in_helm_charts = True
                helm_indent = indent
            continue
        if indent <= helm_indent and not stripped.startswith("- "):
            finish()
            in_helm_charts = False
            continue
        if stripped.startswith("- "):
            finish()
            current = {}
            field = stripped[2:]
        elif current is not None:
            field = stripped
        else:
            continue
        if ":" not in field:
            continue
        key, value = field.split(":", 1)
        current[key.strip()] = strip_scalar(value)
    finish()
    return charts


def referenced_patch_paths(text: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = TOP_LEVEL_KEY_RE.match(line)
        if not match or match.group("key") != "patches":
            continue
        section_indent = len(match.group("indent"))
        paths: list[str] = []
        for item_line in lines[index + 1 :]:
            if not item_line.strip():
                continue
            item_indent = len(item_line) - len(item_line.lstrip())
            if item_indent <= section_indent:
                break
            item_match = PATCH_ITEM_PATH_RE.match(item_line)
            if item_match:
                paths.append(strip_scalar(item_match.group("value")))
        return paths
    return []


def safe_local_target(kustomization: Path, raw_ref: str, repo_root: Path) -> Path | None:
    if not is_static_local_ref(raw_ref) or raw_ref.startswith("/"):
        return None
    target = (kustomization.parent / raw_ref).resolve()
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return target


def kustomization_children(target: Path) -> list[Path]:
    if target.is_file() and target.name in KUSTOMIZATION_NAMES:
        return [target]
    if not target.is_dir():
        return []
    return [target / name for name in KUSTOMIZATION_NAMES if (target / name).is_file()]


def check_kustomization_structure(path: Path, repo_root: Path) -> list[str]:
    findings: list[str] = []
    pending = [path]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            text = read_bounded_text(current, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(f"{display_path(current, repo_root)}: cannot read Kustomization ({exc})")
            continue

        for section in LOCAL_PATH_SECTIONS:
            for raw_ref in list_items_for_section(text, section):
                if not is_static_local_ref(raw_ref):
                    continue
                target = safe_local_target(current, raw_ref, repo_root)
                if target is None:
                    findings.append(
                        f"{display_path(current, repo_root)} references unsafe or out-of-tree {section}: {raw_ref}"
                    )
                    continue
                if not target.exists():
                    findings.append(f"{display_path(current, repo_root)} references missing {section}: {raw_ref}")
                    continue
                children = kustomization_children(target)
                if target.is_dir() and not children:
                    findings.append(
                        f"{display_path(current, repo_root)} references {section} directory without Kustomization: {raw_ref}"
                    )
                pending.extend(children)

        values_files = [strip_scalar(value) for value in VALUES_FILE_RE.findall(text)]
        for raw_ref in values_files:
            if not is_static_local_ref(raw_ref):
                continue
            target = safe_local_target(current, raw_ref, repo_root)
            if target is None:
                findings.append(f"{display_path(current, repo_root)} references unsafe local file: {raw_ref}")
            elif not target.is_file():
                findings.append(f"{display_path(current, repo_root)} references missing Helm valuesFile: {raw_ref}")
        for raw_ref in referenced_patch_paths(text):
            if not is_static_local_ref(raw_ref):
                continue
            target = safe_local_target(current, raw_ref, repo_root)
            if target is None:
                findings.append(f"{display_path(current, repo_root)} references unsafe local patch path: {raw_ref}")
            elif not target.is_file():
                findings.append(f"{display_path(current, repo_root)} references missing patch path: {raw_ref}")

        lines = text.splitlines()
        chart_home = helm_chart_home(lines)
        charts = parse_helm_charts(lines)
        for chart in charts:
            name = chart.get("name", "<unknown>")
            values_file = chart.get("valuesFile", "")
            if values_file and is_static_local_ref(values_file):
                target = safe_local_target(current, values_file, repo_root)
                if target is None or not target.is_file():
                    findings.append(
                        f"{display_path(current, repo_root)} Helm chart {name} references missing valuesFile: {values_file}"
                    )
            if chart.get("repo") or not name:
                continue
            if not chart_home:
                findings.append(
                    f"{display_path(current, repo_root)} local Helm chart {name} requires helmGlobals.chartHome"
                )
                continue
            chart_root = safe_local_target(current, chart_home, repo_root)
            if chart_root is None or not chart_root.is_dir():
                findings.append(
                    f"{display_path(current, repo_root)} references missing Helm chart home: {chart_home}"
                )
                continue
            chart_yaml = chart_root / name / "Chart.yaml"
            if not chart_yaml.is_file():
                findings.append(
                    f"{display_path(current, repo_root)} local Helm chart {name} is missing Chart.yaml: "
                    f"{display_path(chart_yaml, repo_root)}"
                )
    return findings


def profile_structure_findings(repo_root: Path, profile: str, source_paths: list[Path]) -> list[str]:
    findings: list[str] = []
    seen_sources: set[Path] = set()
    for source_path in source_paths:
        source_path = source_path.resolve()
        if source_path in seen_sources:
            continue
        seen_sources.add(source_path)
        if not source_path.is_dir():
            findings.append(f"{display_path(source_path, repo_root)}: missing application source directory")
            continue
        kustomizations = [source_path / name for name in KUSTOMIZATION_NAMES if (source_path / name).is_file()]
        if not kustomizations:
            findings.append(f"{display_path(source_path, repo_root)}: missing Kustomization")
            continue
        for kustomization in kustomizations:
            findings.extend(check_kustomization_structure(kustomization, repo_root))

    if profile == "premium-3node":
        for relative_path in REQUIRED_PREMIUM_SUPPORT_FILES:
            if not (repo_root / relative_path).is_file():
                findings.append(f"{relative_path}: required premium internal-TLS support file is missing")
    return findings


def check_profile(
    repo_root: Path,
    profile: str,
    *,
    check_placeholders: bool = True,
    require_structure: bool = False,
) -> int:
    projects_dir = repo_root / "gitops/clusters/rke2-main/projects"
    findings: list[str] = []
    optional_apps = optional_application_names(repo_root, profile)

    if profile in PROFILE_APP_FILES:
        applications_file = repo_root / PROFILE_APP_FILES[profile]
        if check_placeholders:
            findings.extend(scan_applications_file(applications_file, repo_root, optional_apps))
        source_paths = [
            path
            for path in application_source_paths(applications_file, repo_root)
            if path.name not in optional_apps
        ]
    else:
        try:
            for profile_file in profile_dependency_files(repo_root, profile):
                if check_placeholders:
                    findings.extend(scan_path(profile_file, repo_root))
            source_paths = [
                path
                for path in profile_source_paths(repo_root, profile)
                if path.name not in optional_apps
            ]
        except ValueError as exc:
            return fail(str(exc))
        if not source_paths:
            return fail(f"profile {profile!r} does not include any deployable GitOps application sources")

    if check_placeholders:
        findings.extend(scan_path(projects_dir, repo_root, allow_repo_url=True))
        for source_path in source_paths:
            findings.extend(scan_path(source_path, repo_root))
    if require_structure:
        findings.extend(profile_structure_findings(repo_root, profile, source_paths))

    if findings:
        if require_structure:
            print(f"GitOps profile {profile!r} is incomplete for deployment or contains unresolved placeholders.", file=sys.stderr)
        else:
            print(f"GitOps profile {profile!r} contains unresolved placeholders or missing paths.", file=sys.stderr)
        for finding in findings[:80]:
            print(f" - {finding}", file=sys.stderr)
        if len(findings) > 80:
            print(f" - ... {len(findings) - 80} more", file=sys.stderr)
        print(
            "Render private values, commit safe non-secret deployment values, or use a private secret/config flow before production registration.",
            file=sys.stderr,
        )
        print(
            "Public template checkouts are expected to contain placeholders; do not use skip-incomplete output as production proof.",
            file=sys.stderr,
        )
        print(
            "Render deployment-specific values with platform-render-private-values or the first-deploy seed/private flow, then rerun platform-profile-check or platform-production-check.",
            file=sys.stderr,
        )
        return 1

    print(f"GitOps profile {profile!r} is complete for production registration.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--profile",
        default="premium-3node",
        help="GitOps profile to check. Supports default, premium-3node, and catalog profiles in profiles/.",
    )
    parser.add_argument(
        "--require-structure",
        action="store_true",
        help="Require selected application trees, local Kustomize references, vendored charts, and premium TLS support files.",
    )
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="Skip placeholder scanning while retaining structural checks for template or skip-incomplete flows.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    try:
        return check_profile(
            repo_root,
            args.profile,
            check_placeholders=not args.allow_placeholders,
            require_structure=args.require_structure,
        )
    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
