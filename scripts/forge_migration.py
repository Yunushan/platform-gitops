#!/usr/bin/env python3
"""Mirror and verify Git forge migrations with machine-readable proof.

The first supported migration plane is the Git data plane: branches, tags, and
optional wiki/LFS repositories. Provider metadata such as issues, pull requests,
merge requests, packages, and releases is intentionally modeled in the plan and
rejected when marked required until an importer for that surface exists. That
keeps "verified migration" claims honest.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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
    wiki: str
    lfs: str
    metadata: dict[str, Any]


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


def parse_repo(raw: dict[str, Any], index: int) -> RepoPlan:
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
    return RepoPlan(
        name=name,
        source_url=source_url,
        destination_url=destination_url,
        source_wiki_url=source_wiki,
        destination_wiki_url=destination_wiki,
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
    parsed = [parse_repo(repo, index) for index, repo in enumerate(repositories)]
    return direction, parsed


def require_supported_metadata(repo: RepoPlan) -> list[dict[str, str]]:
    unsupported: list[dict[str, str]] = []
    for surface, state in repo.metadata.items():
        normalized = str(state).strip().lower()
        if normalized in SUPPORTED_METADATA_STATES:
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
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "git": verification,
        "wiki": {"mode": repo.wiki, "status": "not-verified-by-verify-command"},
        "lfs": {"mode": repo.lfs, "status": "not-verified-by-verify-command"},
        "verified": verification["verified"],
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
    return {
        "name": repo.name,
        "source_url": redact_url(repo.source_url),
        "destination_url": redact_url(repo.destination_url),
        "git": verification,
        "wiki": wiki_result,
        "lfs": lfs_result,
        "verified": verification["verified"] and wiki_result.get("status") != "failed",
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
        require_supported_metadata(repo)
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
