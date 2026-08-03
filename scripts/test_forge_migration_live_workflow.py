#!/usr/bin/env python3
"""Verify the opt-in CI workflow for live forge migration evidence."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "live-forge-migration-acceptance.yml"


def require(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"live migration workflow is missing {expected!r}")


def main() -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    for expected in (
        "workflow_dispatch:",
        "concurrency:\n  group: live-forge-migration-acceptance\n  cancel-in-progress: false",
        "environment: forge-migration-live-acceptance",
        "timeout-minutes: 120",
        "persist-credentials: false",
        "FORGE_MIGRATION_LIVE: '1'",
        "FORGE_MIGRATION_LIVE_GITHUB_NAMESPACE: ${{ secrets.FORGE_MIGRATION_LIVE_GITHUB_NAMESPACE }}",
        "FORGE_MIGRATION_LIVE_GITLAB_NAMESPACE: ${{ secrets.FORGE_MIGRATION_LIVE_GITLAB_NAMESPACE }}",
        "FORGE_MIGRATION_LIVE_FORGEJO_API_URL: ${{ secrets.FORGE_MIGRATION_LIVE_FORGEJO_API_URL }}",
        "FORGE_MIGRATION_LIVE_FORGEJO_NAMESPACE: ${{ secrets.FORGE_MIGRATION_LIVE_FORGEJO_NAMESPACE }}",
        "GITHUB_TOKEN: ${{ secrets.FORGE_MIGRATION_LIVE_GITHUB_TOKEN }}",
        "GITLAB_TOKEN: ${{ secrets.FORGE_MIGRATION_LIVE_GITLAB_TOKEN }}",
        "FORGEJO_TOKEN: ${{ secrets.FORGE_MIGRATION_LIVE_FORGEJO_TOKEN }}",
        "run: make forge-migration-live-run",
        "actions/upload-artifact@",
        "retention-days: 90",
    ):
        require(text, expected)
    if "on:\n  push:" in text or "on:\n  pull_request:" in text:
        raise AssertionError("live migration acceptance must never run on push or pull_request")
    print("Live forge migration workflow contract test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
