"""File staging helpers shared by offline bootstrap kits."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

from atomic_file import atomic_binary_output
from bounded_file import read_bounded_bytes


def read_regular(path, max_bytes):
    if path.is_symlink():
        raise ValueError("Recovery inputs must be regular files")
    return read_bounded_bytes(path, max_bytes=max_bytes)


def read_pinned_manifest(path, expected, max_bytes):
    data = read_regular(path, max_bytes)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Manifest differs from the reviewed pin")
    return data


def copy_archive(source, target):
    if source.is_symlink():
        raise ValueError("Image archive must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 1024 ** 3:
            raise ValueError("Use a bounded image archive")
        with atomic_binary_output(target, replace=False) as output:
            remaining = info.st_size
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("Image archive truncated during copy")
                output.write(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise ValueError("Image archive grew during copy")


@contextmanager
def new_recovery_directory(directory):
    output = directory.parent.resolve(strict=True) / directory.name
    output.mkdir(mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=output) as temporary:
            stage = Path(temporary)
            yield stage
            for file in stage.iterdir():
                file.rename(output / file.name)
    except Exception:
        shutil.rmtree(output)
        raise
