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
        'MINIMUM="${FORGE_COVERAGE_MIN:-79.0}"',
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
    if threshold_match is None or float(threshold_match.group(1)) < 79.0:
        raise AssertionError("forge branch-coverage threshold must not fall below the measured 79.0% ratchet")
    require(
        workflow,
        "coverage==7.15.2",
        "bash scripts/forge-coverage.sh",
        "name: forge-coverage-${{ github.sha }}",
        "path: rendered/coverage",
        "if: always()",
        label=".github/workflows/validate.yml",
    )

    print("Forge subprocess branch-coverage contract passed at a 79.0% minimum.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
