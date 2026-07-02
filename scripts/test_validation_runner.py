#!/usr/bin/env python3
"""Self-test the portable repository validation runner."""

from __future__ import annotations

import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_validation  # noqa: E402


def fail(message: str) -> None:
    raise AssertionError(message)


def test_validation_script_list() -> None:
    scripts = list(run_validation.VALIDATION_SCRIPTS)
    if not scripts:
        fail("VALIDATION_SCRIPTS must not be empty")
    if len(scripts) != len(set(scripts)):
        fail("VALIDATION_SCRIPTS contains duplicates")
    if "scripts/run_validation.py" in scripts:
        fail("VALIDATION_SCRIPTS must not recursively include scripts/run_validation.py")
    for required in (
        "scripts/validate_project.py",
        "scripts/test_python_syntax.py",
        "scripts/test_validation_runner.py",
        "scripts/test_line_endings.py",
        "scripts/test_ci_reference_pinning.py",
        "scripts/test_shell_strict_mode.py",
        "scripts/test_ansible_shell_blocks.py",
        "scripts/test_ansible_curl_timeout_contract.py",
        "scripts/test_ansible_until_contract.py",
        "scripts/test_ansible_failed_when_contract.py",
        "scripts/test_ansible_no_log_contract.py",
        "scripts/test_gitops_helm_chart_pinning.py",
        "scripts/test_validation_surface_parity.py",
        "scripts/validate_platform_contract.py",
        "scripts/validate_no_secrets.py",
    ):
        if required not in scripts:
            fail(f"VALIDATION_SCRIPTS is missing {required}")
    for script in scripts:
        if not (ROOT / script).exists():
            fail(f"VALIDATION_SCRIPTS references missing script: {script}")
    if scripts[0] != "scripts/validate_project.py":
        fail("validate_project.py must run first")
    if scripts[-1] != "scripts/validate_no_secrets.py":
        fail("validate_no_secrets.py must run last")


def test_no_secrets_selection() -> None:
    full = run_validation.selected_scripts(skip_no_secrets=False)
    skipped = run_validation.selected_scripts(skip_no_secrets=True)
    if full != list(run_validation.VALIDATION_SCRIPTS):
        fail("selected_scripts(False) must return the full validation list")
    if "scripts/validate_no_secrets.py" in skipped:
        fail("selected_scripts(True) must skip validate_no_secrets.py")
    if skipped != [script for script in full if script != "scripts/validate_no_secrets.py"]:
        fail("selected_scripts(True) must skip only validate_no_secrets.py")


def test_env_flag() -> None:
    old_value = os.environ.get("PLATFORM_RUN_NO_SECRETS")
    try:
        os.environ.pop("PLATFORM_RUN_NO_SECRETS", None)
        if not run_validation.env_flag("PLATFORM_RUN_NO_SECRETS", True):
            fail("missing env var should use the default true value")
        for value in ("0", "false", "False", "NO", "off"):
            os.environ["PLATFORM_RUN_NO_SECRETS"] = value
            if run_validation.env_flag("PLATFORM_RUN_NO_SECRETS", True):
                fail(f"{value!r} should parse as false")
        for value in ("1", "true", "yes", "on", "anything-else"):
            os.environ["PLATFORM_RUN_NO_SECRETS"] = value
            if not run_validation.env_flag("PLATFORM_RUN_NO_SECRETS", False):
                fail(f"{value!r} should parse as true")
    finally:
        if old_value is None:
            os.environ.pop("PLATFORM_RUN_NO_SECRETS", None)
        else:
            os.environ["PLATFORM_RUN_NO_SECRETS"] = old_value


def test_run_script_environment() -> None:
    with mock.patch("run_validation.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        with redirect_stdout(StringIO()):
            result = run_validation.run_script("scripts/validate_project.py")
    if result != 0:
        fail("run_script must return the subprocess return code")
    call = run_mock.call_args
    if call is None:
        fail("run_script did not invoke subprocess.run")
    command = call.args[0]
    kwargs = call.kwargs
    if command[0] != sys.executable:
        fail("run_script must use the current Python interpreter")
    if str(ROOT / "scripts/validate_project.py") != command[1]:
        fail("run_script must execute scripts relative to the repository root")
    if kwargs.get("cwd") != ROOT:
        fail("run_script must run from the repository root")
    if kwargs.get("env", {}).get("PYTHONDONTWRITEBYTECODE") != "1":
        fail("run_script must suppress Python bytecode generation")


def test_main_list_mode() -> None:
    with (
        mock.patch.object(sys, "argv", ["run_validation.py", "--list"]),
        mock.patch("run_validation.run_script") as run_mock,
        redirect_stdout(StringIO()) as stdout,
    ):
        result = run_validation.main()
    if result != 0:
        fail("--list mode must exit successfully")
    if run_mock.called:
        fail("--list mode must not execute validation scripts")
    listed = stdout.getvalue().splitlines()
    if listed != list(run_validation.VALIDATION_SCRIPTS):
        fail("--list mode must print the validation scripts in run order")


def test_main_stops_on_first_failure() -> None:
    scripts = ["scripts/validate_project.py", "scripts/test_python_syntax.py", "scripts/test_line_endings.py"]
    with (
        mock.patch.object(sys, "argv", ["run_validation.py"]),
        mock.patch("run_validation.selected_scripts", return_value=scripts),
        mock.patch("run_validation.run_script", side_effect=[0, 17, 0]) as run_mock,
        redirect_stdout(StringIO()),
    ):
        result = run_validation.main()
    if result != 17:
        fail("main must return the first failing validation script exit code")
    called_scripts = [call.args[0] for call in run_mock.call_args_list]
    if called_scripts != scripts[:2]:
        fail("main must stop immediately after the first failing validation script")


def main() -> int:
    test_validation_script_list()
    test_no_secrets_selection()
    test_env_flag()
    test_run_script_environment()
    test_main_list_mode()
    test_main_stops_on_first_failure()
    print("Validation runner self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
