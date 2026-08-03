#!/usr/bin/env python3
"""Check that a GitOps profile is complete enough for production registration."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bounded_file import read_bounded_text


PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
APPLICATION_PATH_RE = re.compile(
    r"""(?m)^\s+path:\s*(?P<quote>['"]?)(?P<path>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)
VENDORED_PATH_PARTS = {"charts", "crds"}
EXAMPLE_SUFFIXES = (".example.yaml", ".example.yml")
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


def check_profile(repo_root: Path, profile: str) -> int:
    projects_dir = repo_root / "gitops/clusters/rke2-main/projects"
    findings: list[str] = []

    if profile in PROFILE_APP_FILES:
        applications_file = repo_root / PROFILE_APP_FILES[profile]
        findings.extend(scan_path(applications_file, repo_root, allow_repo_url=True))
        source_paths = application_source_paths(applications_file, repo_root)
    else:
        try:
            for profile_file in profile_dependency_files(repo_root, profile):
                findings.extend(scan_path(profile_file, repo_root))
            source_paths = profile_source_paths(repo_root, profile)
        except ValueError as exc:
            return fail(str(exc))
        if not source_paths:
            return fail(f"profile {profile!r} does not include any deployable GitOps application sources")

    findings.extend(scan_path(projects_dir, repo_root, allow_repo_url=True))
    for source_path in source_paths:
        findings.extend(scan_path(source_path, repo_root))

    if findings:
        print(
            f"GitOps profile {profile!r} contains unresolved placeholders or missing paths.",
            file=sys.stderr,
        )
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
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    try:
        return check_profile(repo_root, args.profile)
    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
