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
    test_pipeline_schedule_import_is_not_history_import()
    print("Forge workspace migration self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
