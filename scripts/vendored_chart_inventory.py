#!/usr/bin/env python3
"""Validate and refresh the reviewed inventory of consumed local Helm charts."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_stream, read_bounded_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "config" / "vendored-charts.json"
INVENTORY_MAX_BYTES = 1 * 1024 * 1024
CHART_METADATA_MAX_BYTES = 1 * 1024 * 1024
CHART_FILE_MAX_BYTES = 64 * 1024 * 1024
CHART_TREE_MAX_BYTES = 256 * 1024 * 1024
CHART_TREE_MAX_FILES = 20_000
CHART_PACKAGE_MAX_BYTES = 256 * 1024 * 1024
CHART_PACKAGE_MAX_FILES = 20_000
CHART_PACKAGE_MAX_MEMBERS = 40_000
HELM_PULL_TIMEOUT_SECONDS = 180
ENTRY_FIELDS = {
    "path",
    "repository",
    "name",
    "version",
    "packageSha256",
    "upstreamTreeSha256",
    "treeSha256",
    "patches",
}
PATCH_FIELDS = {"path", "reason"}
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


def _chart_metadata_from_text(text: str, label: object) -> dict[str, str]:
    """Read top-level chart identity fields from bounded UTF-8 text."""
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
            raise ValueError(f"duplicate top-level {key} in {label}")
        metadata[key] = _clean_yaml_scalar(value)
    if not metadata.get("name") or not metadata.get("version"):
        raise ValueError(f"{label} must declare top-level name and version")
    return metadata


def chart_metadata(chart_root: Path) -> dict[str, str]:
    """Read the top-level name and version from a bounded Chart.yaml."""
    chart_yaml = chart_root / "Chart.yaml"
    chart_yaml_metadata = chart_yaml.lstat()
    if not stat.S_ISREG(chart_yaml_metadata.st_mode) or _is_link_like(chart_yaml):
        raise ValueError(f"chart metadata is not a regular file: {chart_yaml}")
    text = read_bounded_text(chart_yaml, max_bytes=CHART_METADATA_MAX_BYTES)
    return _chart_metadata_from_text(text, chart_yaml)


def _digest_tree_records(records: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, data in sorted(records.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def chart_tree_records(chart_root: Path) -> dict[str, bytes]:
    """Read a bounded regular chart tree keyed by normalized relative path."""
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

    records: dict[str, bytes] = {}
    total_bytes = 0
    for path in sorted(files, key=lambda item: item.relative_to(chart_root).as_posix()):
        data = read_bounded_bytes(path, max_bytes=CHART_FILE_MAX_BYTES)
        total_bytes += len(data)
        if total_bytes > CHART_TREE_MAX_BYTES:
            raise ValueError(
                f"chart tree exceeds the {CHART_TREE_MAX_BYTES}-byte limit: {chart_root}"
            )
        relative = path.relative_to(chart_root).as_posix()
        records[relative] = data
    return records


def chart_tree_sha256(chart_root: Path) -> str:
    """Hash regular chart files by normalized path, length, and exact bytes."""
    return _digest_tree_records(chart_tree_records(chart_root))


def chart_package_record(package_path: Path) -> dict[str, object]:
    """Inspect a bounded Helm package without writing archive members to disk."""
    package_bytes = read_bounded_bytes(
        package_path,
        max_bytes=CHART_PACKAGE_MAX_BYTES,
    )
    package_sha256 = hashlib.sha256(package_bytes).hexdigest()
    records: dict[str, bytes] = {}
    seen_members: set[str] = set()
    chart_root = ""
    member_count = 0
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > CHART_PACKAGE_MAX_MEMBERS:
                    raise ValueError(
                        "chart package exceeds the "
                        f"{CHART_PACKAGE_MAX_MEMBERS}-member limit"
                    )
                raw_name = member.name
                member_path = PurePosixPath(raw_name)
                normalized_name = member_path.as_posix()
                if (
                    not raw_name
                    or "\\" in raw_name
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                    or "." in member_path.parts
                    or raw_name.rstrip("/") != normalized_name
                ):
                    raise ValueError(
                        f"chart package contains an unsafe member path: {raw_name!r}"
                    )
                if normalized_name in seen_members:
                    raise ValueError(
                        f"chart package contains a duplicate member path: {raw_name!r}"
                    )
                seen_members.add(normalized_name)
                if not chart_root:
                    chart_root = member_path.parts[0]
                if member_path.parts[0] != chart_root:
                    raise ValueError("chart package contains more than one top-level root")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(
                        f"chart package contains a non-regular member: {raw_name!r}"
                    )
                if len(member_path.parts) < 2:
                    raise ValueError(
                        f"chart package file is outside the chart root: {raw_name!r}"
                    )
                if member.size < 0 or member.size > CHART_FILE_MAX_BYTES:
                    raise ValueError(
                        f"chart package member exceeds the {CHART_FILE_MAX_BYTES}-byte limit: "
                        f"{raw_name!r}"
                    )
                relative = PurePosixPath(*member_path.parts[1:]).as_posix()
                if relative in records:
                    raise ValueError(
                        f"chart package contains a duplicate member path: {relative!r}"
                    )
                if len(records) >= CHART_PACKAGE_MAX_FILES:
                    raise ValueError(
                        f"chart package exceeds the {CHART_PACKAGE_MAX_FILES}-file limit"
                    )
                total_bytes += member.size
                if total_bytes > CHART_TREE_MAX_BYTES:
                    raise ValueError(
                        f"chart package exceeds the {CHART_TREE_MAX_BYTES}-byte expanded limit"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"chart package member is unreadable: {raw_name!r}")
                data = read_bounded_stream(
                    stream,
                    max_bytes=CHART_FILE_MAX_BYTES,
                    label=f"{package_path}!/{raw_name}",
                )
                if len(data) != member.size:
                    raise ValueError(
                        f"chart package member size does not match metadata: {raw_name!r}"
                    )
                records[relative] = data
    except (tarfile.TarError, EOFError) as exc:
        raise ValueError(f"invalid Helm chart package {package_path}: {exc}") from exc
    chart_yaml = records.get("Chart.yaml")
    if chart_yaml is None:
        raise ValueError(f"chart package is missing Chart.yaml: {package_path}")
    try:
        metadata = _chart_metadata_from_text(
            chart_yaml.decode("utf-8"),
            f"{package_path}!/Chart.yaml",
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"chart package Chart.yaml is not UTF-8: {package_path}") from exc
    if chart_root != metadata["name"]:
        raise ValueError(
            f"chart package root {chart_root!r} does not match Chart.yaml name "
            f"{metadata['name']!r}"
        )
    return {
        "name": metadata["name"],
        "version": metadata["version"],
        "packageSha256": package_sha256,
        "upstreamTreeSha256": _digest_tree_records(records),
        "records": records,
        "path": package_path,
    }


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
    if isinstance(schema_version, bool) or schema_version != 2:
        problems.append("chart inventory schemaVersion must be 2")
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


def _patch_paths(value: object, label: str) -> tuple[set[str], list[str]]:
    problems: list[str] = []
    if not isinstance(value, list):
        return set(), [f"{label} patches must be a list"]
    if len(value) > 128:
        return set(), [f"{label} patches exceeds the 128-entry limit"]
    paths: list[str] = []
    for index, patch in enumerate(value):
        patch_label = f"{label} patch {index}"
        if not isinstance(patch, dict) or set(patch) != PATCH_FIELDS:
            problems.append(
                f"{patch_label} must contain exactly {', '.join(sorted(PATCH_FIELDS))}"
            )
            continue
        path = patch.get("path")
        reason = patch.get("reason")
        if not isinstance(path, str) or not path or len(path) > 1024:
            problems.append(
                f"{patch_label} path must be a non-empty string no longer than 1024 characters"
            )
        else:
            parsed = PurePosixPath(path)
            if (
                parsed.is_absolute()
                or ".." in parsed.parts
                or "." in parsed.parts
                or path != parsed.as_posix()
            ):
                problems.append(
                    f"{patch_label} path must be normalized, relative, and non-escaping"
                )
            else:
                paths.append(path)
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 512
            or "\n" in reason
            or "\r" in reason
        ):
            problems.append(
                f"{patch_label} reason must be one non-empty line no longer than 512 characters"
            )
    if paths != sorted(paths):
        problems.append(f"{label} patch paths must be sorted")
    if len(paths) != len(set(paths)):
        problems.append(f"{label} patch paths must be unique")
    return set(paths), problems


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
    source_records: dict[tuple[str, str, str], tuple[str, str]] = {}
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
        package_digest = entry["packageSha256"]
        upstream_digest = entry["upstreamTreeSha256"]
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
        for field, value in (
            ("packageSha256", package_digest),
            ("upstreamTreeSha256", upstream_digest),
            ("treeSha256", digest),
        ):
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                problems.append(f"{label} {field} must be a lowercase SHA-256 digest")
        patch_paths, patch_problems = _patch_paths(entry["patches"], label)
        problems.extend(patch_problems)
        if (
            isinstance(upstream_digest, str)
            and isinstance(digest, str)
            and SHA256_RE.fullmatch(upstream_digest)
            and SHA256_RE.fullmatch(digest)
        ):
            if patch_paths and upstream_digest == digest:
                problems.append(f"{label} declares patches but its local tree matches upstream")
            if not patch_paths and upstream_digest != digest:
                problems.append(
                    f"{label} local tree differs from upstream but declares no patches"
                )

        source_key = (str(entry["repository"]), str(name), str(version))
        source_value = (str(package_digest), str(upstream_digest))
        previous_source = source_records.setdefault(source_key, source_value)
        if previous_source != source_value:
            problems.append(
                f"{label} duplicates a chart source with different package provenance"
            )

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


def _package_directory_records(
    package_directory: Path,
) -> tuple[dict[tuple[str, str], dict[str, object]], list[str]]:
    problems: list[str] = []
    try:
        metadata = package_directory.lstat()
    except OSError as exc:
        return {}, [f"chart package directory cannot be inspected: {package_directory}: {exc}"]
    if not stat.S_ISDIR(metadata.st_mode) or _is_link_like(package_directory):
        return {}, [f"chart package directory is not a regular directory: {package_directory}"]

    packages: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        package_directory,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        for name in directory_names:
            child = current / name
            child_metadata = child.lstat()
            if not stat.S_ISDIR(child_metadata.st_mode) or _is_link_like(child):
                problems.append(f"chart package directory contains a linked entry: {child}")
        for name in file_names:
            child = current / name
            if child.suffix != ".tgz":
                continue
            child_metadata = child.lstat()
            if not stat.S_ISREG(child_metadata.st_mode) or _is_link_like(child):
                problems.append(f"chart package is not a regular file: {child}")
                continue
            packages.append(child)
            if len(packages) > 256:
                problems.append("chart package directory exceeds the 256-package limit")
                return {}, problems
    if not packages:
        problems.append(f"chart package directory contains no .tgz files: {package_directory}")
        return {}, problems

    records: dict[tuple[str, str], dict[str, object]] = {}
    for package in sorted(packages):
        try:
            record = chart_package_record(package)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"cannot inspect chart package {package}: {exc}")
            continue
        key = (str(record["name"]), str(record["version"]))
        if key in records:
            problems.append(f"duplicate chart package for {key[0]} {key[1]}")
            continue
        records[key] = record
    return records, problems


def verify_package_directory(
    *,
    root: Path,
    inventory_path: Path,
    package_directory: Path,
) -> list[str]:
    """Verify upstream package bytes, trees, and exact local patch declarations."""
    document, problems = _load_inventory(inventory_path)
    if document is None:
        return problems
    entries, entry_problems = _inventory_entries(document)
    problems.extend(entry_problems)
    if entry_problems:
        return problems
    package_records, package_problems = _package_directory_records(package_directory)
    problems.extend(package_problems)
    if package_problems:
        return problems

    expected_keys = {(str(entry["name"]), str(entry["version"])) for entry in entries}
    ambiguous_keys = {
        key
        for key in expected_keys
        if len(
            {
                str(entry["repository"])
                for entry in entries
                if (str(entry["name"]), str(entry["version"])) == key
            }
        )
        != 1
    }
    for name, version in sorted(ambiguous_keys):
        problems.append(
            f"chart package identity is ambiguous across repositories: {name} {version}"
        )
    for name, version in sorted(expected_keys - set(package_records)):
        problems.append(f"required chart package is missing: {name} {version}")
    for name, version in sorted(set(package_records) - expected_keys):
        problems.append(f"chart package is not declared by the inventory: {name} {version}")
    if problems:
        return problems

    for index, entry in enumerate(entries):
        label = f"chart inventory entry {index}"
        key = (str(entry["name"]), str(entry["version"]))
        package = package_records[key]
        if package["packageSha256"] != entry["packageSha256"]:
            problems.append(
                f"{label} packageSha256 does not match downloaded package: {package['path']}"
            )
        if package["upstreamTreeSha256"] != entry["upstreamTreeSha256"]:
            problems.append(
                f"{label} upstreamTreeSha256 does not match downloaded package: "
                f"{package['path']}"
            )
        chart_root, path_problem = _safe_relative_path(root, entry["path"])
        if path_problem:
            problems.append(f"{label} {path_problem}")
            continue
        assert chart_root is not None
        try:
            local_records = chart_tree_records(chart_root)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"{label} cannot inspect local chart tree: {exc}")
            continue
        upstream_records = package["records"]
        assert isinstance(upstream_records, dict)
        changed_paths = {
            path
            for path in set(local_records) | set(upstream_records)
            if local_records.get(path) != upstream_records.get(path)
        }
        declared_paths, patch_problems = _patch_paths(entry["patches"], label)
        problems.extend(patch_problems)
        undeclared = sorted(changed_paths - declared_paths)
        stale = sorted(declared_paths - changed_paths)
        if undeclared:
            problems.append(
                f"{label} has undeclared local chart patches: {', '.join(undeclared)}"
            )
        if stale:
            problems.append(
                f"{label} declares chart patches that do not differ upstream: "
                + ", ".join(stale)
            )
    return problems


def _resolve_helm_executable(value: str) -> str | None:
    """Resolve an explicit Helm path or a binary available on PATH."""
    if not value or len(value) > 4096 or "\0" in value:
        return None
    resolved = shutil.which(value)
    if resolved:
        return resolved
    candidate = Path(value)
    try:
        metadata = candidate.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or _is_link_like(candidate):
        return None
    return str(candidate.resolve(strict=True))


def _subprocess_detail(value: str | bytes | None) -> str:
    """Return one bounded diagnostic line for a failed Helm invocation."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    compact = " ".join(text.split())
    return compact[:4096]


