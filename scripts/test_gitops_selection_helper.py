#!/usr/bin/env python3
"""Self-test the GitOps profile selection bootstrap helper."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/bootstrap/validate-gitops-selection.sh"


def bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix().split(":", 1)[1].lstrip("/")
    return f"/mnt/{drive}/{rest}"


def run_helper(profile: str, mode: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        raise AssertionError("bash is required for GitOps selection helper validation")

    command = " ".join(
        [
            "cd",
            shlex.quote(bash_path(ROOT)),
            "&&",
            f"PLATFORM_PROFILE={shlex.quote(profile)}",
            f"PLATFORM_GITOPS_PLACEHOLDER_MODE={shlex.quote(mode)}",
            "PLATFORM_REPO_URL=git://selection.example/platform-gitops.git",
            f"PYTHON={shlex.quote(bash_path(Path(sys.executable)))}",
            "PYTHONDONTWRITEBYTECODE=1",
            "bash",
            "scripts/bootstrap/validate-gitops-selection.sh",
            ".",
        ]
    )
    return subprocess.run(
        [bash, "-lc", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def assert_rc(
    result: subprocess.CompletedProcess[str],
    expected: int,
    description: str,
) -> str:
    combined = output(result)
    if result.returncode != expected:
        raise AssertionError(
            f"expected {description} to exit {expected}, got {result.returncode}\n{combined}"
        )
    return combined


def assert_contains(text: str, needle: str, description: str) -> None:
    if needle not in text:
        raise AssertionError(f"expected {description} to contain {needle!r}\n{text}")


def main() -> int:
    existing_rendered = set(ROOT.glob(".platform-gitops-selection-*.yaml"))

    strict = run_helper("premium-3node", "strict")
    strict_output = assert_rc(strict, 1, "premium strict template profile")
    assert_contains(strict_output, "unresolved placeholders", "strict failure")
    assert_contains(
        strict_output,
        "do not use skip-incomplete output as production proof",
        "strict failure",
    )

    for profile in ("default", "premium-3node"):
        result = run_helper(profile, "skip-incomplete")
        rendered_output = assert_rc(result, 0, f"{profile} skip-incomplete selection")
        assert_contains(rendered_output, "Deployable GitOps applications:", f"{profile} selection")
        assert_contains(rendered_output, "Skipped incomplete GitOps applications:", f"{profile} selection")

    bad_profile = run_helper("unknown-profile", "skip-incomplete")
    bad_profile_output = assert_rc(bad_profile, 1, "unsupported profile")
    assert_contains(bad_profile_output, "Unsupported PLATFORM_PROFILE=unknown-profile", "bad profile")

    bad_mode = run_helper("premium-3node", "surprise")
    bad_mode_output = assert_rc(bad_mode, 1, "unsupported placeholder mode")
    assert_contains(
        bad_mode_output,
        "Unsupported PLATFORM_GITOPS_PLACEHOLDER_MODE=surprise",
        "bad placeholder mode",
    )

    leaked_rendered = set(ROOT.glob(".platform-gitops-selection-*.yaml")) - existing_rendered
    if leaked_rendered:
        leaked = ", ".join(sorted(path.name for path in leaked_rendered))
        raise AssertionError(f"GitOps selection helper left temporary rendered files: {leaked}")

    print("GitOps selection helper self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
