#!/usr/bin/env python3
"""Validate and refresh the reviewed inventory of consumed local Helm charts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from urllib.parse import urlsplit

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vendored-charts.json"
INVENTORY_MAX_BYTES = 1 * 1024 * 1024
CHART_METADATA_MAX_BYTES = 1 * 1024 * 1024
CHART_FILE_MAX_BYTES = 64 * 1024 * 1024
CHART_TREE_MAX_BYTES = 256 * 1024 * 1024
CHART_TREE_MAX_FILES = 20_000
ENTRY_FIELDS = {"path", "repository", "name", "version", "treeSha256"}
NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_link_like(path: Path) -> bool:
    """Return whether a path is a symlink or Windows directory junction."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _clean_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def chart_metadata(chart_root: Path) -> dict[str, str]:
    """Read the top-level name and version from a bounded Chart.yaml."""
    chart_yaml = chart_root / "Chart.yaml"
    chart_yaml_metadata = chart_yaml.lstat()
    if not stat.S_ISREG(chart_yaml_metadata.st_mode) or _is_link_like(chart_yaml):
        raise ValueError(f"chart metadata is not a regular file: {chart_yaml}")
    text = read_bounded_text(chart_yaml, max_bytes=CHART_METADATA_MAX_BYTES)
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) != len(line.lstrip(" ")) or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key not in {"name", "version"}:
            continue
        if key in metadata:
            raise ValueError(f"duplicate top-level {key} in {chart_yaml}")
        metadata[key] = _clean_yaml_scalar(value)
    if not metadata.get("name") or not metadata.get("version"):
        raise ValueError(f"{chart_yaml} must declare top-level name and version")
    return metadata


def chart_tree_sha256(chart_root: Path) -> str:
    """Hash regular chart files by normalized path, length, and exact bytes."""
    root_metadata = chart_root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or _is_link_like(chart_root):
        raise ValueError(f"chart root is not a regular directory: {chart_root}")

    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        chart_root,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        for name in directory_names:
            child = current / name
            metadata = child.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or _is_link_like(child):
                raise ValueError(f"chart tree contains a non-directory entry: {child}")
        for name in file_names:
            child = current / name
            metadata = child.lstat()
            if not stat.S_ISREG(metadata.st_mode) or _is_link_like(child):
                raise ValueError(f"chart tree contains a non-regular file: {child}")
            files.append(child)
            if len(files) > CHART_TREE_MAX_FILES:
                raise ValueError(
                    f"chart tree exceeds the {CHART_TREE_MAX_FILES}-file limit: {chart_root}"
                )

    if not files:
        raise ValueError(f"chart tree contains no regular files: {chart_root}")

    digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(files, key=lambda item: item.relative_to(chart_root).as_posix()):
        data = read_bounded_bytes(path, max_bytes=CHART_FILE_MAX_BYTES)
        total_bytes += len(data)
        if total_bytes > CHART_TREE_MAX_BYTES:
            raise ValueError(
                f"chart tree exceeds the {CHART_TREE_MAX_BYTES}-byte limit: {chart_root}"
            )
        relative = path.relative_to(chart_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _load_inventory(path: Path) -> tuple[dict[str, object] | None, list[str]]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or _is_link_like(path):
            return None, [f"chart inventory is not a regular file: {path}"]
        document = loads_strict_json(
            read_bounded_text(path, max_bytes=INVENTORY_MAX_BYTES)
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return None, [f"cannot read strict chart inventory {path}: {exc}"]
    if not isinstance(document, dict):
        return None, [f"chart inventory must contain a JSON object: {path}"]
    return document, []


def _safe_relative_path(root: Path, value: object) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value or len(value) > 1024:
        return None, "path must be a non-empty string no longer than 1024 characters"
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or value != parsed.as_posix():
        return None, f"path must be normalized, relative, and non-escaping: {value!r}"
    candidate = root.joinpath(*parsed.parts)
    try:
        root_metadata = root.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode) or _is_link_like(root):
            return None, f"repository root is not a regular directory: {root}"
        current = root
        for part in parsed.parts:
            current = current / part
            current.lstat()
            if _is_link_like(current):
                return None, f"path contains a symbolic link or junction: {value!r}"
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        return None, f"path does not resolve inside the repository: {value!r}: {exc}"
    return resolved, None


def _repository_problem(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return "repository must be a non-empty string no longer than 2048 characters"
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "oci"} or not parsed.hostname:
        return f"repository must use an absolute HTTPS or OCI URL: {value!r}"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return f"repository must not contain credentials, a query, or a fragment: {value!r}"
    if parsed.scheme == "oci" and not parsed.path.strip("/"):
        return f"OCI repository must include a path: {value!r}"
    return None


def _inventory_entries(
    document: dict[str, object],
) -> tuple[list[dict[str, object]], list[str]]:
    problems: list[str] = []
    if set(document) != {"schemaVersion", "charts"}:
        problems.append("chart inventory must contain only schemaVersion and charts")
    schema_version = document.get("schemaVersion")
    if isinstance(schema_version, bool) or schema_version != 1:
        problems.append("chart inventory schemaVersion must be 1")
    raw_entries = document.get("charts")
    if not isinstance(raw_entries, list) or not raw_entries:
        problems.append("chart inventory charts must be a non-empty list")
        return [], problems
    if len(raw_entries) > 256:
        problems.append("chart inventory charts exceeds the 256-entry limit")
        return [], problems
    entries: list[dict[str, object]] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            problems.append(f"chart inventory entry {index} must be an object")
            continue
        if set(raw_entry) != ENTRY_FIELDS:
            problems.append(
                f"chart inventory entry {index} must contain exactly "
                f"{', '.join(sorted(ENTRY_FIELDS))}"
            )
            continue
        entries.append(raw_entry)
    return entries, problems


def validate_inventory(
    *,
    root: Path,
    inventory_path: Path,
    expected_paths: set[str] | None = None,
) -> list[str]:
    """Validate chart provenance, metadata, content, and optional coverage."""
    document, problems = _load_inventory(inventory_path)
    if document is None:
        return problems
    entries, entry_problems = _inventory_entries(document)
    problems.extend(entry_problems)
    if entry_problems:
        return problems

    paths: list[str] = []
    for index, entry in enumerate(entries):
        label = f"chart inventory entry {index}"
        path_value = entry["path"]
        chart_root, path_problem = _safe_relative_path(root, path_value)
        if path_problem:
            problems.append(f"{label} {path_problem}")
            continue
        assert chart_root is not None
        path_text = str(path_value)
        paths.append(path_text)

        repository_problem = _repository_problem(entry["repository"])
        if repository_problem:
            problems.append(f"{label} {repository_problem}")

        name = entry["name"]
        version = entry["version"]
        digest = entry["treeSha256"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            problems.append(f"{label} has an invalid Helm chart name: {name!r}")
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 128
            or any(character.isspace() for character in version)
        ):
            problems.append(f"{label} has an invalid Helm chart version: {version!r}")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            problems.append(f"{label} treeSha256 must be a lowercase SHA-256 digest")

        try:
            metadata = chart_metadata(chart_root)
            actual_digest = chart_tree_sha256(chart_root)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"{label} cannot inspect {path_text}: {exc}")
            continue
        if metadata["name"] != name:
            problems.append(
                f"{label} name {name!r} does not match Chart.yaml {metadata['name']!r}"
            )
        if metadata["version"] != version:
            problems.append(
                f"{label} version {version!r} does not match Chart.yaml "
                f"{metadata['version']!r}"
            )
        if actual_digest != digest:
            problems.append(
                f"{label} treeSha256 does not match {path_text}: "
                f"expected {digest}, actual {actual_digest}"
            )

    if paths != sorted(paths):
        problems.append("chart inventory entries must be sorted by path")
    if len(paths) != len(set(paths)):
        problems.append("chart inventory paths must be unique")
    if expected_paths is not None:
        inventory_paths = set(paths)
        for path in sorted(expected_paths - inventory_paths):
            problems.append(f"consumed local chart is missing from inventory: {path}")
        for path in sorted(inventory_paths - expected_paths):
            problems.append(f"chart inventory contains an unconsumed chart: {path}")
    return problems


