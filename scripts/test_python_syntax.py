#!/usr/bin/env python3
"""Parse every repository Python file without importing it."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {
    ".git",
    ".cache",
    ".pytest_cache",
    ".terraform",
    ".venv",
    "__pycache__",
    "build",
    "charts",
    "dist",
    "private",
    "rendered",
    "secrets",
}


def should_skip(name: str) -> bool:
    return name in EXCLUDE_DIRS or name.startswith(".shell-syntax-")


def python_files() -> list[Path]:
    paths = []
    for directory, directories, files in os.walk(ROOT):
        directories[:] = [name for name in directories if not should_skip(name)]
        parent = Path(directory)
        for name in files:
            path = parent / name
            if not should_skip(name) and path.match("*.py") and path.is_file():
                paths.append(path.relative_to(ROOT))
    return sorted(paths)


def main() -> int:
    failures: list[tuple[Path, str]] = []
    for rel in python_files():
        path = ROOT / rel
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=rel.as_posix())
        except SyntaxError as exc:
            location = f"{exc.lineno}:{exc.offset}" if exc.lineno else "unknown"
            failures.append((rel, f"{location}: {exc.msg}"))

    if failures:
        print("Python syntax validation failed:")
        for rel, detail in failures:
            print(f" - {rel}: {detail}")
        return 1

    print(f"Python syntax validation passed for {len(python_files())} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