def verify_upstream_packages(
    *,
    root: Path,
    inventory_path: Path,
    helm_bin: str,
) -> list[str]:
    """Download exact chart versions with Helm and verify package provenance."""
    problems = validate_inventory(root=root, inventory_path=inventory_path)
    if problems:
        return problems
    document, load_problems = _load_inventory(inventory_path)
    if document is None:
        return load_problems
    entries, entry_problems = _inventory_entries(document)
    if entry_problems:
        return entry_problems

    executable = _resolve_helm_executable(helm_bin)
    if executable is None:
        return [f"Helm executable was not found or is not a regular file: {helm_bin!r}"]
    try:
        timeout = bounded_timeout_seconds(
            HELM_PULL_TIMEOUT_SECONDS,
            "PLATFORM_HELM_PULL_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        return [str(exc)]

    sources = sorted(
        {
            (str(entry["repository"]), str(entry["name"]), str(entry["version"]))
            for entry in entries
        }
    )
    with tempfile.TemporaryDirectory(prefix="platform-vendored-charts-") as temporary:
        package_directory = Path(temporary)
        for index, (repository, name, version) in enumerate(sources):
            destination = package_directory / f"source-{index:03d}"
            destination.mkdir(mode=0o700)
            if repository.startswith("oci://"):
                reference = f"{repository.rstrip('/')}/{name}"
                command = [
                    executable,
                    "pull",
                    reference,
                    "--version",
                    version,
                    "--destination",
                    str(destination),
                ]
            else:
                command = [
                    executable,
                    "pull",
                    name,
                    "--repo",
                    repository,
                    "--version",
                    version,
                    "--destination",
                    str(destination),
                ]
            try:
                result = run_bounded(
                    command,
                    timeout=timeout,
                    text=True,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                detail = _subprocess_detail(exc.stderr or exc.output)
                suffix = f": {detail}" if detail else ""
                problems.append(
                    f"Helm pull timed out for {name} {version} from {repository}{suffix}"
                )
                continue
            except (BoundedSubprocessError, OSError, UnicodeError, ValueError) as exc:
                problems.append(
                    f"Helm pull failed for {name} {version} from {repository}: {exc}"
                )
                continue
            if result.returncode != 0:
                detail = _subprocess_detail(result.stderr or result.stdout)
                suffix = f": {detail}" if detail else ""
                problems.append(
                    f"Helm pull failed for {name} {version} from {repository} "
                    f"with exit code {result.returncode}{suffix}"
                )

        if problems:
            return problems
        return verify_package_directory(
            root=root,
            inventory_path=inventory_path,
            package_directory=package_directory,
        )


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

    refreshed: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    source_records: dict[tuple[str, str, str], tuple[str, str]] = {}
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
        package_digest = entry["packageSha256"]
        upstream_digest = entry["upstreamTreeSha256"]
        for field, value in (
            ("packageSha256", package_digest),
            ("upstreamTreeSha256", upstream_digest),
        ):
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                problems.append(f"{label} {field} must be a lowercase SHA-256 digest")
        patch_paths, patch_problems = _patch_paths(entry["patches"], label)
        problems.extend(patch_problems)
        if isinstance(upstream_digest, str) and SHA256_RE.fullmatch(upstream_digest):
            if patch_paths and upstream_digest == digest:
                problems.append(f"{label} declares patches but its local tree matches upstream")
            if not patch_paths and upstream_digest != digest:
                problems.append(
                    f"{label} local tree differs from upstream but declares no patches"
                )
        source_key = (
            str(repository),
            metadata["name"],
            metadata["version"],
        )
        source_value = (str(package_digest), str(upstream_digest))
        previous_source = source_records.setdefault(source_key, source_value)
        if previous_source != source_value:
            problems.append(
                f"{label} duplicates a chart source with different package provenance"
            )
        refreshed.append(
            {
                "path": path_text,
                "repository": str(repository),
                "name": metadata["name"],
                "version": metadata["version"],
                "packageSha256": entry["packageSha256"],
                "upstreamTreeSha256": entry["upstreamTreeSha256"],
                "treeSha256": digest,
                "patches": entry["patches"],
            }
        )

    if problems:
        return problems
    output = {
        "schemaVersion": 2,
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
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh Chart.yaml metadata and local deterministic tree digests.",
    )
    action.add_argument(
        "--verify-packages",
        type=Path,
        metavar="DIRECTORY",
        help="Verify downloaded .tgz packages and exact declared local patches.",
    )
    action.add_argument(
        "--verify-upstream",
        action="store_true",
        help="Download exact chart versions with Helm and verify package provenance.",
    )
    action.add_argument(
        "--inspect-package",
        type=Path,
        metavar="PACKAGE",
        help="Print identity and digests for one bounded Helm .tgz package.",
    )
    parser.add_argument(
        "--helm",
        default=os.environ.get("HELM_BIN", "helm"),
        help="Helm executable used by --verify-upstream (default: HELM_BIN or helm).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.inspect_package is not None:
        package_path = args.inspect_package
        if not package_path.is_absolute():
            package_path = Path.cwd() / package_path
        try:
            record = chart_package_record(package_path)
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"Helm package inspection failed: {exc}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "name": record["name"],
                    "version": record["version"],
                    "packageSha256": record["packageSha256"],
                    "upstreamTreeSha256": record["upstreamTreeSha256"],
                },
                indent=2,
            )
        )
        return 0
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
    elif args.verify_packages is not None:
        package_directory = args.verify_packages
        if not package_directory.is_absolute():
            package_directory = Path.cwd() / package_directory
        problems = validate_inventory(root=ROOT, inventory_path=inventory_path)
        if not problems:
            problems = verify_package_directory(
                root=ROOT,
                inventory_path=inventory_path,
                package_directory=package_directory,
            )
        action = "package verification"
    elif args.verify_upstream:
        problems = verify_upstream_packages(
            root=ROOT,
            inventory_path=inventory_path,
            helm_bin=args.helm,
        )
        action = "upstream verification"
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
