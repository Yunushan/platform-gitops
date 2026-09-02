#!/usr/bin/env python3
"""Validate bounded first-party subprocess output capture."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bounded_subprocess  # noqa: E402


def production_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.glob("*.py")
        if not path.name.startswith("test_")
    )


def test_limit_validation() -> None:
    with mock.patch.dict(os.environ, {bounded_subprocess.OUTPUT_LIMIT_ENV: ""}, clear=False):
        if bounded_subprocess.bounded_output_max_bytes() != 32 * 1024 * 1024:
            raise AssertionError("default subprocess output limit changed unexpectedly")

    with mock.patch.dict(
        os.environ,
        {bounded_subprocess.OUTPUT_LIMIT_ENV: "4096"},
        clear=False,
    ):
        if bounded_subprocess.bounded_output_max_bytes() != 4096:
            raise AssertionError("subprocess output limit override was not applied")

    for invalid in ("0", "-1", "1.5", "not-a-number", str(256 * 1024 * 1024 + 1)):
        with mock.patch.dict(
            os.environ,
            {bounded_subprocess.OUTPUT_LIMIT_ENV: invalid},
            clear=False,
        ):
            try:
                bounded_subprocess.bounded_output_max_bytes()
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid subprocess output limit was accepted: {invalid}")


def test_text_and_binary_capture() -> None:
    text_result = bounded_subprocess.run_bounded(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        text=True,
        timeout=10,
        output_max_bytes=4096,
    )
    if text_result.stdout.splitlines() != ["out"] or text_result.stderr.splitlines() != ["err"]:
        raise AssertionError("text subprocess output was not captured correctly")

    binary_result = bounded_subprocess.run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bytes')"],
        timeout=10,
        output_max_bytes=4096,
    )
    if binary_result.stdout != b"bytes" or binary_result.stderr != b"":
        raise AssertionError("binary subprocess output was not captured correctly")

    input_result = bounded_subprocess.run_bounded(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        input="bounded input",
        text=True,
        timeout=10,
        output_max_bytes=4096,
    )
    if input_result.stdout.splitlines() != ["BOUNDED INPUT"]:
        raise AssertionError("text subprocess input was not delivered correctly")


def test_combined_output_limit() -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('o' * 800); sys.stdout.flush(); "
        "sys.stderr.write('e' * 800); sys.stderr.flush()",
    ]
    try:
        bounded_subprocess.run_bounded(
            command,
            text=True,
            timeout=10,
            output_max_bytes=1024,
        )
    except bounded_subprocess.SubprocessOutputLimitExceeded as exc:
        retained = len(exc.stdout.encode("utf-8")) + len(exc.stderr.encode("utf-8"))
        if retained > 1024:
            raise AssertionError(f"output limiter retained too many bytes: {retained}")
    else:
        raise AssertionError("combined subprocess output limit was not enforced")

    utf8_command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write((chr(0x20ac) * 1000).encode('utf-8')); sys.stdout.flush()",
    ]
    try:
        bounded_subprocess.run_bounded(
            utf8_command,
            text=True,
            encoding="utf-8",
            timeout=10,
            output_max_bytes=1001,
        )
    except bounded_subprocess.SubprocessOutputLimitExceeded as exc:
        if not isinstance(exc.stdout, str):
            raise AssertionError("text output-limit diagnostics were not decoded")
    else:
        raise AssertionError("multibyte output did not trigger the configured limit")


def test_timeout_preserves_bounded_partial_output() -> None:
    command = [
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(5)",
    ]
    try:
        bounded_subprocess.run_bounded(
            command,
            text=True,
            timeout=0.2,
            output_max_bytes=1024,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.output != b"partial":
            raise AssertionError(f"timeout partial output changed: {exc.output!r}")
        if len(exc.output or b"") + len(exc.stderr or b"") > 1024:
            raise AssertionError("timeout retained output beyond the configured limit")
    else:
        raise AssertionError("timed-out subprocess unexpectedly completed")


def test_check_behavior() -> None:
    try:
        bounded_subprocess.run_bounded(
            [sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"],
            text=True,
            check=True,
            timeout=10,
            output_max_bytes=4096,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 7 or exc.stdout.splitlines() != ["failed"]:
            raise AssertionError("check=True did not preserve bounded command output")
    else:
        raise AssertionError("check=True accepted a non-zero subprocess")


def test_stdout_callback_stops_and_reaps_child() -> None:
    received = bytearray()

    def ready(chunk: bytes) -> bool:
        received.extend(chunk)
        return b"ready" in received

    started = time.monotonic()
    with mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as spawn:
        result = bounded_subprocess.run_bounded(
            [sys.executable, "-c", "import sys,time; "
             "print('ready', flush=True); time.sleep(30)"],
            timeout=5, output_max_bytes=1024, stdout_callback=ready,
        )
        if spawn.call_count != 1 or result.returncode == 0:
            raise AssertionError("callback did not terminate its long-lived child")
    if time.monotonic() - started >= 4 or received.splitlines() != [b"ready"]:
        raise AssertionError("streaming callback waited for the child timeout")

    def broken(chunk: bytes) -> bool:
        del chunk
        raise RuntimeError("callback-test-error")

    try:
        bounded_subprocess.run_bounded(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            timeout=5, output_max_bytes=1024, stdout_callback=broken,
        )
    except bounded_subprocess.SubprocessCaptureError as exc:
        if "callback-test-error" not in str(exc):
            raise AssertionError("callback failure was lost") from exc
    else:
        raise AssertionError("callback exception was ignored")

    callback = mock.Mock(return_value=False)
    try:
        bounded_subprocess.run_bounded(
            [sys.executable, "-c", "print('x' * 10000, flush=True)"],
            timeout=5, output_max_bytes=32, stdout_callback=callback,
        )
    except bounded_subprocess.SubprocessOutputLimitExceeded:
        if sum(len(call.args[0]) for call in callback.call_args_list) > 32:
            raise AssertionError("callback received output beyond the capture limit")
    else:
        raise AssertionError("callback bypassed the output limit")

    try:
        bounded_subprocess.run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.2, output_max_bytes=1024, stdout_callback=ready,
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("silent streaming child escaped its deadline")


def test_production_capture_uses_shared_runner() -> None:
    direct_capture: list[str] = []
    unbounded_runner: list[str] = []
    popen_calls: list[str] = []
    runner_count = 0
    for path in production_python_files():
        document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(document):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "Popen"
                and path.name != "bounded_subprocess.py"
            ):
                popen_calls.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "run"
            ):
                keywords = {item.arg: item.value for item in node.keywords}
                capture = keywords.get("capture_output")
                pipe_names = {"stdout", "stderr"}
                explicit_pipe = any(
                    name in keywords
                    and isinstance(keywords[name], ast.Attribute)
                    and keywords[name].attr == "PIPE"
                    for name in pipe_names
                )
                if (isinstance(capture, ast.Constant) and capture.value is True) or explicit_pipe:
                    direct_capture.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node.func, ast.Name) and node.func.id == "run_bounded":
                runner_count += 1
                timeout = next((item.value for item in node.keywords if item.arg == "timeout"), None)
                if timeout is None or (isinstance(timeout, ast.Constant) and timeout.value is None):
                    unbounded_runner.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    if direct_capture:
        raise AssertionError("direct production subprocess capture remains:\n" + "\n".join(direct_capture))
    if popen_calls:
        raise AssertionError("production Popen escaped the shared runner:\n" + "\n".join(popen_calls))
    if unbounded_runner:
        raise AssertionError("run_bounded call lacks timeout:\n" + "\n".join(unbounded_runner))
    if runner_count < 12:
        raise AssertionError(f"bounded subprocess scan covered too few calls: {runner_count}")


def test_injected_runner_defaults_to_bounded_capture() -> None:
    path = SCRIPTS / "verify_production_readiness_score.py"
    document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node
        for node in document.body
        if isinstance(node, ast.FunctionDef) and node.name == "verify_release_bundle"
    ]
    if len(functions) != 1:
        raise AssertionError("verify_release_bundle definition was not uniquely identified")
    function = functions[0]
    keyword_defaults = dict(
        zip(
            (argument.arg for argument in function.args.kwonlyargs),
            function.args.kw_defaults,
            strict=True,
        )
    )
    runner_default = keyword_defaults.get("runner")
    if not isinstance(runner_default, ast.Name) or runner_default.id != "run_bounded":
        raise AssertionError("production-readiness injected runner does not default to run_bounded")
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "runner"
    ]
    if len(calls) != 1:
        raise AssertionError(f"expected one production-readiness runner call, found {len(calls)}")
    keywords = {item.arg: item.value for item in calls[0].keywords}
    if "capture_output" in keywords or "stdout" in keywords or "stderr" in keywords:
        raise AssertionError("injected production-readiness runner bypasses bounded capture")


def main() -> int:
    test_stdout_callback_stops_and_reaps_child()
    test_limit_validation()
    test_text_and_binary_capture()
    test_combined_output_limit()
    test_timeout_preserves_bounded_partial_output()
    test_check_behavior()
    test_production_capture_uses_shared_runner()
    test_injected_runner_defaults_to_bounded_capture()
    print("First-party subprocess output contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
