#!/usr/bin/env python3
"""Shared byte limits for first-party local file inputs."""

from __future__ import annotations

import os
from pathlib import Path
import stat


FILE_INPUT_LIMIT_ENV = "PLATFORM_FILE_INPUT_MAX_BYTES"
DEFAULT_FILE_INPUT_MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_INPUT_BYTES = 512 * 1024 * 1024


class FileInputTooLarge(ValueError):
    """Raised when a local input exceeds the configured byte limit."""

    def __init__(self, path: Path, limit: int) -> None:
        super().__init__(f"file input exceeds the {limit}-byte limit: {path}")
        self.path = path
        self.limit = limit


class FileInputNotRegular(ValueError):
    """Raised when an input is not a regular file."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"file input is not a regular file: {path}")
        self.path = path


def bounded_file_input_max_bytes(default: int = DEFAULT_FILE_INPUT_MAX_BYTES) -> int:
    """Return a positive local-input limit with a fixed hard ceiling."""
    selected_name = "default"
    raw_value = str(default)
    if os.environ.get(FILE_INPUT_LIMIT_ENV, "").strip():
        selected_name = FILE_INPUT_LIMIT_ENV
        raw_value = os.environ[FILE_INPUT_LIMIT_ENV].strip()

    try:
        limit = int(raw_value, 10)
    except ValueError as exc:
        raise ValueError(f"{selected_name} must be a whole number of bytes") from exc
    if limit <= 0 or limit > MAX_FILE_INPUT_BYTES:
        raise ValueError(
            f"{selected_name} must be greater than zero and no more than "
            f"{MAX_FILE_INPUT_BYTES} bytes"
        )
    return limit


def _explicit_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_bytes must be a whole number of bytes")
    if value <= 0 or value > MAX_FILE_INPUT_BYTES:
        raise ValueError(
            "max_bytes must be greater than zero and no more than "
            f"{MAX_FILE_INPUT_BYTES} bytes"
        )
    return value


def read_bounded_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read at most the configured number of bytes from a local input."""
    target = Path(path)
    limit = (
        bounded_file_input_max_bytes()
        if max_bytes is None
        else _explicit_limit(max_bytes)
    )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor: int | None = os.open(target, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FileInputNotRegular(target)
        if metadata.st_size > limit:
            raise FileInputTooLarge(target, limit)
        handle = os.fdopen(descriptor, "rb")
        descriptor = None
        with handle:
            data = handle.read(limit + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(data) > limit:
        raise FileInputTooLarge(target, limit)
    return data


def read_bounded_text(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
    max_bytes: int | None = None,
) -> str:
    """Read and decode a local input only after enforcing its byte limit."""
    decoded = read_bounded_bytes(path, max_bytes=max_bytes).decode(
        encoding,
        errors=errors,
    )
    return decoded.replace("\r\n", "\n").replace("\r", "\n")
