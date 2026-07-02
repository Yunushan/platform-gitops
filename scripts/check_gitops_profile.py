#!/usr/bin/env python3
"""Check that a GitOps profile is complete enough for production registration."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
APPLICATION_PATH_RE = re.compile(
    r"""(?m)^\s+path:\s*(?P<quote>['"]?)(?P<path>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)
VENDORED_PATH_PARTS = {"charts", "crds"}
EXAMPLE_SUFFIXES = (".example.yaml", ".example.yml")


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
        lines = path.read_text(encoding="utf-8").splitlines()
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


def application_source_paths(applications_file: Path, repo_root: Path) -> list[Path]:
    text = applications_file.read_text(encoding="utf-8")
    paths: list[Path] = []
    for match in APPLICATION_PATH_RE.finditer(text):
        paths.append(repo_root / match.group("path"))
    return paths


def profile_applications_file(repo_root: Path, profile: str) -> Path:
    if profile == "default":
        return repo_root / "gitops/clusters/rke2-main/platform-apps.yaml"
    if profile == "premium-3node":
        return repo_root / "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml"
    raise ValueError(f"unsupported profile {profile!r}")


def check_profile(repo_root: Path, profile: str) -> int:
    applications_file = profile_applications_file(repo_root, profile)
    projects_dir = repo_root / "gitops/clusters/rke2-main/projects"
    findings: list[str] = []

    findings.extend(scan_path(applications_file, repo_root, allow_repo_url=True))
    findings.extend(scan_path(projects_dir, repo_root, allow_repo_url=True))
    for source_path in application_source_paths(applications_file, repo_root):
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
        choices=("default", "premium-3node"),
        default="premium-3node",
        help="GitOps profile to check. Defaults to premium-3node.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    try:
        return check_profile(repo_root, args.profile)
    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
