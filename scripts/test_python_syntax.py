#!/usr/bin/env python3
"""Parse every repository Python file without importing it."""

from __future__ import annotations

import ast
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


def should_skip(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS or part.startswith(".shell-syntax-") for part in path.parts)


def python_files() -> list[Path]:
    return sorted(
        path.relative_to(ROOT)
        for path in ROOT.rglob("*.py")
        if path.is_file() and not should_skip(path)
    )


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
