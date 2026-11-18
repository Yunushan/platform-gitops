#!/usr/bin/env python3
"""Validate bounded first-party local file input."""

from __future__ import annotations

import ast
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
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


def network_openers(document):
    """Recognize urllib factories, rather than ignoring every `.opener.open`."""
    factories = {item.asname or item.name for node in ast.walk(document) if isinstance(node, ast.ImportFrom) and node.module == "urllib.request" for item in node.names if item.name == "build_opener"}
    assigned = {}
    for node in ast.walk(document):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)): continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        is_factory = isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in factories
        for target in targets:
            assigned.setdefault(ast.unparse(target), []).append(is_factory)
    return {name for name, values in assigned.items() if values and all(values)}


def direct_file_read(node: ast.Call, path: Path, *, http_openers=()) -> bool:
    if isinstance(node.func, ast.Attribute) and node.func.attr in {
        "read_text",
        "read_bytes",
    }:
        return True
    if path.name == "bounded_file.py":
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == "open" and ast.unparse(node.func.value) in http_openers:
        return False
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "tarfile"
        and any(keyword.arg == "fileobj" for keyword in node.keywords)
    ):
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
    if not mode or any(c not in "rwaxbt+" for c in mode) or sum(c in "rwax" for c in mode) != 1 or len(mode) != len(set(mode)):
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


def test_binary_stream_boundaries() -> None:
    exact = io.BytesIO(b"a" * 32)
    if bounded_file.read_bounded_stream(
        exact,
        max_bytes=32,
        label="exact fixture",
    ) != b"a" * 32:
        raise AssertionError("an exact-limit binary stream changed")

    oversized = io.BytesIO(b"b" * 33)
    try:
        bounded_file.read_bounded_stream(
            oversized,
            max_bytes=32,
            label="oversized fixture",
        )
    except bounded_file.StreamInputTooLarge as exc:
        if exc.label != "oversized fixture" or exc.limit != 32:
            raise AssertionError("oversized stream error lost its label or limit")
    else:
        raise AssertionError("an oversized binary stream was accepted")


def test_non_regular_inputs_are_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="bounded-file-regular-") as temporary:
        root = Path(temporary)
        path = root / "input.bin"
        path.write_bytes(b"content")
        metadata = SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0)
        with mock.patch.object(bounded_file.os, "fstat", return_value=metadata):
            try:
                bounded_file.read_bounded_bytes(path)
            except bounded_file.FileInputNotRegular as exc:
                if exc.path != path:
                    raise AssertionError("non-regular input error lost its path")
            else:
                raise AssertionError("a non-regular local input was accepted")

        if hasattr(os, "mkfifo"):
            fifo = root / "input.fifo"
            os.mkfifo(fifo)
            try:
                bounded_file.read_bounded_bytes(fifo)
            except bounded_file.FileInputNotRegular:
                pass
            else:
                raise AssertionError("a FIFO local input was accepted")


def test_production_reads_use_shared_policy() -> None:
    direct_reads: list[str] = []
    bounded_reads = 0
    for path in production_python_files():
        document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        http_openers = network_openers(document)
        for node in ast.walk(document):
            if not isinstance(node, ast.Call):
                continue
            if direct_file_read(node, path, http_openers=http_openers):
                direct_reads.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node.func, ast.Name) and node.func.id in {
                "read_bounded_text",
                "read_bounded_bytes",
                "read_bounded_stream",
                "read_bootstrap_bytes",
                "open_bounded_file",
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
        'tarfile.open("input.tgz", mode="r:gz")',
    ):
        document = ast.parse(source)
        calls = [node for node in ast.walk(document) if isinstance(node, ast.Call)]
        if not any(direct_file_read(node, synthetic_path) for node in calls):
            raise AssertionError(f"direct local read escaped static detection: {source}")

    document = ast.parse('open("output", "wb")')
    call = next(node for node in ast.walk(document) if isinstance(node, ast.Call))
    if direct_file_read(call, synthetic_path):
        raise AssertionError("write-only file output was classified as a local input")

    document = ast.parse('tarfile.open(fileobj=stream, mode="r:gz")')
    call = next(node for node in ast.walk(document) if isinstance(node, ast.Call))
    if direct_file_read(call, synthetic_path):
        raise AssertionError("an in-memory tar stream was classified as a local file read")

    for source, expected in ((
        'from urllib.request import build_opener\nself.opener = build_opener()\nself.opener.open(request, timeout=15)', False), (
        'self.opener.open("local-file")', True), (
        'from urllib.request import build_opener\nself.opener = build_opener()\nself.opener = Path("input")\nself.opener.open()', True)):
        document = ast.parse(source)
        calls = [n for n in ast.walk(document) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "open"]
        if len(calls) != 1 or direct_file_read(calls[0], synthetic_path, http_openers=network_openers(document)) is not expected:
            raise AssertionError("HTTP versus local opener classification is incorrect")


