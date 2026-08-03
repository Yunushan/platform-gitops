#!/usr/bin/env python3
"""Write local evidence and state files atomically with private permissions."""

from __future__ import annotations

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
) -> None:
    """Replace *path* only after a complete, durable same-directory write."""

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
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            descriptor_open = False
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            os.chmod(destination, mode)
        _fsync_directory(destination.parent)
    except BaseException:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
