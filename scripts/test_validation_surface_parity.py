#!/usr/bin/env python3
"""Keep validation runner, CI, bootstrap, and Makefile surfaces in sync."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
RUNNER = ROOT / "scripts" / "run_validation.py"
VALIDATION_SCRIPT_RE = re.compile(r"scripts/[A-Za-z0-9_./-]+\.py")
CI_SURFACE_FILES = [
    ROOT / ".github" / "workflows" / "validate.yml",
    ROOT / ".gitea" / "workflows" / "validate.yml",
    ROOT / ".forgejo" / "workflows" / "validate.yml",
    ROOT / ".gitlab-ci.yml",
    ROOT / ".woodpecker" / "validate.yml",
]
RUNNER_SURFACE_FILES = [
    ROOT / "scripts" / "bootstrap" / "private-first-deploy.sh",
    ROOT / "scripts" / "bootstrap" / "seed-first-deploy.sh",
    ROOT / "scripts" / "bootstrap" / "sync-seed-git.sh",
]
ALLOWED_EXTRA_SCRIPTS = {
    Path(".github/workflows/validate.yml"): {
        "scripts/verify_active_kyverno_policies.py",
        "scripts/verify_supply_chain_evidence.py",
    },
    Path("scripts/bootstrap/private-first-deploy.sh"): {"scripts/render_private_platform_values.py"},
    Path("scripts/bootstrap/seed-first-deploy.sh"): {"scripts/render_private_platform_values.py"},
    Path("scripts/bootstrap/sync-seed-git.sh"): {"scripts/render_private_platform_values.py"},
}


def unique_in_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def make_validate_block(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == "validate:":
            block: list[str] = []
            for next_line in lines[index + 1 :]:
                if next_line and not next_line.startswith("\t"):
                    break
                block.append(next_line)
            return "\n".join(block)
    raise AssertionError("Makefile is missing validate target")


def make_validate_scripts() -> list[str]:
    block = make_validate_block(MAKEFILE.read_text(encoding="utf-8"))
    scripts = unique_in_order(VALIDATION_SCRIPT_RE.findall(block))
    if scripts != ["scripts/run_validation.py"]:
        raise AssertionError("Makefile validate target must call only scripts/run_validation.py")
    return scripts


def runner_validation_scripts() -> list[str]:
    text = RUNNER.read_text(encoding="utf-8")
    match = re.search(r"VALIDATION_SCRIPTS\s*=\s*\((?P<body>.*?)\)\s*\n", text, re.S)
    if not match:
        raise AssertionError("scripts/run_validation.py is missing VALIDATION_SCRIPTS")
    scripts = VALIDATION_SCRIPT_RE.findall(match.group("body"))
    scripts = unique_in_order(scripts)
    if not scripts:
        raise AssertionError("scripts/run_validation.py does not list validation scripts")
    if "scripts/run_validation.py" in scripts:
        raise AssertionError("scripts/run_validation.py must not recursively list itself")
    for script in scripts:
        if not (ROOT / script).exists():
            raise AssertionError(f"scripts/run_validation.py references missing script: {script}")
    return scripts


def surface_scripts(path: Path) -> list[str]:
    return unique_in_order(VALIDATION_SCRIPT_RE.findall(path.read_text(encoding="utf-8")))


def check_ci_surface(path: Path, expected: list[str]) -> list[str]:
    rel_path = path.relative_to(ROOT)
    actual = surface_scripts(path)
    problems: list[str] = []
    allowed_extras = ALLOWED_EXTRA_SCRIPTS.get(rel_path, set())
    unexpected = [script for script in actual if script not in expected and script not in allowed_extras]
    if unexpected:
        problems.append(f"{rel_path} runs unexpected Python script(s): {', '.join(unexpected)}")

    missing = [script for script in expected if script not in actual]
    if missing:
        problems.append(f"{rel_path} is missing validation script(s): {', '.join(missing)}")
        return problems

    positions = [actual.index(script) for script in expected]
    if positions != sorted(positions):
        problems.append(f"{rel_path} runs validation scripts out of make validate order")
    unused_allowed = sorted(allowed_extras.difference(actual))
    if unused_allowed:
        problems.append(f"{rel_path} allowlist contains unused script(s): {', '.join(unused_allowed)}")
    return problems


def check_runner_surface(path: Path) -> list[str]:
    rel_path = path.relative_to(ROOT)
    actual = surface_scripts(path)
    allowed_scripts = {"scripts/run_validation.py"} | ALLOWED_EXTRA_SCRIPTS.get(rel_path, set())
    problems: list[str] = []
    if "scripts/run_validation.py" not in actual:
        problems.append(f"{rel_path} must call scripts/run_validation.py")
    unexpected = [script for script in actual if script not in allowed_scripts]
    if unexpected:
        problems.append(f"{rel_path} runs validation script(s) outside the shared runner: {', '.join(unexpected)}")
    unused_allowed = sorted(ALLOWED_EXTRA_SCRIPTS.get(rel_path, set()).difference(actual))
    if unused_allowed:
        problems.append(f"{rel_path} allowlist contains unused script(s): {', '.join(unused_allowed)}")
    return problems


def main() -> int:
    make_validate_scripts()
    expected = runner_validation_scripts()
    problems: list[str] = []
    for path in CI_SURFACE_FILES + RUNNER_SURFACE_FILES:
        if not path.exists():
            problems.append(f"validation surface is missing: {path.relative_to(ROOT)}")
            continue
        if path in CI_SURFACE_FILES:
            problems.extend(check_ci_surface(path, expected))
        else:
            problems.extend(check_runner_surface(path))

    if problems:
        print("Validation surface parity failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(f"Validation surface parity passed for {len(CI_SURFACE_FILES) + len(RUNNER_SURFACE_FILES)} surfaces.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
