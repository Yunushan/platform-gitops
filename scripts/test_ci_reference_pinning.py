#!/usr/bin/env python3
"""Validate CI actions and container images avoid floating refs."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI_FILES = (
    ROOT / ".github" / "workflows" / "validate.yml",
    ROOT / ".gitea" / "workflows" / "validate.yml",
    ROOT / ".forgejo" / "workflows" / "validate.yml",
    ROOT / ".gitlab-ci.yml",
    ROOT / ".woodpecker" / "validate.yml",
    ROOT / "examples" / "service-template" / ".github" / "workflows" / "ci.yml",
    ROOT / "examples" / "service-template" / ".gitea" / "workflows" / "ci.yml",
    ROOT / "examples" / "service-template" / ".forgejo" / "workflows" / "ci.yml",
    ROOT / "examples" / "service-template" / ".gitlab-ci.yml",
    ROOT / "examples" / "service-template" / ".woodpecker.yml",
)
DOCKERFILES = tuple((ROOT / "examples").rglob("Dockerfile"))
MUTABLE_REFS = {
    "latest",
    "main",
    "master",
    "dev",
    "devel",
    "develop",
    "development",
    "edge",
    "nightly",
    "next",
    "snapshot",
    "canary",
    "unstable",
}
MUTABLE_PREFIXES = tuple(f"{ref}-" for ref in MUTABLE_REFS)
USES_RE = re.compile(r"^\s*-\s+uses:\s*(?P<ref>\S+)\s*(?:#.*)?$")
IMAGE_RE = re.compile(r"^\s*image:\s*(?P<image>\S+)\s*(?:#.*)?$")
FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?:\s+AS\s+\S+)?\s*(?:#.*)?$", re.I)


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def is_mutable(ref: str) -> bool:
    normalized = ref.lower()
    return normalized in MUTABLE_REFS or normalized.startswith(MUTABLE_PREFIXES)


def image_tag(image: str) -> str:
    if "@sha256:" in image:
        return ""
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return "<missing>"
    return last_segment.rsplit(":", 1)[-1]


def check_action_ref(path: Path, line_number: int, value: str) -> list[str]:
    problems: list[str] = []
    if "@" not in value:
        problems.append(f"{rel_path(path)}:{line_number}: action reference must include @ref: {value}")
        return problems
    ref = value.rsplit("@", 1)[-1]
    if not ref:
        problems.append(f"{rel_path(path)}:{line_number}: action reference has an empty @ref: {value}")
    elif is_mutable(ref):
        problems.append(f"{rel_path(path)}:{line_number}: action reference uses floating ref {ref}: {value}")
    return problems


def check_image_ref(path: Path, line_number: int, value: str) -> list[str]:
    image = strip_quotes(value)
    if "$" in image or "{{" in image or "<" in image:
        return []
    tag = image_tag(image)
    if tag == "<missing>":
        return [f"{rel_path(path)}:{line_number}: container image must pin a tag or sha256 digest: {image}"]
    if tag and is_mutable(tag):
        return [f"{rel_path(path)}:{line_number}: container image uses floating tag {tag}: {image}"]
    return []


def scan_ci_file(path: Path) -> list[str]:
    problems: list[str] = []
    if not path.exists():
        problems.append(f"{rel_path(path)}: expected CI file is missing")
        return problems
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        uses = USES_RE.match(line)
        if uses:
            problems.extend(check_action_ref(path, line_number, strip_quotes(uses.group("ref"))))
            continue
        image = IMAGE_RE.match(line)
        if image:
            problems.extend(check_image_ref(path, line_number, image.group("image")))
    return problems


def scan_dockerfile(path: Path) -> list[str]:
    problems: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = FROM_RE.match(line)
        if match:
            problems.extend(check_image_ref(path, line_number, match.group("image")))
    return problems


def main() -> int:
    problems: list[str] = []
    for path in CI_FILES:
        problems.extend(scan_ci_file(path))
    for path in DOCKERFILES:
        problems.extend(scan_dockerfile(path))

    if problems:
        print("CI reference pinning validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(f"CI reference pinning validation passed for {len(CI_FILES)} CI files and {len(DOCKERFILES)} Dockerfiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
