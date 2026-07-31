#!/usr/bin/env python3
"""Validate the subprocess-aware forge branch-coverage gate."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    path = ROOT / relative
    if not path.is_file():
        raise AssertionError(f"missing required coverage file: {relative}")
    return path.read_text(encoding="utf-8")


def require(text: str, *needles: str, label: str) -> None:
    for needle in needles:
        if needle not in text:
            raise AssertionError(f"{label} is missing required text: {needle}")


def main() -> int:
    config = read(".coveragerc")
    script = read("scripts/forge-coverage.sh")
    workflow = read(".github/workflows/validate.yml")
    coverage_lock = read("requirements/ci-coverage.txt")

    require(
        config,
        "branch = True",
        "parallel = True",
        "patch =\n    subprocess",
        "scripts/forge_migration.py",
        "scripts/forge_cutover.py",
        "scripts/forge_transition.py",
        label=".coveragerc",
    )
    require(
        script,
        'MINIMUM="${FORGE_COVERAGE_MIN:-81.0}"',
        'PYTHON_BIN="${PYTHON_BIN:-python}"',
        "scripts/test_forge_migration.py",
        "scripts/test_forge_cutover.py",
        "scripts/test_forge_transition.py",
        "coverage combine",
        "forge-coverage.json",
        "forge-coverage.xml",
        '--fail-under="${MINIMUM}"',
        label="scripts/forge-coverage.sh",
    )
    threshold_match = re.search(r"FORGE_COVERAGE_MIN:-([0-9.]+)", script)
    if threshold_match is None or float(threshold_match.group(1)) < 81.0:
        raise AssertionError("forge branch-coverage threshold must not fall below the measured 81.0% ratchet")
    require(
        workflow,
        "requirements/ci-coverage.txt",
        "--require-hashes",
        "--no-deps",
        "--only-binary=:all:",
        "bash scripts/forge-coverage.sh",
        "name: forge-coverage-${{ github.sha }}",
        "path: rendered/coverage",
        "if: always()",
        label=".github/workflows/validate.yml",
    )
    require(
        coverage_lock,
        "coverage==7.15.2",
        "68af907f595ab01a78f794932ff3bdf929c316d3000810d38dbc247129e26f8b",
        "afa29e2eff3d5729267e2cb2fd4ce9d61c952932fb2694e34ccb5d9540c6a296",
        label="requirements/ci-coverage.txt",
    )

    print("Forge subprocess branch-coverage contract passed at an 81.0% minimum.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
