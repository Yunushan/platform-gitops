#!/usr/bin/env python3
"""Validate bounded first-party local file input."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bounded_file  # noqa: E402


def production_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.rglob("*.py")
        if not path.name.startswith("test_")
    )


def direct_file_read(node: ast.Call, path: Path) -> bool:
    if isinstance(node.func, ast.Attribute) and node.func.attr in {
        "read_text",
        "read_bytes",
    }:
        return True
    if path.name == "bounded_file.py":
        return False

    mode_index: int
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        mode_index = 1
    elif (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
        and not isinstance(node.func.value, ast.Call)
        and not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        )
    ):
        mode_index = 0
    else:
        return False

    mode_node: ast.expr | None = None
    if len(node.args) > mode_index:
        mode_node = node.args[mode_index]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
            break
    if mode_node is None:
        mode = "r"
    elif isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        mode = mode_node.value
    else:
        return True
    return not any(flag in mode for flag in "wax")


def test_limit_validation() -> None:
    with mock.patch.dict(os.environ, {bounded_file.FILE_INPUT_LIMIT_ENV: ""}, clear=False):
        if bounded_file.bounded_file_input_max_bytes() != 64 * 1024 * 1024:
            raise AssertionError("default local file input limit changed unexpectedly")

    with mock.patch.dict(
        os.environ,
        {bounded_file.FILE_INPUT_LIMIT_ENV: "4096"},
        clear=False,
    ):
        if bounded_file.bounded_file_input_max_bytes() != 4096:
            raise AssertionError("local file input limit override was not applied")

    for invalid in ("0", "-1", "1.5", "not-a-number", str(512 * 1024 * 1024 + 1)):
        with mock.patch.dict(
            os.environ,
            {bounded_file.FILE_INPUT_LIMIT_ENV: invalid},
            clear=False,
        ):
            try:
                bounded_file.bounded_file_input_max_bytes()
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid local file input limit was accepted: {invalid}")


def test_binary_and_text_boundaries() -> None:
    with tempfile.TemporaryDirectory(prefix="bounded-file-contract-") as temporary:
        root = Path(temporary)
        exact = root / "exact.bin"
        exact.write_bytes(b"a" * 32)
        if bounded_file.read_bounded_bytes(exact, max_bytes=32) != b"a" * 32:
            raise AssertionError("an exact-limit binary input changed")

        oversized = root / "oversized.bin"
        oversized.write_bytes(b"b" * 33)
        try:
            bounded_file.read_bounded_bytes(oversized, max_bytes=32)
        except bounded_file.FileInputTooLarge as exc:
            if exc.path != oversized or exc.limit != 32:
                raise AssertionError("oversized input error lost its path or limit")
        else:
            raise AssertionError("an oversized binary input was accepted")

        text = root / "text.txt"
        text.write_bytes(b"valid\xff")
        if bounded_file.read_bounded_text(text, errors="replace", max_bytes=32) != "valid" + chr(0xfffd):
            raise AssertionError("bounded text decoding did not honor errors=replace")

        newlines = root / "newlines.txt"
        newlines.write_bytes(b"first\r\nsecond\rthird\n")
        if bounded_file.read_bounded_text(newlines, max_bytes=32) != "first\nsecond\nthird\n":
            raise AssertionError("bounded text decoding lost universal-newline behavior")


def test_explicit_limit_ignores_environment_override() -> None:
    with tempfile.TemporaryDirectory(prefix="bounded-file-explicit-") as temporary:
        path = Path(temporary) / "input.bin"
        path.write_bytes(b"1234")
        with mock.patch.dict(
            os.environ,
            {bounded_file.FILE_INPUT_LIMIT_ENV: "1"},
            clear=False,
        ):
            if bounded_file.read_bounded_bytes(path, max_bytes=4) != b"1234":
                raise AssertionError("explicit local input limit did not override the environment")


def test_production_reads_use_shared_policy() -> None:
    direct_reads: list[str] = []
    bounded_reads = 0
    for path in production_python_files():
        document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(document):
            if not isinstance(node, ast.Call):
                continue
            if direct_file_read(node, path):
                direct_reads.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node.func, ast.Name) and node.func.id in {
                "read_bounded_text",
                "read_bounded_bytes",
            }:
                bounded_reads += 1

    if direct_reads:
        raise AssertionError("direct production file reads remain:\n" + "\n".join(direct_reads))
    if bounded_reads < 50:
        raise AssertionError(f"bounded file scan covered too few calls: {bounded_reads}")


def test_direct_read_detection() -> None:
    synthetic_path = SCRIPTS / "synthetic.py"
    for source in (
        'Path("input").read_text()',
        'open("input", "rb")',
        'path.open()',
        'path.open(mode="r+")',
    ):
        document = ast.parse(source)
        calls = [node for node in ast.walk(document) if isinstance(node, ast.Call)]
        if not any(direct_file_read(node, synthetic_path) for node in calls):
            raise AssertionError(f"direct local read escaped static detection: {source}")

    document = ast.parse('open("output", "wb")')
    call = next(node for node in ast.walk(document) if isinstance(node, ast.Call))
    if direct_file_read(call, synthetic_path):
        raise AssertionError("write-only file output was classified as a local input")


def main() -> int:
    test_limit_validation()
    test_binary_and_text_boundaries()
    test_explicit_limit_ignores_environment_override()
    test_direct_read_detection()
    test_production_reads_use_shared_policy()
    print("First-party bounded file input contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
