#!/usr/bin/env python3
"""Validate repository text files follow the declared LF line-ending policy."""

from __future__ import annotations

import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LF_PATTERNS = (
    ".gitattributes",
    ".gitignore",
    ".helmignore",
    ".gitkeep",
    "LICENSE",
    "Makefile",
    "Dockerfile",
    "*.env",
    "*.env.example",
    "*.json",
    "*.lock",
    "*.gotmpl",
    "*.tpl",
    "*.txt",
    "*.sh",
    "*.py",
    "*.yml",
    "*.yaml",
    "*.ini",
    "*.cfg",
    "*.md",
)
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "rendered",
}
ALLOWED_PRIVATE_FILES = {
    Path("private/README.md"),
    Path("private/.gitkeep"),
    Path("secrets/README.md"),
    Path("secrets/.gitkeep"),
}


def git_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts.intersection(SKIP_PARTS):
        return True
    if path.parts and path.parts[0] in {"private", "secrets"}:
        return path not in ALLOWED_PRIVATE_FILES
    return False


def requires_lf(path: Path) -> bool:
    posix = path.as_posix()
    name = path.name
    return any(posix == pattern or fnmatch(name, pattern) for pattern in LF_PATTERNS)


def has_cr_line_endings(path: Path) -> bool:
    data = (ROOT / path).read_bytes()
    return b"\r\n" in data or b"\r" in data


def main() -> int:
    offenders = [
        path
        for path in git_files()
        if requires_lf(path) and not should_skip(path) and (ROOT / path).is_file() and has_cr_line_endings(path)
    ]
    if offenders:
        print("CRLF or CR line endings found in files covered by .gitattributes:")
        for path in offenders:
            print(f" - {path.as_posix()}")
        return 1
    print("Line ending validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
