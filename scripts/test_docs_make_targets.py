#!/usr/bin/env python3
"""Verify documented `make` commands reference real Makefile targets."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
DOC_ROOT = ROOT / "docs"
DOC_EXCLUDED_PARTS = {"i18n"}

TARGET_LINE_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*(?:\s+[A-Za-z0-9][A-Za-z0-9_.-]*)*)\s*:"
)
MAKE_LINE_RE = re.compile(r"(?<![\w./-])make\s+(?P<args>.*)$")
TARGET_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
STOP_WORDS = {"&&", "||", "|", ";", "\\"}


def parse_make_targets(makefile: Path = MAKEFILE) -> set[str]:
    targets: set[str] = set()
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            continue
        match = TARGET_LINE_RE.match(line)
        if match:
            targets.update(match.group(1).split())
    return targets


def iter_markdown_docs() -> list[Path]:
    paths = [ROOT / "README.md"]
    if DOC_ROOT.exists():
        paths.extend(
            sorted(
                path
                for path in DOC_ROOT.rglob("*.md")
                if not (DOC_EXCLUDED_PARTS & set(path.relative_to(ROOT).parts))
            )
        )
    return [path for path in paths if path.exists()]


def iter_make_snippets(path: Path):
    in_fence = False
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        if in_fence and "make " in line:
            yield line_no, line

        for inline in re.findall(r"`([^`]*\bmake\b[^`]*)`", line):
            yield line_no, inline


def candidates_from_snippet(snippet: str) -> list[str]:
    match = MAKE_LINE_RE.search(snippet.replace("`", " "))
    if not match:
        return []

    args = match.group("args").split("#", 1)[0]
    try:
        tokens = shlex.split(args, comments=False, posix=True)
    except ValueError:
        tokens = args.split()

    candidates: list[str] = []
    for raw_word in tokens:
        word = raw_word.strip().strip("`'\".,:)")
        if not word:
            continue
        if word in STOP_WORDS:
            break
        if word.endswith("\\"):
            word = word[:-1]
        if not word or word in STOP_WORDS:
            break
        if word.startswith("-") or "=" in word:
            continue
        if word.startswith("$") or word.startswith("<") or word.startswith("{"):
            continue
        if "/" in word:
            continue
        if TARGET_WORD_RE.fullmatch(word):
            candidates.append(word)
    return candidates


def main() -> int:
    targets = parse_make_targets()
    if not targets:
        print("No Makefile targets were parsed.", file=sys.stderr)
        return 1

    problems: list[str] = []
    checked = 0
    for path in iter_markdown_docs():
        for line_no, snippet in iter_make_snippets(path):
            for target in candidates_from_snippet(snippet):
                checked += 1
                if target not in targets:
                    rel = path.relative_to(ROOT)
                    problems.append(
                        f"{rel}:{line_no}: unknown make target `{target}` in `{snippet.strip()}`"
                    )

    if problems:
        print("Documented make target validation failed:", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1

    print(f"Documented make target validation passed for {checked} references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
