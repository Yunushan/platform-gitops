#!/usr/bin/env python3
"""Validate the bounded ClusterFuzzLite integration for forge plan parsers."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_migration
import fuzz_forge_plans


ACTION_SHA = "884713a6c30a92e5e8544c39945cd7cb630abcd1"
BUILDER_DIGEST = "sha256:485af05cc843949fd60d0e6e78b1a4922e661baf9af5eb595f9b96f563b42748"


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"missing required fuzzing file: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, *needles: str, label: str) -> None:
    for needle in needles:
        if needle not in text:
            raise AssertionError(f"{label} is missing required text: {needle}")


def test_static_contract() -> None:
    dockerignore = read(ROOT / ".dockerignore")
    project = read(ROOT / ".clusterfuzzlite" / "project.yaml")
    dockerfile = read(ROOT / ".clusterfuzzlite" / "Dockerfile")
    build = read(ROOT / ".clusterfuzzlite" / "build.sh")
    fuzzer = read(ROOT / ".clusterfuzzlite" / "fuzzers" / "forge_plan_fuzzer.py")
    surface = read(ROOT / "scripts" / "fuzz_forge_plans.py")
    workflow = read(ROOT / ".github" / "workflows" / "fuzz.yml")

    require(
        dockerignore,
        ".git",
        "private",
        "rendered",
        "secrets",
        "config/*.local.yaml",
        "inventory/*.local.*",
        "*.key",
        "*.pem",
        "*.kubeconfig",
        label=".dockerignore",
    )

    if project.strip() != "language: python":
        raise AssertionError("ClusterFuzzLite project language must be exactly python")
    require(
        dockerfile,
        f"FROM gcr.io/oss-fuzz-base/base-builder-python@{BUILDER_DIGEST}",
        "COPY . $SRC/platform-gitops",
        "RUN chmod +x $SRC/build.sh",
        label=".clusterfuzzlite/Dockerfile",
    )
    require(
        build,
        "pyinstaller",
        "--onefile",
        "LLVMFuzzerTestOneInput",
        '${fuzzer_name}_seed_corpus.zip',
        "examples\" / \"migrations",
        label=".clusterfuzzlite/build.sh",
    )
    if "LD_PRELOAD" in build:
        raise AssertionError("pure-Python fuzzer must not preload a sanitizer runtime")
    require(
        fuzzer,
        "atheris.instrument_imports()",
        "atheris.Setup(sys.argv, test_one_input)",
        "fuzz_forge_plans.exercise_input(data)",
        label="forge_plan_fuzzer.py",
    )
    require(
        surface,
        "MAX_INPUT_BYTES = 128 * 1024",
        "MAX_STRUCTURE_DEPTH = 64",
        "forge_migration.parse_plan",
        "forge_cutover.parse_cutover_plan",
        "forge_transition.parse_transition_plan",
        "except forge_migration.MigrationError",
        label="scripts/fuzz_forge_plans.py",
    )
    require(
        workflow,
        "permissions: read-all",
        "runs-on: ubuntu-24.04",
        f"google/clusterfuzzlite/actions/build_fuzzers@{ACTION_SHA}",
        f"google/clusterfuzzlite/actions/run_fuzzers@{ACTION_SHA}",
        "language: python",
        "sanitizer: address",
        "mode: ${{ github.event_name == 'pull_request' && 'code-change' || 'batch' }}",
        "mode: prune",
        "output-sarif: true",
        label=".github/workflows/fuzz.yml",
    )


def test_deterministic_inputs() -> None:
    inputs = [
        b"",
        b"not-json",
        b"\xff\xfe",
        b"null",
        b"[]",
        b"{}",
        b'{"direction":"gitlab-to-forgejo","repositories":[]}',
        b'{"token":"literal"}',
        b"x" * (fuzz_forge_plans.MAX_INPUT_BYTES + 1),
    ]
    inputs.extend(
        path.read_bytes()
        for path in sorted((ROOT / "examples" / "migrations").glob("*.json"))
    )
    if len(inputs) < 10:
        raise AssertionError("fuzz smoke corpus is unexpectedly small")
    for payload in inputs:
        fuzz_forge_plans.exercise_input(payload)

    original = forge_migration.parse_plan

    def unexpected(_plan):
        raise ValueError("unexpected parser defect")

    forge_migration.parse_plan = unexpected
    try:
        try:
            fuzz_forge_plans.exercise_input(b"{}")
        except ValueError as exc:
            if "unexpected parser defect" not in str(exc):
                raise
        else:
            raise AssertionError("fuzz surface swallowed an unexpected parser exception")
    finally:
        forge_migration.parse_plan = original


def main() -> int:
    test_static_contract()
    test_deterministic_inputs()
    print("Forge plan ClusterFuzzLite contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
