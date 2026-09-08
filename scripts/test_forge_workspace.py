#!/usr/bin/env python3
"""Self-test selective GitLab workspace export/import contracts."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_cutover as cutover
import forge_workspace as workspace


def base_plan() -> dict[str, object]:
    return {
        "version": 1,
        "direction": "gitlab-to-forgejo",
        "source": {
            "api_url": "https://gitlab.example.test/api/v4",
            "token_env": "GITLAB_TOKEN",
            "project_paths": ["platform/control-plane"],
            "group_paths": ["platform"],
            "usernames": ["alice"],
            "all_available_projects": False,
        },
        "destination": {
            "api_url": "https://forgejo.example.test/api/v1",
            "token_env": "FORGEJO_TOKEN",
            "owner_kind": "organization",
        },
        "surfaces": {
            "users": {"mode": "managed", "default_password_env": "IMPORT_PASSWORD"},
            "groups": {"mode": "managed", "members_mode": "import"},
            "subgroups": {"mode": "skip"},
            "projects": {"mode": "managed"},
            "repositories": {"mode": "managed"},
            "runners": {"mode": "mapped", "label_mappings": {"linux-amd64": {"platform": "linux"}}},
            "variables": {"mode": "managed"},
            "ci": {"mode": "managed", "include_content": True},
            "pipelines": {"mode": "managed", "import_history": False},
        },
        "services": {
            "woodpecker": {
                "api_url": "https://woodpecker.example.test",
                "token_env": "WOODPECKER_TOKEN",
            }
        },
    }


def expect_error(plan: dict[str, object], text: str) -> None:
    try:
        workspace.validate_plan(plan)
    except workspace.WorkspaceError as exc:
        if text not in str(exc):
            raise AssertionError(f"expected {text!r} in {exc!r}")
    else:
        raise AssertionError(f"expected validation failure containing {text!r}")


def test_selective_plan_contract() -> None:
    plan = base_plan()
    workspace.validate_plan(plan)

    no_users = copy.deepcopy(plan)
    no_users["surfaces"]["users"] = {"mode": "managed", "default_password_env": "IMPORT_PASSWORD"}  # type: ignore[index]
    no_users["source"]["usernames"] = []  # type: ignore[index]
    expect_error(no_users, "source.usernames or surfaces.users.all_available")

    unsafe_ci = copy.deepcopy(plan)
    unsafe_ci["surfaces"]["ci"]["include_content"] = False  # type: ignore[index]
    expect_error(unsafe_ci, "include_content=true")

    unsafe_history = copy.deepcopy(plan)
    unsafe_history["surfaces"]["pipelines"]["import_history"] = True  # type: ignore[index]
    expect_error(unsafe_history, "historical GitLab runs are export-only")

    unsafe_rules = copy.deepcopy(plan)
    unsafe_rules["surfaces"]["rules"] = {"mode": "managed", "reconcile": "exact"}  # type: ignore[index]
    expect_error(unsafe_rules, "surfaces.rules.reconcile=exact requires accepted=true")
    unsafe_rules["surfaces"]["rules"].update({"accepted": True, "reason": "approved"})  # type: ignore[index]
    workspace.validate_plan(unsafe_rules)

    unsafe_schedule_activation = copy.deepcopy(plan)
    unsafe_schedule_activation["surfaces"]["pipelines"]["schedule_mappings"] = {  # type: ignore[index]
        "4": {"name": "nightly", "enabled": True}
    }
    expect_error(unsafe_schedule_activation, "cannot enable a schedule during workspace import")

    members_without_users = copy.deepcopy(plan)
    members_without_users["surfaces"]["users"] = {"mode": "skip"}  # type: ignore[index]
    members_without_users["surfaces"]["groups"] = {"mode": "managed"}  # type: ignore[index]
    expect_error(members_without_users, "members_mode=skip|mapped|manual")


def test_redaction_and_destination_url() -> None:
    safe = workspace.safe_record(
        {"key": "DEPLOY_TOKEN", "value": "do-not-write", "token": "also-secret", "nested": {"password": "x"}}
    )
    if safe != {"key": "DEPLOY_TOKEN", "configured": True, "nested": {}}:
        raise AssertionError(f"workspace redaction changed unexpectedly: {safe!r}")
    plan = base_plan()
    if workspace.destination_git_url(plan, "platform", "control-plane") != "https://forgejo.example.test/platform/control-plane.git":
        raise AssertionError("destination API URL was not converted to a Forgejo Git URL")
    if workspace.source_variable_path({"source_scope": "group:platform", "key": "REGISTRY"}, "7") != "groups/platform/variables/REGISTRY":
        raise AssertionError("group variable endpoint was not selected")
    if workspace.source_variable_path({"source_scope": "instance", "key": "GLOBAL"}, "7") != "admin/ci/variables/GLOBAL":
        raise AssertionError("instance variable endpoint was not selected")
    if workspace.source_variable_query({"environment_scope": "production", "key": "REGISTRY"}) != {"filter[environment_scope]": "production"}:
        raise AssertionError("environment-scoped variable filter was not selected")
    if workspace.repository_create_path("alice", "user", "alice") != "user/repos":
        raise AssertionError("authenticated user repository endpoint was not selected")
    if workspace.repository_create_path("alice", "user", "admin") != "admin/users/alice/repos":
        raise AssertionError("administrative user repository endpoint was not selected")
    mapped = copy.deepcopy(plan)
    mapped["mappings"] = {"groups": {"platform": {"target_name": "platform-team"}}}
    if workspace.mapped_name(mapped, "groups", "platform", "fallback") != "platform-team":
        raise AssertionError("target_name group mapping was ignored")


def test_selected_nested_group_is_a_root() -> None:
    plan = base_plan()
    plan["source"]["group_paths"] = ["engineering/platform"]  # type: ignore[index]
    if workspace.group_is_subgroup(plan, "engineering/platform"):
        raise AssertionError("selected nested group was incorrectly classified as a subgroup")
    if not workspace.group_is_subgroup(plan, "engineering/platform/api"):
        raise AssertionError("child of selected nested group was not classified as a subgroup")


def test_project_rules_discovery_is_redacted_and_scoped() -> None:
    source = workspace.Endpoint("gitlab", "https://gitlab.example.test/api/v4", "GITLAB_TOKEN")
    project = {"id": 7, "path_with_namespace": "platform/control-plane"}
    protected = {
        "name": "main",
        "push_access_levels": [{"access_level": 40}],
        "merge_access_levels": [{"access_level": 40}],
        "secret": "must-not-be-exported",
    }
    with mock.patch.object(workspace, "list_pages", return_value=[protected]) as list_pages:
        result = workspace.discover_project_rules(source, project)
    if result.get("project") != "platform/control-plane" or result.get("project_id") != 7:
        raise AssertionError(f"protected-branch inventory lost project identity: {result!r}")
    if result.get("rules") != [{"name": "main", "push_access_levels": [{"access_level": 40}], "merge_access_levels": [{"access_level": 40}]}]:
        raise AssertionError(f"protected-branch inventory was not safely redacted: {result!r}")
    if list_pages.call_args.args[1] != "projects/7/protected_branches":
        raise AssertionError(f"protected-branch API was not scoped to the selected project: {list_pages.call_args!r}")


def test_ci_checkout_is_retryable() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_root = Path(temp_dir) / "ci" / "platform-control"
        (repo_root / ".git").mkdir(parents=True)
        with mock.patch.object(workspace.migration, "run_command") as run_command:
            workspace.prepare_ci_checkout("https://forgejo.example.test/platform/control-plane.git", repo_root)
        commands = [call.args[0] for call in run_command.call_args_list]
        if not any(command[0:4] == ["git", "-C", str(repo_root), "fetch"] for command in commands):
            raise AssertionError("existing CI checkout was not fetched for retry")
        if not any(command[0:4] == ["git", "-C", str(repo_root), "reset"] for command in commands):
            raise AssertionError("existing CI checkout was not reset for retry")


def test_managed_user_requires_readback() -> None:
    plan = base_plan()
    snapshot = {
        "surfaces": {
            "users": {
                "items": [{"username": "alice", "public_email": "alice@example.test"}]
            }
        }
    }
    destination = object()
    with (
        mock.patch.dict(workspace.os.environ, {"IMPORT_PASSWORD": "temporary-password"}),
        mock.patch.object(
            workspace,
            "forgejo_user",
            side_effect=[(404, {}), (200, {"login": "alice"})],
        ) as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.import_users(plan, destination, snapshot)  # type: ignore[arg-type]
    if result.get("verified") is not True or result.get("verified_count") != 1:
        raise AssertionError(f"managed user was not proven by read-back: {result!r}")
    if user_probe.call_count != 2:
        raise AssertionError("new Forgejo user was not read back after creation")
    if not any(call.args[1:3] == ("POST", "admin/users") for call in api_request.call_args_list):
        raise AssertionError("Forgejo administrative user create was not requested")

    with (
        mock.patch.dict(workspace.os.environ, {"IMPORT_PASSWORD": "temporary-password"}),
        mock.patch.object(workspace, "forgejo_user", side_effect=[(404, {}), (404, {})]),
        mock.patch.object(workspace, "request"),
    ):
        try:
            workspace.import_users(plan, destination, snapshot)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "not readable after reconciliation" not in str(exc):
                raise AssertionError(f"unexpected user read-back failure: {exc}") from exc
        else:
            raise AssertionError("managed user import accepted a missing read-back")


def test_user_mapping_collision_fails_before_mutation() -> None:
    plan = base_plan()
    plan["mappings"] = {"users": {"alice": "shared", "bob": "shared"}}
    snapshot = {
        "surfaces": {
            "users": {
                "items": [
                    {"username": "alice", "public_email": "alice@example.test"},
                    {"username": "bob", "public_email": "bob@example.test"},
                ]
            }
        }
    }
    with (
        mock.patch.object(workspace, "forgejo_user") as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "unique targets" not in str(exc):
                raise AssertionError(f"unexpected user mapping collision diagnostic: {exc}") from exc
        else:
            raise AssertionError("duplicate Forgejo user target unexpectedly passed")
    if user_probe.called or api_request.called:
        raise AssertionError("user mapping collision was detected after destination mutation")


def test_variable_environment_collision_fails_before_mutation() -> None:
    plan = base_plan()
    snapshot = {
        "surfaces": {
            "variables": {
                "items": [
                    {
                        "project": "platform/control-plane",
                        "variables": [
                            {"source_scope": "project", "key": "DEPLOY_TOKEN", "environment_scope": "*"},
                            {"source_scope": "project", "key": "DEPLOY_TOKEN", "environment_scope": "production"},
                        ],
                    }
                ]
            }
        },
        "indexes": {
            "projects": [
                {
                    "project": {"path_with_namespace": "platform/control-plane", "id": 7},
                    "destination": {"owner": "platform", "repo": "control-plane"},
                }
            ]
        },
    }
    with (
        mock.patch.object(cutover, "service_target", return_value=object()),
        mock.patch.object(cutover, "woodpecker_lookup") as lookup,
        mock.patch.object(cutover, "service_request") as service_request,
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_variables(plan, snapshot)
        except workspace.WorkspaceError as exc:
            if "same Woodpecker secret" not in str(exc):
                raise AssertionError(f"unexpected variable collision diagnostic: {exc}") from exc
        else:
            raise AssertionError("environment-scoped variable collision unexpectedly passed")
    if lookup.called or service_request.called or api_request.called:
        raise AssertionError("variable collision was detected after destination mutation")


def test_mapped_variable_is_non_mutating() -> None:
    plan = base_plan()
    plan["mappings"] = {"variables": {"project:DEPLOY_TOKEN:*": {"mode": "mapped"}}}
    snapshot = {
        "surfaces": {
            "variables": {
                "items": [
                    {
                        "project": "platform/control-plane",
                        "variables": [{"source_scope": "project", "key": "DEPLOY_TOKEN", "environment_scope": "*"}],
                    }
                ]
            }
        },
        "indexes": {
            "projects": [
                {
                    "project": {"path_with_namespace": "platform/control-plane", "id": 7},
                    "destination": {"owner": "platform", "repo": "control-plane"},
                }
            ]
        },
    }
    with (
        mock.patch.object(cutover, "service_target", return_value=object()),
        mock.patch.object(cutover, "woodpecker_lookup") as lookup,
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.import_variables(plan, snapshot)
    if result.get("verified") is not True or result.get("items") != [{"project": "platform/control-plane", "identity": "project:DEPLOY_TOKEN:*", "mode": "mapped", "verified": True}]:
        raise AssertionError(f"mapped variable was not recorded as non-mutating: {result!r}")
    if lookup.called or api_request.called:
        raise AssertionError("mapped variable unexpectedly contacted a destination or source API")


def test_team_membership_is_reconciled_and_verified() -> None:
    destination = object()
    teams = {50: 1, 40: 2, 30: 3, 20: 4, 10: 5}

    def team_members(_destination: object, path: str, **_kwargs: object) -> list[dict[str, str]]:
        return [{"login": "alice"}] if path == "teams/2/members" else []

    with (
        mock.patch.object(workspace, "request") as api_request,
        mock.patch.object(workspace, "list_pages", side_effect=team_members),
    ):
        workspace.reconcile_team_membership(destination, teams, 40, "alice")  # type: ignore[arg-type]
    methods = [call.args[1] for call in api_request.call_args_list]
    if methods.count("PUT") != 1 or methods.count("DELETE") != 4:
        raise AssertionError(f"team membership was not reconciled exactly: {methods!r}")

    with (
        mock.patch.object(workspace, "request"),
        mock.patch.object(workspace, "list_pages", return_value=[]),
    ):
        try:
            workspace.reconcile_team_membership(destination, teams, 40, "alice")  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "membership read-back mismatch" not in str(exc):
                raise AssertionError(f"unexpected membership read-back failure: {exc}") from exc
        else:
            raise AssertionError("team membership import accepted a missing read-back")


def test_team_permission_fails_closed() -> None:
    with mock.patch.object(
        workspace,
        "list_pages",
        return_value=[{"id": 7, "name": "gitlab-owners", "permission": "write"}],
    ):
        try:
            workspace.ensure_team(object(), "platform", "gitlab-owners", "admin")  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "permission mismatch" not in str(exc):
                raise AssertionError(f"unexpected team permission failure: {exc}") from exc
        else:
            raise AssertionError("team permission mismatch was accepted")


def test_recursive_group_discovery_keeps_direct_and_effective_members() -> None:
    plan = base_plan()
    plan["surfaces"]["subgroups"] = {"mode": "managed", "include_subgroups": True}  # type: ignore[index]
    endpoint = object()

    def get_group(_source: object, path: str, **_kwargs: object) -> dict[str, object]:
        if path == "groups/platform":
            return {"id": 1, "full_path": "platform", "name": "Platform"}
        raise AssertionError(f"unexpected group lookup: {path}")

    def pages(_source: object, path: str, **_kwargs: object) -> list[dict[str, object]]:
        groups = {
            "groups/1/subgroups": [{"id": 2, "full_path": "platform/child", "name": "Child"}],
            "groups/2/subgroups": [{"id": 3, "full_path": "platform/child/grand", "name": "Grand"}],
            "groups/3/subgroups": [],
        }
        if path in groups:
            return groups[path]
        if path.endswith("/members"):
            return [{"username": "direct", "access_level": 30}]
        if path.endswith("/members/all"):
            return [
                {"username": "direct", "access_level": 30},
                {"username": "inherited", "access_level": 20},
            ]
        raise AssertionError(f"unexpected group page: {path}")

    with (
        mock.patch.object(workspace, "get_endpoint_value", side_effect=get_group),
        mock.patch.object(workspace, "list_pages", side_effect=pages),
    ):
        groups = workspace.discover_groups(endpoint, plan)  # type: ignore[arg-type]
    paths = [item["full_path"] for item in groups]
    if paths != ["platform", "platform/child", "platform/child/grand"]:
        raise AssertionError(f"nested groups were not discovered recursively: {paths!r}")
    if groups[1]["direct_members"] != [{"username": "direct", "access_level": 30}]:
        raise AssertionError("direct group membership was not retained")
    if len(groups[1]["effective_members"]) != 2:
        raise AssertionError("effective group membership was not retained")


def test_role_mapping_supports_custom_roles_and_fails_closed() -> None:
    plan = base_plan()
    plan["surfaces"]["memberships"] = {  # type: ignore[index]
        "mode": "managed",
        "role_mappings": {"custom:9001": {"permission": "write", "team": "release-reviewers"}},
    }
    custom = {"username": "alice", "access_level": 20, "member_role_id": 9001}
    resolved = workspace.resolve_member_role(plan, custom, "memberships")
    if resolved["permission"] != "write" or resolved["team"] != "release-reviewers":
        raise AssertionError(f"custom GitLab role mapping was not honored: {resolved!r}")
    try:
        workspace.resolve_member_role(plan, {"username": "bob", "access_level": 30, "member_role_id": 9002}, "memberships")
    except workspace.WorkspaceError as exc:
        if "not mapped" not in str(exc):
            raise AssertionError(f"unexpected custom-role diagnostic: {exc}") from exc
    else:
        raise AssertionError("unmapped custom GitLab role was collapsed into a base role")


def test_permission_surface_validation_requires_safe_exact_confirmation() -> None:
    plan = base_plan()
    plan["surfaces"]["permissions"] = {"mode": "managed", "reconcile": "exact"}  # type: ignore[index]
    expect_error(plan, "accepted=true and a reason")
    plan["surfaces"]["permissions"]["accepted"] = True  # type: ignore[index]
    plan["surfaces"]["permissions"]["reason"] = "verified migration scope"  # type: ignore[index]
    workspace.validate_plan(plan)
    invalid = copy.deepcopy(plan)
    invalid["surfaces"]["permissions"]["role_mappings"] = {"30": "execute"}  # type: ignore[index]
    expect_error(invalid, "must be one of")


def test_membership_import_uses_direct_members_by_default() -> None:
    plan = base_plan()
    plan["surfaces"]["memberships"] = {"mode": "managed"}  # type: ignore[index]
    snapshot = {
        "surfaces": {
            "memberships": {
                "items": [
                    {
                        "group": "platform",
                        "direct_members": [{"username": "alice", "access_level": 30}],
                        "effective_members": [
                            {"username": "alice", "access_level": 30},
                            {"username": "bob", "access_level": 40},
                        ],
                    }
                ]
            }
        }
    }
    group_result = {"organizations": {"platform": "platform"}}
    with (
        mock.patch.object(workspace, "ensure_team", return_value=7),
        mock.patch.object(workspace, "reconcile_team_membership") as reconcile,
        mock.patch.object(workspace, "forgejo_user") as user_probe,
    ):
        result = workspace.import_memberships(plan, object(), snapshot, group_result, {"alice"})  # type: ignore[arg-type]
    if result.get("verified") is not True or reconcile.call_count != 1:
        raise AssertionError(f"direct membership import was not verified: {result!r}")
    call = reconcile.call_args
    if call.args[2:] != ("gitlab-developers", "alice"):
        raise AssertionError(f"inherited membership was incorrectly materialized: {call!r}")
    if set(call.args[1]) != {
        "gitlab-owners",
        "gitlab-maintainers",
        "gitlab-developers",
        "gitlab-reporters",
        "gitlab-guests",
    }:
        raise AssertionError("empty managed role teams were omitted from downgrade reconciliation")
    if user_probe.called:
        raise AssertionError("known imported user was probed unnecessarily")


def _permission_plan() -> dict[str, object]:
    plan = base_plan()
    plan["surfaces"]["permissions"] = {  # type: ignore[index]
        "mode": "managed",
        "include_direct": True,
        "include_inherited": True,
        "group_strategy": "both",
    }
    return plan


def _permission_snapshot() -> dict[str, object]:
    return {
        "surfaces": {
            "permissions": {
                "items": [
                    {
                        "project": "platform/control-plane",
                        "direct_members": [
                            {"username": "alice", "access_level": 20},
                            {"username": "alice", "access_level": 40},
                        ],
                        "effective_members": [{"username": "bob", "access_level": 30}],
                    }
                ]
            }
        },
        "indexes": {
            "projects": [
                {
                    "project": {
                        "path_with_namespace": "platform/control-plane",
                        "namespace": {"full_path": "platform", "kind": "group"},
                    },
                    "destination": {
                        "owner": "platform",
                        "repo": "control-plane",
                        "owner_kind": "organization",
                    },
                }
            ]
        },
    }


def test_permission_import_merges_effective_access_and_verifies_repo_teams() -> None:
    plan = _permission_plan()
    snapshot = _permission_snapshot()
    group_result = {
        "organizations": {"platform": "platform"},
        "teams": {
            "platform": {
                "gitlab-reporters": {"id": 7, "name": "gitlab-reporters", "permission": "read"},
                "gitlab-developers": {"id": 8, "name": "gitlab-developers", "permission": "write"},
            }
        },
    }
    calls: list[tuple[str, str, object]] = []

    def api(_destination: object, method: str, path: str, **kwargs: object) -> object:
        calls.append((method, path, kwargs.get("body")))
        if method == "GET" and path.endswith("/permission"):
            return 200, {"permission": "write"}
        return {}

    with (
        mock.patch.object(workspace, "request", side_effect=api),
        mock.patch.object(workspace, "repository_teams", return_value=[{"name": "gitlab-reporters"}, {"name": "gitlab-developers"}]),
    ):
        result = workspace.import_permissions(plan, snapshot, object(), group_result, {"alice", "bob"})  # type: ignore[arg-type]
    if result.get("verified") is not True:
        raise AssertionError(f"permission import was not verified: {result!r}")
    collaborator_puts = [call for call in calls if call[0] == "PUT" and "/collaborators/" in call[1]]
    if {call[1].rsplit("/", 1)[-1] for call in collaborator_puts} != {"alice", "bob"}:
        raise AssertionError(f"effective project collaborators were not reconciled: {collaborator_puts!r}")
    alice_body = next(call[2] for call in collaborator_puts if call[1].endswith("/alice"))
    if alice_body != {"permission": "write"}:
        raise AssertionError("strongest duplicate project permission did not win")
    team_puts = [call for call in calls if call[0] == "PUT" and "/teams/" in call[1]]
    if len(team_puts) != 2:
        raise AssertionError(f"group teams were not attached to the repository: {team_puts!r}")


def test_exact_permission_reconciliation_does_not_remove_unmanaged_collaborators() -> None:
    plan = _permission_plan()
    plan["surfaces"]["permissions"].update({"reconcile": "exact", "accepted": True, "reason": "approved"})  # type: ignore[index]
    snapshot = _permission_snapshot()
    snapshot["surfaces"]["permissions"]["items"][0]["effective_members"].append({"username": "stale", "access_level": 0})  # type: ignore[index]
    calls: list[tuple[str, str, object]] = []

    def api(_destination: object, method: str, path: str, **kwargs: object) -> object:
        calls.append((method, path, kwargs.get("body")))
        if method == "GET" and path.endswith("/permission"):
            return (200, {"permission": "write"}) if any(path.endswith(f"/{name}/permission") for name in ("alice", "bob")) else (404, {})
        if method == "GET" and path.endswith("/collaborators"):
            return [{"login": "alice"}, {"login": "unmanaged"}, {"login": "stale"}]
        return {}

    with (
        mock.patch.object(workspace, "request", side_effect=api),
        mock.patch.object(workspace, "repository_teams", return_value=[]),
        mock.patch.object(workspace, "list_pages", return_value=[{"login": "alice"}, {"login": "unmanaged"}, {"login": "stale"}]),
    ):
        result = workspace.import_permissions(plan, snapshot, object(), None, {"alice", "bob", "stale"})  # type: ignore[arg-type]
    if result.get("verified") is not True:
        raise AssertionError("exact permission reconciliation was not verified")
    deletes = [call[1] for call in calls if call[0] == "DELETE"]
    if not any(path.endswith("/stale") for path in deletes):
        raise AssertionError("managed stale collaborator was not removed in exact mode")
    if any(path.endswith("/unmanaged") for path in deletes):
        raise AssertionError("unmanaged collaborator was removed in exact mode")


def test_permission_readback_fails_closed() -> None:
    plan = _permission_plan()
    snapshot = _permission_snapshot()

    def api(_destination: object, method: str, path: str, **_kwargs: object) -> object:
        if method == "GET" and path.endswith("/permission"):
            return 200, {"permission": "read"}
        return {}

    with (
        mock.patch.object(workspace, "request", side_effect=api),
        mock.patch.object(workspace, "repository_teams", return_value=[]),
    ):
        try:
            workspace.import_permissions(plan, snapshot, object(), None, {"alice", "bob"})  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "permission mismatch" not in str(exc):
                raise AssertionError(f"unexpected collaborator read-back error: {exc}") from exc
        else:
            raise AssertionError("permission import accepted a weaker read-back permission")


def test_rule_import_runs_after_repository_exists_and_passes_policy() -> None:
    plan = base_plan()
    plan["surfaces"]["rules"] = {  # type: ignore[index]
        "mode": "managed",
        "reconcile": "additive",
        "gitlab_maintainer_team": "gitlab-maintainers",
    }
    snapshot = {
        "surfaces": {
            "rules": {
                "items": [
                    {
                        "project": "platform/control-plane",
                        "rules": [{"name": "main"}],
                    }
                ]
            }
        },
        "indexes": {
            "projects": [
                {
                    "project": {
                        "id": 7,
                        "path_with_namespace": "platform/control-plane",
                        "http_url_to_repo": "https://gitlab.example.test/platform/control-plane.git",
                        "visibility": "private",
                    },
                    "destination": {
                        "owner": "platform",
                        "repo": "control-plane",
                        "owner_kind": "organization",
                        "git_url": "ssh://git@forgejo.example.test/platform/control-plane.git",
                    },
                }
            ]
        },
    }
    with (
        mock.patch.object(workspace, "request", return_value=(200, {})) as request,
        mock.patch.object(workspace, "list_pages", return_value=[{"name": "gitlab-maintainers"}]),
        mock.patch.object(
            workspace.migration,
            "migrate_branch_protections",
            return_value={"verified": True, "created": 1, "reconcile": "additive"},
        ) as migrate,
    ):
        result = workspace.import_rules(plan, snapshot, object())  # type: ignore[arg-type]
    if result.get("verified") is not True or len(result.get("items") or []) != 1:
        raise AssertionError(f"protected-branch import was not verified: {result!r}")
    if request.call_args.args[1:3] != ("GET", "repos/platform/control-plane"):
        raise AssertionError("protected-branch import did not verify the destination repository first")
    repo = migrate.call_args.args[0]
    if repo.metadata.get("branch_protection") != {
        "mode": "required",
        "reconcile": "additive",
        "gitlab_maintainer_team": "gitlab-maintainers",
    }:
        raise AssertionError(f"workspace rule policy was not passed to repository migration: {repo.metadata!r}")


def test_ci_destination_and_remote_proof() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_root = Path(temp_dir) / "checkout"
        repo_root.mkdir()
        destination, relative = workspace.safe_ci_destination(repo_root, ".woodpecker/build.yml")
        if destination != repo_root / ".woodpecker" / "build.yml" or relative != ".woodpecker/build.yml":
            raise AssertionError("safe CI destination was normalized incorrectly")
        for unsafe in ("../outside.yml", "/absolute.yml", "C:\\absolute.yml", "nested//empty.yml"):
            try:
                workspace.safe_ci_destination(repo_root, unsafe)
            except workspace.WorkspaceError:
                pass
            else:
                raise AssertionError(f"unsafe CI destination was accepted: {unsafe!r}")

        content = "steps:\n  test:\n    image: alpine\n"
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        readback = mock.Mock(returncode=0, stdout=content, stderr="")
        with mock.patch.object(
            workspace.migration,
            "run_command",
            side_effect=[completed, readback],
        ):
            verified = workspace.verify_ci_remote_files(
                repo_root,
                [(".woodpecker/build.yml", content)],
            )
        if verified[0].get("sha256") != workspace.hashlib.sha256(content.encode("utf-8")).hexdigest():
            raise AssertionError("CI remote proof did not include the expected digest")

        wrong_readback = mock.Mock(returncode=0, stdout="different\n", stderr="")
        with mock.patch.object(
            workspace.migration,
            "run_command",
            side_effect=[completed, wrong_readback],
        ):
            try:
                workspace.verify_ci_remote_files(repo_root, [(".woodpecker/build.yml", content)])
            except workspace.WorkspaceError as exc:
                if "read-back mismatch" not in str(exc):
                    raise AssertionError(f"unexpected CI read-back failure: {exc}") from exc
            else:
                raise AssertionError("converted CI import accepted mismatched remote content")


def test_ci_commit_is_idempotent() -> None:
    repo_root = Path("checkout")
    unchanged = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(
        workspace.migration,
        "run_command",
        side_effect=[mock.Mock(returncode=0), unchanged],
    ) as run_command:
        action = workspace.commit_ci_changes(repo_root, [".woodpecker.yml"])
    if action != "unchanged" or run_command.call_count != 2:
        raise AssertionError("unchanged CI import attempted to create another commit")

    changed = mock.Mock(returncode=1, stdout="", stderr="")
    with mock.patch.object(
        workspace.migration,
        "run_command",
        side_effect=[mock.Mock(returncode=0), changed, mock.Mock(returncode=0), mock.Mock(returncode=0)],
    ) as run_command:
        action = workspace.commit_ci_changes(repo_root, [".woodpecker.yml"])
    if action != "committed" or run_command.call_count != 4:
        raise AssertionError("changed CI import did not commit and push exactly once")


def test_pipeline_schedule_import_is_not_history_import() -> None:
    plan = base_plan()
    snapshot = {
        "surfaces": {
            "pipelines": {
                "items": [
                    {
                        "project": "platform/control-plane",
                        "pipelines": {
                            "runs": [{"id": 1}],
                            "schedules": [{"id": 4, "description": "nightly", "cron": "0 2 * * *", "ref": "main", "active": True}],
                            "triggers": [],
                        },
                    }
                ]
            }
        },
        "indexes": {
            "projects": [
                {
                    "project": {"path_with_namespace": "platform/control-plane", "default_branch": "main"},
                    "destination": {"owner": "platform", "repo": "control-plane", "git_url": "ssh://git@forgejo.example.test/platform/control-plane.git"},
                }
            ]
        },
    }
    fake_target = object()
    with (
        mock.patch.object(cutover, "service_target", return_value=fake_target),
        mock.patch.object(cutover, "woodpecker_lookup", return_value={"id": 41}),
        mock.patch.object(cutover, "woodpecker_cron_upsert", return_value={"action": "created", "verified": True}) as upsert,
    ):
        result = workspace.import_pipelines(plan, snapshot)
    if result.get("verified") is not True or result.get("history_imported") is not False:
        raise AssertionError(f"pipeline schedule import result was not verified: {result!r}")
    if upsert.call_args.args[1:] != (41, "gitlab-schedule-4", "0 2 * * *", "main", False):
        raise AssertionError(f"unexpected Woodpecker cron mapping: {upsert.call_args!r}")

    active_mapping = copy.deepcopy(plan)
    active_mapping["surfaces"]["pipelines"]["schedule_mappings"] = {  # type: ignore[index]
        "4": {"name": "nightly", "enabled": True}
    }
    with (
        mock.patch.object(cutover, "service_target", return_value=fake_target),
        mock.patch.object(cutover, "woodpecker_lookup", return_value={"id": 41}),
        mock.patch.object(cutover, "woodpecker_cron_upsert") as active_upsert,
    ):
        try:
            workspace.import_pipelines(active_mapping, snapshot)
        except workspace.WorkspaceError as exc:
            if "cannot be enabled before cutover" not in str(exc):
                raise AssertionError(f"unexpected schedule activation diagnostic: {exc}") from exc
        else:
            raise AssertionError("workspace import unexpectedly enabled a schedule")
    if active_upsert.called:
        raise AssertionError("workspace import attempted to activate a schedule")


def main() -> int:
    test_selective_plan_contract()
    test_redaction_and_destination_url()
    test_selected_nested_group_is_a_root()
    test_project_rules_discovery_is_redacted_and_scoped()
    test_ci_checkout_is_retryable()
    test_managed_user_requires_readback()
    test_user_mapping_collision_fails_before_mutation()
    test_variable_environment_collision_fails_before_mutation()
    test_mapped_variable_is_non_mutating()
    test_team_membership_is_reconciled_and_verified()
    test_team_permission_fails_closed()
    test_recursive_group_discovery_keeps_direct_and_effective_members()
    test_role_mapping_supports_custom_roles_and_fails_closed()
    test_permission_surface_validation_requires_safe_exact_confirmation()
    test_membership_import_uses_direct_members_by_default()
    test_permission_import_merges_effective_access_and_verifies_repo_teams()
    test_exact_permission_reconciliation_does_not_remove_unmanaged_collaborators()
    test_permission_readback_fails_closed()
    test_rule_import_runs_after_repository_exists_and_passes_policy()
    test_ci_destination_and_remote_proof()
    test_ci_commit_is_idempotent()
    test_pipeline_schedule_import_is_not_history_import()
    print("Forge workspace migration self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
