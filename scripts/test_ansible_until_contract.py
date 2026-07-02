#!/usr/bin/env python3
"""Keep Ansible retry loops bounded and intentional."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_DIRS = (
    ROOT / "ansible" / "playbooks",
    ROOT / "ansible" / "tasks",
)
TASK_RE = re.compile(r"^(?P<indent>\s*)-\s+name:\s*(?P<name>.+?)\s*$")
UNTIL_RE = re.compile(r"^(?P<indent>\s*)until:\s*")
RETRIES_RE = re.compile(r"^\s*retries:\s*\S+", re.M)
DELAY_RE = re.compile(r"^\s*delay:\s*\S+", re.M)


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def clean_task_name(raw: str) -> str:
    return raw.strip().strip("'\"")


def ansible_files() -> list[Path]:
    files: list[Path] = []
    for directory in ANSIBLE_DIRS:
        if directory.exists():
            files.extend(directory.glob("*.yml"))
    return sorted(files)


def named_tasks(lines: list[str]) -> list[tuple[int, int, str]]:
    tasks: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = TASK_RE.match(line)
        if match:
            tasks.append((index, len(match.group("indent")), clean_task_name(match.group("name"))))
    return tasks


def containing_task(
    tasks: list[tuple[int, int, str]],
    line_index: int,
    until_indent: int,
) -> tuple[int, int, str] | None:
    candidates = [task for task in tasks if task[0] < line_index and task[1] < until_indent]
    if not candidates:
        return None
    return candidates[-1]


def task_block(lines: list[str], tasks: list[tuple[int, int, str]], task: tuple[int, int, str]) -> str:
    start, task_indent, _ = task
    end = len(lines)
    for index, indent, _ in tasks:
        if index > start and indent <= task_indent:
            end = index
            break
    return "\n".join(lines[start:end])


def find_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    tasks = named_tasks(lines)
    problems: list[str] = []
    for index, line in enumerate(lines):
        match = UNTIL_RE.match(line)
        if not match:
            continue
        task = containing_task(tasks, index, len(match.group("indent")))
        if task is None:
            problems.append(f"{rel_path(path)}:{index + 1} until loop has no named task")
            continue
        _, _, task_name = task
        block = task_block(lines, tasks, task)
        missing: list[str] = []
        if not RETRIES_RE.search(block):
            missing.append("retries")
        if not DELAY_RE.search(block):
            missing.append("delay")
        if missing:
            problems.append(
                f"{rel_path(path)}:{index + 1} until loop must set {', '.join(missing)}: {task_name}"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    for path in ansible_files():
        problems.extend(find_problems(path))

    if problems:
        print("Ansible until contract validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("Ansible until contract validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
