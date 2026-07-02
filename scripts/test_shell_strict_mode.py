#!/usr/bin/env python3
"""Validate production shell scripts use Bash strict mode."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = ROOT / "scripts"
REQUIRED_SHEBANG = "#!/usr/bin/env bash"
REQUIRED_STRICT_MODE = "set -euo pipefail"


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def shell_scripts() -> list[Path]:
    return sorted(SCRIPT_ROOT.rglob("*.sh"))


def find_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []
    if not lines:
        return [f"{rel_path(path)}: shell script is empty"]
    if lines[0].strip() != REQUIRED_SHEBANG:
        problems.append(f"{rel_path(path)}:1 must use {REQUIRED_SHEBANG}")

    prologue = [
        line.strip()
        for line in lines[1:6]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if REQUIRED_STRICT_MODE not in prologue:
        problems.append(f"{rel_path(path)}: must set {REQUIRED_STRICT_MODE} in the script prologue")
    return problems


def main() -> int:
    problems: list[str] = []
    scripts = shell_scripts()
    for path in scripts:
        problems.extend(find_problems(path))

    if problems:
        print("Shell strict mode validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(f"Shell strict mode validation passed for {len(scripts)} scripts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
