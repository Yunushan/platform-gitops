#!/usr/bin/env python3
"""Export and selectively import GitLab workspace state into Forgejo.

The workspace command is deliberately separate from the repository migrator.
It inventories users, groups, direct/effective memberships, projects,
repository authorization and protected-branch rules, CI/CD metadata,
variables, runners, and pipeline history, then applies only surfaces whose
plan mode is ``managed``.
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
    "memberships",
    "projects",
    "repositories",
    "permissions",
    "rules",
    "runners",
    "variables",
    "ci",
    "pipelines",
)
MODES = {"skip", "export", "managed", "mapped", "manual"}
ACCOUNTED_MODES = {"managed", "mapped", "manual", "skipped"}
FORGEJO_PERMISSIONS = {"none", "read", "write", "admin"}
PERMISSION_RANK = {"none": 0, "read": 1, "write": 2, "admin": 3}
DEFAULT_ROLE_BUCKETS = (
    (50, "owner", "gitlab-owners", "admin"),
    (40, "maintainer", "gitlab-maintainers", "write"),
    (30, "developer", "gitlab-developers", "write"),
    (20, "reporter", "gitlab-reporters", "read"),
    (10, "guest", "gitlab-guests", "read"),
)
ROLE_NAME_ALIASES = {
    "no_access": "none",
    "none": "none",
    "minimal_access": "none",
    "guest": "guest",
    "planner": "reporter",
    "reporter": "reporter",
    "security_manager": "reporter",
    "developer": "developer",
    "maintainer": "maintainer",
    "owner": "owner",
    "admin": "owner",
}
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
    if (
        not selectors
        and not bool_value(source.get("all_available_projects"))
        and not bool_value(source.get("all_available_groups"))
        and not user_selector
    ):
        raise WorkspaceError(
            "source.project_paths, source.group_paths, source.usernames, or an explicit all_available_projects/all_available_groups selector is required"
        )
    if selectors is not None and (not isinstance(selectors, list) or not all(string(item) for item in selectors)):
        raise WorkspaceError("source.project_paths and source.group_paths must contain non-empty strings")


def validate_role_mapping_config(config: dict[str, Any], label: str) -> None:
    mappings = config.get("role_mappings") or {}
    custom_mappings = config.get("custom_role_mappings") or {}
    if not isinstance(mappings, dict):
        raise WorkspaceError(f"{label}.role_mappings must be an object")
    if not isinstance(custom_mappings, dict):
        raise WorkspaceError(f"{label}.custom_role_mappings must be an object")
    for mapping_label, mapping_values in (("role_mappings", mappings), ("custom_role_mappings", custom_mappings)):
        for role, value in mapping_values.items():
            if not isinstance(value, (str, dict)):
                raise WorkspaceError(f"{label}.{mapping_label}[{role!r}] must be a permission string or object")
            permission = string(value if isinstance(value, str) else value.get("permission")).lower()
            if permission not in FORGEJO_PERMISSIONS:
                raise WorkspaceError(
                    f"{label}.{mapping_label}[{role!r}].permission must be one of {sorted(FORGEJO_PERMISSIONS)}"
                )
            if isinstance(value, dict):
                team = string(value.get("team") or value.get("team_name"))
                if team and (len(team) > 100 or not re.fullmatch(r"[A-Za-z0-9_.-]+", team)):
                    raise WorkspaceError(
                        f"{label}.{mapping_label}[{role!r}] team names must contain only letters, numbers, '.', '_' or '-'"
                    )
    unmapped = string(config.get("unmapped_role") or "fail").lower()
    if unmapped not in {"fail", "skip", "manual"}:
        raise WorkspaceError(f"{label}.unmapped_role must be fail, skip, or manual")
    if unmapped == "manual" and not string(config.get("unmapped_role_reason")):
        raise WorkspaceError(f"{label}.unmapped_role=manual requires unmapped_role_reason")
    pending = string(config.get("pending_memberships") or "skip").lower()
    if pending not in {"skip", "fail", "manual"}:
        raise WorkspaceError(f"{label}.pending_memberships must be skip, fail, or manual")
    if pending == "manual" and not string(config.get("pending_membership_reason")):
        raise WorkspaceError(f"{label}.pending_memberships=manual requires pending_membership_reason")


def validate_permission_surface(config: dict[str, Any], label: str) -> None:
    validate_role_mapping_config(config, label)
    reconcile = string(config.get("reconcile") or "additive").lower()
    if reconcile not in {"additive", "exact"}:
        raise WorkspaceError(f"{label}.reconcile must be additive or exact")
    if reconcile == "exact" and (
        config.get("accepted") is not True or not string(config.get("reason"))
    ):
        raise WorkspaceError(f"{label}.reconcile=exact requires accepted=true and a reason")
    if not bool_value(config.get("include_direct"), True) and not bool_value(config.get("include_inherited"), True):
        raise WorkspaceError(f"{label} must include direct or inherited project members")
    group_strategy = string(config.get("group_strategy") or "both").lower()
    if group_strategy not in {"teams", "users", "both"}:
        raise WorkspaceError(f"{label}.group_strategy must be teams, users, or both")


def validate_rules_surface(config: dict[str, Any], label: str) -> None:
    """Validate the portable GitLab protected-branch policy options."""
    reconcile = string(config.get("reconcile") or "additive").lower()
    if reconcile not in {"additive", "exact"}:
        raise WorkspaceError(f"{label}.reconcile must be additive or exact")
    if reconcile == "exact" and (
        not bool_value(config.get("accepted")) or not string(config.get("reason"))
    ):
        raise WorkspaceError(f"{label}.reconcile=exact requires accepted=true and a reason")
    if "gitlab_maintainer_team" in config and not string(config.get("gitlab_maintainer_team")):
        raise WorkspaceError(f"{label}.gitlab_maintainer_team must not be empty")


def membership_surface_config(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit membership policy or the legacy groups policy."""
    surfaces = plan.get("surfaces") or {}
    if "memberships" in surfaces:
        return surface_config(surfaces.get("memberships"), "surfaces.memberships")
    groups = surface_config(surfaces.get("groups"), "surfaces.groups")
    subgroups = surface_config(surfaces.get("subgroups"), "surfaces.subgroups")
    if groups["mode"] == "managed" or subgroups["mode"] == "managed":
        policies = [
            string(groups.get("members_mode") or "import").lower(),
            string(subgroups.get("members_mode") or "import").lower(),
        ]
        if any(policy == "import" for policy in policies):
            return {"mode": "managed", "include_inherited": False}
    return {"mode": "skip"}


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
        for name in ("projects", "repositories", "permissions", "rules", "runners", "variables", "ci", "pipelines")
    )
    if project_surfaces_selected and not (
        source_project_paths(plan)
        or source_group_paths(plan)
        or bool_value(plan["source"].get("all_available_projects"))
        or bool_value(plan["source"].get("all_available_groups"))
    ):
        raise WorkspaceError(
            "project, repository, permission, rule, runner, variable, CI, or pipeline surfaces require source.project_paths, source.group_paths, all_available_projects=true, or all_available_groups=true"
        )
    for name, config in normalized.items():
        if (
            name in {"groups", "subgroups", "memberships"}
            and config["mode"] != "skip"
            and not source_group_paths(plan)
            and not bool_value(plan["source"].get("all_available_groups"))
        ):
            raise WorkspaceError(f"surfaces.{name} requires source.group_paths or source.all_available_groups=true")
        if name == "users" and config["mode"] != "skip":
            usernames = plan["source"].get("usernames") or []
            authorization_selected = (
                normalized["memberships"]["mode"] != "skip"
                or normalized["permissions"]["mode"] != "skip"
                or normalized["groups"]["mode"] != "skip"
                or normalized["subgroups"]["mode"] != "skip"
            )
            if not usernames and not bool_value(config.get("all_available")) and not (
                bool_value(config.get("include_members"), False)
                and authorization_selected
            ):
                raise WorkspaceError(
                    "surfaces.users requires source.usernames or surfaces.users.all_available=true; include_members=true with an authorization surface is also accepted"
                )
            if usernames and (not isinstance(usernames, list) or not all(string(item) for item in usernames)):
                raise WorkspaceError("source.usernames must contain non-empty strings")
        if name in {"groups", "subgroups"} and config["mode"] == "managed":
            if string(config.get("target_kind") or "organization") != "organization":
                raise WorkspaceError(f"surfaces.{name}.target_kind must be organization")
        if name == "memberships" and config["mode"] == "managed":
            if normalized["groups"]["mode"] != "managed" and normalized["subgroups"]["mode"] != "managed":
                raise WorkspaceError("surfaces.memberships.managed requires managed groups or subgroups")
            validate_role_mapping_config(config, "surfaces.memberships")
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
        if name == "permissions" and config["mode"] == "managed":
            if normalized["projects"]["mode"] == "skip" and normalized["repositories"]["mode"] == "skip":
                raise WorkspaceError("surfaces.permissions.managed requires projects or repositories to be selected")
            validate_permission_surface(config, "surfaces.permissions")
        if name == "rules" and config["mode"] == "managed":
            validate_rules_surface(config, "surfaces.rules")
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
    for name in ("users", "groups", "projects", "permissions", "rules", "runners", "variables"):
        value = mappings.get(name)
        if value is not None and not isinstance(value, dict):
            raise WorkspaceError(f"mappings.{name} must be an object")
    rule_mappings = mappings.get("rules") or {}
    for project_path, mapping in rule_mappings.items():
        if not string(project_path):
            raise WorkspaceError("mappings.rules keys must be non-empty project paths")
        if not isinstance(mapping, (str, dict)):
            raise WorkspaceError(f"mappings.rules[{project_path!r}] must be a team name or object")
        if isinstance(mapping, str):
            merged = {**normalized["rules"], "gitlab_maintainer_team": mapping}
        else:
            merged = {**normalized["rules"], **mapping}
        validate_rules_surface(merged, f"mappings.rules[{project_path!r}]")


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


