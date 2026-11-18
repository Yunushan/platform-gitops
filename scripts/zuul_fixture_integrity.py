"""Bounded content and metadata fingerprint for isolated local test artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


def fingerprint(root: Path) -> dict:
    if root.is_symlink():
        raise ValueError("Fixture root cannot be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Fixture root must be a directory")
    digest = hashlib.sha256()
    digest.update(json.dumps({"path": ".", "kind": "directory", "mode": stat.S_IMODE(root.stat().st_mode)},
                             sort_keys=True, separators=(",", ":")).encode() + b"\n")
    counts = {"files": 0, "directories": 0, "symlinks": 0, "bytes": 0}
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in sorted(dirs + files):
            path = Path(base) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            record = {"path": rel, "mode": stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise ValueError("Fixture symlink escapes the verified root")
                record.update(kind="symlink", target=os.readlink(path))
                counts["symlinks"] += 1
            elif stat.S_ISDIR(info.st_mode):
                record["kind"] = "directory"
                counts["directories"] += 1
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > 128 * 1024 ** 2:
                    raise ValueError("Fixture file exceeds size limit")
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                        raise ValueError("Fixture file changed during inspection")
                    content = hashlib.file_digest(stream, "sha256").hexdigest()
                    final = os.fstat(stream.fileno())
                    if (final.st_size, final.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                        raise ValueError("Fixture file changed during inspection")
                record.update(kind="file", size=info.st_size, sha256=content)
                counts["files"] += 1
                counts["bytes"] += info.st_size
            else:
                raise ValueError("Unsupported fixture artifact type")
            if sum(counts[k] for k in ("files", "directories", "symlinks")) > 500000 or counts["bytes"] > 6 * 1024 ** 3:
                raise ValueError("Fixture exceeds inventory bounds")
            digest.update(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
    return {"algorithm": "sha256-jsonl-v1", "digest": digest.hexdigest(), **counts}
