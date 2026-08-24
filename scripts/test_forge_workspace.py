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
    if upsert.call_args.args[1:] != (41, "gitlab-schedule-4", "0 2 * * *", "main", True):
        raise AssertionError(f"unexpected Woodpecker cron mapping: {upsert.call_args!r}")


def main() -> int:
    test_selective_plan_contract()
    test_redaction_and_destination_url()
    test_selected_nested_group_is_a_root()
    test_ci_checkout_is_retryable()
    test_managed_user_requires_readback()
    test_user_mapping_collision_fails_before_mutation()
    test_variable_environment_collision_fails_before_mutation()
    test_mapped_variable_is_non_mutating()
    test_team_membership_is_reconciled_and_verified()
    test_team_permission_fails_closed()
    test_ci_destination_and_remote_proof()
    test_ci_commit_is_idempotent()
    test_pipeline_schedule_import_is_not_history_import()
    print("Forge workspace migration self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
