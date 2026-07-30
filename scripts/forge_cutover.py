#!/usr/bin/env python3
"""Orchestrate an explicit, fail-closed GitLab-to-Forgejo CI/CD cutover.

This module deliberately sits above ``forge_migration.py``. Repository and
portable metadata migration stay in the existing engine; this command adds the
operational inventory, shadow preparation, verification, activation, and
rollback gates needed to move deployment authority from GitLab CI to
Forgejo/Woodpecker/Harbor/Argo CD.

Nothing in this file runs from bootstrap or validation. Every remote mutation
requires a dedicated subcommand, and source freeze/CI changes additionally
require a proof-digest confirmation and a live-operation environment gate.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import forge_migration as migration
from atomic_file import atomic_write_text
from strict_json import loads_strict_json
from http_transport import (
    HttpTransportPolicyError,
    http_timeout_seconds,
    read_bounded_response,
)


TOOL = "scripts/forge_cutover.py"
PLAN_VERSION = 1
PROOF_VERSION = 1
SUPPORTED_DIRECTION = "gitlab-to-forgejo"
ACCOUNTED_MODES = {"managed", "mapped", "manual", "skipped"}
ACTIVE_GITLAB_PIPELINE_STATES = {
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
    "scheduled",
}
WOODPECKER_ACTIVE_STATES = {"pending", "running", "blocked"}
WOODPECKER_SUCCESS_STATES = {"success"}
WOODPECKER_FAILURE_STATES = {
    "error",
    "failure",
    "killed",
    "declined",
    "cancelled",
    "canceled",
}
DEFAULT_SOURCE_PIPELINE_GLOBS = (
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".gitlab/ci/*.yml",
    ".gitlab/ci/*.yaml",
)
DEFAULT_DESTINATION_PIPELINE_GLOBS = (
    ".woodpecker.yml",
    ".woodpecker.yaml",
    ".woodpecker/*.yml",
    ".woodpecker/*.yaml",
)
SENSITIVE_PROOF_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "password",
    "private_token",
    "secret_value",
    "token",
    "value",
}
_KNOWN_SECRET_VALUES: set[str] = set()


class CutoverError(migration.MigrationError):
    """Raised when cutover safety or verification cannot be proven."""


@dataclass(frozen=True)
class ServiceTarget:
    name: str
    api_url: str
    token_env: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    auth: str = "bearer"


@dataclass(frozen=True)
class CutoverRepo:
    migration: migration.RepoPlan
    raw: dict[str, Any]
    cutover: dict[str, Any]


@dataclass(frozen=True)
class CutoverPlan:
    raw: dict[str, Any]
    repositories: tuple[CutoverRepo, ...]
    services: dict[str, dict[str, Any]]
    activation: dict[str, Any]
    sha256: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CutoverError(f"invalid boolean value: {value!r}")


def int_value(value: Any, default: int, minimum: int = 1) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CutoverError(f"invalid integer value: {value!r}") from exc
    if parsed < minimum:
        raise CutoverError(f"integer value must be at least {minimum}: {parsed}")
    return parsed


def object_value(parent: dict[str, Any], key: str, label: str, required: bool = True) -> dict[str, Any]:
    value = parent.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise CutoverError(f"{label}.{key} must be an object")
    return value


def list_value(parent: dict[str, Any], key: str, label: str) -> list[Any]:
    value = parent.get(key, [])
    if not isinstance(value, list):
        raise CutoverError(f"{label}.{key} must be an array")
    return value


def string_value(parent: dict[str, Any], key: str, label: str, required: bool = True) -> str:
    value = str(parent.get(key) or "").strip()
    if required and not value:
        raise CutoverError(f"{label}.{key} is required")
    return value


def env_name(parent: dict[str, Any], key: str, label: str, required: bool = True) -> str:
    value = string_value(parent, key, label, required=required)
    if value and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise CutoverError(f"{label}.{key} must name an environment variable")
    return value


def require_credential_free_plan(value: Any, path: str = "plan") -> None:
    try:
        migration.require_credential_free_plan(value, path)
    except migration.MigrationError as exc:
        raise CutoverError(str(exc)) from exc


def validate_accounted_entry(entry: Any, label: str, managed_fields: Iterable[str] = ()) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise CutoverError(f"{label} must be an object")
    mode = str(entry.get("mode") or "").strip().lower()
    if mode == "unsupported":
        raise CutoverError(f"{label} is marked unsupported and blocks cutover")
    if mode not in ACCOUNTED_MODES:
        raise CutoverError(
            f"{label}.mode must be one of: {', '.join(sorted(ACCOUNTED_MODES | {'unsupported'}))}"
        )
    if mode in {"manual", "skipped"}:
        if not bool_value(entry.get("accepted"), False):
            raise CutoverError(f"{label} requires accepted=true for mode={mode}")
        if not str(entry.get("reason") or "").strip():
            raise CutoverError(f"{label} requires a reason for mode={mode}")
    if mode == "managed":
        for field in managed_fields:
            if entry.get(field) in (None, "", []):
                raise CutoverError(f"{label}.{field} is required for mode=managed")
    return entry


def validate_unique_sources(entries: list[Any], label: str) -> None:
    sources = [str(entry.get("source") or "") for entry in entries if isinstance(entry, dict)]
    duplicates = sorted({source for source in sources if source and sources.count(source) > 1})
    if duplicates:
        raise CutoverError(f"{label} has duplicate source mappings: {', '.join(duplicates)}")


def validate_pipeline_section(section: dict[str, Any], label: str) -> None:
    if str(section.get("unmapped") or "fail").lower() != "fail":
        raise CutoverError(f"{label}.unmapped must be fail")
    mappings = list_value(section, "mappings", label)
    validate_unique_sources(mappings, f"{label}.mappings")
    for index, mapping in enumerate(mappings):
        checked = validate_accounted_entry(mapping, f"{label}.mappings[{index}]")
        string_value(checked, "source", f"{label}.mappings[{index}]")
        if checked["mode"] in {"managed", "mapped"}:
            destinations = checked.get("destinations")
            if isinstance(destinations, str):
                destinations = [destinations]
            if not isinstance(destinations, list) or not all(str(item).strip() for item in destinations):
                raise CutoverError(f"{label}.mappings[{index}].destinations must be a non-empty array")
    external_includes = list_value(section, "external_includes", label)
    validate_unique_sources(external_includes, f"{label}.external_includes")
    for index, include in enumerate(external_includes):
        checked = validate_accounted_entry(include, f"{label}.external_includes[{index}]")
        string_value(checked, "source", f"{label}.external_includes[{index}]")
    string_value(section, "deployment_gate_marker", label)
    for field in ("source_globs", "destination_globs"):
        patterns = section.get(field)
        if patterns is not None and (
            not isinstance(patterns, list) or not patterns or not all(str(item).strip() for item in patterns)
        ):
            raise CutoverError(f"{label}.{field} must be a non-empty array when configured")


def validate_variable_section(section: dict[str, Any], label: str) -> None:
    if str(section.get("unmapped") or "fail").lower() != "fail":
        raise CutoverError(f"{label}.unmapped must be fail")
    groups = list_value(section, "group_ids", label)
    if not all(str(group).strip() for group in groups):
        raise CutoverError(f"{label}.group_ids must contain non-empty GitLab group IDs or paths")
    for scope_name in ("group_hierarchy", "instance_scope"):
        scope = object_value(section, scope_name, label)
        validate_accounted_entry(scope, f"{label}.{scope_name}")
    mappings = list_value(section, "mappings", label)
    validate_unique_sources(mappings, f"{label}.mappings")
    for index, mapping in enumerate(mappings):
        checked = validate_accounted_entry(
            mapping,
            f"{label}.mappings[{index}]",
            managed_fields=("source", "target_name"),
        )
        string_value(checked, "source", f"{label}.mappings[{index}]")
        if checked["mode"] == "managed":
            if str(checked.get("target") or "woodpecker_secret") != "woodpecker_secret":
                raise CutoverError(
                    f"{label}.mappings[{index}].target only supports woodpecker_secret for managed values"
                )
            events = checked.get("events", [])
            images = checked.get("images", [])
            if not isinstance(events, list) or not isinstance(images, list):
                raise CutoverError(f"{label}.mappings[{index}] events/images must be arrays")


def validate_mapped_section(
    section: dict[str, Any],
    label: str,
    source_field: str,
    managed_fields: Iterable[str] = (),
) -> None:
    if str(section.get("unmapped") or "fail").lower() != "fail":
        raise CutoverError(f"{label}.unmapped must be fail")
    mappings = list_value(section, "mappings", label)
    validate_unique_sources(mappings, f"{label}.mappings")
    for index, mapping in enumerate(mappings):
        checked = validate_accounted_entry(
            mapping,
            f"{label}.mappings[{index}]",
            managed_fields=managed_fields,
        )
        string_value(checked, source_field, f"{label}.mappings[{index}]")


def validate_services(services: dict[str, Any]) -> None:
    for service_name in ("woodpecker", "harbor", "argocd"):
        service = object_value(services, service_name, "services")
        mode = str(service.get("mode") or "managed").lower()
        service["mode"] = mode
        validate_accounted_entry(service, f"services.{service_name}")
        if mode in {"managed", "mapped"}:
            string_value(service, "api_url", f"services.{service_name}")
            auth = str(service.get("auth") or ("basic" if service_name == "harbor" else "bearer"))
            if auth not in {"basic", "bearer", "token"}:
                raise CutoverError(f"services.{service_name}.auth is unsupported: {auth}")
            if service_name == "harbor":
                env_name(service, "username_env", f"services.{service_name}")
                env_name(service, "password_env", f"services.{service_name}")
                string_value(service, "project", f"services.{service_name}")
            else:
                env_name(service, "token_env", f"services.{service_name}")
    woodpecker = services["woodpecker"]
    if woodpecker["mode"] in {"managed", "mapped"}:
        string_value(woodpecker, "shadow_gate_secret", "services.woodpecker")
        int_value(woodpecker.get("canary_timeout_seconds"), 900, minimum=30)
    argocd = services["argocd"]
    if argocd["mode"] in {"managed", "mapped"}:
        apps = list_value(argocd, "applications", "services.argocd")
        if not apps:
            raise CutoverError("services.argocd.applications must not be empty")
        for index, app in enumerate(apps):
            if not isinstance(app, dict):
                raise CutoverError(f"services.argocd.applications[{index}] must be an object")
            string_value(app, "name", f"services.argocd.applications[{index}]")
        names = [str(app["name"]) for app in apps]
        if len(names) != len(set(names)):
            raise CutoverError("services.argocd.applications contains duplicate names")


def validate_activation(activation: dict[str, Any]) -> None:
    if str(activation.get("freeze") or "archive").lower() != "archive":
        raise CutoverError("activation.freeze must be archive")
    env_name(activation, "confirmation_env", "activation")
    env_name(activation, "live_env", "activation")
    env_name(activation, "change_ticket_env", "activation")
    if activation.get("prepare_confirmation_env"):
        env_name(activation, "prepare_confirmation_env", "activation")
    if activation.get("rollback_confirmation_env"):
        env_name(activation, "rollback_confirmation_env", "activation")
    bool_value(activation.get("cancel_active_pipelines"), False)
    int_value(activation.get("max_verification_age_seconds"), 3600, minimum=60)


def parse_cutover_plan(data: dict[str, Any]) -> CutoverPlan:
    require_credential_free_plan(data)
    version = data.get("version", PLAN_VERSION)
    if version != PLAN_VERSION:
        raise CutoverError(f"plan.version must be {PLAN_VERSION}")
    if str(data.get("direction") or "").strip().lower() != SUPPORTED_DIRECTION:
        raise CutoverError(f"plan.direction must be {SUPPORTED_DIRECTION}")
    _direction, migration_repos = migration.parse_plan(data)
    raw_repositories = data["repositories"]
    cutover_repositories: list[CutoverRepo] = []
    for index, (repo, raw_repo) in enumerate(zip(migration_repos, raw_repositories, strict=True)):
        label = f"repositories[{index}].cutover"
        cutover = object_value(raw_repo, "cutover", f"repositories[{index}]")
        expected_surfaces = {
            "pipelines",
            "variables",
            "schedules",
            "runner_tags",
            "protections",
            "integrations",
        }
        unexpected_surfaces = sorted(set(cutover).difference(expected_surfaces))
        if unexpected_surfaces:
            raise CutoverError(
                f"{label} has unsupported surface(s): {', '.join(unexpected_surfaces)}"
            )
        for side, token_env in (
            ("source", repo.source_token_env),
            ("destination", repo.destination_token_env),
        ):
            if not token_env:
                raise CutoverError(f"repositories[{index}].{side}.token_env is required")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env):
                raise CutoverError(
                    f"repositories[{index}].{side}.token_env must name an environment variable"
                )
        validate_pipeline_section(object_value(cutover, "pipelines", label), f"{label}.pipelines")
        validate_variable_section(object_value(cutover, "variables", label), f"{label}.variables")
        validate_mapped_section(
            object_value(cutover, "schedules", label),
            f"{label}.schedules",
            "source",
            managed_fields=("target_name", "schedule", "branch"),
        )
        validate_mapped_section(
            object_value(cutover, "runner_tags", label),
            f"{label}.runner_tags",
            "source",
        )
        for mapping_index, mapping_item in enumerate(cutover["runner_tags"].get("mappings", [])):
            if str(mapping_item["mode"]).lower() in {"managed", "mapped"}:
                labels = mapping_item.get("target_labels")
                if not isinstance(labels, dict) or not labels:
                    raise CutoverError(
                        f"{label}.runner_tags.mappings[{mapping_index}].target_labels "
                        "must be a non-empty object"
                    )
        validate_mapped_section(
            object_value(cutover, "protections", label),
            f"{label}.protections",
            "source",
        )
        for mapping_index, mapping_item in enumerate(cutover["protections"].get("mappings", [])):
            mode = str(mapping_item["mode"]).lower()
            if mode in {"managed", "mapped"}:
                if not str(mapping_item.get("target") or mapping_item.get("target_pattern") or "").strip():
                    raise CutoverError(
                        f"{label}.protections.mappings[{mapping_index}] requires target or target_pattern"
                    )
            if mode == "managed":
                settings = mapping_item.get("settings")
                if not isinstance(settings, dict) or not settings:
                    raise CutoverError(
                        f"{label}.protections.mappings[{mapping_index}].settings "
                        "must be a non-empty object for mode=managed"
                    )
        validate_mapped_section(
            object_value(cutover, "integrations", label),
            f"{label}.integrations",
            "source",
        )
        for mapping_index, mapping_item in enumerate(cutover["integrations"].get("mappings", [])):
            mode = str(mapping_item["mode"]).lower()
            if mode in {"managed", "mapped"}:
                string_value(
                    mapping_item,
                    "target",
                    f"{label}.integrations.mappings[{mapping_index}]",
                )
                if mapping_item.get("target_host"):
                    string_value(
                        mapping_item,
                        "target_host",
                        f"{label}.integrations.mappings[{mapping_index}]",
                    )
            if mode == "managed" and mapping_item.get("target") != "woodpecker_webhook":
                raise CutoverError(
                    f"{label}.integrations.mappings[{mapping_index}] only supports "
                    "target=woodpecker_webhook for mode=managed"
                )
        cutover_repositories.append(CutoverRepo(repo, raw_repo, cutover))
    services = object_value(data, "services", "plan")
    validate_services(services)
    activation = object_value(data, "activation", "plan")
    validate_activation(activation)
    return CutoverPlan(
        raw=data,
        repositories=tuple(cutover_repositories),
        services=services,
        activation=activation,
        sha256=canonical_digest(data),
    )


def load_cutover_plan(path: Path) -> CutoverPlan:
    return parse_cutover_plan(migration.load_plan(path))


def register_secret(value: str | None) -> str:
    if value:
        _KNOWN_SECRET_VALUES.add(value)
    return value or ""


def env_credential(name: str | None, label: str) -> str:
    if not name:
        return ""
    value = os.environ.get(name, "")
    if not value:
        raise CutoverError(f"{label} requires environment variable {name}")
    return register_secret(value)


def require_provider_credentials(plan: CutoverPlan) -> None:
    for repo in plan.repositories:
        env_credential(repo.migration.source_token_env, f"{repo.migration.name} GitLab API token")
        env_credential(
            repo.migration.destination_token_env,
            f"{repo.migration.name} Forgejo API token",
        )


def redact_text(value: str) -> str:
    redacted = migration.redact_url(value)
    for known_value in sorted(_KNOWN_SECRET_VALUES, key=len, reverse=True):
        if known_value:
            redacted = redacted.replace(known_value, "<redacted>")
    redacted = re.sub(
        r"(?i)(authorization|private-token|token|password|secret)([\"'=:\s]+)[^\s\",}]+",
        r"\1\2<redacted>",
        redacted,
    )
    return redacted


def sanitize_for_proof(value: Any, key: str = "") -> Any:
    normalized_key = key.strip().lower()
    if normalized_key in SENSITIVE_PROOF_KEYS:
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(child_key): sanitize_for_proof(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [sanitize_for_proof(child, key) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def service_target(name: str, config: dict[str, Any]) -> ServiceTarget:
    return ServiceTarget(
        name=name,
        api_url=str(config.get("api_url") or "").rstrip("/"),
        token_env=str(config.get("token_env") or "") or None,
        username_env=str(config.get("username_env") or "") or None,
        password_env=str(config.get("password_env") or "") or None,
        auth=str(config.get("auth") or ("basic" if name == "harbor" else "bearer")).lower(),
    )


def service_headers(target: ServiceTarget) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "platform-gitops-forge-cutover",
    }
    if target.auth == "basic":
        username = env_credential(target.username_env, f"{target.name} username")
        auth_credential = env_credential(target.password_env, f"{target.name} password")
        encoded = base64.b64encode(f"{username}:{auth_credential}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    elif target.auth in {"bearer", "token"}:
        auth_credential = env_credential(target.token_env, f"{target.name} token")
        prefix = "Bearer" if target.auth == "bearer" else "token"
        headers["Authorization"] = f"{prefix} {auth_credential}"
    else:
        raise CutoverError(f"unsupported auth mode for {target.name}: {target.auth}")
    return headers


def service_request(
    target: ServiceTarget,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
    return_status: bool = False,
) -> Any:
    url = f"{target.api_url}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, headers=service_headers(target), method=method)
    try:
        timeout = http_timeout_seconds()
    except HttpTransportPolicyError as exc:
        raise CutoverError(str(exc)) from None
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            payload = read_bounded_response(response).decode("utf-8")
    except HTTPError as exc:
        try:
            payload = read_bounded_response(exc).decode("utf-8", errors="replace")
        except HttpTransportPolicyError as policy_error:
            raise CutoverError(
                f"{method} {migration.redact_url(url)} response rejected: {policy_error}"
            ) from policy_error
        if exc.code not in expected:
            raise CutoverError(
                f"{method} {migration.redact_url(url)} failed with HTTP {exc.code}: "
                f"{redact_text(payload[:500])}"
            ) from exc
        status = exc.code
    except (HttpTransportPolicyError, UnicodeDecodeError) as exc:
        raise CutoverError(
            f"{method} {migration.redact_url(url)} response rejected: {exc}"
        ) from exc
    except URLError as exc:
        raise CutoverError(f"{method} {migration.redact_url(url)} failed: {exc}") from exc
    if status not in expected:
        raise CutoverError(
            f"{method} {migration.redact_url(url)} returned HTTP {status}: {redact_text(payload[:500])}"
        )
    try:
        decoded: Any = loads_strict_json(payload) if payload else {}
    except json.JSONDecodeError as exc:
        raise CutoverError(f"{method} {migration.redact_url(url)} returned invalid JSON") from exc
    return (status, decoded) if return_status else decoded


def gitlab_list(target: migration.ApiTarget, path: str, query: dict[str, Any] | None = None) -> list[Any]:
    result: list[Any] = []
    page = 1
    while True:
        request_query = {**(query or {}), "per_page": 100, "page": page}
        payload = migration.api_request(target, "GET", path, query=request_query)
        if not isinstance(payload, list):
            raise CutoverError(f"GitLab list endpoint {path} returned a non-array response")
        result.extend(payload)
        if len(payload) < 100:
            return result
        page += 1


def repo_base(repo: CutoverRepo, side: str) -> tuple[migration.ApiTarget, str]:
    target = migration.api_target(repo.migration, side)
    return target, migration.repo_api_base(target)


def match_globs(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def gitlab_file_text(repo: CutoverRepo, path: str, branch: str) -> str:
    target, base = repo_base(repo, "source")
    payload = migration.api_request(
        target,
        "GET",
        f"{base}/repository/files/{quote(path, safe='')}",
        query={"ref": branch},
    )
    if not isinstance(payload, dict):
        raise CutoverError(f"{repo.migration.name}: GitLab returned invalid content for {path}")
    content = str(payload.get("content") or "")
    if str(payload.get("encoding") or "base64").lower() != "base64" or not content:
        raise CutoverError(
            f"{repo.migration.name}: GitLab did not return base64 file content for {path}"
        )
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CutoverError(
            f"{repo.migration.name}: GitLab returned unreadable pipeline content for {path}"
        ) from exc


def yaml_scalar(value: str) -> str:
    scalar = value.strip().rstrip(",").strip()
    if scalar.startswith("[") and scalar.endswith("]"):
        scalar = scalar[1:-1].strip()
    return scalar.strip("'\"")


def yaml_scalar_list(value: str) -> list[str]:
    scalar = value.strip().rstrip(",").strip()
    if scalar.startswith("[") and scalar.endswith("]"):
        scalar = scalar[1:-1]
    return [
        item.strip().strip("'\"")
        for item in scalar.split(",")
        if item.strip().strip("'\"")
    ]


def gitlab_external_includes(content: str) -> list[str]:
    if not re.search(r"(?m)^\s*include\s*:", content):
        return []
    includes: set[str] = set()
    for match in re.finditer(
        r"(?m)^\s*(?:-\s*)?(remote|project|template|component)\s*:\s*([^#\r\n]+)",
        content,
    ):
        kind = match.group(1).lower()
        scalar = yaml_scalar(match.group(2))
        if scalar:
            includes.add(f"{kind}:{scalar}")
    for match in re.finditer(r"(?m)^\s*include\s*:\s*([^#\r\n]+)", content):
        scalar = yaml_scalar(match.group(1))
        for url in re.findall(r"https?://[^\s,'\"\]]+", scalar):
            includes.add(f"remote:{url}")
    return sorted(includes)


def gitlab_local_includes(content: str) -> list[str]:
    if not re.search(r"(?m)^\s*include\s*:", content):
        return []
    includes: set[str] = set()
    for match in re.finditer(r"(?m)^\s*(?:-\s*)?local\s*:\s*([^#\r\n]+)", content):
        includes.update(yaml_scalar_list(match.group(1)))
    for match in re.finditer(r"(?m)^\s*include\s*:\s*([^#\r\n]+)", content):
        for scalar in yaml_scalar_list(match.group(1)):
            if "://" not in scalar and not scalar.startswith(("{", "$")):
                includes.add(scalar)
    return sorted(include.lstrip("/") for include in includes if include.strip("/"))


def inventory_gitlab_pipeline_files(repo: CutoverRepo, project: dict[str, Any]) -> list[dict[str, Any]]:
    target, base = repo_base(repo, "source")
    pipelines = object_value(repo.cutover, "pipelines", f"{repo.migration.name}.cutover")
    patterns = pipelines.get("source_globs") or list(DEFAULT_SOURCE_PIPELINE_GLOBS)
    if not isinstance(patterns, list) or not all(str(pattern).strip() for pattern in patterns):
        raise CutoverError(f"{repo.migration.name}: pipelines.source_globs must be a non-empty array")
    branch = str(project.get("default_branch") or "main")
    tree = gitlab_list(
        target,
        f"{base}/repository/tree",
        {"recursive": "true", "ref": branch},
    )
    tree_files = {
        str(item.get("path") or ""): item
        for item in tree
        if item.get("type") == "blob" and str(item.get("path") or "")
    }
    files = [
        {
            "path": str(item.get("path") or ""),
            "sha": str(item.get("id") or ""),
            "source": "gitlab",
        }
        for item in tree_files.values()
        if match_globs(str(item.get("path") or ""), patterns)
    ]
    ci_path = str(project.get("ci_config_path") or ".gitlab-ci.yml")
    if ci_path and ci_path not in {item["path"] for item in files}:
        files.append({"path": ci_path, "sha": "", "source": "gitlab-project-setting", "missing": True})
    files_by_path = {str(item["path"]): item for item in files}
    pending = sorted(files_by_path)
    visited: set[str] = set()
    while pending:
        path = pending.pop(0)
        if path in visited:
            continue
        visited.add(path)
        item = files_by_path[path]
        if item.get("missing"):
            item["external_includes"] = []
            item["local_includes"] = []
            item["unresolved_local_includes"] = [path]
            continue
        content = gitlab_file_text(repo, path, branch)
        item["external_includes"] = gitlab_external_includes(content)
        local_includes = gitlab_local_includes(content)
        item["local_includes"] = local_includes
        unresolved: list[str] = []
        for include in local_includes:
            matches = sorted(
                candidate
                for candidate in tree_files
                if fnmatch.fnmatchcase(candidate, include)
            )
            if not matches:
                unresolved.append(include)
                continue
            for matched_path in matches:
                if matched_path not in files_by_path:
                    tree_item = tree_files[matched_path]
                    files_by_path[matched_path] = {
                        "path": matched_path,
                        "sha": str(tree_item.get("id") or ""),
                        "source": "gitlab-local-include",
                    }
                if matched_path not in visited:
                    pending.append(matched_path)
        item["unresolved_local_includes"] = sorted(unresolved)
    return sorted(files_by_path.values(), key=lambda item: item["path"])


def inventory_forgejo_pipeline_files(repo: CutoverRepo, default_branch: str) -> list[dict[str, Any]]:
    target, base = repo_base(repo, "destination")
    pipelines = object_value(repo.cutover, "pipelines", f"{repo.migration.name}.cutover")
    patterns = pipelines.get("destination_globs") or list(DEFAULT_DESTINATION_PIPELINE_GLOBS)
    tree = migration.api_request(
        target,
        "GET",
        f"{base}/git/trees/{quote(default_branch, safe='')}",
        query={"recursive": "true"},
    )
    entries = tree.get("tree", []) if isinstance(tree, dict) else []
    files = [
        {
            "path": str(item.get("path") or ""),
            "sha": str(item.get("sha") or ""),
            "source": "forgejo",
        }
        for item in entries
        if str(item.get("type") or "") in {"blob", "file"}
        and match_globs(str(item.get("path") or ""), patterns)
    ]
    return sorted(files, key=lambda item: item["path"])


def forgejo_file_text(repo: CutoverRepo, path: str, branch: str) -> str:
    target, base = repo_base(repo, "destination")
    payload = migration.api_request(
        target,
        "GET",
        f"{base}/contents/{quote(path, safe='/')}",
        query={"ref": branch},
    )
    if not isinstance(payload, dict):
        raise CutoverError(f"{repo.migration.name}: Forgejo returned invalid content for {path}")
    encoding = str(payload.get("encoding") or "base64").lower()
    content = str(payload.get("content") or "")
    if encoding != "base64" or not content:
        raise CutoverError(
            f"{repo.migration.name}: Forgejo did not return base64 file content for {path}"
        )
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CutoverError(
            f"{repo.migration.name}: Forgejo returned unreadable workflow content for {path}"
        ) from exc


def account_pipeline_files(
    repo: CutoverRepo,
    source_files: list[dict[str, Any]],
    destination_files: list[dict[str, Any]],
    destination_branch: str | None = None,
    verify_destination: bool = True,
) -> dict[str, Any]:
    section = repo.cutover["pipelines"]
    source_by_path = {item["path"]: item for item in source_files}
    destination_by_path = {item["path"]: item for item in destination_files}
    accounted_sources: set[str] = set()
    mapped: list[dict[str, Any]] = []
    verified = True
    marker = str(section["deployment_gate_marker"])
    source_mapping_counts = {path: 0 for path in source_by_path}
    detected_includes = sorted(
        {
            str(include)
            for source_file in source_files
            for include in source_file.get("external_includes", [])
        }
    )
    unresolved_local_includes = sorted(
        {
            f"{source_file['path']}->{include}"
            for source_file in source_files
            for include in source_file.get("unresolved_local_includes", [])
        }
    )
    include_plan = {
        str(item.get("source") or ""): item
        for item in section.get("external_includes", [])
    }
    unaccounted_includes = sorted(set(detected_includes).difference(include_plan))
    stale_include_mappings = sorted(set(include_plan).difference(detected_includes))
    include_results = [
        {
            "source": source,
            "mode": str(include_plan[source]["mode"]).lower(),
            "verified": True,
        }
        for source in detected_includes
        if source in include_plan
    ]
    for mapping_item in section.get("mappings", []):
        mode = str(mapping_item["mode"]).lower()
        source_pattern = str(mapping_item.get("source") or "")
        matched_sources = sorted(path for path in source_by_path if fnmatch.fnmatchcase(path, source_pattern))
        for matched_source in matched_sources:
            source_mapping_counts[matched_source] += 1
        destinations = mapping_item.get("destinations", [])
        if isinstance(destinations, str):
            destinations = [destinations]
        missing_destinations = [path for path in destinations if path not in destination_by_path]
        gate_checks: list[dict[str, Any]] = []
        if mode in {"managed", "mapped"} and destination_branch and verify_destination:
            for destination_path in destinations:
                marker_present = False
                if destination_path not in missing_destinations:
                    marker_present = marker in forgejo_file_text(
                        repo,
                        str(destination_path),
                        destination_branch,
                    )
                gate_checks.append(
                    {
                        "path": destination_path,
                        "marker_present": marker_present,
                    }
                )
        mapping_verified = bool(matched_sources)
        if mode in {"managed", "mapped"} and verify_destination:
            mapping_verified = (
                mapping_verified
                and not missing_destinations
                and bool(gate_checks)
                and all(item["marker_present"] for item in gate_checks)
            )
        accounted_sources.update(matched_sources)
        verified = verified and mapping_verified
        mapped.append(
            {
                "source": source_pattern,
                "destinations": list(destinations),
                "mode": mode,
                "matched_sources": matched_sources,
                "missing_destinations": missing_destinations,
                "deployment_gate_checks": gate_checks,
                "verified": mapping_verified,
            }
        )
    unaccounted = sorted(set(source_by_path).difference(accounted_sources))
    ambiguous_sources = sorted(
        path for path, mapping_count in source_mapping_counts.items() if mapping_count > 1
    )
    if (
        unaccounted
        or ambiguous_sources
        or unresolved_local_includes
        or unaccounted_includes
        or stale_include_mappings
    ):
        verified = False
    return {
        "source_files": source_files,
        "destination_files": destination_files,
        "mappings": mapped,
        "unaccounted_source_files": unaccounted,
        "ambiguous_source_files": ambiguous_sources,
        "unresolved_local_includes": unresolved_local_includes,
        "external_includes": {
            "detected": detected_includes,
            "accounted": include_results,
            "unaccounted": unaccounted_includes,
            "stale_mappings": stale_include_mappings,
            "verified": not unaccounted_includes and not stale_include_mappings,
        },
        "deployment_gate_marker": marker,
        "destination_verification_deferred": not verify_destination,
        "verified": verified,
    }


def variable_identity(scope: str, variable: dict[str, Any]) -> str:
    key = str(variable.get("key") or "")
    environment_scope = str(variable.get("environment_scope") or "*")
    return f"{scope}:{key}:{environment_scope}"


def list_gitlab_variables(
    repo: CutoverRepo,
    include_values: bool,
    project: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    target, base = repo_base(repo, "source")
    section = repo.cutover["variables"]
    if project is None:
        project = migration.api_request(target, "GET", base)
    variables: list[dict[str, Any]] = []
    for raw in gitlab_list(target, f"{base}/variables"):
        item = dict(raw)
        item["source_scope"] = "project"
        variables.append(item)
    groups = {str(group) for group in section.get("group_ids", [])}
    hierarchy_mode = str(section["group_hierarchy"]["mode"]).lower()
    namespace = project.get("namespace") or {}
    if hierarchy_mode in {"managed", "mapped"} and str(namespace.get("kind") or "").lower() == "group":
        full_path = str(namespace.get("full_path") or namespace.get("path") or "").strip("/")
        parts = [part for part in full_path.split("/") if part]
        groups.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    for group_id in sorted(groups):
        group_path = f"groups/{quote(group_id, safe='')}/variables"
        for raw in gitlab_list(target, group_path):
            item = dict(raw)
            item["source_scope"] = f"group:{group_id}"
            variables.append(item)
    instance_mode = str(section["instance_scope"]["mode"]).lower()
    if instance_mode in {"managed", "mapped"}:
        for raw in gitlab_list(target, "admin/ci/variables"):
            item = dict(raw)
            item["source_scope"] = "instance"
            variables.append(item)
    if include_values:
        for item in variables:
            register_secret(str(item.get("value") or ""))
    return variables


def account_variables(repo: CutoverRepo, variables: list[dict[str, Any]]) -> dict[str, Any]:
    mappings = repo.cutover["variables"].get("mappings", [])
    mapping_by_source = {str(item.get("source") or ""): item for item in mappings}
    results: list[dict[str, Any]] = []
    unaccounted: list[str] = []
    verified = True
    for variable in variables:
        scope = str(variable.get("source_scope") or "project")
        identity = variable_identity(scope, variable)
        short_identity = f"{scope}:{variable.get('key')}"
        mapping_item = mapping_by_source.get(identity) or mapping_by_source.get(short_identity)
        if not mapping_item:
            unaccounted.append(identity)
            verified = False
            mode = "unaccounted"
            target_name = ""
        else:
            mode = str(mapping_item["mode"]).lower()
            target_name = str(mapping_item.get("target_name") or "")
            if mode == "managed":
                if variable.get("variable_type") == "file" and not bool_value(
                    mapping_item.get("file_semantics_acknowledged"), False
                ):
                    verified = False
                if str(variable.get("environment_scope") or "*") != "*" and not bool_value(
                    mapping_item.get("environment_scope_acknowledged"), False
                ):
                    verified = False
                if bool_value(variable.get("protected"), False) and not bool_value(
                    mapping_item.get("protected_ref_policy_acknowledged"), False
                ):
                    verified = False
        results.append(
            {
                "identity": identity,
                "key": str(variable.get("key") or ""),
                "source_scope": scope,
                "variable_type": str(variable.get("variable_type") or "env_var"),
                "environment_scope": str(variable.get("environment_scope") or "*"),
                "protected": bool_value(variable.get("protected"), False),
                "masked": bool_value(variable.get("masked"), False),
                "hidden": bool_value(variable.get("hidden"), False),
                "raw": bool_value(variable.get("raw"), False),
                "mode": mode,
                "target_name": target_name,
                "configured": bool(variable.get("value")),
            }
        )
    return {
        "items": sorted(results, key=lambda item: item["identity"]),
        "unaccounted": sorted(unaccounted),
        "verified": verified and not unaccounted,
    }


def source_key(item: dict[str, Any], candidates: Iterable[str]) -> str:
    for key in candidates:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def account_named_surface(
    items: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    candidates: Iterable[str],
    label: str,
) -> dict[str, Any]:
    mapping_by_source = {str(item.get("source") or ""): item for item in mappings}
    results: list[dict[str, Any]] = []
    unaccounted: list[str] = []
    for item in items:
        key = source_key(item, candidates)
        mapping_item = mapping_by_source.get(key)
        if not mapping_item:
            unaccounted.append(key or "<unnamed>")
            results.append(
                {
                    "source": key,
                    "source_metadata": sanitize_for_proof(item),
                    "mode": "unaccounted",
                }
            )
            continue
        results.append(
            {
                "source": key,
                "source_metadata": sanitize_for_proof(item),
                "mode": str(mapping_item["mode"]).lower(),
                "target": sanitize_for_proof(
                    {k: v for k, v in mapping_item.items() if k not in {"source", "mode", "reason", "accepted"}}
                ),
            }
        )
    return {
        "surface": label,
        "items": sorted(results, key=lambda item: str(item.get("source") or "")),
        "unaccounted": sorted(unaccounted),
        "verified": not unaccounted,
    }


def discover_repository(repo: CutoverRepo, verify_destination: bool = False) -> dict[str, Any]:
    source, source_base = repo_base(repo, "source")
    destination, destination_base = repo_base(repo, "destination")
    project = migration.api_request(source, "GET", source_base)
    destination_status, destination_project = migration.api_request(
        destination,
        "GET",
        destination_base,
        expected=(200, 404),
        return_status=True,
    )
    destination_exists = destination_status == 200
    if destination_status == 404 and repo.migration.destination_create != "required":
        raise CutoverError(
            f"{repo.migration.name}: Forgejo destination is absent and destination.create is not required"
        )
    default_branch = str(destination_project.get("default_branch") or project.get("default_branch") or "main")
    source_pipelines = inventory_gitlab_pipeline_files(repo, project)
    destination_pipelines = (
        inventory_forgejo_pipeline_files(repo, default_branch)
        if destination_exists and verify_destination
        else []
    )
    pipeline_result = account_pipeline_files(
        repo,
        source_pipelines,
        destination_pipelines,
        default_branch,
        verify_destination=verify_destination,
    )
    variables = account_variables(
        repo,
        list_gitlab_variables(repo, include_values=False, project=project),
    )
    schedules_raw = gitlab_list(source, f"{source_base}/pipeline_schedules")
    schedules = account_named_surface(
        schedules_raw,
        repo.cutover["schedules"].get("mappings", []),
        ("description", "id"),
        "schedules",
    )
    runners_raw = gitlab_list(source, f"{source_base}/runners")
    runner_tags_raw = sorted(
        {
            str(tag)
            for runner in runners_raw
            for tag in (runner.get("tag_list") or [])
            if str(tag).strip()
        }
    )
    runner_tags = account_named_surface(
        [{"tag": tag} for tag in runner_tags_raw],
        repo.cutover["runner_tags"].get("mappings", []),
        ("tag",),
        "runner_tags",
    )
    protections_raw = gitlab_list(source, f"{source_base}/protected_branches")
    protections = account_named_surface(
        protections_raw,
        repo.cutover["protections"].get("mappings", []),
        ("name",),
        "protections",
    )
    hooks_raw = gitlab_list(source, f"{source_base}/hooks")
    mirrors_raw = gitlab_list(source, f"{source_base}/remote_mirrors")
    integration_inventory: list[dict[str, Any]] = []
    for hook in hooks_raw:
        identity = source_key(hook, ("name", "url", "id"))
        integration_inventory.append(
            {
                **hook,
                "cutover_key": f"hook:{identity}",
                "cutover_kind": "project_hook",
            }
        )
    for mirror in mirrors_raw:
        identity = source_key(mirror, ("url", "id"))
        integration_inventory.append(
            {
                **mirror,
                "cutover_key": f"remote_mirror:{identity}",
                "cutover_kind": "remote_mirror",
            }
        )
    integrations = account_named_surface(
        integration_inventory,
        repo.cutover["integrations"].get("mappings", []),
        ("cutover_key",),
        "integrations",
    )
    source_state = {
        "project_id": project.get("id"),
        "path_with_namespace": project.get("path_with_namespace"),
        "default_branch": project.get("default_branch"),
        "archived": bool_value(project.get("archived"), False),
        "builds_access_level": str(project.get("builds_access_level") or "enabled"),
        "schedules": [
            {
                "id": schedule.get("id"),
                "description": schedule.get("description"),
                "active": bool_value(schedule.get("active"), False),
            }
            for schedule in schedules_raw
        ],
    }
    verified = all(
        surface["verified"]
        for surface in (pipeline_result, variables, schedules, runner_tags, protections, integrations)
    )
    return {
        "name": repo.migration.name,
        "source_url": migration.redact_url(repo.migration.source_url),
        "destination_url": migration.redact_url(repo.migration.destination_url),
        "source_state": source_state,
        "destination": {
            "exists": destination_exists,
            "id": destination_project.get("id"),
            "full_name": destination_project.get("full_name"),
            "default_branch": default_branch,
        },
        "pipelines": pipeline_result,
        "variables": variables,
        "schedules": schedules,
        "runner_tags": runner_tags,
        "protections": protections,
        "integrations": integrations,
        "verified": verified,
    }


def proof_base(plan: CutoverPlan, command: str, repositories: list[dict[str, Any]]) -> dict[str, Any]:
    proof = {
        "version": PROOF_VERSION,
        "tool": TOOL,
        "command": command,
        "generated_at": utc_now(),
        "plan_sha256": plan.sha256,
        "direction": SUPPORTED_DIRECTION,
        "repositories": sanitize_for_proof(repositories),
        "verified": all(bool(item.get("verified")) for item in repositories),
    }
    return proof


def write_proof(path: Path | None, proof: dict[str, Any]) -> dict[str, Any]:
    safe_proof = sanitize_for_proof(proof)
    safe_proof["proof_sha256"] = migration.proof_digest(safe_proof)
    text = json.dumps(safe_proof, indent=2, sort_keys=True) + "\n"
    if path:
        try:
            atomic_write_text(path, text)
        except OSError as exc:
            raise CutoverError(f"could not write proof {path}: {exc}") from exc
    else:
        print(text, end="")
    return safe_proof


def load_integrity_proof(
    path: Path,
    plan: CutoverPlan,
    expected_commands: Iterable[str],
) -> dict[str, Any]:
    proof = migration.load_plan(path)
    claimed = str(proof.get("proof_sha256") or "")
    actual = migration.proof_digest(proof)
    if not claimed or claimed != actual:
        raise CutoverError(f"{path}: proof integrity verification failed")
    accepted_commands = set(expected_commands)
    if proof.get("command") not in accepted_commands:
        raise CutoverError(
            f"{path}: expected one of {sorted(accepted_commands)}, got {proof.get('command')}"
        )
    if proof.get("plan_sha256") != plan.sha256:
        raise CutoverError(f"{path}: proof was produced from a different plan")
    return proof


def load_verified_proof(path: Path, plan: CutoverPlan, expected_command: str) -> dict[str, Any]:
    proof = load_integrity_proof(path, plan, (expected_command,))
    if proof.get("verified") is not True:
        raise CutoverError(f"{path}: proof is not verified")
    return proof


def write_activation_checkpoint(
    path: Path,
    plan: CutoverPlan,
    verification: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
    authority_attempted: set[str],
    state: str,
    rollback_results: list[dict[str, Any]] | None = None,
    activation_proof_sha256: str = "",
) -> dict[str, Any]:
    repositories = []
    for repo in plan.repositories:
        name = repo.migration.name
        repositories.append(
            {
                "name": name,
                "source_before": snapshots.get(name),
                "destination_authority_attempted": name in authority_attempted,
                "verified": True,
            }
        )
    checkpoint = proof_base(plan, "activation-checkpoint", repositories)
    checkpoint["verified"] = False
    checkpoint["checkpoint_state"] = state
    checkpoint["recovery_required"] = state not in {
        "completed",
        "automatic-rollback-complete",
        "manual-rollback-complete",
    }
    checkpoint["verification_proof_sha256"] = verification.get("proof_sha256")
    checkpoint["change_ticket"] = os.environ.get(str(plan.activation["change_ticket_env"]), "")
    checkpoint["automatic_rollback"] = rollback_results or []
    if activation_proof_sha256:
        checkpoint["activation_proof_sha256"] = activation_proof_sha256
    return write_proof(path, checkpoint)


def proof_age_seconds(proof: dict[str, Any]) -> float:
    generated = str(proof.get("generated_at") or "")
    try:
        timestamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CutoverError("proof generated_at is invalid") from exc
    return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())


def woodpecker_lookup(target: ServiceTarget, full_name: str, required: bool = True) -> dict[str, Any] | None:
    status, payload = service_request(
        target,
        "GET",
        f"api/repos/lookup/{quote(full_name, safe='/')}",
        expected=(200, 404),
        return_status=True,
    )
    if status == 404:
        if required:
            raise CutoverError(f"Woodpecker repository is not active: {full_name}")
        return None
    if not isinstance(payload, dict):
        raise CutoverError(f"Woodpecker repository lookup returned invalid data for {full_name}")
    return payload


def woodpecker_secret_upsert(
    target: ServiceTarget,
    repo_id: int,
    name: str,
    value: str,
    events: list[str] | None = None,
    images: list[str] | None = None,
    note: str = "Managed by platform forge cutover",
) -> dict[str, Any]:
    register_secret(value)
    path = f"api/repos/{repo_id}/secrets/{quote(name, safe='')}"
    status, _existing = service_request(target, "GET", path, expected=(200, 404), return_status=True)
    body = {
        "name": name,
        "value": value,
        "events": events or [],
        "images": images or [],
        "note": note,
    }
    if status == 404:
        service_request(target, "POST", f"api/repos/{repo_id}/secrets", body=body, expected=(200, 201))
        action = "created"
    else:
        service_request(target, "PATCH", path, body=body, expected=(200,))
        action = "updated"
    verify_status, _ = service_request(
        target,
        "GET",
        path,
        expected=(200, 404),
        return_status=True,
    )
    return {"name": name, "action": action, "verified": verify_status == 200}


def woodpecker_cron_upsert(
    target: ServiceTarget,
    repo_id: int,
    name: str,
    schedule: str,
    branch: str,
    enabled: bool,
) -> dict[str, Any]:
    crons = service_request(target, "GET", f"api/repos/{repo_id}/cron")
    if not isinstance(crons, list):
        raise CutoverError("Woodpecker cron list returned invalid data")
    existing = next((item for item in crons if str(item.get("name") or "") == name), None)
    body = {"name": name, "schedule": schedule, "branch": branch, "enabled": enabled}
    if existing:
        cron_id = int(existing["id"])
        result = service_request(target, "PATCH", f"api/repos/{repo_id}/cron/{cron_id}", body=body)
        action = "updated"
    else:
        result = service_request(target, "POST", f"api/repos/{repo_id}/cron", body=body, expected=(200, 201))
        cron_id = int(result.get("id") or 0)
        action = "created"
    return {"id": cron_id, "name": name, "enabled": enabled, "action": action, "verified": cron_id > 0}


def prepare_harbor(plan: CutoverPlan, woodpecker_target: ServiceTarget, woodpecker_repo_id: int) -> dict[str, Any]:
    config = plan.services["harbor"]
    if config["mode"] in {"manual", "skipped"}:
        return {"mode": config["mode"], "verified": True}
    target = service_target("harbor", config)
    project = str(config["project"])
    status, payload = service_request(
        target,
        "GET",
        f"api/v2.0/projects/{quote(project, safe='')}",
        expected=(200, 404),
        return_status=True,
    )
    action = "existing"
    if status == 404:
        if config["mode"] != "managed" or not bool_value(config.get("create_project"), False):
            raise CutoverError(f"Harbor project is missing and is not managed: {project}")
        service_request(
            target,
            "POST",
            "api/v2.0/projects",
            body={"project_name": project, "metadata": {"public": "false"}},
            expected=(201,),
        )
        payload = service_request(target, "GET", f"api/v2.0/projects/{quote(project, safe='')}")
        action = "created"
    registry_host = str(config.get("registry_host") or urlsplit(target.api_url).netloc)
    username = env_credential(target.username_env, "harbor username")
    registry_credential = env_credential(target.password_env, "harbor password")
    registry_path = f"api/repos/{woodpecker_repo_id}/registries/{quote(registry_host, safe='')}"
    registry_status, _ = service_request(
        woodpecker_target,
        "GET",
        registry_path,
        expected=(200, 404),
        return_status=True,
    )
    registry_body = {"address": registry_host, "username": username, "password": registry_credential}
    if registry_status == 404:
        service_request(
            woodpecker_target,
            "POST",
            f"api/repos/{woodpecker_repo_id}/registries",
            body=registry_body,
            expected=(200, 201),
        )
        registry_action = "created"
    else:
        service_request(woodpecker_target, "PATCH", registry_path, body=registry_body)
        registry_action = "updated"
    return {
        "mode": config["mode"],
        "project": project,
        "project_action": action,
        "project_id": payload.get("project_id") if isinstance(payload, dict) else None,
        "registry": registry_host,
        "registry_action": registry_action,
        "verified": True,
    }


def verify_argocd(plan: CutoverPlan) -> dict[str, Any]:
    config = plan.services["argocd"]
    if config["mode"] in {"manual", "skipped"}:
        return {"mode": config["mode"], "applications": [], "verified": True}
    target = service_target("argocd", config)
    applications: list[dict[str, Any]] = []
    verified = True
    for app_plan in config.get("applications", []):
        name = str(app_plan["name"])
        payload = service_request(target, "GET", f"api/v1/applications/{quote(name, safe='')}")
        sync = str(payload.get("status", {}).get("sync", {}).get("status") or "")
        health = str(payload.get("status", {}).get("health", {}).get("status") or "")
        sources = payload.get("spec", {}).get("sources") or [payload.get("spec", {}).get("source") or {}]
        repo_urls = sorted(str(source.get("repoURL") or "") for source in sources if source)
        expected_repo = str(app_plan.get("expected_repo_url") or "")
        app_verified = sync == "Synced" and health == "Healthy"
        if expected_repo:
            app_verified = app_verified and expected_repo in repo_urls
        verified = verified and app_verified
        applications.append(
            {
                "name": name,
                "sync": sync,
                "health": health,
                "repo_urls": [migration.redact_url(url) for url in repo_urls],
                "expected_repo_url": migration.redact_url(expected_repo),
                "verified": app_verified,
            }
        )
    return {"mode": config["mode"], "applications": applications, "verified": verified}


def prepare_destination_protections(repo: CutoverRepo) -> dict[str, Any]:
    target, base = repo_base(repo, "destination")
    existing_payload = migration.api_request(target, "GET", f"{base}/branch_protections")
    if not isinstance(existing_payload, list):
        raise CutoverError("Forgejo branch protection list returned invalid data")
    existing = {
        str(item.get("rule_name") or item.get("branch_name") or item.get("name") or ""): item
        for item in existing_payload
    }
    results: list[dict[str, Any]] = []
    for mapping_item in repo.cutover["protections"].get("mappings", []):
        mode = str(mapping_item["mode"]).lower()
        target_name = str(mapping_item.get("target") or mapping_item.get("target_pattern") or "")
        if mode in {"manual", "skipped"}:
            results.append(
                {
                    "source": mapping_item.get("source"),
                    "target": target_name,
                    "mode": mode,
                    "action": "accepted",
                    "verified": True,
                }
            )
            continue
        action = "existing"
        if mode == "managed":
            body = {**mapping_item["settings"], "rule_name": target_name}
            if target_name in existing:
                migration.api_request(
                    target,
                    "PATCH",
                    f"{base}/branch_protections/{quote(target_name, safe='')}",
                    body=body,
                )
                action = "updated"
            else:
                migration.api_request(
                    target,
                    "POST",
                    f"{base}/branch_protections",
                    body=body,
                    expected=(200, 201),
                )
                action = "created"
        results.append(
            {
                "source": mapping_item.get("source"),
                "target": target_name,
                "mode": mode,
                "action": action,
                "verified": True,
            }
        )
    verification = verify_destination_protections(repo)
    return {
        "mappings": results,
        "verification": verification,
        "verified": verification.get("verified"),
    }


def prepare_repository(plan: CutoverPlan, repo: CutoverRepo) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="forge-cutover-prepare-") as temp:
        migration_result = migration.migrate_repo(repo.migration, Path(temp))
    destination_repository = migration_result["destination_repository"]
    destination, destination_base = repo_base(repo, "destination")
    destination_payload = migration.api_request(destination, "GET", destination_base)
    forge_remote_id = int(destination_payload.get("id") or 0)
    if forge_remote_id <= 0:
        raise CutoverError(f"{repo.migration.name}: Forgejo repository ID is missing")
    woodpecker_config = plan.services["woodpecker"]
    if woodpecker_config["mode"] in {"manual", "skipped"}:
        raise CutoverError("Woodpecker must be managed or mapped for automated shadow preparation")
    woodpecker = service_target("woodpecker", woodpecker_config)
    full_name = str(destination_payload.get("full_name") or repo.migration.destination_api_repository or "")
    woodpecker_repo = woodpecker_lookup(woodpecker, full_name, required=False)
    repo_action = "existing"
    if woodpecker_repo is None:
        woodpecker_repo = service_request(
            woodpecker,
            "POST",
            "api/repos",
            query={"forge_remote_id": forge_remote_id},
            expected=(200, 201),
        )
        repo_action = "activated"
    woodpecker_repo_id = int(woodpecker_repo.get("id") or 0)
    if woodpecker_repo_id <= 0:
        raise CutoverError(f"{repo.migration.name}: Woodpecker repository ID is missing")
    pipeline_config = repo.cutover["pipelines"]
    config_file = str(pipeline_config.get("config_file") or "")
    patch_body: dict[str, Any] = {
        "require_approval": str(woodpecker_config.get("require_approval") or "pull_requests"),
        "trusted": {"network": False, "volumes": False, "security": False},
        "allow_pr": True,
    }
    if config_file:
        patch_body["config_file"] = config_file
    service_request(woodpecker, "PATCH", f"api/repos/{woodpecker_repo_id}", body=patch_body)
    gate = woodpecker_secret_upsert(
        woodpecker,
        woodpecker_repo_id,
        str(woodpecker_config["shadow_gate_secret"]),
        "false",
        events=["push", "tag", "deployment", "manual"],
        note="Fail-closed deployment authority gate managed by platform forge cutover",
    )
    source_variables = list_gitlab_variables(repo, include_values=True)
    variable_by_identity: dict[str, dict[str, Any]] = {}
    for variable in source_variables:
        scope = str(variable.get("source_scope") or "project")
        variable_by_identity[variable_identity(scope, variable)] = variable
        variable_by_identity[f"{scope}:{variable.get('key')}"] = variable
    managed_secrets: list[dict[str, Any]] = []
    for mapping_item in repo.cutover["variables"].get("mappings", []):
        if str(mapping_item["mode"]).lower() != "managed":
            continue
        identity = str(mapping_item["source"])
        variable = variable_by_identity.get(identity)
        if variable is None:
            raise CutoverError(f"{repo.migration.name}: mapped GitLab variable is missing: {identity}")
        value = str(variable.get("value") or "")
        if not value:
            raise CutoverError(f"{repo.migration.name}: mapped GitLab variable has no readable value: {identity}")
        managed_secrets.append(
            woodpecker_secret_upsert(
                woodpecker,
                woodpecker_repo_id,
                str(mapping_item["target_name"]),
                value,
                events=[str(item) for item in mapping_item.get("events", [])],
                images=[str(item) for item in mapping_item.get("images", [])],
                note=f"Migrated from {identity}; value omitted from proof",
            )
        )
    crons: list[dict[str, Any]] = []
    for mapping_item in repo.cutover["schedules"].get("mappings", []):
        if str(mapping_item["mode"]).lower() != "managed":
            continue
        crons.append(
            woodpecker_cron_upsert(
                woodpecker,
                woodpecker_repo_id,
                str(mapping_item["target_name"]),
                str(mapping_item["schedule"]),
                str(mapping_item["branch"]),
                enabled=False,
            )
        )
    protections = prepare_destination_protections(repo)
    harbor = prepare_harbor(plan, woodpecker, woodpecker_repo_id)
    argocd = verify_argocd(plan)
    verified = all(
        (
            destination_repository.get("verified"),
            migration_result.get("verified"),
            woodpecker_repo_id > 0,
            gate.get("verified"),
            all(secret.get("verified") for secret in managed_secrets),
            all(cron.get("verified") for cron in crons),
            protections.get("verified"),
            harbor.get("verified"),
            argocd.get("verified"),
        )
    )
    return {
        "name": repo.migration.name,
        "destination_repository": destination_repository,
        "migration": migration_result,
        "woodpecker": {
            "repository": full_name,
            "repository_id": woodpecker_repo_id,
            "action": repo_action,
            "shadow_gate": gate,
            "managed_secrets": managed_secrets,
            "crons": crons,
            "deployment_enabled": False,
            "verified": woodpecker_repo_id > 0 and gate.get("verified"),
        },
        "harbor": harbor,
        "protections": protections,
        "argocd": argocd,
        "verified": verified,
    }


def expected_woodpecker_labels(mapping_item: dict[str, Any]) -> dict[str, str]:
    labels = mapping_item.get("target_labels") or mapping_item.get("labels") or {}
    if not isinstance(labels, dict):
        raise CutoverError("runner tag target_labels must be an object")
    return {str(key): str(value) for key, value in labels.items()}


def verify_runner_capabilities(repo: CutoverRepo, agents: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    verified = True
    for mapping_item in repo.cutover["runner_tags"].get("mappings", []):
        mode = str(mapping_item["mode"]).lower()
        if mode in {"manual", "skipped"}:
            results.append({"source": mapping_item.get("source"), "mode": mode, "verified": True})
            continue
        labels = expected_woodpecker_labels(mapping_item)
        matching = []
        for agent in agents:
            actual = agent.get("custom_labels") or {}
            if bool_value(agent.get("no_schedule"), False):
                continue
            if all(str(actual.get(key)) == value for key, value in labels.items()):
                matching.append(str(agent.get("name") or agent.get("id") or ""))
        item_verified = bool(matching)
        verified = verified and item_verified
        results.append(
            {
                "source": mapping_item.get("source"),
                "target_labels": labels,
                "matching_agents": matching,
                "verified": item_verified,
            }
        )
    return {"mappings": results, "verified": verified}


def verify_destination_protections(repo: CutoverRepo) -> dict[str, Any]:
    target, base = repo_base(repo, "destination")
    payload = migration.api_request(target, "GET", f"{base}/branch_protections")
    if not isinstance(payload, list):
        raise CutoverError("Forgejo branch protection list returned invalid data")
    available = {
        str(item.get("rule_name") or item.get("branch_name") or item.get("name") or "")
        for item in payload
    }
    results: list[dict[str, Any]] = []
    verified = True
    for mapping_item in repo.cutover["protections"].get("mappings", []):
        mode = str(mapping_item["mode"]).lower()
        target_name = str(mapping_item.get("target") or mapping_item.get("target_pattern") or "")
        item_verified = mode in {"manual", "skipped"} or target_name in available
        verified = verified and item_verified
        results.append(
            {
                "source": mapping_item.get("source"),
                "target": target_name,
                "mode": mode,
                "verified": item_verified,
            }
        )
    return {"mappings": results, "verified": verified}


def verify_destination_integrations(repo: CutoverRepo, woodpecker_api_url: str) -> dict[str, Any]:
    target, base = repo_base(repo, "destination")
    hooks = migration.api_request(target, "GET", f"{base}/hooks")
    if not isinstance(hooks, list):
        raise CutoverError("Forgejo hooks list returned invalid data")
    woodpecker_host = urlsplit(woodpecker_api_url).netloc.lower()
    hook_hosts = {
        urlsplit(str((hook.get("config") or {}).get("url") or hook.get("url") or "")).netloc.lower()
        for hook in hooks
    }
    results: list[dict[str, Any]] = []
    verified = True
    for mapping_item in repo.cutover["integrations"].get("mappings", []):
        mode = str(mapping_item["mode"]).lower()
        target_kind = str(mapping_item.get("target") or "")
        if mode in {"manual", "skipped"}:
            item_verified = True
        elif target_kind == "woodpecker_webhook":
            expected_host = str(mapping_item.get("target_host") or woodpecker_host).lower()
            item_verified = expected_host in hook_hosts
        else:
            item_verified = bool_value(mapping_item.get("verified_externally"), False)
        verified = verified and item_verified
        results.append(
            {
                "source": mapping_item.get("source"),
                "target": target_kind,
                "mode": mode,
                "verified": item_verified,
            }
        )
    return {"hook_hosts": sorted(host for host in hook_hosts if host), "mappings": results, "verified": verified}


def verify_woodpecker_configuration(
    plan: CutoverPlan,
    repo: CutoverRepo,
    target: ServiceTarget,
    repo_id: int,
    phase: str,
) -> dict[str, Any]:
    secrets = service_request(target, "GET", f"api/repos/{repo_id}/secrets")
    crons = service_request(target, "GET", f"api/repos/{repo_id}/cron")
    if not isinstance(secrets, list) or not isinstance(crons, list):
        raise CutoverError("Woodpecker secret or cron inventory returned invalid data")
    available_secrets = {
        str(item.get("name") or "")
        for item in secrets
        if str(item.get("name") or "")
    }
    expected_secrets = {
        str(plan.services["woodpecker"]["shadow_gate_secret"]),
        *(
            str(item["target_name"])
            for item in repo.cutover["variables"].get("mappings", [])
            if str(item["mode"]).lower() == "managed"
        ),
    }
    missing_secrets = sorted(expected_secrets.difference(available_secrets))
    cron_by_name = {
        str(item.get("name") or ""): item
        for item in crons
        if str(item.get("name") or "")
    }
    expected_enabled = phase == "post-cutover"
    cron_results: list[dict[str, Any]] = []
    for mapping_item in repo.cutover["schedules"].get("mappings", []):
        if str(mapping_item["mode"]).lower() != "managed":
            continue
        name = str(mapping_item["target_name"])
        actual = cron_by_name.get(name)
        actual_enabled = bool_value(actual.get("enabled"), False) if actual else None
        item_verified = actual is not None and actual_enabled == expected_enabled
        cron_results.append(
            {
                "name": name,
                "expected_enabled": expected_enabled,
                "actual_enabled": actual_enabled,
                "verified": item_verified,
            }
        )
    return {
        "required_secret_names": sorted(expected_secrets),
        "missing_secret_names": missing_secrets,
        "managed_crons": cron_results,
        "verified": not missing_secrets and all(item["verified"] for item in cron_results),
    }


def trigger_woodpecker_canary(
    target: ServiceTarget,
    repo_id: int,
    branch: str,
    timeout_seconds: int,
    phase: str,
) -> dict[str, Any]:
    payload = service_request(
        target,
        "POST",
        f"api/repos/{repo_id}/pipelines",
        body={"branch": branch, "variables": {"FORGE_CUTOVER_CANARY": phase}},
        expected=(200, 201),
    )
    pipeline_number = int(payload.get("number") or payload.get("id") or 0)
    if pipeline_number <= 0:
        raise CutoverError("Woodpecker canary did not return a pipeline number")
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        current = service_request(target, "GET", f"api/repos/{repo_id}/pipelines/{pipeline_number}")
        last_status = str(current.get("status") or "").lower()
        if last_status in WOODPECKER_SUCCESS_STATES:
            return {
                "pipeline": pipeline_number,
                "status": last_status,
                "commit": str(current.get("commit") or ""),
                "phase": phase,
                "verified": True,
            }
        if last_status in WOODPECKER_FAILURE_STATES:
            break
        if last_status not in WOODPECKER_ACTIVE_STATES and last_status:
            break
        time.sleep(2)
    return {
        "pipeline": pipeline_number,
        "status": last_status or "timeout",
        "phase": phase,
        "verified": False,
    }


def verify_harbor_canary(plan: CutoverPlan) -> dict[str, Any]:
    config = plan.services["harbor"]
    canary = config.get("canary") or {}
    if config["mode"] in {"manual", "skipped"} or not canary:
        return {"mode": "not-configured", "verified": True}
    if not isinstance(canary, dict):
        raise CutoverError("services.harbor.canary must be an object")
    repository = string_value(canary, "repository", "services.harbor.canary")
    reference = string_value(canary, "reference", "services.harbor.canary")
    target = service_target("harbor", config)
    payload = service_request(
        target,
        "GET",
        f"api/v2.0/projects/{quote(str(config['project']), safe='')}/repositories/"
        f"{quote(repository, safe='')}/artifacts/{quote(reference, safe='')}",
    )
    digest = str(payload.get("digest") or "") if isinstance(payload, dict) else ""
    return {
        "repository": repository,
        "reference": reference,
        "digest": digest,
        "verified": bool(digest),
    }


def verify_repository(plan: CutoverPlan, repo: CutoverRepo, prepared: dict[str, Any], phase: str) -> dict[str, Any]:
    current_inventory = discover_repository(repo, verify_destination=True)
    migration_result = migration.verify_repo(repo.migration)
    woodpecker_config = plan.services["woodpecker"]
    woodpecker = service_target("woodpecker", woodpecker_config)
    destination, destination_base = repo_base(repo, "destination")
    destination_payload = migration.api_request(destination, "GET", destination_base)
    full_name = str(destination_payload.get("full_name") or repo.migration.destination_api_repository or "")
    wp_repo = woodpecker_lookup(woodpecker, full_name)
    assert wp_repo is not None
    repo_id = int(wp_repo.get("id") or 0)
    agents = service_request(woodpecker, "GET", "api/agents")
    if not isinstance(agents, list):
        raise CutoverError("Woodpecker agent list returned invalid data")
    runner_capabilities = verify_runner_capabilities(repo, agents)
    woodpecker_configuration = verify_woodpecker_configuration(
        plan,
        repo,
        woodpecker,
        repo_id,
        phase,
    )
    protections = verify_destination_protections(repo)
    integrations = verify_destination_integrations(repo, woodpecker.api_url)
    pipeline_files = current_inventory["pipelines"]
    variables = current_inventory["variables"]
    canary = trigger_woodpecker_canary(
        woodpecker,
        repo_id,
        str(destination_payload.get("default_branch") or "main"),
        int_value(woodpecker_config.get("canary_timeout_seconds"), 900, minimum=30),
        phase,
    )
    harbor = verify_harbor_canary(plan)
    argocd = verify_argocd(plan)
    verified = all(
        (
            migration_result.get("verified"),
            current_inventory.get("verified"),
            woodpecker_configuration.get("verified"),
            variables.get("verified"),
            runner_capabilities.get("verified"),
            protections.get("verified"),
            integrations.get("verified"),
            pipeline_files.get("verified"),
            canary.get("verified"),
            harbor.get("verified"),
            argocd.get("verified"),
        )
    )
    return {
        "name": repo.migration.name,
        "inventory": current_inventory,
        "migration": migration_result,
        "woodpecker_repository_id": repo_id,
        "woodpecker_configuration": woodpecker_configuration,
        "pipeline_definitions": pipeline_files,
        "variables": variables,
        "runner_capabilities": runner_capabilities,
        "protections": protections,
        "integrations": integrations,
        "canary": canary,
        "harbor": harbor,
        "argocd": argocd,
        "prepared_repository": prepared.get("name"),
        "verified": verified,
    }


def find_proof_repo(proof: dict[str, Any], name: str) -> dict[str, Any]:
    for item in proof.get("repositories", []):
        if item.get("name") == name:
            return item
    raise CutoverError(f"proof does not contain repository {name}")


def gitlab_active_pipelines(repo: CutoverRepo) -> list[dict[str, Any]]:
    target, base = repo_base(repo, "source")
    pipelines = gitlab_list(target, f"{base}/pipelines", {"order_by": "id", "sort": "desc"})
    return [item for item in pipelines if str(item.get("status") or "").lower() in ACTIVE_GITLAB_PIPELINE_STATES]


def freeze_gitlab(
    repo: CutoverRepo,
    cancel_active: bool,
    snapshot_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    target, base = repo_base(repo, "source")
    project = migration.api_request(target, "GET", base)
    schedules = gitlab_list(target, f"{base}/pipeline_schedules")
    snapshot = {
        "project_id": project.get("id"),
        "archived": bool_value(project.get("archived"), False),
        "builds_access_level": str(project.get("builds_access_level") or "enabled"),
        "schedules": [
            {"id": item.get("id"), "active": bool_value(item.get("active"), False)}
            for item in schedules
        ],
    }
    if snapshot_callback is not None:
        snapshot_callback(snapshot)
    for schedule in schedules:
        if bool_value(schedule.get("active"), False):
            migration.api_request(
                target,
                "PUT",
                f"{base}/pipeline_schedules/{schedule['id']}",
                body={"active": False},
            )
    active = gitlab_active_pipelines(repo)
    if active and cancel_active:
        for pipeline in active:
            migration.api_request(
                target,
                "POST",
                f"{base}/pipelines/{pipeline['id']}/cancel",
                expected=(200, 201),
            )
        active = gitlab_active_pipelines(repo)
    if active:
        ids = ", ".join(str(item.get("id")) for item in active)
        raise CutoverError(f"{repo.migration.name}: active GitLab pipelines block cutover: {ids}")
    migration.api_request(target, "PUT", base, body={"builds_access_level": "disabled"})
    if not bool_value(project.get("archived"), False):
        migration.api_request(target, "POST", f"{base}/archive", expected=(200, 201))
    current = migration.api_request(target, "GET", base)
    if not bool_value(current.get("archived"), False):
        raise CutoverError(f"{repo.migration.name}: GitLab project did not become archived")
    if str(current.get("builds_access_level") or "") != "disabled":
        raise CutoverError(f"{repo.migration.name}: GitLab CI did not become disabled")
    return snapshot


def restore_gitlab(repo: CutoverRepo, snapshot: dict[str, Any]) -> dict[str, Any]:
    target, base = repo_base(repo, "source")
    current = migration.api_request(target, "GET", base)
    if bool_value(current.get("archived"), False) and not bool_value(snapshot.get("archived"), False):
        migration.api_request(target, "POST", f"{base}/unarchive", expected=(200, 201))
    migration.api_request(
        target,
        "PUT",
        base,
        body={"builds_access_level": str(snapshot.get("builds_access_level") or "enabled")},
    )
    for schedule in snapshot.get("schedules", []):
        migration.api_request(
            target,
            "PUT",
            f"{base}/pipeline_schedules/{schedule['id']}",
            body={"active": bool_value(schedule.get("active"), False)},
        )
    restored = migration.api_request(target, "GET", base)
    return {
        "archived": bool_value(restored.get("archived"), False),
        "builds_access_level": str(restored.get("builds_access_level") or ""),
        "verified": (
            bool_value(restored.get("archived"), False) == bool_value(snapshot.get("archived"), False)
            and str(restored.get("builds_access_level") or "")
            == str(snapshot.get("builds_access_level") or "enabled")
        ),
    }


def set_destination_authority(plan: CutoverPlan, repo: CutoverRepo, enabled: bool) -> dict[str, Any]:
    woodpecker_config = plan.services["woodpecker"]
    woodpecker = service_target("woodpecker", woodpecker_config)
    destination, base = repo_base(repo, "destination")
    destination_payload = migration.api_request(destination, "GET", base)
    full_name = str(destination_payload.get("full_name") or repo.migration.destination_api_repository or "")
    wp_repo = woodpecker_lookup(woodpecker, full_name)
    assert wp_repo is not None
    repo_id = int(wp_repo.get("id") or 0)
    gate = woodpecker_secret_upsert(
        woodpecker,
        repo_id,
        str(woodpecker_config["shadow_gate_secret"]),
        "true" if enabled else "false",
        events=["push", "tag", "deployment", "manual"],
        note="Fail-closed deployment authority gate managed by platform forge cutover",
    )
    crons: list[dict[str, Any]] = []
    for mapping_item in repo.cutover["schedules"].get("mappings", []):
        if str(mapping_item["mode"]).lower() != "managed":
            continue
        crons.append(
            woodpecker_cron_upsert(
                woodpecker,
                repo_id,
                str(mapping_item["target_name"]),
                str(mapping_item["schedule"]),
                str(mapping_item["branch"]),
                enabled=enabled,
            )
        )
    return {
        "woodpecker_repository_id": repo_id,
        "deployment_enabled": enabled,
        "gate": gate,
        "crons": crons,
        "verified": gate.get("verified") and all(cron.get("verified") for cron in crons),
    }


def require_activation_confirmation(plan: CutoverPlan, proof: dict[str, Any]) -> None:
    live_env = str(plan.activation["live_env"])
    confirmation_env = str(plan.activation["confirmation_env"])
    change_ticket_env = str(plan.activation["change_ticket_env"])
    if os.environ.get(live_env, "") != "1":
        raise CutoverError(f"live cutover is disabled; set {live_env}=1 for this invocation")
    expected = str(proof.get("proof_sha256") or "")
    supplied = os.environ.get(confirmation_env, "")
    if not expected or supplied != expected:
        raise CutoverError(
            f"cutover approval mismatch; set {confirmation_env} to the verified proof digest {expected}"
        )
    if not os.environ.get(change_ticket_env, "").strip():
        raise CutoverError(f"{change_ticket_env} is required for live cutover evidence")
    max_age = int_value(plan.activation.get("max_verification_age_seconds"), 3600, minimum=60)
    age = proof_age_seconds(proof)
    if age > max_age:
        raise CutoverError(f"verification proof is stale ({int(age)}s > {max_age}s); verify again")


def require_rollback_confirmation(plan: CutoverPlan, proof: dict[str, Any]) -> None:
    live_env = str(plan.activation["live_env"])
    rollback_env = str(plan.activation.get("rollback_confirmation_env") or "FORGE_CUTOVER_ROLLBACK_CONFIRM")
    change_ticket_env = str(plan.activation["change_ticket_env"])
    if os.environ.get(live_env, "") != "1":
        raise CutoverError(f"live rollback is disabled; set {live_env}=1 for this invocation")
    expected = str(proof.get("proof_sha256") or "")
    if os.environ.get(rollback_env, "") != expected:
        raise CutoverError(f"rollback approval mismatch; set {rollback_env} to {expected}")
    if not os.environ.get(change_ticket_env, "").strip():
        raise CutoverError(f"{change_ticket_env} is required for live rollback evidence")


def command_validate(args: argparse.Namespace) -> int:
    plan = load_cutover_plan(args.plan)
    repositories = [
        {
            "name": repo.migration.name,
            "source_url": migration.redact_url(repo.migration.source_url),
            "destination_url": migration.redact_url(repo.migration.destination_url),
            "surfaces": sorted(repo.cutover),
            "verified": True,
        }
        for repo in plan.repositories
    ]
    proof = proof_base(plan, "validate-plan", repositories)
    write_proof(args.proof, proof)
    return 0


def command_discover(args: argparse.Namespace) -> int:
    plan = load_cutover_plan(args.plan)
    require_provider_credentials(plan)
    repositories: list[dict[str, Any]] = []
    for repo in plan.repositories:
        try:
            repositories.append(discover_repository(repo))
        except migration.MigrationError as exc:
            repositories.append(
                {
                    "name": repo.migration.name,
                    "error": redact_text(str(exc)),
                    "verified": False,
                }
            )
    proof = proof_base(plan, "discover", repositories)
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


def command_prepare(args: argparse.Namespace) -> int:
    plan = load_cutover_plan(args.plan)
    require_provider_credentials(plan)
    discovery = load_verified_proof(args.discovery, plan, "discover")
    confirmation_env = str(plan.activation.get("prepare_confirmation_env") or "FORGE_CUTOVER_PREPARE_CONFIRM")
    if os.environ.get(confirmation_env, "") != discovery.get("proof_sha256"):
        raise CutoverError(
            f"shadow preparation approval mismatch; set {confirmation_env} to {discovery.get('proof_sha256')}"
        )
    repositories: list[dict[str, Any]] = []
    for repo in plan.repositories:
        approved_inventory = find_proof_repo(discovery, repo.migration.name)
        try:
            current_inventory = discover_repository(repo)
            if not current_inventory.get("verified"):
                raise CutoverError(
                    f"{repo.migration.name}: current source inventory no longer satisfies the plan"
                )
            if canonical_digest(sanitize_for_proof(current_inventory)) != canonical_digest(
                approved_inventory
            ):
                raise CutoverError(
                    f"{repo.migration.name}: source inventory changed after approval; "
                    "run discover and approve the new proof"
                )
            prepared = prepare_repository(plan, repo)
            prepared["approved_inventory_sha256"] = canonical_digest(approved_inventory)
            repositories.append(prepared)
        except migration.MigrationError as exc:
            repositories.append(
                {
                    "name": repo.migration.name,
                    "error": redact_text(str(exc)),
                    "verified": False,
                }
            )
    proof = proof_base(plan, "prepare", repositories)
    proof["discovery_proof_sha256"] = discovery.get("proof_sha256")
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


def command_verify(args: argparse.Namespace) -> int:
    plan = load_cutover_plan(args.plan)
    require_provider_credentials(plan)
    prepared = load_verified_proof(args.prepared, plan, "prepare")
    repositories: list[dict[str, Any]] = []
    for repo in plan.repositories:
        prepared_repo = find_proof_repo(prepared, repo.migration.name)
        try:
            repositories.append(verify_repository(plan, repo, prepared_repo, "shadow"))
        except migration.MigrationError as exc:
            repositories.append(
                {
                    "name": repo.migration.name,
                    "error": redact_text(str(exc)),
                    "verified": False,
                }
            )
    proof = proof_base(plan, "verify", repositories)
    proof["prepare_proof_sha256"] = prepared.get("proof_sha256")
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


def command_activate(args: argparse.Namespace) -> int:
    plan = load_cutover_plan(args.plan)
    require_provider_credentials(plan)
    verification = load_verified_proof(args.verification, plan, "verify")
    require_activation_confirmation(plan, verification)
    snapshots: dict[str, dict[str, Any]] = {}
    authority_attempted: set[str] = set()
    preflight: dict[str, dict[str, Any]] = {}
    repositories: list[dict[str, Any]] = []
    rollback_results: list[dict[str, Any]] = []
    write_activation_checkpoint(
        args.checkpoint,
        plan,
        verification,
        snapshots,
        authority_attempted,
        "approval-confirmed",
    )
    try:
        for repo in plan.repositories:
            verified_repository = find_proof_repo(verification, repo.migration.name)
            approved_inventory = verified_repository.get("inventory")
            if not isinstance(approved_inventory, dict):
                raise CutoverError(
                    f"{repo.migration.name}: verification proof is missing its approved inventory"
                )
            current = discover_repository(repo, verify_destination=True)
            if not current.get("verified"):
                raise CutoverError(
                    f"{repo.migration.name}: current GitLab/Forgejo inventory no longer matches the plan"
                )
            source_state = current.get("source_state") or {}
            if bool_value(source_state.get("archived"), False):
                raise CutoverError(
                    f"{repo.migration.name}: GitLab source is already archived; activation state is ambiguous"
                )
            if str(source_state.get("builds_access_level") or "") != "enabled":
                raise CutoverError(
                    f"{repo.migration.name}: GitLab CI is not enabled before activation"
                )
            if canonical_digest(sanitize_for_proof(current)) != canonical_digest(
                approved_inventory
            ):
                raise CutoverError(
                    f"{repo.migration.name}: source or destination inventory changed after verification; "
                    "run verify and approve the new proof"
                )
            preflight[repo.migration.name] = current
        write_activation_checkpoint(
            args.checkpoint,
            plan,
            verification,
            snapshots,
            authority_attempted,
            "current-inventory-verified",
        )
        cancel_active = bool_value(plan.activation.get("cancel_active_pipelines"), False)
        for repo in plan.repositories:
            name = repo.migration.name

            def persist_snapshot(snapshot: dict[str, Any], repository_name: str = name) -> None:
                snapshots[repository_name] = snapshot
                write_activation_checkpoint(
                    args.checkpoint,
                    plan,
                    verification,
                    snapshots,
                    authority_attempted,
                    f"source-snapshot-captured:{repository_name}",
                )

            snapshots[name] = freeze_gitlab(repo, cancel_active, persist_snapshot)
            write_activation_checkpoint(
                args.checkpoint,
                plan,
                verification,
                snapshots,
                authority_attempted,
                f"source-frozen:{name}",
            )
        work_dir = args.work_dir
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="forge-cutover-activate-")
            work_dir = Path(temporary.name)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            for repo in plan.repositories:
                migration_result = migration.migrate_repo(repo.migration, work_dir)
                authority_attempted.add(repo.migration.name)
                write_activation_checkpoint(
                    args.checkpoint,
                    plan,
                    verification,
                    snapshots,
                    authority_attempted,
                    f"destination-authority-attempted:{repo.migration.name}",
                )
                authority = set_destination_authority(plan, repo, True)
                post_verify = verify_repository(plan, repo, {"name": repo.migration.name}, "post-cutover")
                item_verified = all(
                    (migration_result.get("verified"), authority.get("verified"), post_verify.get("verified"))
                )
                repositories.append(
                    {
                        "name": repo.migration.name,
                        "activation_preflight": preflight[repo.migration.name],
                        "source_before": snapshots[repo.migration.name],
                        "final_migration": migration_result,
                        "destination_authority": authority,
                        "post_cutover_verification": post_verify,
                        "verified": item_verified,
                    }
                )
                if not item_verified:
                    raise CutoverError(f"{repo.migration.name}: post-cutover verification failed")
                write_activation_checkpoint(
                    args.checkpoint,
                    plan,
                    verification,
                    snapshots,
                    authority_attempted,
                    f"repository-activated:{repo.migration.name}",
                )
        finally:
            if temporary is not None:
                temporary.cleanup()
    except Exception as exc:  # Roll back every source/destination mutation before reporting failure.
        for repo in reversed(plan.repositories):
            result: dict[str, Any] = {"name": repo.migration.name, "verified": True}
            errors: list[str] = []
            try:
                if repo.migration.name in authority_attempted:
                    result["destination"] = set_destination_authority(plan, repo, False)
            except Exception as rollback_exc:
                errors.append(redact_text(str(rollback_exc)))
            try:
                if repo.migration.name in snapshots:
                    result["source"] = restore_gitlab(repo, snapshots[repo.migration.name])
            except Exception as rollback_exc:
                errors.append(redact_text(str(rollback_exc)))
            result["errors"] = errors
            result["verified"] = not errors and all(
                child.get("verified", True)
                for child in (result.get("destination", {}), result.get("source", {}))
            )
            rollback_results.append(result)
        failure = proof_base(plan, "activate", repositories or [{"name": "cutover", "verified": False}])
        failure["verified"] = False
        failure["error"] = redact_text(str(exc))
        failure["automatic_rollback"] = rollback_results
        failure["verification_proof_sha256"] = verification.get("proof_sha256")
        failure["change_ticket"] = os.environ.get(str(plan.activation["change_ticket_env"]), "")
        write_proof(args.proof, failure)
        rollback_complete = bool(rollback_results) and all(
            bool(item.get("verified")) for item in rollback_results
        )
        write_activation_checkpoint(
            args.checkpoint,
            plan,
            verification,
            snapshots,
            authority_attempted,
            "automatic-rollback-complete" if rollback_complete else "automatic-rollback-incomplete",
            rollback_results=rollback_results,
        )
        return 1
    proof = proof_base(plan, "activate", repositories)
    proof["verification_proof_sha256"] = verification.get("proof_sha256")
    proof["change_ticket"] = os.environ.get(str(plan.activation["change_ticket_env"]), "")
    proof["source_authority"] = "gitlab-frozen"
    proof["destination_authority"] = "woodpecker-argocd"
    final_proof = write_proof(args.proof, proof)
    write_activation_checkpoint(
        args.checkpoint,
        plan,
        verification,
        snapshots,
        authority_attempted,
        "completed",
        activation_proof_sha256=str(final_proof.get("proof_sha256") or ""),
    )
    return 0 if proof["verified"] else 1


def command_rollback(args: argparse.Namespace) -> int:
    plan = load_cutover_plan(args.plan)
    require_provider_credentials(plan)
    activation = load_integrity_proof(
        args.activation,
        plan,
        ("activate", "activation-checkpoint"),
    )
    if activation.get("command") == "activate" and activation.get("verified") is not True:
        raise CutoverError(f"{args.activation}: failed activation proof cannot drive manual rollback")
    require_rollback_confirmation(plan, activation)
    repositories: list[dict[str, Any]] = []
    for repo in reversed(plan.repositories):
        activated_repo = find_proof_repo(activation, repo.migration.name)
        source_before = activated_repo.get("source_before")
        authority_attempted = bool_value(
            activated_repo.get("destination_authority_attempted"),
            activation.get("command") == "activate",
        )
        if source_before is not None and not isinstance(source_before, dict):
            raise CutoverError(f"activation evidence has invalid source_before for {repo.migration.name}")
        try:
            destination = (
                set_destination_authority(plan, repo, False)
                if authority_attempted
                else {"skipped": True, "verified": True}
            )
            source = (
                restore_gitlab(repo, source_before)
                if isinstance(source_before, dict)
                else {"skipped": True, "verified": True}
            )
            repositories.append(
                {
                    "name": repo.migration.name,
                    "destination": destination,
                    "source": source,
                    "verified": destination.get("verified") and source.get("verified"),
                }
            )
        except migration.MigrationError as exc:
            repositories.append(
                {"name": repo.migration.name, "error": redact_text(str(exc)), "verified": False}
            )
    proof = proof_base(plan, "rollback", repositories)
    proof["activation_proof_sha256"] = activation.get("proof_sha256")
    proof["source_authority"] = "gitlab-restored" if proof["verified"] else "rollback-incomplete"
    proof["destination_authority"] = "woodpecker-shadow"
    write_proof(args.proof, proof)
    if activation.get("command") == "activation-checkpoint":
        write_activation_checkpoint(
            args.activation,
            plan,
            {"proof_sha256": activation.get("verification_proof_sha256")},
            {
                item["name"]: item["source_before"]
                for item in activation.get("repositories", [])
                if isinstance(item.get("source_before"), dict)
            },
            {
                item["name"]
                for item in activation.get("repositories", [])
                if bool_value(item.get("destination_authority_attempted"), False)
            },
            "manual-rollback-complete" if proof["verified"] else "manual-rollback-incomplete",
        )
    return 0 if proof["verified"] else 1


def command_verify_proof(args: argparse.Namespace) -> int:
    proof = migration.load_plan(args.proof_file)
    claimed = str(proof.get("proof_sha256") or "")
    actual = migration.proof_digest(proof)
    accepted = bool(claimed) and claimed == actual and proof.get("verified") is True
    print(
        json.dumps(
            {
                "proof_file": str(args.proof_file),
                "proof_sha256": claimed,
                "actual_sha256": actual,
                "integrity_verified": claimed == actual,
                "cutover_verified": proof.get("verified") is True,
                "accepted": accepted,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if accepted else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-plan")
    validate.add_argument("plan", type=Path)
    validate.add_argument("--proof", type=Path)
    validate.set_defaults(handler=command_validate)
    discover = subparsers.add_parser("discover")
    discover.add_argument("plan", type=Path)
    discover.add_argument("--proof", type=Path, required=True)
    discover.set_defaults(handler=command_discover)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("plan", type=Path)
    prepare.add_argument("--discovery", type=Path, required=True)
    prepare.add_argument("--proof", type=Path, required=True)
    prepare.set_defaults(handler=command_prepare)
    verify = subparsers.add_parser("verify")
    verify.add_argument("plan", type=Path)
    verify.add_argument("--prepared", type=Path, required=True)
    verify.add_argument("--proof", type=Path, required=True)
    verify.set_defaults(handler=command_verify)
    activate = subparsers.add_parser("activate")
    activate.add_argument("plan", type=Path)
    activate.add_argument("--verification", type=Path, required=True)
    activate.add_argument("--proof", type=Path, required=True)
    activate.add_argument("--checkpoint", type=Path, required=True)
    activate.add_argument("--work-dir", type=Path)
    activate.set_defaults(handler=command_activate)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("plan", type=Path)
    rollback.add_argument("--activation", type=Path, required=True)
    rollback.add_argument("--proof", type=Path, required=True)
    rollback.set_defaults(handler=command_rollback)
    verify_proof = subparsers.add_parser("verify-proof")
    verify_proof.add_argument("proof_file", type=Path)
    verify_proof.set_defaults(handler=command_verify_proof)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return args.handler(args)
    except migration.MigrationError as exc:
        print(f"forge cutover failed: {redact_text(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
