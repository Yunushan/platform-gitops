#!/usr/bin/env python3
"""Operate an optional GitLab/GitHub to Forgejo transition period.

The transition keeps the source forge writable while repository data moves in
one direction to Forgejo. Source CI is disabled only after shadow verification;
Woodpecker and Argo CD then become the sole CI/CD authority. Finalization freezes
the source, proves zero ref drift, and promotes Forgejo to repository authority.
"""

from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import time
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

import forge_cutover as cutover
import forge_migration as migration


PLAN_VERSION = 1
PROOF_VERSION = 1
TOOL = "platform-forge-transition"
SUPPORTED_DIRECTIONS = {"gitlab-to-forgejo", "github-to-forgejo"}
RELAY_DRIVERS = {"external", "gitlab-push"}
TRANSITION_PHASES = {
    "planned",
    "shadow",
    "transition",
    "finalized",
    "rolled-back",
    "rollback-failed",
}
GITHUB_ACTIVE_RUN_STATES = {
    "queued",
    "in_progress",
    "requested",
    "pending",
    "waiting",
}
GITHUB_SECRET_TYPES = {"repository-secret", "environment-secret", "organization-secret"}


class TransitionError(migration.MigrationError):
    """Raised when transition state cannot be proven safe."""


@dataclass(frozen=True)
class TransitionRepo:
    migration: migration.RepoPlan
    raw: dict[str, Any]
    mappings: dict[str, Any]
    transition: dict[str, Any]

    @property
    def cutover_repo(self) -> cutover.CutoverRepo:
        return cutover.CutoverRepo(self.migration, self.raw, self.mappings)


@dataclass(frozen=True)
class TransitionPlan:
    raw: dict[str, Any]
    direction: str
    source_provider: str
    repositories: tuple[TransitionRepo, ...]
    services: dict[str, dict[str, Any]]
    control: dict[str, Any]
    sha256: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_digest(value: Any) -> str:
    return cutover.canonical_digest(value)


def object_value(parent: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise TransitionError(f"{label}.{key} must be an object")
    return value


def list_value(parent: dict[str, Any], key: str, label: str) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise TransitionError(f"{label}.{key} must be an array")
    return value


def string_value(parent: dict[str, Any], key: str, label: str, required: bool = True) -> str:
    value = str(parent.get(key) or "").strip()
    if required and not value:
        raise TransitionError(f"{label}.{key} is required")
    return value


def env_name(parent: dict[str, Any], key: str, label: str, required: bool = True) -> str:
    value = string_value(parent, key, label, required=required)
    if value and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise TransitionError(f"{label}.{key} must name an environment variable")
    return value


def accounted_mode(entry: dict[str, Any], label: str) -> str:
    mode = str(entry.get("mode") or "").strip().lower()
    if mode == "unsupported":
        raise TransitionError(f"{label} is marked unsupported")
    if mode not in cutover.ACCOUNTED_MODES:
        raise TransitionError(f"{label}.mode must be one of {sorted(cutover.ACCOUNTED_MODES)}")
    if mode in {"manual", "skipped"}:
        if not cutover.bool_value(entry.get("accepted"), False) or not str(entry.get("reason") or "").strip():
            raise TransitionError(f"{label} {mode} mode requires accepted=true and a reason")
    return mode


def validate_relay(repo: migration.RepoPlan, section: dict[str, Any], label: str) -> None:
    mode = accounted_mode(section, label)
    if mode not in {"managed", "mapped"}:
        raise TransitionError(f"{label} must be managed or mapped")
    driver = string_value(section, "driver", label).lower()
    if driver not in RELAY_DRIVERS:
        raise TransitionError(f"{label}.driver must be one of {sorted(RELAY_DRIVERS)}")
    if driver == "gitlab-push" and repo.source_provider != "gitlab":
        raise TransitionError(f"{label}.driver=gitlab-push requires a GitLab source")
    cutover.int_value(section.get("sync_interval_seconds"), 60, minimum=10)
    cutover.int_value(section.get("max_lag_seconds"), 300, minimum=30)
    cutover.int_value(section.get("sync_timeout_seconds"), 900, minimum=30)
    if driver == "gitlab-push" and mode == "managed":
        auth_method = str(section.get("auth_method") or "password").lower()
        if auth_method not in {"password", "ssh_public_key"}:
            raise TransitionError(f"{label}.auth_method must be password or ssh_public_key")
        if auth_method == "password":
            env_name(section, "username_env", label)
            env_name(section, "password_env", label)
        else:
            host_keys = section.get("host_keys") or []
            if not isinstance(host_keys, list) or not all(str(item).strip() for item in host_keys):
                raise TransitionError(f"{label}.host_keys must contain the approved Forgejo SSH host keys")


def validate_source_ci(section: dict[str, Any], label: str) -> None:
    mode = accounted_mode(section, label)
    if mode not in {"managed", "mapped"}:
        raise TransitionError(f"{label} must be managed or mapped")
    if cutover.bool_value(section.get("keep_repository_writable"), True) is not True:
        raise TransitionError(f"{label}.keep_repository_writable must be true during transition")
    cutover.bool_value(section.get("cancel_active"), False)
    cutover.int_value(section.get("shutdown_timeout_seconds"), 300, minimum=30)


def validate_github_variables(section: dict[str, Any], label: str) -> None:
    if str(section.get("unmapped") or "fail").lower() != "fail":
        raise TransitionError(f"{label}.unmapped must be fail")
    for scope_name in ("organization_scope", "environment_scope"):
        accounted_mode(object_value(section, scope_name, label), f"{label}.{scope_name}")
    mappings = list_value(section, "mappings", label)
    seen: set[str] = set()
    for index, raw_mapping in enumerate(mappings):
        item_label = f"{label}.mappings[{index}]"
        if not isinstance(raw_mapping, dict):
            raise TransitionError(f"{item_label} must be an object")
        source = string_value(raw_mapping, "source", item_label)
        if source in seen:
            raise TransitionError(f"{label}.mappings contains duplicate source {source}")
        seen.add(source)
        mode = accounted_mode(raw_mapping, item_label)
        if mode == "managed":
            string_value(raw_mapping, "target_name", item_label)
            target = str(raw_mapping.get("target") or "woodpecker_secret")
            if target != "woodpecker_secret":
                raise TransitionError(f"{item_label}.target only supports woodpecker_secret")
            kind = source.split(":", 1)[0]
            if kind in GITHUB_SECRET_TYPES:
                env_name(raw_mapping, "value_env", item_label)
            for array_name in ("events", "images"):
                value = raw_mapping.get(array_name, [])
                if not isinstance(value, list):
                    raise TransitionError(f"{item_label}.{array_name} must be an array")


def validate_control(control: dict[str, Any]) -> None:
    for key in (
        "prepare_confirmation_env",
        "enter_confirmation_env",
        "finalize_confirmation_env",
        "fallback_confirmation_env",
        "rollback_confirmation_env",
        "failback_confirmation_env",
        "live_env",
        "change_ticket_env",
    ):
        env_name(control, key, "transition_control")
    cutover.int_value(control.get("max_proof_age_seconds"), 3600, minimum=60)
    cutover.int_value(control.get("relay_failure_threshold"), 3, minimum=1)
    if not cutover.bool_value(control.get("auto_rollback"), True):
        raise TransitionError("transition_control.auto_rollback must be true")


def validate_destination_access(section: dict[str, Any], label: str) -> None:
    mode = accounted_mode(section, label)
    if mode != "managed":
        raise TransitionError(f"{label} must use mode=managed for fail-closed authority control")
    mirror_actor = string_value(section, "mirror_actor", label)
    string_value(section, "protection_pattern", label)
    shadow_settings = object_value(section, "shadow_settings", label)
    final_settings = object_value(section, "final_settings", label)
    if not cutover.bool_value(shadow_settings.get("enable_push_whitelist"), False):
        raise TransitionError(f"{label}.shadow_settings.enable_push_whitelist must be true")
    shadow_writers = shadow_settings.get("push_whitelist_usernames")
    if not isinstance(shadow_writers, list) or sorted(str(item) for item in shadow_writers) != [mirror_actor]:
        raise TransitionError(
            f"{label}.shadow_settings.push_whitelist_usernames must contain only mirror_actor"
        )
    if not final_settings:
        raise TransitionError(f"{label}.final_settings must define the post-finalization writer policy")


def parse_transition_plan(data: dict[str, Any]) -> TransitionPlan:
    cutover.require_credential_free_plan(data)
    if data.get("transition_version", PLAN_VERSION) != PLAN_VERSION:
        raise TransitionError(f"transition_version must be {PLAN_VERSION}")
    direction = str(data.get("direction") or "").strip().lower()
    if direction not in SUPPORTED_DIRECTIONS:
        raise TransitionError(f"direction must be one of {sorted(SUPPORTED_DIRECTIONS)}")
    source_provider, destination_provider = migration.split_direction(direction)
    if destination_provider != "forgejo":
        raise TransitionError("transition destination must be Forgejo")
    _parsed_direction, migration_repos = migration.parse_plan(data)
    raw_repositories = list_value(data, "repositories", "plan")
    repositories: list[TransitionRepo] = []
    for index, (repo, raw_repo) in enumerate(zip(migration_repos, raw_repositories, strict=True)):
        if not isinstance(raw_repo, dict):
            raise TransitionError(f"repositories[{index}] must be an object")
        label = f"repositories[{index}]"
        for side, token_env in (
            ("source", repo.source_token_env),
            ("destination", repo.destination_token_env),
        ):
            if not token_env or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env):
                raise TransitionError(f"{label}.{side}.token_env must name an environment variable")
        mappings = object_value(raw_repo, "cutover", label)
        transition = object_value(raw_repo, "transition", label)
        expected_mappings = {
            "pipelines",
            "variables",
            "schedules",
            "runner_tags",
            "protections",
            "integrations",
        }
        unexpected = sorted(set(mappings).difference(expected_mappings))
        if unexpected:
            raise TransitionError(f"{label}.cutover has unsupported surface(s): {', '.join(unexpected)}")
        cutover.validate_pipeline_section(object_value(mappings, "pipelines", f"{label}.cutover"), f"{label}.cutover.pipelines")
        if source_provider == "gitlab":
            cutover.validate_variable_section(object_value(mappings, "variables", f"{label}.cutover"), f"{label}.cutover.variables")
        else:
            validate_github_variables(object_value(mappings, "variables", f"{label}.cutover"), f"{label}.cutover.variables")
        cutover.validate_mapped_section(object_value(mappings, "schedules", f"{label}.cutover"), f"{label}.cutover.schedules", "source", managed_fields=("target_name", "schedule", "branch"))
        cutover.validate_mapped_section(object_value(mappings, "runner_tags", f"{label}.cutover"), f"{label}.cutover.runner_tags", "source")
        for mapping_index, mapping_item in enumerate(mappings["runner_tags"].get("mappings", [])):
            if str(mapping_item.get("mode") or "").lower() in {"managed", "mapped"}:
                labels = mapping_item.get("target_labels")
                if not isinstance(labels, dict) or not labels:
                    raise TransitionError(f"{label}.cutover.runner_tags.mappings[{mapping_index}].target_labels must be a non-empty object")
        cutover.validate_mapped_section(object_value(mappings, "protections", f"{label}.cutover"), f"{label}.cutover.protections", "source")
        cutover.validate_mapped_section(object_value(mappings, "integrations", f"{label}.cutover"), f"{label}.cutover.integrations", "source")
        expected_transition = {"relay", "source_ci", "destination_access"}
        unexpected_transition = sorted(set(transition).difference(expected_transition))
        if unexpected_transition:
            raise TransitionError(f"{label}.transition has unsupported setting(s): {', '.join(unexpected_transition)}")
        validate_relay(repo, object_value(transition, "relay", f"{label}.transition"), f"{label}.transition.relay")
        validate_source_ci(object_value(transition, "source_ci", f"{label}.transition"), f"{label}.transition.source_ci")
        destination_access = object_value(transition, "destination_access", f"{label}.transition")
        validate_destination_access(destination_access, f"{label}.transition.destination_access")
        repositories.append(TransitionRepo(repo, raw_repo, mappings, transition))
    services = object_value(data, "services", "plan")
    cutover.validate_services(services)
    control = object_value(data, "transition_control", "plan")
    validate_control(control)
    return TransitionPlan(
        raw=copy.deepcopy(data),
        direction=direction,
        source_provider=source_provider,
        repositories=tuple(repositories),
        services=services,
        control=control,
        sha256=canonical_digest(data),
    )