def list_pages_optional(endpoint_obj: Endpoint, path: str, *, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """List an endpoint whose absence is a provider/version capability, not an import failure."""
    result: list[dict[str, Any]] = []
    for page in range(1, 101):
        current = dict(query or {})
        current.update({"page": page, "per_page": 100})
        status, payload = request(
            endpoint_obj,
            "GET",
            path,
            query=current,
            expected=(200, 404),
            return_status=True,
        )
        if status == 404:
            return result
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
    normalized_path = string(path).strip("/")
    roots = set(source_group_paths(plan))
    if roots:
        return normalized_path not in roots
    return "/" in normalized_path


def source_project_paths(plan: dict[str, Any]) -> list[str]:
    source = plan["source"]
    values = source.get("project_paths") or []
    return sorted({string(item).strip("/") for item in values if string(item).strip("/")})


def discover_groups(source: Endpoint, plan: dict[str, Any]) -> list[dict[str, Any]]:
    paths = source_group_paths(plan)
    groups: dict[str, dict[str, Any]] = {}
    subgroup_config = surface_config((plan.get("surfaces") or {}).get("subgroups"), "surfaces.subgroups")
    include_subgroups = bool_value(subgroup_config.get("include_subgroups"), subgroup_config["mode"] != "skip")
    pending: list[dict[str, Any]] = []
    if bool_value(plan["source"].get("all_available_groups")):
        for group in list_pages(source, "groups", query={"all_available": True}):
            group_path = string(group.get("full_path")) or string(group.get("path"))
            if group_path:
                if not group.get("full_path"):
                    group["full_path"] = group_path
                groups[group_path] = group
                pending.append(group)
    for path in paths:
        group = get_endpoint_value(source, f"groups/{quote(path, safe='')}")
        if not isinstance(group, dict):
            raise WorkspaceError(f"GitLab group {path!r} returned an invalid object")
        groups[string(group.get("full_path") or path)] = group
        pending.append(group)
    if include_subgroups:
        # GitLab's subgroup endpoint is one level deep. Walk it explicitly so
        # arbitrarily nested namespaces cannot silently lose their members.
        visited: set[str] = set()
        while pending:
            parent = pending.pop(0)
            parent_id = string(parent.get("id") or parent.get("full_path"))
            parent_key = string(parent.get("full_path") or parent_id)
            if parent_key in visited:
                continue
            visited.add(parent_key)
            for child in list_pages(source, f"groups/{quote(parent_id, safe='')}/subgroups"):
                child_path = string(child.get("full_path"))
                if not child_path:
                    child_name = string(child.get("path"))
                    child_path = f"{parent_key}/{child_name}".strip("/") if child_name else ""
                if child_path:
                    groups[child_path] = child
                    if not child.get("full_path"):
                        child["full_path"] = child_path
                    pending.append(child)
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
        direct_members = list_pages(source, f"groups/{quote(group_id, safe='')}/members")
        effective_members = list_pages(source, f"groups/{quote(group_id, safe='')}/members/all")
        result.append(
            {
                "id": group.get("id"),
                "full_path": path,
                "name": group.get("name"),
                "path": group.get("path"),
                "description": group.get("description"),
                "visibility": group.get("visibility"),
                "parent_id": group.get("parent_id"),
                # Keep both views. Direct members become Forgejo org members;
                # effective members are needed to preserve inherited access.
                "direct_members": [safe_record(member) for member in direct_members],
                "effective_members": [safe_record(member) for member in effective_members],
                "members": [safe_record(member) for member in effective_members],
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
    group_paths = source_group_paths(plan)
    if bool_value(plan["source"].get("all_available_groups")):
        available_group_paths = sorted(
            {
                string(item.get("full_path"))
                for item in groups
                if string(item.get("full_path"))
            },
            key=str.casefold,
        )
        group_paths = [path for path in available_group_paths if "/" not in path] or available_group_paths
        if not group_paths:
            group_paths = [
                string(item.get("full_path") or item.get("path"))
                for item in list_pages(source, "groups", query={"all_available": True})
                if string(item.get("full_path") or item.get("path"))
            ]
    for group_path in group_paths:
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


def discover_project_permissions(source: Endpoint, project: dict[str, Any]) -> dict[str, Any]:
    """Capture direct and effective GitLab project authorization without secrets."""
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    project_path = string(project.get("path_with_namespace"))
    direct = list_pages(source, f"projects/{quote(project_id, safe='')}/members")
    effective = list_pages(source, f"projects/{quote(project_id, safe='')}/members/all")
    invited_groups = list_pages_optional(source, f"projects/{quote(project_id, safe='')}/invited_groups")
    namespace = project.get("namespace") or {}
    group_path = string(namespace.get("full_path"))
    return {
        "project": project_path,
        "project_id": project.get("id"),
        "group_path": group_path,
        "direct_members": [safe_record(member) for member in direct],
        "effective_members": [safe_record(member) for member in effective],
        "invited_groups": [safe_record(group) for group in invited_groups],
    }


def discover_project_rules(source: Endpoint, project: dict[str, Any]) -> dict[str, Any]:
    """Capture GitLab protected-branch rules without any secret-bearing fields."""
    project_id = string(project.get("id") or project.get("path_with_namespace"))
    project_path = string(project.get("path_with_namespace"))
    rules = list_pages(source, f"projects/{quote(project_id, safe='')}/protected_branches")
    return {
        "project": project_path,
        "project_id": project.get("id"),
        "rules": [safe_record(rule) for rule in rules],
    }


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


def member_username(member: dict[str, Any]) -> str:
    nested_user = member.get("user")
    if isinstance(nested_user, dict):
        nested = string(nested_user.get("username") or nested_user.get("login"))
        if nested:
            return nested
    return string(
        member.get("username")
        or member.get("user_username")
        or member.get("user_login")
        or member.get("login")
    )


def discover_users(
    source: Endpoint,
    plan: dict[str, Any],
    groups: list[dict[str, Any]] | None = None,
    project_permissions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    config = surface_config((plan.get("surfaces") or {}).get("users"), "surfaces.users")
    query = {key: value for key, value in (("active", config.get("active")), ("blocked", config.get("blocked")), ("external", config.get("external"))) if value is not None}
    usernames = plan["source"].get("usernames") or []
    users_by_username: dict[str, dict[str, Any]] = {}
    if usernames:
        for username in usernames:
            candidates = list_pages(source, "users", query={**query, "username": string(username)})
            user = next((item for item in candidates if string(item.get("username")) == string(username)), None)
            if not user:
                raise WorkspaceError(f"GitLab user {username!r} was not found")
            users_by_username[string(user.get("username"))] = user
    elif bool_value(config.get("all_available")):
        for user in list_pages(source, "users", query=query):
            username = string(user.get("username"))
            if username:
                users_by_username[username] = user

    if bool_value(config.get("include_members"), False):
        for group in groups or []:
            members = group.get("effective_members") or group.get("members") or []
            for member in members:
                if not isinstance(member, dict):
                    continue
                username = member_username(member)
                if username and username.casefold() not in {key.casefold() for key in users_by_username}:
                    users_by_username[username] = {"username": username}
        for project_item in project_permissions or []:
            members = project_item.get("effective_members") or project_item.get("direct_members") or []
            for member in members:
                if not isinstance(member, dict):
                    continue
                username = member_username(member)
                if username and username.casefold() not in {key.casefold() for key in users_by_username}:
                    users_by_username[username] = {"username": username}
    if not users_by_username and source_mode(plan, "users") != "skip":
        raise WorkspaceError("GitLab user discovery returned no users for the selected scope")
    return [safe_record(item) for _, item in sorted(users_by_username.items(), key=lambda entry: entry[0].casefold())]


def export_workspace(plan: dict[str, Any]) -> dict[str, Any]:
    source = endpoint(plan, "source", "gitlab")
    surfaces = plan.get("surfaces") or {}
    groups = discover_groups(source, plan) if any(surface_config(surfaces.get(name), f"surfaces.{name}")["mode"] != "skip" for name in ("groups", "subgroups", "memberships", "projects", "repositories", "permissions", "rules", "variables", "runners")) else []
    projects = discover_projects(source, plan, groups) if any(surface_config(surfaces.get(name), f"surfaces.{name}")["mode"] != "skip" for name in ("projects", "repositories", "permissions", "rules", "variables", "runners", "ci", "pipelines")) else []
    project_permissions = [discover_project_permissions(source, project) for project in projects] if source_mode(plan, "permissions") != "skip" else []
    discovered_users = discover_users(source, plan, groups, project_permissions) if source_mode(plan, "users") != "skip" else []
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
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": discovered_users}
        elif name in {"groups", "subgroups"}:
            items = [item for item in groups if group_is_subgroup(plan, string(item.get("full_path"))) == (name == "subgroups")]
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": items}
        elif name == "memberships":
            snapshot["surfaces"][name] = {
                "mode": config["mode"],
                "items": [
                    {
                        "group": string(item.get("full_path")),
                        "group_id": item.get("id"),
                        "direct_members": item.get("direct_members") or [],
                        "effective_members": item.get("effective_members") or item.get("members") or [],
                    }
                    for item in groups
                ],
            }
        elif name in {"projects", "repositories"}:
            items = project_index
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": items}
        elif name == "permissions":
            snapshot["surfaces"][name] = {"mode": config["mode"], "items": project_permissions}
        elif name == "rules":
            snapshot["surfaces"][name] = {
                "mode": config["mode"],
                "items": [discover_project_rules(source, project) for project in projects],
            }
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


def validate_unique_group_targets(plan: dict[str, Any], items: list[dict[str, Any]]) -> None:
    targets: dict[str, str] = {}
    for item in items:
        source_path = string(item.get("full_path"))
        if not source_path:
            raise WorkspaceError("group snapshot item is missing full_path")
        default_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_path.replace("/", "-"))[:100] or "migrated"
        target_name = mapped_name(plan, "groups", source_path, default_name)
        if not target_name:
            raise WorkspaceError(f"GitLab group {source_path!r} maps to an empty Forgejo organization")
        key = target_name.casefold()
        previous = targets.get(key)
        if previous and previous.casefold() != source_path.casefold():
            raise WorkspaceError(
                f"GitLab groups {previous!r} and {source_path!r} map to the same Forgejo organization {target_name!r}"
            )
        targets[key] = source_path


def validate_unique_repository_targets(snapshot: dict[str, Any]) -> None:
    targets: dict[str, str] = {}
    for item in project_index(snapshot):
        project = item.get("project") or {}
        destination = item.get("destination") or {}
        source_path = string(project.get("path_with_namespace"))
        owner = string(destination.get("owner"))
        repo = string(destination.get("repo"))
        if not source_path or not owner or not repo:
            raise WorkspaceError("project snapshot item has an incomplete repository destination mapping")
        target = f"{owner.casefold()}/{repo.casefold()}"
        previous = targets.get(target)
        if previous and previous.casefold() != source_path.casefold():
            raise WorkspaceError(
                f"GitLab projects {previous!r} and {source_path!r} map to the same Forgejo repository {owner}/{repo}"
            )
        targets[target] = source_path


def import_users(plan: dict[str, Any], destination: Endpoint, snapshot: dict[str, Any]) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("users"), "surfaces.users")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "verified": config["mode"] in {"skip", "export", "mapped", "manual"}, "created": 0, "existing": 0, "targets": []}
    created = 0
    existing = 0
    targets: list[str] = []
    items = snapshot["surfaces"].get("users", {}).get("items", [])
    validate_unique_user_targets(plan, config, items)
    for item in items:
        source_username = string(item.get("username"))
        if not source_username or bool_value(item.get("is_bot")) and bool_value(config.get("skip_bots"), True):
            continue
        target_username = mapped_name(plan, "users", source_username, source_username)
        targets.append(target_username)
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
        "targets": sorted(set(targets), key=str.casefold),
        "verified": True,
    }


