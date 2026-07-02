#!/usr/bin/env python3
"""Validate curated GitOps app image/chart references avoid mutable or implicit tags."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    ROOT / "gitops/clusters/rke2-main/apps",
    ROOT / "gitops/clusters/rke2-main/premium-3node/apps",
)
SKIP_PARTS = {"charts", "crds"}
YAML_SUFFIXES = {".yaml", ".yml"}
KEY_VALUE_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+):\s*(?P<value>.*?)\s*$")
MUTABLE_TAGS = {
    "latest",
    "main",
    "master",
    "dev",
    "devel",
    "develop",
    "development",
    "edge",
    "nightly",
    "next",
    "snapshot",
    "canary",
    "unstable",
}
MUTABLE_PREFIXES = (
    "latest-",
    "main-",
    "master-",
    "dev-",
    "devel-",
    "develop-",
    "edge-",
    "nightly-",
    "next-",
    "snapshot-",
    "canary-",
    "unstable-",
)


def strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    result: list[str] = []
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and in_double:
            result.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result).strip()


def strip_quotes(value: str) -> str:
    value = strip_inline_comment(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def is_mutable(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in MUTABLE_TAGS or normalized.startswith(MUTABLE_PREFIXES)


def explicit_image_tag(image_ref: str) -> str:
    if "@sha256:" in image_ref:
        return ""
    last_segment = image_ref.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return "<missing>"
    return last_segment.rsplit(":", 1)[-1]


def repository_has_explicit_pin(repository: str) -> bool:
    return "@sha256:" in repository or explicit_image_tag(repository) not in {"", "<missing>"}


def should_scan(path: Path) -> bool:
    if path.suffix not in YAML_SUFFIXES:
        return False
    return not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)


def finish_image_block(path: Path, problems: list[str], image_block: dict[str, object]) -> None:
    repository = str(image_block.get("repository", ""))
    if not repository:
        return
    repository_tag = explicit_image_tag(repository)
    if repository_tag and repository_tag != "<missing>" and is_mutable(repository_tag):
        problems.append(
            f"{path.relative_to(ROOT)}:{image_block['repository_line']}: image repository embeds mutable tag: {repository}"
        )
    tag = image_block.get("tag")
    if (tag is None or tag == "") and not repository_has_explicit_pin(repository):
        problems.append(
            f"{path.relative_to(ROOT)}:{image_block['line']}: image block with repository {repository} "
            "must set a non-empty tag or sha256 digest"
        )


def scan_file(path: Path) -> list[str]:
    problems: list[str] = []
    image_blocks: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = KEY_VALUE_RE.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        key = match.group("key")
        value = strip_quotes(match.group("value"))

        while image_blocks and indent <= int(image_blocks[-1]["indent"]):
            finish_image_block(path, problems, image_blocks.pop())

        if image_blocks and indent > int(image_blocks[-1]["indent"]):
            if key == "repository" and value not in {"|", ">"}:
                image_blocks[-1]["repository"] = value
                image_blocks[-1]["repository_line"] = line_number
            elif key == "tag" and value not in {"|", ">"}:
                image_blocks[-1]["tag"] = value
                image_blocks[-1]["tag_line"] = line_number

        if key == "image" and not value:
            image_blocks.append({"indent": indent, "line": line_number, "repository": "", "tag": None})
            continue
        if not value or value in {"|", ">"}:
            continue

        if key == "image":
            tag = explicit_image_tag(value)
            if tag == "<missing>":
                problems.append(
                    f"{path.relative_to(ROOT)}:{line_number}: image reference must pin a tag or sha256 digest: {value}"
                )
            elif tag and is_mutable(tag):
                problems.append(
                    f"{path.relative_to(ROOT)}:{line_number}: image tag is mutable: {value}"
                )
        elif key == "tag" and is_mutable(value):
            problems.append(f"{path.relative_to(ROOT)}:{line_number}: image tag is mutable: {value}")
        elif key == "version" and is_mutable(value):
            problems.append(f"{path.relative_to(ROOT)}:{line_number}: chart version is mutable: {value}")
    while image_blocks:
        finish_image_block(path, problems, image_blocks.pop())
    return problems


def main() -> int:
    problems: list[str] = []
    scanned = 0
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*")):
            if path.is_file() and should_scan(path):
                scanned += 1
                problems.extend(scan_file(path))

    if problems:
        print("GitOps image pinning validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(f"GitOps image pinning validation passed for {scanned} curated YAML files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