def load_transition_plan(path: Path) -> TransitionPlan:
    return parse_transition_plan(migration.load_plan(path))


def require_credentials(plan: TransitionPlan) -> None:
    for repo in plan.repositories:
        for label, name in (
            ("source", repo.migration.source_token_env),
            ("destination", repo.migration.destination_token_env),
        ):
            if not name or not os.environ.get(name, ""):
                raise TransitionError(f"{repo.migration.name}: {label} credential environment variable {name or '<missing>'} is not set")
        relay = repo.transition["relay"]
        if relay["driver"] == "gitlab-push" and relay["mode"] == "managed" and str(relay.get("auth_method") or "password") == "password":
            for key in ("username_env", "password_env"):
                name = str(relay[key])
                if not os.environ.get(name, ""):
                    raise TransitionError(f"{repo.migration.name}: relay credential {name} is not set")
        if repo.migration.source_provider == "github":
            for mapping_item in repo.mappings["variables"].get("mappings", []):
                value_env = str(mapping_item.get("value_env") or "")
                if value_env and not os.environ.get(value_env, ""):
                    raise TransitionError(
                        f"{repo.migration.name}: GitHub secret source environment variable {value_env} is not set"
                    )
    for service_name, config in plan.services.items():
        if config.get("mode") not in {"managed", "mapped"}:
            continue
        keys = ("username_env", "password_env") if service_name == "harbor" else ("token_env",)
        for key in keys:
            name = str(config.get(key) or "")
            if not name or not os.environ.get(name, ""):
                raise TransitionError(f"services.{service_name}.{key} credential {name or '<missing>'} is not set")


def sanitize(value: Any) -> Any:
    return cutover.sanitize_for_proof(value)


def proof_base(plan: TransitionPlan, command: str, repositories: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": PROOF_VERSION,
        "tool": TOOL,
        "command": command,
        "generated_at": utc_now(),
        "plan_sha256": plan.sha256,
        "direction": plan.direction,
        "repositories": sanitize(repositories),
        "verified": all(bool(item.get("verified")) for item in repositories),
    }


def write_proof(path: Path | None, proof: dict[str, Any]) -> dict[str, Any]:
    safe = sanitize(proof)
    safe["proof_sha256"] = migration.proof_digest(safe)
    text = json.dumps(safe, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(text, end="")
        return safe
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise TransitionError(f"could not write proof {path}: {exc}") from exc
    return safe


def load_proof(path: Path, plan: TransitionPlan, commands: Iterable[str], require_verified: bool = True) -> dict[str, Any]:
    proof = migration.load_plan(path)
    claimed = str(proof.get("proof_sha256") or "")
    if not claimed or claimed != migration.proof_digest(proof):
        raise TransitionError(f"{path}: proof integrity verification failed")
    if proof.get("tool") != TOOL or proof.get("command") not in set(commands):
        raise TransitionError(f"{path}: proof command is not accepted for this operation")
    if proof.get("plan_sha256") != plan.sha256:
        raise TransitionError(f"{path}: proof was produced from a different plan")
    if require_verified and proof.get("verified") is not True:
        raise TransitionError(f"{path}: proof is not verified")
    return proof


def proof_age_seconds(proof: dict[str, Any]) -> float:
    generated = str(proof.get("generated_at") or "")
    try:
        return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(generated.replace("Z", "+00:00"))).total_seconds())
    except ValueError as exc:
        raise TransitionError("proof generated_at is invalid") from exc


def require_confirmation(plan: TransitionPlan, proof: dict[str, Any], env_key: str) -> None:
    live_env = str(plan.control["live_env"])
    confirmation_env = str(plan.control[env_key])
    ticket_env = str(plan.control["change_ticket_env"])
    if os.environ.get(live_env, "") != "1":
        raise TransitionError(f"live transition is disabled; set {live_env}=1")
    if os.environ.get(confirmation_env, "") != str(proof.get("proof_sha256") or ""):
        raise TransitionError(f"approval mismatch; set {confirmation_env} to the approved proof digest")
    if not os.environ.get(ticket_env, "").strip():
        raise TransitionError(f"{ticket_env} is required")
    max_age = cutover.int_value(plan.control.get("max_proof_age_seconds"), 3600, minimum=60)
    if proof_age_seconds(proof) > max_age:
        raise TransitionError("approved proof is stale; verify again")


def api_target(repo: TransitionRepo, side: str) -> migration.ApiTarget:
    return migration.api_target(repo.migration, side)


def api_base(repo: TransitionRepo, side: str) -> tuple[migration.ApiTarget, str]:
    target = api_target(repo, side)
    return target, migration.repo_api_base(target)


