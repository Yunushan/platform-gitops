"""Configuration environment names consumed by the pinned Forgejo chart."""

from __future__ import annotations

import copy

from forgejo_storage_contract import config_env_key


DEPENDENCY_BINDINGS = {
    ("database", "PASSWD"), ("cache", "HOST"), ("queue", "CONN_STR"),
    ("storage", "MINIO_ACCESS_KEY_ID"), ("storage", "MINIO_SECRET_ACCESS_KEY"),
    ("storage.minio", "MINIO_ACCESS_KEY_ID"), ("storage.minio", "MINIO_SECRET_ACCESS_KEY"),
}


class ConfigEnvironmentError(ValueError):
    """Configuration bindings cannot be migrated unambiguously."""


def normalize_config_env(entries: list[dict]) -> list[dict]:
    """Migrate generated dependency bindings, not backends, hosts, or launcher vars."""
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ConfigEnvironmentError("Expected a Forgejo environment list.")
    result = []
    seen = {}
    for entry in entries:
        cloned = copy.deepcopy(entry)
        name = str(entry.get("name", ""))
        identity = config_env_key(name)
        if identity in DEPENDENCY_BINDINGS:
            section, key = identity
            section = section.upper().replace(".", "_0X2E_").replace("-", "_0X2D_")
            key = key.replace(".", "_0X2E_").replace("-", "_0X2D_")
            cloned["name"] = f"FORGEJO__{section}__{key}"
            if cloned["name"] in seen:
                if seen[cloned["name"]] != cloned:
                    # Neither private literal values nor secret references belong in errors.
                    raise ConfigEnvironmentError("Conflicting legacy and current Forgejo configuration bindings; reconcile the environment entries before repair.")
                continue
            seen[cloned["name"]] = cloned
        result.append(cloned)
    return result
