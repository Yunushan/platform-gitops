#!/usr/bin/env python3
"""Export and selectively import GitLab workspace state into Forgejo.

The workspace command is deliberately separate from the repository migrator.
It inventories users, groups, projects, CI/CD metadata, variables, runners,
and pipeline history, then applies only surfaces whose plan mode is ``managed``.
``skip``, ``export``, ``mapped``, and ``manual`` are explicit non-mutating
choices. Secret values never appear in a plan, snapshot proof, or stdout.
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
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import forge_migration as migration
import forge_pipeline as pipeline
from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from strict_json import loads_strict_json


TOOL = "scripts/forge_workspace.py"
PLAN_VERSION = 1
SNAPSHOT_VERSION = 1
PROOF_VERSION = 1
SURFACES = (
    "users",
    "groups",
    "subgroups",
    "projects",
    "repositories",
    "runners",
    "variables",
    "ci",
    "pipelines",
)
MODES = {"skip", "export", "managed", "mapped", "manual"}
ACCOUNTED_MODES = {"managed", "mapped", "manual", "skipped"}
DEFAULT_PIPELINE_GLOBS = (
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".gitlab/ci/*.yml",
    ".gitlab/ci/*.yaml",
)
SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "password",
    "private_token",
    "secret",
    "token",
    "value",
}
SAFE_PROJECT_KEYS = (
    "id",
    "path",
    "path_with_namespace",
    "name",
    "name_with_namespace",
    "namespace",
    "description",
    "visibility",
    "archived",
    "default_branch",
    "web_url",
    "http_url_to_repo",
    "ssh_url_to_repo",
    "topics",
    "tag_list",
    "issues_enabled",
    "wiki_enabled",
    "snippets_enabled",
    "lfs_enabled",
    "packages_enabled",
    "container_registry_enabled",
    "ci_config_path",
    "only_allow_merge_if_pipeline_succeeds",
    "remove_source_branch_after_merge",
    "merge_method",
    "squash_option",
)


class WorkspaceError(migration.MigrationError):
    """Raised when a workspace operation cannot be proven safe."""


@dataclass(frozen=True)
class Endpoint:
    provider: str
    api_url: str
    token_env: str

    def target(self) -> migration.ApiTarget:
        return migration.ApiTarget(
            provider=self.provider,
            api_url=self.api_url.rstrip("/"),
            repository="workspace",
            token_env=self.token_env,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return string(value).lower() not in {"", "0", "false", "no", "off", "none"}


def surface_config(raw: Any, label: str) -> dict[str, Any]:
    if raw is None:
        return {"mode": "skip"}
    if isinstance(raw, bool):
        return {"mode": "managed" if raw else "skip"}
    if isinstance(raw, str):
        config = {"mode": raw}
    elif isinstance(raw, dict):
        config = dict(raw)
    else:
        raise WorkspaceError(f"{label} must be false, a mode string, or an object")
    mode = string(config.get("mode") or config.get("action") or "skip").lower()
    if mode == "skipped":
        mode = "skip"
    if mode not in MODES:
        raise WorkspaceError(f"{label}.mode must be one of {sorted(MODES)}")
    config["mode"] = mode
    if mode == "manual" and (not bool_value(config.get("accepted")) or not string(config.get("reason"))):
        raise WorkspaceError(f"{label} manual mode requires accepted=true and a reason")
    return config


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = loads_strict_json(read_bounded_text(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"{path}: cannot read strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"{path}: JSON root must be an object")
    return value


def load_plan(path: Path) -> dict[str, Any]:
    plan = load_json(path)
    try:
        migration.require_credential_free_plan(plan)
    except migration.MigrationError as exc:
        raise WorkspaceError(str(exc)) from exc
    validate_plan(plan)
    return plan


def endpoint(plan: dict[str, Any], name: str, provider: str) -> Endpoint:
    raw = plan.get(name)
    if not isinstance(raw, dict):
        raise WorkspaceError(f"{name} must be an object")
    api_url = string(raw.get("api_url")).rstrip("/")
    token_env = string(raw.get("token_env"))
    if not api_url or not token_env:
        raise WorkspaceError(f"{name}.api_url and {name}.token_env are required")
    if not api_url.startswith("https://"):
        raise WorkspaceError(f"{name}.api_url must use HTTPS")
    return Endpoint(provider, api_url, token_env)


def require_selector(plan: dict[str, Any]) -> None:
    source = plan.get("source")
    if not isinstance(source, dict):
        raise WorkspaceError("source must be an object")
    selectors = source.get("project_paths") or source.get("group_paths")
    users = surface_config((plan.get("surfaces") or {}).get("users"), "surfaces.users") if isinstance(plan.get("surfaces"), dict) else {"mode": "skip"}
    user_selector = source.get("usernames") or bool_value(users.get("all_available"))
    if not selectors and not bool_value(source.get("all_available_projects")) and not user_selector:
        raise WorkspaceError(
            "source.project_paths, source.group_paths, source.usernames, or an explicit all_available selector is required"
        )
    if selectors is not None and (not isinstance(selectors, list) or not all(string(item) for item in selectors)):
        raise WorkspaceError("source.project_paths and source.group_paths must contain non-empty strings")


def validate_plan(plan: dict[str, Any]) -> None:
    if string(plan.get("direction")) != "gitlab-to-forgejo":
        raise WorkspaceError("direction must be gitlab-to-forgejo")
    if int(plan.get("version") or PLAN_VERSION) != PLAN_VERSION:
        raise WorkspaceError(f"unsupported workspace plan version: {plan.get('version')}")
    source = endpoint(plan, "source", "gitlab")
    destination = endpoint(plan, "destination", "forgejo")
    if source.provider != "gitlab" or destination.provider != "forgejo":
        raise WorkspaceError("workspace endpoints must be GitLab source and Forgejo destination")
    require_selector(plan)
    surfaces = plan.get("surfaces")
    if surfaces is not None and not isinstance(surfaces, dict):
        raise WorkspaceError("surfaces must be an object")
    normalized = {name: surface_config((surfaces or {}).get(name), f"surfaces.{name}") for name in SURFACES}
    if all(item["mode"] == "skip" for item in normalized.values()):
        raise WorkspaceError("at least one workspace surface must be selected")
    project_surfaces_selected = any(
        normalized[name]["mode"] != "skip"
        for name in ("projects", "repositories", "runners", "variables", "ci", "pipelines")
    )
    if project_surfaces_selected and not (
        source_project_paths(plan)
        or source_group_paths(plan)
        or bool_value(plan["source"].get("all_available_projects"))
    ):
        raise WorkspaceError(
            "project, repository, runner, variable, CI, or pipeline surfaces require source.project_paths, source.group_paths, or all_available_projects=true"
        )
    for name, config in normalized.items():
        if name in {"groups", "subgroups"} and config["mode"] != "skip" and not source_group_paths(plan):
            raise WorkspaceError(f"surfaces.{name} requires source.group_paths")
        if name == "users" and config["mode"] != "skip":
            usernames = plan["source"].get("usernames") or []
            if not usernames and not bool_value(config.get("all_available")):
                raise WorkspaceError("surfaces.users requires source.usernames or surfaces.users.all_available=true")
            if usernames and (not isinstance(usernames, list) or not all(string(item) for item in usernames)):
                raise WorkspaceError("source.usernames must contain non-empty strings")
        if name in {"groups", "subgroups"} and config["mode"] == "managed":
            if string(config.get("target_kind") or "organization") != "organization":
                raise WorkspaceError(f"surfaces.{name}.target_kind must be organization")
        if name == "users" and config["mode"] == "managed":
            if not string(config.get("default_password_env")) and not isinstance(config.get("password_env_by_username"), dict):
                raise WorkspaceError(
                    "surfaces.users.managed requires default_password_env or password_env_by_username"
                )
        if name in {"groups", "subgroups"} and config["mode"] == "managed":
            if source_mode(plan, "users") != "managed" and string(config.get("members_mode") or "import") not in {
                "skip",
                "mapped",
                "manual",
            }:
                raise WorkspaceError(
                    f"surfaces.{name}.members_mode=skip|mapped|manual is required when users are not managed"
                )
        if name == "runners" and config["mode"] == "managed":
            if string(config.get("target") or "woodpecker") != "woodpecker":
                raise WorkspaceError("surfaces.runners.target currently supports woodpecker only")
            if not isinstance(config.get("label_mappings"), dict) or not config.get("label_mappings"):
                raise WorkspaceError("surfaces.runners.managed requires non-empty label_mappings")
        if name == "variables" and config["mode"] == "managed":
            if string(config.get("target") or "woodpecker") != "woodpecker":
                raise WorkspaceError("surfaces.variables.target currently supports woodpecker only")
        if name == "pipelines" and bool_value(config.get("import_history")):
            raise WorkspaceError(
                "surfaces.pipelines.import_history cannot be true: historical GitLab runs are export-only; use ci or schedules for import"
            )
        if name == "pipelines":
            schedule_mappings = config.get("schedule_mappings") or {}
            if not isinstance(schedule_mappings, dict):
                raise WorkspaceError("surfaces.pipelines.schedule_mappings must be an object")
            for source_id, mapping in schedule_mappings.items():
                if isinstance(mapping, dict) and bool_value(mapping.get("enabled")):
                    raise WorkspaceError(
                        f"surfaces.pipelines.schedule_mappings[{source_id!r}] cannot enable a schedule during workspace import; use the approved cutover controller"
                    )
        if name == "ci" and config["mode"] == "managed" and not bool_value(config.get("include_content")):
            raise WorkspaceError("surfaces.ci.managed requires include_content=true for fail-closed conversion")
    services = plan.get("services") or {}
    if not isinstance(services, dict):
        raise WorkspaceError("services must be an object")
    needs_woodpecker = any(
        normalized[name]["mode"] == "managed" for name in ("runners", "variables", "ci", "pipelines")
    )
    if needs_woodpecker:
        wp = services.get("woodpecker")
        if not isinstance(wp, dict) or not string(wp.get("api_url")) or not string(wp.get("token_env")):
            raise WorkspaceError("services.woodpecker.api_url and token_env are required for selected CI surfaces")
    mappings = plan.get("mappings") or {}
    if not isinstance(mappings, dict):
        raise WorkspaceError("mappings must be an object")
    for name in ("users", "groups", "projects", "runners", "variables"):
        value = mappings.get(name)
        if value is not None and not isinstance(value, dict):
            raise WorkspaceError(f"mappings.{name} must be an object")


def get_endpoint_value(endpoint_obj: Endpoint, path: str, *, query: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,)) -> Any:
    try:
        return migration.api_request(endpoint_obj.target(), "GET", path, query=query, expected=expected)
    except migration.MigrationError as exc:
        raise WorkspaceError(str(exc)) from exc


def request(endpoint_obj: Endpoint, method: str, path: str, *, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,), return_status: bool = False) -> Any:
    try:
        return migration.api_request(
            endpoint_obj.target(),
            method,
            path,
            body=body,
            query=query,
            expected=expected,
            return_status=return_status,
        )
    except migration.MigrationError as exc:
        raise WorkspaceError(str(exc)) from exc


def list_pages(endpoint_obj: Endpoint, path: str, *, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in range(1, 101):
        current = dict(query or {})
        current.update({"page": page, "per_page": 100})
        payload = get_endpoint_value(endpoint_obj, path, query=current)
        if not isinstance(payload, list):
            raise WorkspaceError(f"{endpoint_obj.provider} {path} returned a non-list response")
        result.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            return result
    raise WorkspaceError(f"{endpoint_obj.provider} {path} exceeded the 100-page safety bound")


def safe_record(value: Any, *, include_email: bool = False) -> Any:
    if isinstance(value, list):
        return [safe_record(item, include_email=include_email) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, child in value.items():
        normalized = string(key).lower()
        if normalized == "value":
            result["configured"] = child not in (None, "")
            continue
        if normalized in SENSITIVE_KEYS or "token" in normalized or "password" in normalized:
            continue
        if normalized in {"email", "private_email"} and not include_email:
            continue
        if normalized in {"avatar_url", "web_url", "http_url_to_repo", "ssh_url_to_repo"}:
            result[key] = string(child)
        elif isinstance(child, (dict, list)):
            result[key] = safe_record(child, include_email=include_email)
        else:
            result[key] = child
    return result


def project_record(project: dict[str, Any]) -> dict[str, Any]:
    return {key: safe_record(project.get(key)) for key in SAFE_PROJECT_KEYS if key in project}


def source_group_paths(plan: dict[str, Any]) -> list[str]:
    source = plan["source"]
    values = source.get("group_paths") or []
    return sorted({string(item).strip("/") for item in values if string(item).strip("/")})


def group_is_subgroup(plan: dict[str, Any], path: str) -> bool:
    """Classify groups relative to the explicitly selected migration roots."""
    return string(path).strip("/") not in set(source_group_paths(plan))


def source_project_paths(plan: dict[str, Any]) -> list[str]:
    source = plan["source"]
    values = source.get("project_paths") or []
    return sorted({string(item).strip("/") for item in values if string(item).strip("/")})


def discover_groups(source: Endpoint, plan: dict[str, Any]) -> list[dict[str, Any]]:
    paths = source_group_paths(plan)
    groups: dict[str, dict[str, Any]] = {}
    subgroup_config = surface_config((plan.get("surfaces") or {}).get("subgroups"), "surfaces.subgroups")
    include_subgroups = bool_value(subgroup_config.get("include_subgroups"), subgroup_config["mode"] != "skip")
    for path in paths:
        group = get_endpoint_value(source, f"groups/{quote(path, safe='')}")
        if not isinstance(group, dict):
            raise WorkspaceError(f"GitLab group {path!r} returned an invalid object")
        groups[string(group.get("full_path") or path)] = group
        if include_subgroups:
            for child in list_pages(source, f"groups/{quote(string(group.get('id') or path), safe='')}/subgroups"):
                child_path = string(child.get("full_path") or child.get("path"))
                if child_path:
                    groups[child_path] = child
    result: list[dict[str, Any]] = []
    group_mode = surface_config((plan.get("surfaces") or {}).get("groups"), "surfaces.groups")["mode"]
    subgroup_mode = surface_config((plan.get("surfaces") or {}).get("subgroups"), "surfaces.subgroups")["mode"]
    for path, group in sorted(groups.items()):
        is_subgroup = group_is_subgroup(plan, path)
        if is_subgroup and subgroup_mode == "skip":
            continue
        if not is_subgroup and group_mode == "skip":
            continue
        group_id = string(group.get("id") or path)
        members = list_pages(source, f"groups/{quote(group_id, safe='')}/members/all")
        result.append(
            {
                "id": group.get("id"),
                "full_path": path,
                "name": group.get("name"),
                "path": group.get("path"),
                "description": group.get("description"),
                "visibility": group.get("visibility"),
                "parent_id": group.get("parent_id"),
                "members": [safe_record(member) for member in members],
            }
        )
    return result


def discover_projects(source: Endpoint, plan: dict[str, Any], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths = source_project_paths(plan)
    projects: dict[str, dict[str, Any]] = {}
    for path in paths:
        project = get_endpoint_value(source, f"projects/{quote(path, safe='')}")
        if not isinstance(project, dict):
            raise WorkspaceError(f"GitLab project {path!r} returned an invalid object")
        projects[string(project.get("path_with_namespace") or path)] = project
    for group_path in source_group_paths(plan):
        group = next((item for item in groups if item.get("full_path") == group_path), None)
        group_id = string((group or {}).get("id") or group_path)
        for project in list_pages(
            source,
            f"groups/{quote(group_id, safe='')}/projects",
            query={"include_subgroups": bool_value(surface_config((plan.get("surfaces") or {}).get("subgroups"), "surfaces.subgroups").get("include_subgroups"), True)},
        ):
            path = string(project.get("path_with_namespace"))
            if path:
                projects[path] = get_endpoint_value(source, f"projects/{quote(path, safe='')}" )
    if bool_value(plan["source"].get("all_available_projects")):
        for project in list_pages(source, "projects", query={"membership": True, "archived": False}):
            path = string(project.get("path_with_namespace"))
            if path:
                projects[path] = get_endpoint_value(source, f"projects/{quote(path, safe='')}" )
    return [project for _, project in sorted(projects.items())]


def destination_name(plan: dict[str, Any], project: dict[str, Any]) -> tuple[str, str]:
    path = string(project.get("path_with_namespace"))
    mappings = (plan.get("mappings") or {}).get("projects") or {}
    mapping = mappings.get(path) if isinstance(mappings, dict) else None
    if isinstance(mapping, str):
        target = mapping.strip("/").split("/")
        if len(target) == 2:
            return target[0], target[1]
    if isinstance(mapping, dict):
        owner = string(mapping.get("owner") or mapping.get("organization"))
        repo = string(mapping.get("repo") or mapping.get("name"))
        if owner and repo:
            return owner, repo
    namespace = project.get("namespace") or {}
    full_path = string(namespace.get("full_path") or "").strip("/")
    owner = string((plan.get("destination") or {}).get("default_owner"))
    if not owner:
        owner = re.sub(r"[^A-Za-z0-9_.-]+", "-", full_path.split("/")[-1] if full_path else "migrated") or "migrated"
    return owner, string(project.get("path") or path.rsplit("/", 1)[-1])


def destination_owner_kind(plan: dict[str, Any], project: dict[str, Any]) -> str:
    mappings = (plan.get("mappings") or {}).get("projects") or {}
    path = string(project.get("path_with_namespace"))
    mapping = mappings.get(path) if isinstance(mappings, dict) else None
    if isinstance(mapping, dict) and string(mapping.get("owner_kind")):
        return string(mapping.get("owner_kind")).lower()
    configured = string((plan.get("destination") or {}).get("owner_kind"))
    if configured:
        return configured.lower()
    namespace = project.get("namespace") or {}
    return "organization" if string(namespace.get("kind")) == "group" else "user"


def destination_git_url(plan: dict[str, Any], owner: str, repo: str) -> str:
    destination = plan["destination"]
    template = string(destination.get("git_url_template"))
    if template:
        return template.format(owner=owner, repo=repo)
    parsed = urlsplit(string(destination["api_url"]))
    base_path = parsed.path
    if base_path.endswith("/api/v1"):
        base_path = base_path[: -len("/api/v1")]
    base = urlunsplit((parsed.scheme, parsed.netloc, base_path.rstrip("/"), "", ""))
    return f"{base}/{quote(owner, safe='')}/{quote(repo, safe='')}.git"


def file_tree(source: Endpoint, project: dict[str, Any]) -> list[dict[str, Any]]:
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    branch = string(project.get("default_branch") or "main")
    return list_pages(source, f"projects/{quote(project_id, safe='')}/repository/tree", query={"recursive": True, "ref": branch})


def source_file(source: Endpoint, project: dict[str, Any], path: str) -> str:
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    response = get_endpoint_value(
        source,
        f"projects/{quote(project_id, safe='')}/repository/files/{quote(path, safe='')}",
        query={"ref": string(project.get("default_branch") or "main")},
    )
    if not isinstance(response, dict):
        raise WorkspaceError(f"GitLab repository file {path!r} returned an invalid object")
    content = string(response.get("content"))
    try:
        decoded = base64.b64decode(content, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise WorkspaceError(f"GitLab repository file {path!r} is not valid UTF-8 base64 content") from exc
    if len(decoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise WorkspaceError(f"GitLab repository file {path!r} exceeds the 2 MiB safety limit")
    return decoded


def discover_ci(source: Endpoint, plan: dict[str, Any], project: dict[str, Any]) -> list[dict[str, Any]]:
    config = surface_config((plan.get("surfaces") or {}).get("ci"), "surfaces.ci")
    globs = config.get("source_globs") or list(DEFAULT_PIPELINE_GLOBS)
    if not isinstance(globs, list) or not all(string(item) for item in globs):
        raise WorkspaceError("surfaces.ci.source_globs must contain non-empty strings")
    files: list[dict[str, Any]] = []
    for item in file_tree(source, project):
        path = string(item.get("path"))
        if not path or not any(fnmatch.fnmatch(path, string(pattern)) for pattern in globs):
            continue
        record: dict[str, Any] = {"path": path, "type": item.get("type"), "id": item.get("id"), "mode": config["mode"]}
        if bool_value(config.get("include_content")):
            record["content"] = source_file(source, project, path)
        files.append(record)
    return files


def variable_metadata(source: Endpoint, project: dict[str, Any], groups: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    values: list[dict[str, Any]] = []
    scopes = config.get("scopes") or ["project", "group"]
    if "project" in scopes:
        for item in list_pages(source, f"projects/{quote(project_id, safe='')}/variables"):
            values.append({"source_scope": "project", **safe_record(item)})
    if "group" in scopes:
        namespace = project.get("namespace") or {}
        full_path = string(namespace.get("full_path") or "").strip("/")
        parts = [part for part in full_path.split("/") if part]
        for index in range(1, len(parts) + 1):
            group_path = "/".join(parts[:index])
            group_id = next((string(item.get("id")) for item in groups if item.get("full_path") == group_path), group_path)
            for item in list_pages(source, f"groups/{quote(group_id, safe='')}/variables"):
                values.append({"source_scope": f"group:{group_path}", **safe_record(item)})
    if "instance" in scopes:
        for item in list_pages(source, "admin/ci/variables"):
            values.append({"source_scope": "instance", **safe_record(item)})
    return values


def discover_runners(source: Endpoint, project: dict[str, Any], groups: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    result: dict[str, dict[str, Any]] = {}
    if "project" in (config.get("scopes") or ["project"]):
        for item in list_pages(source, f"projects/{quote(project_id, safe='')}/runners"):
            record = safe_record(item)
            record["source_scope"] = "project"
            result[f"project:{item.get('id')}"] = record
    if "group" in (config.get("scopes") or []):
        namespace = project.get("namespace") or {}
        group_path = string(namespace.get("full_path"))
        group_id = next((string(item.get("id")) for item in groups if item.get("full_path") == group_path), group_path)
        for item in list_pages(source, f"groups/{quote(group_id, safe='')}/runners"):
            record = safe_record(item)
            record["source_scope"] = f"group:{group_path}"
            result[f"group:{item.get('id')}"] = record
    if bool_value(config.get("include_instance")):
        for item in list_pages(source, "runners/all"):
            record = safe_record(item)
            record["source_scope"] = "instance"
            result[f"instance:{item.get('id')}"] = record
    return list(result.values())


def discover_pipelines(source: Endpoint, project: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    result: dict[str, Any] = {"runs": [], "schedules": [], "triggers": []}
    if bool_value(config.get("include_history"), True):
        result["runs"] = [safe_record(item) for item in list_pages(source, f"projects/{quote(project_id, safe='')}/pipelines")]
    if bool_value(config.get("include_schedules"), True):
        result["schedules"] = [safe_record(item) for item in list_pages(source, f"projects/{quote(project_id, safe='')}/pipeline_schedules")]
    if bool_value(config.get("include_triggers")):
        result["triggers"] = [safe_record(item) for item in list_pages(source, f"projects/{quote(project_id, safe='')}/triggers")]
    return result


def discover_users(source: Endpoint, plan: dict[str, Any]) -> list[dict[str, Any]]:
    config = surface_config((plan.get("surfaces") or {}).get("users"), "surfaces.users")
    query = {key: value for key, value in (("active", config.get("active")), ("blocked", config.get("blocked")), ("external", config.get("external"))) if value is not None}
    usernames = plan["source"].get("usernames") or []
    if usernames:
        users = []
        for username in usernames:
            candidates = list_pages(source, "users", query={**query, "username": string(username)})
            user = next((item for item in candidates if string(item.get("username")) == string(username)), None)
            if not user:
                raise WorkspaceError(f"GitLab user {username!r} was not found")
            users.append(user)
    else:
        users = list_pages(source, "users", query=query)
    return [safe_record(item) for item in users]


def export_workspace(plan: dict[str, Any]) -> dict[str, Any]:
    source = endpoint(plan, "source", "gitlab")
    surfaces = plan.get("surfaces") or {}
    groups = discover_groups(source, plan) if any(surface_config(surfaces.get(name), f"surfaces.{name}")["mode"] != "skip" for name in ("groups", "subgroups", "projects", "repositories", "variables", "runners")) else []
    projects = discover_projects(source, plan, groups) if any(surface_config(surfaces.get(name), f"surfaces.{name}")["mode"] != "skip" for name in ("projects", "repositories", "variables", "runners", "ci", "pipelines")) else []
    project_index: list[dict[str, Any]] = []
    for project in projects:
        owner, repo = destination_name(plan, project)
        project_index.append(
            {
                "project": project_record(project),
                "destination": {
                    "owner": owner,
                    "repo": repo,
                    "owner_kind": destination_owner_kind(plan, project),
                    "git_url": destination_git_url(plan, owner, repo),
                },
            }
        )
    snapshot: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "generated_at": utc_now(),
        "plan_sha256": canonical_digest(plan),
        "source": {"api_url": migration.redact_url(source.api_url), "provider": source.provider},
        "surfaces": {},
        "indexes": {"projects": project_index},
    }
    for name in SURFACES:
        config = surface_config(surfaces.get(name), f"surfaces.{name}")
        if config["mode"] == "skip":
            snapshot["surfaces"][name] = {"mode": "skip", "items": []}
            continue
        if name == "users":
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": discover_users(source, plan)}
        elif name in {"groups", "subgroups"}:
            items = [item for item in groups if group_is_subgroup(plan, string(item.get("full_path"))) == (name == "subgroups")]
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": items}
        elif name in {"projects", "repositories"}:
            items = project_index
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": items}
        elif name == "ci":
            items = []
            for project in projects:
                path = string(project.get("path_with_namespace"))
                items.append({"project": path, "files": discover_ci(source, plan, project)})
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": items}
        elif name == "variables":
            snapshot["surfaces"][name] = {
                "mode": config["mode"],
                "items": [
                    {"project": string(project.get("path_with_namespace")), "variables": variable_metadata(source, project, groups, config)}
                    for project in projects
                ],
            }
        elif name == "runners":
            snapshot["surfaces"][name] = {
                "mode": config["mode"],
                "items": [
                    {"project": string(project.get("path_with_namespace")), "runners": discover_runners(source, project, groups, config)}
                    for project in projects
                ],
            }
        elif name == "pipelines":
            snapshot["surfaces"][name] = {
                "mode": config["mode"],
                "items": [
                    {"project": string(project.get("path_with_namespace")), "pipelines": discover_pipelines(source, project, config)}
                    for project in projects
                ],
            }
    snapshot["counts"] = {name: len(value.get("items") or []) for name, value in snapshot["surfaces"].items()}
    return snapshot


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def require_snapshot(plan: dict[str, Any], path: Path) -> dict[str, Any]:
    snapshot = load_json(path)
    if int(snapshot.get("snapshot_version") or 0) != SNAPSHOT_VERSION:
        raise WorkspaceError(f"{path}: unsupported snapshot version")
    if snapshot.get("plan_sha256") != canonical_digest(plan):
        raise WorkspaceError(f"{path}: snapshot was produced from a different plan")
    if not isinstance(snapshot.get("surfaces"), dict):
        raise WorkspaceError(f"{path}: snapshot.surfaces must be an object")
    return snapshot


def mappings_for(plan: dict[str, Any], surface: str) -> dict[str, Any]:
    raw = (plan.get("mappings") or {}).get(surface) or {}
    return raw if isinstance(raw, dict) else {}


def mapped_name(plan: dict[str, Any], surface: str, source_name: str, default: str) -> str:
    mapping = mappings_for(plan, surface).get(source_name)
    if isinstance(mapping, str):
        return mapping.strip()
    if isinstance(mapping, dict):
        return string(mapping.get("target") or mapping.get("target_name") or mapping.get("name") or default)
    return default


def source_mode(plan: dict[str, Any], surface: str) -> str:
    return surface_config((plan.get("surfaces") or {}).get(surface), f"surfaces.{surface}")["mode"]


def forgejo_user(destination: Endpoint, username: str) -> tuple[int, dict[str, Any]]:
    return request(destination, "GET", f"users/{quote(username, safe='')}", expected=(200, 404), return_status=True)


def require_named_api_record(
    status: int,
    record: Any,
    expected_name: str,
    label: str,
) -> dict[str, Any]:
    if status != 200 or not isinstance(record, dict):
        raise WorkspaceError(f"{label} {expected_name!r} was not readable after reconciliation")
    actual_name = string(record.get("login") or record.get("username"))
    if actual_name.casefold() != expected_name.casefold():
        raise WorkspaceError(
            f"{label} read-back mismatch: expected {expected_name!r}, got {actual_name or '<missing>'!r}"
        )
    return record


def validate_unique_user_targets(
    plan: dict[str, Any],
    config: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    targets: dict[str, str] = {}
    for item in items:
        source_username = string(item.get("username"))
        if not source_username or (
            bool_value(item.get("is_bot")) and bool_value(config.get("skip_bots"), True)
        ):
            continue
        target_username = mapped_name(plan, "users", source_username, source_username)
        if not target_username:
            raise WorkspaceError(
                f"user {source_username!r} maps to an empty Forgejo username"
            )
        target_key = target_username.casefold()
        previous_source = targets.get(target_key)
        if previous_source is not None and previous_source.casefold() != source_username.casefold():
            raise WorkspaceError(
                f"users {previous_source!r} and {source_username!r} map to the same "
                f"Forgejo username {target_username!r}; user mappings must have unique targets"
            )
        targets[target_key] = source_username


def import_users(plan: dict[str, Any], destination: Endpoint, snapshot: dict[str, Any]) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("users"), "surfaces.users")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "verified": config["mode"] in {"skip", "export", "mapped", "manual"}, "created": 0, "existing": 0}
    created = 0
    existing = 0
    items = snapshot["surfaces"].get("users", {}).get("items", [])
    validate_unique_user_targets(plan, config, items)
    for item in items:
        source_username = string(item.get("username"))
        if not source_username or bool_value(item.get("is_bot")) and bool_value(config.get("skip_bots"), True):
            continue
        target_username = mapped_name(plan, "users", source_username, source_username)
        status, current = forgejo_user(destination, target_username)
        if status == 200:
            require_named_api_record(status, current, target_username, "Forgejo user")
            existing += 1
            continue
        env_map = config.get("password_env_by_username") or {}
        password_env = string(env_map.get(source_username) if isinstance(env_map, dict) else "") or string(config.get("default_password_env"))
        password = os.environ.get(password_env, "") if password_env else ""
        if not password:
            raise WorkspaceError(f"user {source_username!r} requires password environment variable {password_env or '<missing>'}")
        email = string(item.get("public_email")) or f"{target_username}@{string(config.get('placeholder_email_domain'), 'migration.invalid')}"
        body = {
            "username": target_username,
            "login_name": target_username,
            "email": email,
            "password": password,
            "must_change_password": True,
            "send_notify": False,
        }
        request(destination, "POST", "admin/users", body=body, expected=(201, 200))
        verified_status, verified_user = forgejo_user(destination, target_username)
        require_named_api_record(verified_status, verified_user, target_username, "Forgejo user")
        created += 1
    return {
        "mode": config["mode"],
        "created": created,
        "existing": existing,
        "verified_count": created + existing,
        "verified": True,
    }


def forgejo_org(destination: Endpoint, name: str) -> tuple[int, dict[str, Any]]:
    return request(destination, "GET", f"orgs/{quote(name, safe='')}", expected=(200, 404), return_status=True)


def reconcile_team_membership(
    destination: Endpoint,
    teams: dict[int, int],
    selected_level: int | None,
    username: str,
) -> None:
    encoded_username = quote(username, safe="")
    for level, team_id in teams.items():
        member_path = f"teams/{team_id}/members/{encoded_username}"
        if level == selected_level:
            request(destination, "PUT", member_path, expected=(204, 200, 201))
        else:
            request(destination, "DELETE", member_path, expected=(204, 200, 404))

    for level, team_id in teams.items():
        members = list_pages(destination, f"teams/{team_id}/members")
        present = any(
            string(member.get("login") or member.get("username")).casefold() == username.casefold()
            for member in members
        )
        if present != (level == selected_level):
            expected = "present" if level == selected_level else "absent"
            raise WorkspaceError(
                f"Forgejo team membership read-back mismatch for {username!r} in team {team_id}: "
                f"expected {expected}"
            )


def import_groups(plan: dict[str, Any], destination: Endpoint, snapshot: dict[str, Any]) -> dict[str, Any]:
    group_items = []
    for surface in ("groups", "subgroups"):
        if source_mode(plan, surface) == "managed":
            group_items.extend(snapshot["surfaces"].get(surface, {}).get("items", []))
    if not group_items:
        return {"mode": "skip", "created": 0, "existing": 0, "verified": True}
    created = 0
    existing = 0
    org_by_path: dict[str, str] = {}
    mappings = mappings_for(plan, "groups")
    group_configs = {
        "group": surface_config((plan.get("surfaces") or {}).get("groups"), "surfaces.groups"),
        "subgroup": surface_config((plan.get("surfaces") or {}).get("subgroups"), "surfaces.subgroups"),
    }
    for item in sorted(group_items, key=lambda entry: string(entry.get("full_path")).count("/")):
        source_path = string(item.get("full_path"))
        default_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_path.replace("/", "-"))[:100] or "migrated"
        target_name = mapped_name(plan, "groups", source_path, default_name)
        status, current = forgejo_org(destination, target_name)
        if status == 404:
            body = {
                "username": target_name,
                "full_name": string(item.get("name") or target_name),
                "description": string(item.get("description")),
                "visibility": "public" if string(item.get("visibility")) == "public" else "private",
            }
            request(destination, "POST", "orgs", body=body, expected=(201, 200))
            created += 1
        else:
            require_named_api_record(status, current, target_name, "Forgejo organization")
            existing += 1
        verified_status, verified_org = forgejo_org(destination, target_name)
        require_named_api_record(
            verified_status,
            verified_org,
            target_name,
            "Forgejo organization",
        )
        org_by_path[source_path] = target_name
        policy = string(group_configs["subgroup" if group_is_subgroup(plan, source_path) else "group"].get("members_mode") or "import").lower()
        if isinstance(mappings, dict):
            mapping = mappings.get(source_path)
            if isinstance(mapping, dict) and string(mapping.get("members_mode")):
                policy = string(mapping.get("members_mode")).lower()
        if source_mode(plan, "users") != "managed" and policy not in {"skip", "mapped", "manual"}:
            raise WorkspaceError(
                f"group {source_path!r} has members but users are not managed; set members_mode=skip|mapped|manual"
            )
        if policy in {"skip", "mapped", "manual"}:
            continue
        members = item.get("members") or []
        team_definitions = (
            (50, "gitlab-owners", "admin"),
            (40, "gitlab-maintainers", "write"),
            (30, "gitlab-developers", "write"),
            (20, "gitlab-reporters", "read"),
            (10, "gitlab-guests", "read"),
        )
        teams = {
            level: ensure_team(destination, target_name, team_name, permission)
            for level, team_name, permission in team_definitions
        }
        for member in members:
            access_level = int(member.get("access_level") or 0)
            selected = next((level for level, _name, _permission in team_definitions if access_level >= level), None)
            username = mapped_name(plan, "users", string(member.get("username")), string(member.get("username")))
            if not username:
                raise WorkspaceError(f"GitLab group {source_path!r} contains a member without a username")
            reconcile_team_membership(destination, teams, selected, username)
    return {
        "mode": "managed",
        "created": created,
        "existing": existing,
        "organizations": org_by_path,
        "verified_count": created + existing,
        "verified": True,
    }


def ensure_team(destination: Endpoint, org: str, name: str, permission: str) -> int:
    teams = list_pages(destination, f"orgs/{quote(org, safe='')}/teams")
    existing = next((item for item in teams if string(item.get("name")) == name), None)
    if existing and existing.get("id") is not None:
        actual_permission = string(existing.get("permission")).lower()
        if actual_permission != permission:
            raise WorkspaceError(
                f"Forgejo team {org}/{name} permission mismatch: "
                f"expected {permission!r}, got {actual_permission or '<missing>'!r}"
            )
        return int(existing["id"])
    body = {
        "name": name,
        "description": "Imported GitLab access mapping",
        "permission": permission,
        "can_create_org_repo": False,
        "includes_all_repositories": False,
    }
    created = request(destination, "POST", f"orgs/{quote(org, safe='')}/teams", body=body, expected=(201, 200))
    if not isinstance(created, dict) or created.get("id") is None:
        raise WorkspaceError(f"Forgejo team create returned no id for {org}/{name}")
    team_id = int(created["id"])
    verified_teams = list_pages(destination, f"orgs/{quote(org, safe='')}/teams")
    verified = next(
        (item for item in verified_teams if int(item.get("id") or 0) == team_id),
        None,
    )
    if not verified or string(verified.get("permission")).lower() != permission:
        raise WorkspaceError(f"Forgejo team {org}/{name} did not verify after creation")
    return team_id


def destination_authenticated_username(destination: Endpoint) -> str:
    current = request(destination, "GET", "user", expected=(200,))
    if not isinstance(current, dict):
        raise WorkspaceError("Forgejo authenticated-user response was not an object")
    username = string(current.get("login") or current.get("username"))
    if not username:
        raise WorkspaceError("Forgejo authenticated-user response did not include login")
    return username


def repository_create_path(owner: str, owner_kind: str, authenticated_username: str | None) -> str:
    if owner_kind in {"organization", "organisation", "org", "group"}:
        return f"orgs/{quote(owner, safe='')}/repos"
    if owner_kind == "user":
        if not authenticated_username:
            raise WorkspaceError(f"authenticated Forgejo username is required to create user repository {owner!r}")
        if owner.casefold() == authenticated_username.casefold():
            return "user/repos"
        return f"admin/users/{quote(owner, safe='')}/repos"
    raise WorkspaceError(f"unsupported Forgejo repository owner_kind {owner_kind!r} for {owner}")


def ensure_repository(destination: Endpoint, owner: str, repo: str, owner_kind: str, project: dict[str, Any]) -> dict[str, Any]:
    status, current = request(destination, "GET", f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}", expected=(200, 404), return_status=True)
    private = string(project.get("visibility")) != "public"
    if status == 404:
        body = {
            "name": repo,
            "description": string(project.get("description")),
            "private": private,
            "auto_init": False,
        }
        authenticated_username = destination_authenticated_username(destination) if owner_kind == "user" else None
        path = repository_create_path(owner, owner_kind, authenticated_username)
        current = request(destination, "POST", path, body=body, expected=(201, 200))
        action = "created"
    else:
        action = "existing"
        if not isinstance(current, dict):
            raise WorkspaceError(f"Forgejo repository probe returned an invalid object for {owner}/{repo}")
        patch = {"description": string(project.get("description")), "private": private, "archived": bool_value(project.get("archived"))}
        current = request(destination, "PATCH", f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}", body=patch, expected=(200, 201))
    return {"owner": owner, "repo": repo, "action": action, "verified": isinstance(current, dict)}


def repo_plan_from_item(plan: dict[str, Any], item: dict[str, Any]) -> migration.RepoPlan:
    project = item["project"]
    source_url = string(project.get("http_url_to_repo"))
    destination = item["destination"]
    if not source_url or not string(destination.get("git_url")):
        raise WorkspaceError("repository snapshot item is missing source or destination Git URL")
    source_api = endpoint(plan, "source", "gitlab")
    destination_api = endpoint(plan, "destination", "forgejo")
    return migration.RepoPlan(
        name=string(project.get("path_with_namespace") or project.get("name")),
        source_url=source_url,
        destination_url=string(destination["git_url"]),
        source_wiki_url=None,
        destination_wiki_url=None,
        source_provider="gitlab",
        destination_provider="forgejo",
        source_api_url=source_api.api_url,
        destination_api_url=destination_api.api_url,
        source_api_repository=string(project.get("path_with_namespace")),
        destination_api_repository=f"{destination['owner']}/{destination['repo']}",
        source_token_env=source_api.token_env,
        destination_token_env=destination_api.token_env,
        destination_create="false",
        destination_private=string(project.get("visibility")) != "public",
        destination_description=string(project.get("description")),
        destination_namespace_id=None,
        wiki="false",
        lfs="auto" if bool_value(project.get("lfs_enabled")) else "false",
        metadata={surface: "skip" for surface in migration.SUPPORTED_METADATA_SURFACES},
    )


def import_repositories(plan: dict[str, Any], snapshot: dict[str, Any], destination: Endpoint, work_dir: Path) -> dict[str, Any]:
    repository_mode = source_mode(plan, "repositories")
    project_mode = source_mode(plan, "projects")
    items = snapshot["surfaces"].get("repositories", {}).get("items", [])
    if not items and project_mode != "skip":
        items = snapshot["surfaces"].get("projects", {}).get("items", [])
    results: list[dict[str, Any]] = []
    for item in items:
        project = item["project"]
        owner = string(item["destination"]["owner"])
        repo = string(item["destination"]["repo"])
        owner_kind = string(item["destination"].get("owner_kind") or "organization")
        results.append(ensure_repository(destination, owner, repo, owner_kind, project))
        if repository_mode == "managed":
            result = migration.migrate_repo(repo_plan_from_item(plan, item), work_dir)
            results[-1]["git"] = result
    mode = "managed" if project_mode == "managed" or repository_mode == "managed" else repository_mode
    return {"mode": mode, "items": results, "verified": all(item.get("verified") and item.get("git", {}).get("verified", True) for item in results)}


def variable_identity(item: dict[str, Any]) -> str:
    return f"{string(item.get('source_scope'))}:{string(item.get('key'))}:{string(item.get('environment_scope') or '*')}"


def variable_mapping(plan: dict[str, Any], identity: str, key: str) -> dict[str, Any]:
    mapping = mappings_for(plan, "variables").get(identity) or mappings_for(plan, "variables").get(key)
    if isinstance(mapping, str):
        return {"mode": "managed", "target_name": mapping}
    if isinstance(mapping, dict):
        return dict(mapping)
    scope = identity.split(":", 1)[0]
    if scope == "project":
        target_name = key
    elif scope == "instance":
        target_name = f"GL_INSTANCE_{key}"
    else:
        target_name = f"GL_GROUP_{key}"
    return {"mode": "managed", "target_name": target_name}


def project_index(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    indexes = snapshot.get("indexes") or {}
    indexed = indexes.get("projects") if isinstance(indexes, dict) else None
    if isinstance(indexed, list):
        return [item for item in indexed if isinstance(item, dict)]
    # Accept snapshots produced by the initial implementation.
    return [
        item
        for item in snapshot.get("surfaces", {}).get("projects", {}).get("items", [])
        if isinstance(item, dict)
    ]


def project_snapshot_for(snapshot: dict[str, Any], path: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in project_index(snapshot)
            if string(item.get("project", {}).get("path_with_namespace")) == path
        ),
        None,
    )


def source_variable_path(metadata: dict[str, Any], project_id: str) -> str:
    key = quote(string(metadata.get("key")), safe="")
    scope = string(metadata.get("source_scope"))
    if scope == "project":
        return f"projects/{quote(project_id, safe='')}/variables/{key}"
    if scope.startswith("group:"):
        return f"groups/{quote(scope.split(':', 1)[1], safe='')}/variables/{key}"
    if scope == "instance":
        return f"admin/ci/variables/{key}"
    raise WorkspaceError(f"unsupported GitLab variable scope {scope!r}")


def source_variable_query(metadata: dict[str, Any]) -> dict[str, str]:
    return {"filter[environment_scope]": string(metadata.get("environment_scope") or "*")}


def planned_variable_imports(
    plan: dict[str, Any],
    project_path: str,
    variables: Any,
) -> list[dict[str, Any]]:
    """Validate variable mappings before any Woodpecker mutation for a repo."""
    if not isinstance(variables, list):
        raise WorkspaceError(f"variables for {project_path} must be a list")
    planned: list[dict[str, Any]] = []
    targets: dict[str, str] = {}
    for metadata in variables:
        if not isinstance(metadata, dict):
            raise WorkspaceError(f"variable metadata for {project_path} must be an object")
        key = string(metadata.get("key"))
        if not key:
            raise WorkspaceError(f"variable metadata for {project_path} is missing key")
        identity = variable_identity(metadata)
        mapping = variable_mapping(plan, identity, key)
        mode = string(mapping.get("mode") or "managed").lower()
        if mode in {"skip", "skipped", "manual", "mapped"}:
            planned.append({"metadata": metadata, "identity": identity, "mapping": mapping, "mode": mode})
            continue
        if mode != "managed":
            raise WorkspaceError(f"unsupported variable mapping mode {mode!r} for {identity}")
        target_name = string(mapping.get("target_name") or key)
        if not target_name:
            raise WorkspaceError(f"managed variable {identity} has no Woodpecker target name")
        normalized_target = target_name.casefold()
        previous_identity = targets.get(normalized_target)
        if previous_identity is not None:
            raise WorkspaceError(
                f"variables for {project_path} map {previous_identity!r} and {identity!r} "
                f"to the same Woodpecker secret {target_name!r}; provide unique full-identity mappings "
                "or mark one mapping manual, mapped, or skip"
            )
        targets[normalized_target] = identity
        planned.append(
            {
                "metadata": metadata,
                "identity": identity,
                "mapping": mapping,
                "mode": mode,
                "target_name": target_name,
            }
        )
    return planned


def import_variables(plan: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("variables"), "surfaces.variables")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "verified": True}
    # Values are deliberately read from GitLab at import time, never from the snapshot.
    source = endpoint(plan, "source", "gitlab")
    services = plan.get("services") or {}
    wp_config = services.get("woodpecker") or {}
    if not string(wp_config.get("api_url")) or not string(wp_config.get("token_env")):
        raise WorkspaceError("Woodpecker service configuration is required to import variables")
    import forge_cutover as cutover

    wp = cutover.service_target("woodpecker", wp_config)
    results: list[dict[str, Any]] = []
    planned_projects: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for project_item in snapshot["surfaces"].get("variables", {}).get("items", []):
        project_path = string(project_item.get("project"))
        project_snapshot = project_snapshot_for(snapshot, project_path)
        if not project_snapshot:
            continue
        planned = planned_variable_imports(plan, project_path, project_item.get("variables", []))
        planned_projects.append((project_path, project_snapshot, planned))
    for project_path, project_snapshot, planned in planned_projects:
        managed_items = [item for item in planned if item["mode"] == "managed"]
        for item in planned:
            if item["mode"] in {"skip", "skipped", "manual", "mapped"}:
                results.append(
                    {
                        "project": project_path,
                        "identity": item["identity"],
                        "mode": item["mode"],
                        "verified": True,
                    }
                )
        if not managed_items:
            continue
        owner = string(project_snapshot["destination"]["owner"])
        repo = string(project_snapshot["destination"]["repo"])
        wp_repo = cutover.woodpecker_lookup(wp, f"{owner}/{repo}", required=False)
        if not wp_repo:
            wp_repo = cutover.service_request(wp, "POST", "api/repos", body={"clone_url": string(project_snapshot["destination"]["git_url"]), "repo": f"{owner}/{repo}"}, expected=(200, 201))
        repo_id = int(wp_repo.get("id") or 0)
        if repo_id <= 0:
            raise WorkspaceError(f"Woodpecker repository id is missing for {owner}/{repo}")
        source_project = string(project_snapshot["project"].get("id") or project_path)
        for item in managed_items:
            metadata = item["metadata"]
            identity = item["identity"]
            mapping = item["mapping"]
            mode = item["mode"]
            live = request(
                source,
                "GET",
                source_variable_path(metadata, source_project),
                query=source_variable_query(metadata),
                expected=(200,),
            )
            value = string(live.get("value") if isinstance(live, dict) else "")
            if not value:
                raise WorkspaceError(f"GitLab variable {identity} has no readable value; map it manually")
            target_name = item["target_name"]
            result = cutover.woodpecker_secret_upsert(wp, repo_id, target_name, value)
            results.append({"project": project_path, "identity": identity, "target_name": target_name, "mode": mode, "verified": result.get("verified") is True})
    return {"mode": config["mode"], "items": results, "verified": all(item.get("verified") is True for item in results)}


def prepare_ci_checkout(destination_url: str, repo_root: Path) -> None:
    """Reuse a dedicated checkout so interrupted imports can be retried safely."""
    repo_root.parent.mkdir(parents=True, exist_ok=True)
    if repo_root.exists():
        if repo_root.is_symlink() or not repo_root.is_dir():
            raise WorkspaceError(f"CI checkout path is not a safe directory: {repo_root}")
        if (repo_root / ".git").exists():
            migration.run_command(["git", "-C", str(repo_root), "remote", "set-url", "origin", destination_url], check=True)
            migration.run_command(["git", "-C", str(repo_root), "fetch", "--quiet", "--prune", "origin"], check=True)
            migration.run_command(["git", "-C", str(repo_root), "reset", "--hard", "origin/HEAD"], check=True)
            migration.run_command(["git", "-C", str(repo_root), "clean", "-fdx"], check=True)
            return
        shutil.rmtree(repo_root)
    migration.run_command(["git", "clone", "--quiet", destination_url, str(repo_root)], check=True)


def safe_ci_destination(repo_root: Path, value: str) -> tuple[Path, str]:
    normalized = value.replace("\\", "/").strip()
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 for character in normalized)
    ):
        raise WorkspaceError(f"unsafe CI destination path {value!r}")
    relative = PurePosixPath(*parts).as_posix()
    destination = repo_root.joinpath(*parts)
    root_resolved = repo_root.resolve()
    destination_resolved = destination.resolve(strict=False)
    if root_resolved != destination_resolved and root_resolved not in destination_resolved.parents:
        raise WorkspaceError(f"CI destination path escapes checkout: {value!r}")
    return destination, relative


def verify_ci_remote_files(repo_root: Path, rendered_files: list[tuple[str, str]]) -> list[dict[str, str]]:
    migration.run_command(
        ["git", "-C", str(repo_root), "fetch", "--quiet", "origin"],
        check=True,
    )
    verified: list[dict[str, str]] = []
    for path, expected_content in rendered_files:
        result = migration.run_command(
            ["git", "-C", str(repo_root), "show", f"origin/HEAD:{path}"],
            check=True,
        )
        expected_digest = hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
        actual_digest = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
        if actual_digest != expected_digest:
            raise WorkspaceError(
                f"converted CI read-back mismatch for {path!r}: "
                f"expected sha256:{expected_digest}, got sha256:{actual_digest}"
            )
        verified.append({"path": path, "sha256": expected_digest})
    return verified


def commit_ci_changes(repo_root: Path, paths: list[str]) -> str:
    migration.run_command(["git", "-C", str(repo_root), "add", *paths], check=True)
    staged = migration.run_command(
        ["git", "-C", str(repo_root), "diff", "--cached", "--quiet"],
        check=False,
    )
    if staged.returncode == 0:
        return "unchanged"
    if staged.returncode != 1:
        raise WorkspaceError(
            f"git staged-change probe failed for {repo_root.name!r} with rc={staged.returncode}"
        )
    migration.run_command(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "user.name=platform-forge-workspace",
            "-c",
            "user.email=forge-workspace@invalid",
            "commit",
            "-m",
            "Import GitLab CI as Woodpecker workflow",
        ],
        check=True,
    )
    migration.run_command(["git", "-C", str(repo_root), "push", "origin", "HEAD"], check=True)
    return "committed"


def import_ci(plan: dict[str, Any], snapshot: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("ci"), "surfaces.ci")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "verified": True}
    mappings = config.get("destination_mappings") or {".gitlab-ci.yml": ".woodpecker.yml"}
    if not isinstance(mappings, dict):
        raise WorkspaceError("surfaces.ci.destination_mappings must be an object")
    conversion_config = dict(config.get("conversion") or {})
    conversion_config.setdefault("deployment_gate_marker", string(config.get("deployment_gate_marker"), "FORGE_WORKSPACE_DEPLOYMENT_ENABLED"))
    results: list[dict[str, Any]] = []
    for project_item in snapshot["surfaces"].get("ci", {}).get("items", []):
        project_path = string(project_item.get("project"))
        source_item = project_snapshot_for(snapshot, project_path)
        if not source_item:
            raise WorkspaceError(f"CI project {project_path} is missing a repository destination mapping")
        rendered_files: list[tuple[str, str]] = []
        for file_item in project_item.get("files", []):
            source_path = string(file_item.get("path"))
            configured_destination = string(mappings.get(source_path))
            if not configured_destination:
                continue
            content = file_item.get("content")
            if not isinstance(content, str):
                raise WorkspaceError(f"CI content for {project_path}:{source_path} is absent; export with include_content=true")
            rendered, report = pipeline.convert_pipeline("gitlab", content, source_path, conversion_config)
            if not report.get("supported"):
                raise WorkspaceError(f"GitLab pipeline {project_path}:{source_path} has unsupported constructs")
            rendered_files.append((configured_destination, rendered))
        if not rendered_files:
            results.append({"project": project_path, "files": [], "verified": True, "action": "no-files"})
            continue
        destination_url = string(source_item["destination"]["git_url"])
        repo_root = work_dir / "ci" / re.sub(r"[^A-Za-z0-9_.-]+", "-", project_path)
        prepare_ci_checkout(destination_url, repo_root)
        safe_rendered_files: list[tuple[str, str]] = []
        for path, content in rendered_files:
            destination_file, safe_path = safe_ci_destination(repo_root, path)
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(destination_file, content)
            safe_rendered_files.append((safe_path, content))
        paths = [path for path, _content in safe_rendered_files]
        action = commit_ci_changes(repo_root, paths)
        verified_files = verify_ci_remote_files(repo_root, safe_rendered_files)
        results.append(
            {
                "project": project_path,
                "files": paths,
                "verified_files": verified_files,
                "verified": True,
                "action": action,
            }
        )
    return {"mode": config["mode"], "items": results, "verified": all(item.get("verified") is True for item in results)}


def import_runners(plan: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("runners"), "surfaces.runners")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "verified": True}
    services = plan.get("services") or {}
    import forge_cutover as cutover

    wp = cutover.service_target("woodpecker", services["woodpecker"])
    agents = cutover.service_request(wp, "GET", "api/agents")
    if not isinstance(agents, list):
        raise WorkspaceError("Woodpecker agent inventory returned an invalid response")
    mappings = config.get("label_mappings") or {}
    results: list[dict[str, Any]] = []
    for source_tag, labels in mappings.items():
        expected = labels if isinstance(labels, dict) else {"platform": string(labels)}
        matching = []
        for agent in agents:
            actual = agent.get("custom_labels") or {}
            if all(string(actual.get(key)) == string(value) for key, value in expected.items()) and not bool_value(agent.get("no_schedule")):
                matching.append(string(agent.get("name") or agent.get("id")))
        results.append({"source_tag": source_tag, "target_labels": expected, "matching_agents": matching, "verified": bool(matching)})
    return {"mode": config["mode"], "items": results, "verified": all(item["verified"] for item in results)}


def import_pipelines(plan: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("pipelines"), "surfaces.pipelines")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "history_imported": False, "verified": True}
    services = plan.get("services") or {}
    wp_config = services.get("woodpecker") or {}
    import forge_cutover as cutover

    wp = cutover.service_target("woodpecker", wp_config)
    schedule_mappings = config.get("schedule_mappings") or {}
    results: list[dict[str, Any]] = []
    for project_item in snapshot["surfaces"].get("pipelines", {}).get("items", []):
        project_path = string(project_item.get("project"))
        project_snapshot = project_snapshot_for(snapshot, project_path)
        if not project_snapshot:
            raise WorkspaceError(f"pipeline project {project_path} is missing a repository destination mapping")
        owner = string(project_snapshot["destination"].get("owner"))
        repo = string(project_snapshot["destination"].get("repo"))
        wp_repo = cutover.woodpecker_lookup(wp, f"{owner}/{repo}", required=False)
        if not wp_repo:
            raise WorkspaceError(f"Woodpecker repository {owner}/{repo} is not active; activate it before importing schedules")
        repo_id = int(wp_repo.get("id") or 0)
        if repo_id <= 0:
            raise WorkspaceError(f"Woodpecker repository id is missing for {owner}/{repo}")
        schedules = ((project_item.get("pipelines") or {}).get("schedules") or [])
        for schedule in schedules:
            source_id = string(schedule.get("id"))
            configured = schedule_mappings.get(source_id) or schedule_mappings.get(f"{project_path}:{source_id}")
            if isinstance(configured, dict):
                name = string(configured.get("name"))
                branch = string(configured.get("branch"))
                if bool_value(configured.get("enabled")):
                    raise WorkspaceError(
                        f"workspace schedule {project_path}:{source_id} cannot be enabled before cutover"
                    )
            else:
                name = string(configured) or f"gitlab-schedule-{source_id or hashlib.sha256(canonical_digest(schedule).encode()).hexdigest()[:12]}"
                branch = ""
            enabled = False
            name = name or f"gitlab-schedule-{source_id}"
            cron = string(schedule.get("cron"))
            branch = branch or string(schedule.get("ref")) or string(project_snapshot["project"].get("default_branch") or "main")
            if not cron:
                raise WorkspaceError(f"GitLab pipeline schedule {project_path}:{name} is missing its cron expression")
            result = cutover.woodpecker_cron_upsert(wp, repo_id, name, cron, branch, enabled)
            results.append({"project": project_path, "source_id": source_id, "name": name, "action": result.get("action"), "verified": result.get("verified") is True})
    return {"mode": config["mode"], "items": results, "history_imported": False, "verified": all(item.get("verified") is True for item in results)}


def import_workspace(plan: dict[str, Any], snapshot: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    destination = endpoint(plan, "destination", "forgejo")
    work_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    if source_mode(plan, "users") == "managed":
        results["users"] = import_users(plan, destination, snapshot)
    if source_mode(plan, "groups") == "managed" or source_mode(plan, "subgroups") == "managed":
        results["groups"] = import_groups(plan, destination, snapshot)
    if source_mode(plan, "projects") == "managed" or source_mode(plan, "repositories") == "managed":
        results["repositories"] = import_repositories(plan, snapshot, destination, work_dir)
    if source_mode(plan, "variables") == "managed":
        results["variables"] = import_variables(plan, snapshot)
    if source_mode(plan, "runners") == "managed":
        results["runners"] = import_runners(plan, snapshot)
    if source_mode(plan, "ci") == "managed":
        results["ci"] = import_ci(plan, snapshot, work_dir)
    if source_mode(plan, "pipelines") != "skip":
        results["pipelines"] = import_pipelines(plan, snapshot)
    return {"verified": all(value.get("verified") is True for value in results.values()), "surfaces": results}


def proof(command: str, plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "proof_version": PROOF_VERSION,
        "tool": TOOL,
        "command": command,
        "generated_at": utc_now(),
        "plan_sha256": canonical_digest(plan),
        "verified": result.get("verified") is True,
        "result": sanitize_proof(result),
    }


def sanitize_proof(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(child_key): sanitize_proof(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [sanitize_proof(child, key) for child in value]
    if key.lower() in SENSITIVE_KEYS or "password" in key.lower() or "token" in key.lower():
        return "<redacted>" if value not in (None, "") else value
    return value


def command_validate(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    result = {"verified": True, "surfaces": {name: surface_config((plan.get("surfaces") or {}).get(name), f"surfaces.{name}") for name in SURFACES}}
    if args.proof:
        write_json(args.proof, proof("validate-plan", plan, result))
    print(json.dumps(sanitize_proof(result), indent=2, sort_keys=True))
    return 0


def command_export(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    result = export_workspace(plan)
    write_json(args.snapshot, result)
    evidence = proof("export", plan, {"verified": True, "counts": result.get("counts", {})})
    if args.proof:
        write_json(args.proof, evidence)
    print(json.dumps(sanitize_proof(evidence), indent=2, sort_keys=True))
    return 0


def command_import(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    snapshot = require_snapshot(plan, args.snapshot)
    result = import_workspace(plan, snapshot, args.work_dir)
    evidence = proof("import", plan, result)
    if args.proof:
        write_json(args.proof, evidence)
    print(json.dumps(sanitize_proof(evidence), indent=2, sort_keys=True))
    return 0 if result.get("verified") else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-plan")
    validate.add_argument("plan", type=Path)
    validate.add_argument("--proof", type=Path)
    validate.set_defaults(handler=command_validate)
    export = subparsers.add_parser("export")
    export.add_argument("plan", type=Path)
    export.add_argument("--snapshot", type=Path, required=True)
    export.add_argument("--proof", type=Path)
    export.set_defaults(handler=command_export)
    import_command = subparsers.add_parser("import")
    import_command.add_argument("plan", type=Path)
    import_command.add_argument("--snapshot", type=Path, required=True)
    import_command.add_argument("--work-dir", type=Path, required=True)
    import_command.add_argument("--proof", type=Path)
    import_command.set_defaults(handler=command_import)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or None)
    try:
        return int(args.handler(args))
    except (WorkspaceError, migration.MigrationError, OSError, ValueError) as exc:
        print(f"forge workspace failed: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
