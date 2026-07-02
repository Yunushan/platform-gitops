#!/usr/bin/env python3
"""Keep Ansible and bootstrap curl probes bounded."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_DIRS = (
    ROOT / "ansible" / "playbooks",
    ROOT / "ansible" / "tasks",
)
SCRIPT_DIRS = (
    ROOT / "scripts",
)
CURL_COMMAND_RE = re.compile(r"(^|[\s\"'`$({;|&])curl(?:\.exe)?\s+(?P<after>\S*)")
SKIP_LINE_FRAGMENTS = (
    "command -v curl",
    "curl is not installed",
    "curl-not-installed",
    "no-curl-or",
    "grep -E",
    "net-misc/curl",
)
SKIP_LINE_PREFIXES = (
    "#",
    "- curl",
    "echo ",
    "printf ",
)


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for directory in ANSIBLE_DIRS:
        if directory.exists():
            files.extend(directory.glob("*.yml"))
    for directory in SCRIPT_DIRS:
        if directory.exists():
            files.extend(directory.rglob("*.sh"))
    return sorted(files)


def is_probable_curl_command(stripped: str) -> bool:
    if any(fragment in stripped for fragment in SKIP_LINE_FRAGMENTS):
        return False
    if stripped.startswith(SKIP_LINE_PREFIXES):
        return False
    match = CURL_COMMAND_RE.search(stripped)
    if not match:
        return False
    after = match.group("after")
    return after.startswith(("-", '"', "'", "$", "http://", "https://"))


def curl_block(lines: list[str], start: int) -> str:
    block: list[str] = []
    for index in range(start, min(len(lines), start + 12)):
        block.append(lines[index].strip())
        if index > start and not lines[index].rstrip().endswith("\\"):
            if index - start >= 2:
                break
    return " ".join(block)


def is_bounded(block: str) -> bool:
    if "--connect-timeout" in block and "--max-time" in block:
        return True
    return bool(re.search(r"\b(?:timeout|run_bounded)\b[^\n]*\bcurl(?:\.exe)?\b", block))


def find_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not is_probable_curl_command(stripped):
            continue
        block = curl_block(lines, index)
        if not is_bounded(block):
            problems.append(
                f"{rel_path(path)}:{index + 1} curl probe must set --connect-timeout and --max-time or use run_bounded/timeout"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    for path in scanned_files():
        problems.extend(find_problems(path))

    if problems:
        print("Ansible curl timeout contract validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("Ansible curl timeout contract validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
