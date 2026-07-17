#!/usr/bin/env python3
"""Run an opt-in live acceptance suite for the verified forge migration contract.

The regular migration helper has deterministic local tests. This runner adds
evidence from real GitHub, GitLab, and Forgejo APIs by creating isolated private
repositories, seeding the portable migration surface, exercising every supported
direction, and writing redacted proof artifacts. It never contacts a provider
unless both ``--run`` and ``FORGE_MIGRATION_LIVE=1`` are supplied.
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
import sys
import tempfile
import uuid
from typing import Any, Mapping
from urllib.parse import quote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from forge_migration import (  # noqa: E402
    ApiTarget,
    MigrationError,
    RepoPlan,
    api_request,
    api_target,
    authenticated_login,
    build_proof,
    create_destination_repository,
    create_issue,
    create_issue_comment,
    create_label,
    create_milestone,
    create_release,
    destination_label_maps,
    destination_milestone_maps,
    git,
    migrate_repo,
    parse_plan,
    proof_digest,
    repo_api_base,
    repository_probe,
    verify_repo,
    write_proof,
)


SUPPORTED_DIRECTIONS = (
    "github-to-forgejo",
    "gitlab-to-forgejo",
    "forgejo-to-github",
    "forgejo-to-gitlab",
)
PROVIDER_DEFAULTS = {
    "github": {
        "api_url": "https://api.github.com",
        "token_env": "GITHUB_TOKEN",
    },
    "gitlab": {
        "api_url": "https://gitlab.com/api/v4",
        "token_env": "GITLAB_TOKEN",
    },
    "forgejo": {
        "api_url": "",
        "token_env": "FORGEJO_TOKEN",
    },
}
PORTABLE_METADATA = {
    "labels": "required",
    "milestones": "required",
    "releases": "required",
    "issues": "required",
    "pull_requests": "skip",
    "merge_requests": "skip",
    "release_assets": "skip",
    "packages": "skip",
    "project_boards": "skip",
    "wikis_metadata": "skip",
    "users": "skip",
    "teams": "skip",
    "permissions": "skip",
    "branch_protection": "skip",
    "webhooks": "skip",
}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    api_url: str
    namespace: str
    token_env: str


def environment_text(environment: Mapping[str, str], name: str, default: str = "") -> str:
    return str(environment.get(name, default)).strip()


def load_configuration(environment: Mapping[str, str]) -> dict[str, ProviderConfig]:
    configs: dict[str, ProviderConfig] = {}
    for provider, defaults in PROVIDER_DEFAULTS.items():
        prefix = f"FORGE_MIGRATION_LIVE_{provider.upper()}"
        configs[provider] = ProviderConfig(
            provider=provider,
            api_url=environment_text(environment, f"{prefix}_API_URL", defaults["api_url"]),
            namespace=environment_text(environment, f"{prefix}_NAMESPACE"),
            token_env=environment_text(environment, f"{prefix}_TOKEN_ENV", defaults["token_env"]),
        )
    return configs


def validate_configuration(configs: Mapping[str, ProviderConfig], environment: Mapping[str, str], require_tokens: bool) -> None:
    problems: list[str] = []
    for provider in ("github", "gitlab", "forgejo"):
        config = configs[provider]
        parsed = urlsplit(config.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            problems.append(f"{provider}: FORGE_MIGRATION_LIVE_{provider.upper()}_API_URL must be an HTTP(S) URL")
        if not config.namespace.strip("/"):
            problems.append(f"{provider}: FORGE_MIGRATION_LIVE_{provider.upper()}_NAMESPACE is required")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.token_env):
            problems.append(f"{provider}: token environment variable name {config.token_env!r} is invalid")
        elif require_tokens and not environment_text(environment, config.token_env):
            problems.append(f"{provider}: environment variable {config.token_env} is required")
    if problems:
        raise MigrationError("live migration configuration is incomplete:\n - " + "\n - ".join(problems))


def repository_name(config: ProviderConfig, slug: str) -> str:
    return f"{config.namespace.strip('/')}/{slug}"


def git_base_url(config: ProviderConfig) -> str:
    parts = urlsplit(config.api_url)
    path = parts.path.rstrip("/")
    if config.provider == "github":
        if parts.netloc.casefold() == "api.github.com":
            return "https://github.com"
        if path.endswith("/api/v3"):
            path = path[: -len("/api/v3")]
    elif config.provider == "gitlab" and path.endswith("/api/v4"):
        path = path[: -len("/api/v4")]
    elif config.provider == "forgejo" and path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))


def authenticated_git_url(config: ProviderConfig, repository: str, login: str, token: str) -> str:
    base = urlsplit(git_base_url(config))
    username = {
        "github": "x-access-token",
        "gitlab": "oauth2",
        "forgejo": login,
    }[config.provider]
    userinfo = f"{quote(username, safe='')}:{quote(token, safe='')}"
    path = f"{base.path.rstrip('/')}/{repository.strip('/')}.git"
    return urlunsplit((base.scheme, f"{userinfo}@{base.netloc}", path, "", ""))


def make_plan(
    direction: str,
    name: str,
    source: ProviderConfig,
    destination: ProviderConfig,
    source_url: str,
    destination_url: str,
    source_repository: str,
    destination_repository: str,
) -> RepoPlan:
    _direction, repositories = parse_plan(
        {
            "direction": direction,
            "repositories": [
                {
                    "name": name,
                    "source": {
                        "url": source_url,
                        "api_url": source.api_url,
                        "api_repository": source_repository,
                        "token_env": source.token_env,
                    },
                    "destination": {
                        "url": destination_url,
                        "api_url": destination.api_url,
                        "api_repository": destination_repository,
                        "token_env": destination.token_env,
                        "create": "required",
                        "private": True,
                        "description": "Disposable verified forge migration acceptance repository",
                    },
                    "wiki": False,
                    "lfs": False,
                    "metadata": PORTABLE_METADATA,
                }
            ],
        }
    )
    return repositories[0]


def ensure_absent(target: ApiTarget, expected_prefix: str) -> None:
    leaf = target.repository.strip("/").split("/")[-1]
    if not leaf.startswith(f"{expected_prefix}-"):
        raise MigrationError(
            f"refusing to use repository {target.repository!r}: it does not start with {expected_prefix!r}"
        )
    status, _payload = repository_probe(target)
    if status != 404:
        raise MigrationError(
            f"refusing to reuse existing live acceptance repository {target.repository!r}; choose another run id"
        )


def create_source_repository(repo: RepoPlan, prefix: str) -> ApiTarget:
    source = api_target(repo, "source")
    ensure_absent(source, prefix)
    create_destination_repository(repo, source)
    status, _payload = repository_probe(source)
    if status != 200:
        raise MigrationError(f"source repository {source.repository!r} was not readable after creation")
    return source


def seed_git_repository(source_url: str, work_dir: Path) -> None:
    work = work_dir / "seed"
    git(["init", str(work)])
    git(["config", "user.email", "forge-migration-live@example.invalid"], cwd=work)
    git(["config", "user.name", "Forge Migration Live Acceptance"], cwd=work)
    (work / "README.md").write_text("# live migration acceptance\n", encoding="utf-8")
    git(["add", "README.md"], cwd=work)
    git(["commit", "-m", "Seed live migration acceptance repository"], cwd=work)
    git(["branch", "-M", "main"], cwd=work)
    git(["remote", "add", "origin", source_url], cwd=work)
    git(["push", "--set-upstream", "origin", "main"], cwd=work)
    git(["checkout", "-b", "feature/live-proof"], cwd=work)
    (work / "feature.txt").write_text("portable feature branch\n", encoding="utf-8")
    git(["add", "feature.txt"], cwd=work)
    git(["commit", "-m", "Add portable feature branch"], cwd=work)
    git(["push", "origin", "feature/live-proof"], cwd=work)
    git(["checkout", "main"], cwd=work)
    git(["tag", "-a", "v1.0.0", "-m", "Live acceptance release"], cwd=work)
    git(["push", "origin", "v1.0.0"], cwd=work)
    git(["notes", "add", "-m", "live migration proof note"], cwd=work)
    git(["push", "origin", "refs/notes/commits:refs/notes/commits"], cwd=work)


def set_default_branch(target: ApiTarget, branch: str) -> None:
    method = "PUT" if target.provider == "gitlab" else "PATCH"
    api_request(
        target,
        method,
        repo_api_base(target),
        body={"default_branch": branch},
        expected=(200,),
    )


def seed_portable_metadata(target: ApiTarget) -> None:
    labels = [
        {"name": "live-proof", "color": "0e8a16", "description": "Live migration acceptance"},
        {"name": "migration", "color": "0366d6", "description": "Portable migration contract"},
    ]
    milestones = [
        {
            "title": "Live acceptance milestone",
            "description": "Portable milestone proof",
            "state": "open",
            "due_date": "2030-01-01",
        }
    ]
    release = {
        "tag_name": "v1.0.0",
        "name": "Live acceptance release",
        "body": "Portable release body for live migration verification.",
    }
    issue = {
        "title": "Live migration acceptance issue",
        "body": "Portable issue body for cross-forge verification.",
        "state": "closed",
        "labels": ["live-proof", "migration"],
        "milestone": "Live acceptance milestone",
    }
    for label in labels:
        create_label(target, label)
    for milestone in milestones:
        create_milestone(target, milestone)
    create_release(target, release)
    _labels, label_ids = destination_label_maps(target)
    milestone_map = destination_milestone_maps(target)
    created = create_issue(target, issue, label_ids, milestone_map)
    create_issue_comment(target, created, "First portable live migration comment.")
    create_issue_comment(target, created, "Second portable live migration comment.")


def delete_repository(target: ApiTarget, expected_prefix: str) -> dict[str, Any]:
    leaf = target.repository.strip("/").split("/")[-1]
    if not leaf.startswith(f"{expected_prefix}-"):
        raise MigrationError(
            f"refusing to delete repository {target.repository!r}: unexpected live acceptance prefix"
        )
    response = api_request(
        target,
        "DELETE",
        repo_api_base(target),
        expected=(202, 204),
        return_status=True,
    )
    if not isinstance(response, tuple):
        raise MigrationError(f"{target.provider} delete response for {target.repository!r} did not include a status")
    return {"provider": target.provider, "repository": target.repository, "status": int(response[0])}


def direction_specs(configs: Mapping[str, ProviderConfig], prefix: str, run_id: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for direction in SUPPORTED_DIRECTIONS:
        source_name, destination_name = direction.split("-to-", 1)
        source = configs[source_name]
        destination = configs[destination_name]
        source_slug = f"{prefix}-{run_id}-{source_name}-source"
        destination_slug = f"{prefix}-{run_id}-{destination_name}-destination"
        specs.append(
            {
                "direction": direction,
                "source": source,
                "destination": destination,
                "source_repository": repository_name(source, source_slug),
                "destination_repository": repository_name(destination, destination_slug),
            }
        )
    return specs


def dry_run_manifest(configs: Mapping[str, ProviderConfig], prefix: str, run_id: str, environment: Mapping[str, str]) -> dict[str, Any]:
    return {
        "version": 1,
        "tool": "scripts/forge_migration_live.py",
        "command": "dry-run",
        "run_id": run_id,
        "prefix": prefix,
        "providers": {
            name: {
                "api_url": config.api_url,
                "namespace": config.namespace,
                "token_env": config.token_env,
                "token_present": bool(environment_text(environment, config.token_env)),
            }
            for name, config in sorted(configs.items())
        },
        "directions": [
            {
                "direction": spec["direction"],
                "source_repository": spec["source_repository"],
                "destination_repository": spec["destination_repository"],
                "metadata": PORTABLE_METADATA,
                "wiki": False,
                "lfs": False,
            }
            for spec in direction_specs(configs, prefix, run_id)
        ],
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_acceptance(configs: Mapping[str, ProviderConfig], prefix: str, run_id: str, output_dir: Path, cleanup: bool) -> int:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise MigrationError(f"refusing to overwrite existing live migration evidence directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    logins: dict[str, str] = {}
    credentials: dict[str, str] = {}
    for provider, config in configs.items():
        credential = os.environ.get(config.token_env, "")
        credentials[provider] = credential
        target = ApiTarget(provider=config.provider, api_url=config.api_url.rstrip("/"), repository="placeholder/repository", token_env=config.token_env)
        logins[provider] = authenticated_login(target)

    direction_results: list[dict[str, Any]] = []
    cleanup_targets: list[ApiTarget] = []
    with tempfile.TemporaryDirectory(prefix="forge-migration-live-") as temp_name:
        temporary_root = Path(temp_name)
        for spec in direction_specs(configs, prefix, run_id):
            direction = str(spec["direction"])
            source = spec["source"]
            destination = spec["destination"]
            source_repository = str(spec["source_repository"])
            destination_repository = str(spec["destination_repository"])
            source_url = authenticated_git_url(source, source_repository, logins[source.provider], credentials[source.provider])
            destination_url = authenticated_git_url(destination, destination_repository, logins[destination.provider], credentials[destination.provider])
            repo = make_plan(
                direction,
                f"live-{direction}",
                source,
                destination,
                source_url,
                destination_url,
                source_repository,
                destination_repository,
            )
            source_target = create_source_repository(repo, prefix)
            destination_target = api_target(repo, "destination")
            ensure_absent(destination_target, prefix)
            cleanup_targets.extend((source_target, destination_target))
            direction_work = temporary_root / direction
            seed_git_repository(source_url, direction_work)
            set_default_branch(source_target, "main")
            seed_portable_metadata(source_target)
            migrated = migrate_repo(repo, direction_work / "migration")
            migrate_proof = build_proof(direction, "migrate", [migrated])
            migrate_path = output_dir / f"{direction}.migrate.proof.json"
            write_proof(migrate_path, migrate_proof)
            verified = verify_repo(repo)
            verify_proof = build_proof(direction, "verify", [verified])
            verify_path = output_dir / f"{direction}.verify.proof.json"
            write_proof(verify_path, verify_proof)
            direction_results.append(
                {
                    "direction": direction,
                    "source_provider": source.provider,
                    "destination_provider": destination.provider,
                    "source_repository": source_repository,
                    "destination_repository": destination_repository,
                    "migrate_proof": migrate_path.name,
                    "migrate_proof_sha256": migrate_proof["proof_sha256"],
                    "verify_proof": verify_path.name,
                    "verify_proof_sha256": verify_proof["proof_sha256"],
                    "verified": bool(migrate_proof["verified"] and verify_proof["verified"]),
                }
            )

    cleanup_results: list[dict[str, Any]] = []
    if cleanup and all(result["verified"] for result in direction_results):
        for target in reversed(cleanup_targets):
            cleanup_results.append(delete_repository(target, prefix))

    proof = {
        "version": 1,
        "tool": "scripts/forge_migration_live.py",
        "command": "live-acceptance",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_id": run_id,
        "prefix": prefix,
        "portable_contract": {
            "git_refs": ["branches", "tags", "notes", "default_branch"],
            "metadata": ["labels", "milestones", "releases", "issues", "comments"],
            "excluded": sorted(key for key, value in PORTABLE_METADATA.items() if value == "skip"),
        },
        "directions": direction_results,
        "cleanup": {"requested": cleanup, "results": cleanup_results},
        "verified": len(direction_results) == len(SUPPORTED_DIRECTIONS)
        and all(result["verified"] for result in direction_results),
    }
    proof["proof_sha256"] = proof_digest(proof)
    write_json(output_dir / "live-acceptance.proof.json", proof)
    return 0 if proof["verified"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the redacted four-direction acceptance manifest (default).")
    mode.add_argument("--run", action="store_true", help="Create live repositories and execute the acceptance suite.")
    parser.add_argument("--output-dir", type=Path, help="Private directory for redacted proof artifacts; required with --run.")
    parser.add_argument("--run-id", help="Unique safe suffix for disposable repositories. Generated when omitted.")
    parser.add_argument("--prefix", default="platform-migration-live", help="Required safe prefix for every created repository.")
    parser.add_argument("--cleanup", action="store_true", help="Delete only successful disposable repositories bearing --prefix after proof is written.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prefix = str(args.prefix).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,48}", prefix):
        print("forge migration live acceptance failed: --prefix must be 3-49 lowercase letters, digits, or hyphens", file=sys.stderr)
        return 2
    run_id = re.sub(r"[^a-z0-9-]+", "-", str(args.run_id or uuid.uuid4().hex[:12]).lower()).strip("-")
    if not run_id:
        print("forge migration live acceptance failed: --run-id produced an empty repository suffix", file=sys.stderr)
        return 2
    configs = load_configuration(os.environ)
    try:
        if not args.run:
            validate_configuration(configs, os.environ, require_tokens=False)
            print(json.dumps(dry_run_manifest(configs, prefix, run_id, os.environ), indent=2, sort_keys=True))
            return 0
        if str(os.environ.get("FORGE_MIGRATION_LIVE", "")).strip().lower() not in {"1", "true", "yes"}:
            raise MigrationError("set FORGE_MIGRATION_LIVE=1 with --run before any provider is contacted")
        if args.output_dir is None:
            raise MigrationError("--output-dir is required with --run so proof evidence is retained privately")
        validate_configuration(configs, os.environ, require_tokens=True)
        return run_acceptance(configs, prefix, run_id, args.output_dir, args.cleanup)
    except MigrationError as exc:
        print(f"forge migration live acceptance failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
