#!/usr/bin/env python3
"""Plan or apply the mutable GitHub release-governance controls."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_VERSION = "2026-03-10"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_SECURITY_CONTROLS = (
    "dependabot_security_updates",
    "secret_scanning",
    "secret_scanning_push_protection",
    "secret_scanning_non_provider_patterns",
    "secret_scanning_validity_checks",
)
REQUIRED_TAG_RULES = ("creation", "update", "deletion", "non_fast_forward")
PREMIUM_SECRET_CONTROLS = (
    "secret_scanning_non_provider_patterns",
    "secret_scanning_validity_checks",
)
NOT_FOUND = object()


class ConfigurationError(ValueError):
    """Raised when a governance change cannot be applied safely."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a JSON object")
    return value


class GitHubApi:
    """Small GitHub REST client with a secure gh fallback for local CA issues."""

    def __init__(self, *, api_url: str, token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token

    def _gh_request(
        self,
        method: str,
        path: str,
        payload: Any,
        not_found: Any,
    ) -> Any:
        if shutil.which("gh") is None:
            raise ConfigurationError("GitHub API TLS transport failed and gh is not installed")
        environment = os.environ.copy()
        environment["GH_TOKEN"] = self.token
        command = [
            "gh",
            "api",
            "--method",
            method,
            path,
            "--header",
            f"X-GitHub-Api-Version: {API_VERSION}",
        ]
        request_input = None
        if payload is not None:
            command.extend(["--input", "-"])
            request_input = json.dumps(payload)
        result = subprocess.run(
            command,
            input=request_input,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
        if result.returncode != 0:
            if "HTTP 404" in result.stderr and not_found is not NOT_FOUND:
                return not_found
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
            raise ConfigurationError(f"gh api request failed for {path}: {detail}")
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"gh api returned invalid JSON: {path}") from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        not_found: Any = NOT_FOUND,
    ) -> Any:
        encoded = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.api_url}/{path.lstrip('/')}",
            data=encoded,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "platform-gitops-governance-configurator",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except HTTPError as exc:
            if exc.code == 404 and not_found is not NOT_FOUND:
                return not_found
            raise ConfigurationError(
                f"GitHub API request failed with HTTP {exc.code}: {path}"
            ) from exc
        except URLError as exc:
            if self.api_url == "https://api.github.com":
                return self._gh_request(method, path, payload, not_found)
            raise ConfigurationError(f"GitHub API request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"GitHub API returned invalid JSON: {path}") from exc

    def get(self, path: str, *, not_found: Any = NOT_FOUND) -> Any:
        return self.request("GET", path, not_found=not_found)

    def patch(self, path: str, payload: Any) -> Any:
        return self.request("PATCH", path, payload=payload)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload=payload)

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, payload=payload)


def security_patch(repository_document: Any) -> dict[str, Any]:
    repository = require_object(repository_document, "repository")
    security = require_object(repository.get("security_and_analysis"), "security_and_analysis")
    changes = {
        name: {"status": "enabled"}
        for name in REQUIRED_SECURITY_CONTROLS
        if not isinstance(security.get(name), dict)
        or security[name].get("status") != "enabled"
    }
    return {"security_and_analysis": changes} if changes else {}


def unavailable_security_controls(repository_document: Any) -> tuple[str, ...]:
    repository = require_object(repository_document, "repository")
    owner = require_object(repository.get("owner"), "repository owner")
    if owner.get("type") != "User":
        return ()
    changes = security_patch(repository).get("security_and_analysis", {})
    return tuple(control for control in PREMIUM_SECRET_CONTROLS if control in changes)


def require_security_readback(repository_document: Any) -> None:
    remaining = security_patch(repository_document).get("security_and_analysis", {})
    if remaining:
        raise ConfigurationError(
            "GitHub did not enable requested security controls: "
            + ", ".join(sorted(remaining))
        )


