#!/usr/bin/env python3
"""Behavior-test GitHub governance planning and lockout safeguards."""

from __future__ import annotations

from configure_github_governance import (
    ConfigurationError,
    environment_payload,
    extract_environment_reviewers,
    has_tag_policy,
    merge_tag_ruleset,
    principal_repository_permission,
    principal_team_members,
    repository_permission,
    require_security_readback,
    resolve_principal,
    ruleset_is_current,
    security_patch,
    unavailable_security_controls,
    validate_independent_reviewer,
    validate_release_authority,
)


class FakeApi:
    def __init__(self, documents: dict[str, object]) -> None:
        self.documents = documents

    def get(self, path: str, *, not_found: object = None) -> object:
        return self.documents.get(path, not_found)


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
        "bypass_actors": [
            {"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"},
            {
                "actor_id": None,
                "actor_type": "OrganizationAdmin",
                "bypass_mode": "always",
            },
        ],
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
    if any(actor.get("actor_type") == "OrganizationAdmin" for actor in desired["bypass_actors"]):
        raise AssertionError("ruleset merge retained an unscoped bypass actor")
    if ruleset_is_current(existing, desired):
        raise AssertionError("drifted ruleset was reported current")
    if not ruleset_is_current(desired, desired):
        raise AssertionError("current ruleset was reported drifted")

    team_ruleset = merge_tag_ruleset(
        None,
        name="platform semantic release tags",
        tag_ref_pattern="refs/tags/v*.*.*",
        release_authority_id=84,
        release_authority_type="Team",
    )
    if not any(
        actor == {"actor_id": 84, "actor_type": "Team", "bypass_mode": "always"}
        for actor in team_ruleset["bypass_actors"]
    ):
        raise AssertionError("team release authority was not encoded as a team bypass actor")

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
    environment["protection_rules"][0]["reviewers"][0]["reviewer"]["login"] = "reviewer"
    if environment_payload(environment, reviewer={"type": "User", "id": 11})["reviewers"] != [
        {"type": "User", "id": 11}
    ]:
        raise AssertionError("environment payload leaked read-only reviewer identity fields")
    expect_rejection(
        lambda: environment_payload(
            {
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "reviewers": [
                            {"type": "User", "reviewer": {"id": identifier}}
                            for identifier in range(1, 7)
                        ],
                    }
                ]
            },
            reviewer={"type": "User", "id": 7},
        ),
        "more than six reviewers",
    )

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
        "must have write, maintain, or admin repository access",
    )
    expect_rejection(
        lambda: validate_independent_reviewer(
            reviewer={"type": "User", "id": 8},
            release_authority={"id": 42},
            permission_document={"permission": "read"},
        ),
        "must have write, maintain, or admin repository access",
    )
    validate_independent_reviewer(
        reviewer={"type": "User", "id": 8},
        release_authority={"id": 42},
        permission_document={"permission": "write"},
    )
    validate_release_authority(
        release_authority={"type": "User", "id": 42},
        permission_document={"permission": "admin"},
    )
    expect_rejection(
        lambda: validate_release_authority(
            release_authority={"type": "User", "id": 42},
            permission_document={"permission": "read"},
        ),
        "must have write, maintain, or admin repository access",
    )
    if repository_permission({"permission": "push"}) != "write":
        raise AssertionError("legacy team push permission was not normalized to write")
    if repository_permission({"permission": "pull"}) != "read":
        raise AssertionError("legacy team pull permission was not normalized to read")
    if repository_permission(
        {"role_name": "custom-release-reviewer", "permissions": {"push": True}}
    ) != "write":
        raise AssertionError("custom team role did not fall back to permission flags")

    owner = {"type": "Organization", "login": "example"}
    api = FakeApi(
        {
            "orgs/example/teams/release-security": {
                "id": 84,
                "slug": "release-security",
            },
            "orgs/example/teams/release-security/repos/example/platform-gitops": {
                "permissions": {"pull": True, "push": True},
            },
            "orgs/example/teams/release-security/members?role=all&per_page=100": [
                {"id": 101, "login": "one"},
                {"id": 102, "login": "two"},
            ],
            "users/release-admin": {"type": "User", "id": 42, "login": "release-admin"},
        }
    )
    team = resolve_principal(
        api,
        owner=owner,
        user="",
        team="release-security",
        label="release reviewer",
    )
    if team != {
        "type": "Team",
        "id": 84,
        "slug": "release-security",
        "organization": "example",
    }:
        raise AssertionError(f"unexpected resolved reviewer team: {team}")
    if repository_permission(
        principal_repository_permission(
            api,
            repository_path="example/platform-gitops",
            principal=team,
        )
    ) != "write":
        raise AssertionError("team repository write permission was not recognized")
    members = principal_team_members(api, team)
    validate_independent_reviewer(
        reviewer=team,
        release_authority={"type": "User", "id": 42},
        permission_document={"permissions": {"pull": True, "push": True}},
        team_members_document=members,
    )
    expect_rejection(
        lambda: validate_independent_reviewer(
            reviewer=team,
            release_authority={"type": "Team", "id": 84},
            permission_document={"permissions": {"push": True}},
            team_members_document=members,
        ),
        "must differ",
    )
    expect_rejection(
        lambda: validate_independent_reviewer(
            reviewer=team,
            release_authority={"type": "User", "id": 101},
            permission_document={"permissions": {"push": True}},
            team_members_document=members,
        ),
        "must not share members",
    )
    expect_rejection(
        lambda: validate_independent_reviewer(
            reviewer=team,
            release_authority={"type": "User", "id": 42},
            permission_document={"permissions": {"push": True}},
            team_members_document=[{"id": 101}],
        ),
        "must contain at least two members",
    )
    user = resolve_principal(
        api,
        owner=owner,
        user="release-admin",
        team="",
        label="release authority",
    )
    if user != {"type": "User", "id": 42, "login": "release-admin"}:
        raise AssertionError(f"unexpected resolved release user: {user}")
    expect_rejection(
        lambda: resolve_principal(
            api,
            owner=owner,
            user="release-admin",
            team="release-security",
            label="release authority",
        ),
        "either a user or a team",
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
