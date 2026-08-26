"""Resolve Forgejo's effective static DB_TYPE from rendered Helm values."""

from __future__ import annotations

import re


FORGEJO_NON_POSTGRES_DATABASE_TYPES = frozenset({"mysql", "mssql", "sqlite3"})


def _literal_scalar(raw: str) -> str | None:
    value = re.sub(r"\s+#.*$", "", raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        return None
    return value


def _named_env_value(text: str, name: str) -> tuple[bool, str | None]:
    lines = text.splitlines()
    pattern = re.compile(
        rf"^(?P<indent>\s*)-\s+name:\s*['\"]?{re.escape(name)}['\"]?\s*(?:#.*)?$"
    )
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            matches.append((index, len(match.group("indent"))))
    if not matches:
        return False, None
    if len(matches) != 1:
        return True, None

    start, entry_indent = matches[0]
    for line in lines[start + 1 :]:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent <= entry_indent:
            break
        if stripped.startswith("valueFrom:"):
            return True, None
        if stripped.startswith("value:"):
            return True, _literal_scalar(stripped.split(":", 1)[1])
    return True, None


def _has_nonempty_config_sources(text: str) -> bool:
    lines = text.splitlines()
    pattern = re.compile(r"^(?P<indent>\s*)additionalConfigSources:\s*(?P<value>.*)$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        inline = re.sub(r"\s+#.*$", "", match.group("value")).strip()
        if inline:
            return inline not in {"[]", "{}", "null", "~"}
        base_indent = len(match.group("indent"))
        for child in lines[index + 1 :]:
            stripped = child.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            return len(child) - len(stripped) > base_indent
    return False


def _configured_database_type(text: str) -> str | None:
    matches = re.findall(
        r"(?im)^\s*DB_TYPE:\s*(['\"]?)([A-Za-z0-9_.+-]+)\1\s*(?:#.*)?$",
        text,
    )
    values = {value.lower() for _, value in matches}
    if len(values) != 1:
        return None
    return values.pop()


def effective_forgejo_database_type(text: str) -> str | None:
    """Apply Forgejo chart precedence to statically declared database types."""
    modern_present, modern_type = _named_env_value(text, "FORGEJO__DATABASE__DB_TYPE")
    if modern_present:
        return modern_type.lower() if modern_type else None
    if _has_nonempty_config_sources(text):
        return None
    legacy_present, legacy_type = _named_env_value(text, "GITEA__database__DB_TYPE")
    if legacy_present:
        return legacy_type.lower() if legacy_type else None
    return _configured_database_type(text)
