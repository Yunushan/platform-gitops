#!/usr/bin/env python3
"""Behavior-test private atomic artifact writes."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from unittest import mock

from atomic_file import PRIVATE_FILE_MODE, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
ATOMIC_ARTIFACT_PRODUCERS = (
    "scripts/capture_live_image_inventory.py",
    "scripts/configure_github_governance.py",
    "scripts/forge_cutover.py",
    "scripts/forge_migration.py",
    "scripts/forge_migration_live.py",
    "scripts/forge_transition.py",
    "scripts/reconcile_image_inventory.py",
    "scripts/verify_github_governance.py",
    "scripts/verify_github_release_approval.py",
    "scripts/verify_github_release_ref.py",
    "scripts/verify_production_readiness_score.py",
)


def assert_no_temporary_files(directory: Path, destination: Path) -> None:
    leftovers = list(directory.glob(f".{destination.name}.*.tmp"))
    if leftovers:
        raise AssertionError(f"atomic writer left temporary files behind: {leftovers}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="platform-atomic-file-") as temporary_name:
        root = Path(temporary_name)
        destination = root / "nested" / "evidence.json"

        atomic_write_text(destination, '{"result":"passed"}\n')
        if destination.read_text(encoding="utf-8") != '{"result":"passed"}\n':
            raise AssertionError("atomic writer did not preserve exact text")
        if os.name == "posix" and stat.S_IMODE(destination.stat().st_mode) != PRIVATE_FILE_MODE:
            raise AssertionError("atomic writer did not apply owner-only permissions")
        assert_no_temporary_files(destination.parent, destination)

        destination.write_text("retained\n", encoding="utf-8")
        with mock.patch("atomic_file.os.replace", side_effect=OSError("simulated replace failure")):
            try:
                atomic_write_text(destination, "partial\n")
            except OSError as exc:
                if "simulated replace failure" not in str(exc):
                    raise
            else:
                raise AssertionError("atomic writer ignored a replacement failure")
        if destination.read_text(encoding="utf-8") != "retained\n":
            raise AssertionError("failed atomic write damaged the prior artifact")
        assert_no_temporary_files(destination.parent, destination)

    for relative_path in ATOMIC_ARTIFACT_PRODUCERS:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        if "from atomic_file import atomic_write_text" not in source:
            raise AssertionError(f"{relative_path} does not import the shared atomic writer")
        if "atomic_write_text(" not in source:
            raise AssertionError(f"{relative_path} does not use the shared atomic writer")

    production_runner = (
        ROOT / "scripts/bootstrap/run-platform-production-evidence.sh"
    ).read_text(encoding="utf-8")
    for control in (
        "umask 077",
        "from scripts.atomic_file import atomic_write_text",
        "atomic_write_text(",
    ):
        if control not in production_runner:
            raise AssertionError(
                "production evidence runner is missing private atomic output control: "
                f"{control}"
            )

    print("Private atomic artifact writer self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
