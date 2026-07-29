#!/usr/bin/env python3
"""Verify live GitHub release governance and emit sanitized evidence."""

from __future__ import annotations

import argparse
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
from urllib.request import Request, urlopen


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_REQUIRED_CHECKS = ("validate", "Analyze (actions)", "Analyze (python)")
REQUIRED_TAG_RULES = {"creation", "update", "deletion", "non_fast_forward"}
API_VERSION = "2026-03-10"


class GovernanceError(ValueError):
    """Raised when live GitHub controls do not meet the release contract."""


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
    verification = require_object(
        require_object(commit.get("commit"), "default branch commit.commit").get("verification"),
        "default branch commit verification",
    )
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit_sha)), "default branch commit SHA is invalid")
    require(verification.get("verified") is True, "default branch tip is not GitHub-verified")
    require(verification.get("reason") == "valid", "default branch signature reason is not valid")

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
    require(reviews.get("required_approving_review_count", 0) >= 1, "pull requests require no approval")
    require(reviews.get("dismiss_stale_reviews") is True, "stale approvals are not dismissed")
    require(reviews.get("require_code_owner_reviews") is True, "CODEOWNER review is not required")
    require(reviews.get("require_last_push_approval") is True, "last-push approval is not required")
    for field, message in (
        ("required_signatures", "signed commits are not required"),
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
        require(
            isinstance(bypass, list)
            and any(isinstance(actor, dict) and actor.get("bypass_mode") == "always" for actor in bypass),
            "tag ruleset has no explicit release-authority bypass actor",
        )

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
        "schemaVersion": 1,
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
            "signedDefaultBranchTip": "passed",
            "releaseTagRuleset": "passed",
            "independentReleaseApproval": "passed",
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
    result = subprocess.run(
        ["gh", "api", "--method", "GET", path],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        if "HTTP 404" in result.stderr and not_found is not NO_NOT_FOUND_DEFAULT:
            return not_found
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise GovernanceError(f"gh api request failed for {path}: {detail}")
    try:
        return json.loads(result.stdout)
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
        with urlopen(request, timeout=30) as response:
            return json.load(response)
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
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
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
