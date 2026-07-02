#!/usr/bin/env python3
"""Keep suppressed Ansible failures diagnosable."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_DIRS = (
    ROOT / "ansible" / "playbooks",
    ROOT / "ansible" / "tasks",
)
TASK_RE = re.compile(r"^(?P<indent>\s*)-\s+name:\s*(?P<name>.+?)\s*$")
FAILED_WHEN_FALSE_RE = re.compile(r"^(?P<indent>\s*)failed_when:\s*false\s*(?:#.*)?$")
REGISTER_RE = re.compile(r"^\s*register:\s*\S+\s*(?:#.*)?$", re.M)
DIAGNOSTIC_ACTION_RE = re.compile(
    r"^\s*(?:ansible\.builtin\.)?(debug|fail|set_fact|assert|meta):",
    re.M,
)
DIAGNOSTIC_NAME_PREFIXES = (
    "Print ",
    "Show ",
    "Continue ",
)


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
    failed_when_indent: int,
) -> tuple[int, int, str] | None:
    candidates = [
        task for task in tasks if task[0] < line_index and task[1] < failed_when_indent
    ]
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


def is_diagnosable(task_name: str, block: str) -> bool:
    if REGISTER_RE.search(block):
        return True
    if DIAGNOSTIC_ACTION_RE.search(block):
        return True
    return task_name.startswith(DIAGNOSTIC_NAME_PREFIXES)


def find_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    tasks = named_tasks(lines)
    problems: list[str] = []
    for index, line in enumerate(lines):
        match = FAILED_WHEN_FALSE_RE.match(line)
        if not match:
            continue
        task = containing_task(tasks, index, len(match.group("indent")))
        if task is None:
            problems.append(f"{rel_path(path)}:{index + 1} failed_when false has no named task")
            continue
        _, _, task_name = task
        block = task_block(lines, tasks, task)
        if not is_diagnosable(task_name, block):
            problems.append(
                f"{rel_path(path)}:{index + 1} task must register suppressed failure result: {task_name}"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    for path in ansible_files():
        problems.extend(find_problems(path))

    if problems:
        print("Ansible failed_when contract validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("Ansible failed_when contract validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
