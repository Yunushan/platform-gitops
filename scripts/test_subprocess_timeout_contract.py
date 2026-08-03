#!/usr/bin/env python3
"""Validate bounded first-party subprocess execution and timeout configuration."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import subprocess_timeout  # noqa: E402


def production_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.glob("*.py")
        if not path.name.startswith("test_")
    )


def subprocess_calls(path: Path) -> list[ast.Call]:
    document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(document)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            )
            or (isinstance(node.func, ast.Name) and node.func.id == "run_bounded")
        )
    ]


def test_every_production_subprocess_is_bounded() -> None:
    checked = 0
    failures: list[str] = []
    for path in production_python_files():
        for call in subprocess_calls(path):
            checked += 1
            timeout = next((item.value for item in call.keywords if item.arg == "timeout"), None)
            if timeout is None or (
                isinstance(timeout, ast.Constant) and timeout.value is None
            ):
                failures.append(f"{path.relative_to(ROOT)}:{call.lineno}: subprocess call lacks timeout")
    if checked < 13:
        raise AssertionError(f"subprocess timeout scan covered too few calls: {checked}")
    if failures:
        raise AssertionError("\n".join(failures))


def test_injected_subprocess_runner_is_bounded() -> None:
    path = SCRIPTS / "verify_production_readiness_score.py"
    document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [
        node
        for node in ast.walk(document)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "runner"
    ]
    if len(calls) != 1:
        raise AssertionError(f"expected one injected readiness runner call, found {len(calls)}")
    timeout = next((item.value for item in calls[0].keywords if item.arg == "timeout"), None)
    if timeout is None or (isinstance(timeout, ast.Constant) and timeout.value is None):
        raise AssertionError("injected production-readiness subprocess runner lacks timeout")


def test_timeout_precedence_and_validation() -> None:
    names = {
        subprocess_timeout.GLOBAL_TIMEOUT_ENV: "",
        "PLATFORM_TEST_COMMAND_TIMEOUT_SECONDS": "",
    }
    with mock.patch.dict(os.environ, names, clear=False):
        if subprocess_timeout.bounded_timeout_seconds(45) != 45:
            raise AssertionError("default subprocess timeout was not retained")

    with mock.patch.dict(
        os.environ,
        {
            subprocess_timeout.GLOBAL_TIMEOUT_ENV: "90",
            "PLATFORM_TEST_COMMAND_TIMEOUT_SECONDS": "120",
        },
        clear=False,
    ):
        if subprocess_timeout.bounded_timeout_seconds(45) != 90:
            raise AssertionError("global subprocess timeout override was not applied")
        if (
            subprocess_timeout.bounded_timeout_seconds(
                45, "PLATFORM_TEST_COMMAND_TIMEOUT_SECONDS"
            )
            != 120
        ):
            raise AssertionError("specific subprocess timeout did not override the global value")

    for invalid in ("0", "-1", "nan", "inf", "86401", "not-a-number"):
        with mock.patch.dict(
            os.environ,
            {subprocess_timeout.GLOBAL_TIMEOUT_ENV: invalid},
            clear=False,
        ):
            try:
                subprocess_timeout.bounded_timeout_seconds(45)
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid subprocess timeout was accepted: {invalid}")


def test_timeout_stream_normalization() -> None:
    if subprocess_timeout.timeout_stream_text(None) != "":
        raise AssertionError("missing timeout output did not normalize to an empty string")
    if subprocess_timeout.timeout_stream_text(b"partial\xff") != "partial�":
        raise AssertionError("byte timeout output was not decoded safely")
    if subprocess_timeout.timeout_stream_text("partial") != "partial":
        raise AssertionError("text timeout output changed during normalization")


def main() -> int:
    test_every_production_subprocess_is_bounded()
    test_injected_subprocess_runner_is_bounded()
    test_timeout_precedence_and_validation()
    test_timeout_stream_normalization()
    print("First-party subprocess timeout contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
