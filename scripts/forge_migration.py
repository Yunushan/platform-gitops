#!/usr/bin/env python3
"""Mirror and verify Git forge migrations with machine-readable proof.

The supported migration planes are Git refs, repository labels, milestones,
portable releases, issues/comments, open or closed same-repository pull or
merge requests, and a fail-closed branch-protection subset: branches, tags,
optional wiki/LFS repositories, and provider-common metadata. Other provider
metadata such as packages and release assets is intentionally modeled in the
plan and rejected when marked required until an importer for that surface
exists. That keeps "verified migration" claims honest.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from http_transport import (
    HttpTransportPolicyError,
    http_timeout_seconds,
    open_http_request,
    read_bounded_response,
)
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


SUPPORTED_DIRECTIONS = {
    "github-to-forgejo",
    "gitlab-to-forgejo",
    "forgejo-to-github",
    "forgejo-to-gitlab",
}
METADATA_VERIFICATION_ATTEMPTS = 6
METADATA_VERIFICATION_INITIAL_DELAY_SECONDS = 0.25
OPTIONAL_DIRECTIONS = {
    "forgejo-to-forgejo",
    "github-to-gitlab",
    "gitlab-to-github",
}
SUPPORTED_METADATA_STATES = {"skip", "skipped", "false", "none", "not-required"}
SUPPORTED_METADATA_SURFACES = {
    "branch_protection",
    "labels",
    "milestones",
    "releases",
    "issues",
    "pull_requests",
    "merge_requests",
}
UNSUPPORTED_METADATA_SURFACES = {
    "release_assets",
    "packages",
    "project_boards",
    "wikis_metadata",
    "users",
    "teams",
    "permissions",
    "webhooks",
}
SENSITIVE_LITERAL_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "password",
    "private_token",
    "secret",
    "token",
    "value",
}
MIGRATION_COMMAND_TIMEOUT_SECONDS = 7_200
BRANCH_PROTECTION_OPTION_KEYS = {
    "mode",
    "branches",
    "gitlab_maintainer_team",
}
BRANCH_PROTECTION_LIST_FIELDS = {
    "approvals_whitelist_teams",
    "approvals_whitelist_username",
    "merge_whitelist_teams",
    "merge_whitelist_usernames",
    "push_whitelist_teams",
    "push_whitelist_usernames",
    "status_check_contexts",
}
BRANCH_PROTECTION_BOOL_FIELDS = {
    "apply_to_admins",
    "block_on_official_review_requests",
    "block_on_outdated_branch",
    "block_on_rejected_reviews",
    "dismiss_stale_approvals",
    "enable_approvals_whitelist",
    "enable_merge_whitelist",
    "enable_push",
    "enable_push_whitelist",
    "enable_status_check",
    "ignore_stale_approvals",
    "push_whitelist_deploy_keys",
    "require_signed_commits",
}


class MigrationError(RuntimeError):
    """Raised when a migration or verification step cannot be proven."""


@dataclass(frozen=True)
class RepoPlan:
    name: str
    source_url: str
    destination_url: str
    source_wiki_url: str | None
    destination_wiki_url: str | None
    source_provider: str
    destination_provider: str
    source_api_url: str | None
    destination_api_url: str | None
    source_api_repository: str | None
    destination_api_repository: str | None
    source_token_env: str | None
    destination_token_env: str | None
    destination_create: str
    destination_private: bool
    destination_description: str
    destination_namespace_id: str | None
    wiki: str
    lfs: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ApiTarget:
    provider: str
    api_url: str
    repository: str
    token_env: str | None


def run_command(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        timeout = bounded_timeout_seconds(
            MIGRATION_COMMAND_TIMEOUT_SECONDS,
            "FORGE_MIGRATION_COMMAND_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        raise MigrationError(str(exc)) from None
    try:
        result = run_bounded(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        command = " ".join(redact_url(arg) for arg in args)
        raise MigrationError(
            f"command timed out after {timeout:g} seconds: {command}"
        ) from None
    except (BoundedSubprocessError, ValueError) as exc:
        command = " ".join(redact_url(arg) for arg in args)
        raise MigrationError(f"command output rejected: {command}: {exc}") from None
    if check and result.returncode != 0:
        command = " ".join(redact_url(arg) for arg in args)
        stdout = result.stdout
        stderr = result.stderr
        for arg in args:
            redacted = redact_url(arg)
            if redacted != arg:
                stdout = stdout.replace(arg, redacted)
                stderr = stderr.replace(arg, redacted)
        raise MigrationError(
            f"command failed rc={result.returncode}: {command}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )
    return result


def git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *args], cwd=cwd, check=check)


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return name or "repository"


def redact_url(value: str) -> str:
    if "://" not in value:
        return value
    parts = urlsplit(value)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        username = userinfo.split(":", 1)[0]
        netloc = f"{username}:<redacted>@{host}"
    query = re.sub(r"(?i)(token|password|secret|access_token)=[^&]+", r"\1=<redacted>", parts.query)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def derive_wiki_url(repo_url: str) -> str:
    if repo_url.endswith(".git"):
        return f"{repo_url[:-4]}.wiki.git"
    return f"{repo_url}.wiki.git"


def split_direction(direction: str) -> tuple[str, str]:
    if "-to-" not in direction:
        raise MigrationError(f"unsupported direction: {direction}")
    source_provider, destination_provider = direction.split("-to-", 1)
    return source_provider, destination_provider


def nested_value(raw: dict[str, Any], nested_key: str, key: str) -> str:
    nested = raw.get(nested_key)
    if isinstance(nested, dict) and nested.get(key):
        return str(nested[key]).strip()
    return ""


def flat_or_nested(raw: dict[str, Any], flat_key: str, nested_key: str, nested_field: str) -> str:
    value = raw.get(flat_key)
    if value:
        return str(value).strip()
    return nested_value(raw, nested_key, nested_field)


def strip_git_suffix(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value


def derive_api_repository(repo_url: str) -> str:
    if repo_url.startswith("git@") and ":" in repo_url:
        return strip_git_suffix(repo_url.split(":", 1)[1].strip("/"))
    parts = urlsplit(repo_url)
    if parts.scheme in {"http", "https", "ssh", "git"} and parts.path:
        return strip_git_suffix(parts.path.strip("/"))
    return ""


def infer_api_url(repo_url: str, provider: str) -> str:
    parts = urlsplit(repo_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    host = parts.netloc
    scheme = parts.scheme
    if provider == "github":
        if host.lower() == "github.com":
            return "https://api.github.com"
        return f"{scheme}://{host}/api/v3"
    if provider == "gitlab":
        return f"{scheme}://{host}/api/v4"
    if provider == "forgejo":
        return f"{scheme}://{host}/api/v1"
    return ""


def normalize_bool_mode(value: Any, default: str = "false") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "required" if value else "false"
    mode = str(value).strip().lower()
    if mode in {"1", "yes", "true", "required", "require"}:
        return "required"
    if mode in {"auto", "optional"}:
        return "auto"
    if mode in {"0", "no", "false", "skip", "none"}:
        return "false"
    raise MigrationError(f"unsupported mode {value!r}; use true, false, required, or auto")


def normalize_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise MigrationError(f"unsupported boolean value {value!r}")


def load_plan(path: Path) -> dict[str, Any]:
    try:
        loaded = loads_strict_json(read_bounded_text(path, encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MigrationError(f"{path}: JSON root must be an object")
    return loaded


def require_credential_free_plan(value: Any, path: str = "plan") -> None:
    """Reject literal credentials while allowing references to environment variables."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in SENSITIVE_LITERAL_KEYS and child not in (None, "", False):
                raise MigrationError(
                    f"{path}.{key} must not contain credential or secret data; reference an *_env variable"
                )
            require_credential_free_plan(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            require_credential_free_plan(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError as exc:
            raise MigrationError(f"{path} must contain a valid URL") from exc
        if parts.password or (parts.scheme in {"http", "https"} and parts.username):
            raise MigrationError(f"{path} must not embed credentials in a URL")


def get_url(raw: dict[str, Any], flat_key: str, nested_key: str) -> str:
    value = raw.get(flat_key)
    if value:
        return str(value).strip()
    nested = raw.get(nested_key)
    if isinstance(nested, dict) and nested.get("url"):
        return str(nested["url"]).strip()
    return ""


def parse_repo(raw: dict[str, Any], index: int, direction: str) -> RepoPlan:
    name = str(raw.get("name") or f"repo-{index + 1}").strip()
    source_url = get_url(raw, "source_url", "source")
    destination_url = get_url(raw, "destination_url", "destination")
    if not source_url:
        raise MigrationError(f"repositories[{index}].source_url is required")
    if not destination_url:
        raise MigrationError(f"repositories[{index}].destination_url is required")

    source_wiki_url = raw.get("source_wiki_url")
    destination_wiki_url = raw.get("destination_wiki_url")
    source_wiki = str(source_wiki_url).strip() if source_wiki_url else None
    destination_wiki = str(destination_wiki_url).strip() if destination_wiki_url else None
    wiki = normalize_bool_mode(raw.get("wiki", raw.get("include_wiki")), default="false")
    lfs = normalize_bool_mode(raw.get("lfs", raw.get("include_lfs")), default="false")
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise MigrationError(f"repositories[{index}].metadata must be an object")
    direction_source_provider, direction_destination_provider = split_direction(direction)
    source_provider = (
        flat_or_nested(raw, "source_provider", "source", "provider") or direction_source_provider
    ).lower()
    destination_provider = (
        flat_or_nested(raw, "destination_provider", "destination", "provider") or direction_destination_provider
    ).lower()
    if source_provider != direction_source_provider or destination_provider != direction_destination_provider:
        raise MigrationError(
            f"repositories[{index}] provider pair {source_provider}-to-{destination_provider} "
            f"does not match plan direction {direction}"
        )
    source_api_url = (
        flat_or_nested(raw, "source_api_url", "source", "api_url")
        or infer_api_url(source_url, source_provider)
        or None
    )
    destination_api_url = (
        flat_or_nested(raw, "destination_api_url", "destination", "api_url")
        or infer_api_url(destination_url, destination_provider)
        or None
    )
    source_api_repository = (
        flat_or_nested(raw, "source_api_repository", "source", "api_repository")
        or flat_or_nested(raw, "source_api_project", "source", "api_project")
        or derive_api_repository(source_url)
        or None
    )
    destination_api_repository = (
        flat_or_nested(raw, "destination_api_repository", "destination", "api_repository")
        or flat_or_nested(raw, "destination_api_project", "destination", "api_project")
        or derive_api_repository(destination_url)
        or None
    )
    source_token_env = flat_or_nested(raw, "source_token_env", "source", "token_env") or None
    destination_token_env = flat_or_nested(raw, "destination_token_env", "destination", "token_env") or None
    destination_config = raw.get("destination") if isinstance(raw.get("destination"), dict) else {}
    destination_create_value = (
        raw["destination_create"]
        if "destination_create" in raw
        else destination_config.get("create")
    )
    destination_private_value = (
        raw["destination_private"]
        if "destination_private" in raw
        else destination_config.get("private")
    )
    destination_create = normalize_bool_mode(
        destination_create_value,
        default="false",
    )
    destination_private = normalize_bool(
        destination_private_value,
        default=True,
    )
    destination_description = flat_or_nested(
        raw,
        "destination_description",
        "destination",
        "description",
    )
    destination_namespace_id = (
        flat_or_nested(raw, "destination_namespace_id", "destination", "namespace_id") or None
    )
    return RepoPlan(
        name=name,
        source_url=source_url,
        destination_url=destination_url,
        source_wiki_url=source_wiki,
        destination_wiki_url=destination_wiki,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_api_url=source_api_url,
        destination_api_url=destination_api_url,
        source_api_repository=source_api_repository,
        destination_api_repository=destination_api_repository,
        source_token_env=source_token_env,
        destination_token_env=destination_token_env,
        destination_create=destination_create,
        destination_private=destination_private,
        destination_description=destination_description,
        destination_namespace_id=destination_namespace_id,
        wiki=wiki,
        lfs=lfs,
        metadata=metadata,
    )


def parse_plan(data: dict[str, Any]) -> tuple[str, list[RepoPlan]]:
    require_credential_free_plan(data)
    direction = str(data.get("direction") or "").strip().lower()
    allowed = SUPPORTED_DIRECTIONS | OPTIONAL_DIRECTIONS
    if direction not in allowed:
        raise MigrationError(
            f"direction must be one of: {', '.join(sorted(SUPPORTED_DIRECTIONS))}"
        )
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise MigrationError("plan.repositories must be a non-empty list")
    parsed: list[RepoPlan] = []
    for index, raw_repo in enumerate(repositories):
        if not isinstance(raw_repo, dict):
            raise MigrationError(f"repositories[{index}] must be an object")
        parsed.append(parse_repo(raw_repo, index, direction))
    names = [repo.name.casefold() for repo in parsed]
    if len(names) != len(set(names)):
        raise MigrationError("repository names must be unique within a migration plan")
    work_names = [safe_name(repo.name).casefold() for repo in parsed]
    if len(work_names) != len(set(work_names)):
        raise MigrationError("repository names must map to unique migration work directories")
    destinations = [repo.destination_url.casefold() for repo in parsed]
    if len(destinations) != len(set(destinations)):
        raise MigrationError("destination URLs must be unique within a migration plan")
    for repo in parsed:
        if repo.source_url.casefold() == repo.destination_url.casefold():
            raise MigrationError(f"{repo.name}: source and destination URLs must be different")
    return direction, parsed


def require_supported_metadata(repo: RepoPlan) -> list[dict[str, str]]:
    unsupported: list[dict[str, str]] = []
    for surface, state in repo.metadata.items():
        normalized = str(state).strip().lower()
        if normalized in SUPPORTED_METADATA_STATES:
            continue
        if surface in SUPPORTED_METADATA_SURFACES:
            continue
        if surface in UNSUPPORTED_METADATA_SURFACES:
            unsupported.append({"surface": surface, "state": normalized or "required"})
        else:
            unsupported.append({"surface": surface, "state": normalized or "required"})
    if unsupported:
        details = ", ".join(f"{item['surface']}={item['state']}" for item in unsupported)
        raise MigrationError(
            f"{repo.name}: metadata migration is not implemented for required surfaces: {details}. "
            "Set them to skip until a provider-specific importer is added."
        )
    return unsupported


def metadata_mode(repo: RepoPlan, surface: str) -> str:
    value = repo.metadata.get(surface)
    if value is None:
        return "skip"
    if isinstance(value, bool):
        return "required" if value else "skip"
    if isinstance(value, dict):
        value = value.get("mode", "required")
    mode = str(value).strip().lower()
    if mode in SUPPORTED_METADATA_STATES:
        return "skip"
    if mode in {"1", "yes", "true", "required", "require", "migrate", "mirror"}:
        return "required"
    if mode in {"auto", "optional"}:
        return "auto"
    raise MigrationError(f"{repo.name}: unsupported metadata mode for {surface}: {value!r}")


def branch_protection_options(repo: RepoPlan) -> dict[str, Any]:
    value = repo.metadata.get("branch_protection")
    if value is None or isinstance(value, (bool, str)):
        return {}
    if not isinstance(value, dict):
        raise MigrationError(
            f"{repo.name}: branch_protection must be a mode or an object"
        )
    unknown = sorted(set(value) - BRANCH_PROTECTION_OPTION_KEYS)
    if unknown:
        raise MigrationError(
            f"{repo.name}: branch_protection has unsupported option(s): "
            f"{', '.join(unknown)}"
        )
    return value


def github_branch_protection_scope(repo: RepoPlan) -> list[str]:
    options = branch_protection_options(repo)
    raw_branches = options.get("branches")
    if not isinstance(raw_branches, list) or not raw_branches:
        raise MigrationError(
            f"{repo.name}: GitHub branch_protection requires a non-empty branches list; "
            "the REST API cannot enumerate wildcard branch-protection rules safely"
        )
    branches: list[str] = []
    for index, raw_branch in enumerate(raw_branches):
        branch = str(raw_branch).strip()
        if not branch:
            raise MigrationError(
                f"{repo.name}: branch_protection.branches[{index}] must not be empty"
            )
        if any(character in branch for character in "*?["):
            raise MigrationError(
                f"{repo.name}: GitHub branch protection scope must use exact branch names, "
                f"not wildcard {branch!r}"
            )
        branches.append(branch)
    if len(branches) != len(set(branches)):
        raise MigrationError(
            f"{repo.name}: branch_protection.branches must not contain duplicates"
        )
    return branches


def gitlab_maintainer_team(repo: RepoPlan) -> str | None:
    value = branch_protection_options(repo).get("gitlab_maintainer_team")
    if value is None:
        return None
    team = str(value).strip()
    if not team:
        raise MigrationError(
            f"{repo.name}: branch_protection.gitlab_maintainer_team must not be empty"
        )
    return team


def validate_branch_protection_contract(
    repo: RepoPlan,
    source: ApiTarget,
    destination: ApiTarget,
) -> None:
    options = branch_protection_options(repo)
    if destination.provider != "forgejo" or source.provider not in {"github", "gitlab"}:
        raise MigrationError(
            f"{repo.name}: verified branch-protection migration currently supports "
            "GitHub or GitLab sources with a Forgejo destination"
        )
    if source.provider == "github":
        github_branch_protection_scope(repo)
        if "gitlab_maintainer_team" in options:
            raise MigrationError(
                f"{repo.name}: gitlab_maintainer_team only applies to GitLab sources"
            )
    elif "branches" in options:
        raise MigrationError(
            f"{repo.name}: GitLab protected branches are enumerated by the API; "
            "branch_protection.branches is only valid for GitHub sources"
        )


def branch_protection_plan(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "branch_protection")
    if mode == "skip":
        return {"mode": mode, "status": "skipped"}
    try:
        source = api_target(repo, "source")
        destination = api_target(repo, "destination")
        validate_branch_protection_contract(repo, source, destination)
    except MigrationError as exc:
        if mode == "auto":
            return {
                "mode": mode,
                "status": "skipped",
                "reason": str(exc),
            }
        raise
    plan = {
        "mode": mode,
        "status": "planned",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "source_api_url": redact_url(source.api_url),
        "destination_api_url": redact_url(destination.api_url),
        "source_repository": source.repository,
        "destination_repository": destination.repository,
    }
    if source.provider == "github":
        plan["branches"] = github_branch_protection_scope(repo)
    return plan


def api_target(repo: RepoPlan, side: str) -> ApiTarget:
    if side == "source":
        provider = repo.source_provider
        api_url = repo.source_api_url
        repository = repo.source_api_repository
        token_env = repo.source_token_env
    elif side == "destination":
        provider = repo.destination_provider
        api_url = repo.destination_api_url
        repository = repo.destination_api_repository
        token_env = repo.destination_token_env
    else:
        raise MigrationError(f"unsupported API target side: {side}")
    if provider not in {"github", "gitlab", "forgejo"}:
        raise MigrationError(f"{repo.name}: unsupported {side} provider for metadata API: {provider}")
    if not api_url:
        raise MigrationError(f"{repo.name}: {side}.api_url is required for metadata migration")
    if not repository:
        raise MigrationError(f"{repo.name}: {side}.api_repository is required for metadata migration")
    return ApiTarget(provider=provider, api_url=api_url.rstrip("/"), repository=repository, token_env=token_env)


def validate_metadata_requirements(repo: RepoPlan) -> dict[str, Any]:
    require_supported_metadata(repo)
    source: ApiTarget | None = None
    destination: ApiTarget | None = None

    def planned_surface(surface: str, mode_override: str | None = None) -> dict[str, Any]:
        nonlocal source, destination
        mode = mode_override if mode_override is not None else metadata_mode(repo, surface)
        if mode == "skip":
            return {"mode": mode, "status": "skipped"}
        if source is None:
            source = api_target(repo, "source")
        if destination is None:
            destination = api_target(repo, "destination")
        return {
            "mode": mode,
            "status": "planned",
            "source_provider": source.provider,
            "destination_provider": destination.provider,
            "source_api_url": redact_url(source.api_url),
            "destination_api_url": redact_url(destination.api_url),
            "source_repository": source.repository,
            "destination_repository": destination.repository,
        }

    return {
        "branch_protection": branch_protection_plan(repo),
        "labels": planned_surface("labels"),
        "milestones": planned_surface("milestones"),
        "releases": planned_surface("releases"),
        "issues": planned_surface("issues"),
        "change_requests": planned_surface(change_request_surface(repo), change_request_mode(repo)),
        "verified": True,
    }


def api_headers(target: ApiTarget) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "platform-gitops-forge-migration",
    }
    credential_value = os.environ.get(target.token_env, "") if target.token_env else ""
    if credential_value:
        if target.provider == "gitlab":
            headers["PRIVATE-TOKEN"] = credential_value
        elif target.provider == "github":
            headers["Authorization"] = f"Bearer {credential_value}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        else:
            headers["Authorization"] = f"token {credential_value}"
    return headers


def redact_api_text(target: ApiTarget, value: str) -> str:
    """Remove the configured API credential from remote diagnostics."""
    credential_value = os.environ.get(target.token_env, "") if target.token_env else ""
    if credential_value:
        value = value.replace(credential_value, "<redacted>")
    return value


def api_request(
    target: ApiTarget,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
    return_status: bool = False,
) -> Any:
    url = f"{target.api_url}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, headers=api_headers(target), method=method)
    attempts = 3 if method.upper() == "GET" else 1
    try:
        timeout = http_timeout_seconds()
    except HttpTransportPolicyError as exc:
        raise MigrationError(str(exc)) from None
    for attempt in range(1, attempts + 1):
        try:
            with open_http_request(request, timeout=timeout) as response:
                status = response.status
                payload = read_bounded_response(response).decode("utf-8")
            break
        except HTTPError as exc:
            try:
                payload = read_bounded_response(exc).decode("utf-8", errors="replace")
            except HttpTransportPolicyError as policy_error:
                raise MigrationError(
                    f"{method} {redact_url(url)} response rejected: {policy_error}"
                ) from policy_error
            if exc.code in expected:
                try:
                    decoded = loads_strict_json(payload) if payload else {}
                except json.JSONDecodeError as decode_error:
                    raise MigrationError(
                        f"{method} {redact_url(url)} returned invalid JSON: {decode_error}"
                    ) from decode_error
                return (exc.code, decoded) if return_status else decoded
            raise MigrationError(
                f"{method} {redact_url(url)} failed with HTTP {exc.code}: "
                f"{redact_api_text(target, payload[:500])}"
            ) from exc
        except (HttpTransportPolicyError, UnicodeDecodeError) as exc:
            raise MigrationError(
                f"{method} {redact_url(url)} response rejected: {exc}"
            ) from exc
        except (URLError, ConnectionError, TimeoutError) as exc:
            if attempt < attempts:
                time.sleep(0.1 * (2 ** (attempt - 1)))
                continue
            raise MigrationError(f"{method} {redact_url(url)} failed after {attempt} attempt(s): {exc}") from exc
    if status not in expected:
        raise MigrationError(
            f"{method} {redact_url(url)} returned HTTP {status}: "
            f"{redact_api_text(target, payload[:500])}"
        )
    if not payload:
        decoded = {}
        return (status, decoded) if return_status else decoded
    try:
        decoded = loads_strict_json(payload)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{method} {redact_url(url)} returned invalid JSON: {exc}") from exc
    return (status, decoded) if return_status else decoded


def repo_api_base(target: ApiTarget) -> str:
    if target.provider in {"github", "forgejo"}:
        parts = target.repository.strip("/").split("/")
        if len(parts) != 2:
            raise MigrationError(
                f"{target.provider} metadata APIs require api_repository as owner/repo, got {target.repository!r}"
            )
        owner, repo = parts
        return f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    if target.provider == "gitlab":
        return f"projects/{quote(target.repository.strip('/'), safe='')}"
    raise MigrationError(f"unsupported provider: {target.provider}")


def destination_repository_plan(repo: RepoPlan) -> dict[str, Any]:
    mode = repo.destination_create
    if mode == "false":
        return {"mode": mode, "status": "not-managed", "verified": True}
    destination = api_target(repo, "destination")
    return {
        "mode": mode,
        "status": "planned",
        "provider": destination.provider,
        "api_url": redact_url(destination.api_url),
        "repository": destination.repository,
        "private": repo.destination_private,
        "verified": True,
    }


def repository_probe(target: ApiTarget) -> tuple[int, dict[str, Any]]:
    response = api_request(
        target,
        "GET",
        repo_api_base(target),
        expected=(200, 404),
        return_status=True,
    )
    if not isinstance(response, tuple) or len(response) != 2:
        raise MigrationError(f"{target.provider} repository probe did not return an HTTP status")
    status, payload = response
    if not isinstance(payload, dict):
        raise MigrationError(f"{target.provider} repository probe returned a non-object response")
    return int(status), payload


def authenticated_login(target: ApiTarget) -> str:
    payload = api_request(target, "GET", "user", expected=(200,))
    if not isinstance(payload, dict):
        raise MigrationError(f"{target.provider} current-user API returned a non-object response")
    login = str(payload.get("login") or payload.get("username") or "").strip()
    if not login:
        raise MigrationError(f"{target.provider} current-user API response is missing login/username")
    return login


def gitlab_namespace_id(target: ApiTarget, namespace_path: str) -> str:
    for page in range(1, 101):
        payload = api_request(
            target,
            "GET",
            "namespaces",
            query={"search": namespace_path, "per_page": 100, "page": page},
            expected=(200,),
        )
        if not isinstance(payload, list):
            raise MigrationError("gitlab namespaces API returned a non-list response")
        for namespace in payload:
            if not isinstance(namespace, dict):
                continue
            candidates = {
                str(namespace.get("full_path") or "").strip().casefold(),
                str(namespace.get("path") or "").strip().casefold(),
            }
            if namespace_path.casefold() in candidates and namespace.get("id") is not None:
                return str(namespace["id"])
        if len(payload) < 100:
            break
    raise MigrationError(
        f"gitlab destination namespace {namespace_path!r} was not found; "
        "set destination.namespace_id explicitly when the token cannot list namespaces"
    )


def create_destination_repository(repo: RepoPlan, target: ApiTarget) -> dict[str, Any]:
    parts = [part for part in target.repository.strip("/").split("/") if part]
    if not parts:
        raise MigrationError(f"{repo.name}: destination.api_repository is empty")
    repository_name = parts[-1]
    if target.provider == "gitlab":
        payload: dict[str, Any] = {
            "name": repository_name,
            "path": repository_name,
            "description": repo.destination_description,
            "visibility": "private" if repo.destination_private else "public",
            "initialize_with_readme": False,
        }
        namespace_path = "/".join(parts[:-1])
        namespace_id = repo.destination_namespace_id
        if namespace_path and not namespace_id:
            namespace_id = gitlab_namespace_id(target, namespace_path)
        if namespace_id:
            payload["namespace_id"] = namespace_id
        created = api_request(target, "POST", "projects", body=payload, expected=(201,))
    else:
        if len(parts) != 2:
            raise MigrationError(
                f"{target.provider} destination creation requires api_repository as owner/repo, "
                f"got {target.repository!r}"
            )
        owner, repository_name = parts
        login = authenticated_login(target)
        create_path = "user/repos" if owner.casefold() == login.casefold() else (
            f"orgs/{quote(owner, safe='')}/repos"
        )
        created = api_request(
            target,
            "POST",
            create_path,
            body={
                "name": repository_name,
                "description": repo.destination_description,
                "private": repo.destination_private,
                "auto_init": False,
            },
            expected=(201,),
        )
    if not isinstance(created, dict):
        raise MigrationError(f"{target.provider} repository create API returned a non-object response")
    return created


def ensure_destination_repository(repo: RepoPlan) -> dict[str, Any]:
    mode = repo.destination_create
    if mode == "false":
        return {"mode": mode, "status": "not-managed", "verified": True}
    target = api_target(repo, "destination")
    status, _payload = repository_probe(target)
    created = False
    if status == 404:
        create_destination_repository(repo, target)
        created = True
        status, _payload = repository_probe(target)
    verified = status == 200
    if not verified and mode == "required":
        raise MigrationError(
            f"{repo.name}: destination repository {target.repository!r} was not available after creation"
        )
    return {
        "mode": mode,
        "status": "created" if created else "existing",
        "provider": target.provider,
        "repository": target.repository,
        "private": repo.destination_private,
        "verified": verified,
    }


def verify_destination_repository(repo: RepoPlan) -> dict[str, Any]:
    mode = repo.destination_create
    if mode == "false":
        return {"mode": mode, "status": "not-managed", "verified": True}
    target = api_target(repo, "destination")
    status, _payload = repository_probe(target)
    return {
        "mode": mode,
        "status": "existing" if status == 200 else "missing",
        "provider": target.provider,
        "repository": target.repository,
        "private": repo.destination_private,
        "verified": status == 200,
    }


def reconcile_destination_default_branch(
    repo: RepoPlan,
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    if repo.destination_create == "false":
        return lifecycle
    source_default_ref, source_error = ls_remote_default_branch(repo.source_url)
    if source_error:
        raise MigrationError(
            f"{repo.name}: cannot read source default branch: {source_error}"
        )
    if source_default_ref is None:
        return {**lifecycle, "default_branch": None, "default_branch_verified": True}
    source_default_branch = source_default_ref.removeprefix("refs/heads/")
    target = api_target(repo, "destination")
    status, payload = repository_probe(target)
    if status != 200:
        raise MigrationError(f"{repo.name}: destination repository disappeared before default-branch update")
    current_default_branch = str(payload.get("default_branch") or "").strip()
    updated = False
    if current_default_branch != source_default_branch:
        method = "PUT" if target.provider == "gitlab" else "PATCH"
        api_request(
            target,
            method,
            repo_api_base(target),
            body={"default_branch": source_default_branch},
            expected=(200,),
        )
        updated = True
        status, payload = repository_probe(target)
        current_default_branch = str(payload.get("default_branch") or "").strip()
    verified = status == 200 and current_default_branch == source_default_branch
    if not verified and repo.destination_create == "required":
        raise MigrationError(
            f"{repo.name}: destination default branch is {current_default_branch!r}, "
            f"expected {source_default_branch!r}"
        )
    return {
        **lifecycle,
        "default_branch": source_default_branch,
        "default_branch_updated": updated,
        "default_branch_verified": verified,
        "verified": lifecycle.get("verified", False) and verified,
    }


def list_labels(target: ApiTarget) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    base = repo_api_base(target)
    for page in range(1, 1001):
        payload = api_request(
            target,
            "GET",
            f"{base}/labels",
            query={"per_page": 100, "page": page},
            expected=(200,),
        )
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} labels API returned a non-list response")
        labels.extend(payload)
        if len(payload) < 100:
            break
    return labels


def normalize_label_color(value: Any) -> str:
    color = str(value or "").strip().lstrip("#").lower()
    if not re.fullmatch(r"[0-9a-f]{6}", color):
        raise MigrationError(f"label color must be a 6-digit hex value, got {value!r}")
    return color


def normalize_label(label: dict[str, Any]) -> dict[str, str]:
    name = str(label.get("name") or "").strip()
    if not name:
        raise MigrationError("label is missing a name")
    description = label.get("description")
    return {
        "name": name,
        "color": normalize_label_color(label.get("color")),
        "description": "" if description is None else str(description),
    }


def normalized_labels(labels: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized = [normalize_label(label) for label in labels]
    return sorted(normalized, key=lambda item: item["name"].casefold())


def label_digest(labels: list[dict[str, str]]) -> str:
    payload = json.dumps(labels, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def provider_label_payload(target: ApiTarget, label: dict[str, str], update: bool = False) -> dict[str, Any]:
    color = label["color"]
    if target.provider == "github":
        payload: dict[str, Any] = {
            "color": color,
            "description": label["description"],
        }
        if update:
            payload["new_name"] = label["name"]
        else:
            payload["name"] = label["name"]
        return payload
    color_with_hash = f"#{color}"
    if target.provider == "gitlab":
        payload = {
            "color": color_with_hash,
            "description": label["description"],
        }
        if update:
            payload["new_name"] = label["name"]
        else:
            payload["name"] = label["name"]
        return payload
    return {
        "name": label["name"],
        "color": color_with_hash,
        "description": label["description"],
    }


def label_update_path(target: ApiTarget, existing_label: dict[str, Any], label: dict[str, str]) -> str:
    base = repo_api_base(target)
    if target.provider == "github":
        return f"{base}/labels/{quote(label['name'], safe='')}"
    label_id = existing_label.get("id") or existing_label.get("name") or label["name"]
    return f"{base}/labels/{quote(str(label_id), safe='')}"


def create_label(target: ApiTarget, label: dict[str, str]) -> None:
    api_request(
        target,
        "POST",
        f"{repo_api_base(target)}/labels",
        body=provider_label_payload(target, label, update=False),
        expected=(200, 201),
    )


def update_label(target: ApiTarget, existing_label: dict[str, Any], label: dict[str, str]) -> None:
    method = "PATCH" if target.provider in {"github", "forgejo"} else "PUT"
    api_request(
        target,
        method,
        label_update_path(target, existing_label, label),
        body=provider_label_payload(target, label, update=True),
        expected=(200,),
    )


def compare_label_sets(source_labels: list[dict[str, str]], destination_labels: list[dict[str, str]]) -> dict[str, Any]:
    source_by_name = {label["name"]: label for label in source_labels}
    destination_by_name = {label["name"]: label for label in destination_labels}
    missing = sorted(set(source_by_name) - set(destination_by_name), key=str.casefold)
    mismatched = sorted(
        name
        for name in set(source_by_name) & set(destination_by_name)
        if source_by_name[name] != destination_by_name[name]
    )
    extra = sorted(set(destination_by_name) - set(source_by_name), key=str.casefold)
    replicated_destination_labels = sorted(
        (destination_by_name[name] for name in source_by_name if name in destination_by_name),
        key=lambda item: item["name"].casefold(),
    )
    return {
        "verified": not missing and not mismatched,
        "source_count": len(source_labels),
        "destination_count": len(destination_labels),
        "source_digest": label_digest(source_labels),
        "destination_digest": label_digest(replicated_destination_labels),
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
    }


def poll_verified_comparison(
    loader: Callable[[], dict[str, Any]],
    *,
    attempts: int = METADATA_VERIFICATION_ATTEMPTS,
    initial_delay_seconds: float = METADATA_VERIFICATION_INITIAL_DELAY_SECONDS,
) -> dict[str, Any]:
    """Retry a post-write comparison while forge APIs converge."""

    if attempts <= 0:
        raise ValueError("comparison attempts must be greater than zero")
    comparison: dict[str, Any] = {}
    for attempt in range(attempts):
        comparison = loader()
        if comparison.get("verified") is True:
            return comparison
        if attempt + 1 < attempts:
            time.sleep(initial_delay_seconds * (2**attempt))
    return comparison


def require_api_bool(value: Any, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, dict):
        value = value.get("enabled")
    if not isinstance(value, bool):
        raise MigrationError(f"{label} must be a boolean")
    return value


def require_api_int(value: Any, label: str, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError(f"{label} must be a non-negative integer")
    return value


def require_api_string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MigrationError(f"{label} must be a list")
    normalized: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise MigrationError(f"{label} must not contain empty values")
        normalized.append(text)
    return sorted(set(normalized), key=str.casefold)


def empty_forgejo_branch_protection(branch_name: str) -> dict[str, Any]:
    return {
        "branch_name": branch_name,
        "apply_to_admins": False,
        "enable_push": True,
        "enable_push_whitelist": False,
        "push_whitelist_usernames": [],
        "push_whitelist_teams": [],
        "push_whitelist_deploy_keys": False,
        "enable_merge_whitelist": False,
        "merge_whitelist_usernames": [],
        "merge_whitelist_teams": [],
        "enable_status_check": False,
        "status_check_contexts": [],
        "required_approvals": 0,
        "enable_approvals_whitelist": False,
        "approvals_whitelist_username": [],
        "approvals_whitelist_teams": [],
        "block_on_rejected_reviews": False,
        "block_on_official_review_requests": False,
        "block_on_outdated_branch": False,
        "dismiss_stale_approvals": False,
        "ignore_stale_approvals": False,
        "require_signed_commits": False,
        "protected_file_patterns": "",
        "unprotected_file_patterns": "",
    }


def normalize_forgejo_branch_protection(rule: dict[str, Any]) -> dict[str, Any]:
    branch_name = str(rule.get("rule_name") or rule.get("branch_name") or "").strip()
    if not branch_name:
        raise MigrationError("Forgejo branch-protection rule is missing rule_name/branch_name")
    normalized = empty_forgejo_branch_protection(branch_name)
    for field in BRANCH_PROTECTION_BOOL_FIELDS:
        normalized[field] = require_api_bool(
            rule.get(field), f"Forgejo branch protection {branch_name!r}.{field}"
        )
    for field in BRANCH_PROTECTION_LIST_FIELDS:
        value = rule.get(field)
        if field == "approvals_whitelist_username" and value is None:
            value = rule.get("approvals_whitelist_usernames")
        normalized[field] = require_api_string_list(
            value, f"Forgejo branch protection {branch_name!r}.{field}"
        )
    normalized["required_approvals"] = require_api_int(
        rule.get("required_approvals"),
        f"Forgejo branch protection {branch_name!r}.required_approvals",
    )
    for field in ("protected_file_patterns", "unprotected_file_patterns"):
        value = rule.get(field)
        normalized[field] = "" if value is None else str(value)
    return normalized


def github_actor_restrictions_present(value: Any, label: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, dict):
        raise MigrationError(f"{label} must be an object or null")
    for field in ("users", "teams", "apps"):
        actors = value.get(field)
        if actors is not None and not isinstance(actors, list):
            raise MigrationError(f"{label}.{field} must be a list")
        if actors:
            return True
    return False


def normalize_github_branch_protection(
    branch_name: str,
    protection: dict[str, Any],
) -> dict[str, Any]:
    if github_actor_restrictions_present(
        protection.get("restrictions"),
        f"GitHub branch protection {branch_name!r}.restrictions",
    ):
        raise MigrationError(
            f"GitHub branch {branch_name!r} has push actor restrictions; "
            "identity and role restrictions require an explicit destination mapping"
        )
    enforce_admins = require_api_bool(
        protection.get("enforce_admins"),
        f"GitHub branch protection {branch_name!r}.enforce_admins",
    )
    if not enforce_admins:
        raise MigrationError(
            f"GitHub branch {branch_name!r} exempts administrators; Forgejo's portable "
            "rule cannot prove that exception"
        )
    unsupported_enabled = []
    for field in (
        "allow_deletions",
        "allow_force_pushes",
        "allow_fork_syncing",
        "block_creations",
        "lock_branch",
        "required_conversation_resolution",
        "required_linear_history",
    ):
        if require_api_bool(
            protection.get(field),
            f"GitHub branch protection {branch_name!r}.{field}",
        ):
            unsupported_enabled.append(field)
    if unsupported_enabled:
        raise MigrationError(
            f"GitHub branch {branch_name!r} enables non-portable control(s): "
            f"{', '.join(unsupported_enabled)}"
        )

    normalized = empty_forgejo_branch_protection(branch_name)
    normalized["apply_to_admins"] = enforce_admins
    status_checks = protection.get("required_status_checks")
    if status_checks is not None:
        if not isinstance(status_checks, dict):
            raise MigrationError(
                f"GitHub branch protection {branch_name!r}.required_status_checks must be an object"
            )
        contexts = require_api_string_list(
            status_checks.get("contexts"),
            f"GitHub branch protection {branch_name!r}.required_status_checks.contexts",
        )
        checks = status_checks.get("checks")
        if checks is not None:
            if not isinstance(checks, list):
                raise MigrationError(
                    f"GitHub branch protection {branch_name!r}.required_status_checks.checks "
                    "must be a list"
                )
            for check in checks:
                if not isinstance(check, dict) or not str(check.get("context") or "").strip():
                    raise MigrationError(
                        f"GitHub branch protection {branch_name!r} has a malformed status check"
                    )
                contexts.append(str(check["context"]).strip())
        normalized["status_check_contexts"] = sorted(set(contexts), key=str.casefold)
        normalized["enable_status_check"] = True
        normalized["block_on_outdated_branch"] = require_api_bool(
            status_checks.get("strict"),
            f"GitHub branch protection {branch_name!r}.required_status_checks.strict",
        )

    reviews = protection.get("required_pull_request_reviews")
    if reviews is not None:
        if not isinstance(reviews, dict):
            raise MigrationError(
                f"GitHub branch protection {branch_name!r}.required_pull_request_reviews "
                "must be an object"
            )
        if github_actor_restrictions_present(
            reviews.get("dismissal_restrictions"),
            f"GitHub branch protection {branch_name!r}.dismissal_restrictions",
        ) or github_actor_restrictions_present(
            reviews.get("bypass_pull_request_allowances"),
            f"GitHub branch protection {branch_name!r}.bypass_pull_request_allowances",
        ):
            raise MigrationError(
                f"GitHub branch {branch_name!r} has review actor restrictions; "
                "identity restrictions require an explicit destination mapping"
            )
        for field in ("require_code_owner_reviews", "require_last_push_approval"):
            if require_api_bool(
                reviews.get(field),
                f"GitHub branch protection {branch_name!r}.{field}",
            ):
                raise MigrationError(
                    f"GitHub branch {branch_name!r} enables non-portable control {field}"
                )
        normalized["enable_push"] = False
        normalized["required_approvals"] = require_api_int(
            reviews.get("required_approving_review_count"),
            f"GitHub branch protection {branch_name!r}.required_approving_review_count",
        )
        normalized["dismiss_stale_approvals"] = require_api_bool(
            reviews.get("dismiss_stale_reviews"),
            f"GitHub branch protection {branch_name!r}.dismiss_stale_reviews",
        )
        normalized["block_on_rejected_reviews"] = True

    normalized["require_signed_commits"] = require_api_bool(
        protection.get("required_signatures"),
        f"GitHub branch protection {branch_name!r}.required_signatures",
    )
    return normalized


def gitlab_access_level(
    branch_name: str,
    field: str,
    value: Any,
) -> int:
    if not isinstance(value, list) or not value:
        raise MigrationError(
            f"GitLab protected branch {branch_name!r}.{field} must be a non-empty list"
        )
    levels: set[int] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise MigrationError(
                f"GitLab protected branch {branch_name!r}.{field} contains a non-object entry"
            )
        if any(entry.get(key) is not None for key in ("user_id", "group_id", "deploy_key_id")):
            raise MigrationError(
                f"GitLab protected branch {branch_name!r}.{field} has identity-specific access; "
                "users, groups, and deploy keys require explicit destination mappings"
            )
        raw_level = entry.get("access_level")
        if isinstance(raw_level, bool) or not isinstance(raw_level, int):
            raise MigrationError(
                f"GitLab protected branch {branch_name!r}.{field}.access_level must be an integer"
            )
        levels.add(raw_level)
    if len(levels) != 1:
        raise MigrationError(
            f"GitLab protected branch {branch_name!r}.{field} combines access levels that "
            "cannot be represented as one Forgejo rule"
        )
    level = next(iter(levels))
    if level not in {0, 30, 40}:
        raise MigrationError(
            f"GitLab protected branch {branch_name!r}.{field} uses unsupported access level {level}"
        )
    return level


def normalize_gitlab_branch_protection(
    repo: RepoPlan,
    protection: dict[str, Any],
) -> dict[str, Any]:
    branch_name = str(protection.get("name") or "").strip()
    if not branch_name:
        raise MigrationError("GitLab protected branch is missing its name")
    if require_api_bool(
        protection.get("allow_force_push"),
        f"GitLab protected branch {branch_name!r}.allow_force_push",
    ):
        raise MigrationError(
            f"GitLab protected branch {branch_name!r} allows force pushes, which is not "
            "portable to the verified Forgejo policy subset"
        )
    if require_api_bool(
        protection.get("code_owner_approval_required"),
        f"GitLab protected branch {branch_name!r}.code_owner_approval_required",
    ):
        raise MigrationError(
            f"GitLab protected branch {branch_name!r} requires code-owner approval, which "
            "is not portable to the verified Forgejo policy subset"
        )

    push_level = gitlab_access_level(
        branch_name, "push_access_levels", protection.get("push_access_levels")
    )
    merge_level = gitlab_access_level(
        branch_name, "merge_access_levels", protection.get("merge_access_levels")
    )
    unprotect = protection.get("unprotect_access_levels")
    if unprotect is not None and gitlab_access_level(
        branch_name, "unprotect_access_levels", unprotect
    ) != 40:
        raise MigrationError(
            f"GitLab protected branch {branch_name!r} allows non-maintainers to unprotect it"
        )

    maintainer_team = gitlab_maintainer_team(repo)
    if 40 in {push_level, merge_level} and maintainer_team is None:
        raise MigrationError(
            f"GitLab protected branch {branch_name!r} uses Maintainers-only access; set "
            "branch_protection.gitlab_maintainer_team to the pre-created Forgejo team"
        )

    normalized = empty_forgejo_branch_protection(branch_name)
    # GitLab protected branches do not expose an administrator-bypass switch.
    # Applying the Forgejo rule to administrators avoids weakening the source policy.
    normalized["apply_to_admins"] = True
    normalized["enable_push"] = push_level != 0
    if push_level == 40:
        normalized["enable_push_whitelist"] = True
        normalized["push_whitelist_teams"] = [maintainer_team]
    if merge_level == 0:
        normalized["enable_merge_whitelist"] = True
    elif merge_level == 40:
        normalized["enable_merge_whitelist"] = True
        normalized["merge_whitelist_teams"] = [maintainer_team]
    return normalized


def normalized_branch_protections(
    repo: RepoPlan,
    target: ApiTarget,
    protections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if target.provider == "forgejo":
        normalized = [normalize_forgejo_branch_protection(rule) for rule in protections]
    elif target.provider == "gitlab":
        normalized = [normalize_gitlab_branch_protection(repo, rule) for rule in protections]
    else:
        raise MigrationError(
            "GitHub branch protections require an explicit branch name during normalization"
        )
    names = [rule["branch_name"] for rule in normalized]
    if len(names) != len(set(names)):
        raise MigrationError(
            f"{target.provider} returned duplicate branch-protection rule names"
        )
    return sorted(normalized, key=lambda item: item["branch_name"].casefold())


def list_forgejo_branch_protections(target: ApiTarget) -> list[dict[str, Any]]:
    protections: list[dict[str, Any]] = []
    for page in range(1, 1001):
        payload = api_request(
            target,
            "GET",
            f"{repo_api_base(target)}/branch_protections",
            query={"limit": 100, "page": page},
            expected=(200,),
        )
        if not isinstance(payload, list):
            raise MigrationError("Forgejo branch-protection API returned a non-list response")
        protections.extend(payload)
        if len(payload) < 100:
            break
    return protections


def list_source_branch_protections(repo: RepoPlan, target: ApiTarget) -> list[dict[str, Any]]:
    if target.provider == "github":
        protections: list[dict[str, Any]] = []
        for branch_name in github_branch_protection_scope(repo):
            payload = api_request(
                target,
                "GET",
                f"{repo_api_base(target)}/branches/{quote(branch_name, safe='')}/protection",
                expected=(200,),
            )
            if not isinstance(payload, dict):
                raise MigrationError(
                    f"GitHub branch protection for {branch_name!r} returned a non-object response"
                )
            protections.append(normalize_github_branch_protection(branch_name, payload))
        return sorted(protections, key=lambda item: item["branch_name"].casefold())
    if target.provider == "gitlab":
        protections = []
        for page in range(1, 1001):
            payload = api_request(
                target,
                "GET",
                f"{repo_api_base(target)}/protected_branches",
                query={"per_page": 100, "page": page},
                expected=(200,),
            )
            if not isinstance(payload, list):
                raise MigrationError("GitLab protected-branches API returned a non-list response")
            protections.extend(payload)
            if len(payload) < 100:
                break
        return normalized_branch_protections(repo, target, protections)
    raise MigrationError(
        f"{repo.name}: unsupported branch-protection source provider {target.provider}"
    )


def branch_protection_digest(protections: list[dict[str, Any]]) -> str:
    payload = json.dumps(protections, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compare_branch_protection_sets(
    source_protections: list[dict[str, Any]],
    destination_protections: list[dict[str, Any]],
) -> dict[str, Any]:
    source_by_name = {rule["branch_name"]: rule for rule in source_protections}
    destination_by_name = {rule["branch_name"]: rule for rule in destination_protections}
    missing = sorted(set(source_by_name) - set(destination_by_name), key=str.casefold)
    mismatched = sorted(
        name
        for name in set(source_by_name) & set(destination_by_name)
        if source_by_name[name] != destination_by_name[name]
    )
    extra = sorted(set(destination_by_name) - set(source_by_name), key=str.casefold)
    replicated = sorted(
        (destination_by_name[name] for name in source_by_name if name in destination_by_name),
        key=lambda item: item["branch_name"].casefold(),
    )
    return {
        "verified": not missing and not mismatched,
        "source_count": len(source_protections),
        "destination_count": len(destination_protections),
        "source_digest": branch_protection_digest(source_protections),
        "destination_digest": branch_protection_digest(replicated),
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
    }


def forgejo_branch_protection_payload(
    protection: dict[str, Any],
    create: bool,
) -> dict[str, Any]:
    payload = dict(protection)
    branch_name = str(payload.pop("branch_name"))
    if create:
        payload["branch_name"] = branch_name
        payload["rule_name"] = branch_name
    return payload


def create_forgejo_branch_protection(
    target: ApiTarget,
    protection: dict[str, Any],
) -> None:
    api_request(
        target,
        "POST",
        f"{repo_api_base(target)}/branch_protections",
        body=forgejo_branch_protection_payload(protection, create=True),
        expected=(200, 201),
    )


def update_forgejo_branch_protection(
    target: ApiTarget,
    existing: dict[str, Any],
    protection: dict[str, Any],
) -> None:
    rule_name = str(existing.get("rule_name") or existing.get("branch_name") or "").strip()
    if not rule_name:
        raise MigrationError("Forgejo branch-protection update is missing its rule name")
    api_request(
        target,
        "PATCH",
        f"{repo_api_base(target)}/branch_protections/{quote(rule_name, safe='')}",
        body=forgejo_branch_protection_payload(protection, create=False),
        expected=(200,),
    )


def branch_protection_skip(mode: str, exc: MigrationError) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": "skipped",
        "reason": str(exc),
        "verified": True,
    }


def migrate_branch_protections(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "branch_protection")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    try:
        source = api_target(repo, "source")
        destination = api_target(repo, "destination")
        validate_branch_protection_contract(repo, source, destination)
        source_protections = list_source_branch_protections(repo, source)
    except MigrationError as exc:
        if mode == "auto":
            return branch_protection_skip(mode, exc)
        raise

    destination_before_raw = list_forgejo_branch_protections(destination)
    destination_before = normalized_branch_protections(
        repo, destination, destination_before_raw
    )
    raw_by_name = {
        normalize_forgejo_branch_protection(rule)["branch_name"]: rule
        for rule in destination_before_raw
    }
    destination_by_name = {
        protection["branch_name"]: protection for protection in destination_before
    }
    created = 0
    updated = 0
    for protection in source_protections:
        name = protection["branch_name"]
        existing = destination_by_name.get(name)
        if existing is None:
            create_forgejo_branch_protection(destination, protection)
            created += 1
        elif existing != protection:
            update_forgejo_branch_protection(destination, raw_by_name[name], protection)
            updated += 1
    comparison = poll_verified_comparison(
        lambda: compare_branch_protection_sets(
            source_protections,
            normalized_branch_protections(
                repo, destination, list_forgejo_branch_protections(destination)
            ),
        )
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "created": created,
        "updated": updated,
        **comparison,
    }


def verify_branch_protections(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "branch_protection")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    try:
        source = api_target(repo, "source")
        destination = api_target(repo, "destination")
        validate_branch_protection_contract(repo, source, destination)
        source_protections = list_source_branch_protections(repo, source)
    except MigrationError as exc:
        if mode == "auto":
            return branch_protection_skip(mode, exc)
        raise
    comparison = compare_branch_protection_sets(
        source_protections,
        normalized_branch_protections(
            repo, destination, list_forgejo_branch_protections(destination)
        ),
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        **comparison,
    }


def migrate_labels(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "labels")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    source_labels = normalized_labels(list_labels(source))
    destination_before_raw = list_labels(destination)
    destination_before = normalized_labels(destination_before_raw)
    raw_by_name = {normalize_label(label)["name"]: label for label in destination_before_raw}
    destination_by_name = {label["name"]: label for label in destination_before}
    created = 0
    updated = 0
    for label in source_labels:
        existing = destination_by_name.get(label["name"])
        if existing is None:
            create_label(destination, label)
            created += 1
        elif existing != label:
            update_label(destination, raw_by_name[label["name"]], label)
            updated += 1
    comparison = poll_verified_comparison(
        lambda: compare_label_sets(source_labels, normalized_labels(list_labels(destination)))
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "created": created,
        "updated": updated,
        **comparison,
    }


def verify_labels(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "labels")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    comparison = compare_label_sets(normalized_labels(list_labels(source)), normalized_labels(list_labels(destination)))
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        **comparison,
    }


def list_milestones(target: ApiTarget) -> list[dict[str, Any]]:
    milestones: list[dict[str, Any]] = []
    base = repo_api_base(target)
    for page in range(1, 1001):
        query: dict[str, Any] = {"per_page": 100, "page": page}
        if target.provider in {"github", "forgejo"}:
            query["state"] = "all"
        payload = api_request(
            target,
            "GET",
            f"{base}/milestones",
            query=query,
            expected=(200,),
        )
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} milestones API returned a non-list response")
        milestones.extend(payload)
        if len(payload) < 100:
            break
    return milestones


def normalize_milestone_state(value: Any) -> str:
    state = str(value or "open").strip().lower()
    if state in {"open", "active"}:
        return "open"
    if state == "closed":
        return "closed"
    raise MigrationError(f"milestone state must be open/active or closed, got {value!r}")


def normalize_milestone_due_date(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    match = re.match(r"^\d{4}-\d{2}-\d{2}", text)
    if not match:
        raise MigrationError(f"milestone due date must start with YYYY-MM-DD, got {value!r}")
    return match.group(0)


def normalize_milestone(milestone: dict[str, Any]) -> dict[str, str]:
    title = str(milestone.get("title") or "").strip()
    if not title:
        raise MigrationError("milestone is missing a title")
    description = milestone.get("description")
    due_value = milestone.get("due_date")
    if due_value is None:
        due_value = milestone.get("due_on")
    if due_value is None:
        due_value = milestone.get("deadline")
    return {
        "title": title,
        "description": "" if description is None else str(description),
        "state": normalize_milestone_state(milestone.get("state")),
        "due_date": normalize_milestone_due_date(due_value),
    }


def normalized_milestones(milestones: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized = [normalize_milestone(milestone) for milestone in milestones]
    return sorted(normalized, key=lambda item: item["title"].casefold())


def milestone_digest(milestones: list[dict[str, str]]) -> str:
    payload = json.dumps(milestones, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def milestone_timestamp(due_date: str) -> str | None:
    if not due_date:
        return None
    return f"{due_date}T00:00:00Z"


def provider_milestone_payload(
    target: ApiTarget,
    milestone: dict[str, str],
    existing_milestone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if target.provider == "gitlab":
        payload: dict[str, Any] = {
            "title": milestone["title"],
            "description": milestone["description"],
            "due_date": milestone["due_date"] or None,
        }
        if existing_milestone is not None:
            existing_state = normalize_milestone_state(existing_milestone.get("state"))
            if milestone["state"] != existing_state:
                payload["state_event"] = "close" if milestone["state"] == "closed" else "activate"
        return payload
    if target.provider == "forgejo":
        return {
            "title": milestone["title"],
            "description": milestone["description"],
            "state": milestone["state"],
            "deadline": milestone_timestamp(milestone["due_date"]),
        }
    return {
        "title": milestone["title"],
        "description": milestone["description"],
        "state": milestone["state"],
        "due_on": milestone_timestamp(milestone["due_date"]),
    }


def milestone_update_path(target: ApiTarget, existing_milestone: dict[str, Any], milestone: dict[str, str]) -> str:
    base = repo_api_base(target)
    if target.provider == "github":
        milestone_id = existing_milestone.get("number") or existing_milestone.get("id") or milestone["title"]
    else:
        milestone_id = existing_milestone.get("id") or existing_milestone.get("iid") or milestone["title"]
    return f"{base}/milestones/{quote(str(milestone_id), safe='')}"


def create_milestone(target: ApiTarget, milestone: dict[str, str]) -> dict[str, Any]:
    payload = provider_milestone_payload(target, milestone)
    if target.provider == "gitlab":
        initial_payload = {key: value for key, value in payload.items() if key != "state_event"}
        created = api_request(
            target,
            "POST",
            f"{repo_api_base(target)}/milestones",
            body=initial_payload,
            expected=(200, 201),
        )
        if milestone["state"] == "closed":
            update_milestone(target, created, milestone)
        return created if isinstance(created, dict) else {}
    created = api_request(
        target,
        "POST",
        f"{repo_api_base(target)}/milestones",
        body=payload,
        expected=(200, 201),
    )
    return created if isinstance(created, dict) else {}


def update_milestone(target: ApiTarget, existing_milestone: dict[str, Any], milestone: dict[str, str]) -> None:
    method = "PUT" if target.provider == "gitlab" else "PATCH"
    api_request(
        target,
        method,
        milestone_update_path(target, existing_milestone, milestone),
        body=provider_milestone_payload(target, milestone, existing_milestone=existing_milestone),
        expected=(200,),
    )


def compare_milestone_sets(
    source_milestones: list[dict[str, str]],
    destination_milestones: list[dict[str, str]],
) -> dict[str, Any]:
    source_by_title = {milestone["title"]: milestone for milestone in source_milestones}
    destination_by_title = {milestone["title"]: milestone for milestone in destination_milestones}
    missing = sorted(set(source_by_title) - set(destination_by_title), key=str.casefold)
    mismatched = sorted(
        title
        for title in set(source_by_title) & set(destination_by_title)
        if source_by_title[title] != destination_by_title[title]
    )
    extra = sorted(set(destination_by_title) - set(source_by_title), key=str.casefold)
    replicated_destination_milestones = sorted(
        (destination_by_title[title] for title in source_by_title if title in destination_by_title),
        key=lambda item: item["title"].casefold(),
    )
    return {
        "verified": not missing and not mismatched,
        "source_count": len(source_milestones),
        "destination_count": len(destination_milestones),
        "source_digest": milestone_digest(source_milestones),
        "destination_digest": milestone_digest(replicated_destination_milestones),
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
    }


def migrate_milestones(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "milestones")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    source_milestones = normalized_milestones(list_milestones(source))
    destination_before_raw = list_milestones(destination)
    destination_before = normalized_milestones(destination_before_raw)
    raw_by_title = {normalize_milestone(milestone)["title"]: milestone for milestone in destination_before_raw}
    destination_by_title = {milestone["title"]: milestone for milestone in destination_before}
    created = 0
    updated = 0
    for milestone in source_milestones:
        existing = destination_by_title.get(milestone["title"])
        if existing is None:
            create_milestone(destination, milestone)
            created += 1
        elif existing != milestone:
            update_milestone(destination, raw_by_title[milestone["title"]], milestone)
            updated += 1
    comparison = poll_verified_comparison(
        lambda: compare_milestone_sets(
            source_milestones, normalized_milestones(list_milestones(destination))
        )
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "created": created,
        "updated": updated,
        **comparison,
    }


def verify_milestones(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "milestones")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    comparison = compare_milestone_sets(
        normalized_milestones(list_milestones(source)),
        normalized_milestones(list_milestones(destination)),
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        **comparison,
    }


def list_releases(target: ApiTarget) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    base = repo_api_base(target)
    for page in range(1, 1001):
        payload = api_request(
            target,
            "GET",
            f"{base}/releases",
            query={"per_page": 100, "page": page},
            expected=(200,),
        )
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} releases API returned a non-list response")
        releases.extend(payload)
        if len(payload) < 100:
            break
    return releases


def normalize_release(release: dict[str, Any]) -> dict[str, str]:
    tag_name = str(release.get("tag_name") or "").strip()
    if not tag_name:
        raise MigrationError("release is missing a tag_name")
    name = release.get("name")
    if name is None:
        name = release.get("title")
    body = release.get("body")
    if body is None:
        body = release.get("description")
    return {
        "tag_name": tag_name,
        "name": str(name or tag_name),
        "body": "" if body is None else str(body),
    }


def normalized_releases(releases: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized = [normalize_release(release) for release in releases]
    return sorted(normalized, key=lambda item: item["tag_name"].casefold())


def release_digest(releases: list[dict[str, str]]) -> str:
    payload = json.dumps(releases, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def provider_release_payload(target: ApiTarget, release: dict[str, str], create: bool) -> dict[str, Any]:
    if target.provider == "gitlab":
        payload: dict[str, Any] = {
            "name": release["name"],
            "description": release["body"],
        }
        if create:
            payload["tag_name"] = release["tag_name"]
        return payload
    return {
        "tag_name": release["tag_name"],
        "name": release["name"],
        "body": release["body"],
        "draft": False,
        "prerelease": False,
    }


def release_update_path(target: ApiTarget, existing_release: dict[str, Any], release: dict[str, str]) -> str:
    base = repo_api_base(target)
    if target.provider == "gitlab":
        release_id = release["tag_name"]
    else:
        release_id = existing_release.get("id")
        if release_id is None:
            raise MigrationError(
                f"{target.provider} release {release['tag_name']!r} is missing its API id"
            )
    return f"{base}/releases/{quote(str(release_id), safe='')}"


def create_release(target: ApiTarget, release: dict[str, str]) -> dict[str, Any]:
    created = api_request(
        target,
        "POST",
        f"{repo_api_base(target)}/releases",
        body=provider_release_payload(target, release, create=True),
        expected=(200, 201),
    )
    if not isinstance(created, dict):
        raise MigrationError(f"{target.provider} release create API returned a non-object response")
    return created


def update_release(target: ApiTarget, existing_release: dict[str, Any], release: dict[str, str]) -> None:
    method = "PUT" if target.provider == "gitlab" else "PATCH"
    api_request(
        target,
        method,
        release_update_path(target, existing_release, release),
        body=provider_release_payload(target, release, create=False),
        expected=(200,),
    )


def compare_release_sets(
    source_releases: list[dict[str, str]],
    destination_releases: list[dict[str, str]],
) -> dict[str, Any]:
    source_by_tag = {release["tag_name"]: release for release in source_releases}
    destination_by_tag = {release["tag_name"]: release for release in destination_releases}
    missing = sorted(set(source_by_tag) - set(destination_by_tag), key=str.casefold)
    mismatched = sorted(
        tag_name
        for tag_name in set(source_by_tag) & set(destination_by_tag)
        if source_by_tag[tag_name] != destination_by_tag[tag_name]
    )
    extra = sorted(set(destination_by_tag) - set(source_by_tag), key=str.casefold)
    replicated_destination_releases = sorted(
        (destination_by_tag[tag_name] for tag_name in source_by_tag if tag_name in destination_by_tag),
        key=lambda item: item["tag_name"].casefold(),
    )
    return {
        "verified": not missing and not mismatched,
        "source_count": len(source_releases),
        "destination_count": len(destination_releases),
        "source_digest": release_digest(source_releases),
        "destination_digest": release_digest(replicated_destination_releases),
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
    }


def migrate_releases(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "releases")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    source_releases = normalized_releases(list_releases(source))
    destination_before_raw = list_releases(destination)
    destination_before = normalized_releases(destination_before_raw)
    raw_by_tag = {
        normalize_release(release)["tag_name"]: release for release in destination_before_raw
    }
    destination_by_tag = {release["tag_name"]: release for release in destination_before}
    created = 0
    updated = 0
    for release in source_releases:
        existing = destination_by_tag.get(release["tag_name"])
        if existing is None:
            create_release(destination, release)
            created += 1
        elif existing != release:
            update_release(destination, raw_by_tag[release["tag_name"]], release)
            updated += 1
    comparison = poll_verified_comparison(
        lambda: compare_release_sets(
            source_releases, normalized_releases(list_releases(destination))
        )
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "created": created,
        "updated": updated,
        **comparison,
    }


def verify_releases(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "releases")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    comparison = compare_release_sets(
        normalized_releases(list_releases(source)),
        normalized_releases(list_releases(destination)),
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        **comparison,
    }


def list_issues(target: ApiTarget) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    base = repo_api_base(target)
    for page in range(1, 1001):
        payload = api_request(
            target,
            "GET",
            f"{base}/issues",
            query={"state": "all", "per_page": 100, "page": page},
            expected=(200,),
        )
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} issues API returned a non-list response")
        for issue in payload:
            if isinstance(issue, dict) and issue.get("pull_request"):
                continue
            issues.append(issue)
        if len(payload) < 100:
            break
    return issues


def issue_api_id(target: ApiTarget, issue: dict[str, Any]) -> str:
    if target.provider == "gitlab":
        value = issue.get("iid") or issue.get("number") or issue.get("id")
    else:
        value = issue.get("number") or issue.get("index") or issue.get("id")
    if value is None or str(value).strip() == "":
        raise MigrationError(f"{target.provider} issue is missing a stable API id: {issue}")
    return str(value)


def list_issue_comments(target: ApiTarget, issue: dict[str, Any]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    base = repo_api_base(target)
    issue_id = quote(issue_api_id(target, issue), safe="")
    if target.provider == "gitlab":
        path = f"{base}/issues/{issue_id}/notes"
        query: dict[str, Any] = {
            "sort": "asc",
            "order_by": "created_at",
            "per_page": 100,
        }
    else:
        path = f"{base}/issues/{issue_id}/comments"
        query = {"per_page": 100}
        if target.provider == "github":
            query.update({"sort": "created", "direction": "asc"})
    for page in range(1, 1001):
        query["page"] = page
        payload = api_request(target, "GET", path, query=query, expected=(200,))
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} issue comments API returned a non-list response")
        comments.extend(comment for comment in payload if not (isinstance(comment, dict) and comment.get("system")))
        if len(payload) < 100:
            break
    return comments


def normalize_issue_state(value: Any) -> str:
    state = str(value or "open").strip().lower()
    if state in {"open", "opened"}:
        return "open"
    if state == "closed":
        return "closed"
    raise MigrationError(f"issue state must be open/opened or closed, got {value!r}")


def normalize_issue_body(issue: dict[str, Any]) -> str:
    value = issue.get("body")
    if value is None:
        value = issue.get("description")
    return "" if value is None else str(value)


def normalize_issue_labels(value: Any) -> list[str]:
    labels: list[str] = []
    if value is None:
        return labels
    if isinstance(value, str):
        candidates: list[Any] = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        candidates = value
    else:
        raise MigrationError(f"issue labels must be a string or list, got {type(value).__name__}")
    for label in candidates:
        if isinstance(label, dict):
            name = str(label.get("name") or "").strip()
        else:
            name = str(label or "").strip()
        if name:
            labels.append(name)
    return sorted(set(labels), key=str.casefold)


def normalize_issue_milestone(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("title") or "").strip()
    return str(value or "").strip()


def normalize_comments(comments: list[dict[str, Any]]) -> list[str]:
    bodies: list[str] = []
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if body is not None:
            bodies.append(str(body))
    return bodies


def normalize_issue(issue: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    title = str(issue.get("title") or "").strip()
    if not title:
        raise MigrationError("issue is missing a title")
    return {
        "title": title,
        "body": normalize_issue_body(issue),
        "state": normalize_issue_state(issue.get("state")),
        "labels": normalize_issue_labels(issue.get("labels")),
        "milestone": normalize_issue_milestone(issue.get("milestone")),
        "comments": normalize_comments(comments),
    }


def normalized_issues(target: ApiTarget) -> list[dict[str, Any]]:
    normalized = [normalize_issue(issue, list_issue_comments(target, issue)) for issue in list_issues(target)]
    return sorted(normalized, key=lambda item: item["title"].casefold())


def issue_digest(issues: list[dict[str, Any]]) -> str:
    payload = json.dumps(issues, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def issues_by_title(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_title: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for issue in issues:
        title = issue["title"]
        if title in by_title:
            duplicates.add(title)
        by_title[title] = issue
    if duplicates:
        details = ", ".join(sorted(duplicates, key=str.casefold))
        raise MigrationError(f"duplicate issue titles cannot be proven by the portable importer: {details}")
    return by_title


def issue_core(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": issue["labels"],
        "milestone": issue["milestone"],
    }


def missing_comment_bodies(source_comments: list[str], destination_comments: list[str]) -> list[str]:
    missing: list[str] = []
    destination_index = 0
    for source_body in source_comments:
        found = False
        while destination_index < len(destination_comments):
            if destination_comments[destination_index] == source_body:
                destination_index += 1
                found = True
                break
            destination_index += 1
        if not found:
            missing.append(source_body)
    return missing


def replicated_issue_view(source_issue: dict[str, Any], destination_issue: dict[str, Any]) -> dict[str, Any]:
    view = issue_core(destination_issue)
    destination_comments = destination_issue["comments"]
    matched_comments: list[str] = []
    destination_index = 0
    for source_body in source_issue["comments"]:
        while destination_index < len(destination_comments):
            if destination_comments[destination_index] == source_body:
                matched_comments.append(destination_comments[destination_index])
                destination_index += 1
                break
            destination_index += 1
    view["comments"] = matched_comments
    return view


def compare_issue_sets(source_issues: list[dict[str, Any]], destination_issues: list[dict[str, Any]]) -> dict[str, Any]:
    source_by_title = issues_by_title(source_issues)
    destination_by_title = issues_by_title(destination_issues)
    missing = sorted(set(source_by_title) - set(destination_by_title), key=str.casefold)
    extra = sorted(set(destination_by_title) - set(source_by_title), key=str.casefold)
    mismatched: list[str] = []
    missing_comment_counts: dict[str, int] = {}
    extra_comment_counts: dict[str, int] = {}
    replicated_destination_issues: list[dict[str, Any]] = []

    for title in sorted(set(source_by_title) & set(destination_by_title), key=str.casefold):
        source_issue = source_by_title[title]
        destination_issue = destination_by_title[title]
        comment_missing = missing_comment_bodies(source_issue["comments"], destination_issue["comments"])
        if issue_core(source_issue) != issue_core(destination_issue) or comment_missing:
            mismatched.append(title)
        if comment_missing:
            missing_comment_counts[title] = len(comment_missing)
        extra_comments = max(0, len(destination_issue["comments"]) - len(source_issue["comments"]))
        if extra_comments:
            extra_comment_counts[title] = extra_comments
        replicated_destination_issues.append(replicated_issue_view(source_issue, destination_issue))

    replicated_destination_issues = sorted(replicated_destination_issues, key=lambda item: item["title"].casefold())
    return {
        "verified": not missing and not mismatched,
        "source_count": len(source_issues),
        "destination_count": len(destination_issues),
        "source_digest": issue_digest(source_issues),
        "destination_digest": issue_digest(replicated_destination_issues),
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
        "missing_comment_counts": missing_comment_counts,
        "extra_comment_counts": extra_comment_counts,
    }


def destination_label_maps(target: ApiTarget) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    raw_by_name: dict[str, dict[str, Any]] = {}
    id_by_name: dict[str, int] = {}
    for label in list_labels(target):
        normalized = normalize_label(label)
        raw_by_name[normalized["name"]] = label
        label_id = label.get("id")
        if label_id is not None:
            id_by_name[normalized["name"]] = int(label_id)
    return raw_by_name, id_by_name


def destination_milestone_maps(target: ApiTarget) -> dict[str, dict[str, Any]]:
    raw_by_title: dict[str, dict[str, Any]] = {}
    for milestone in list_milestones(target):
        raw_by_title[normalize_milestone(milestone)["title"]] = milestone
    return raw_by_title


def issue_label_payload(target: ApiTarget, issue: dict[str, Any], label_id_by_name: dict[str, int]) -> Any:
    labels = issue["labels"]
    if target.provider == "gitlab":
        return ",".join(labels)
    if target.provider == "forgejo":
        missing = [label for label in labels if label not in label_id_by_name]
        if missing:
            raise MigrationError(
                f"Forgejo issue migration requires destination label IDs; missing labels: {', '.join(missing)}"
            )
        return [label_id_by_name[label] for label in labels]
    return labels


def milestone_payload_id(target: ApiTarget, issue: dict[str, Any], milestone_by_title: dict[str, dict[str, Any]]) -> Any:
    title = issue["milestone"]
    if not title:
        return None
    milestone = milestone_by_title.get(title)
    if milestone is None:
        raise MigrationError(f"issue {issue['title']!r} references missing destination milestone {title!r}")
    if target.provider == "github":
        return milestone.get("number") or milestone.get("id")
    if target.provider == "gitlab":
        return milestone.get("id") or milestone.get("iid")
    return milestone.get("id") or milestone.get("number")


def provider_issue_payload(
    target: ApiTarget,
    issue: dict[str, Any],
    label_id_by_name: dict[str, int],
    milestone_by_title: dict[str, dict[str, Any]],
    existing_issue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    milestone_id = milestone_payload_id(target, issue, milestone_by_title)
    if target.provider == "gitlab":
        payload: dict[str, Any] = {
            "title": issue["title"],
            "description": issue["body"],
            "labels": issue_label_payload(target, issue, label_id_by_name),
        }
        if milestone_id is not None:
            payload["milestone_id"] = milestone_id
        if existing_issue is not None and issue["state"] != normalize_issue_state(existing_issue.get("state")):
            payload["state_event"] = "close" if issue["state"] == "closed" else "reopen"
        return payload

    payload = {
        "title": issue["title"],
        "body": issue["body"],
        "labels": issue_label_payload(target, issue, label_id_by_name),
    }
    if milestone_id is not None:
        payload["milestone"] = milestone_id
    if existing_issue is not None:
        payload["state"] = issue["state"]
    return payload


def issue_update_path(target: ApiTarget, existing_issue: dict[str, Any]) -> str:
    return f"{repo_api_base(target)}/issues/{quote(issue_api_id(target, existing_issue), safe='')}"


def create_issue(
    target: ApiTarget,
    issue: dict[str, Any],
    label_id_by_name: dict[str, int],
    milestone_by_title: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    created = api_request(
        target,
        "POST",
        f"{repo_api_base(target)}/issues",
        body=provider_issue_payload(target, issue, label_id_by_name, milestone_by_title),
        expected=(200, 201),
    )
    if not isinstance(created, dict):
        raise MigrationError(f"{target.provider} issue create API returned a non-object response")
    if issue["state"] == "closed":
        update_issue(target, created, issue, label_id_by_name, milestone_by_title)
    return created


def update_issue(
    target: ApiTarget,
    existing_issue: dict[str, Any],
    issue: dict[str, Any],
    label_id_by_name: dict[str, int],
    milestone_by_title: dict[str, dict[str, Any]],
) -> None:
    method = "PUT" if target.provider == "gitlab" else "PATCH"
    api_request(
        target,
        method,
        issue_update_path(target, existing_issue),
        body=provider_issue_payload(
            target,
            issue,
            label_id_by_name,
            milestone_by_title,
            existing_issue=existing_issue,
        ),
        expected=(200,),
    )


def create_issue_comment(target: ApiTarget, issue: dict[str, Any], body: str) -> None:
    base = repo_api_base(target)
    issue_id = quote(issue_api_id(target, issue), safe="")
    if target.provider == "gitlab":
        path = f"{base}/issues/{issue_id}/notes"
    else:
        path = f"{base}/issues/{issue_id}/comments"
    api_request(target, "POST", path, body={"body": body}, expected=(200, 201))


def migrate_issues(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "issues")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    source_issues = normalized_issues(source)
    destination_before_raw = list_issues(destination)
    destination_before = [
        normalize_issue(issue, list_issue_comments(destination, issue)) for issue in destination_before_raw
    ]
    raw_by_title = {
        normalize_issue(issue, list_issue_comments(destination, issue))["title"]: issue
        for issue in destination_before_raw
    }
    destination_by_title = issues_by_title(destination_before)
    _, label_id_by_name = destination_label_maps(destination)
    milestone_by_title = destination_milestone_maps(destination)

    created = 0
    updated = 0
    comments_created = 0
    for issue in source_issues:
        existing = destination_by_title.get(issue["title"])
        if existing is None:
            created_issue = create_issue(destination, issue, label_id_by_name, milestone_by_title)
            created += 1
            for comment_body in issue["comments"]:
                create_issue_comment(destination, created_issue, comment_body)
                comments_created += 1
            continue
        existing_raw = raw_by_title[issue["title"]]
        if issue_core(existing) != issue_core(issue):
            update_issue(destination, existing_raw, issue, label_id_by_name, milestone_by_title)
            updated += 1
        destination_comments = normalize_comments(list_issue_comments(destination, existing_raw))
        for comment_body in missing_comment_bodies(issue["comments"], destination_comments):
            create_issue_comment(destination, existing_raw, comment_body)
            comments_created += 1

    comparison = poll_verified_comparison(
        lambda: compare_issue_sets(source_issues, normalized_issues(destination))
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "created": created,
        "updated": updated,
        "comments_created": comments_created,
        **comparison,
    }


def verify_issues(repo: RepoPlan) -> dict[str, Any]:
    mode = metadata_mode(repo, "issues")
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    comparison = compare_issue_sets(normalized_issues(source), normalized_issues(destination))
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        **comparison,
    }


def change_request_surface(repo: RepoPlan) -> str:
    return "merge_requests" if repo.source_provider == "gitlab" else "pull_requests"


def change_request_mode(repo: RepoPlan) -> str:
    expected_surface = change_request_surface(repo)
    other_surface = "pull_requests" if expected_surface == "merge_requests" else "merge_requests"
    expected_mode = metadata_mode(repo, expected_surface)
    other_mode = metadata_mode(repo, other_surface)
    if other_mode != "skip":
        raise MigrationError(
            f"{repo.name}: {other_surface} cannot be selected for a {repo.source_provider} source; "
            f"use {expected_surface}"
        )
    return expected_mode


def change_request_resource(target: ApiTarget) -> str:
    return "merge_requests" if target.provider == "gitlab" else "pulls"


def list_change_requests(target: ApiTarget) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    base = repo_api_base(target)
    resource = change_request_resource(target)
    for page in range(1, 1001):
        query: dict[str, Any] = {"state": "all", "per_page": 100, "page": page}
        if target.provider == "gitlab":
            query["scope"] = "all"
        payload = api_request(target, "GET", f"{base}/{resource}", query=query, expected=(200,))
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} {resource} API returned a non-list response")
        requests.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            break
    return requests


def change_request_api_id(target: ApiTarget, request: dict[str, Any]) -> str:
    if target.provider == "gitlab":
        value = request.get("iid") or request.get("number") or request.get("id")
    else:
        value = request.get("number") or request.get("index") or request.get("id")
    if value is None or str(value).strip() == "":
        raise MigrationError(f"{target.provider} change request is missing a stable API id: {request}")
    return str(value)


def list_change_request_comments(target: ApiTarget, request: dict[str, Any]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    base = repo_api_base(target)
    request_id = quote(change_request_api_id(target, request), safe="")
    if target.provider == "gitlab":
        path = f"{base}/merge_requests/{request_id}/notes"
        query: dict[str, Any] = {"sort": "asc", "order_by": "created_at", "per_page": 100}
    else:
        path = f"{base}/issues/{request_id}/comments"
        query = {"per_page": 100}
        if target.provider == "github":
            query.update({"sort": "created", "direction": "asc"})
    for page in range(1, 1001):
        query["page"] = page
        payload = api_request(target, "GET", path, query=query, expected=(200,))
        if not isinstance(payload, list):
            raise MigrationError(f"{target.provider} change request comments API returned a non-list response")
        comments.extend(comment for comment in payload if not (isinstance(comment, dict) and comment.get("system")))
        if len(payload) < 100:
            break
    return comments


def normalize_change_request_state(request: dict[str, Any]) -> str:
    state = str(request.get("state") or "open").strip().lower()
    if state in {"open", "opened"}:
        return "open"
    if state == "closed" and not request.get("merged"):
        return "closed"
    if state == "merged" or request.get("merged"):
        raise MigrationError(
            "merged pull or merge requests cannot be recreated without rewriting destination Git history; "
            "keep this surface skipped or use a provider-native archival export"
        )
    raise MigrationError(f"change request state must be open/opened or closed, got {state!r}")


def normalize_change_request_branch(request: dict[str, Any], provider: str, side: str) -> str:
    if provider == "gitlab":
        value = request.get("source_branch" if side == "source" else "target_branch")
    else:
        branch = request.get("head" if side == "source" else "base")
        value = branch.get("ref") if isinstance(branch, dict) else ""
    normalized = str(value or "").strip()
    if not normalized:
        raise MigrationError(f"{provider} change request is missing its {side} branch")
    return normalized


def require_same_repository_change_request(target: ApiTarget, request: dict[str, Any]) -> None:
    if target.provider == "gitlab":
        source_project = request.get("source_project_id")
        target_project = request.get("target_project_id")
        if source_project is not None and target_project is not None and str(source_project) != str(target_project):
            raise MigrationError("fork-originated GitLab merge requests are not portable through a repository mirror")
        return
    head = request.get("head")
    head_repository = head.get("repo") if isinstance(head, dict) else None
    if not isinstance(head_repository, dict):
        raise MigrationError(
            "pull request source repository is unavailable, so same-repository migration cannot be proven"
        )
    source_repository = str(
        head_repository.get("full_name") or head_repository.get("path_with_namespace") or ""
    ).strip("/")
    if not source_repository:
        raise MigrationError("pull request source repository is missing its stable full_name")
    if source_repository.casefold() != target.repository.strip("/").casefold():
        raise MigrationError("fork-originated pull requests are not portable through a repository mirror")


def normalize_change_request(target: ApiTarget, request: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    title = str(request.get("title") or "").strip()
    if not title:
        raise MigrationError("change request is missing a title")
    if request.get("draft") or request.get("work_in_progress"):
        raise MigrationError("draft pull or merge requests are not portable across all supported providers")
    require_same_repository_change_request(target, request)
    return {
        "title": title,
        "body": normalize_issue_body(request),
        "state": normalize_change_request_state(request),
        "source_branch": normalize_change_request_branch(request, target.provider, "source"),
        "target_branch": normalize_change_request_branch(request, target.provider, "target"),
        "labels": normalize_issue_labels(request.get("labels")),
        "milestone": normalize_issue_milestone(request.get("milestone")),
        "comments": normalize_comments(comments),
    }


def normalized_change_requests(target: ApiTarget) -> list[dict[str, Any]]:
    normalized = [
        normalize_change_request(target, request, list_change_request_comments(target, request))
        for request in list_change_requests(target)
    ]
    return sorted(normalized, key=lambda item: change_request_key(item).casefold())


def change_request_key(request: dict[str, Any]) -> str:
    return f"{request['source_branch']}->{request['target_branch']}:{request['title']}"


def change_request_digest(requests: list[dict[str, Any]]) -> str:
    payload = json.dumps(requests, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def change_requests_by_key(requests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for request in requests:
        key = change_request_key(request)
        if key in by_key:
            duplicates.add(key)
        by_key[key] = request
    if duplicates:
        details = ", ".join(sorted(duplicates, key=str.casefold))
        raise MigrationError(f"duplicate change request keys cannot be proven by the portable importer: {details}")
    return by_key


def change_request_core(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": request["title"],
        "body": request["body"],
        "state": request["state"],
        "source_branch": request["source_branch"],
        "target_branch": request["target_branch"],
        "labels": request["labels"],
        "milestone": request["milestone"],
    }


def replicated_change_request_view(
    source_request: dict[str, Any], destination_request: dict[str, Any]
) -> dict[str, Any]:
    view = change_request_core(destination_request)
    destination_comments = destination_request["comments"]
    matched_comments: list[str] = []
    destination_index = 0
    for source_body in source_request["comments"]:
        while destination_index < len(destination_comments):
            if destination_comments[destination_index] == source_body:
                matched_comments.append(destination_comments[destination_index])
                destination_index += 1
                break
            destination_index += 1
    view["comments"] = matched_comments
    return view


def compare_change_request_sets(
    source_requests: list[dict[str, Any]], destination_requests: list[dict[str, Any]]
) -> dict[str, Any]:
    source_by_key = change_requests_by_key(source_requests)
    destination_by_key = change_requests_by_key(destination_requests)
    missing = sorted(set(source_by_key) - set(destination_by_key), key=str.casefold)
    extra = sorted(set(destination_by_key) - set(source_by_key), key=str.casefold)
    mismatched: list[str] = []
    missing_comment_counts: dict[str, int] = {}
    extra_comment_counts: dict[str, int] = {}
    replicated: list[dict[str, Any]] = []
    for key in sorted(set(source_by_key) & set(destination_by_key), key=str.casefold):
        source_request = source_by_key[key]
        destination_request = destination_by_key[key]
        comment_missing = missing_comment_bodies(source_request["comments"], destination_request["comments"])
        if change_request_core(source_request) != change_request_core(destination_request) or comment_missing:
            mismatched.append(key)
        if comment_missing:
            missing_comment_counts[key] = len(comment_missing)
        extra_comments = max(0, len(destination_request["comments"]) - len(source_request["comments"]))
        if extra_comments:
            extra_comment_counts[key] = extra_comments
        replicated.append(replicated_change_request_view(source_request, destination_request))
    replicated = sorted(replicated, key=lambda item: change_request_key(item).casefold())
    return {
        "verified": not missing and not mismatched,
        "source_count": len(source_requests),
        "destination_count": len(destination_requests),
        "source_digest": change_request_digest(source_requests),
        "destination_digest": change_request_digest(replicated),
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
        "missing_comment_counts": missing_comment_counts,
        "extra_comment_counts": extra_comment_counts,
    }


def change_request_payload(
    target: ApiTarget,
    request: dict[str, Any],
    label_id_by_name: dict[str, int],
    milestone_by_title: dict[str, dict[str, Any]],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    milestone_id = milestone_payload_id(target, request, milestone_by_title)
    if target.provider == "gitlab":
        payload: dict[str, Any] = {
            "source_branch": request["source_branch"],
            "target_branch": request["target_branch"],
            "title": request["title"],
            "description": request["body"],
            "labels": issue_label_payload(target, request, label_id_by_name),
        }
        if milestone_id is not None:
            payload["milestone_id"] = milestone_id
        if existing is not None and request["state"] != normalize_change_request_state(existing):
            payload["state_event"] = "close" if request["state"] == "closed" else "reopen"
        return payload
    if existing is None:
        return {
            "title": request["title"],
            "body": request["body"],
            "head": request["source_branch"],
            "base": request["target_branch"],
        }
    payload = {
        "title": request["title"],
        "body": request["body"],
        "base": request["target_branch"],
        "state": request["state"],
    }
    if existing is not None:
        existing_source = normalize_change_request_branch(existing, target.provider, "source")
        if existing_source != request["source_branch"]:
            raise MigrationError(
                f"cannot retarget existing {target.provider} pull request from {existing_source!r} "
                f"to {request['source_branch']!r}"
            )
    return payload


def change_request_update_path(target: ApiTarget, existing: dict[str, Any]) -> str:
    resource = change_request_resource(target)
    return f"{repo_api_base(target)}/{resource}/{quote(change_request_api_id(target, existing), safe='')}"


def create_change_request(
    target: ApiTarget,
    request: dict[str, Any],
    label_id_by_name: dict[str, int],
    milestone_by_title: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    created = api_request(
        target,
        "POST",
        f"{repo_api_base(target)}/{change_request_resource(target)}",
        body=change_request_payload(target, request, label_id_by_name, milestone_by_title),
        expected=(200, 201),
    )
    if not isinstance(created, dict):
        raise MigrationError(f"{target.provider} change request create API returned a non-object response")
    if target.provider != "gitlab":
        update_issue(target, created, request, label_id_by_name, milestone_by_title)
    if request["state"] == "closed":
        update_change_request(target, created, request, label_id_by_name, milestone_by_title)
    return created


def update_change_request(
    target: ApiTarget,
    existing: dict[str, Any],
    request: dict[str, Any],
    label_id_by_name: dict[str, int],
    milestone_by_title: dict[str, dict[str, Any]],
) -> None:
    method = "PUT" if target.provider == "gitlab" else "PATCH"
    api_request(
        target,
        method,
        change_request_update_path(target, existing),
        body=change_request_payload(target, request, label_id_by_name, milestone_by_title, existing=existing),
        expected=(200,),
    )
    if target.provider != "gitlab":
        update_issue(target, existing, request, label_id_by_name, milestone_by_title)


def create_change_request_comment(target: ApiTarget, request: dict[str, Any], body: str) -> None:
    base = repo_api_base(target)
    request_id = quote(change_request_api_id(target, request), safe="")
    path = (
        f"{base}/merge_requests/{request_id}/notes"
        if target.provider == "gitlab"
        else f"{base}/issues/{request_id}/comments"
    )
    api_request(target, "POST", path, body={"body": body}, expected=(200, 201))


def migrate_change_requests(repo: RepoPlan) -> dict[str, Any]:
    mode = change_request_mode(repo)
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    source_requests = normalized_change_requests(source)
    destination_before_raw = list_change_requests(destination)
    destination_before = [
        normalize_change_request(destination, request, list_change_request_comments(destination, request))
        for request in destination_before_raw
    ]
    raw_by_key = {
        change_request_key(normalize_change_request(destination, request, list_change_request_comments(destination, request))): request
        for request in destination_before_raw
    }
    destination_by_key = change_requests_by_key(destination_before)
    _, label_id_by_name = destination_label_maps(destination)
    milestone_by_title = destination_milestone_maps(destination)
    created = 0
    updated = 0
    comments_created = 0
    for request in source_requests:
        key = change_request_key(request)
        existing = destination_by_key.get(key)
        if existing is None:
            created_request = create_change_request(destination, request, label_id_by_name, milestone_by_title)
            created += 1
            for comment_body in request["comments"]:
                create_change_request_comment(destination, created_request, comment_body)
                comments_created += 1
            continue
        existing_raw = raw_by_key[key]
        if change_request_core(existing) != change_request_core(request):
            update_change_request(destination, existing_raw, request, label_id_by_name, milestone_by_title)
            updated += 1
        destination_comments = normalize_comments(list_change_request_comments(destination, existing_raw))
        for comment_body in missing_comment_bodies(request["comments"], destination_comments):
            create_change_request_comment(destination, existing_raw, comment_body)
            comments_created += 1
    comparison = poll_verified_comparison(
        lambda: compare_change_request_sets(
            source_requests, normalized_change_requests(destination)
        )
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        "created": created,
        "updated": updated,
        "comments_created": comments_created,
        **comparison,
    }


def verify_change_requests(repo: RepoPlan) -> dict[str, Any]:
    mode = change_request_mode(repo)
    if mode == "skip":
        return {"mode": mode, "status": "skipped", "verified": True}
    source = api_target(repo, "source")
    destination = api_target(repo, "destination")
    comparison = compare_change_request_sets(
        normalized_change_requests(source), normalized_change_requests(destination)
    )
    return {
        "mode": mode,
        "status": "verified" if comparison["verified"] else "failed",
        "source_provider": source.provider,
        "destination_provider": destination.provider,
        **comparison,
    }


def migrate_metadata(repo: RepoPlan) -> dict[str, Any]:
    branch_protection = migrate_branch_protections(repo)
    labels = migrate_labels(repo)
    milestones = migrate_milestones(repo)
    releases = migrate_releases(repo)
    issues = migrate_issues(repo)
    change_requests = migrate_change_requests(repo)
    return {
        "branch_protection": branch_protection,
        "labels": labels,
        "milestones": milestones,
        "releases": releases,
        "issues": issues,
        "change_requests": change_requests,
        "verified": branch_protection.get("verified", False)
        and labels.get("verified", False)
        and milestones.get("verified", False)
        and releases.get("verified", False)
        and issues.get("verified", False)
        and change_requests.get("verified", False),
    }


def verify_metadata(repo: RepoPlan) -> dict[str, Any]:
    branch_protection = verify_branch_protections(repo)
    labels = verify_labels(repo)
    milestones = verify_milestones(repo)
    releases = verify_releases(repo)
    issues = verify_issues(repo)
    change_requests = verify_change_requests(repo)
    return {
        "branch_protection": branch_protection,
        "labels": labels,
        "milestones": milestones,
        "releases": releases,
        "issues": issues,
        "change_requests": change_requests,
        "verified": branch_protection.get("verified", False)
        and labels.get("verified", False)
        and milestones.get("verified", False)
        and releases.get("verified", False)
        and issues.get("verified", False)
        and change_requests.get("verified", False),
    }


def ls_remote_refs(url: str) -> tuple[dict[str, str], str | None]:
    result = git(
        [
            "ls-remote",
            "--refs",
            url,
            "refs/heads/*",
            "refs/tags/*",
            "refs/notes/*",
        ],
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or f"git ls-remote failed for {redact_url(url)}"
        return {}, error.replace(url, redact_url(url))
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sha, ref = parts
            refs[ref] = sha
    return refs, None


def ls_remote_default_branch(url: str) -> tuple[str | None, str | None]:
    result = git(["ls-remote", "--symref", url, "HEAD"], check=False)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or (
            f"git ls-remote --symref failed for {redact_url(url)}"
        )
        return None, error.replace(url, redact_url(url))
    for line in result.stdout.splitlines():
        match = re.match(r"^ref:\s+(refs/heads/\S+)\s+HEAD$", line.strip())
        if match:
            return match.group(1), None
    return None, None


def compare_refs(source_url: str, destination_url: str) -> dict[str, Any]:
    source_refs, source_error = ls_remote_refs(source_url)
    if source_error:
        raise MigrationError(f"cannot read source refs {redact_url(source_url)}: {source_error}")
    destination_refs, destination_error = ls_remote_refs(destination_url)
    if destination_error:
        raise MigrationError(f"cannot read destination refs {redact_url(destination_url)}: {destination_error}")
    missing = sorted(set(source_refs) - set(destination_refs))
    extra = sorted(set(destination_refs) - set(source_refs))
    mismatched = sorted(
        ref for ref in set(source_refs) & set(destination_refs) if source_refs[ref] != destination_refs[ref]
    )
    source_default_branch, source_default_error = ls_remote_default_branch(source_url)
    if source_default_error:
        raise MigrationError(
            f"cannot read source default branch {redact_url(source_url)}: {source_default_error}"
        )
    destination_default_branch, destination_default_error = ls_remote_default_branch(destination_url)
    if destination_default_error:
        raise MigrationError(
            f"cannot read destination default branch {redact_url(destination_url)}: "
            f"{destination_default_error}"
        )
    default_branch_verified = (
        source_default_branch is None or source_default_branch == destination_default_branch
    )
    branches = [ref for ref in source_refs if ref.startswith("refs/heads/")]
    tags = [ref for ref in source_refs if ref.startswith("refs/tags/")]
    notes = [ref for ref in source_refs if ref.startswith("refs/notes/")]
    source_ref_digest = hashlib.sha256(
        json.dumps(source_refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    destination_ref_digest = hashlib.sha256(
        json.dumps(destination_refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "verified": not missing and not extra and not mismatched and default_branch_verified,
        "source_ref_count": len(source_refs),
        "destination_ref_count": len(destination_refs),
        "branch_count": len(branches),
        "tag_count": len(tags),
        "note_ref_count": len(notes),
        "source_ref_digest": source_ref_digest,
        "destination_ref_digest": destination_ref_digest,
        "source_default_branch": source_default_branch,
        "destination_default_branch": destination_default_branch,
        "default_branch_verified": default_branch_verified,
        "missing_refs": missing,
        "extra_refs": extra,
        "mismatched_refs": mismatched,
    }


def prepare_mirror(repo: RepoPlan, work_dir: Path) -> Path:
    repo_root = work_dir / safe_name(repo.name)
    mirror = repo_root / "repository.git"
    resolved_work = work_dir.resolve()
    if mirror.exists():
        resolved_mirror = mirror.resolve()
        if resolved_work not in (resolved_mirror, *resolved_mirror.parents):
            raise MigrationError(f"refusing to remove mirror outside work dir: {mirror}")
        shutil.rmtree(mirror)
    repo_root.mkdir(parents=True, exist_ok=True)
    git(["clone", "--mirror", repo.source_url, str(mirror)])
    git(["fsck", "--full"], cwd=mirror)
    return mirror


def push_mirror(mirror: Path, destination_url: str) -> None:
    git(
        [
            "push",
            "--prune",
            destination_url,
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
            "+refs/notes/*:refs/notes/*",
        ],
        cwd=mirror,
    )


def migrate_lfs(repo: RepoPlan, mirror: Path) -> dict[str, Any]:
    if repo.lfs == "false":
        return {"mode": "false", "transfer_status": "skipped"}
    if not command_exists("git-lfs"):
        if repo.lfs == "required":
            raise MigrationError(f"{repo.name}: git-lfs is required but not installed")
        return {"mode": repo.lfs, "transfer_status": "skipped-no-git-lfs"}
    git(["lfs", "fetch", "--all"], cwd=mirror)
    git(["lfs", "push", "--all", repo.destination_url], cwd=mirror)
    return {"mode": repo.lfs, "transfer_status": "pushed"}


def lfs_object_ids(mirror: Path) -> list[str]:
    result = git(["lfs", "ls-files", "--all", "--long"], cwd=mirror)
    object_ids: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\b", line.strip())
        if match:
            object_ids.add(match.group(1).lower())
    return sorted(object_ids)


def lfs_object_digest(object_ids: list[str]) -> str:
    payload = json.dumps(object_ids, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clone_mirror_for_verification(url: str, path: Path, work_dir: Path) -> Path:
    if path.exists():
        resolved_work = work_dir.resolve()
        resolved_path = path.resolve()
        if resolved_work not in (resolved_path, *resolved_path.parents):
            raise MigrationError(f"refusing to remove verification mirror outside work dir: {path}")
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--mirror", url, str(path)])
    return path


def verify_lfs(
    repo: RepoPlan,
    work_dir: Path,
    source_mirror: Path | None = None,
) -> dict[str, Any]:
    if repo.lfs == "false":
        return {"mode": "false", "status": "skipped", "verified": True}
    if not command_exists("git-lfs"):
        if repo.lfs == "required":
            raise MigrationError(f"{repo.name}: git-lfs is required but not installed")
        return {
            "mode": repo.lfs,
            "status": "skipped-no-git-lfs",
            "verified": True,
            "accepted_skip": True,
        }
    verification_root = work_dir / safe_name(repo.name) / "lfs-verification"
    if source_mirror is None:
        source_mirror = clone_mirror_for_verification(
            repo.source_url,
            verification_root / "source.git",
            work_dir,
        )
    destination_mirror = clone_mirror_for_verification(
        repo.destination_url,
        verification_root / "destination.git",
        work_dir,
    )
    git(["lfs", "fetch", "--all"], cwd=source_mirror)
    git(["lfs", "fsck"], cwd=source_mirror)
    git(["lfs", "fetch", "--all"], cwd=destination_mirror)
    git(["lfs", "fsck"], cwd=destination_mirror)
    source_objects = lfs_object_ids(source_mirror)
    destination_objects = lfs_object_ids(destination_mirror)
    missing = sorted(set(source_objects) - set(destination_objects))
    extra = sorted(set(destination_objects) - set(source_objects))
    verified = not missing and not extra
    return {
        "mode": repo.lfs,
        "status": "verified" if verified else "failed",
        "source_object_count": len(source_objects),
        "destination_object_count": len(destination_objects),
        "source_object_digest": lfs_object_digest(source_objects),
        "destination_object_digest": lfs_object_digest(destination_objects),
        "missing_object_ids": missing,
        "extra_object_ids": extra,
        "verified": verified,
    }


def migrate_wiki(repo: RepoPlan, work_dir: Path) -> dict[str, Any]:
    if repo.wiki == "false":
        return {"mode": "false", "status": "skipped", "verified": True}
    source_wiki = repo.source_wiki_url or derive_wiki_url(repo.source_url)
    destination_wiki = repo.destination_wiki_url or derive_wiki_url(repo.destination_url)
    refs, error = ls_remote_refs(source_wiki)
    if error or not refs:
        if repo.wiki == "required":
            raise MigrationError(f"{repo.name}: required wiki source is not readable: {error or 'no refs'}")
        return {"mode": repo.wiki, "status": "skipped-no-source-wiki", "verified": True}

    wiki_repo = RepoPlan(
        name=f"{repo.name}-wiki",
        source_url=source_wiki,
        destination_url=destination_wiki,
        source_wiki_url=None,
        destination_wiki_url=None,
        source_provider=repo.source_provider,
        destination_provider=repo.destination_provider,
        source_api_url=None,
        destination_api_url=None,
        source_api_repository=None,
        destination_api_repository=None,
        source_token_env=None,
        destination_token_env=None,
        destination_create="false",
        destination_private=True,
        destination_description="",
        destination_namespace_id=None,
        wiki="false",
        lfs="false",
        metadata={},
    )
    mirror = prepare_mirror(wiki_repo, work_dir)
    push_mirror(mirror, destination_wiki)
    verification = compare_refs(source_wiki, destination_wiki)
    if not verification["verified"]:
        raise MigrationError(f"{repo.name}: wiki refs did not verify after push")
    return {"mode": repo.wiki, "status": "verified", "source_url": redact_url(source_wiki), **verification}


def verify_wiki(repo: RepoPlan) -> dict[str, Any]:
    if repo.wiki == "false":
        return {"mode": "false", "status": "skipped", "verified": True}
    source_wiki = repo.source_wiki_url or derive_wiki_url(repo.source_url)
    destination_wiki = repo.destination_wiki_url or derive_wiki_url(repo.destination_url)
    refs, error = ls_remote_refs(source_wiki)
    if error or not refs:
        if repo.wiki == "required":
            raise MigrationError(f"{repo.name}: required wiki source is not readable: {error or 'no refs'}")
        return {"mode": repo.wiki, "status": "skipped-no-source-wiki", "verified": True}
    verification = compare_refs(source_wiki, destination_wiki)
    return {
        "mode": repo.wiki,
        "status": "verified" if verification["verified"] else "failed",
        "source_url": redact_url(source_wiki),
        "destination_url": redact_url(destination_wiki),
        **verification,
    }


def verify_repo(repo: RepoPlan) -> dict[str, Any]:
    require_supported_metadata(repo)
    destination_repository = verify_destination_repository(repo)
    verification = compare_refs(repo.source_url, repo.destination_url)
    wiki = verify_wiki(repo)
    with tempfile.TemporaryDirectory(prefix="forge-migration-lfs-verify-") as temp:
        lfs = verify_lfs(repo, Path(temp))
    metadata = verify_metadata(repo)
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "destination_repository": destination_repository,
        "git": verification,
        "wiki": wiki,
        "lfs": lfs,
        "metadata": metadata,
        "verified": destination_repository["verified"]
        and verification["verified"]
        and wiki["verified"]
        and lfs["verified"]
        and metadata["verified"],
    }


def migrate_repo(repo: RepoPlan, work_dir: Path) -> dict[str, Any]:
    require_supported_metadata(repo)
    destination_repository = ensure_destination_repository(repo)
    mirror = prepare_mirror(repo, work_dir)
    lfs_transfer = migrate_lfs(repo, mirror)
    push_mirror(mirror, repo.destination_url)
    destination_repository = reconcile_destination_default_branch(repo, destination_repository)
    verification = compare_refs(repo.source_url, repo.destination_url)
    if not verification["verified"]:
        raise MigrationError(f"{repo.name}: repository refs did not verify after push")
    lfs_result = {**lfs_transfer, **verify_lfs(repo, work_dir, source_mirror=mirror)}
    wiki_result = migrate_wiki(repo, work_dir)
    metadata = migrate_metadata(repo)
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "destination_repository": destination_repository,
        "git": verification,
        "wiki": wiki_result,
        "lfs": lfs_result,
        "metadata": metadata,
        "verified": destination_repository["verified"]
        and verification["verified"]
        and wiki_result["verified"]
        and lfs_result["verified"]
        and metadata["verified"],
    }


def build_proof(direction: str, command: str, repositories: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 2,
        "tool": "scripts/forge_migration.py",
        "command": command,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "direction": direction,
        "supported_directions": sorted(SUPPORTED_DIRECTIONS),
        "repositories": repositories,
        "verified": all(repo.get("verified") for repo in repositories),
    }


def failed_repository_result(repo: RepoPlan, error: MigrationError) -> dict[str, Any]:
    message = str(error)
    for url in (repo.source_url, repo.destination_url):
        message = message.replace(url, redact_url(url))
    for token_env in (repo.source_token_env, repo.destination_token_env):
        credential_value = os.environ.get(token_env, "") if token_env else ""
        if credential_value:
            message = message.replace(credential_value, "<redacted>")
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "status": "failed",
        "error": message,
        "verified": False,
    }


def proof_digest(proof: dict[str, Any]) -> str:
    digest_payload = {key: value for key, value in proof.items() if key != "proof_sha256"}
    canonical = json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_proof(path: Path | None, proof: dict[str, Any]) -> None:
    proof["proof_sha256"] = proof_digest(proof)
    text = json.dumps(proof, indent=2, sort_keys=True) + "\n"
    if path:
        atomic_write_text(path, text)
    else:
        print(text, end="")


def report_unverified_proof(proof: dict[str, Any]) -> None:
    """Emit a compact, credential-safe reason for a rejected proof."""

    print("forge migration verification failed:", file=sys.stderr)
    repositories = proof.get("repositories")
    if not isinstance(repositories, list):
        print(" - proof contains no repository results", file=sys.stderr)
        return
    for repository in repositories:
        if not isinstance(repository, dict) or repository.get("verified") is True:
            continue
        name = str(repository.get("name") or "unnamed")
        reasons: list[str] = []
        error = str(repository.get("error") or "").strip()
        if error:
            reasons.append(error)
        for surface in ("destination_repository", "git", "wiki", "lfs"):
            result = repository.get(surface)
            if isinstance(result, dict) and result.get("verified") is False:
                reasons.append(surface)
        metadata = repository.get("metadata")
        if isinstance(metadata, dict) and metadata.get("verified") is False:
            failed_metadata = sorted(
                key
                for key, value in metadata.items()
                if isinstance(value, dict) and value.get("verified") is False
            )
            reasons.extend(f"metadata.{surface}" for surface in failed_metadata)
        if not reasons:
            reasons.append("unverified result")
        print(f" - {name}: {', '.join(reasons)}", file=sys.stderr)


def command_validate_plan(args: argparse.Namespace) -> int:
    direction, repos = parse_plan(load_plan(args.plan))
    for repo in repos:
        validate_metadata_requirements(repo)
        destination_repository_plan(repo)
    proof = build_proof(
        direction,
        "validate-plan",
        [
            {
                "name": repo.name,
                "source_url": redact_url(repo.source_url),
                "destination_url": redact_url(repo.destination_url),
                "destination_repository": destination_repository_plan(repo),
                "wiki": repo.wiki,
                "lfs": repo.lfs,
                "metadata": validate_metadata_requirements(repo),
                "verified": True,
            }
            for repo in repos
        ],
    )
    write_proof(args.proof, proof)
    return 0


def command_verify(args: argparse.Namespace) -> int:
    direction, repos = parse_plan(load_plan(args.plan))
    results: list[dict[str, Any]] = []
    for repo in repos:
        try:
            results.append(verify_repo(repo))
        except MigrationError as exc:
            results.append(failed_repository_result(repo, exc))
    proof = build_proof(direction, "verify", results)
    write_proof(args.proof, proof)
    if not proof["verified"]:
        report_unverified_proof(proof)
        return 1
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    direction, repos = parse_plan(load_plan(args.plan))
    work_dir = args.work_dir
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if work_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="forge-migration-")
        work_dir = Path(temporary.name)
    work_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    try:
        for repo in repos:
            try:
                results.append(migrate_repo(repo, work_dir))
            except MigrationError as exc:
                results.append(failed_repository_result(repo, exc))
    finally:
        if temporary is not None:
            temporary.cleanup()
    proof = build_proof(direction, "migrate", results)
    write_proof(args.proof, proof)
    if not proof["verified"]:
        report_unverified_proof(proof)
        return 1
    return 0


def command_verify_proof(args: argparse.Namespace) -> int:
    proof = load_plan(args.proof_file)
    claimed_digest = str(proof.get("proof_sha256") or "")
    actual_digest = proof_digest(proof)
    integrity_verified = bool(claimed_digest) and claimed_digest == actual_digest
    accepted = integrity_verified and proof.get("verified") is True
    result = {
        "proof_file": str(args.proof_file),
        "proof_sha256": claimed_digest,
        "actual_sha256": actual_digest,
        "integrity_verified": integrity_verified,
        "migration_verified": proof.get("verified") is True,
        "accepted": accepted,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if accepted else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler in (
        ("validate-plan", command_validate_plan),
        ("verify", command_verify),
        ("migrate", command_migrate),
    ):
        sub = subparsers.add_parser(name)
        sub.add_argument("plan", type=Path, help="JSON migration plan")
        sub.add_argument("--proof", type=Path, help="Write JSON proof to this path")
        sub.set_defaults(handler=handler)
        if name == "migrate":
            sub.add_argument("--work-dir", type=Path, help="Scratch directory for mirror clones")
    verify_proof = subparsers.add_parser("verify-proof")
    verify_proof.add_argument("proof_file", type=Path, help="Proof JSON to verify for integrity and acceptance")
    verify_proof.set_defaults(handler=command_verify_proof)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return args.handler(args)
    except MigrationError as exc:
        print(f"forge migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
