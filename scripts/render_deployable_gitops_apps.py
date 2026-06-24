#!/usr/bin/env python3
"""Render an Argo CD Application list, skipping apps with placeholders."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
VENDORED_PATH_PARTS = {"charts", "crds"}


def unresolved_in_text(text: str) -> list[str]:
    return [match for match in PLACEHOLDER_RE.findall(text) if match != "<THIS_REPO_URL>"]


def display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def scan_path(path: Path, repo_root: Path) -> list[str]:
    findings: list[str] = []
    if not path.exists():
        return [f"{path}: missing application path"]

    files = [path] if path.is_file() else sorted(path.rglob("*"))
    for file_path in files:
        if not file_path.is_file():
            continue
        rel_parts = set(file_path.relative_to(path if path.is_dir() else path.parent).parts)
        if rel_parts & VENDORED_PATH_PARTS:
            continue
        if file_path.suffix not in {".yaml", ".yml"}:
            continue
        if file_path.name.endswith(".example.yaml") or file_path.name.endswith(".example.yml"):
            continue

        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(lines, start=1):
            if unresolved_in_text(line):
                findings.append(
                    f"{display_path(file_path, repo_root)}:{line_number}: {line.strip()}"
                )
    return findings


def render(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    applications_file = args.applications_file.resolve()
    raw = applications_file.read_text(encoding="utf-8")
    documents = [doc.strip() for doc in re.split(r"(?m)^---\s*$", raw) if doc.strip()]
    kept: list[str] = []
    skipped: list[tuple[str, list[str]]] = []

    for doc in documents:
        name_match = re.search(r"(?m)^  name:\s*([^\s]+)\s*$", doc)
        path_match = re.search(r"(?m)^    path:\s*([^\s]+)\s*$", doc)
        name = name_match.group(1) if name_match else "unknown"

        doc_findings = unresolved_in_text(doc)
        if path_match:
            path_findings = scan_path(repo_root / path_match.group(1), repo_root)
        else:
            path_findings = ["application source path was not found"]

        findings = doc_findings + path_findings
        if findings:
            skipped.append((name, findings))
        else:
            kept.append(doc.replace("<THIS_REPO_URL>", args.repo_url))

    if not kept:
        print(
            "No deployable GitOps applications remain after skipping incomplete apps.",
            file=sys.stderr,
        )
        for name, findings in skipped:
            print(f"- {name}", file=sys.stderr)
            for finding in findings[:8]:
                print(f"  {finding}", file=sys.stderr)
        return 2

    args.output.write_text("---\n" + "\n---\n".join(kept) + "\n", encoding="utf-8")

    print("Deployable GitOps applications:")
    for doc in kept:
        name_match = re.search(r"(?m)^  name:\s*([^\s]+)\s*$", doc)
        print(f"- {name_match.group(1) if name_match else 'unknown'}")

    if skipped:
        print()
        print("Skipped incomplete GitOps applications:")
        for name, findings in skipped:
            print(f"- {name}: {len(findings)} unresolved placeholder finding(s)")
            for finding in findings[:5]:
                print(f"  {finding}")
            if len(findings) > 5:
                print(f"  ... {len(findings) - 5} more")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--applications-file", type=Path, required=True)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return render(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
