#!/usr/bin/env python3
"""Run the repository validation suite with one portable entrypoint."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_SCRIPTS = (
    "scripts/validate_project.py",
    "scripts/test_python_syntax.py",
    "scripts/test_validation_runner.py",
    "scripts/test_line_endings.py",
    "scripts/test_profile_checker.py",
    "scripts/test_deployable_renderer.py",
    "scripts/test_gitops_selection_helper.py",
    "scripts/test_private_values_renderer.py",
    "scripts/test_platform_secret_contract.py",
    "scripts/test_no_secrets.py",
    "scripts/test_private_artifact_boundary.py",
    "scripts/test_ci_reference_pinning.py",
    "scripts/test_shell_syntax.py",
    "scripts/test_shell_strict_mode.py",
    "scripts/test_ansible_shell_blocks.py",
    "scripts/test_ansible_curl_timeout_contract.py",
    "scripts/test_ansible_until_contract.py",
    "scripts/test_ansible_failed_when_contract.py",
    "scripts/test_ansible_no_log_contract.py",
    "scripts/test_docs_make_targets.py",
    "scripts/test_markdown_links.py",
    "scripts/test_example_templates.py",
    "scripts/test_ansible_playbook_references.py",
    "scripts/test_gitops_application_contract.py",
    "scripts/test_kustomization_references.py",
    "scripts/test_gitops_helm_chart_pinning.py",
    "scripts/test_gitops_image_pinning.py",
    "scripts/test_makefile_help.py",
    "scripts/test_validation_surface_parity.py",
    "scripts/validate_platform_contract.py",
    "scripts/validate_no_secrets.py",
)


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def selected_scripts(skip_no_secrets: bool) -> list[str]:
    scripts = list(VALIDATION_SCRIPTS)
    if skip_no_secrets:
        scripts = [script for script in scripts if script != "scripts/validate_no_secrets.py"]
    return scripts


def run_script(script: str) -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    print(f"== {script} ==", flush=True)
    return subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT, env=env).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repository validation scripts.")
    parser.add_argument("--list", action="store_true", help="Print validation scripts and exit.")
    parser.add_argument(
        "--skip-no-secrets",
        action="store_true",
        help="Skip the final public secret/privacy scan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skip_no_secrets = args.skip_no_secrets or not env_flag("PLATFORM_RUN_NO_SECRETS", True)
    scripts = selected_scripts(skip_no_secrets)
    if args.list:
        for script in scripts:
            print(script)
        return 0

    for script in scripts:
        returncode = run_script(script)
        if returncode != 0:
            return returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
