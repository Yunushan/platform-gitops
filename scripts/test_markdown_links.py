#!/usr/bin/env python3
"""Verify local Markdown documentation links point to real repository paths."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOC_ROOT = ROOT / "docs"
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_HREF_RE = re.compile(r"""href=["'](?P<target>[^"']+)["']""")
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel"}


def docs() -> list[Path]:
    paths = [ROOT / "README.md"]
    if DOC_ROOT.exists():
        paths.extend(sorted(DOC_ROOT.rglob("*.md")))
    return [path for path in paths if path.exists()]


def local_link_targets(text: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for pattern_name, pattern in (("markdown", MARKDOWN_LINK_RE), ("html", HTML_HREF_RE)):
        for match in pattern.finditer(text):
            raw_target = match.group("target").strip()
            if raw_target:
                targets.append((pattern_name, raw_target))
    return targets


def is_external_or_anchor(target: str) -> bool:
    parsed = urlsplit(target)
    return target.startswith("#") or parsed.scheme in EXTERNAL_SCHEMES


def resolve_local_target(source: Path, target: str) -> Path:
    path_part = urlsplit(target).path
    decoded = unquote(path_part)
    if decoded.startswith("/"):
        return (ROOT / decoded.lstrip("/")).resolve()
    return (source.parent / decoded).resolve()


def check_doc(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for kind, target in local_link_targets(text):
        if is_external_or_anchor(target):
            continue
        resolved = resolve_local_target(path, target)
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            problems.append(f"{path.relative_to(ROOT)} links outside the repository via {kind} link: {target}")
            continue
        if not resolved.exists():
            problems.append(f"{path.relative_to(ROOT)} has missing {kind} link target: {target}")
    return problems


def main() -> int:
    problems: list[str] = []
    for path in docs():
        problems.extend(check_doc(path))

    if problems:
        print("Markdown link validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(f"Markdown link validation passed for {len(docs())} docs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
