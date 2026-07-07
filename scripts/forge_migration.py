#!/usr/bin/env python3
"""Mirror and verify Git forge migrations with machine-readable proof.

The first supported migration planes are Git refs and repository labels:
branches, tags, optional wiki/LFS repositories, and provider-common label
metadata. Other provider metadata such as issues, pull requests, merge requests,
packages, and releases is intentionally modeled in the plan and rejected when
marked required until an importer for that surface exists. That keeps
"verified migration" claims honest.
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
SUPPORTED_METADATA_SURFACES = {"labels"}
UNSUPPORTED_METADATA_SURFACES = {
    "issues",
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
    labels_mode = metadata_mode(repo, "labels")
    labels: dict[str, Any] = {"mode": labels_mode, "status": "skipped"}
    if labels_mode != "skip":
        source = api_target(repo, "source")
        destination = api_target(repo, "destination")
        labels = {
            "mode": labels_mode,
            "status": "planned",
            "source_provider": source.provider,
            "destination_provider": destination.provider,
            "source_api_url": redact_url(source.api_url),
            "destination_api_url": redact_url(destination.api_url),
            "source_repository": source.repository,
            "destination_repository": destination.repository,
        }
    return {"labels": labels, "verified": True}


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


def migrate_metadata(repo: RepoPlan) -> dict[str, Any]:
    labels = migrate_labels(repo)
    return {"labels": labels, "verified": labels.get("verified", False)}


def verify_metadata(repo: RepoPlan) -> dict[str, Any]:
    labels = verify_labels(repo)
    return {"labels": labels, "verified": labels.get("verified", False)}


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
