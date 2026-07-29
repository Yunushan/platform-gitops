#!/usr/bin/env python3
"""Behavior-test GitHub governance planning and lockout safeguards."""

from __future__ import annotations

from configure_github_governance import (
    ConfigurationError,
    environment_payload,
    extract_environment_reviewers,
    has_tag_policy,
    merge_tag_ruleset,
    require_security_readback,
    ruleset_is_current,
    security_patch,
    unavailable_security_controls,
    validate_independent_reviewer,
)


def expect_rejection(callback, expected: str) -> None:
    try:
        callback()
    except ConfigurationError as exc:
        if expected not in str(exc):
            raise AssertionError(f"unexpected rejection: {exc}") from exc
    else:
        raise AssertionError(f"expected rejection: {expected}")


def main() -> int:
    repository = {
        "security_and_analysis": {
            "dependabot_security_updates": {"status": "enabled"},
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
            "secret_scanning_non_provider_patterns": {"status": "disabled"},
            "secret_scanning_validity_checks": {"status": "disabled"},
        }
    }
    patch = security_patch(repository)
    changed = set(patch["security_and_analysis"])
    if changed != {"secret_scanning_non_provider_patterns", "secret_scanning_validity_checks"}:
        raise AssertionError(f"unexpected security patch: {changed}")
    repository["owner"] = {"type": "User"}
    unavailable = unavailable_security_controls(repository)
    if set(unavailable) != changed:
        raise AssertionError(f"unexpected unavailable control classification: {unavailable}")
    expect_rejection(
        lambda: require_security_readback(repository),
        "GitHub did not enable requested security controls",
    )
    repository["security_and_analysis"]["secret_scanning_non_provider_patterns"]["status"] = "enabled"
    repository["security_and_analysis"]["secret_scanning_validity_checks"]["status"] = "enabled"
    require_security_readback(repository)

    existing = {
        "id": 7,
        "name": "platform semantic release tags",
        "target": "tag",
        "enforcement": "disabled",
        "bypass_actors": [{"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["refs/tags/release-*"], "exclude": []}},
        "rules": [{"type": "required_signatures"}, {"type": "deletion"}],
    }
    desired = merge_tag_ruleset(
        existing,
        name="platform semantic release tags",
        tag_ref_pattern="refs/tags/v*.*.*",
        release_authority_id=42,
    )
    rule_types = {rule["type"] for rule in desired["rules"]}
    if not {"creation", "update", "deletion", "non_fast_forward", "required_signatures"} <= rule_types:
        raise AssertionError("ruleset merge discarded or omitted required rules")
    if "refs/tags/release-*" not in desired["conditions"]["ref_name"]["include"]:
        raise AssertionError("ruleset merge discarded an existing ref condition")
    if not any(actor.get("actor_id") == 42 for actor in desired["bypass_actors"]):
        raise AssertionError("ruleset merge omitted the release authority")
    if ruleset_is_current(existing, desired):
        raise AssertionError("drifted ruleset was reported current")
    if not ruleset_is_current(desired, desired):
        raise AssertionError("current ruleset was reported drifted")

    environment = {
        "wait_timer": 15,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "reviewers": [{"type": "User", "reviewer": {"id": 11}}],
            }
        ],
    }
    reviewers = extract_environment_reviewers(environment)
    if reviewers != [{"type": "User", "id": 11}]:
        raise AssertionError(f"unexpected reviewer extraction: {reviewers}")
    payload = environment_payload(
        environment,
        reviewer={"type": "User", "id": 12},
    )
    if payload["reviewers"] != [
        {"type": "User", "id": 11},
        {"type": "User", "id": 12},
    ]:
        raise AssertionError("environment merge discarded an existing reviewer")
    if payload["prevent_self_review"] is not True or payload["wait_timer"] != 15:
        raise AssertionError("environment safety controls were not preserved")

    expect_rejection(
        lambda: validate_independent_reviewer(
            reviewer={"type": "User", "id": 42},
            release_authority={"id": 42},
            permission_document={"permission": "admin"},
        ),
        "must differ",
    )
    expect_rejection(
        lambda: validate_independent_reviewer(
            reviewer={"type": "User", "id": 8},
            release_authority={"id": 42},
            permission_document={},
        ),
        "must be a repository collaborator",
    )
    validate_independent_reviewer(
        reviewer={"type": "User", "id": 8},
        release_authority={"id": 42},
        permission_document={"permission": "read"},
    )

    if not has_tag_policy(
        {"branch_policies": [{"name": "v*.*.*", "type": "tag"}]},
        "v*.*.*",
    ):
        raise AssertionError("tag deployment policy was not recognized")
    if has_tag_policy(
        {"branch_policies": [{"name": "release/*", "type": "branch"}]},
        "v*.*.*",
    ):
        raise AssertionError("branch deployment policy was accepted as the release tag policy")

    print("GitHub governance configurator self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
