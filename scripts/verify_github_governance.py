#!/usr/bin/env python3
"""Verify live GitHub release governance and emit sanitized evidence."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
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
from urllib.request import Request

from atomic_file import atomic_write_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from http_transport import (
    HttpTransportPolicyError,
    http_timeout_seconds,
    open_http_request,
    read_bounded_response,
    require_bounded_text,
)
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_REQUIRED_CHECKS = ("validate", "Analyze (actions)", "Analyze (python)")
REQUIRED_TAG_RULES = {"creation", "update", "deletion", "non_fast_forward"}
API_VERSION = "2026-03-10"
GH_COMMAND_TIMEOUT_SECONDS = 30


class GovernanceError(ValueError):
    """Raised when live GitHub controls do not meet the release contract."""


def codeowner_principals(document: Any, repository_owner: str) -> set[str]:
    codeowners = require_object(document, "active CODEOWNERS")
    if codeowners.get("type") != "file" or codeowners.get("path") != ".github/CODEOWNERS":
        raise GovernanceError("active .github/CODEOWNERS file is missing")
    if codeowners.get("encoding") != "base64" or not isinstance(codeowners.get("content"), str):
        raise GovernanceError("active CODEOWNERS content is not base64 encoded")
    try:
        encoded_content = "".join(codeowners["content"].split())
        content = base64.b64decode(encoded_content, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise GovernanceError("active CODEOWNERS content is invalid") from exc

    principals: set[str] = set()
    has_catch_all = False
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            raise GovernanceError(f"CODEOWNERS line {line_number} has no owner")
        if fields[0] == "*":
            has_catch_all = True
        owners = [field for field in fields[1:] if field.startswith("@")]
        if not owners:
            raise GovernanceError(f"CODEOWNERS line {line_number} has no GitHub principal")
        for owner in owners:
            normalized = owner.lower()
            if "<" in owner or ">" in owner or "your_org" in normalized or "your-org" in normalized:
                raise GovernanceError("active CODEOWNERS still contains a placeholder owner")
            if "/" in owner and not normalized.startswith(f"@{repository_owner.lower()}/"):
                raise GovernanceError(
                    f"CODEOWNERS team principal is outside repository owner: {owner}"
                )
            principals.add(normalized)
    if not has_catch_all:
        raise GovernanceError("active CODEOWNERS has no catch-all ownership rule")
    if len(principals) < 2:
        raise GovernanceError("active CODEOWNERS must name at least two distinct owners")
    return principals


def eligible_collaborators(document: Any) -> dict[int, str]:
    if not isinstance(document, list):
        raise GovernanceError("repository collaborators must be a JSON array")
    eligible: dict[int, str] = {}
    for collaborator in document:
        if not isinstance(collaborator, dict):
            continue
        identifier = collaborator.get("id")
        login = str(collaborator.get("login") or "").strip()
        permissions = collaborator.get("permissions")
        role = str(collaborator.get("role_name") or "").lower()
        can_review = role in {"write", "maintain", "admin"} or (
            isinstance(permissions, dict)
            and any(permissions.get(name) is True for name in ("push", "maintain", "admin"))
        )
        if isinstance(identifier, int) and login and can_review:
            eligible[identifier] = login
    return eligible


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceError(f"{label} must be a JSON object")
    return value


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_governance(
    *,
    repository: str,
    default_branch: str,
    tag_ref_pattern: str,
    environment_name: str,
    environment_tag_pattern: str,
    required_checks: tuple[str, ...],
    repository_document: Any,
    codeowners_document: Any,
    collaborators_document: Any,
    reviewer_members_document: Any,
    private_vulnerability_reporting_document: Any,
    codeql_default_setup_document: Any,
    commit_document: Any,
    protection_document: Any,
    rulesets_document: Any,
    environment_document: Any,
    environment_policies_document: Any,
    workflow_permissions_document: Any,
    actions_permissions_document: Any,
) -> dict[str, Any]:
    problems: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    if not REPOSITORY_RE.fullmatch(repository):
        raise GovernanceError("repository must use OWNER/REPOSITORY syntax")
    if not default_branch or any(char.isspace() for char in default_branch):
        raise GovernanceError("default branch must be a non-empty branch name")
    if not tag_ref_pattern.startswith("refs/tags/"):
        raise GovernanceError("tag ref pattern must start with refs/tags/")
    if not environment_name or not environment_tag_pattern:
        raise GovernanceError("release environment and tag pattern are required")
    if not required_checks:
        raise GovernanceError("at least one required status check is required")

    repo = require_object(repository_document, "repository")
    require(repo.get("full_name") == repository, "repository identity does not match")
    require(repo.get("default_branch") == default_branch, "repository default branch does not match")
    repository_owner = require_object(repo.get("owner"), "repository.owner")
    repository_owner_id = repository_owner.get("id")
    require(isinstance(repository_owner_id, int), "repository owner ID is invalid")
    codeowner_principals(codeowners_document, repository.split("/", 1)[0])
    collaborators = eligible_collaborators(collaborators_document)
    require(len(collaborators) >= 2, "repository has fewer than two review-capable collaborators")
    reviewer_members = require_object(reviewer_members_document, "release reviewer members")
    security = require_object(repo.get("security_and_analysis"), "repository.security_and_analysis")
    for control in (
        "dependabot_security_updates",
        "secret_scanning",
        "secret_scanning_push_protection",
        "secret_scanning_non_provider_patterns",
        "secret_scanning_validity_checks",
    ):
        item = security.get(control)
        require(
            isinstance(item, dict) and item.get("status") == "enabled",
            f"repository security control is not enabled: {control}",
        )

    private_reporting = require_object(
        private_vulnerability_reporting_document,
        "private vulnerability reporting",
    )
    require(
        private_reporting.get("enabled") is True,
        "private vulnerability reporting is not enabled",
    )

    codeql = require_object(codeql_default_setup_document, "CodeQL default setup")
    require(codeql.get("state") == "configured", "CodeQL default setup is not configured")
    languages = codeql.get("languages")
    language_set = {str(language) for language in languages} if isinstance(languages, list) else set()
    for language in ("actions", "python"):
        require(language in language_set, f"CodeQL default setup is missing language: {language}")
    require(
        codeql.get("query_suite") in {"default", "extended"},
        "CodeQL default setup query suite is invalid",
    )
    require(codeql.get("schedule") == "weekly", "CodeQL default setup is not scheduled weekly")
    require(codeql.get("threat_model") == "remote", "CodeQL default setup threat model is not remote")

    commit = require_object(commit_document, "default branch commit")
    commit_sha = str(commit.get("sha") or "").lower()
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit_sha)), "default branch commit SHA is invalid")

    protection = require_object(protection_document, "branch protection")
    status_checks = require_object(protection.get("required_status_checks"), "required status checks")
    require(status_checks.get("strict") is True, "required status checks are not strict")
    check_names = {
        str(item.get("context"))
        for item in status_checks.get("checks", [])
        if isinstance(item, dict) and item.get("context")
    }
    check_names.update(str(item) for item in status_checks.get("contexts", []) if item)
    for check in required_checks:
        require(check in check_names, f"branch protection is missing required check: {check}")

    reviews = require_object(protection.get("required_pull_request_reviews"), "pull request reviews")
    require(
        reviews.get("required_approving_review_count", 0) == 0,
        "pull request approvals are enabled",
    )
    require(reviews.get("dismiss_stale_reviews") is True, "stale approvals are not dismissed")
    require(
        reviews.get("require_code_owner_reviews") is not True,
        "CODEOWNER approval is enabled",
    )
    require(
        reviews.get("require_last_push_approval") is not True,
        "last-push approval is enabled",
    )
    for field, message in (
        ("enforce_admins", "branch protection is not enforced for administrators"),
        ("required_linear_history", "linear history is not required"),
        ("required_conversation_resolution", "conversation resolution is not required"),
    ):
        value = protection.get(field)
        require(isinstance(value, dict) and value.get("enabled") is True, message)
    for field, message in (
        ("allow_force_pushes", "force pushes are allowed"),
        ("allow_deletions", "branch deletion is allowed"),
    ):
        value = protection.get(field)
        require(isinstance(value, dict) and value.get("enabled") is False, message)
    signatures = protection.get("required_signatures")
    require(
        isinstance(signatures, dict) and signatures.get("enabled") is not True,
        "signed commits are enabled",
    )

    if not isinstance(rulesets_document, list):
        raise GovernanceError("repository rulesets must be a JSON array")
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
    if matching_rulesets:
        rule_types = {
            str(rule.get("type"))
            for rule in matching_rulesets[0].get("rules", [])
            if isinstance(rule, dict)
        }
        missing_rules = sorted(REQUIRED_TAG_RULES - rule_types)
        require(not missing_rules, "tag ruleset is missing rules: " + ", ".join(missing_rules))
        bypass = matching_rulesets[0].get("bypass_actors", [])
        explicit_bypass = [
            actor
            for actor in bypass
            if isinstance(actor, dict)
            and actor.get("actor_type") in {"User", "Team"}
            and isinstance(actor.get("actor_id"), int)
            and actor.get("bypass_mode") == "always"
        ] if isinstance(bypass, list) else []
        require(
            bool(explicit_bypass),
            "tag ruleset has no explicit release-authority bypass actor",
        )
        require(
            isinstance(bypass, list) and len(explicit_bypass) == len(bypass),
            "tag ruleset contains an unscoped release bypass actor",
        )
        release_authority_member_ids: set[int] = set()
        for actor in explicit_bypass:
            actor_id = actor["actor_id"]
            if actor.get("actor_type") == "User":
                release_authority_member_ids.add(actor_id)
                continue
            members = reviewer_members.get(f"Team:{actor_id}")
            require(
                isinstance(members, list),
                f"release authority Team:{actor_id} membership could not be verified",
            )
            team_member_ids = {
                member.get("id")
                for member in members
                if isinstance(member, dict) and isinstance(member.get("id"), int)
            } if isinstance(members, list) else set()
            require(
                bool(team_member_ids),
                f"release authority Team:{actor_id} has no verified members",
            )
            release_authority_member_ids.update(team_member_ids)
    else:
        release_authority_member_ids = set()

    environment = require_object(environment_document, "release environment")
    require(environment.get("name") == environment_name, "release environment identity does not match")
    reviewer_rules = [
        rule
        for rule in environment.get("protection_rules", [])
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers"
    ]
    require(bool(reviewer_rules), "release environment has no required reviewers")
    if reviewer_rules:
        reviewer_rule = reviewer_rules[0]
        require(bool(reviewer_rule.get("reviewers")), "release environment reviewer list is empty")
        require(reviewer_rule.get("prevent_self_review") is True, "release environment permits self-review")
        bypass_identities = {
            (str(actor.get("actor_type")), actor.get("actor_id"))
            for ruleset in matching_rulesets
            for actor in ruleset.get("bypass_actors", [])
            if isinstance(actor, dict) and isinstance(actor.get("actor_id"), int)
        }
        independent_reviewer = False
        for item in reviewer_rule.get("reviewers", []):
            if not isinstance(item, dict):
                continue
            reviewer_type = str(item.get("type") or "")
            reviewer = item.get("reviewer")
            reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
            if reviewer_type not in {"User", "Team"} or not isinstance(reviewer_id, int):
                continue
            if (reviewer_type, reviewer_id) in bypass_identities:
                continue
            if reviewer_type == "User":
                if (
                    reviewer_id in collaborators
                    and reviewer_id != repository_owner_id
                    and reviewer_id not in release_authority_member_ids
                ):
                    independent_reviewer = True
            else:
                members = reviewer_members.get(f"Team:{reviewer_id}")
                member_ids = {
                    member.get("id")
                    for member in members
                    if isinstance(member, dict) and isinstance(member.get("id"), int)
                } if isinstance(members, list) else set()
                eligible_members = member_ids & set(collaborators)
                if member_ids & release_authority_member_ids:
                    continue
                if len(eligible_members) >= 2 and any(
                    member_id != repository_owner_id for member_id in eligible_members
                ):
                    independent_reviewer = True
        require(
            independent_reviewer,
            "release environment has no independently review-capable user or team",
        )
    branch_policy = require_object(environment.get("deployment_branch_policy"), "deployment branch policy")
    require(branch_policy.get("protected_branches") is False, "release environment is not using tag allowlists")
    require(branch_policy.get("custom_branch_policies") is True, "custom release ref policies are disabled")

    policies = require_object(environment_policies_document, "environment deployment policies")
    policy_items = policies.get("branch_policies")
    if not isinstance(policy_items, list):
        raise GovernanceError("environment branch_policies must be an array")
    require(
        any(
            isinstance(policy, dict)
            and policy.get("type") in (None, "tag")
            and policy.get("name") == environment_tag_pattern
            for policy in policy_items
        ),
        f"release environment does not allow only the intended tag pattern: {environment_tag_pattern}",
    )

    workflow_permissions = require_object(workflow_permissions_document, "workflow permissions")
    require(
        workflow_permissions.get("default_workflow_permissions") == "read",
        "default workflow token permission is not read-only",
    )
    require(
        workflow_permissions.get("can_approve_pull_request_reviews") is False,
        "Actions can approve pull requests",
    )
    actions_permissions = require_object(actions_permissions_document, "Actions permissions")
    require(actions_permissions.get("enabled") is True, "GitHub Actions is disabled")
    require(actions_permissions.get("sha_pinning_required") is True, "Actions SHA pinning is not required")

    if problems:
        raise GovernanceError("GitHub governance requirements failed:\n - " + "\n - ".join(problems))

    inputs = {
        "repository": repository_document,
        "codeowners": codeowners_document,
        "collaborators": collaborators_document,
        "reviewerMembers": reviewer_members_document,
        "privateVulnerabilityReporting": private_vulnerability_reporting_document,
        "codeqlDefaultSetup": codeql_default_setup_document,
        "commit": commit_document,
        "protection": protection_document,
        "rulesets": rulesets_document,
        "environment": environment_document,
        "environmentPolicies": environment_policies_document,
        "workflowPermissions": workflow_permissions_document,
        "actionsPermissions": actions_permissions_document,
    }
    return {
        "schemaVersion": 4,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": repository,
        "defaultBranch": default_branch,
        "commit": commit_sha,
        "releaseEnvironment": environment_name,
        "tagPattern": tag_ref_pattern,
        "result": "passed",
        "inputSha256": {name: canonical_sha256(value) for name, value in inputs.items()},
        "controls": {
            "branchProtection": "passed",
            "activeCodeowners": "passed",
            "independentCollaborators": "passed",
            "defaultBranchCommitIdentity": "passed",
            "releaseTagRuleset": "passed",
            "independentReleaseReviewConfigured": "passed",
            "releaseTagEnvironmentPolicy": "passed",
            "readOnlyWorkflowToken": "passed",
            "actionShaPinning": "passed",
            "securityScanning": "passed",
            "privateVulnerabilityReporting": "passed",
            "codeqlDefaultSetup": "passed",
        },
    }


NO_NOT_FOUND_DEFAULT = object()


def gh_api_get(path: str, token: str, *, not_found: Any) -> Any:
    if shutil.which("gh") is None:
        raise GovernanceError("GitHub API TLS transport failed and gh is not installed")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    try:
        timeout = bounded_timeout_seconds(
            GH_COMMAND_TIMEOUT_SECONDS,
            "GITHUB_API_COMMAND_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        raise GovernanceError(str(exc)) from None
    try:
        result = run_bounded(
            ["gh", "api", "--method", "GET", path],
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        raise GovernanceError(
            f"gh api request timed out after {timeout:g} seconds for {path}"
        ) from None
    except (BoundedSubprocessError, ValueError) as exc:
        raise GovernanceError(f"gh api output rejected for {path}: {exc}") from None
    if result.returncode != 0:
        if "HTTP 404" in result.stderr and not_found is not NO_NOT_FOUND_DEFAULT:
            return not_found
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise GovernanceError(f"gh api request failed for {path}: {detail}")
    try:
        require_bounded_text(result.stdout)
    except HttpTransportPolicyError as exc:
        raise GovernanceError(f"gh api response rejected for {path}: {exc}") from exc
    try:
        return loads_strict_json(result.stdout)
    except json.JSONDecodeError as exc:
        raise GovernanceError(f"gh api returned invalid JSON: {path}") from exc


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
            "User-Agent": "platform-gitops-governance-verifier",
        },
    )
    try:
        timeout = http_timeout_seconds()
        with open_http_request(request, timeout=timeout) as response:
            return loads_strict_json(read_bounded_response(response))
    except HTTPError as exc:
        if exc.code == 404 and not_found is not NO_NOT_FOUND_DEFAULT:
            return not_found
        raise GovernanceError(f"GitHub API request failed with HTTP {exc.code}: {path}") from exc
    except URLError as exc:
        if api_url.rstrip("/") == "https://api.github.com":
            return gh_api_get(path, token, not_found=not_found)
        raise GovernanceError(f"GitHub API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise GovernanceError(f"GitHub API returned invalid JSON: {path}") from exc
    except HttpTransportPolicyError as exc:
        raise GovernanceError(f"GitHub API response rejected for {path}: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--default-branch", default=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"))
    parser.add_argument("--tag-ref-pattern", default="refs/tags/v*.*.*")
    parser.add_argument("--environment", default="production-release")
    parser.add_argument("--environment-tag-pattern", default="v*.*.*")
    parser.add_argument("--required-check", action="append", dest="required_checks")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            os.environ.get(
                "GITHUB_GOVERNANCE_EVIDENCE_OUTPUT",
                "rendered/governance/github-governance-evidence.json",
            )
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GitHub governance verification requires GITHUB_TOKEN", file=sys.stderr)
        return 2
    if not REPOSITORY_RE.fullmatch(args.repository):
        print("GitHub governance verification requires GITHUB_REPOSITORY=OWNER/REPOSITORY", file=sys.stderr)
        return 2
    repository_path = quote(args.repository, safe="/")
    branch_path = quote(args.default_branch, safe="")
    environment_path = quote(args.environment, safe="")
    try:
        repository_document = api_get(args.api_url, f"repos/{repository_path}", token)
        codeowners_document = api_get(
            args.api_url,
            f"repos/{repository_path}/contents/{quote('.github/CODEOWNERS', safe='/')}?ref={branch_path}",
            token,
            not_found={},
        )
        collaborators_document = api_get(
            args.api_url,
            f"repos/{repository_path}/collaborators?affiliation=all&per_page=100",
            token,
        )
        private_vulnerability_reporting_document = api_get(
            args.api_url,
            f"repos/{repository_path}/private-vulnerability-reporting",
            token,
            not_found={"enabled": False},
        )
        codeql_default_setup_document = api_get(
            args.api_url,
            f"repos/{repository_path}/code-scanning/default-setup",
            token,
            not_found={},
        )
        commit_document = api_get(
            args.api_url,
            f"repos/{repository_path}/commits/{branch_path}",
            token,
        )
        protection_document = api_get(
            args.api_url,
            f"repos/{repository_path}/branches/{branch_path}/protection",
            token,
        )
        ruleset_summaries = api_get(
            args.api_url,
            f"repos/{repository_path}/rulesets?includes_parents=true&per_page=100",
            token,
        )
        if not isinstance(ruleset_summaries, list):
            raise GovernanceError("GitHub rulesets response must be an array")
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
        environment_document = api_get(
            args.api_url,
            f"repos/{repository_path}/environments/{environment_path}",
            token,
            not_found={
                "name": "",
                "protection_rules": [],
                "deployment_branch_policy": {},
            },
        )
        reviewer_members_document: dict[str, Any] = {}
        owner_document = require_object(repository_document.get("owner"), "repository owner")
        organization_id = owner_document.get("id")
        organization = quote(str(owner_document.get("login") or ""), safe="")
        for ruleset in rulesets_document:
            for actor in ruleset.get("bypass_actors", []):
                if (
                    not isinstance(actor, dict)
                    or actor.get("actor_type") != "Team"
                    or not isinstance(actor.get("actor_id"), int)
                ):
                    continue
                if not isinstance(organization_id, int) or not organization:
                    raise GovernanceError("repository organization identity is missing")
                reviewer_id = actor["actor_id"]
                key = f"Team:{reviewer_id}"
                if key in reviewer_members_document:
                    continue
                team = require_object(
                    api_get(
                        args.api_url,
                        f"organizations/{organization_id}/team/{reviewer_id}",
                        token,
                    ),
                    f"release authority Team:{reviewer_id}",
                )
                slug = str(team.get("slug") or "")
                if not slug:
                    raise GovernanceError(
                        f"release authority Team:{reviewer_id} slug is missing"
                    )
                reviewer_members_document[key] = api_get(
                    args.api_url,
                    f"orgs/{organization}/teams/{quote(slug, safe='')}/members?role=all&per_page=100",
                    token,
                )
        for rule in environment_document.get("protection_rules", []):
            if not isinstance(rule, dict) or rule.get("type") != "required_reviewers":
                continue
            for item in rule.get("reviewers", []):
                if not isinstance(item, dict) or item.get("type") != "Team":
                    continue
                reviewer = item.get("reviewer")
                reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
                slug = str(reviewer.get("slug") or "") if isinstance(reviewer, dict) else ""
                if isinstance(reviewer_id, int) and slug:
                    key = f"Team:{reviewer_id}"
                    if key not in reviewer_members_document:
                        reviewer_members_document[key] = api_get(
                            args.api_url,
                            f"orgs/{organization}/teams/{quote(slug, safe='')}/members?role=all&per_page=100",
                            token,
                        )
        environment_policies_document = api_get(
            args.api_url,
            f"repos/{repository_path}/environments/{environment_path}/deployment-branch-policies?per_page=100",
            token,
            not_found={"total_count": 0, "branch_policies": []},
        )
        workflow_permissions_document = api_get(
            args.api_url,
            f"repos/{repository_path}/actions/permissions/workflow",
            token,
        )
        actions_permissions_document = api_get(
            args.api_url,
            f"repos/{repository_path}/actions/permissions",
            token,
        )
        evidence = validate_governance(
            repository=args.repository,
            default_branch=args.default_branch,
            tag_ref_pattern=args.tag_ref_pattern,
            environment_name=args.environment,
            environment_tag_pattern=args.environment_tag_pattern,
            required_checks=tuple(args.required_checks or DEFAULT_REQUIRED_CHECKS),
            repository_document=repository_document,
            codeowners_document=codeowners_document,
            collaborators_document=collaborators_document,
            reviewer_members_document=reviewer_members_document,
            private_vulnerability_reporting_document=private_vulnerability_reporting_document,
            codeql_default_setup_document=codeql_default_setup_document,
            commit_document=commit_document,
            protection_document=protection_document,
            rulesets_document=rulesets_document,
            environment_document=environment_document,
            environment_policies_document=environment_policies_document,
            workflow_permissions_document=workflow_permissions_document,
            actions_permissions_document=actions_permissions_document,
        )
        atomic_write_text(args.output, json.dumps(evidence, indent=2) + "\n")
    except (OSError, GovernanceError) as exc:
        print(f"GitHub governance verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        "GitHub governance verified: "
        f"repository={evidence['repository']} branch={evidence['defaultBranch']} "
        f"commit={evidence['commit']} evidence={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