def merge_tag_ruleset(
    existing: Any,
    *,
    name: str,
    tag_ref_pattern: str,
    release_authority_id: int,
) -> dict[str, Any]:
    current = deepcopy(existing) if isinstance(existing, dict) else {}
    if current and current.get("target") not in (None, "tag"):
        raise ConfigurationError(f"managed ruleset name is already used by a non-tag ruleset: {name}")

    conditions = deepcopy(current.get("conditions")) if isinstance(current.get("conditions"), dict) else {}
    ref_name = deepcopy(conditions.get("ref_name")) if isinstance(conditions.get("ref_name"), dict) else {}
    includes = [str(value) for value in ref_name.get("include", []) if value]
    if tag_ref_pattern not in includes:
        includes.append(tag_ref_pattern)
    conditions["ref_name"] = {
        "include": includes,
        "exclude": [str(value) for value in ref_name.get("exclude", []) if value],
    }

    rules = [deepcopy(rule) for rule in current.get("rules", []) if isinstance(rule, dict)]
    rule_types = {str(rule.get("type")) for rule in rules}
    rules.extend({"type": rule_type} for rule_type in REQUIRED_TAG_RULES if rule_type not in rule_types)

    bypass = [
        deepcopy(actor)
        for actor in current.get("bypass_actors", [])
        if isinstance(actor, dict)
    ]
    authority = {
        "actor_id": release_authority_id,
        "actor_type": "User",
        "bypass_mode": "always",
    }
    if not any(
        actor.get("actor_id") == release_authority_id
        and actor.get("actor_type") == "User"
        and actor.get("bypass_mode") == "always"
        for actor in bypass
    ):
        bypass.append(authority)

    return {
        "name": name,
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": bypass,
        "conditions": conditions,
        "rules": rules,
    }


def ruleset_is_current(existing: Any, desired: dict[str, Any]) -> bool:
    if not isinstance(existing, dict):
        return False
    projection = {
        key: existing.get(key)
        for key in ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")
    }
    return canonical(projection) == canonical(desired)


def extract_environment_reviewers(environment: Any) -> list[dict[str, Any]]:
    if not isinstance(environment, dict):
        return []
    reviewers: list[dict[str, Any]] = []
    for rule in environment.get("protection_rules", []):
        if not isinstance(rule, dict) or rule.get("type") != "required_reviewers":
            continue
        for item in rule.get("reviewers", []):
            if not isinstance(item, dict):
                continue
            reviewer = item.get("reviewer")
            reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else item.get("id")
            reviewer_type = item.get("type")
            if reviewer_type in ("User", "Team") and isinstance(reviewer_id, int):
                reviewers.append({"type": reviewer_type, "id": reviewer_id})
    return reviewers


