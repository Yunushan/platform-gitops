#!/usr/bin/env python3
"""Validate static local references inside repo-owned Kustomization files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GITOPS_ROOT = ROOT / "gitops"
KUSTOMIZATION_NAMES = {"kustomization.yaml", "kustomization.yml", "Kustomization"}
LOCAL_PATH_SECTIONS = {
    "resources",
    "components",
    "patchesStrategicMerge",
}
TOP_LEVEL_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z][A-Za-z0-9_-]*):\s*(?:#.*)?$")
LIST_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s*(?P<value>[^#]+?)(?:\s+#.*)?$")
VALUES_FILE_RE = re.compile(r"(?m)^\s+valuesFile:\s*(?P<value>[^#\s]+)")
PATCH_PATH_RE = re.compile(r"(?m)^\s+path:\s*(?P<value>[^#\s]+)")


def strip_scalar(value: str) -> str:
    return value.strip().strip("'\"")


def is_static_local_ref(value: str) -> bool:
    return bool(value) and "{{" not in value and "}}" not in value and "://" not in value and not value.startswith("/")


def kustomization_files() -> list[Path]:
    return sorted(
        path
        for path in GITOPS_ROOT.rglob("*")
        if path.is_file() and path.name in KUSTOMIZATION_NAMES and "charts" not in path.parts
    )


def list_items_for_section(text: str, section: str) -> list[str]:
    lines = text.splitlines()
    items: list[str] = []
    for index, line in enumerate(lines):
        match = TOP_LEVEL_KEY_RE.match(line)
        if not match or match.group("key") != section:
            continue
        section_indent = len(match.group("indent"))
        for item_line in lines[index + 1 :]:
            if not item_line.strip():
                continue
            item_indent = len(item_line) - len(item_line.lstrip())
            if item_indent <= section_indent:
                break
            item_match = LIST_ITEM_RE.match(item_line)
            if item_match:
                items.append(strip_scalar(item_match.group("value")))
        break
    return items


def referenced_values_files(text: str) -> list[str]:
    return [strip_scalar(match.group("value")) for match in VALUES_FILE_RE.finditer(text)]


def referenced_patch_paths(text: str) -> list[str]:
    if "patches:" not in text:
        return []
    return [strip_scalar(match.group("value")) for match in PATCH_PATH_RE.finditer(text)]


def check_existing_ref(kustomization: Path, raw_ref: str, description: str) -> list[str]:
    if not is_static_local_ref(raw_ref):
        return []
    target = (kustomization.parent / raw_ref).resolve()
    rel = kustomization.relative_to(ROOT)
    problems: list[str] = []
    if not target.exists():
        problems.append(f"{rel} references missing {description}: {raw_ref}")
        return problems
    if target.is_dir() and not any((target / name).exists() for name in KUSTOMIZATION_NAMES):
        problems.append(f"{rel} references {description} directory without kustomization: {raw_ref}")
    return problems


def check_kustomization(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for section in LOCAL_PATH_SECTIONS:
        for item in list_items_for_section(text, section):
            problems.extend(check_existing_ref(path, item, section))
    for values_file in referenced_values_files(text):
        problems.extend(check_existing_ref(path, values_file, "Helm valuesFile"))
    for patch_path in referenced_patch_paths(text):
        problems.extend(check_existing_ref(path, patch_path, "patch path"))
    return problems


def main() -> int:
    files = kustomization_files()
    problems: list[str] = []
    if not files:
        problems.append("no GitOps Kustomization files were found")
    for path in files:
        problems.extend(check_kustomization(path))
    if problems:
        print("Kustomization reference validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    print(f"Kustomization reference validation passed for {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
