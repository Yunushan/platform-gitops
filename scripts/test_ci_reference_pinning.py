#!/usr/bin/env python3
"""Validate CI references, credentials, runners, and execution bounds."""

from __future__ import annotations

import re
import shlex
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
SEMGREP_WORKFLOWS = (
    ROOT / ".github" / "workflows" / "validate.yml",
    ROOT / ".github" / "workflows" / "release.yml",
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
CI_REQUIREMENT_LOCKS = {
    ROOT / "requirements" / "ci-yaml.txt": (
        "PyYAML==6.0.3",
        frozenset(
            {
                "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d",
                "9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28",
                "ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc",
            }
        ),
    ),
    ROOT / "requirements" / "ci-coverage.txt": (
        "coverage==7.15.2",
        frozenset(
            {
                "68af907f595ab01a78f794932ff3bdf929c316d3000810d38dbc247129e26f8b",
                "afa29e2eff3d5729267e2cb2fd4ce9d61c952932fb2694e34ccb5d9540c6a296",
            }
        ),
    ),
}
CI_REQUIRED_LOCKS = {
    ROOT / ".github" / "workflows" / "validate.yml": (
        "requirements/ci-yaml.txt",
        "requirements/ci-coverage.txt",
    ),
    ROOT / ".github" / "workflows" / "release.yml": ("requirements/ci-yaml.txt",),
    ROOT / ".gitea" / "workflows" / "validate.yml": ("requirements/ci-yaml.txt",),
    ROOT / ".forgejo" / "workflows" / "validate.yml": ("requirements/ci-yaml.txt",),
    ROOT / ".gitlab-ci.yml": ("requirements/ci-yaml.txt",),
    ROOT / ".woodpecker" / "validate.yml": ("requirements/ci-yaml.txt",),
}
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
PYTHON_VERSION_RE = re.compile(r"^\s+python-version:\s*(?P<version>[^#\s]+)\s*(?:#.*)?$")
PACKAGE_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+$")
HASH_TOKEN_RE = re.compile(r"^--hash=sha256:(?P<digest>[0-9a-f]{64})$")
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
PINNED_PYTHON_VERSION = "3.12.13"
SEMGREP_IMAGE_REF = (
    "semgrep/semgrep:1.171.0@"
    "sha256:bdf7013b2c3634a487671158da77c554f531742326b543a9464d2adf6c433ac8"
)


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_requirement_lock_text(text: str) -> tuple[str, frozenset[str]]:
    logical = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", text)
    entries = [
        line.strip()
        for line in logical.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(entries) != 1:
        raise ValueError("lock file must contain exactly one requirement")
    tokens = entries[0].split()
    if not tokens or not PACKAGE_PIN_RE.fullmatch(tokens[0]):
        raise ValueError("requirement must use one exact name==version pin")
    hashes: list[str] = []
    for token in tokens[1:]:
        match = HASH_TOKEN_RE.fullmatch(token)
        if not match:
            raise ValueError("requirement may contain only lowercase sha256 hashes")
        hashes.append(match.group("digest"))
    if not hashes or len(hashes) != len(set(hashes)):
        raise ValueError("requirement hashes must be present and unique")
    return tokens[0], frozenset(hashes)


def parse_requirement_lock(path: Path) -> tuple[str, frozenset[str]]:
    if not path.is_file():
        raise ValueError("lock file is missing")
    return parse_requirement_lock_text(path.read_text(encoding="utf-8"))


def check_requirement_lock_parser_contract() -> list[str]:
    digest_a = "a" * 64
    digest_b = "b" * 64
    valid = (
        f"example==1.2.3 \\\n"
        f"    --hash=sha256:{digest_a} \\\n"
        f"    --hash=sha256:{digest_b}\n"
    )
    invalid = (
        "example>=1.2.3 --hash=sha256:" + digest_a,
        "example==1.2.3",
        "example==1.2.3 --hash=sha256:" + digest_a + " --index-url=https://example.test",
        "example==1.2.3 --hash=sha256:" + digest_a + " --hash=sha256:" + digest_a,
        "example==1.2.3 --hash=sha256:" + digest_a.upper(),
        "example==1.2.3 --hash=sha256:" + digest_a + "\nother==2.0 --hash=sha256:" + digest_b,
    )
    problems: list[str] = []
    try:
        pin, hashes = parse_requirement_lock_text(valid)
    except ValueError as exc:
        problems.append(f"CI requirement lock parser self-test rejected valid lock: {exc}")
    else:
        if pin != "example==1.2.3" or hashes != frozenset({digest_a, digest_b}):
            problems.append("CI requirement lock parser self-test changed valid lock meaning")
    for text in invalid:
        try:
            parse_requirement_lock_text(text)
        except ValueError:
            continue
        problems.append("CI requirement lock parser self-test accepted an unsafe lock")
    return problems


def check_requirement_lock_contract() -> list[str]:
    problems: list[str] = []
    for path, (expected_pin, expected_hashes) in CI_REQUIREMENT_LOCKS.items():
        try:
            pin, hashes = parse_requirement_lock(path)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"{rel_path(path)}: invalid CI requirement lock: {exc}")
            continue
        if pin != expected_pin:
            problems.append(f"{rel_path(path)}: expected exact package pin {expected_pin}; found {pin}")
        if hashes != expected_hashes:
            problems.append(f"{rel_path(path)}: package wheel hashes do not match the reviewed lock")
    return problems


def check_pip_install(path: Path, line_number: int, line: str) -> list[str]:
    if "python -m pip install" not in line:
        return []
    problems: list[str] = []
    for flag in (
        "--disable-pip-version-check",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
    ):
        if flag not in line:
            problems.append(f"{rel_path(path)}:{line_number}: CI pip install must include {flag}")
    try:
        tokens = shlex.split(line, comments=False, posix=True)
        command_index = next(
            index
            for index in range(len(tokens) - 3)
            if tokens[index : index + 4] == ["python", "-m", "pip", "install"]
        )
    except (ValueError, StopIteration):
        return [f"{rel_path(path)}:{line_number}: CI pip install command could not be parsed"]

    requirement_paths: list[str] = []
    allowed_flags = {
        "--disable-pip-version-check",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
    }
    install_tokens = tokens[command_index + 4 :]
    index = 0
    while index < len(install_tokens):
        argument = install_tokens[index]
        if argument == "--requirement":
            if index + 1 >= len(install_tokens):
                problems.append(f"{rel_path(path)}:{line_number}: --requirement needs a lock path")
                break
            requirement_paths.append(install_tokens[index + 1])
            index += 2
            continue
        if argument not in allowed_flags:
            problems.append(
                f"{rel_path(path)}:{line_number}: CI pip install contains unapproved argument {argument}"
            )
        index += 1

    allowed_paths = {rel_path(lock) for lock in CI_REQUIREMENT_LOCKS}
    if not requirement_paths:
        problems.append(f"{rel_path(path)}:{line_number}: CI pip install must use a reviewed requirement lock")
    elif any(requirement not in allowed_paths for requirement in requirement_paths):
        problems.append(f"{rel_path(path)}:{line_number}: CI pip install references an unreviewed lock")
    expected_paths = CI_REQUIRED_LOCKS.get(path)
    if expected_paths is None:
        problems.append(f"{rel_path(path)}:{line_number}: CI pip install is not registered in the lock contract")
    elif tuple(requirement_paths) != expected_paths:
        problems.append(
            f"{rel_path(path)}:{line_number}: CI pip install must use locks in reviewed order: "
            f"{', '.join(expected_paths)}"
        )
    return problems


def check_requirement_usage_contract() -> list[str]:
    problems: list[str] = []
    for path in CI_REQUIRED_LOCKS:
        lines = path.read_text(encoding="utf-8").splitlines()
        installs = [line for line in lines if "python -m pip install" in line]
        if len(installs) != 1:
            problems.append(
                f"{rel_path(path)}: must contain exactly one hash-locked CI pip install; "
                f"found {len(installs)}"
            )
    return problems


def check_semgrep_container_contract() -> list[str]:
    problems: list[str] = []
    for path in SEMGREP_WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        for marker in (
            f"SEMGREP_IMAGE: {SEMGREP_IMAGE_REF}",
            "docker run --rm",
            "--network none",
            "--read-only",
            "--security-opt no-new-privileges",
            "--tmpfs /tmp:rw,noexec,nosuid,size=512m",
            "--env HOME=/tmp",
            '--volume "${PWD}:/src:ro"',
            "--workdir /src",
            '"${SEMGREP_IMAGE}"',
            "semgrep scan --config .semgrep.yml --error --metrics=off .",
        ):
            if marker not in text:
                problems.append(f"{rel_path(path)}: hardened Semgrep container is missing {marker}")
        if re.search(r"pip\s+install[^\n]*semgrep", text, re.I):
            problems.append(f"{rel_path(path)}: Semgrep must not be installed from an unhashed pip graph")
    return problems


def check_go_install(path: Path, line_number: int, line: str) -> list[str]:
    if "go install " not in line:
        return []
    return [
        f"{rel_path(path)}:{line_number}: CI must install reviewed release "
        "artifacts instead of resolving a Go module graph at runtime"
    ]


def check_go_install_contract() -> list[str]:
    path = ROOT / ".github" / "workflows" / "validate.yml"
    if check_go_install(path, 1, "run: echo safe") or not check_go_install(
        path,
        1,
        "run: go install example.invalid/tool@v1.2.3",
    ):
        return ["CI Go-install rejection self-test changed meaning"]
    return []


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


def action_step_lines(lines: list[str], index: int) -> list[str]:
    base_indent = len(lines[index]) - len(lines[index].lstrip())
    end = len(lines)
    for next_index in range(index + 1, len(lines)):
        candidate = lines[next_index]
        if not candidate.strip():
            continue
        indent = len(candidate) - len(candidate.lstrip())
        if indent <= base_indent and candidate.lstrip().startswith("- "):
            end = next_index
            break
    return lines[index + 1 : end]


def check_setup_python_step(path: Path, line_number: int, step: list[str]) -> list[str]:
    versions = [
        strip_quotes(match.group("version"))
        for line in step
        if (match := PYTHON_VERSION_RE.match(line))
    ]
    if versions != [PINNED_PYTHON_VERSION]:
        actual = ",".join(versions) if versions else "missing"
        return [
            f"{rel_path(path)}:{line_number}: setup-python must declare exactly "
            f"python-version {PINNED_PYTHON_VERSION}; found {actual}"
        ]
    return []


def check_python_runtime_contract() -> list[str]:
    path = ROOT / ".github" / "workflows" / "validate.yml"
    valid = [[f"          python-version: '{PINNED_PYTHON_VERSION}'"]]
    invalid = [
        [],
        ["          python-version: '3.x'"],
        ["          python-version: '3.12'"],
        ["          python-version: '${{ matrix.python }}'"],
        [
            f"          python-version: '{PINNED_PYTHON_VERSION}'",
            f"          python-version: '{PINNED_PYTHON_VERSION}'",
        ],
    ]
    problems: list[str] = []
    for step in valid:
        if check_setup_python_step(path, 1, step):
            problems.append("Python runtime self-test rejected the exact pinned version")
    for step in invalid:
        if not check_setup_python_step(path, 1, step):
            problems.append(f"Python runtime self-test accepted an invalid selector: {step}")
    return problems


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
        step = action_step_lines(lines, index)
        if action.endswith("actions/checkout") and not any(
            re.match(r"^\s+persist-credentials:\s*false\s*$", item) for item in step
        ):
            problems.append(
                f"{rel_path(path)}:{index + 1}: checkout must set persist-credentials: false"
            )
        if action.endswith("actions/setup-python"):
            problems.extend(check_setup_python_step(path, index + 1, step))
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
        problems.extend(check_go_install(path, line_number, line))
        problems.extend(check_pip_install(path, line_number, line))
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
    problems = (
        check_image_ref_contract()
        + check_python_runtime_contract()
        + check_requirement_lock_parser_contract()
        + check_requirement_lock_contract()
        + check_requirement_usage_contract()
        + check_semgrep_container_contract()
        + check_go_install_contract()
    )
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
