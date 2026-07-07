#!/usr/bin/env python3
"""Mirror and verify Git forge migrations with machine-readable proof.

The first supported migration planes are Git refs, repository labels,
repository milestones, and portable issues/comments: branches, tags, optional
wiki/LFS repositories, and provider-common metadata. Other provider metadata
such as pull requests, merge requests, packages, and releases is intentionally
modeled in the plan and rejected when marked required until an importer for
that surface exists. That keeps "verified migration" claims honest.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SUPPORTED_DIRECTIONS = {
    "github-to-forgejo",
    "gitlab-to-forgejo",
    "forgejo-to-github",
    "forgejo-to-gitlab",
}
OPTIONAL_DIRECTIONS = {
    "forgejo-to-forgejo",
    "github-to-gitlab",
    "gitlab-to-github",
}
SUPPORTED_METADATA_STATES = {"skip", "skipped", "false", "none", "not-required"}
SUPPORTED_METADATA_SURFACES = {"labels", "milestones", "issues"}
UNSUPPORTED_METADATA_SURFACES = {
    "pull_requests",
    "merge_requests",
    "releases",
    "packages",
    "project_boards",
    "wikis_metadata",
    "users",
    "teams",
    "permissions",
    "branch_protection",
    "webhooks",
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
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        command = " ".join(args)
        raise MigrationError(
            f"command failed rc={result.returncode}: {command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
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


def load_plan(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path}: invalid JSON: {exc}") from exc


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
        wiki=wiki,
        lfs=lfs,
        metadata=metadata,
    )


def parse_plan(data: dict[str, Any]) -> tuple[str, list[RepoPlan]]:
    direction = str(data.get("direction") or "").strip().lower()
    allowed = SUPPORTED_DIRECTIONS | OPTIONAL_DIRECTIONS
    if direction not in allowed:
        raise MigrationError(
            f"direction must be one of: {', '.join(sorted(SUPPORTED_DIRECTIONS))}"
        )
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise MigrationError("plan.repositories must be a non-empty list")
    parsed = [parse_repo(repo, index, direction) for index, repo in enumerate(repositories)]
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

    def planned_surface(surface: str) -> dict[str, Any]:
        nonlocal source, destination
        mode = metadata_mode(repo, surface)
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
        "labels": planned_surface("labels"),
        "milestones": planned_surface("milestones"),
        "issues": planned_surface("issues"),
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


def api_request(
    target: ApiTarget,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
) -> Any:
    url = f"{target.api_url}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, headers=api_headers(target), method=method)
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        if exc.code in expected:
            return json.loads(payload) if payload else {}
        raise MigrationError(
            f"{method} {redact_url(url)} failed with HTTP {exc.code}: {payload[:500]}"
        ) from exc
    except URLError as exc:
        raise MigrationError(f"{method} {redact_url(url)} failed: {exc}") from exc
    if status not in expected:
        raise MigrationError(f"{method} {redact_url(url)} returned HTTP {status}: {payload[:500]}")
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{method} {redact_url(url)} returned invalid JSON: {exc}") from exc


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
    destination_after = normalized_labels(list_labels(destination))
    comparison = compare_label_sets(source_labels, destination_after)
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
    destination_after = normalized_milestones(list_milestones(destination))
    comparison = compare_milestone_sets(source_milestones, destination_after)
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

    destination_after = normalized_issues(destination)
    comparison = compare_issue_sets(source_issues, destination_after)
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


def migrate_metadata(repo: RepoPlan) -> dict[str, Any]:
    labels = migrate_labels(repo)
    milestones = migrate_milestones(repo)
    issues = migrate_issues(repo)
    return {
        "labels": labels,
        "milestones": milestones,
        "issues": issues,
        "verified": labels.get("verified", False)
        and milestones.get("verified", False)
        and issues.get("verified", False),
    }


def verify_metadata(repo: RepoPlan) -> dict[str, Any]:
    labels = verify_labels(repo)
    milestones = verify_milestones(repo)
    issues = verify_issues(repo)
    return {
        "labels": labels,
        "milestones": milestones,
        "issues": issues,
        "verified": labels.get("verified", False)
        and milestones.get("verified", False)
        and issues.get("verified", False),
    }


def ls_remote_refs(url: str) -> tuple[dict[str, str], str | None]:
    result = git(["ls-remote", "--refs", "--heads", "--tags", url], check=False)
    if result.returncode != 0:
        return {}, result.stderr.strip() or result.stdout.strip() or f"git ls-remote failed for {redact_url(url)}"
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sha, ref = parts
            refs[ref] = sha
    return refs, None


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
    branches = [ref for ref in source_refs if ref.startswith("refs/heads/")]
    tags = [ref for ref in source_refs if ref.startswith("refs/tags/")]
    return {
        "verified": not missing and not extra and not mismatched,
        "source_ref_count": len(source_refs),
        "destination_ref_count": len(destination_refs),
        "branch_count": len(branches),
        "tag_count": len(tags),
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
    git(["push", "--mirror", destination_url], cwd=mirror)


def migrate_lfs(repo: RepoPlan, mirror: Path) -> dict[str, Any]:
    if repo.lfs == "false":
        return {"mode": "false", "status": "skipped"}
    if not command_exists("git-lfs"):
        if repo.lfs == "required":
            raise MigrationError(f"{repo.name}: git-lfs is required but not installed")
        return {"mode": repo.lfs, "status": "skipped-no-git-lfs"}
    git(["lfs", "fetch", "--all"], cwd=mirror)
    git(["lfs", "push", "--all", repo.destination_url], cwd=mirror)
    return {"mode": repo.lfs, "status": "pushed"}


def migrate_wiki(repo: RepoPlan, work_dir: Path) -> dict[str, Any]:
    if repo.wiki == "false":
        return {"mode": "false", "status": "skipped"}
    source_wiki = repo.source_wiki_url or derive_wiki_url(repo.source_url)
    destination_wiki = repo.destination_wiki_url or derive_wiki_url(repo.destination_url)
    refs, error = ls_remote_refs(source_wiki)
    if error or not refs:
        if repo.wiki == "required":
            raise MigrationError(f"{repo.name}: required wiki source is not readable: {error or 'no refs'}")
        return {"mode": repo.wiki, "status": "skipped-no-source-wiki"}

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


def verify_repo(repo: RepoPlan) -> dict[str, Any]:
    require_supported_metadata(repo)
    verification = compare_refs(repo.source_url, repo.destination_url)
    metadata = verify_metadata(repo)
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "git": verification,
        "wiki": {"mode": repo.wiki, "status": "not-verified-by-verify-command"},
        "lfs": {"mode": repo.lfs, "status": "not-verified-by-verify-command"},
        "metadata": metadata,
        "verified": verification["verified"] and metadata["verified"],
    }


def migrate_repo(repo: RepoPlan, work_dir: Path) -> dict[str, Any]:
    require_supported_metadata(repo)
    mirror = prepare_mirror(repo, work_dir)
    lfs_result = migrate_lfs(repo, mirror)
    push_mirror(mirror, repo.destination_url)
    verification = compare_refs(repo.source_url, repo.destination_url)
    if not verification["verified"]:
        raise MigrationError(f"{repo.name}: repository refs did not verify after push")
    wiki_result = migrate_wiki(repo, work_dir)
    metadata = migrate_metadata(repo)
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "git": verification,
        "wiki": wiki_result,
        "lfs": lfs_result,
        "metadata": metadata,
        "verified": verification["verified"] and wiki_result.get("status") != "failed" and metadata["verified"],
    }


def build_proof(direction: str, command: str, repositories: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "tool": "scripts/forge_migration.py",
        "command": command,
        "direction": direction,
        "supported_directions": sorted(SUPPORTED_DIRECTIONS),
        "repositories": repositories,
        "verified": all(repo.get("verified") for repo in repositories),
    }


def write_proof(path: Path | None, proof: dict[str, Any]) -> None:
    text = json.dumps(proof, indent=2, sort_keys=True) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def command_validate_plan(args: argparse.Namespace) -> int:
    direction, repos = parse_plan(load_plan(args.plan))
    for repo in repos:
        validate_metadata_requirements(repo)
    proof = build_proof(
        direction,
        "validate-plan",
        [
            {
                "name": repo.name,
                "source_url": redact_url(repo.source_url),
                "destination_url": redact_url(repo.destination_url),
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
    results = [verify_repo(repo) for repo in repos]
    proof = build_proof(direction, "verify", results)
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


def command_migrate(args: argparse.Namespace) -> int:
    direction, repos = parse_plan(load_plan(args.plan))
    work_dir = args.work_dir
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if work_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="forge-migration-")
        work_dir = Path(temporary.name)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        results = [migrate_repo(repo, work_dir) for repo in repos]
    finally:
        if temporary is not None:
            temporary.cleanup()
    proof = build_proof(direction, "migrate", results)
    write_proof(args.proof, proof)
    return 0 if proof["verified"] else 1


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