def environment_payload(
    existing: Any,
    *,
    reviewer: dict[str, Any],
) -> dict[str, Any]:
    current = existing if isinstance(existing, dict) else {}
    reviewers = extract_environment_reviewers(current)
    compact_reviewer = {"type": reviewer["type"], "id": reviewer["id"]}
    if compact_reviewer not in reviewers:
        reviewers.append(compact_reviewer)
    return {
        "wait_timer": int(current.get("wait_timer") or 0),
        "prevent_self_review": True,
        "reviewers": reviewers,
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def validate_independent_reviewer(
    *,
    reviewer: dict[str, Any],
    release_authority: dict[str, Any],
    permission_document: Any,
) -> None:
    reviewer_id = reviewer.get("id")
    if not isinstance(reviewer_id, int):
        raise ConfigurationError("reviewer identity has no numeric GitHub ID")
    if reviewer_id == release_authority.get("id"):
        raise ConfigurationError("release reviewer must differ from the release authority")
    permission = require_object(permission_document, "reviewer repository permission").get("permission")
    if permission not in {"read", "triage", "write", "maintain", "admin"}:
        raise ConfigurationError("release reviewer must be a repository collaborator")


def find_managed_ruleset(rulesets: Any, name: str) -> dict[str, Any] | None:
    if not isinstance(rulesets, list):
        raise ConfigurationError("repository rulesets response must be an array")
    matches = [item for item in rulesets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) > 1:
        raise ConfigurationError(f"multiple rulesets use the managed name: {name}")
    return matches[0] if matches else None


def has_tag_policy(policies: Any, pattern: str) -> bool:
    document = require_object(policies, "deployment policies")
    items = document.get("branch_policies")
    if not isinstance(items, list):
        raise ConfigurationError("deployment policy list is invalid")
    return any(
        isinstance(item, dict)
        and item.get("name") == pattern
        and item.get("type") in (None, "tag")
        for item in items
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply-security", "apply"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--reviewer-user", default=os.environ.get("GITHUB_GOVERNANCE_REVIEWER", ""))
    parser.add_argument(
        "--release-authority-user",
        default=os.environ.get("GITHUB_RELEASE_AUTHORITY", ""),
    )
    parser.add_argument("--ruleset-name", default="platform semantic release tags")
    parser.add_argument("--tag-ref-pattern", default="refs/tags/v*.*.*")
    parser.add_argument("--environment", default="production-release")
    parser.add_argument("--environment-tag-pattern", default="v*.*.*")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("rendered/governance/github-governance-plan.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GitHub governance configuration requires GITHUB_TOKEN", file=sys.stderr)
        return 2
    if not REPOSITORY_RE.fullmatch(args.repository):
        print("GitHub governance configuration requires GITHUB_REPOSITORY=OWNER/REPOSITORY", file=sys.stderr)
        return 2

    api = GitHubApi(api_url=args.api_url, token=token)
    repository_path = quote(args.repository, safe="/")
    environment_path = quote(args.environment, safe="")
    actions: list[str] = []
    blockers: list[str] = []
    applied: list[str] = []
    try:
        repository = require_object(api.get(f"repos/{repository_path}"), "repository")
        owner = require_object(repository.get("owner"), "repository owner")
        authority_login = args.release_authority_user.strip() or str(owner.get("login") or "")
        if not authority_login:
            raise ConfigurationError("release authority user could not be determined")
        authority = require_object(
            api.get(f"users/{quote(authority_login, safe='')}"),
            "release authority",
        )

        patch = security_patch(repository)
        unavailable_controls = unavailable_security_controls(repository)
        if patch:
            controls = sorted(patch["security_and_analysis"])
            actions.append("enable security controls: " + ", ".join(controls))
            if unavailable_controls:
                blockers.append(
                    "GitHub does not offer these controls on user-owned repositories: "
                    + ", ".join(unavailable_controls)
                    + "; transfer the repository to an organization with GitHub Team "
                    "and Secret Protection to satisfy the strict governance gate"
                )
        else:
            actions.append("security controls already current")

        if args.mode == "apply-security":
            if unavailable_controls:
                raise ConfigurationError("refusing unavailable security-control apply:\n - " + "\n - ".join(blockers))
            if patch:
                api.patch(f"repos/{repository_path}", patch)
                require_security_readback(api.get(f"repos/{repository_path}"))
                applied.append("security controls")
            print("GitHub security controls applied." if applied else "GitHub security controls already current.")
            return 0

        summaries = api.get(
            f"repos/{repository_path}/rulesets?includes_parents=false&per_page=100"
        )
        managed_summary = find_managed_ruleset(summaries, args.ruleset_name)
        managed = None
        if managed_summary is not None:
            ruleset_id = managed_summary.get("id")
            if not isinstance(ruleset_id, int):
                raise ConfigurationError("managed ruleset has no numeric ID")
            managed = require_object(
                api.get(f"repos/{repository_path}/rulesets/{ruleset_id}"),
                "managed ruleset",
            )
        desired_ruleset = merge_tag_ruleset(
            managed,
            name=args.ruleset_name,
            tag_ref_pattern=args.tag_ref_pattern,
            release_authority_id=int(authority["id"]),
        )
        if ruleset_is_current(managed, desired_ruleset):
            actions.append("release tag ruleset already current")
        else:
            actions.append("create release tag ruleset" if managed is None else "update release tag ruleset")

        environment = api.get(
            f"repos/{repository_path}/environments/{environment_path}",
            not_found={},
        )
        reviewer: dict[str, Any] | None = None
        permission: Any = None
        if args.reviewer_user.strip():
            reviewer_login = args.reviewer_user.strip()
            reviewer = require_object(
                api.get(f"users/{quote(reviewer_login, safe='')}"),
                "release reviewer",
            )
            reviewer = {"type": "User", "id": reviewer.get("id"), "login": reviewer.get("login")}
            permission = api.get(
                f"repos/{repository_path}/collaborators/{quote(reviewer_login, safe='')}/permission",
                not_found={},
            )
            validate_independent_reviewer(
                reviewer=reviewer,
                release_authority=authority,
                permission_document=permission,
            )
        else:
            existing_reviewers = extract_environment_reviewers(environment)
            reviewer = next(
                (
                    item
                    for item in existing_reviewers
                    if item.get("type") == "User" and item.get("id") != authority.get("id")
                ),
                None,
            )
            if reviewer is None:
                blockers.append(
                    "add a repository collaborator and pass --reviewer-user LOGIN; "
                    "the reviewer must differ from the release authority"
                )

        desired_environment = environment_payload(environment, reviewer=reviewer) if reviewer else None
        if desired_environment is not None:
            actions.append("create or reconcile production-release reviewer gate")
        policies = api.get(
            f"repos/{repository_path}/environments/{environment_path}/deployment-branch-policies?per_page=100",
            not_found={"total_count": 0, "branch_policies": []},
        )
        if has_tag_policy(policies, args.environment_tag_pattern):
            actions.append("release environment tag policy already current")
        else:
            actions.append("create release environment tag-only policy")

        if args.mode == "apply" and blockers:
            raise ConfigurationError("refusing partial governance apply:\n - " + "\n - ".join(blockers))

        if args.mode == "apply":
            if patch:
                api.patch(f"repos/{repository_path}", patch)
                require_security_readback(api.get(f"repos/{repository_path}"))
                applied.append("security controls")
            if managed is None:
                api.post(f"repos/{repository_path}/rulesets", desired_ruleset)
                applied.append("release tag ruleset")
            elif not ruleset_is_current(managed, desired_ruleset):
                api.put(
                    f"repos/{repository_path}/rulesets/{managed['id']}",
                    desired_ruleset,
                )
                applied.append("release tag ruleset")
            assert desired_environment is not None
            api.put(
                f"repos/{repository_path}/environments/{environment_path}",
                desired_environment,
            )
            applied.append("release environment reviewer gate")
            if not has_tag_policy(policies, args.environment_tag_pattern):
                api.post(
                    f"repos/{repository_path}/environments/{environment_path}/deployment-branch-policies",
                    {"name": args.environment_tag_pattern, "type": "tag"},
                )
                applied.append("release environment tag policy")

        plan = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": args.mode,
            "repository": args.repository,
            "releaseAuthority": authority.get("login"),
            "reviewer": reviewer.get("login") if reviewer else None,
            "actions": actions,
            "blockers": blockers,
            "applied": applied,
            "residualRequirements": [
                "merge a reviewed GitHub-verified commit to the protected default branch",
                "run make github-governance-verify with a read-only audit token",
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    except (ConfigurationError, KeyError, OSError, TypeError, ValueError) as exc:
        print(f"GitHub governance configuration failed: {exc}", file=sys.stderr)
        return 1

    status = "applied" if args.mode == "apply" else "planned"
    print(
        f"GitHub governance {status}: repository={args.repository} "
        f"actions={len(actions)} blockers={len(blockers)} output={args.output}"
    )
    for blocker in blockers:
        print(f"BLOCKED: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
