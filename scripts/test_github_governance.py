#!/usr/bin/env python3
"""Behavior-test fail-closed GitHub release governance validation."""

from __future__ import annotations

from copy import deepcopy

from verify_github_governance import GovernanceError, validate_governance


REPOSITORY = "example/platform-gitops"


def fixtures() -> dict[str, object]:
    return {
        "repository_document": {
            "full_name": REPOSITORY,
            "default_branch": "main",
            "security_and_analysis": {
                name: {"status": "enabled"}
                for name in (
                    "dependabot_security_updates",
                    "secret_scanning",
                    "secret_scanning_push_protection",
                    "secret_scanning_non_provider_patterns",
                    "secret_scanning_validity_checks",
                )
            },
        },
        "private_vulnerability_reporting_document": {"enabled": True},
        "codeql_default_setup_document": {
            "state": "configured",
            "languages": ["actions", "python"],
            "query_suite": "default",
            "threat_model": "remote",
            "schedule": "weekly",
        },
        "commit_document": {
            "sha": "a" * 40,
            "commit": {"verification": {"verified": True, "reason": "valid"}},
        },
        "protection_document": {
            "required_status_checks": {
                "strict": True,
                "checks": [
                    {"context": "validate"},
                    {"context": "Analyze (actions)"},
                    {"context": "Analyze (python)"},
                ],
            },
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": True,
                "require_last_push_approval": True,
            },
            "required_signatures": {"enabled": True},
            "enforce_admins": {"enabled": True},
            "required_linear_history": {"enabled": True},
            "required_conversation_resolution": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
        },
        "rulesets_document": [
            {
                "id": 1,
                "name": "immutable semantic release tags",
                "target": "tag",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": ["refs/tags/v*.*.*"], "exclude": []}},
                "rules": [{"type": name} for name in sorted(("creation", "update", "deletion", "non_fast_forward"))],
                "bypass_actors": [{"actor_type": "Team", "actor_id": 7, "bypass_mode": "always"}],
            }
        ],
        "environment_document": {
            "name": "production-release",
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": True,
                    "reviewers": [{"type": "Team", "reviewer": {"id": 7}}],
                }
            ],
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        },
        "environment_policies_document": {
            "total_count": 1,
            "branch_policies": [{"id": 1, "name": "v*.*.*", "type": "tag"}],
        },
        "workflow_permissions_document": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "actions_permissions_document": {
            "enabled": True,
            "allowed_actions": "all",
            "sha_pinning_required": True,
        },
    }


def validate(values: dict[str, object]) -> dict[str, object]:
    return validate_governance(
        repository=REPOSITORY,
        default_branch="main",
        tag_ref_pattern="refs/tags/v*.*.*",
        environment_name="production-release",
        environment_tag_pattern="v*.*.*",
        required_checks=("validate", "Analyze (actions)", "Analyze (python)"),
        **values,
    )


def reject(mutator, expected: str) -> None:
    values = deepcopy(fixtures())
    mutator(values)
    try:
        validate(values)
    except GovernanceError as exc:
        if expected not in str(exc):
            raise AssertionError(f"unexpected governance rejection: {exc}") from exc
    else:
        raise AssertionError(f"governance mutation passed: {expected}")


def main() -> int:
    evidence = validate(fixtures())
    if evidence["result"] != "passed" or evidence["controls"]["releaseTagRuleset"] != "passed":
        raise AssertionError("valid GitHub governance evidence was not accepted")

    reject(
        lambda values: values["commit_document"]["commit"]["verification"].update(verified=False),
        "default branch tip is not GitHub-verified",
    )
    reject(
        lambda values: values.update(rulesets_document=[]),
        "no active tag ruleset",
    )
    reject(
        lambda values: values["environment_document"]["protection_rules"][0].update(
            prevent_self_review=False
        ),
        "release environment permits self-review",
    )
    reject(
        lambda values: values["workflow_permissions_document"].update(
            default_workflow_permissions="write"
        ),
        "default workflow token permission is not read-only",
    )
    reject(
        lambda values: values["repository_document"]["security_and_analysis"][
            "secret_scanning_push_protection"
        ].update(status="disabled"),
        "secret_scanning_push_protection",
    )
    reject(
        lambda values: values["actions_permissions_document"].update(sha_pinning_required=False),
        "Actions SHA pinning is not required",
    )
    reject(
        lambda values: values["private_vulnerability_reporting_document"].update(enabled=False),
        "private vulnerability reporting is not enabled",
    )
    reject(
        lambda values: values["codeql_default_setup_document"].update(languages=["python"]),
        "CodeQL default setup is missing language: actions",
    )
    reject(
        lambda values: values["codeql_default_setup_document"].update(schedule="none"),
        "CodeQL default setup is not scheduled weekly",
    )
    reject(
        lambda values: values["environment_policies_document"]["branch_policies"][0].update(
            type="branch"
        ),
        "release environment does not allow only the intended tag pattern",
    )

    print("GitHub governance verifier self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
