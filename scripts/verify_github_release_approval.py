#!/usr/bin/env python3
"""Require an independent GitHub environment approval for the current release run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from atomic_file import atomic_write_text
from http_transport import (
    HttpTransportPolicyError,
    http_timeout_seconds,
    read_bounded_response,
    require_bounded_text,
)
from subprocess_timeout import bounded_timeout_seconds


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
API_VERSION = "2026-03-10"
REQUIRED_TAG_RULES = {"creation", "update", "deletion", "non_fast_forward"}
GH_COMMAND_TIMEOUT_SECONDS = 30


class ReleaseApprovalError(ValueError):
    """Raised when a release run lacks an independent recorded approval."""


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseApprovalError(f"{label} must be a JSON object")
    return value


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def eligible_collaborators(document: Any) -> set[int]:
    if not isinstance(document, list):
        raise ReleaseApprovalError("repository collaborators must be a JSON array")
    eligible: set[int] = set()
    for collaborator in document:
        if not isinstance(collaborator, dict):
            continue
        identifier = collaborator.get("id")
        permissions = collaborator.get("permissions")
        role = str(collaborator.get("role_name") or "").lower()
        can_review = role in {"write", "maintain", "admin"} or (
            isinstance(permissions, dict)
            and any(permissions.get(name) is True for name in ("push", "maintain", "admin"))
        )
        if isinstance(identifier, int) and can_review:
            eligible.add(identifier)
    return eligible


def approved_environment_present(history: Any, environment_id: int, environment_name: str) -> bool:
    if not isinstance(history, list):
        return False
    for review in history:
        if not isinstance(review, dict) or str(review.get("state") or "").lower() != "approved":
            continue
        for environment in review.get("environments", []):
            if not isinstance(environment, dict):
                continue
            if environment.get("id") == environment_id and environment.get("name") == environment_name:
                return True
    return False


def validate_release_approval(
    *,
    repository: str,
    run_id: int,
    expected_sha: str,
    environment_name: str,
    tag_ref_pattern: str,
    repository_document: Any,
    run_document: Any,
    environment_document: Any,
    approval_history_document: Any,
    collaborators_document: Any,
    rulesets_document: Any,
    team_members_document: Any,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    problems: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    if not REPOSITORY_RE.fullmatch(repository):
        raise ReleaseApprovalError("repository must use OWNER/REPOSITORY syntax")
    expected_sha = expected_sha.lower()
    if not COMMIT_RE.fullmatch(expected_sha):
        raise ReleaseApprovalError("expected SHA must be a 40-character lowercase Git commit SHA")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ReleaseApprovalError("workflow run ID must be a positive integer")
    if not environment_name:
        raise ReleaseApprovalError("release environment name is required")
    if not tag_ref_pattern.startswith("refs/tags/"):
        raise ReleaseApprovalError("tag ref pattern must start with refs/tags/")

    repo = require_object(repository_document, "repository")
    require(repo.get("full_name") == repository, "repository identity does not match")
    owner = require_object(repo.get("owner"), "repository.owner")
    owner_id = owner.get("id")
    require(isinstance(owner_id, int), "repository owner ID is invalid")

    run = require_object(run_document, "workflow run")
    require(run.get("id") == run_id, "workflow run identity does not match")
    require(str(run.get("head_sha") or "").lower() == expected_sha, "workflow run commit does not match")
    require(run.get("event") == "push", "release workflow run was not triggered by a push")
    require(run.get("path") == ".github/workflows/release.yml", "workflow run path is not release.yml")
    require(
        run.get("status") in {"queued", "in_progress", "completed", "waiting", "pending"},
        "workflow run status is invalid",
    )
    require(
        run.get("run_attempt") == 1,
        "release publication requires the first workflow run attempt",
    )
    head_repository = require_object(run.get("head_repository"), "workflow run head_repository")
    require(
        head_repository.get("full_name") == repository,
        "workflow run source repository does not match",
    )

    environment = require_object(environment_document, "release environment")
    environment_id = environment.get("id")
    require(isinstance(environment_id, int), "release environment ID is invalid")
    require(environment.get("name") == environment_name, "release environment identity does not match")
    reviewer_rules = [
        rule
        for rule in environment.get("protection_rules", [])
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers"
    ]
    require(bool(reviewer_rules), "release environment has no required reviewers")
    reviewers = reviewer_rules[0].get("reviewers", []) if reviewer_rules else []
    if reviewer_rules:
        require(
            reviewer_rules[0].get("prevent_self_review") is True,
            "release environment permits self-review",
        )
        require(bool(reviewers), "release environment reviewer list is empty")

    collaborators = eligible_collaborators(collaborators_document)
    require(len(collaborators) >= 2, "repository has fewer than two review-capable collaborators")
    team_members = require_object(team_members_document, "team memberships")

    if not isinstance(rulesets_document, list):
        raise ReleaseApprovalError("repository rulesets must be a JSON array")
    matching_rulesets: list[dict[str, Any]] = []
    for ruleset in rulesets_document:
        if not isinstance(ruleset, dict):
            continue
        conditions = ruleset.get("conditions")
        ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
        includes = ref_name.get("include", []) if isinstance(ref_name, dict) else []
        if (
            ruleset.get("target") == "tag"
            and ruleset.get("enforcement") == "active"
            and tag_ref_pattern in includes
        ):
            matching_rulesets.append(ruleset)
    require(bool(matching_rulesets), f"no active tag ruleset covers {tag_ref_pattern}")

    release_authority_ids: set[int] = set()
    for ruleset in matching_rulesets:
        rule_types = {
            str(rule.get("type"))
            for rule in ruleset.get("rules", [])
            if isinstance(rule, dict)
        }
        require(
            REQUIRED_TAG_RULES <= rule_types,
            "release tag ruleset is missing immutable-tag controls",
        )
        bypass = ruleset.get("bypass_actors", [])
        if not isinstance(bypass, list):
            require(False, "release tag ruleset bypass actors are invalid")
            continue
        require(bool(bypass), "release tag ruleset has no explicit release authority")
        for actor in bypass:
            if not isinstance(actor, dict):
                require(False, "release tag ruleset contains an invalid bypass actor")
                continue
            actor_type = actor.get("actor_type")
            actor_id = actor.get("actor_id")
            require(
                actor_type in {"User", "Team"}
                and isinstance(actor_id, int)
                and actor.get("bypass_mode") == "always",
                "release tag ruleset contains an unscoped bypass actor",
            )
            if actor_type == "User" and isinstance(actor_id, int):
                release_authority_ids.add(actor_id)
            elif actor_type == "Team" and isinstance(actor_id, int):
                members = team_members.get(f"Team:{actor_id}")
                require(
                    isinstance(members, list),
                    f"release authority Team:{actor_id} membership could not be verified",
                )
                if isinstance(members, list):
                    release_authority_ids.update(
                        member.get("id")
                        for member in members
                        if isinstance(member, dict) and isinstance(member.get("id"), int)
                    )

    authorized_reviewer_ids: set[int] = set()
    for item in reviewers if isinstance(reviewers, list) else []:
        if not isinstance(item, dict):
            continue
        reviewer_type = item.get("type")
        reviewer = item.get("reviewer")
        reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
        if reviewer_type == "User" and isinstance(reviewer_id, int):
            if reviewer_id in collaborators and reviewer_id != owner_id:
                authorized_reviewer_ids.add(reviewer_id)
        elif reviewer_type == "Team" and isinstance(reviewer_id, int):
            members = team_members.get(f"Team:{reviewer_id}")
            require(
                isinstance(members, list),
                f"release reviewer Team:{reviewer_id} membership could not be verified",
            )
            member_ids = {
                member.get("id")
                for member in members
                if isinstance(member, dict) and isinstance(member.get("id"), int)
            } if isinstance(members, list) else set()
            eligible_members = member_ids & collaborators
            require(
                len(eligible_members) >= 2,
                f"release reviewer Team:{reviewer_id} has fewer than two review-capable members",
            )
            require(
                not (member_ids & release_authority_ids),
                f"release reviewer Team:{reviewer_id} overlaps release authority membership",
            )
            authorized_reviewer_ids.update(
                member_id for member_id in eligible_members if member_id != owner_id
            )
    authorized_reviewer_ids -= release_authority_ids
    require(bool(authorized_reviewer_ids), "release environment has no independent authorized reviewer")

    if not isinstance(approval_history_document, list):
        raise ReleaseApprovalError("workflow approval history must be a JSON array")
    approved_by: int | None = None
    for review in approval_history_document:
        if not isinstance(review, dict) or str(review.get("state") or "").lower() != "approved":
            continue
        environments = review.get("environments", [])
        environment_matches = any(
            isinstance(item, dict)
            and item.get("id") == environment_id
            and item.get("name") == environment_name
            for item in environments
        ) if isinstance(environments, list) else False
        if not environment_matches:
            continue
        user = review.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        if (
            isinstance(user_id, int)
            and user_id in authorized_reviewer_ids
            and user_id != owner_id
            and user_id not in release_authority_ids
        ):
            approved_by = user_id
            break
    require(
        approved_by is not None,
        "workflow run has no recorded approval by an independent authorized reviewer",
    )

    if problems:
        raise ReleaseApprovalError(
            "GitHub release approval requirements failed:\n - " + "\n - ".join(problems)
        )

    generated_at = generated_at or datetime.now(timezone.utc)
    inputs = {
        "repository": repository_document,
        "workflowRun": run_document,
        "environment": environment_document,
        "reviewHistory": approval_history_document,
        "collaborators": collaborators_document,
        "rulesets": rulesets_document,
        "teamMembers": team_members_document,
    }
    approval_binding = {
        "repository": repository,
        "runId": run_id,
        "runAttempt": run["run_attempt"],
        "commit": expected_sha,
        "environmentId": environment_id,
        "approverId": approved_by,
    }
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": repository,
        "runId": run_id,
        "runAttempt": run["run_attempt"],
        "commit": expected_sha,
        "releaseEnvironment": environment_name,
        "result": "passed",
        "approvalBindingSha256": canonical_sha256(approval_binding),
        "inputSha256": {name: canonical_sha256(value) for name, value in inputs.items()},
        "controls": {
            "workflowRunBinding": "passed",
            "firstAttemptOnly": "passed",
            "requiredReviewerProtection": "passed",
            "recordedEnvironmentApproval": "passed",
            "authorizedReviewer": "passed",
            "independentReviewer": "passed",
            "releaseAuthoritySeparation": "passed",
        },
    }


NO_NOT_FOUND_DEFAULT = object()


def gh_api_get(path: str, token: str, *, not_found: Any) -> Any:
    if shutil.which("gh") is None:
        raise ReleaseApprovalError("GitHub API TLS transport failed and gh is not installed")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    try:
        timeout = bounded_timeout_seconds(
            GH_COMMAND_TIMEOUT_SECONDS,
            "GITHUB_API_COMMAND_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        raise ReleaseApprovalError(str(exc)) from None
    try:
        result = subprocess.run(
            ["gh", "api", "--method", "GET", path],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        raise ReleaseApprovalError(
            f"gh api request timed out after {timeout:g} seconds for {path}"
        ) from None
    if result.returncode != 0:
        if "HTTP 404" in result.stderr and not_found is not NO_NOT_FOUND_DEFAULT:
            return not_found
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise ReleaseApprovalError(f"gh api request failed for {path}: {detail}")
    try:
        require_bounded_text(result.stdout)
    except HttpTransportPolicyError as exc:
        raise ReleaseApprovalError(f"gh api response rejected for {path}: {exc}") from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseApprovalError(f"gh api returned invalid JSON: {path}") from exc


def api_get(
    api_url: str,
    path: str,
    token: str,
    *,
    not_found: Any = NO_NOT_FOUND_DEFAULT,
) -> Any:
    request = Request(
        f"{api_url.rstrip('/')}/{path.lstrip('/')}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "platform-gitops-release-approval-verifier",
        },
    )
    try:
        timeout = http_timeout_seconds()
        with urlopen(request, timeout=timeout) as response:
            return json.loads(read_bounded_response(response))
    except HTTPError as exc:
        if exc.code == 404 and not_found is not NO_NOT_FOUND_DEFAULT:
            return not_found
        raise ReleaseApprovalError(f"GitHub API request failed with HTTP {exc.code}: {path}") from exc
    except URLError as exc:
        if api_url.rstrip("/") == "https://api.github.com":
            return gh_api_get(path, token, not_found=not_found)
        raise ReleaseApprovalError(f"GitHub API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseApprovalError(f"GitHub API returned invalid JSON: {path}") from exc
    except HttpTransportPolicyError as exc:
        raise ReleaseApprovalError(f"GitHub API response rejected for {path}: {exc}") from exc


def api_get_all_pages(api_url: str, path: str, token: str) -> list[Any]:
    items: list[Any] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        document = api_get(api_url, f"{path}{separator}per_page=100&page={page}", token)
        if not isinstance(document, list):
            raise ReleaseApprovalError(f"paginated GitHub API response must be an array: {path}")
        items.extend(document)
        if len(document) < 100:
            return items
    raise ReleaseApprovalError(f"GitHub API pagination exceeded 100 pages: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", type=int, default=int(os.environ.get("GITHUB_RUN_ID", "0") or 0))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--environment", default="production-release")
    parser.add_argument("--tag-ref-pattern", default="refs/tags/v*.*.*")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--approval-attempts", type=int, default=12)
    parser.add_argument("--approval-delay-seconds", type=float, default=5.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            os.environ.get(
                "GITHUB_RELEASE_APPROVAL_EVIDENCE_OUTPUT",
                "rendered/governance/github-release-approval-evidence.json",
            )
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GitHub release approval verification requires GITHUB_TOKEN", file=sys.stderr)
        return 2
    if not REPOSITORY_RE.fullmatch(args.repository):
        print("GitHub release approval requires GITHUB_REPOSITORY=OWNER/REPOSITORY", file=sys.stderr)
        return 2
    if args.run_id <= 0 or args.approval_attempts <= 0 or args.approval_delay_seconds < 0:
        print("GitHub release approval polling arguments are invalid", file=sys.stderr)
        return 2

    repository_path = quote(args.repository, safe="/")
    environment_path = quote(args.environment, safe="")
    try:
        repository_document = api_get(args.api_url, f"repos/{repository_path}", token)
        run_document = api_get(
            args.api_url,
            f"repos/{repository_path}/actions/runs/{args.run_id}",
            token,
        )
        environment_document = api_get(
            args.api_url,
            f"repos/{repository_path}/environments/{environment_path}",
            token,
        )
        collaborators_document = api_get_all_pages(
            args.api_url,
            f"repos/{repository_path}/collaborators?affiliation=all",
            token,
        )
        ruleset_summaries = api_get_all_pages(
            args.api_url,
            f"repos/{repository_path}/rulesets?includes_parents=true",
            token,
        )
        rulesets_document = []
        for summary in ruleset_summaries:
            if isinstance(summary, dict) and summary.get("target") == "tag" and summary.get("id"):
                rulesets_document.append(
                    api_get(
                        args.api_url,
                        f"repos/{repository_path}/rulesets/{quote(str(summary['id']), safe='')}",
                        token,
                    )
                )

        team_ids: set[int] = set()
        for rule in environment_document.get("protection_rules", []):
            if not isinstance(rule, dict) or rule.get("type") != "required_reviewers":
                continue
            for item in rule.get("reviewers", []):
                reviewer = item.get("reviewer") if isinstance(item, dict) else None
                reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
                if isinstance(item, dict) and item.get("type") == "Team" and isinstance(reviewer_id, int):
                    team_ids.add(reviewer_id)
        for ruleset in rulesets_document:
            for actor in ruleset.get("bypass_actors", []):
                if (
                    isinstance(actor, dict)
                    and actor.get("actor_type") == "Team"
                    and isinstance(actor.get("actor_id"), int)
                ):
                    team_ids.add(actor["actor_id"])

        owner = require_object(repository_document.get("owner"), "repository owner")
        organization_id = owner.get("id")
        organization = quote(str(owner.get("login") or ""), safe="")
        team_members_document: dict[str, Any] = {}
        for team_id in sorted(team_ids):
            if not isinstance(organization_id, int) or not organization:
                raise ReleaseApprovalError("repository organization identity is missing")
            team = require_object(
                api_get(
                    args.api_url,
                    f"organizations/{organization_id}/team/{team_id}",
                    token,
                ),
                f"Team:{team_id}",
            )
            slug = str(team.get("slug") or "")
            if not slug:
                raise ReleaseApprovalError(f"Team:{team_id} slug is missing")
            team_members_document[f"Team:{team_id}"] = api_get_all_pages(
                args.api_url,
                f"orgs/{organization}/teams/{quote(slug, safe='')}/members?role=all",
                token,
            )

        environment_id = environment_document.get("id")
        approval_history_document: Any = []
        for attempt in range(1, args.approval_attempts + 1):
            approval_history_document = api_get(
                args.api_url,
                f"repos/{repository_path}/actions/runs/{args.run_id}/approvals",
                token,
            )
            if (
                isinstance(environment_id, int)
                and approved_environment_present(
                    approval_history_document,
                    environment_id,
                    args.environment,
                )
            ):
                break
            if attempt < args.approval_attempts:
                time.sleep(args.approval_delay_seconds)

        evidence = validate_release_approval(
            repository=args.repository,
            run_id=args.run_id,
            expected_sha=args.sha,
            environment_name=args.environment,
            tag_ref_pattern=args.tag_ref_pattern,
            repository_document=repository_document,
            run_document=run_document,
            environment_document=environment_document,
            approval_history_document=approval_history_document,
            collaborators_document=collaborators_document,
            rulesets_document=rulesets_document,
            team_members_document=team_members_document,
        )
        atomic_write_text(args.output, json.dumps(evidence, indent=2) + "\n")
    except (OSError, ReleaseApprovalError) as exc:
        print(f"GitHub release approval verification failed: {exc}", file=sys.stderr)
        return 1

    print(
        "GitHub release approval verified: "
        f"repository={evidence['repository']} run={evidence['runId']} "
        f"attempt={evidence['runAttempt']} commit={evidence['commit']} "
        f"evidence={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
