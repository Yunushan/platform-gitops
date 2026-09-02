#!/usr/bin/env python3
"""Run first-party child processes with bounded captured output."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import locale
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, BinaryIO


OUTPUT_LIMIT_ENV = "PLATFORM_SUBPROCESS_OUTPUT_MAX_BYTES"
DEFAULT_OUTPUT_MAX_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_MAX_BYTES = 256 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
STREAM_JOIN_SECONDS = 2.0


class BoundedSubprocessError(subprocess.SubprocessError):
    """Base class for bounded-capture policy failures."""


class SubprocessOutputLimitExceeded(BoundedSubprocessError):
    """Raised after a child exceeds the combined stdout/stderr limit."""

    def __init__(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        limit: int,
        stdout: str | bytes,
        stderr: str | bytes,
    ) -> None:
        super().__init__(
            f"child process exceeded the combined stdout/stderr limit of {limit} bytes"
        )
        self.cmd = cmd
        self.limit = limit
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout


class SubprocessCaptureError(BoundedSubprocessError):
    """Raised when captured streams cannot be drained deterministically."""


def bounded_output_max_bytes(default: int = DEFAULT_OUTPUT_MAX_BYTES) -> int:
    """Return a positive byte limit with a fixed hard ceiling."""
    selected_name = "default"
    raw_value = str(default)
    if os.environ.get(OUTPUT_LIMIT_ENV, "").strip():
        selected_name = OUTPUT_LIMIT_ENV
        raw_value = os.environ[OUTPUT_LIMIT_ENV].strip()

    try:
        limit = int(raw_value, 10)
    except ValueError as exc:
        raise ValueError(f"{selected_name} must be a whole number of bytes") from exc
    if limit <= 0 or limit > MAX_OUTPUT_MAX_BYTES:
        raise ValueError(
            f"{selected_name} must be greater than zero and no more than "
            f"{MAX_OUTPUT_MAX_BYTES} bytes"
        )
    return limit


def _explicit_output_max_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("output_max_bytes must be a whole number of bytes")
    if value <= 0 or value > MAX_OUTPUT_MAX_BYTES:
        raise ValueError(
            "output_max_bytes must be greater than zero and no more than "
            f"{MAX_OUTPUT_MAX_BYTES} bytes"
        )
    return value


def _decode(value: bytes, *, text_mode: bool, encoding: str, errors: str) -> str | bytes:
    if not text_mode:
        return value
    return value.decode(encoding, errors=errors)


def run_bounded(
    args: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    check: bool = False,
    output_max_bytes: int | None = None,
    stdout_callback: Callable[[bytes], bool] | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Capture bounded output; an optional callback can stop a streaming child.

    The callback receives stdout chunks on its reader thread and must bound its
    own work by the caller's deadline. Returning True kills and reaps the child;
    the returned process exit code still reflects that termination.
    """
    if timeout is None or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    limit = (
        bounded_output_max_bytes()
        if output_max_bytes is None
        else _explicit_output_max_bytes(output_max_bytes)
    )
    text_mode = text or encoding is not None or errors is not None
    selected_encoding = encoding or locale.getpreferredencoding(False)
    selected_errors = errors or "strict"

    if input is not None:
        if text_mode and not isinstance(input, str):
            raise TypeError("text-mode subprocess input must be str")
        if not text_mode and not isinstance(input, bytes):
            raise TypeError("binary-mode subprocess input must be bytes")
        input_bytes = input.encode(selected_encoding, errors=selected_errors) if isinstance(input, str) else input
    else:
        input_bytes = None

    process = subprocess.Popen(
        args,
        cwd=Path(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise SubprocessCaptureError("child process did not expose captured streams")

    lock = threading.Lock()
    exceeded = threading.Event()
    reader_failures: list[str] = []
    retained_total = 0
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()

    def terminate() -> None:
        try:
            process.kill()
        except OSError:
            pass

    def read_stream(stream: BinaryIO, destination: bytearray, label: str) -> None:
        nonlocal retained_total
        read = getattr(stream, "read1", stream.read)
        try:
            while True:
                chunk = read(READ_CHUNK_BYTES)
                if not chunk:
                    return
                should_terminate = False
                with lock:
                    remaining = max(limit - retained_total, 0)
                    retained = chunk[:remaining]
                    destination.extend(retained)
                    retained_total += len(retained)
                    if len(chunk) > remaining:
                        exceeded.set()
                        should_terminate = True
                if should_terminate:
                    terminate()
                    return
                if label == "stdout" and stdout_callback is not None:
                    try:
                        stop = stdout_callback(chunk)
                    except Exception as exc:
                        with lock:
                            reader_failures.append(f"stdout callback: {exc}")
                        terminate()
                        return
                    if stop:
                        terminate()
                        return
        except (OSError, ValueError) as exc:
            with lock:
                reader_failures.append(f"{label}: {exc}")
            terminate()

    def write_input() -> None:
        if input_bytes is None or process.stdin is None:
            return
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except OSError as exc:
            with lock:
                reader_failures.append(f"stdin: {exc}")
            terminate()

    readers = [
        threading.Thread(
            target=read_stream,
            args=(process.stdout, stdout_buffer, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(process.stderr, stderr_buffer, "stderr"),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate()
        try:
            returncode = process.wait(timeout=STREAM_JOIN_SECONDS)
        except subprocess.TimeoutExpired:
            returncode = process.returncode if process.returncode is not None else -1

    deadline = time.monotonic() + STREAM_JOIN_SECONDS
    for thread in [writer, *readers]:
        thread.join(max(0.0, deadline - time.monotonic()))

    with lock:
        stdout_bytes = bytes(stdout_buffer)
        stderr_bytes = bytes(stderr_buffer)
        failures = list(reader_failures)

    if timed_out:
        raise subprocess.TimeoutExpired(
            args,
            timeout,
            output=stdout_bytes,
            stderr=stderr_bytes,
        )
    if exceeded.is_set():
        bounded_stdout = _decode(
            stdout_bytes,
            text_mode=text_mode,
            encoding=selected_encoding,
            errors="replace",
        )
        bounded_stderr = _decode(
            stderr_bytes,
            text_mode=text_mode,
            encoding=selected_encoding,
            errors="replace",
        )
        raise SubprocessOutputLimitExceeded(
            args,
            limit,
            bounded_stdout,
            bounded_stderr,
        )
    if any(thread.is_alive() for thread in [writer, *readers]):
        raise SubprocessCaptureError("child output streams did not close after process exit")
    if failures:
        raise SubprocessCaptureError("failed to capture child process streams: " + "; ".join(failures))

    stdout = _decode(
        stdout_bytes,
        text_mode=text_mode,
        encoding=selected_encoding,
        errors=selected_errors,
    )
    stderr = _decode(
        stderr_bytes,
        text_mode=text_mode,
        encoding=selected_encoding,
        errors=selected_errors,
    )

    result = subprocess.CompletedProcess(args, returncode, stdout, stderr)
    if check and returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            args,
            output=stdout,
            stderr=stderr,
        )
    return result
