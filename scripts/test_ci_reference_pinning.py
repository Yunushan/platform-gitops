#!/usr/bin/env python3
"""Validate CI references, credentials, runners, and execution bounds."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIONS_WORKFLOW_DIRS = (
    ROOT / ".github" / "workflows",
    ROOT / ".gitea" / "workflows",
    ROOT / ".forgejo" / "workflows",
    ROOT / "examples" / "service-template" / ".github" / "workflows",
    ROOT / "examples" / "service-template" / ".gitea" / "workflows",
    ROOT / "examples" / "service-template" / ".forgejo" / "workflows",
)
ACTIONS_WORKFLOW_FILES = tuple(
    path
    for directory in ACTIONS_WORKFLOW_DIRS
    for path in sorted(directory.iterdir())
    if path.is_file() and path.suffix in {".yml", ".yaml"}
)
GITHUB_WORKFLOW_FILES = {
    path
    for path in ACTIONS_WORKFLOW_FILES
    if path.parent.parent.name == ".github"
}
GITLAB_CI_FILES = (
    ROOT / ".gitlab-ci.yml",
    ROOT / "examples" / "service-template" / ".gitlab-ci.yml",
)
CI_FILES = ACTIONS_WORKFLOW_FILES + GITLAB_CI_FILES + (
    ROOT / ".woodpecker" / "validate.yml",
    ROOT / "examples" / "service-template" / ".woodpecker.yml",
)
WOODPECKER_VALUES = (
    ROOT
    / "gitops"
    / "clusters"
    / "rke2-main"
    / "premium-3node"
    / "apps"
    / "woodpecker"
    / "values.yaml"
)
DOCKERFILES = tuple(ROOT.rglob("Dockerfile"))
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
ACTION_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
IMAGE_TEMPLATE_MARKERS = ("$", "{{", "}}", "<", ">")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)\s*(?:#.*)?$")
IMAGE_RE = re.compile(r"^\s*image:\s*(?P<image>\S+)\s*(?:#.*)?$")
FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?:\s+AS\s+\S+)?\s*(?:#.*)?$", re.I)
JOB_RE = re.compile(r"^  (?P<name>[A-Za-z_][A-Za-z0-9_-]*):\s*$")
TIMEOUT_RE = re.compile(r"^    timeout-minutes:\s*(?P<minutes>[0-9]+)\s*$")
RUNNER_RE = re.compile(r"^    runs-on:\s*(?P<runner>[^#\s]+)")
GITLAB_KEY_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_.-]*):\s*$")
GITLAB_TIMEOUT_RE = re.compile(r"^  timeout:\s*(?P<minutes>[0-9]+)m\s*$")
GITLAB_RESERVED_KEYS = {
    "after_script",
    "before_script",
    "cache",
    "default",
    "image",
    "include",
    "services",
    "stages",
    "variables",
    "workflow",
}
MAX_JOB_TIMEOUT_MINUTES = 120


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def is_mutable(ref: str) -> bool:
    normalized = ref.lower()
    return normalized in MUTABLE_REFS or normalized.startswith(MUTABLE_PREFIXES)


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
    elif not ACTION_SHA_RE.fullmatch(ref):
        problems.append(f"{rel_path(path)}:{line_number}: action reference must pin a full commit SHA, not a tag or branch: {value}")
    return problems


def check_image_ref(path: Path, line_number: int, value: str) -> list[str]:
    image = strip_quotes(value)
    if any(marker in image for marker in IMAGE_TEMPLATE_MARKERS) or not IMAGE_DIGEST_RE.search(image):
        return [
            f"{rel_path(path)}:{line_number}: CI container image must pin a literal lowercase "
            f"sha256 digest: {image}"
        ]
    return []


def check_image_ref_contract() -> list[str]:
    path = ROOT / ".gitlab-ci.yml"
    digest = "a" * 64
    valid = (
        f"python:3.12-slim@sha256:{digest}",
        f"'python:3.12-slim@sha256:{digest}'",
    )
    invalid = (
        "python:3.12-slim",
        "python:latest",
        f"python:3.12-slim@sha256:{digest.upper()}",
        "${CI_IMAGE}",
        "{{ image }}",
        f"${{CI_IMAGE}}@sha256:{digest}",
        f"{{{{ image }}}}@sha256:{digest}",
    )
    problems: list[str] = []
    for image in valid:
        if check_image_ref(path, 1, image):
            problems.append(f"CI image pinning self-test rejected valid digest reference: {image}")
    for image in invalid:
        if not check_image_ref(path, 1, image):
            problems.append(f"CI image pinning self-test accepted mutable reference: {image}")
    return problems


def actions_job_blocks(path: Path, lines: list[str]) -> list[tuple[str, int, list[str]]]:
    try:
        jobs_index = lines.index("jobs:")
    except ValueError:
        return []
    starts: list[tuple[str, int]] = []
    for index in range(jobs_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            break
        match = JOB_RE.match(line)
        if match:
            starts.append((match.group("name"), index))
    blocks: list[tuple[str, int, list[str]]] = []
    for position, (name, start) in enumerate(starts):
        end = starts[position + 1][1] if position + 1 < len(starts) else len(lines)
        blocks.append((name, start + 1, lines[start + 1 : end]))
    return blocks


def check_actions_execution_contract(path: Path, lines: list[str]) -> list[str]:
    problems: list[str] = []
    blocks = actions_job_blocks(path, lines)
    if not blocks:
        return [f"{rel_path(path)}: Actions workflow has no jobs"]
    for job, line_number, block in blocks:
        timeouts = [TIMEOUT_RE.match(line) for line in block]
        timeout_values = [int(match.group("minutes")) for match in timeouts if match]
        if len(timeout_values) != 1:
            problems.append(
                f"{rel_path(path)}:{line_number}: job {job} must declare exactly one timeout-minutes"
            )
        elif not 1 <= timeout_values[0] <= MAX_JOB_TIMEOUT_MINUTES:
            problems.append(
                f"{rel_path(path)}:{line_number}: job {job} timeout must be between 1 and "
                f"{MAX_JOB_TIMEOUT_MINUTES} minutes"
            )
        if path in GITHUB_WORKFLOW_FILES:
            for line in block:
                runner = RUNNER_RE.match(line)
                if runner and strip_quotes(runner.group("runner")).lower().endswith("-latest"):
                    problems.append(
                        f"{rel_path(path)}:{line_number}: GitHub job {job} uses moving runner label "
                        f"{runner.group('runner')}"
                    )

    for index, line in enumerate(lines):
        uses = USES_RE.match(line)
        if not uses:
            continue
        action = strip_quotes(uses.group("ref")).rsplit("@", 1)[0].rstrip("/").lower()
        if not action.endswith("actions/checkout"):
            continue
        base_indent = len(line) - len(line.lstrip())
        end = len(lines)
        for next_index in range(index + 1, len(lines)):
            candidate = lines[next_index]
            if not candidate.strip():
                continue
            indent = len(candidate) - len(candidate.lstrip())
            if indent <= base_indent and candidate.lstrip().startswith("- "):
                end = next_index
                break
        step = lines[index + 1 : end]
        if not any(re.match(r"^\s+persist-credentials:\s*false\s*$", item) for item in step):
            problems.append(
                f"{rel_path(path)}:{index + 1}: checkout must set persist-credentials: false"
            )
    return problems


def check_gitlab_timeouts(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = GITLAB_KEY_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name not in GITLAB_RESERVED_KEYS and not name.startswith("."):
            starts.append((name, index))
    problems: list[str] = []
    for position, (name, start) in enumerate(starts):
        end = starts[position + 1][1] if position + 1 < len(starts) else len(lines)
        values = [
            int(match.group("minutes"))
            for line in lines[start + 1 : end]
            if (match := GITLAB_TIMEOUT_RE.match(line))
        ]
        if len(values) != 1 or not 1 <= values[0] <= MAX_JOB_TIMEOUT_MINUTES:
            problems.append(
                f"{rel_path(path)}:{start + 1}: GitLab job {name} must declare a timeout from "
                f"1m through {MAX_JOB_TIMEOUT_MINUTES}m"
            )
    return problems


def scan_ci_file(path: Path) -> list[str]:
    problems: list[str] = []
    if not path.exists():
        problems.append(f"{rel_path(path)}: expected CI file is missing")
        return problems
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        uses = USES_RE.match(line)
        if uses:
            problems.extend(check_action_ref(path, line_number, strip_quotes(uses.group("ref"))))
            continue
        image = IMAGE_RE.match(line)
        if image:
            problems.extend(check_image_ref(path, line_number, image.group("image")))
    if path in ACTIONS_WORKFLOW_FILES:
        problems.extend(check_actions_execution_contract(path, lines))
    return problems


def scan_dockerfile(path: Path) -> list[str]:
    problems: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = FROM_RE.match(line)
        if match:
            image = strip_quotes(match.group("image"))
            if not IMAGE_DIGEST_RE.search(image):
                problems.append(
                    f"{rel_path(path)}:{line_number}: Dockerfile base image must pin a lowercase sha256 digest: {image}"
                )
    return problems


def main() -> int:
    problems = check_image_ref_contract()
    for path in CI_FILES:
        problems.extend(scan_ci_file(path))
    for path in GITLAB_CI_FILES:
        problems.extend(check_gitlab_timeouts(path))
    for path in DOCKERFILES:
        problems.extend(scan_dockerfile(path))
    github_validation = ROOT / ".github" / "workflows" / "validate.yml"
    github_validation_text = github_validation.read_text(encoding="utf-8")
    for needle in (
        "permissions:\n  contents: read",
        "concurrency:\n  group: validate-${{ github.workflow }}-${{ github.ref }}\n  cancel-in-progress: true",
    ):
        if needle not in github_validation_text:
            problems.append(f"{rel_path(github_validation)}: missing workflow security control: {needle.splitlines()[0]}")

    woodpecker_values = WOODPECKER_VALUES.read_text(encoding="utf-8")
    for needle in (
        'WOODPECKER_DEFAULT_PIPELINE_TIMEOUT: "60"',
        'WOODPECKER_MAX_PIPELINE_TIMEOUT: "120"',
    ):
        if needle not in woodpecker_values:
            problems.append(f"{rel_path(WOODPECKER_VALUES)}: missing bounded pipeline control: {needle}")

    if problems:
        print("CI execution and reference validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(
        "CI execution and reference validation passed for "
        f"{len(CI_FILES)} CI files and {len(DOCKERFILES)} Dockerfiles."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