def forgejo_org(destination: Endpoint, name: str) -> tuple[int, dict[str, Any]]:
    return request(destination, "GET", f"orgs/{quote(name, safe='')}", expected=(200, 404), return_status=True)


def role_mapping_value(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        return string(value).lower(), ""
    if isinstance(value, dict):
        return string(value.get("permission")).lower(), string(value.get("team") or value.get("team_name"))
    return "", ""


def role_mapping_config(plan: dict[str, Any], surface: str) -> dict[str, Any]:
    surfaces = plan.get("surfaces") or {}
    if surface == "memberships" and surface not in surfaces:
        # Preserve the original groups.members_mode plan shape while allowing
        # newer plans to put role mappings under surfaces.memberships.
        config = surface_config(surfaces.get("groups"), "surfaces.groups")
    else:
        config = surface_config(surfaces.get(surface), f"surfaces.{surface}")
    mappings: dict[str, Any] = {}
    raw = config.get("role_mappings") or {}
    custom = config.get("custom_role_mappings") or {}
    if isinstance(raw, dict):
        mappings.update({string(key).lower(): value for key, value in raw.items()})
    if isinstance(custom, dict):
        mappings.update({string(key).lower(): value for key, value in custom.items()})
    return {"config": config, "mappings": mappings}


def normalized_access_level(member: dict[str, Any]) -> int | None:
    raw = member.get("access_level")
    if raw is None:
        raw = member.get("group_access_level")
    try:
        return int(raw) if raw is not None and string(raw) else None
    except (TypeError, ValueError):
        return None


def custom_role_id(member: dict[str, Any]) -> str:
    for key in ("member_role_id", "custom_role_id", "role_id"):
        value = member.get(key)
        if value not in (None, ""):
            return string(value)
    for key in ("member_role", "custom_role"):
        value = member.get(key)
        if isinstance(value, dict):
            nested = value.get("id")
            if nested not in (None, ""):
                return string(nested)
    return ""


def role_name(member: dict[str, Any]) -> str:
    for key in ("access_level_description", "access_level_name", "role_name", "role"):
        value = member.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("base_access_level")
        if value not in (None, ""):
            return string(value).lower().replace(" ", "_").replace("-", "_")
    return ""


def default_role_mapping(member: dict[str, Any]) -> tuple[str, str, str]:
    level = normalized_access_level(member)
    if level is not None:
        for minimum, key, team, permission in DEFAULT_ROLE_BUCKETS:
            if level >= minimum:
                return key, team, permission
        return "none", "", "none"
    name = ROLE_NAME_ALIASES.get(role_name(member), "")
    if name == "none":
        return "none", "", "none"
    if name:
        for _minimum, key, team, permission in DEFAULT_ROLE_BUCKETS:
            if key == name:
                return key, team, permission
    return "", "", ""


def role_mapping_candidates(member: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    custom = custom_role_id(member)
    if custom:
        candidates.extend((f"custom:{custom}", f"member_role:{custom}", f"role_id:{custom}", custom))
    name = role_name(member)
    if name:
        candidates.append(name)
        alias = ROLE_NAME_ALIASES.get(name)
        if alias:
            candidates.append(alias)
    level = normalized_access_level(member)
    if level is not None:
        candidates.append(str(level))
    return candidates


def default_team_for_role_key(role_key: str) -> str:
    normalized = string(role_key).lower().replace(" ", "_").replace("-", "_")
    aliases = {
        string(alias).lower().replace(" ", "_").replace("-", "_"): team
        for _minimum, key, team, _permission in DEFAULT_ROLE_BUCKETS
        for alias in (key, team)
    }
    try:
        numeric = int(normalized)
    except ValueError:
        numeric = None
    if numeric is not None:
        for minimum, _key, team, _permission in DEFAULT_ROLE_BUCKETS:
            if numeric >= minimum:
                return team
    return aliases.get(normalized, "")


def generated_team_name(role_key: str, permission: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", string(role_key).replace(":", "-")).strip("-._")
    return f"gitlab-{(suffix or permission)[:90]}"


def managed_team_definitions(plan: dict[str, Any], surface: str) -> dict[str, str]:
    """Return every deterministic role team, including currently empty teams."""
    definitions = {
        team: permission for _minimum, _key, team, permission in DEFAULT_ROLE_BUCKETS
    }
    config = role_mapping_config(plan, surface)
    for role_key, mapping in config["mappings"].items():
        permission, team = role_mapping_value(mapping)
        if permission == "none":
            continue
        team = team or default_team_for_role_key(role_key)
        if not team:
            team = generated_team_name(role_key, permission)
        definitions[team] = permission
    return definitions


def resolve_member_role(plan: dict[str, Any], member: dict[str, Any], surface: str) -> dict[str, Any]:
    role_config = role_mapping_config(plan, surface)
    config = role_config["config"]
    mappings = role_config["mappings"]
    custom = custom_role_id(member)
    selected: Any = None
    matched_key = ""
    for candidate in role_mapping_candidates(member):
        if candidate in mappings:
            selected = mappings[candidate]
            matched_key = candidate
            break
    if selected is not None:
        permission, team = role_mapping_value(selected)
        if not permission:
            raise WorkspaceError(f"role mapping {matched_key!r} has no Forgejo permission")
        if not team and permission != "none":
            team = default_team_for_role_key(matched_key)
            if not team and not custom:
                default_key, default_team, _default_permission = default_role_mapping(member)
                team = default_team
            team = team or generated_team_name(matched_key, permission)
        return {
            "key": matched_key,
            "permission": permission,
            "team": team,
            "access_level": normalized_access_level(member),
            "custom_role_id": custom,
        }
    if custom:
        # A GitLab custom role can share a base access level with a different
        # role. Never silently collapse that custom role into the base role.
        default_key = ""
        default_team = ""
        default_permission = ""
    else:
        default_key, default_team, default_permission = default_role_mapping(member)
    if default_key:
        return {
            "key": default_key,
            "permission": default_permission,
            "team": default_team,
            "access_level": normalized_access_level(member),
            "custom_role_id": custom,
        }
    behavior = string(config.get("unmapped_role") or "fail").lower()
    if behavior == "fail":
        identity = role_name(member) or custom or string(normalized_access_level(member), "unknown")
        raise WorkspaceError(
            f"GitLab member role {identity!r} is not mapped for surfaces.{surface}; add an explicit role_mappings entry or set unmapped_role=manual/skip"
        )
    return {
        "key": role_name(member) or custom or "unmapped",
        "permission": "none",
        "team": "",
        "access_level": normalized_access_level(member),
        "custom_role_id": custom,
        "unmapped": True,
        "unmapped_behavior": behavior,
    }


def member_is_expired(member: dict[str, Any]) -> bool:
    expires_at = string(member.get("expires_at") or member.get("expiry_date"))
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        raise WorkspaceError(f"GitLab membership has an invalid expires_at value {expires_at!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


def member_is_pending(member: dict[str, Any]) -> bool:
    state = string(member.get("state") or member.get("membership_state") or member.get("status")).lower()
    return state in {"awaiting", "pending", "invited", "access_requested", "requested"}


def membership_allowed(config: dict[str, Any], member: dict[str, Any]) -> bool:
    if member_is_expired(member) and not bool_value(config.get("include_expired")):
        return False
    if member_is_pending(member):
        pending = string(config.get("pending_memberships") or "skip").lower()
        if pending == "fail":
            raise WorkspaceError(f"pending GitLab membership for {member_username(member)!r} cannot be imported")
        return False
    return True


def reconcile_team_membership(
    destination: Endpoint,
    teams: dict[Any, int],
    selected_level: Any,
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
    validate_unique_group_targets(plan, group_items)
    created = 0
    existing = 0
    org_by_path: dict[str, str] = {}
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
    return {
        "mode": "managed",
        "created": created,
        "existing": existing,
        "organizations": org_by_path,
        "verified_count": created + existing,
        "verified": True,
    }


def membership_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = snapshot.get("surfaces", {}).get("memberships", {})
    if isinstance(explicit, dict) and explicit.get("items"):
        return [item for item in explicit.get("items", []) if isinstance(item, dict)]
    items: list[dict[str, Any]] = []
    for surface in ("groups", "subgroups"):
        for item in snapshot.get("surfaces", {}).get(surface, {}).get("items", []):
            if isinstance(item, dict):
                items.append(item)
    return items


def group_members_for_import(item: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    if bool_value(config.get("include_inherited")):
        members = item.get("effective_members") or item.get("members") or []
    else:
        members = item.get("direct_members") or item.get("members") or []
    return [member for member in members if isinstance(member, dict)]


def membership_policy(plan: dict[str, Any], source_path: str) -> str:
    surfaces = plan.get("surfaces") or {}
    if "memberships" in surfaces:
        config = surface_config(surfaces.get("memberships"), "surfaces.memberships")
        policy = config["mode"]
    else:
        group_surface = "subgroups" if group_is_subgroup(plan, source_path) else "groups"
        config = surface_config(surfaces.get(group_surface), f"surfaces.{group_surface}")
        policy = string(config.get("members_mode") or "import").lower()
        if policy == "import":
            policy = "managed"
    mapping = mappings_for(plan, "groups").get(source_path)
    if isinstance(mapping, dict) and string(mapping.get("members_mode")):
        policy = string(mapping.get("members_mode")).lower()
        if policy == "import":
            policy = "managed"
    return policy


def import_memberships(
    plan: dict[str, Any],
    destination: Endpoint,
    snapshot: dict[str, Any],
    group_result: dict[str, Any] | None,
    known_users: set[str] | None = None,
) -> dict[str, Any]:
    config = membership_surface_config(plan)
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "verified": True}
    organizations = (group_result or {}).get("organizations") or {}
    plans: list[dict[str, Any]] = []
    target_sources: dict[str, str] = {}
    team_permissions: dict[tuple[str, str], str] = {}
    for item in membership_items(snapshot):
        source_path = string(item.get("group") or item.get("full_path"))
        if not source_path:
            raise WorkspaceError("membership snapshot item is missing its group path")
        target_org = string(organizations.get(source_path))
        if not target_org:
            raise WorkspaceError(f"membership group {source_path!r} has no reconciled Forgejo organization")
        policy = membership_policy(plan, source_path)
        if policy in {"skip", "mapped", "manual", "export"}:
            continue
        if policy != "managed":
            raise WorkspaceError(f"unsupported membership policy {policy!r} for {source_path}")
        members = group_members_for_import(item, config)
        resolved_members: list[dict[str, Any]] = []
        for member in members:
            if not membership_allowed(config, member):
                continue
            source_username = member_username(member)
            if not source_username:
                raise WorkspaceError(f"GitLab group {source_path!r} contains a member without a username")
            target_username = mapped_name(plan, "users", source_username, source_username)
            if not target_username:
                raise WorkspaceError(f"GitLab user {source_username!r} maps to an empty Forgejo username")
            previous = target_sources.get(target_username.casefold())
            if previous and previous.casefold() != source_username.casefold():
                raise WorkspaceError(
                    f"GitLab users {previous!r} and {source_username!r} map to the same Forgejo username {target_username!r}"
                )
            target_sources[target_username.casefold()] = source_username
            role = resolve_member_role(plan, member, "memberships")
            if role.get("unmapped"):
                # An explicit skip/manual policy must not turn into an exact
                # deletion of an access grant we could not classify.
                resolved_members.append(
                    {
                        "source_username": source_username,
                        "username": target_username,
                        "role": role,
                    }
                )
                continue
            resolved = {
                "source_username": source_username,
                "username": target_username,
                "role": role,
            }
            resolved_members.append(resolved)
            if role["permission"] != "none" and role.get("team"):
                team_key = (target_org, string(role["team"]))
                previous_permission = team_permissions.get(team_key)
                if previous_permission and previous_permission != role["permission"]:
                    raise WorkspaceError(
                        f"Forgejo team {target_org}/{role['team']} is assigned conflicting permissions"
                    )
                team_permissions[team_key] = string(role["permission"])
        plans.append({"source_path": source_path, "target_org": target_org, "members": resolved_members})

    # Keep empty deterministic teams in the reconciliation set so role
    # downgrades remove stale memberships from a previously populated team.
    definitions = managed_team_definitions(plan, "memberships")
    for item in plans:
        for team_name, permission in definitions.items():
            team_key = (item["target_org"], team_name)
            existing_permission = team_permissions.get(team_key)
            if existing_permission and existing_permission != permission:
                raise WorkspaceError(
                    f"Forgejo team {item['target_org']}/{team_name} is assigned conflicting permissions"
                )
            team_permissions[team_key] = permission

    known = {value.casefold() for value in (known_users or set())}
    for target_key, source_username in target_sources.items():
        target_username = mapped_name(plan, "users", source_username, source_username)
        if target_key in known:
            continue
        status, _current = forgejo_user(destination, target_username)
        if status != 200:
            raise WorkspaceError(
                f"Forgejo user {target_username!r} is not present; manage the users surface or provide a mapped existing account"
            )

    team_ids: dict[tuple[str, str], int] = {
        key: ensure_team(destination, key[0], key[1], permission)
        for key, permission in sorted(team_permissions.items())
    }
    context_teams: dict[str, dict[str, dict[str, Any]]] = {}
    context_members: dict[str, list[str]] = {}
    results: list[dict[str, Any]] = []
    for item in plans:
        source_path = item["source_path"]
        target_org = item["target_org"]
        group_teams: dict[str, dict[str, Any]] = {}
        for member in item["members"]:
            role = member["role"]
            if role.get("unmapped"):
                continue
            team_name = string(role.get("team"))
            if role["permission"] == "none" or not team_name:
                selected_team: str | None = None
            else:
                selected_team = team_name
                group_teams[team_name] = {
                    "id": team_ids[(target_org, team_name)],
                    "name": team_name,
                    "permission": role["permission"],
                }
            available = {
                name: team_id
                for (org, name), team_id in team_ids.items()
                if org == target_org
            }
            reconcile_team_membership(destination, available, selected_team, member["username"])
        context_teams[source_path] = group_teams
        context_members[source_path] = sorted(
            {member["username"] for member in item["members"] if not member["role"].get("unmapped")},
            key=str.casefold,
        )
        results.append(
            {
                "group": source_path,
                "organization": target_org,
                "members": len(item["members"]),
                "teams": sorted(group_teams),
                "verified": True,
            }
        )
    return {
        "mode": "managed",
        "items": results,
        "organizations": organizations,
        "teams": context_teams,
        "members": context_members,
        "verified": all(item.get("verified") is True for item in results),
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


def repository_api_path(owner: str, repo: str, suffix: str = "") -> str:
    base = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    return f"{base}/{suffix.lstrip('/')}" if suffix else base


def forgejo_collaborator_permission(
    destination: Endpoint,
    owner: str,
    repo: str,
    username: str,
) -> tuple[int, dict[str, Any]]:
    return request(
        destination,
        "GET",
        repository_api_path(owner, repo, f"collaborators/{quote(username, safe='')}/permission"),
        expected=(200, 404),
        return_status=True,
    )


def normalized_forgejo_permission(status: int, payload: Any) -> str:
    if status == 404:
        return "none"
    if not isinstance(payload, dict):
        return ""
    permission = string(payload.get("permission") or payload.get("role_name") or payload.get("role")).lower()
    if permission in {"owner", "admin"}:
        return "admin"
    if permission in {"write", "push", "maintain"}:
        return "write"
    if permission in {"read", "pull"}:
        return "read"
    if permission in {"none", ""}:
        return "none" if permission else ""
    return permission


def verify_collaborator_permission(
    destination: Endpoint,
    owner: str,
    repo: str,
    username: str,
    expected_permission: str,
    reconcile: str,
) -> dict[str, Any]:
    status, payload = forgejo_collaborator_permission(destination, owner, repo, username)
    actual = normalized_forgejo_permission(status, payload)
    if actual not in FORGEJO_PERMISSIONS:
        raise WorkspaceError(
            f"Forgejo collaborator permission read-back for {owner}/{repo}/{username} was invalid: {actual or '<missing>'}"
        )
    expected = string(expected_permission).lower()
    if reconcile == "exact":
        valid = actual == expected
    else:
        valid = PERMISSION_RANK[actual] >= PERMISSION_RANK[expected]
    if not valid:
        raise WorkspaceError(
            f"Forgejo collaborator permission mismatch for {owner}/{repo}/{username}: "
            f"expected {expected!r}, got {actual!r}"
        )
    return {"username": username, "permission": actual, "verified": True}


def reconcile_collaborator_permission(
    destination: Endpoint,
    owner: str,
    repo: str,
    username: str,
    permission: str,
    reconcile: str,
) -> dict[str, Any]:
    permission = string(permission).lower()
    if permission not in FORGEJO_PERMISSIONS:
        raise WorkspaceError(f"unsupported Forgejo collaborator permission {permission!r}")
    if permission == "none":
        if reconcile == "exact":
            request(
                destination,
                "DELETE",
                repository_api_path(owner, repo, f"collaborators/{quote(username, safe='')}"),
                expected=(204, 200, 404),
            )
        return verify_collaborator_permission(destination, owner, repo, username, "none", reconcile)
    request(
        destination,
        "PUT",
        repository_api_path(owner, repo, f"collaborators/{quote(username, safe='')}"),
        body={"permission": permission},
        expected=(204, 200, 201),
    )
    return verify_collaborator_permission(destination, owner, repo, username, permission, reconcile)


def repository_teams(destination: Endpoint, owner: str, repo: str) -> list[dict[str, Any]]:
    teams = list_pages(destination, repository_api_path(owner, repo, "teams"))
    return [team for team in teams if isinstance(team, dict)]


def reconcile_repository_team(
    destination: Endpoint,
    owner: str,
    repo: str,
    team_name: str,
) -> None:
    request(
        destination,
        "PUT",
        repository_api_path(owner, repo, f"teams/{quote(team_name, safe='')}"),
        expected=(204, 200, 201),
    )


def verify_repository_teams(
    destination: Endpoint,
    owner: str,
    repo: str,
    expected_names: set[str],
) -> list[dict[str, Any]]:
    current = repository_teams(destination, owner, repo)
    actual_names = {
        string(team.get("name") or team.get("team_name"))
        for team in current
        if string(team.get("name") or team.get("team_name"))
    }
    missing = sorted(expected_names - actual_names)
    if missing:
        raise WorkspaceError(
            f"Forgejo repository team read-back mismatch for {owner}/{repo}; missing {missing}"
        )
    return [{"name": name, "verified": True} for name in sorted(expected_names)]


def source_project_group_path(project: dict[str, Any], project_path: str) -> str:
    namespace = project.get("namespace") or {}
    group_path = string(namespace.get("full_path")).strip("/")
    if group_path:
        return group_path
    return project_path.rsplit("/", 1)[0] if "/" in project_path else ""


def managed_team_names(plan: dict[str, Any], group_result: dict[str, Any] | None) -> set[str]:
    names: set[str] = set()
    for group_teams in ((group_result or {}).get("teams") or {}).values():
        if not isinstance(group_teams, dict):
            continue
        for team_name, team in group_teams.items():
            if isinstance(team, dict) and string(team.get("name") or team_name):
                names.add(string(team.get("name") or team_name))
    for _minimum, _key, team_name, _permission in DEFAULT_ROLE_BUCKETS:
        names.add(team_name)
    names.update(managed_team_definitions(plan, "memberships"))
    names.update(managed_team_definitions(plan, "permissions"))
    return names


def permission_member_records(item: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    if bool_value(config.get("include_direct"), True):
        members.extend(member for member in item.get("direct_members", []) if isinstance(member, dict))
    if bool_value(config.get("include_inherited"), True):
        members.extend(member for member in item.get("effective_members", []) if isinstance(member, dict))
    if not members:
        fallback = item.get("members") or []
        members.extend(member for member in fallback if isinstance(member, dict))
    return members


def import_permissions(
    plan: dict[str, Any],
    snapshot: dict[str, Any],
    destination: Endpoint,
    group_result: dict[str, Any] | None,
    known_users: set[str] | None = None,
) -> dict[str, Any]:
    config = surface_config((plan.get("surfaces") or {}).get("permissions"), "surfaces.permissions")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "verified": True}
    reconcile = string(config.get("reconcile") or "additive").lower()
    items = snapshot.get("surfaces", {}).get("permissions", {}).get("items", [])
    if not isinstance(items, list):
        raise WorkspaceError("permissions snapshot items must be a list")
    validate_unique_repository_targets(snapshot)
    known = {string(value).casefold() for value in (known_users or set())}
    planned: list[dict[str, Any]] = []
    target_sources: dict[str, str] = {}
    skipped: list[dict[str, Any]] = []
    for permission_item in items:
        if not isinstance(permission_item, dict):
            raise WorkspaceError("permissions snapshot item must be an object")
        project_path = string(permission_item.get("project"))
        project_item = project_snapshot_for(snapshot, project_path)
        if not project_item:
            raise WorkspaceError(f"permissions project {project_path!r} is missing a repository destination mapping")
        destination_item = project_item.get("destination") or {}
        owner = string(destination_item.get("owner"))
        repo = string(destination_item.get("repo"))
        if not owner or not repo:
            raise WorkspaceError(f"permissions project {project_path!r} has an incomplete destination mapping")
        owner_kind = string(destination_item.get("owner_kind") or "organization")
        if source_mode(plan, "projects") != "managed" and source_mode(plan, "repositories") != "managed":
            status, _repo = request(
                destination,
                "GET",
                repository_api_path(owner, repo),
                expected=(200, 404),
                return_status=True,
            )
            if status != 200:
                raise WorkspaceError(f"Forgejo repository {owner}/{repo} is not present for permission import")
        desired: dict[str, dict[str, Any]] = {}
        managed: set[str] = set()
        for member in permission_member_records(permission_item, config):
            if not membership_allowed(config, member):
                continue
            source_username = member_username(member)
            if not source_username:
                raise WorkspaceError(f"GitLab project {project_path!r} contains a member without a username")
            target_username = mapped_name(plan, "users", source_username, source_username)
            if not target_username:
                raise WorkspaceError(f"GitLab user {source_username!r} maps to an empty Forgejo username")
            previous = target_sources.get(target_username.casefold())
            if previous and previous.casefold() != source_username.casefold():
                raise WorkspaceError(
                    f"GitLab users {previous!r} and {source_username!r} map to the same Forgejo username {target_username!r}"
                )
            target_sources[target_username.casefold()] = source_username
            role = resolve_member_role(plan, member, "permissions")
            if role.get("unmapped"):
                skipped.append(
                    {
                        "project": project_path,
                        "username": target_username,
                        "role": role.get("key"),
                        "action": role.get("unmapped_behavior"),
                        "verified": True,
                    }
                )
                continue
            managed.add(target_username.casefold())
            permission = string(role.get("permission") or "none")
            existing = desired.get(target_username.casefold())
            if existing is None or PERMISSION_RANK[permission] > PERMISSION_RANK[string(existing["permission"])]:
                desired[target_username.casefold()] = {
                    "username": target_username,
                    "permission": permission,
                    "role": role.get("key"),
                }
        planned.append(
            {
                "project": project_path,
                "project_item": project_item,
                "owner": owner,
                "repo": repo,
                "owner_kind": owner_kind,
                "desired": desired,
                "managed": managed,
                "invited_groups": permission_item.get("invited_groups") or [],
            }
        )

    for target_key, source_username in target_sources.items():
        if target_key in known:
            continue
        target_username = mapped_name(plan, "users", source_username, source_username)
        status, _current = forgejo_user(destination, target_username)
        if status != 200:
            raise WorkspaceError(
                f"Forgejo user {target_username!r} is not present; import or map the users surface before permissions"
            )

    results: list[dict[str, Any]] = []
    for item in planned:
        project_path = item["project"]
        owner = item["owner"]
        repo = item["repo"]
        project_item = item["project_item"]
        source_project = project_item.get("project") or {}
        group_path = source_project_group_path(source_project, project_path)
        context_teams = ((group_result or {}).get("teams") or {}).get(group_path) or {}
        expected_team_names = {
            string(team.get("name") or team_name)
            for team_name, team in context_teams.items()
            if isinstance(team, dict) and string(team.get("name") or team_name)
        }
        group_strategy = string(config.get("group_strategy") or "both").lower()
        if group_strategy == "teams" and item.get("invited_groups") and item["desired"]:
            raise WorkspaceError(
                f"GitLab project {project_path!r} has invited-group access that Forgejo teams cannot represent; use group_strategy=users or both"
            )
        attached_teams: list[dict[str, Any]] = []
        group_org = None
        if group_strategy in {"teams", "both"} and string(item["owner_kind"]).lower() in {"organization", "organisation", "org", "group"}:
            group_org = ((group_result or {}).get("organizations") or {}).get(group_path)
            if group_org and string(group_org).casefold() == owner.casefold():
                for team_name in sorted(expected_team_names):
                    reconcile_repository_team(destination, owner, repo, team_name)
                attached_teams = verify_repository_teams(destination, owner, repo, expected_team_names)
                if reconcile == "exact":
                    current_names = {
                        string(team.get("name") or team.get("team_name"))
                        for team in repository_teams(destination, owner, repo)
                        if string(team.get("name") or team.get("team_name"))
                    }
                    stale = (current_names & managed_team_names(plan, group_result)) - expected_team_names
                    for team_name in sorted(stale):
                        request(
                            destination,
                            "DELETE",
                            repository_api_path(owner, repo, f"teams/{quote(team_name, safe='')}"),
                            expected=(204, 200, 404),
                        )
                    attached_teams = verify_repository_teams(destination, owner, repo, expected_team_names)
        if group_strategy == "teams" and item["desired"] and (
            not group_org or string(group_org).casefold() != owner.casefold() or not expected_team_names
        ):
            raise WorkspaceError(
                f"GitLab project {project_path!r} has permissions that cannot be represented by destination teams alone; use group_strategy=users or both"
            )
        if group_strategy == "teams":
            group_member_keys = {
                string(username).casefold()
                for username in (((group_result or {}).get("members") or {}).get(group_path) or [])
            }
            uncovered = {
                string(desired["username"]).casefold()
                for desired in item["desired"].values()
                if desired["permission"] != "none"
            } - group_member_keys
            if uncovered:
                raise WorkspaceError(
                    f"GitLab project {project_path!r} has direct or inherited users not covered by its destination group teams; use group_strategy=users or both"
                )

        collaborators: list[dict[str, Any]] = []
        for desired in item["desired"].values():
            if group_strategy in {"users", "both"}:
                collaborators.append(
                    reconcile_collaborator_permission(
                        destination,
                        owner,
                        repo,
                        desired["username"],
                        desired["permission"],
                        reconcile,
                    )
                )
            else:
                collaborators.append(
                    verify_collaborator_permission(
                        destination,
                        owner,
                        repo,
                        desired["username"],
                        desired["permission"],
                        reconcile,
                    )
                )
        removed: list[str] = []
        if reconcile == "exact":
            current = list_pages(destination, repository_api_path(owner, repo, "collaborators"))
            desired_keys = set(item["desired"])
            for collaborator in current:
                if not isinstance(collaborator, dict):
                    continue
                login = string(collaborator.get("login") or collaborator.get("username"))
                if login and login.casefold() in item["managed"] and login.casefold() not in desired_keys:
                    request(
                        destination,
                        "DELETE",
                        repository_api_path(owner, repo, f"collaborators/{quote(login, safe='')}"),
                        expected=(204, 200, 404),
                    )
                    verify_collaborator_permission(destination, owner, repo, login, "none", reconcile)
                    removed.append(login)
        results.append(
            {
                "project": project_path,
                "repository": f"{owner}/{repo}",
                "collaborators": collaborators,
                "teams": attached_teams,
                "invited_groups_materialized": len(item.get("invited_groups") or []),
                "removed": sorted(removed),
                "verified": True,
            }
        )
    results.extend(skipped)
    return {
        "mode": "managed",
        "items": results,
        "verified": all(item.get("verified") is True for item in results),
    }


def rules_config_for_project(plan: dict[str, Any], project_path: str) -> dict[str, Any]:
    """Merge global and per-project protected-branch policy settings."""
    config = surface_config((plan.get("surfaces") or {}).get("rules"), "surfaces.rules")
    mapping = mappings_for(plan, "rules").get(project_path)
    if mapping is None:
        return config
    if isinstance(mapping, str):
        return {**config, "gitlab_maintainer_team": mapping}
    if not isinstance(mapping, dict):
        raise WorkspaceError(f"mappings.rules[{project_path!r}] must be a team name or object")
    merged = {**config, **mapping}
    validate_rules_surface(merged, f"mappings.rules[{project_path!r}]")
    return merged


def rule_metadata_for_project(plan: dict[str, Any], project_path: str) -> dict[str, Any]:
    """Translate workspace rule policy into the repository migrator contract."""
    config = rules_config_for_project(plan, project_path)
    metadata: dict[str, Any] = {
        "mode": "required",
        "reconcile": string(config.get("reconcile") or "additive").lower(),
    }
    for key in ("gitlab_maintainer_team", "accepted", "reason"):
        if key in config:
            metadata[key] = config[key]
    return {"branch_protection": metadata}


def repo_plan_from_item(
    plan: dict[str, Any],
    item: dict[str, Any],
    metadata_overrides: dict[str, Any] | None = None,
) -> migration.RepoPlan:
    project = item["project"]
    source_url = string(project.get("http_url_to_repo"))
    destination = item["destination"]
    if not source_url or not string(destination.get("git_url")):
        raise WorkspaceError("repository snapshot item is missing source or destination Git URL")
    source_api = endpoint(plan, "source", "gitlab")
    destination_api = endpoint(plan, "destination", "forgejo")
    metadata = {surface: "skip" for surface in migration.SUPPORTED_METADATA_SURFACES}
    if metadata_overrides:
        metadata.update(metadata_overrides)
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
        metadata=metadata,
    )


def import_repositories(plan: dict[str, Any], snapshot: dict[str, Any], destination: Endpoint, work_dir: Path) -> dict[str, Any]:
    repository_mode = source_mode(plan, "repositories")
    project_mode = source_mode(plan, "projects")
    validate_unique_repository_targets(snapshot)
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


def require_rule_repository(destination: Endpoint, owner: str, repo: str, project_path: str) -> None:
    status, current = request(
        destination,
        "GET",
        repository_api_path(owner, repo),
        expected=(200, 404),
        return_status=True,
    )
    if status != 200 or not isinstance(current, dict):
        raise WorkspaceError(
            f"Forgejo repository {owner}/{repo} for GitLab project {project_path!r} is not present; "
            "import repositories before protected-branch rules"
        )


def require_rule_team(destination: Endpoint, owner: str, team_name: str, project_path: str) -> None:
    teams = list_pages(destination, f"orgs/{quote(owner, safe='')}/teams")
    if not any(string(team.get("name")) == team_name for team in teams):
        raise WorkspaceError(
            f"Forgejo team {owner}/{team_name} required by protected-branch rules for "
            f"GitLab project {project_path!r} is missing; import memberships or map an existing team"
        )


def import_rules(plan: dict[str, Any], snapshot: dict[str, Any], destination: Endpoint) -> dict[str, Any]:
    """Apply GitLab protected branches after destination repositories exist."""
    config = surface_config((plan.get("surfaces") or {}).get("rules"), "surfaces.rules")
    if config["mode"] != "managed":
        return {"mode": config["mode"], "items": [], "verified": True}
    surface = snapshot.get("surfaces", {}).get("rules")
    if not isinstance(surface, dict) or "items" not in surface:
        raise WorkspaceError("rules snapshot surface is missing; export rules before importing them")
    items = surface.get("items")
    if not isinstance(items, list):
        raise WorkspaceError("rules snapshot items must be a list")
    validate_unique_repository_targets(snapshot)
    results: list[dict[str, Any]] = []
    for rule_item in items:
        if not isinstance(rule_item, dict):
            raise WorkspaceError("rules snapshot item must be an object")
        project_path = string(rule_item.get("project"))
        project_item = project_snapshot_for(snapshot, project_path)
        if not project_item:
            raise WorkspaceError(f"rules project {project_path!r} is missing a repository destination mapping")
        destination_item = project_item.get("destination") or {}
        owner = string(destination_item.get("owner"))
        repo = string(destination_item.get("repo"))
        if not owner or not repo:
            raise WorkspaceError(f"rules project {project_path!r} has an incomplete destination mapping")
        require_rule_repository(destination, owner, repo, project_path)
        metadata = rule_metadata_for_project(plan, project_path)
        team_name = string((metadata["branch_protection"] or {}).get("gitlab_maintainer_team"))
        if team_name:
            require_rule_team(destination, owner, team_name, project_path)
        try:
            rule_result = migration.migrate_branch_protections(
                repo_plan_from_item(plan, project_item, metadata)
            )
        except migration.MigrationError as exc:
            raise WorkspaceError(f"protected-branch rules for {project_path!r} failed: {exc}") from exc
        if not isinstance(rule_result, dict):
            raise WorkspaceError(f"protected-branch rules for {project_path!r} returned an invalid result")
        results.append(
            {
                "project": project_path,
                "repository": f"{owner}/{repo}",
                "source_rule_count": len(rule_item.get("rules") or []) if isinstance(rule_item.get("rules"), list) else 0,
                "reconcile": metadata["branch_protection"].get("reconcile", "additive"),
                "rules": rule_result,
                "verified": rule_result.get("verified") is True,
            }
        )
    return {"mode": "managed", "items": results, "verified": all(item.get("verified") is True for item in results)}


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
    user_result: dict[str, Any] | None = None
    if source_mode(plan, "users") == "managed":
        user_result = import_users(plan, destination, snapshot)
        results["users"] = user_result
    group_result: dict[str, Any] | None = None
    if source_mode(plan, "groups") == "managed" or source_mode(plan, "subgroups") == "managed":
        group_result = import_groups(plan, destination, snapshot)
        results["groups"] = group_result
    if membership_surface_config(plan)["mode"] != "skip":
        results["memberships"] = import_memberships(
            plan,
            destination,
            snapshot,
            group_result,
            set((user_result or {}).get("targets") or []),
        )
    if source_mode(plan, "projects") == "managed" or source_mode(plan, "repositories") == "managed":
        results["repositories"] = import_repositories(plan, snapshot, destination, work_dir)
    if source_mode(plan, "permissions") != "skip":
        results["permissions"] = import_permissions(
            plan,
            snapshot,
            destination,
            group_result,
            set((user_result or {}).get("targets") or []),
        )
    if source_mode(plan, "rules") != "skip":
        results["rules"] = import_rules(plan, snapshot, destination)
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