def paged_list(target: migration.ApiTarget, path: str, key: str | None = None, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        request_query = {**(query or {}), "page": page, "per_page": 100}
        payload = migration.api_request(target, "GET", path, query=request_query)
        items = payload.get(key, []) if key and isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise TransitionError(f"{target.provider} endpoint {path} returned an invalid list")
        results.extend(item for item in items if isinstance(item, dict))
        if len(items) < 100:
            return results
        page += 1


def github_file_text(repo: TransitionRepo, path: str, branch: str) -> str:
    target, base = api_base(repo, "source")
    payload = migration.api_request(target, "GET", f"{base}/contents/{quote(path, safe='/')}", query={"ref": branch})
    encoded = str(payload.get("content") or "").replace("\n", "")
    if str(payload.get("encoding") or "") != "base64" or not encoded:
        raise TransitionError(f"{repo.migration.name}: GitHub returned unreadable workflow content for {path}")
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise TransitionError(f"{repo.migration.name}: GitHub workflow {path} is not valid UTF-8") from exc


def github_workflows(repo: TransitionRepo, project: dict[str, Any]) -> list[dict[str, Any]]:
    target, base = api_base(repo, "source")
    default_branch = str(project.get("default_branch") or "")
    payload = migration.api_request(target, "GET", f"{base}/git/trees/{quote(default_branch, safe='')}", query={"recursive": 1})
    tree = payload.get("tree") if isinstance(payload, dict) else None
    if not isinstance(tree, list):
        raise TransitionError(f"{repo.migration.name}: GitHub tree inventory is invalid")
    patterns = repo.mappings["pipelines"].get("source_globs") or [".github/workflows/*.yml", ".github/workflows/*.yaml"]
    workflows: list[dict[str, Any]] = []
    for item in tree:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if item.get("type") != "blob" or not any(fnmatch(path, str(pattern)) for pattern in patterns):
            continue
        text = github_file_text(repo, path, default_branch)
        schedules = sorted(set(re.findall(r"(?m)^\s*-?\s*cron\s*:\s*['\"]?([^'\"#\r\n]+)", text)))
        workflows.append({"path": path, "sha": item.get("sha"), "schedules": schedules})
    return sorted(workflows, key=lambda item: item["path"])


def github_variable_inventory(repo: TransitionRepo, include_values: bool) -> list[dict[str, Any]]:
    target, base = api_base(repo, "source")
    section = repo.mappings["variables"]
    results: list[dict[str, Any]] = []
    for item in paged_list(target, f"{base}/actions/variables", key="variables"):
        results.append({"identity": f"repository-variable:{item.get('name')}", "kind": "repository-variable", "name": item.get("name"), "value": item.get("value") if include_values else None})
    for item in paged_list(target, f"{base}/actions/secrets", key="secrets"):
        results.append({"identity": f"repository-secret:{item.get('name')}", "kind": "repository-secret", "name": item.get("name"), "value": None})
    environments_mode = str(section["environment_scope"]["mode"]).lower()
    if environments_mode in {"managed", "mapped"}:
        for environment in paged_list(target, f"{base}/environments", key="environments"):
            environment_name = str(environment.get("name") or "")
            encoded = quote(environment_name, safe="")
            for item in paged_list(target, f"{base}/environments/{encoded}/variables", key="variables"):
                results.append({"identity": f"environment-variable:{environment_name}:{item.get('name')}", "kind": "environment-variable", "environment": environment_name, "name": item.get("name"), "value": item.get("value") if include_values else None})
            for item in paged_list(target, f"{base}/environments/{encoded}/secrets", key="secrets"):
                results.append({"identity": f"environment-secret:{environment_name}:{item.get('name')}", "kind": "environment-secret", "environment": environment_name, "name": item.get("name"), "value": None})
    organization_mode = str(section["organization_scope"]["mode"]).lower()
    if organization_mode in {"managed", "mapped"}:
        owner = str(repo.migration.source_api_repository or "").split("/", 1)[0]
        for item in paged_list(target, f"orgs/{quote(owner, safe='')}/actions/variables", key="variables"):
            results.append({"identity": f"organization-variable:{item.get('name')}", "kind": "organization-variable", "name": item.get("name"), "value": item.get("value") if include_values else None})
        for item in paged_list(target, f"orgs/{quote(owner, safe='')}/actions/secrets", key="secrets"):
            results.append({"identity": f"organization-secret:{item.get('name')}", "kind": "organization-secret", "name": item.get("name"), "value": None})
    return sorted(results, key=lambda item: item["identity"])


def account_github_variables(repo: TransitionRepo, inventory: list[dict[str, Any]]) -> dict[str, Any]:
    mappings = {str(item.get("source") or ""): item for item in repo.mappings["variables"].get("mappings", [])}
    items: list[dict[str, Any]] = []
    unaccounted: list[str] = []
    for variable in inventory:
        identity = str(variable["identity"])
        mapping_item = mappings.get(identity)
        if mapping_item is None:
            unaccounted.append(identity)
            mode = "unaccounted"
            target_name = ""
        else:
            mode = str(mapping_item["mode"]).lower()
            target_name = str(mapping_item.get("target_name") or "")
        items.append({"identity": identity, "kind": variable["kind"], "name": variable.get("name"), "environment": variable.get("environment"), "mode": mode, "target_name": target_name, "configured": variable["kind"] in GITHUB_SECRET_TYPES or bool(variable.get("value"))})
    stale = sorted(set(mappings).difference(str(item["identity"]) for item in inventory))
    return {"items": items, "unaccounted": sorted(unaccounted), "stale_mappings": stale, "verified": not unaccounted and not stale}


def github_discover(repo: TransitionRepo, verify_destination: bool = False) -> dict[str, Any]:
    source, source_base = api_base(repo, "source")
    destination, destination_base = api_base(repo, "destination")
    project = migration.api_request(source, "GET", source_base)
    destination_status, destination_project = migration.api_request(destination, "GET", destination_base, expected=(200, 404), return_status=True)
    destination_exists = destination_status == 200
    source_workflows = github_workflows(repo, project)
    destination_files: list[dict[str, Any]] = []
    default_branch = str(destination_project.get("default_branch") or project.get("default_branch") or "main")
    if destination_exists:
        destination_files = cutover.inventory_forgejo_pipeline_files(repo.cutover_repo, default_branch)
    pipeline_result = cutover.account_pipeline_files(repo.cutover_repo, source_workflows, destination_files, default_branch, verify_destination=verify_destination and destination_exists)
    variable_inventory = github_variable_inventory(repo, include_values=False)
    variables = account_github_variables(repo, variable_inventory)
    schedules_raw = [{"source": f"{workflow['path']}:{cron}", "workflow": workflow["path"], "cron": cron} for workflow in source_workflows for cron in workflow.get("schedules", [])]
    schedules = cutover.account_named_surface(schedules_raw, repo.mappings["schedules"].get("mappings", []), ("source",), "schedules")
    runners_payload = migration.api_request(source, "GET", f"{source_base}/actions/runners", query={"per_page": 100})
    runners = runners_payload.get("runners", []) if isinstance(runners_payload, dict) else []
    labels = sorted({str(label.get("name") or "") for runner in runners if isinstance(runner, dict) for label in (runner.get("labels") or []) if isinstance(label, dict) and str(label.get("name") or "")})
    runner_tags = cutover.account_named_surface([{"tag": label} for label in labels], repo.mappings["runner_tags"].get("mappings", []), ("tag",), "runner_tags")
    branches = paged_list(source, f"{source_base}/branches", query={"protected": "true"})
    protections = cutover.account_named_surface(branches, repo.mappings["protections"].get("mappings", []), ("name",), "protections")
    hooks = paged_list(source, f"{source_base}/hooks")
    integration_inventory = [{**hook, "cutover_key": f"hook:{cutover.source_key(hook, ('name', 'config', 'id'))}"} for hook in hooks]
    integrations = cutover.account_named_surface(integration_inventory, repo.mappings["integrations"].get("mappings", []), ("cutover_key",), "integrations")
    actions = migration.api_request(source, "GET", f"{source_base}/actions/permissions")
    source_state = {"repository_id": project.get("id"), "full_name": project.get("full_name"), "default_branch": project.get("default_branch"), "archived": cutover.bool_value(project.get("archived"), False), "actions": actions}
    verified = all(surface.get("verified") for surface in (pipeline_result, variables, schedules, runner_tags, protections, integrations))
    return {"name": repo.migration.name, "source_url": migration.redact_url(repo.migration.source_url), "destination_url": migration.redact_url(repo.migration.destination_url), "source_state": source_state, "destination": {"exists": destination_exists, "id": destination_project.get("id"), "full_name": destination_project.get("full_name"), "default_branch": default_branch}, "pipelines": pipeline_result, "variables": variables, "schedules": schedules, "runner_tags": runner_tags, "protections": protections, "integrations": integrations, "verified": verified}


def discover_repository(repo: TransitionRepo, verify_destination: bool = False) -> dict[str, Any]:
    if repo.migration.source_provider == "gitlab":
        return cutover.discover_repository(repo.cutover_repo, verify_destination=verify_destination)
    return github_discover(repo, verify_destination=verify_destination)


def cutover_plan_view(plan: TransitionPlan) -> cutover.CutoverPlan:
    """Expose the shared destination service contract without changing its plan."""
    return cutover.CutoverPlan(
        raw=plan.raw,
        repositories=tuple(repo.cutover_repo for repo in plan.repositories),
        services=plan.services,
        activation={},
        sha256=plan.sha256,
    )


def source_variable_values(repo: TransitionRepo) -> dict[str, str]:
    if repo.migration.source_provider == "gitlab":
        inventory = cutover.list_gitlab_variables(repo.cutover_repo, include_values=True)
        values: dict[str, str] = {}
        for variable in inventory:
            scope = str(variable.get("source_scope") or "project")
            value = str(variable.get("value") or "")
            values[cutover.variable_identity(scope, variable)] = value
            values[f"{scope}:{variable.get('key')}"] = value
        return values

    inventory = github_variable_inventory(repo, include_values=True)
    values = {}
    by_identity = {str(item["identity"]): item for item in inventory}
    for mapping_item in repo.mappings["variables"].get("mappings", []):
        if str(mapping_item.get("mode") or "").lower() != "managed":
            continue
        identity = str(mapping_item["source"])
        item = by_identity.get(identity)
        if item is None:
            raise TransitionError(f"{repo.migration.name}: mapped GitHub variable is missing: {identity}")
        value_env = str(mapping_item.get("value_env") or "")
        value = os.environ.get(value_env, "") if value_env else str(item.get("value") or "")
        if not value:
            source = value_env or identity
            raise TransitionError(f"{repo.migration.name}: mapped GitHub value is unavailable: {source}")
        if value_env:
            cutover.register_secret(value)
        values[identity] = value
    return values


def normalized_setting(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(normalized_setting(item) for item in value)
    if isinstance(value, dict):
        return {str(key): normalized_setting(child) for key, child in sorted(value.items())}
    return value


def protection_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(normalized_setting(actual.get(key)) == normalized_setting(value) for key, value in expected.items())


def set_destination_access(repo: TransitionRepo, phase: str) -> dict[str, Any]:
    if phase not in {"shadow", "finalized"}:
        raise TransitionError(f"unsupported destination access phase: {phase}")
    config = repo.transition["destination_access"]
    pattern = str(config["protection_pattern"])
    settings = copy.deepcopy(config["shadow_settings" if phase == "shadow" else "final_settings"])
    settings["rule_name"] = pattern
    target, base = api_base(repo, "destination")
    payload = migration.api_request(target, "GET", f"{base}/branch_protections")
    if not isinstance(payload, list):
        raise TransitionError(f"{repo.migration.name}: Forgejo branch protections returned invalid data")
    existing = next(
        (
            item
            for item in payload
            if str(item.get("rule_name") or item.get("branch_name") or item.get("name") or "") == pattern
        ),
        None,
    )
    if existing:
        migration.api_request(
            target,
            "PATCH",
            f"{base}/branch_protections/{quote(pattern, safe='')}",
            body=settings,
        )
        action = "updated"
    else:
        migration.api_request(
            target,
            "POST",
            f"{base}/branch_protections",
            body=settings,
            expected=(200, 201),
        )
        action = "created"
    verification = verify_destination_access(repo, phase)
    return {"phase": phase, "pattern": pattern, "action": action, **verification}


def verify_destination_access(repo: TransitionRepo, phase: str) -> dict[str, Any]:
    config = repo.transition["destination_access"]
    pattern = str(config["protection_pattern"])
    expected = copy.deepcopy(config["shadow_settings" if phase == "shadow" else "final_settings"])
    target, base = api_base(repo, "destination")
    payload = migration.api_request(target, "GET", f"{base}/branch_protections")
    if not isinstance(payload, list):
        raise TransitionError(f"{repo.migration.name}: Forgejo branch protections returned invalid data")
    actual = next(
        (
            item
            for item in payload
            if str(item.get("rule_name") or item.get("branch_name") or item.get("name") or "") == pattern
        ),
        None,
    )
    verified = isinstance(actual, dict) and protection_matches(actual, expected)
    return {
        "pattern": pattern,
        "expected_writer_policy": sanitize(expected),
        "present": actual is not None,
        "verified": verified,
    }


def prepare_transition_repository(plan: TransitionPlan, repo: TransitionRepo) -> dict[str, Any]:
    shared_plan = cutover_plan_view(plan)
    with tempfile.TemporaryDirectory(prefix="forge-transition-prepare-") as temp:
        migration_result = migration.migrate_repo(repo.migration, Path(temp))
    destination_repository = migration_result["destination_repository"]
    destination, destination_base = api_base(repo, "destination")
    destination_payload = migration.api_request(destination, "GET", destination_base)
    forge_remote_id = int(destination_payload.get("id") or 0)
    if forge_remote_id <= 0:
        raise TransitionError(f"{repo.migration.name}: Forgejo repository ID is missing")

    woodpecker_config = plan.services["woodpecker"]
    if woodpecker_config["mode"] in {"manual", "skipped"}:
        raise TransitionError("Woodpecker must be managed or mapped for automated transition")
    woodpecker = cutover.service_target("woodpecker", woodpecker_config)
    full_name = str(destination_payload.get("full_name") or repo.migration.destination_api_repository or "")
    woodpecker_repo = cutover.woodpecker_lookup(woodpecker, full_name, required=False)
    repo_action = "existing"
    if woodpecker_repo is None:
        woodpecker_repo = cutover.service_request(
            woodpecker,
            "POST",
            "api/repos",
            query={"forge_remote_id": forge_remote_id},
            expected=(200, 201),
        )
        repo_action = "activated"
    woodpecker_repo_id = int(woodpecker_repo.get("id") or 0)
    if woodpecker_repo_id <= 0:
        raise TransitionError(f"{repo.migration.name}: Woodpecker repository ID is missing")

    pipeline_config = repo.mappings["pipelines"]
    patch_body: dict[str, Any] = {
        "require_approval": str(woodpecker_config.get("require_approval") or "pull_requests"),
        "trusted": {"network": False, "volumes": False, "security": False},
        "allow_pr": True,
    }
    if pipeline_config.get("config_file"):
        patch_body["config_file"] = str(pipeline_config["config_file"])
    cutover.service_request(woodpecker, "PATCH", f"api/repos/{woodpecker_repo_id}", body=patch_body)
    gate = cutover.woodpecker_secret_upsert(
        woodpecker,
        woodpecker_repo_id,
        str(woodpecker_config["shadow_gate_secret"]),
        "false",
        events=["push", "tag", "deployment", "manual"],
        note="Fail-closed deployment authority gate managed by platform forge transition",
    )
    values = source_variable_values(repo)
    managed_secrets: list[dict[str, Any]] = []
    for mapping_item in repo.mappings["variables"].get("mappings", []):
        if str(mapping_item.get("mode") or "").lower() != "managed":
            continue
        identity = str(mapping_item["source"])
        value = values.get(identity, "")
        if not value:
            raise TransitionError(f"{repo.migration.name}: mapped variable has no readable value: {identity}")
        managed_secrets.append(
            cutover.woodpecker_secret_upsert(
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
    for mapping_item in repo.mappings["schedules"].get("mappings", []):
        if str(mapping_item.get("mode") or "").lower() != "managed":
            continue
        crons.append(
            cutover.woodpecker_cron_upsert(
                woodpecker,
                woodpecker_repo_id,
                str(mapping_item["target_name"]),
                str(mapping_item["schedule"]),
                str(mapping_item["branch"]),
                enabled=False,
            )
        )
    protections = cutover.prepare_destination_protections(repo.cutover_repo)
    destination_access = set_destination_access(repo, "shadow")
    harbor = cutover.prepare_harbor(shared_plan, woodpecker, woodpecker_repo_id)
    argocd = cutover.verify_argocd(shared_plan)
    verified = all(
        (
            destination_repository.get("verified"),
            migration_result.get("verified"),
            gate.get("verified"),
            all(secret.get("verified") for secret in managed_secrets),
            all(cron.get("verified") for cron in crons),
            protections.get("verified"),
            destination_access.get("verified"),
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
        "destination_access": destination_access,
        "argocd": argocd,
        "verified": verified,
    }


def verify_transition_repository(
    plan: TransitionPlan,
    repo: TransitionRepo,
    prepared: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    if phase not in {"shadow", "transition", "finalized"}:
        raise TransitionError(f"unsupported verification phase: {phase}")
    shared_plan = cutover_plan_view(plan)
    current_inventory = discover_repository(repo, verify_destination=True)
    migration_result = migration.verify_repo(repo.migration)
    woodpecker_config = plan.services["woodpecker"]
    woodpecker = cutover.service_target("woodpecker", woodpecker_config)
    destination, destination_base = api_base(repo, "destination")
    destination_payload = migration.api_request(destination, "GET", destination_base)
    full_name = str(destination_payload.get("full_name") or repo.migration.destination_api_repository or "")
    wp_repo = cutover.woodpecker_lookup(woodpecker, full_name)
    assert wp_repo is not None
    repo_id = int(wp_repo.get("id") or 0)
    agents = cutover.service_request(woodpecker, "GET", "api/agents")
    if not isinstance(agents, list):
        raise TransitionError("Woodpecker agent list returned invalid data")
    runner_capabilities = cutover.verify_runner_capabilities(repo.cutover_repo, agents)
    authority_phase = "post-cutover" if phase in {"transition", "finalized"} else "shadow"
    woodpecker_configuration = cutover.verify_woodpecker_configuration(
        shared_plan,
        repo.cutover_repo,
        woodpecker,
        repo_id,
        authority_phase,
    )
    protections = cutover.verify_destination_protections(repo.cutover_repo)
    integrations = cutover.verify_destination_integrations(repo.cutover_repo, woodpecker.api_url)
    destination_access = verify_destination_access(
        repo,
        "finalized" if phase == "finalized" else "shadow",
    )
    canary = cutover.trigger_woodpecker_canary(
        woodpecker,
        repo_id,
        str(destination_payload.get("default_branch") or "main"),
        cutover.int_value(woodpecker_config.get("canary_timeout_seconds"), 900, minimum=30),
        phase,
    )
    harbor = cutover.verify_harbor_canary(shared_plan)
    argocd = cutover.verify_argocd(shared_plan)
    verified = all(
        (
            migration_result.get("verified"),
            current_inventory.get("verified"),
            current_inventory["pipelines"].get("verified"),
            current_inventory["variables"].get("verified"),
            woodpecker_configuration.get("verified"),
            runner_capabilities.get("verified"),
            protections.get("verified"),
            integrations.get("verified"),
            destination_access.get("verified"),
            canary.get("verified"),
            harbor.get("verified"),
            argocd.get("verified"),
        )
    )
    return {
        "name": repo.migration.name,
        "phase": phase,
        "inventory": current_inventory,
        "migration": migration_result,
        "woodpecker_repository_id": repo_id,
        "woodpecker_configuration": woodpecker_configuration,
        "runner_capabilities": runner_capabilities,
        "protections": protections,
        "integrations": integrations,
        "destination_access": destination_access,
        "canary": canary,
        "harbor": harbor,
        "argocd": argocd,
        "prepared_repository": prepared.get("name"),
        "verified": verified,
    }


def github_active_runs(repo: TransitionRepo) -> list[dict[str, Any]]:
    target, base = api_base(repo, "source")
    runs = paged_list(target, f"{base}/actions/runs", key="workflow_runs")
    return [item for item in runs if str(item.get("status") or "").lower() in GITHUB_ACTIVE_RUN_STATES]


def source_ci_snapshot(repo: TransitionRepo) -> dict[str, Any]:
    target, base = api_base(repo, "source")
    project = migration.api_request(target, "GET", base)
    if repo.migration.source_provider == "gitlab":
        schedules = cutover.gitlab_list(target, f"{base}/pipeline_schedules")
        return {
            "provider": "gitlab",
            "archived": cutover.bool_value(project.get("archived"), False),
            "builds_access_level": str(project.get("builds_access_level") or "enabled"),
            "schedules": [
                {"id": item.get("id"), "active": cutover.bool_value(item.get("active"), False)}
                for item in schedules
            ],
        }
    actions = migration.api_request(target, "GET", f"{base}/actions/permissions")
    return {
        "provider": "github",
        "archived": cutover.bool_value(project.get("archived"), False),
        "actions_enabled": cutover.bool_value(actions.get("enabled"), False),
        "allowed_actions": str(actions.get("allowed_actions") or "all"),
    }


def wait_for_source_ci_idle(repo: TransitionRepo, cancel_active: bool, timeout_seconds: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if repo.migration.source_provider == "gitlab":
            active = cutover.gitlab_active_pipelines(repo.cutover_repo)
        else:
            active = github_active_runs(repo)
        if not active:
            return []
        if cancel_active:
            target, base = api_base(repo, "source")
            for item in active:
                item_id = item.get("id")
                if repo.migration.source_provider == "gitlab":
                    migration.api_request(
                        target,
                        "POST",
                        f"{base}/pipelines/{item_id}/cancel",
                        expected=(200, 201),
                    )
                else:
                    migration.api_request(
                        target,
                        "POST",
                        f"{base}/actions/runs/{item_id}/cancel",
                        expected=(202, 409),
                    )
        if time.monotonic() >= deadline:
            return active
        time.sleep(2)


def disable_source_ci(repo: TransitionRepo, snapshot: dict[str, Any]) -> dict[str, Any]:
    target, base = api_base(repo, "source")
    if cutover.bool_value(snapshot.get("archived"), False):
        raise TransitionError(f"{repo.migration.name}: source repository is archived before transition")
    source_ci = repo.transition["source_ci"]
    cancel_active = cutover.bool_value(source_ci.get("cancel_active"), False)
    timeout_seconds = cutover.int_value(source_ci.get("shutdown_timeout_seconds"), 300, minimum=30)
    if repo.migration.source_provider == "gitlab":
        if str(snapshot.get("builds_access_level") or "") != "enabled":
            raise TransitionError(f"{repo.migration.name}: GitLab CI must be enabled before transition")
        for schedule in snapshot.get("schedules", []):
            if cutover.bool_value(schedule.get("active"), False):
                migration.api_request(
                    target,
                    "PUT",
                    f"{base}/pipeline_schedules/{schedule['id']}",
                    body={"active": False},
                )
        active = wait_for_source_ci_idle(repo, cancel_active, timeout_seconds)
        if active:
            identifiers = ", ".join(str(item.get("id")) for item in active)
            raise TransitionError(f"{repo.migration.name}: active GitLab pipelines block transition: {identifiers}")
        migration.api_request(target, "PUT", base, body={"builds_access_level": "disabled"})
    else:
        if not cutover.bool_value(snapshot.get("actions_enabled"), False):
            raise TransitionError(f"{repo.migration.name}: GitHub Actions must be enabled before transition")
        active = wait_for_source_ci_idle(repo, cancel_active, timeout_seconds)
        if active:
            identifiers = ", ".join(str(item.get("id")) for item in active)
            raise TransitionError(f"{repo.migration.name}: active GitHub Actions runs block transition: {identifiers}")
        migration.api_request(
            target,
            "PUT",
            f"{base}/actions/permissions",
            body={"enabled": False},
            expected=(204,),
        )
    verification = verify_source_authority(repo, "transition")
    return {"snapshot": snapshot, **verification}


def restore_source_ci(repo: TransitionRepo, snapshot: dict[str, Any]) -> dict[str, Any]:
    target, base = api_base(repo, "source")
    if str(snapshot.get("provider") or "") != repo.migration.source_provider:
        raise TransitionError(f"{repo.migration.name}: source CI snapshot provider mismatch")
    if repo.migration.source_provider == "gitlab":
        return cutover.restore_gitlab(repo.cutover_repo, snapshot)
    migration.api_request(
        target,
        "PUT",
        f"{base}/actions/permissions",
        body={"enabled": cutover.bool_value(snapshot.get("actions_enabled"), True)},
        expected=(204,),
    )
    current = source_ci_snapshot(repo)
    return {
        "archived": current["archived"],
        "actions_enabled": current["actions_enabled"],
        "verified": (
            current["archived"] == cutover.bool_value(snapshot.get("archived"), False)
            and current["actions_enabled"] == cutover.bool_value(snapshot.get("actions_enabled"), True)
        ),
    }


def verify_source_authority(repo: TransitionRepo, phase: str) -> dict[str, Any]:
    if phase not in {"shadow", "transition", "finalized"}:
        raise TransitionError(f"unsupported source authority phase: {phase}")
    snapshot = source_ci_snapshot(repo)
    archived = cutover.bool_value(snapshot.get("archived"), False)
    if repo.migration.source_provider == "gitlab":
        ci_enabled = str(snapshot.get("builds_access_level") or "") == "enabled"
        schedules_disabled = all(
            not cutover.bool_value(item.get("active"), False)
            for item in snapshot.get("schedules", [])
        )
    else:
        ci_enabled = cutover.bool_value(snapshot.get("actions_enabled"), False)
        schedules_disabled = not ci_enabled
    if phase == "shadow":
        verified = not archived and ci_enabled
    elif phase == "transition":
        verified = not archived and not ci_enabled and schedules_disabled
    elif phase == "finalized":
        verified = archived and not ci_enabled
    return {
        "provider": repo.migration.source_provider,
        "phase": phase,
        "repository_writable": not archived,
        "ci_enabled": ci_enabled,
        "schedules_disabled": schedules_disabled,
        "verified": verified,
    }


def freeze_source_repository(repo: TransitionRepo) -> dict[str, Any]:
    target, base = api_base(repo, "source")
    before = source_ci_snapshot(repo)
    if cutover.bool_value(before.get("archived"), False):
        raise TransitionError(f"{repo.migration.name}: source is already archived before finalization")
    if repo.migration.source_provider == "gitlab":
        migration.api_request(target, "POST", f"{base}/archive", expected=(200, 201))
    else:
        migration.api_request(target, "PATCH", base, body={"archived": True})
    verification = verify_source_authority(repo, "finalized")
    return {"before": before, **verification}


def restore_source_repository(repo: TransitionRepo, snapshot: dict[str, Any]) -> dict[str, Any]:
    target, base = api_base(repo, "source")
    if repo.migration.source_provider == "gitlab":
        project = migration.api_request(target, "GET", base)
        if cutover.bool_value(project.get("archived"), False):
            migration.api_request(target, "POST", f"{base}/unarchive", expected=(200, 201))
    else:
        migration.api_request(
            target,
            "PATCH",
            base,
            body={"archived": cutover.bool_value(snapshot.get("archived"), False)},
        )
    return verify_source_authority(repo, "transition")


def set_destination_authority(plan: TransitionPlan, repo: TransitionRepo, enabled: bool) -> dict[str, Any]:
    return cutover.set_destination_authority(
        cutover_plan_view(plan),
        repo.cutover_repo,
        enabled,
    )


def verify_operational_repository(
    plan: TransitionPlan,
    repo: TransitionRepo,
    phase: str,
) -> dict[str, Any]:
    shared_plan = cutover_plan_view(plan)
    inventory = discover_repository(repo, verify_destination=True)
    migration_result = migration.verify_repo(repo.migration)
    woodpecker_config = plan.services["woodpecker"]
    woodpecker = cutover.service_target("woodpecker", woodpecker_config)
    destination, destination_base = api_base(repo, "destination")
    destination_payload = migration.api_request(destination, "GET", destination_base)
    full_name = str(destination_payload.get("full_name") or repo.migration.destination_api_repository or "")
    wp_repo = cutover.woodpecker_lookup(woodpecker, full_name)
    assert wp_repo is not None
    repo_id = int(wp_repo.get("id") or 0)
    agents = cutover.service_request(woodpecker, "GET", "api/agents")
    if not isinstance(agents, list):
        raise TransitionError("Woodpecker agent list returned invalid data")
    authority_phase = "post-cutover" if phase in {"transition", "finalized"} else "shadow"
    woodpecker_configuration = cutover.verify_woodpecker_configuration(
        shared_plan,
        repo.cutover_repo,
        woodpecker,
        repo_id,
        authority_phase,
    )
    runners = cutover.verify_runner_capabilities(repo.cutover_repo, agents)
    protections = cutover.verify_destination_protections(repo.cutover_repo)
    integrations = cutover.verify_destination_integrations(repo.cutover_repo, woodpecker.api_url)
    access = verify_destination_access(repo, "finalized" if phase == "finalized" else "shadow")
    harbor = cutover.verify_harbor_canary(shared_plan)
    argocd = cutover.verify_argocd(shared_plan)
    source_phase = "shadow" if phase == "rolled-back" else phase
    source = verify_source_authority(repo, source_phase)
    verified = all(
        (
            inventory.get("verified"),
            migration_result.get("verified"),
            woodpecker_configuration.get("verified"),
            runners.get("verified"),
            protections.get("verified"),
            integrations.get("verified"),
            access.get("verified"),
            harbor.get("verified"),
            argocd.get("verified"),
            source.get("verified"),
        )
    )
    return {
        "name": repo.migration.name,
        "phase": phase,
        "inventory": inventory,
        "migration": migration_result,
        "source_authority": source,
        "woodpecker_configuration": woodpecker_configuration,
        "runner_capabilities": runners,
        "protections": protections,
        "integrations": integrations,
        "destination_access": access,
        "harbor": harbor,
        "argocd": argocd,
        "verified": verified,
    }


def inventory_matches(approved: dict[str, Any], current: dict[str, Any]) -> bool:
    return canonical_digest(sanitize(current)) == canonical_digest(approved)


def source_inventory_view(inventory: dict[str, Any]) -> dict[str, Any]:
    pipelines = inventory.get("pipelines") or {}
    pipeline_mappings = []
    for mapping_item in pipelines.get("mappings", []):
        pipeline_mappings.append(
            {
                key: copy.deepcopy(value)
                for key, value in mapping_item.items()
                if key not in {"missing_destinations", "deployment_gate_checks", "verified"}
            }
        )
    return sanitize(
        {
            "name": inventory.get("name"),
            "source_url": inventory.get("source_url"),
            "source_state": inventory.get("source_state"),
            "pipelines": {
                "source_files": pipelines.get("source_files", []),
                "mappings": pipeline_mappings,
                "unaccounted_source_files": pipelines.get("unaccounted_source_files", []),
                "ambiguous_source_files": pipelines.get("ambiguous_source_files", []),
                "unresolved_local_includes": pipelines.get("unresolved_local_includes", []),
                "external_includes": pipelines.get("external_includes", {}),
                "deployment_gate_marker": pipelines.get("deployment_gate_marker"),
            },
            "variables": inventory.get("variables"),
            "schedules": inventory.get("schedules"),
            "runner_tags": inventory.get("runner_tags"),
            "protections": inventory.get("protections"),
            "integrations": inventory.get("integrations"),
        }
    )


def relay_age_seconds(state: dict[str, Any], repository_name: str) -> float | None:
    repository = state.get("repositories", {}).get(repository_name, {})
    generated = str(repository.get("synced_at") or "")
    if not generated:
        return None
    try:
        return max(
            0.0,
            (datetime.now(timezone.utc) - datetime.fromisoformat(generated.replace("Z", "+00:00"))).total_seconds(),
        )
    except ValueError:
        return None


def rollback_transition_state(
    plan: TransitionPlan,
    state: dict[str, Any],
    stop_relay: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for repo in reversed(plan.repositories):
        errors: list[str] = []
        result: dict[str, Any] = {"name": repo.migration.name}
        try:
            result["destination_authority"] = set_destination_authority(plan, repo, False)
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["destination_access"] = set_destination_access(repo, "shadow")
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        snapshot = state.get("source_ci_snapshots", {}).get(repo.migration.name)
        try:
            result["source_ci"] = (
                restore_source_ci(repo, snapshot)
                if isinstance(snapshot, dict)
                else {"skipped": True, "verified": True}
            )
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["relay"] = set_native_relay_enabled(repo, not stop_relay)
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        result["errors"] = errors
        result["verified"] = not errors and all(
            child.get("verified", True)
            for child in (
                result.get("destination_authority", {}),
                result.get("destination_access", {}),
                result.get("source_ci", {}),
                result.get("relay", {}),
            )
        )
        results.append(result)
    next_state = copy.deepcopy(state)
    rollback_ok = bool(results) and all(item.get("verified") for item in results)
    next_state["phase"] = ("rolled-back" if stop_relay else "shadow") if rollback_ok else "rollback-failed"
    next_state["destination_authority_enabled"] = False
    next_state["last_rollback"] = sanitize(results)
    if rollback_ok:
        next_state["source_ci_snapshots"] = {}
        next_state["consecutive_failures"] = 0
    return results, next_state


def normalized_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit((parsed.scheme.lower(), f"{host}{port}", path, "", ""))


def credentialed_url(value: str, username: str, password: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TransitionError("password-authenticated mirrors require an HTTP(S) destination URL")
    cutover.register_secret(username)
    cutover.register_secret(password)
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit((parsed.scheme, f"{userinfo}{host}", parsed.path, parsed.query, parsed.fragment))


def find_gitlab_push_mirror(repo: TransitionRepo) -> dict[str, Any] | None:
    target, base = api_base(repo, "source")
    desired = normalized_repository_url(repo.migration.destination_url)
    mirrors = cutover.gitlab_list(target, f"{base}/remote_mirrors")
    matching = [item for item in mirrors if normalized_repository_url(str(item.get("url") or "")) == desired]
    if len(matching) > 1:
        raise TransitionError(f"{repo.migration.name}: multiple GitLab push mirrors target Forgejo")
    return matching[0] if matching else None


def ensure_forgejo_deploy_key(repo: TransitionRepo, title: str, public_key: str) -> dict[str, Any]:
    target, base = api_base(repo, "destination")
    keys = paged_list(target, f"{base}/keys")
    for key in keys:
        if str(key.get("key") or "").strip() == public_key.strip():
            if cutover.bool_value(key.get("read_only"), True):
                raise TransitionError(f"{repo.migration.name}: existing mirror deploy key is read-only")
            return {"id": key.get("id"), "title": key.get("title"), "status": "existing", "verified": True}
    payload = migration.api_request(
        target,
        "POST",
        f"{base}/keys",
        body={"title": title, "key": public_key, "read_only": False},
        expected=(201,),
    )
    return {"id": payload.get("id"), "title": payload.get("title") or title, "status": "created", "verified": bool(payload.get("id"))}


def ensure_gitlab_push_mirror(repo: TransitionRepo) -> dict[str, Any]:
    relay = repo.transition["relay"]
    target, base = api_base(repo, "source")
    existing = find_gitlab_push_mirror(repo)
    if str(relay["mode"]).lower() == "mapped":
        verified = bool(existing) and cutover.bool_value(existing.get("enabled"), False) and not str(existing.get("last_error") or "")
        return {"driver": "gitlab-push", "mode": "mapped", "mirror_id": existing.get("id") if existing else None, "status": existing.get("update_status") if existing else "missing", "verified": verified}

    auth_method = str(relay.get("auth_method") or "password").lower()
    body: dict[str, Any] = {
        "url": repo.migration.destination_url,
        "enabled": True,
        "auth_method": auth_method,
        "only_protected_branches": False,
        "keep_divergent_refs": False,
    }
    if relay.get("mirror_branch_regex"):
        body["mirror_branch_regex"] = str(relay["mirror_branch_regex"])
    if auth_method == "password":
        username = os.environ[str(relay["username_env"])]
        body["url"] = credentialed_url(
            repo.migration.destination_url,
            username,
            os.environ[str(relay["password_env"])],
        )
    else:
        body["host_keys"] = [str(item) for item in relay.get("host_keys", [])]

    previous = None
    if existing:
        previous = {
            "enabled": cutover.bool_value(existing.get("enabled"), False),
            "only_protected_branches": cutover.bool_value(existing.get("only_protected_branches"), False),
            "keep_divergent_refs": cutover.bool_value(existing.get("keep_divergent_refs"), False),
        }
        mirror_id = int(existing["id"])
        payload = migration.api_request(target, "PUT", f"{base}/remote_mirrors/{mirror_id}", body=body)
        action = "updated"
    else:
        payload = migration.api_request(target, "POST", f"{base}/remote_mirrors", body=body, expected=(201,))
        mirror_id = int(payload.get("id") or 0)
        action = "created"
    if mirror_id <= 0:
        raise TransitionError(f"{repo.migration.name}: GitLab did not return a remote mirror ID")

    deploy_key: dict[str, Any] | None = None
    if auth_method == "ssh_public_key":
        key_payload = migration.api_request(target, "GET", f"{base}/remote_mirrors/{mirror_id}/public_key")
        public_key = str(key_payload.get("public_key") or "").strip()
        if not public_key:
            raise TransitionError(f"{repo.migration.name}: GitLab mirror public key is missing")
        deploy_key = ensure_forgejo_deploy_key(repo, f"platform-transition-{repo.migration.name}", public_key)

    migration.api_request(target, "POST", f"{base}/remote_mirrors/{mirror_id}/sync", expected=(204,))
    current = migration.api_request(target, "GET", f"{base}/remote_mirrors/{mirror_id}")
    verified = cutover.bool_value(current.get("enabled"), False) and not str(current.get("last_error") or "")
    return {
        "driver": "gitlab-push",
        "mode": "managed",
        "mirror_id": mirror_id,
        "action": action,
        "created": action == "created",
        "previous": previous,
        "auth_method": auth_method,
        "deploy_key": deploy_key,
        "update_status": current.get("update_status"),
        "last_successful_update_at": current.get("last_successful_update_at"),
        "last_error": current.get("last_error"),
        "verified": verified and (deploy_key is None or deploy_key.get("verified") is True),
    }


def force_gitlab_push_mirror(repo: TransitionRepo) -> dict[str, Any]:
    target, base = api_base(repo, "source")
    mirror = find_gitlab_push_mirror(repo)
    if not mirror:
        raise TransitionError(f"{repo.migration.name}: managed GitLab push mirror is missing")
    mirror_id = int(mirror["id"])
    migration.api_request(target, "POST", f"{base}/remote_mirrors/{mirror_id}/sync", expected=(204,))
    current = migration.api_request(target, "GET", f"{base}/remote_mirrors/{mirror_id}")
    return {
        "mirror_id": mirror_id,
        "enabled": cutover.bool_value(current.get("enabled"), False),
        "update_status": current.get("update_status"),
        "last_successful_update_at": current.get("last_successful_update_at"),
        "last_error": current.get("last_error"),
        "verified": cutover.bool_value(current.get("enabled"), False) and not str(current.get("last_error") or ""),
    }


def set_native_relay_enabled(repo: TransitionRepo, enabled: bool) -> dict[str, Any]:
    if str(repo.transition["relay"]["driver"]) != "gitlab-push":
        return {"driver": "external", "enabled": enabled, "managed_externally": True, "verified": True}
    target, base = api_base(repo, "source")
    mirror = find_gitlab_push_mirror(repo)
    if not mirror:
        raise TransitionError(f"{repo.migration.name}: GitLab push mirror is missing")
    mirror_id = int(mirror["id"])
    migration.api_request(
        target,
        "PUT",
        f"{base}/remote_mirrors/{mirror_id}",
        body={"enabled": enabled},
    )
    current = migration.api_request(target, "GET", f"{base}/remote_mirrors/{mirror_id}")
    actual = cutover.bool_value(current.get("enabled"), False)
    return {
        "driver": "gitlab-push",
        "mirror_id": mirror_id,
        "enabled": actual,
        "last_error": current.get("last_error"),
        "verified": actual == enabled,
    }


def sync_git_data(repo: TransitionRepo, work_dir: Path) -> dict[str, Any]:
    mirror = migration.prepare_mirror(repo.migration, work_dir)
    lfs_transfer = migration.migrate_lfs(repo.migration, mirror)
    migration.push_mirror(mirror, repo.migration.destination_url)
    destination = migration.verify_destination_repository(repo.migration)
    destination = migration.reconcile_destination_default_branch(repo.migration, destination)
    refs = migration.compare_refs(repo.migration.source_url, repo.migration.destination_url)
    if not refs.get("verified"):
        raise TransitionError(f"{repo.migration.name}: repository refs differ after relay sync")
    lfs = {**lfs_transfer, **migration.verify_lfs(repo.migration, work_dir, source_mirror=mirror)}
    wiki = migration.migrate_wiki(repo.migration, work_dir)
    metadata = migration.migrate_metadata(repo.migration)
    verified = all(
        (
            destination.get("verified"),
            refs.get("verified"),
            lfs.get("verified"),
            wiki.get("verified"),
            metadata.get("verified"),
        )
    )
    return {
        "destination_repository": destination,
        "git": refs,
        "lfs": lfs,
        "wiki": wiki,
        "metadata": metadata,
        "verified": bool(verified),
    }


def reconcile_repository(repo: TransitionRepo, work_dir: Path) -> dict[str, Any]:
    driver = str(repo.transition["relay"]["driver"])
    native: dict[str, Any] | None = None
    if driver == "gitlab-push":
        native = force_gitlab_push_mirror(repo)
    git_data = sync_git_data(repo, work_dir)
    return {
        "name": repo.migration.name,
        "driver": driver,
        "native": native,
        "git_data": git_data,
        "synced_at": utc_now(),
        "verified": git_data.get("verified") and (native is None or native.get("verified")),
    }


def initial_state(plan: TransitionPlan) -> dict[str, Any]:
    return {
        "version": 1,
        "tool": TOOL,
        "plan_sha256": plan.sha256,
        "phase": "planned",
        "updated_at": utc_now(),
        "consecutive_failures": 0,
        "repositories": {},
        "source_ci_snapshots": {},
        "finalize_snapshots": {},
        "relay_configuration": {},
        "destination_authority_enabled": False,
    }


def load_state(path: Path, plan: TransitionPlan, allow_missing: bool = False) -> dict[str, Any]:
    if not path.exists():
        if allow_missing:
            return initial_state(plan)
        raise TransitionError(f"transition state does not exist: {path}")
    state = migration.load_plan(path)
    if state.get("tool") != TOOL or state.get("plan_sha256") != plan.sha256:
        raise TransitionError(f"{path}: state belongs to a different transition plan")
    claimed_digest = str(state.get("state_sha256") or "")
    digest_payload = {key: value for key, value in state.items() if key != "state_sha256"}
    if not claimed_digest or claimed_digest != canonical_digest(digest_payload):
        raise TransitionError(f"{path}: state integrity verification failed")
    if state.get("phase") not in TRANSITION_PHASES:
        raise TransitionError(f"{path}: invalid transition phase {state.get('phase')!r}")
    if not isinstance(state.get("repositories"), dict):
        raise TransitionError(f"{path}: repositories state must be an object")
    return state


def write_state(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    safe = sanitize(state)
    safe["updated_at"] = utc_now()
    safe.pop("state_sha256", None)
    safe["state_sha256"] = canonical_digest(safe)
    text = json.dumps(safe, indent=2, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise TransitionError(f"could not write transition state {path}: {exc}") from exc
    return safe


class StateLock:
    def __init__(self, state_path: Path) -> None:
        self.path = state_path.with_name(f"{state_path.name}.lock")
        self.fd: int | None = None

    def __enter__(self) -> StateLock:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.fd, f"pid={os.getpid()} created_at={utc_now()}\n".encode("utf-8"))
        except FileExistsError as exc:
            raise TransitionError(f"another relay process owns {self.path}; remove it only after proving that process is stopped") from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def command_prepare(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    discovery = load_proof(args.discovery, plan, ("discover",))
    require_confirmation(plan, discovery, "prepare_confirmation_env")
    with StateLock(args.state):
        state = load_state(args.state, plan, allow_missing=True)
        if state["phase"] not in {"planned", "rolled-back"}:
            raise TransitionError(f"prepare requires planned or rolled-back state, found {state['phase']}")
        repositories: list[dict[str, Any]] = []
        relay_configuration: dict[str, Any] = {}
        for repo in plan.repositories:
            approved = cutover.find_proof_repo(discovery, repo.migration.name)
            try:
                current = discover_repository(repo)
                source_authority = verify_source_authority(repo, "shadow")
                unchanged = canonical_digest(source_inventory_view(current)) == canonical_digest(
                    source_inventory_view(approved)
                )
                if not current.get("verified") or not source_authority.get("verified") or not unchanged:
                    raise TransitionError(
                        f"{repo.migration.name}: approved source inventory changed or source CI is not enabled"
                    )
                prepared = prepare_transition_repository(plan, repo)
                relay = (
                    ensure_gitlab_push_mirror(repo)
                    if str(repo.transition["relay"]["driver"]) == "gitlab-push"
                    else {"driver": "external", "mode": repo.transition["relay"]["mode"], "verified": True}
                )
                relay_configuration[repo.migration.name] = relay
                prepared["relay"] = relay
                prepared["source_authority"] = source_authority
                prepared["source_inventory"] = source_inventory_view(current)
                prepared["approved_inventory_sha256"] = canonical_digest(source_inventory_view(approved))
                prepared["verified"] = bool(prepared.get("verified") and relay.get("verified"))
                repositories.append(prepared)
            except migration.MigrationError as exc:
                repositories.append(
                    {
                        "name": repo.migration.name,
                        "error": cutover.redact_text(str(exc)),
                        "verified": False,
                    }
                )
        proof = proof_base(plan, "prepare", repositories)
        proof["discovery_proof_sha256"] = discovery.get("proof_sha256")
        proof["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
        final_proof = write_proof(args.proof, proof)
        if not final_proof.get("verified"):
            for repo in plan.repositories:
                if repo.migration.name in relay_configuration:
                    try:
                        set_native_relay_enabled(repo, False)
                    except migration.MigrationError:
                        pass
            return 1
        next_state = initial_state(plan)
        next_state["phase"] = "shadow"
        next_state["prepared_proof_sha256"] = final_proof["proof_sha256"]
        next_state["relay_configuration"] = sanitize(relay_configuration)
        next_state["repositories"] = {
            item["name"]: {
                "synced_at": utc_now(),
                "prepared": True,
                "verified": bool(item.get("verified")),
            }
            for item in repositories
        }
        write_state(args.state, next_state)
        return 0


def command_verify_shadow(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    prepared = load_proof(args.prepared, plan, ("prepare",))
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] != "shadow":
            raise TransitionError(f"shadow verification requires shadow state, found {state['phase']}")
        if state.get("prepared_proof_sha256") != prepared.get("proof_sha256"):
            raise TransitionError("prepared proof does not match durable transition state")
        work_dir = args.work_dir
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="forge-transition-shadow-")
            work_dir = Path(temporary.name)
        work_dir.mkdir(parents=True, exist_ok=True)
        relay_results, next_state = reconcile_plan(plan, state, work_dir)
        relay_by_name = {str(item.get("name")): item for item in relay_results}
        repositories: list[dict[str, Any]] = []
        for repo in plan.repositories:
            prepared_repo = cutover.find_proof_repo(prepared, repo.migration.name)
            relay = relay_by_name.get(repo.migration.name, {"verified": False})
            try:
                verification = verify_transition_repository(plan, repo, prepared_repo, "shadow")
                current_source = source_inventory_view(verification["inventory"])
                unchanged = canonical_digest(current_source) == canonical_digest(
                    prepared_repo.get("source_inventory") or {}
                )
                source_authority = verify_source_authority(repo, "shadow")
                repositories.append(
                    {
                        "name": repo.migration.name,
                        "relay": relay,
                        "inventory": verification["inventory"],
                        "verification": verification,
                        "source_inventory_unchanged": unchanged,
                        "source_authority": source_authority,
                        "verified": bool(
                            relay.get("verified")
                            and verification.get("verified")
                            and unchanged
                            and source_authority.get("verified")
                        ),
                    }
                )
            except migration.MigrationError as exc:
                repositories.append(
                    {
                        "name": repo.migration.name,
                        "relay": relay,
                        "error": cutover.redact_text(str(exc)),
                        "verified": False,
                    }
                )
        if temporary:
            temporary.cleanup()
        proof = proof_base(plan, "verify-shadow", repositories)
        proof["prepare_proof_sha256"] = prepared.get("proof_sha256")
        final_proof = write_proof(args.proof, proof)
        if final_proof.get("verified"):
            next_state["last_shadow_proof_sha256"] = final_proof["proof_sha256"]
            next_state["last_shadow_verified_at"] = utc_now()
        write_state(args.state, next_state)
        return 0 if final_proof.get("verified") else 1


def command_enter(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    shadow = load_proof(args.verification, plan, ("verify-shadow",))
    require_confirmation(plan, shadow, "enter_confirmation_env")
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] != "shadow":
            raise TransitionError(f"enter requires shadow state, found {state['phase']}")
        if state.get("last_shadow_proof_sha256") != shadow.get("proof_sha256"):
            raise TransitionError("shadow verification proof does not match durable transition state")
        work_dir = args.work_dir
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="forge-transition-enter-")
            work_dir = Path(temporary.name)
        work_dir.mkdir(parents=True, exist_ok=True)
        repositories: list[dict[str, Any]] = []
        next_state = copy.deepcopy(state)
        try:
            for repo in plan.repositories:
                approved = cutover.find_proof_repo(shadow, repo.migration.name)
                current = discover_repository(repo, verify_destination=True)
                if not current.get("verified"):
                    raise TransitionError(f"{repo.migration.name}: current source/destination contract is not verified")
                if canonical_digest(source_inventory_view(current)) != canonical_digest(
                    source_inventory_view(approved.get("inventory") or {})
                ):
                    raise TransitionError(
                        f"{repo.migration.name}: source CI/CD inventory changed after shadow verification"
                    )
                if not verify_source_authority(repo, "shadow").get("verified"):
                    raise TransitionError(f"{repo.migration.name}: source CI is not ready for handover")
            relay_results, next_state = reconcile_plan(plan, next_state, work_dir)
            if not all(item.get("verified") for item in relay_results):
                raise TransitionError("final pre-handover relay reconciliation failed")

            snapshots = {repo.migration.name: source_ci_snapshot(repo) for repo in plan.repositories}
            next_state["source_ci_snapshots"] = sanitize(snapshots)
            next_state["handover_checkpoint"] = "source-ci-snapshots-captured"
            write_state(args.state, next_state)

            disabled: dict[str, dict[str, Any]] = {}
            for repo in plan.repositories:
                disabled[repo.migration.name] = disable_source_ci(repo, snapshots[repo.migration.name])
                next_state["handover_checkpoint"] = f"source-ci-disabled:{repo.migration.name}"
                write_state(args.state, next_state)

            authorities: dict[str, dict[str, Any]] = {}
            for repo in plan.repositories:
                authorities[repo.migration.name] = set_destination_authority(plan, repo, True)
                if not authorities[repo.migration.name].get("verified"):
                    raise TransitionError(f"{repo.migration.name}: destination authority did not enable")
                next_state["handover_checkpoint"] = f"destination-authority-enabled:{repo.migration.name}"
                write_state(args.state, next_state)

            relay_by_name = {str(item.get("name")): item for item in relay_results}
            for repo in plan.repositories:
                verification = verify_transition_repository(
                    plan,
                    repo,
                    cutover.find_proof_repo(shadow, repo.migration.name),
                    "transition",
                )
                source_authority = verify_source_authority(repo, "transition")
                item = {
                    "name": repo.migration.name,
                    "relay": relay_by_name[repo.migration.name],
                    "source_before": snapshots[repo.migration.name],
                    "source_ci": disabled[repo.migration.name],
                    "destination_authority": authorities[repo.migration.name],
                    "verification": verification,
                    "source_authority": source_authority,
                    "verified": bool(
                        relay_by_name[repo.migration.name].get("verified")
                        and disabled[repo.migration.name].get("verified")
                        and authorities[repo.migration.name].get("verified")
                        and verification.get("verified")
                        and source_authority.get("verified")
                    ),
                }
                repositories.append(item)
                if not item["verified"]:
                    raise TransitionError(f"{repo.migration.name}: post-handover verification failed")
        except Exception as exc:
            rollback_results, rollback_state = rollback_transition_state(plan, next_state, stop_relay=False)
            rollback_state["handover_checkpoint"] = "automatic-rollback-complete" if all(
                item.get("verified") for item in rollback_results
            ) else "automatic-rollback-incomplete"
            write_state(args.state, rollback_state)
            failure = proof_base(
                plan,
                "enter",
                repositories or [{"name": "handover", "verified": False}],
            )
            failure["verified"] = False
            failure["error"] = cutover.redact_text(str(exc))
            failure["automatic_rollback"] = rollback_results
            failure["shadow_proof_sha256"] = shadow.get("proof_sha256")
            failure["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
            write_proof(args.proof, failure)
            if temporary:
                temporary.cleanup()
            return 1
        if temporary:
            temporary.cleanup()
        proof = proof_base(plan, "enter", repositories)
        proof["shadow_proof_sha256"] = shadow.get("proof_sha256")
        proof["source_authority"] = f"{plan.source_provider}-writable-ci-disabled"
        proof["destination_authority"] = "forgejo-woodpecker-argocd"
        proof["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
        final_proof = write_proof(args.proof, proof)
        next_state["phase"] = "transition"
        next_state["destination_authority_enabled"] = True
        next_state["handover_checkpoint"] = "completed"
        next_state["enter_proof_sha256"] = final_proof["proof_sha256"]
        write_state(args.state, next_state)
        return 0


def command_status(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    state = load_state(args.state, plan)
    if state["phase"] not in {"shadow", "transition", "finalized", "rolled-back"}:
        raise TransitionError(f"status verification is not available for phase {state['phase']}")
    repositories: list[dict[str, Any]] = []
    for repo in plan.repositories:
        try:
            operational = verify_operational_repository(plan, repo, state["phase"])
            age = relay_age_seconds(state, repo.migration.name)
            max_lag = cutover.int_value(
                repo.transition["relay"].get("max_lag_seconds"),
                300,
                minimum=30,
            )
            lag_verified = state["phase"] in {"finalized", "rolled-back"} or (
                age is not None and age <= max_lag
            )
            repositories.append(
                {
                    "name": repo.migration.name,
                    "phase": state["phase"],
                    "operational": operational,
                    "relay_age_seconds": int(age) if age is not None else None,
                    "relay_max_lag_seconds": max_lag,
                    "relay_lag_verified": lag_verified,
                    "verified": bool(operational.get("verified") and lag_verified),
                }
            )
        except migration.MigrationError as exc:
            repositories.append(
                {
                    "name": repo.migration.name,
                    "error": cutover.redact_text(str(exc)),
                    "verified": False,
                }
            )
    proof = proof_base(plan, "status", repositories)
    proof["phase"] = state["phase"]
    proof["state_sha256"] = state.get("state_sha256")
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


def command_rollback(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    evidence = load_proof(
        args.evidence,
        plan,
        ("enter", "status", "reconcile", "automatic-rollback"),
        require_verified=False,
    )
    require_confirmation(plan, evidence, "rollback_confirmation_env")
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] not in {"transition", "rollback-failed", "shadow"}:
            raise TransitionError(f"rollback is not available for phase {state['phase']}")
        repositories, next_state = rollback_transition_state(plan, state, stop_relay=True)
        proof = proof_base(plan, "rollback", repositories)
        proof["evidence_proof_sha256"] = evidence.get("proof_sha256")
        proof["source_authority"] = f"{plan.source_provider}-restored"
        proof["destination_authority"] = "woodpecker-shadow"
        proof["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
        final_proof = write_proof(args.proof, proof)
        next_state["rollback_proof_sha256"] = final_proof["proof_sha256"]
        write_state(args.state, next_state)
        return 0 if final_proof.get("verified") else 1


def command_fallback(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    evidence = load_proof(
        args.evidence,
        plan,
        ("enter", "status", "reconcile", "automatic-rollback"),
        require_verified=False,
    )
    require_confirmation(plan, evidence, "fallback_confirmation_env")
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] not in {"transition", "rollback-failed", "shadow"}:
            raise TransitionError(f"fallback is not available for phase {state['phase']}")
        repositories, next_state = rollback_transition_state(plan, state, stop_relay=False)
        proof = proof_base(plan, "fallback", repositories)
        proof["evidence_proof_sha256"] = evidence.get("proof_sha256")
        proof["source_authority"] = f"{plan.source_provider}-restored"
        proof["destination_authority"] = "woodpecker-shadow"
        proof["relay"] = "running"
        proof["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
        final_proof = write_proof(args.proof, proof)
        next_state["fallback_proof_sha256"] = final_proof["proof_sha256"]
        write_state(args.state, next_state)
        return 0 if final_proof.get("verified") else 1


def reverse_migration_plan(repo: TransitionRepo) -> migration.RepoPlan:
    original = repo.migration
    metadata = copy.deepcopy(original.metadata)
    if original.source_provider == "gitlab":
        metadata["pull_requests"] = metadata.get("merge_requests", "skip")
    metadata["merge_requests"] = "skip"
    return migration.RepoPlan(
        name=f"{original.name}-failback",
        source_url=original.destination_url,
        destination_url=original.source_url,
        source_wiki_url=original.destination_wiki_url,
        destination_wiki_url=original.source_wiki_url,
        source_provider=original.destination_provider,
        destination_provider=original.source_provider,
        source_api_url=original.destination_api_url,
        destination_api_url=original.source_api_url,
        source_api_repository=original.destination_api_repository,
        destination_api_repository=original.source_api_repository,
        source_token_env=original.destination_token_env,
        destination_token_env=original.source_token_env,
        destination_create="false",
        destination_private=original.destination_private,
        destination_description="",
        destination_namespace_id=None,
        wiki=original.wiki,
        lfs=original.lfs,
        metadata=metadata,
    )


def reverse_sync_repository(repo: TransitionRepo, work_dir: Path) -> dict[str, Any]:
    reverse = reverse_migration_plan(repo)
    result = migration.migrate_repo(reverse, work_dir)
    return {
        "name": repo.migration.name,
        "direction": f"forgejo-to-{repo.migration.source_provider}",
        "source_url": migration.redact_url(reverse.source_url),
        "destination_url": migration.redact_url(reverse.destination_url),
        "result": result,
        "verified": result.get("verified") is True,
    }


def restore_finalized_after_failback_failure(
    plan: TransitionPlan,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for repo in reversed(plan.repositories):
        result: dict[str, Any] = {"name": repo.migration.name}
        errors: list[str] = []
        disabled_snapshot = state.get("finalize_snapshots", {}).get(repo.migration.name)
        try:
            if not isinstance(disabled_snapshot, dict):
                raise TransitionError(
                    f"{repo.migration.name}: finalized source snapshot is missing"
                )
            current = source_ci_snapshot(repo)
            if cutover.bool_value(current.get("archived"), False):
                source = verify_source_authority(repo, "finalized")
                if not source.get("verified"):
                    restore_source_repository(repo, disabled_snapshot)
                    restore_source_ci(repo, disabled_snapshot)
                    source = freeze_source_repository(repo)
            else:
                restore_source_ci(repo, disabled_snapshot)
                source = freeze_source_repository(repo)
            result["source_authority"] = source
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["destination_access"] = set_destination_access(repo, "finalized")
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["destination_authority"] = set_destination_authority(plan, repo, True)
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["relay"] = set_native_relay_enabled(repo, False)
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        result["errors"] = errors
        result["verified"] = not errors and all(
            child.get("verified", True)
            for child in (
                result.get("source_authority", {}),
                result.get("destination_access", {}),
                result.get("destination_authority", {}),
                result.get("relay", {}),
            )
        )
        results.append(result)
    recovered = bool(results) and all(item.get("verified") for item in results)
    next_state = copy.deepcopy(state)
    next_state["phase"] = "finalized" if recovered else "rollback-failed"
    next_state["destination_authority_enabled"] = recovered
    next_state["failback_checkpoint"] = (
        "automatic-recovery-complete" if recovered else "automatic-recovery-incomplete"
    )
    next_state["last_failback_recovery"] = sanitize(results)
    return results, next_state


def command_failback(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    evidence = load_proof(args.evidence, plan, ("finalize", "status"))
    require_confirmation(plan, evidence, "failback_confirmation_env")
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] != "finalized":
            raise TransitionError(f"failback requires finalized state, found {state['phase']}")
        if evidence.get("command") == "finalize" and state.get(
            "finalize_proof_sha256"
        ) != evidence.get("proof_sha256"):
            raise TransitionError("finalization evidence does not match durable transition state")
        if evidence.get("command") == "status":
            if evidence.get("phase") != "finalized":
                raise TransitionError("status evidence was not produced during finalized phase")
            if evidence.get("state_sha256") != state.get("state_sha256"):
                raise TransitionError("status evidence does not match durable transition state")

        work_dir = args.work_dir
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="forge-transition-failback-")
            work_dir = Path(temporary.name)
        work_dir.mkdir(parents=True, exist_ok=True)

        repositories: list[dict[str, Any]] = []
        next_state = copy.deepcopy(state)
        destination_authority: dict[str, dict[str, Any]] = {}
        destination_access: dict[str, dict[str, Any]] = {}
        relay_state: dict[str, dict[str, Any]] = {}
        source_unfrozen: dict[str, dict[str, Any]] = {}
        reverse_sync: dict[str, dict[str, Any]] = {}
        source_ci: dict[str, dict[str, Any]] = {}
        try:
            for repo in plan.repositories:
                if not verify_operational_repository(plan, repo, "finalized").get("verified"):
                    raise TransitionError(f"{repo.migration.name}: finalized preflight is not healthy")
                if not isinstance(
                    state.get("source_ci_snapshots", {}).get(repo.migration.name),
                    dict,
                ):
                    raise TransitionError(f"{repo.migration.name}: original source CI snapshot is missing")
                if not isinstance(
                    state.get("finalize_snapshots", {}).get(repo.migration.name),
                    dict,
                ):
                    raise TransitionError(f"{repo.migration.name}: finalized source snapshot is missing")

            next_state["failback_checkpoint"] = "preflight-complete"
            write_state(args.state, next_state)

            for repo in plan.repositories:
                destination_authority[repo.migration.name] = set_destination_authority(
                    plan,
                    repo,
                    False,
                )
                if not destination_authority[repo.migration.name].get("verified"):
                    raise TransitionError(
                        f"{repo.migration.name}: destination authority did not disable"
                    )
                next_state["failback_checkpoint"] = (
                    f"destination-authority-disabled:{repo.migration.name}"
                )
                write_state(args.state, next_state)

            for repo in plan.repositories:
                destination_access[repo.migration.name] = set_destination_access(repo, "shadow")
                relay_state[repo.migration.name] = set_native_relay_enabled(repo, False)
                if not destination_access[repo.migration.name].get("verified") or not relay_state[
                    repo.migration.name
                ].get("verified"):
                    raise TransitionError(
                        f"{repo.migration.name}: destination failback lock did not apply"
                    )

            for repo in plan.repositories:
                source_unfrozen[repo.migration.name] = restore_source_repository(
                    repo,
                    state["finalize_snapshots"][repo.migration.name],
                )
                if not source_unfrozen[repo.migration.name].get("verified"):
                    raise TransitionError(
                        f"{repo.migration.name}: source did not unarchive with CI disabled"
                    )
                next_state["failback_checkpoint"] = f"source-unfrozen:{repo.migration.name}"
                write_state(args.state, next_state)

            for repo in plan.repositories:
                reverse_sync[repo.migration.name] = reverse_sync_repository(repo, work_dir)
                if not reverse_sync[repo.migration.name].get("verified"):
                    raise TransitionError(f"{repo.migration.name}: reverse synchronization failed")
                next_state["repositories"][repo.migration.name] = sanitize(
                    reverse_sync[repo.migration.name]
                )
                next_state["failback_checkpoint"] = f"reverse-sync-verified:{repo.migration.name}"
                write_state(args.state, next_state)

            for repo in plan.repositories:
                source_ci[repo.migration.name] = restore_source_ci(
                    repo,
                    state["source_ci_snapshots"][repo.migration.name],
                )
                if not source_ci[repo.migration.name].get("verified"):
                    raise TransitionError(f"{repo.migration.name}: source CI restoration failed")
                next_state["failback_checkpoint"] = f"source-ci-restored:{repo.migration.name}"
                write_state(args.state, next_state)

            for repo in plan.repositories:
                operational = verify_operational_repository(plan, repo, "rolled-back")
                item = {
                    "name": repo.migration.name,
                    "destination_authority": destination_authority[repo.migration.name],
                    "destination_access": destination_access[repo.migration.name],
                    "relay": relay_state[repo.migration.name],
                    "source_unfrozen": source_unfrozen[repo.migration.name],
                    "reverse_sync": reverse_sync[repo.migration.name],
                    "source_ci": source_ci[repo.migration.name],
                    "operational": operational,
                    "verified": all(
                        child.get("verified")
                        for child in (
                            destination_authority[repo.migration.name],
                            destination_access[repo.migration.name],
                            relay_state[repo.migration.name],
                            source_unfrozen[repo.migration.name],
                            reverse_sync[repo.migration.name],
                            source_ci[repo.migration.name],
                            operational,
                        )
                    ),
                }
                repositories.append(item)
                if not item["verified"]:
                    raise TransitionError(f"{repo.migration.name}: failback verification failed")
        except Exception as exc:
            recovery, recovery_state = restore_finalized_after_failback_failure(
                plan,
                next_state,
            )
            write_state(args.state, recovery_state)
            failure = proof_base(
                plan,
                "failback",
                repositories or [{"name": "failback", "verified": False}],
            )
            failure["verified"] = False
            failure["error"] = cutover.redact_text(str(exc))
            failure["automatic_recovery"] = recovery
            failure["evidence_proof_sha256"] = evidence.get("proof_sha256")
            failure["change_ticket"] = os.environ.get(
                str(plan.control["change_ticket_env"]),
                "",
            )
            write_proof(args.proof, failure)
            if temporary:
                temporary.cleanup()
            return 1

        if temporary:
            temporary.cleanup()
        proof = proof_base(plan, "failback", repositories)
        proof["evidence_proof_sha256"] = evidence.get("proof_sha256")
        proof["source_authority"] = f"{plan.source_provider}-restored"
        proof["destination_authority"] = "woodpecker-disabled"
        proof["reverse_sync"] = "forgejo-to-source-verified"
        proof["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
        final_proof = write_proof(args.proof, proof)
        next_state["phase"] = "rolled-back"
        next_state["destination_authority_enabled"] = False
        next_state["failback_checkpoint"] = "completed"
        next_state["failback_proof_sha256"] = final_proof["proof_sha256"]
        write_state(args.state, next_state)
        return 0


def restore_failed_finalization(
    plan: TransitionPlan,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for repo in reversed(plan.repositories):
        errors: list[str] = []
        result: dict[str, Any] = {"name": repo.migration.name}
        snapshot = state.get("finalize_snapshots", {}).get(repo.migration.name)
        try:
            result["destination_access"] = set_destination_access(repo, "shadow")
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["relay"] = set_native_relay_enabled(repo, True)
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        try:
            result["source"] = (
                restore_source_repository(repo, snapshot)
                if isinstance(snapshot, dict)
                else {"skipped": True, "verified": True}
            )
        except Exception as exc:
            errors.append(cutover.redact_text(str(exc)))
        result["errors"] = errors
        result["verified"] = not errors and all(
            child.get("verified", True)
            for child in (
                result.get("destination_access", {}),
                result.get("relay", {}),
                result.get("source", {}),
            )
        )
        results.append(result)
    next_state = copy.deepcopy(state)
    restored = bool(results) and all(item.get("verified") for item in results)
    next_state["phase"] = "transition" if restored else "rollback-failed"
    next_state["destination_authority_enabled"] = True
    next_state["last_finalize_rollback"] = sanitize(results)
    if restored:
        next_state["finalize_snapshots"] = {}
    return results, next_state


def command_finalize(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    evidence = load_proof(args.evidence, plan, ("enter", "status"))
    require_confirmation(plan, evidence, "finalize_confirmation_env")
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] != "transition":
            raise TransitionError(f"finalize requires transition state, found {state['phase']}")
        if evidence.get("command") == "enter" and state.get("enter_proof_sha256") != evidence.get(
            "proof_sha256"
        ):
            raise TransitionError("enter evidence does not match durable transition state")
        if evidence.get("command") == "status" and evidence.get("phase") != "transition":
            raise TransitionError("status evidence was not produced during transition phase")
        work_dir = args.work_dir
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="forge-transition-finalize-")
            work_dir = Path(temporary.name)
        work_dir.mkdir(parents=True, exist_ok=True)
        repositories: list[dict[str, Any]] = []
        next_state = copy.deepcopy(state)
        try:
            for repo in plan.repositories:
                operational = verify_operational_repository(plan, repo, "transition")
                if not operational.get("verified"):
                    raise TransitionError(f"{repo.migration.name}: transition preflight is not healthy")

            finalize_snapshots = {
                repo.migration.name: source_ci_snapshot(repo)
                for repo in plan.repositories
            }
            next_state["finalize_snapshots"] = sanitize(finalize_snapshots)
            next_state["finalize_checkpoint"] = "source-snapshots-captured"
            write_state(args.state, next_state)

            frozen: dict[str, dict[str, Any]] = {}
            for repo in plan.repositories:
                frozen[repo.migration.name] = freeze_source_repository(repo)
                next_state["finalize_checkpoint"] = f"source-frozen:{repo.migration.name}"
                write_state(args.state, next_state)

            relay_results, next_state = reconcile_plan(plan, next_state, work_dir)
            if not all(item.get("verified") for item in relay_results):
                raise TransitionError("final frozen-source relay reconciliation failed")
            relay_by_name = {str(item.get("name")): item for item in relay_results}

            stopped: dict[str, dict[str, Any]] = {}
            access: dict[str, dict[str, Any]] = {}
            for repo in plan.repositories:
                stopped[repo.migration.name] = set_native_relay_enabled(repo, False)
                access[repo.migration.name] = set_destination_access(repo, "finalized")
                if not stopped[repo.migration.name].get("verified") or not access[repo.migration.name].get("verified"):
                    raise TransitionError(f"{repo.migration.name}: final authority policy did not apply")

            for repo in plan.repositories:
                verification = verify_transition_repository(plan, repo, {"name": repo.migration.name}, "finalized")
                source = verify_source_authority(repo, "finalized")
                item = {
                    "name": repo.migration.name,
                    "source_before": finalize_snapshots[repo.migration.name],
                    "source_frozen": frozen[repo.migration.name],
                    "final_relay": relay_by_name[repo.migration.name],
                    "relay_stopped": stopped[repo.migration.name],
                    "destination_access": access[repo.migration.name],
                    "verification": verification,
                    "source_authority": source,
                    "verified": bool(
                        frozen[repo.migration.name].get("verified")
                        and relay_by_name[repo.migration.name].get("verified")
                        and stopped[repo.migration.name].get("verified")
                        and access[repo.migration.name].get("verified")
                        and verification.get("verified")
                        and source.get("verified")
                    ),
                }
                repositories.append(item)
                if not item["verified"]:
                    raise TransitionError(f"{repo.migration.name}: final authority verification failed")
        except Exception as exc:
            rollback_results, rollback_state = restore_failed_finalization(plan, next_state)
            rollback_state["finalize_checkpoint"] = "automatic-rollback-complete" if all(
                item.get("verified") for item in rollback_results
            ) else "automatic-rollback-incomplete"
            write_state(args.state, rollback_state)
            failure = proof_base(
                plan,
                "finalize",
                repositories or [{"name": "finalization", "verified": False}],
            )
            failure["verified"] = False
            failure["error"] = cutover.redact_text(str(exc))
            failure["automatic_rollback"] = rollback_results
            failure["evidence_proof_sha256"] = evidence.get("proof_sha256")
            failure["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
            write_proof(args.proof, failure)
            if temporary:
                temporary.cleanup()
            return 1
        if temporary:
            temporary.cleanup()
        proof = proof_base(plan, "finalize", repositories)
        proof["evidence_proof_sha256"] = evidence.get("proof_sha256")
        proof["source_authority"] = f"{plan.source_provider}-frozen"
        proof["destination_authority"] = "forgejo-final"
        proof["change_ticket"] = os.environ.get(str(plan.control["change_ticket_env"]), "")
        final_proof = write_proof(args.proof, proof)
        next_state["phase"] = "finalized"
        next_state["destination_authority_enabled"] = True
        next_state["finalize_checkpoint"] = "completed"
        next_state["finalize_proof_sha256"] = final_proof["proof_sha256"]
        write_state(args.state, next_state)
        return 0


def command_verify_proof(args: argparse.Namespace) -> int:
    proof = migration.load_plan(args.proof_file)
    claimed = str(proof.get("proof_sha256") or "")
    actual = migration.proof_digest(proof)
    accepted = (
        proof.get("tool") == TOOL
        and bool(claimed)
        and claimed == actual
        and proof.get("verified") is True
    )
    print(
        json.dumps(
            {
                "proof_file": str(args.proof_file),
                "proof_sha256": claimed,
                "actual_sha256": actual,
                "integrity_verified": claimed == actual,
                "transition_verified": proof.get("verified") is True,
                "accepted": accepted,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if accepted else 1


def reconcile_plan(plan: TransitionPlan, state: dict[str, Any], work_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    next_state = copy.deepcopy(state)
    failure_count = 0
    for repo in plan.repositories:
        try:
            result = reconcile_repository(repo, work_dir)
        except migration.MigrationError as exc:
            result = {"name": repo.migration.name, "error": cutover.redact_text(str(exc)), "synced_at": utc_now(), "verified": False}
        repositories.append(result)
        next_state["repositories"][repo.migration.name] = sanitize(result)
        if not result.get("verified"):
            failure_count += 1
    if failure_count:
        next_state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        next_state["last_error_at"] = utc_now()
    else:
        next_state["consecutive_failures"] = 0
        next_state["last_success_at"] = utc_now()
    next_state["verified"] = failure_count == 0
    return repositories, next_state


def command_reconcile(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    with StateLock(args.state):
        state = load_state(args.state, plan)
        if state["phase"] not in {"shadow", "transition"}:
            raise TransitionError(f"relay cannot run while transition phase is {state['phase']}")
        work_dir = args.work_dir
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="forge-transition-relay-")
            work_dir = Path(temporary.name)
        work_dir.mkdir(parents=True, exist_ok=True)
        repositories, next_state = reconcile_plan(plan, state, work_dir)
        write_state(args.state, next_state)
        proof = proof_base(plan, "reconcile", repositories)
        proof["phase"] = state["phase"]
        proof["consecutive_failures"] = next_state["consecutive_failures"]
        write_proof(args.proof, proof)
        if temporary:
            temporary.cleanup()
        return 0 if proof["verified"] else 1


def command_run_relay(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    interval = args.interval or min(cutover.int_value(repo.transition["relay"].get("sync_interval_seconds"), 60, minimum=10) for repo in plan.repositories)
    while not stop:
        with StateLock(args.state):
            state = load_state(args.state, plan)
            if state["phase"] not in {"shadow", "transition"}:
                return 0
            with tempfile.TemporaryDirectory(prefix="forge-transition-relay-") as temp:
                repositories, next_state = reconcile_plan(plan, state, Path(temp))
            proof = proof_base(plan, "reconcile", repositories)
            proof["phase"] = state["phase"]
            proof["consecutive_failures"] = next_state["consecutive_failures"]
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            relay_proof = write_proof(args.proof_dir / f"relay-{timestamp}.json", proof)
            threshold = cutover.int_value(plan.control.get("relay_failure_threshold"), 3, minimum=1)
            if int(next_state["consecutive_failures"]) >= threshold:
                if state["phase"] == "transition" and cutover.bool_value(
                    plan.control.get("auto_rollback"),
                    True,
                ):
                    rollback_results, rollback_state = rollback_transition_state(
                        plan,
                        next_state,
                        stop_relay=False,
                    )
                    rollback = proof_base(plan, "automatic-rollback", rollback_results)
                    rollback["trigger_proof_sha256"] = relay_proof.get("proof_sha256")
                    rollback["trigger"] = "relay-failure-threshold"
                    rollback["threshold"] = threshold
                    rollback_proof = write_proof(
                        args.proof_dir / f"automatic-rollback-{timestamp}.json",
                        rollback,
                    )
                    rollback_state["automatic_rollback_proof_sha256"] = rollback_proof["proof_sha256"]
                    write_state(args.state, rollback_state)
                else:
                    write_state(args.state, next_state)
                return 1
            write_state(args.state, next_state)
            if args.once:
                return 0 if proof["verified"] else 1
        deadline = time.monotonic() + interval
        while not stop and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    repositories = [{"name": repo.migration.name, "source_provider": repo.migration.source_provider, "source_url": migration.redact_url(repo.migration.source_url), "destination_url": migration.redact_url(repo.migration.destination_url), "relay_driver": repo.transition["relay"]["driver"], "verified": True} for repo in plan.repositories]
    proof = proof_base(plan, "validate-plan", repositories)
    write_proof(args.proof, proof)
    return 0


def command_discover(args: argparse.Namespace) -> int:
    plan = load_transition_plan(args.plan)
    require_credentials(plan)
    repositories: list[dict[str, Any]] = []
    for repo in plan.repositories:
        try:
            repositories.append(discover_repository(repo))
        except migration.MigrationError as exc:
            repositories.append({"name": repo.migration.name, "error": cutover.redact_text(str(exc)), "verified": False})
    proof = proof_base(plan, "discover", repositories)
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-plan")
    validate.add_argument("plan", type=Path)
    validate.add_argument("--proof", type=Path)
    validate.set_defaults(func=command_validate)
    discover = sub.add_parser("discover")
    discover.add_argument("plan", type=Path)
    discover.add_argument("--proof", type=Path, required=True)
    discover.set_defaults(func=command_discover)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("plan", type=Path)
    prepare.add_argument("--discovery", type=Path, required=True)
    prepare.add_argument("--state", type=Path, required=True)
    prepare.add_argument("--proof", type=Path, required=True)
    prepare.set_defaults(func=command_prepare)
    verify_shadow = sub.add_parser("verify-shadow")
    verify_shadow.add_argument("plan", type=Path)
    verify_shadow.add_argument("--prepared", type=Path, required=True)
    verify_shadow.add_argument("--state", type=Path, required=True)
    verify_shadow.add_argument("--work-dir", type=Path)
    verify_shadow.add_argument("--proof", type=Path, required=True)
    verify_shadow.set_defaults(func=command_verify_shadow)
    enter = sub.add_parser("enter")
    enter.add_argument("plan", type=Path)
    enter.add_argument("--verification", type=Path, required=True)
    enter.add_argument("--state", type=Path, required=True)
    enter.add_argument("--work-dir", type=Path)
    enter.add_argument("--proof", type=Path, required=True)
    enter.set_defaults(func=command_enter)
    status = sub.add_parser("status")
    status.add_argument("plan", type=Path)
    status.add_argument("--state", type=Path, required=True)
    status.add_argument("--proof", type=Path, required=True)
    status.set_defaults(func=command_status)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("plan", type=Path)
    reconcile.add_argument("--state", type=Path, required=True)
    reconcile.add_argument("--work-dir", type=Path)
    reconcile.add_argument("--proof", type=Path, required=True)
    reconcile.set_defaults(func=command_reconcile)
    relay = sub.add_parser("run-relay")
    relay.add_argument("plan", type=Path)
    relay.add_argument("--state", type=Path, required=True)
    relay.add_argument("--proof-dir", type=Path, required=True)
    relay.add_argument("--interval", type=int)
    relay.add_argument("--once", action="store_true")
    relay.set_defaults(func=command_run_relay)
    fallback = sub.add_parser("fallback")
    fallback.add_argument("plan", type=Path)
    fallback.add_argument("--state", type=Path, required=True)
    fallback.add_argument("--evidence", type=Path, required=True)
    fallback.add_argument("--proof", type=Path, required=True)
    fallback.set_defaults(func=command_fallback)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("plan", type=Path)
    rollback.add_argument("--state", type=Path, required=True)
    rollback.add_argument("--evidence", type=Path, required=True)
    rollback.add_argument("--proof", type=Path, required=True)
    rollback.set_defaults(func=command_rollback)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("plan", type=Path)
    finalize.add_argument("--state", type=Path, required=True)
    finalize.add_argument("--evidence", type=Path, required=True)
    finalize.add_argument("--work-dir", type=Path)
    finalize.add_argument("--proof", type=Path, required=True)
    finalize.set_defaults(func=command_finalize)
    failback = sub.add_parser("failback")
    failback.add_argument("plan", type=Path)
    failback.add_argument("--state", type=Path, required=True)
    failback.add_argument("--evidence", type=Path, required=True)
    failback.add_argument("--work-dir", type=Path)
    failback.add_argument("--proof", type=Path, required=True)
    failback.set_defaults(func=command_failback)
    verify_proof = sub.add_parser("verify-proof")
    verify_proof.add_argument("proof_file", type=Path)
    verify_proof.set_defaults(func=command_verify_proof)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return int(args.func(args))
    except (TransitionError, migration.MigrationError) as exc:
        print(f"forge transition failed: {cutover.redact_text(str(exc))}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
