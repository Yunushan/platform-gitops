"""Forgejo 15 storage bindings shared by rendering and runtime preflight."""

from __future__ import annotations

import copy
import ipaddress
import re
from urllib.parse import urlsplit


STORAGE_SECTIONS = (
    "storage", "attachment", "lfs", "avatar", "repo-avatar", "repo-archive",
    "packages", "storage.packages", "storage.actions_log", "actions.artifacts",
    "storage.attachments", "storage.lfs", "storage.avatars", "storage.repo-avatars",
    "storage.repo-archive", "storage.actions_artifacts",
)
FILESYSTEM_MODES = {"filesystem", "file", "local", "disk"}
OBJECT_MODES = {"s3", "minio", "object", "object-storage", "object_storage"}


class StorageContractError(ValueError):
    """Storage cannot be repaired without guessing private configuration."""


def config_env_key(name: str) -> tuple[str, str] | None:
    parts = name.upper().split("__")
    if len(parts) != 3 or parts[0] not in {"FORGEJO", "GITEA"}:
        return None
    section = parts[1].replace("_0X2E_", ".").replace("_0X2D_", "-").lower()
    return section, parts[2]


def valid_minio_endpoint(value: str) -> bool:
    # The SDK accepts host[:port], not a URL, userinfo, path, or placeholders.
    if not value or any(char.isspace() for char in value) or any(char in value for char in "/@?#<>\\"):
        return False
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname or ""
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return bool(host) and len(host) <= 253 and all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in host.rstrip(".").split(".")
            )
    except ValueError:
        return False


def minio_sections(config: dict) -> list[str]:
    selected = [
        section for section in STORAGE_SECTIONS
        if str((config.get(section) or {}).get("STORAGE_TYPE", "")).lower() == "minio"
    ]
    if str((config.get("picture") or {}).get("AVATAR_STORAGE_TYPE", "")).lower() == "minio":
        selected.append("picture")
    return selected


def repair_minio_inheritance(config: dict, env: list[dict]) -> bool:
    """Bind only the old generated split configuration to its existing S3 store.

    Forgejo selects [storage.minio] for explicit minio subsystem types. Without
    it, [attachment] is self-contained and does not inherit [storage] settings.
    """
    selected = minio_sections(config)
    if not selected or "storage.minio" in config:
        return False
    common = config.get("storage") or {}
    incomplete = [section for section in selected if section != "storage" and not (
        config.get(section) or {}
    ).get("MINIO_ENDPOINT")]
    if not incomplete:
        return False
    # Adding a named type changes precedence for every minio selector. Never
    # override an independently configured subsystem or its private credentials.
    for section in selected:
        if section == "storage":
            continue
        if any(key.startswith("MINIO_") and key not in {"MINIO_BUCKET", "MINIO_BASE_PATH"}
               for key in config.get(section, {})):
            raise StorageContractError("Mixed per-subsystem MinIO settings require manual reconciliation; no storage was changed.")
    if not valid_minio_endpoint(str(common.get("MINIO_ENDPOINT", ""))) or not common.get("MINIO_BUCKET"):
        raise StorageContractError("MinIO is selected but its shared MINIO_ENDPOINT/MINIO_BUCKET is missing or invalid. Select filesystem explicitly if no S3 service exists.")
    for entry in env:
        key = config_env_key(str(entry.get("name", "")))
        if key and key[0] in set(selected) - {"storage"} and key[1].startswith("MINIO_"):
            raise StorageContractError("Per-subsystem MinIO environment overrides require manual reconciliation.")

    # Named storage types use the prefix verbatim instead of appending a
    # subsystem name. Retain the original generated per-subsystem prefixes.
    if common.get("MINIO_BASE_PATH"):
        raise StorageContractError("A custom global MINIO_BASE_PATH requires manual reconciliation.")
    effective = {}
    for prefix in ("GITEA__", "FORGEJO__"):
        for entry in env:
            key = config_env_key(str(entry.get("name", "")))
            if str(entry.get("name", "")).upper().startswith(prefix) and key and key[0] == "storage" and key[1].startswith("MINIO_"):
                effective[key[1]] = entry
    aliases = []
    for key, entry in effective.items():
        if key == "MINIO_BASE_PATH":
            raise StorageContractError("A dynamic global MINIO_BASE_PATH requires manual reconciliation.")
        cloned = copy.deepcopy(entry)
        cloned["name"] = "FORGEJO__STORAGE_0X2E_MINIO__" + key
        if any(config_env_key(str(item.get("name", ""))) == ("storage.minio", key) for item in env):
            raise StorageContractError("Existing named MinIO environment overrides require manual reconciliation.")
        aliases.append(cloned)
    config["storage.minio"] = {"STORAGE_TYPE": "minio", **{
        key: copy.deepcopy(value) for key, value in common.items()
        if key.startswith("MINIO_") and key != "MINIO_BASE_PATH"
    }}
    env.extend(aliases)
    return True


def select_filesystem_storage(config: dict, env: list[dict]) -> None:
    """Apply an explicit operator choice, retaining all paths and data volumes."""
    for section in STORAGE_SECTIONS:
        config.setdefault(section, {})["STORAGE_TYPE"] = "local"
    config.setdefault("picture", {})["AVATAR_STORAGE_TYPE"] = "local"
    managed_sections = set(STORAGE_SECTIONS) | {"storage.minio", "picture"}
    env[:] = [entry for entry in env if not (
        (key := config_env_key(str(entry.get("name", ""))))
        and key[0] in managed_sections
        and (key[1].startswith("MINIO_") or key[1] in {"STORAGE_TYPE", "AVATAR_STORAGE_TYPE"})
    )]


def validate_minio_config(config: dict) -> None:
    for section in minio_sections(config):
        selected = config.get("storage.minio") if section != "storage" else None
        if selected is None:
            selected = config.get("storage") if section == "picture" else config.get(section)
        selected = selected or {}
        if not valid_minio_endpoint(str(selected.get("MINIO_ENDPOINT", ""))):
            raise StorageContractError(f"MinIO section {section} has no valid effective endpoint; reconcile Forgejo storage values before restarting.")
        if not selected.get("MINIO_BUCKET"):
            raise StorageContractError(f"MinIO section {section} has no configured bucket.")
        if not all(selected.get(key) for key in ("MINIO_ACCESS_KEY_ID", "MINIO_SECRET_ACCESS_KEY")):
            raise StorageContractError(f"MinIO section {section} has no resolved credential pair.")
