#!/usr/bin/env python3
"""Check Ansible playbook references and basic playbook structure."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_DIR = ROOT / "ansible" / "playbooks"
PLAYBOOK_REF_RE = re.compile(r"ansible/playbooks/[A-Za-z0-9_.-]+\.ya?ml")
CONFLICT_MARKER_RE = re.compile(r"^(<<<<<<< .+|=======|>>>>>>> .+)$", re.MULTILINE)
TEXT_EXTENSIONS = {
    ".md",
    ".mk",
    ".sh",
    ".txt",
    ".yml",
    ".yaml",
}
REFERENCE_FILES = [
    ROOT / "Makefile",
    ROOT / "README.md",
    ROOT / ".github" / "workflows" / "validate.yml",
    ROOT / ".gitea" / "workflows" / "validate.yml",
    ROOT / ".forgejo" / "workflows" / "validate.yml",
    ROOT / ".gitlab-ci.yml",
    ROOT / ".woodpecker" / "validate.yml",
]
REFERENCE_DIRS = [
    ROOT / "docs",
    ROOT / "scripts",
    ROOT / "config",
]
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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def reference_sources() -> list[Path]:
    sources: set[Path] = {path for path in REFERENCE_FILES if path.exists()}
    for directory in REFERENCE_DIRS:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_dir() or should_skip(path):
                continue
            if path.suffix.lower() in TEXT_EXTENSIONS:
                sources.add(path)
    return sorted(sources)


def referenced_playbooks() -> dict[Path, list[tuple[Path, int]]]:
    references: dict[Path, list[tuple[Path, int]]] = {}
    for source in reference_sources():
        text = read_text(source)
        for line_no, line in enumerate(text.splitlines(), 1):
            for match in PLAYBOOK_REF_RE.finditer(line):
                rel = Path(match.group(0))
                references.setdefault(rel, []).append((source, line_no))
    return references


def check_references() -> list[str]:
    problems: list[str] = []
    for rel, locations in referenced_playbooks().items():
        if (ROOT / rel).exists():
            continue
        where = ", ".join(f"{path.relative_to(ROOT)}:{line_no}" for path, line_no in locations[:3])
        if len(locations) > 3:
            where += f", ... {len(locations) - 3} more"
        problems.append(f"referenced playbook is missing: {rel} ({where})")
    return problems


def check_playbook_structure() -> list[str]:
    problems: list[str] = []
    for path in sorted(PLAYBOOK_DIR.glob("*.yml")):
        text = read_text(path)
        rel = path.relative_to(ROOT)
        if not text.startswith("---\n"):
            problems.append(f"{rel} must start with YAML document marker ---")
        if "\t" in text:
            problems.append(f"{rel} contains tab indentation")
        if CONFLICT_MARKER_RE.search(text):
            problems.append(f"{rel} contains unresolved merge conflict markers")
        if "- name:" not in text:
            problems.append(f"{rel} does not contain a named play")
        if "hosts:" not in text:
            problems.append(f"{rel} does not declare hosts")
    return problems


def main() -> int:
    problems = check_references() + check_playbook_structure()
    if problems:
        print("Ansible playbook reference validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    print("Ansible playbook reference validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
