#!/usr/bin/env python3
"""Shared byte limits for first-party local file inputs."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import BinaryIO


FILE_INPUT_LIMIT_ENV = "PLATFORM_FILE_INPUT_MAX_BYTES"
DEFAULT_FILE_INPUT_MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_INPUT_BYTES = 512 * 1024 * 1024


def read_bootstrap_bytes(path, max_bytes, *, follow_symlinks=True):
    """Self-contained primitive mirrored by the pre-import SSH staging agent."""
    import os
    import stat
    if type(max_bytes) is not int or not 0 < max_bytes <= 512 * 1024 * 1024:
        raise ValueError("Invalid bootstrap file limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow_symlinks: flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("Invalid or excessive bootstrap file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise ValueError("Bootstrap file grew beyond its limit")
        return value
    finally:
        os.close(descriptor)


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


class StreamInputTooLarge(ValueError):
    """Raised when a bounded binary stream exceeds its explicit byte limit."""

    def __init__(self, label: str, limit: int) -> None:
        super().__init__(f"stream input exceeds the {limit}-byte limit: {label}")
        self.label = label
        self.limit = limit


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


def read_bounded_stream(
    stream: BinaryIO,
    *,
    max_bytes: int,
    label: str = "stream",
) -> bytes:
    """Read a binary stream to EOF while retaining at most an explicit limit."""
    limit = _explicit_limit(max_bytes)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError(f"bounded stream must return bytes: {label}")
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise StreamInputTooLarge(label, limit)
    return b"".join(chunks)


class BoundedFileReader:
    """Forward-only reads with a total budget and bounded per-call allocation."""
    def __init__(self, stream, path, limit):
        self._stream, self.path, self.limit, self.total = stream, path, limit, 0

    def read(self, size=-1):
        if type(size) is not int or size < 0 or size > MAX_FILE_INPUT_BYTES:
            raise ValueError("Streaming file reads require an explicit bounded chunk")
        if self.total > self.limit: raise FileInputTooLarge(self.path, self.limit)
        if size == 0: return b""
        value = self._stream.read(min(size, self.limit - self.total + 1))
        self.total += len(value)
        if self.total > self.limit: raise FileInputTooLarge(self.path, self.limit)
        return value

    def tell(self): return self.total
    def readable(self): return True
    def seekable(self): return False
    @property
    def closed(self): return self._stream.closed


@contextmanager
def open_bounded_file(path, *, max_bytes):
    """Stream a regular file, including large backups, without loading it whole.

    The total limit is explicit (up to signed 64-bit file size), independent of
    the whole-file allocation ceiling. Each read must specify a bounded chunk.
    Symlinks and special files are refused before reading; growth is bounded.
    """
    if type(max_bytes) is not int or not 0 <= max_bytes <= 2 ** 63 - 1:
        raise ValueError("Streaming files require an explicit nonnegative byte budget")
    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(target, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode): raise FileInputNotRegular(target)
        if info.st_size > max_bytes: raise FileInputTooLarge(target, max_bytes)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield BoundedFileReader(stream, target, max_bytes)
    finally:
        os.close(descriptor)


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
