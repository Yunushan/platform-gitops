#!/usr/bin/env python3
"""Write local evidence and state files atomically with private permissions."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


PRIVATE_FILE_MODE = 0o600


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the host filesystem supports it."""

    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = PRIVATE_FILE_MODE,
    replace: bool = True,
) -> None:
    """Replace *path* only after a complete, durable same-directory write."""

    atomic_write_bytes(path, text.encode(encoding), mode=mode, replace=replace)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = PRIVATE_FILE_MODE, replace: bool = True) -> None:
    """Replace *path* with bytes from a durable same-directory temporary file."""

    with atomic_binary_output(path, mode=mode, replace=replace) as handle:
        handle.write(data)


@contextmanager
def atomic_binary_output(path: Path, *, mode: int = PRIVATE_FILE_MODE, replace: bool = True):
    """Publish a complete streamed file, optionally refusing an existing path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor_open = False
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, destination)
        elif os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        _fsync_directory(destination.parent)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