def refresh_inventory(*, root: Path, inventory_path: Path) -> list[str]:
    """Refresh metadata and digests while preserving reviewed source URLs."""
    if _is_link_like(inventory_path):
        return [f"refusing to replace linked chart inventory: {inventory_path}"]
    document, problems = _load_inventory(inventory_path)
    if document is None:
        return problems
    entries, entry_problems = _inventory_entries(document)
    problems.extend(entry_problems)
    if entry_problems:
        return problems

    refreshed: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"chart inventory entry {index}"
        path_value = entry["path"]
        chart_root, path_problem = _safe_relative_path(root, path_value)
        if path_problem:
            problems.append(f"{label} {path_problem}")
            continue
        assert chart_root is not None
        path_text = str(path_value)
        if path_text in seen_paths:
            problems.append(f"chart inventory paths must be unique: {path_text}")
            continue
        seen_paths.add(path_text)
        repository = entry["repository"]
        repository_problem = _repository_problem(repository)
        if repository_problem:
            problems.append(f"{label} {repository_problem}")
            continue
        try:
            metadata = chart_metadata(chart_root)
            digest = chart_tree_sha256(chart_root)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"{label} cannot inspect {path_text}: {exc}")
            continue
        refreshed.append(
            {
                "path": path_text,
                "repository": str(repository),
                "name": metadata["name"],
                "version": metadata["version"],
                "treeSha256": digest,
            }
        )

    if problems:
        return problems
    output = {
        "schemaVersion": 1,
        "charts": sorted(refreshed, key=lambda entry: entry["path"]),
    }
    atomic_write_text(
        inventory_path,
        json.dumps(output, indent=2, sort_keys=False) + "\n",
        mode=0o644,
    )
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or refresh committed local Helm chart inventory."
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_INVENTORY,
        help="Inventory JSON path.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh Chart.yaml metadata and deterministic tree digests.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inventory_path = args.inventory
    if not inventory_path.is_absolute():
        inventory_path = ROOT / inventory_path
    try:
        inventory_path.parent.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
    except (OSError, ValueError) as exc:
        print(
            f"Vendored chart inventory path must remain inside the repository: {exc}",
            file=sys.stderr,
        )
        return 1
    if args.refresh:
        problems = refresh_inventory(root=ROOT, inventory_path=inventory_path)
        action = "refresh"
    else:
        problems = validate_inventory(root=ROOT, inventory_path=inventory_path)
        action = "validation"
    if problems:
        print(f"Vendored chart inventory {action} failed:", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1
    print(f"Vendored chart inventory {action} passed: {inventory_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
