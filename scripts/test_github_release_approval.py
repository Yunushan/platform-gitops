#!/usr/bin/env python3
"""Behavior-test independent GitHub release approval verification."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from verify_github_release_approval import ReleaseApprovalError, validate_release_approval


REPOSITORY = "example/platform-gitops"
COMMIT = "a" * 40


def fixtures() -> dict[str, object]:
    return {
        "repository_document": {
            "full_name": REPOSITORY,
            "owner": {"id": 100, "login": "example"},
        },
        "run_document": {
            "id": 1234,
            "head_sha": COMMIT,
            "event": "push",
            "path": ".github/workflows/release.yml",
            "status": "in_progress",
            "run_attempt": 1,
            "head_repository": {"full_name": REPOSITORY},
        },
        "environment_document": {
            "id": 900,
            "name": "production-release",
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": True,
                    "reviewers": [{"type": "Team", "reviewer": {"id": 7}}],
                }
            ],
        },
        "approval_history_document": [
            {
                "state": "approved",
                "environments": [{"id": 900, "name": "production-release"}],
                "user": {"id": 200, "login": "security-one"},
            }
        ],
        "collaborators_document": [
            {"id": 100, "login": "release-owner", "role_name": "admin"},
            {"id": 200, "login": "security-one", "role_name": "maintain"},
            {"id": 201, "login": "security-two", "role_name": "write"},
            {"id": 300, "login": "release-one", "role_name": "maintain"},
        ],
        "rulesets_document": [
            {
                "target": "tag",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": ["refs/tags/v*.*.*"]}},
                "rules": [
                    {"type": name}
                    for name in ("creation", "update", "deletion", "non_fast_forward")
                ],
                "bypass_actors": [
                    {"actor_type": "Team", "actor_id": 8, "bypass_mode": "always"}
                ],
            }
        ],
        "team_members_document": {
            "Team:7": [
                {"id": 200, "login": "security-one"},
                {"id": 201, "login": "security-two"},
            ],
            "Team:8": [{"id": 300, "login": "release-one"}],
        },
    }


def validate(values: dict[str, object]) -> dict[str, object]:
    return validate_release_approval(
        repository=REPOSITORY,
        run_id=1234,
        expected_sha=COMMIT,
        environment_name="production-release",
        tag_ref_pattern="refs/tags/v*.*.*",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        **values,
    )


def reject(mutator, expected: str) -> None:  # type: ignore[no-untyped-def]
    values = deepcopy(fixtures())
    mutator(values)
    try:
        validate(values)
    except ReleaseApprovalError as exc:
        if expected not in str(exc):
            raise AssertionError(f"unexpected release approval rejection: {exc}") from exc
    else:
        raise AssertionError(f"release approval mutation passed: {expected}")


def main() -> int:
    evidence = validate(fixtures())
    if evidence["result"] != "passed" or evidence["schemaVersion"] != 1:
        raise AssertionError("valid release approval did not produce passing evidence")
    if evidence["controls"]["independentReviewer"] != "passed":
        raise AssertionError("release approval omitted independent reviewer proof")
    if set(evidence["inputSha256"]) != {
        "repository",
        "workflowRun",
        "environment",
        "reviewHistory",
        "collaborators",
        "rulesets",
        "teamMembers",
    }:
        raise AssertionError("release approval evidence inputs do not match the contract")
    if len(evidence["approvalBindingSha256"]) != 64:
        raise AssertionError("release approval binding is not a SHA-256")

    reject(
        lambda values: values["run_document"].update(head_sha="b" * 40),
        "workflow run commit does not match",
    )
    reject(
        lambda values: values["run_document"].update(path=".github/workflows/other.yml"),
        "workflow run path is not release.yml",
    )
    reject(
        lambda values: values["run_document"].update(run_attempt=2),
        "release publication requires the first workflow run attempt",
    )
    reject(
        lambda values: values["environment_document"]["protection_rules"][0].update(
            prevent_self_review=False
        ),
        "release environment permits self-review",
    )
    reject(
        lambda values: values["approval_history_document"][0]["user"].update(id=100),
        "no recorded approval by an independent authorized reviewer",
    )
    reject(
        lambda values: values["approval_history_document"][0]["user"].update(id=300),
        "no recorded approval by an independent authorized reviewer",
    )
    reject(
        lambda values: values["approval_history_document"][0].update(state="rejected"),
        "no recorded approval by an independent authorized reviewer",
    )
    reject(
        lambda values: values["approval_history_document"][0].update(
            environments=[{"id": 901, "name": "staging"}]
        ),
        "no recorded approval by an independent authorized reviewer",
    )
    reject(
        lambda values: values["team_members_document"].update(
            {"Team:7": [{"id": 200, "login": "security-one"}]}
        ),
        "fewer than two review-capable members",
    )
    reject(
        lambda values: values["team_members_document"].update(
            {
                "Team:7": [
                    {"id": 200, "login": "security-one"},
                    {"id": 300, "login": "release-one"},
                ]
            }
        ),
        "overlaps release authority membership",
    )
    reject(
        lambda values: values["rulesets_document"][0].update(bypass_actors=[]),
        "release tag ruleset has no explicit release authority",
    )

    print("Independent GitHub release approval behavior validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
