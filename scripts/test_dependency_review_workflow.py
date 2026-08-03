#!/usr/bin/env python3
"""Validate the read-only GitHub dependency-diff gate."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/dependency-review.yml"
ACTION_SHA_RE = re.compile(r"uses:\s*[^\s@]+@[0-9a-f]{40}(?:\s|$)")


def require(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"dependency review workflow is missing {needle!r}")


def main() -> int:
    if not WORKFLOW.is_file():
        raise AssertionError("dependency review workflow is missing")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "name: dependency-review",
        "  pull_request:",
        "permissions:\n  contents: read",
        "runs-on: ubuntu-24.04",
        "persist-credentials: false",
        "actions/dependency-review-action@",
        "# v5.0.0",
        "fail-on-severity: high",
        "fail-on-scopes: runtime, development",
        "license-check: true",
        "vulnerability-check: true",
        "comment-summary-in-pr: never",
        "show-openssf-scorecard: true",
    ):
        require(workflow, needle)

    if "pull-requests: write" in workflow or "contents: write" in workflow:
        raise AssertionError("dependency review must remain read-only")
    if "warn-only: true" in workflow:
        raise AssertionError("dependency review must fail closed for configured severities")
    for line in workflow.splitlines():
        if "uses:" in line and not ACTION_SHA_RE.search(line.strip()):
            raise AssertionError(f"dependency review action is not SHA-pinned: {line.strip()}")

    print("GitHub dependency review workflow contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