def test_streaming_file_limits_and_growth():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "input"; path.write_bytes(b"1234")
        with bounded_file.open_bounded_file(path, max_bytes=4) as stream:
            if stream.read(2) + stream.read(2) != b"1234" or stream.read(1) != b"" or stream.tell() != 4:
                raise AssertionError("Streaming input changed or lost its byte count")
        with bounded_file.open_bounded_file(path, max_bytes=2 ** 63 - 1) as stream:
            for size in (-1, True, bounded_file.MAX_FILE_INPUT_BYTES + 1):
                try: stream.read(size)
                except ValueError: pass
                else: raise AssertionError("An unbounded streaming allocation was accepted")
        with bounded_file.open_bounded_file(path, max_bytes=4) as stream:
            with path.open("ab") as writer: writer.write(b"5")
            for _ in range(2):
                try: stream.read(16)
                except bounded_file.FileInputTooLarge: pass
                else: raise AssertionError("Growth past the original budget was accepted or its error was not retained")
        path.write_bytes(b"")
        with bounded_file.open_bounded_file(path, max_bytes=0) as stream:
            if stream.read(1) != b"": raise AssertionError("An empty sealed file changed")
        if hasattr(os, "mkfifo"):
            fifo = Path(temporary) / "fifo"; os.mkfifo(fifo)
            try:
                with bounded_file.open_bounded_file(fifo, max_bytes=4): pass
            except bounded_file.FileInputNotRegular: pass
            else: raise AssertionError("A streaming FIFO was accepted")


def test_embedded_bootstrap_readers_share_the_exact_policy():
    core = ast.parse((SCRIPTS / "bounded_file.py").read_text())
    definition = next(n for n in core.body if isinstance(n, ast.FunctionDef) and n.name == "read_bootstrap_bytes")
    expected = ast.dump(definition, include_attributes=False)
    for filename in ("ceph_client_io.py", "ceph_device_probe.py", "gerrit_storage.py", "gateway_edge_pair_staging.py"):
        document = ast.parse((SCRIPTS / filename).read_text())
        definitions = [n for n in document.body if isinstance(n, ast.FunctionDef) and n.name == "read_bootstrap_bytes"]
        if len(definitions) != 1 or ast.dump(definitions[0], include_attributes=False) != expected:
            raise AssertionError("Standalone reader drifted from the shared bounded policy: " + filename)
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "input"; path.write_bytes(b"1234")
        if bounded_file.read_bootstrap_bytes(path, 4) != b"1234": raise AssertionError("Bootstrap bytes changed")
        for value in (3, 0, True, 512 * 1024 * 1024 + 1):
            try: bounded_file.read_bootstrap_bytes(path, value)
            except ValueError: pass
            else: raise AssertionError("Bootstrap reader accepted an invalid or exceeded limit")


def main() -> int:
    test_limit_validation()
    test_binary_and_text_boundaries()
    test_explicit_limit_ignores_environment_override()
    test_binary_stream_boundaries()
    test_non_regular_inputs_are_rejected()
    test_direct_read_detection()
    test_streaming_file_limits_and_growth()
    test_embedded_bootstrap_readers_share_the_exact_policy()
    test_production_reads_use_shared_policy()
    print("First-party bounded file input contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
