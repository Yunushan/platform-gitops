#!/usr/bin/env python3
"""Ensure private/generated artifact directories do not contain tracked payloads."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = ("private", "rendered", "secrets")
ALLOWED_TRACKED_PATHS = {
    "private/.gitkeep",
    "private/README.md",
    "secrets/.gitkeep",
    "secrets/README.md",
}
REQUIRED_GITIGNORE_RULES = {
    "private/*",
    "!private/README.md",
    "!private/.gitkeep",
    "rendered/",
    "secrets/*",
    "!secrets/README.md",
    "!secrets/.gitkeep",
}


def git_available() -> bool:
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def tracked_private_paths() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *PRIVATE_DIRS],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or "git ls-files failed")
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def check_gitignore_rules() -> list[str]:
    lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return sorted(REQUIRED_GITIGNORE_RULES.difference(lines))


def main() -> int:
    missing_rules = check_gitignore_rules()
    problems: list[str] = []
    if missing_rules:
        problems.append(".gitignore is missing private artifact boundary rule(s): " + ", ".join(missing_rules))

    if git_available():
        disallowed = sorted(set(tracked_private_paths()).difference(ALLOWED_TRACKED_PATHS))
        if disallowed:
            problems.append(
                "Disallowed tracked private artifact path(s): "
                + ", ".join(disallowed)
                + ". Only README.md and .gitkeep may be tracked under private/ or secrets/, "
                + "and rendered/ must stay untracked."
            )
    else:
        print("Git is not available; skipped tracked private artifact path check.")

    if problems:
        print("Private artifact boundary validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("Private artifact boundary validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
