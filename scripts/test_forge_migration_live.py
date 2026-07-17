#!/usr/bin/env python3
"""Test the opt-in live forge migration acceptance runner without network access."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_migration_live as live


def sample_environment() -> dict[str, str]:
    return {
        "FORGE_MIGRATION_LIVE_GITHUB_NAMESPACE": "migration-bot",
        "FORGE_MIGRATION_LIVE_GITLAB_NAMESPACE": "migration-group",
        "FORGE_MIGRATION_LIVE_FORGEJO_API_URL": "https://forgejo.example.test/api/v1",
        "FORGE_MIGRATION_LIVE_FORGEJO_NAMESPACE": "migration-bot",
        "GITHUB_TOKEN": "github-secret-value",
        "GITLAB_TOKEN": "gitlab-secret-value",
        "FORGEJO_TOKEN": "forgejo-secret-value",
    }


def test_manifest_covers_every_supported_direction() -> None:
    environment = sample_environment()
    configs = live.load_configuration(environment)
    live.validate_configuration(configs, environment, require_tokens=True)
    manifest = live.dry_run_manifest(configs, "platform-migration-live", "test-run", environment)
    directions = [entry["direction"] for entry in manifest["directions"]]
    if directions != list(live.SUPPORTED_DIRECTIONS):
        raise AssertionError(f"manifest directions are incomplete: {directions}")
    for entry in manifest["directions"]:
        if entry["metadata"] != live.PORTABLE_METADATA:
            raise AssertionError(f"{entry['direction']}: portable metadata contract drifted")
        if entry["wiki"] is not False or entry["lfs"] is not False:
            raise AssertionError(f"{entry['direction']}: live acceptance must declare its explicit wiki/LFS scope")
    rendered = json.dumps(manifest, sort_keys=True)
    for secret in ("github-secret-value", "gitlab-secret-value", "forgejo-secret-value"):
        if secret in rendered:
            raise AssertionError("live dry-run manifest leaked a provider token")


def test_provider_git_bases() -> None:
    configs = live.load_configuration(sample_environment())
    expected = {
        "github": "https://github.com",
        "gitlab": "https://gitlab.com",
        "forgejo": "https://forgejo.example.test",
    }
    actual = {name: live.git_base_url(config) for name, config in configs.items()}
    if actual != expected:
        raise AssertionError(f"provider Git base derivation changed: {actual}")


def test_live_run_requires_explicit_environment_guard() -> None:
    original = dict(live.os.environ)
    try:
        live.os.environ.clear()
        live.os.environ.update(sample_environment())
        with tempfile.TemporaryDirectory(prefix="forge-migration-live-test-") as temp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = live.main(["--run", "--output-dir", temp])
        if result != 1:
            raise AssertionError(f"live run without FORGE_MIGRATION_LIVE=1 returned {result}")
        if "FORGE_MIGRATION_LIVE=1" not in stderr.getvalue():
            raise AssertionError("live run guard did not explain how to explicitly enable provider access")
    finally:
        live.os.environ.clear()
        live.os.environ.update(original)


def main() -> int:
    test_manifest_covers_every_supported_direction()
    test_provider_git_bases()
    test_live_run_requires_explicit_environment_guard()
    print("Forge migration live acceptance contract test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
