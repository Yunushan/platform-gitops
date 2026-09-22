#!/usr/bin/env python3
"""Self-test selective GitLab workspace export/import contracts."""

from __future__ import annotations

import copy
import hashlib
import json
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

    all_scope = copy.deepcopy(plan)
    all_scope["source"].update({  # type: ignore[index]
        "project_paths": [],
        "group_paths": [],
        "usernames": [],
        "all_available_groups": True,
    })
    all_scope["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "all_available": True,
        "default_password_env": "IMPORT_PASSWORD",
    }
    workspace.validate_plan(all_scope)

    unsafe_schedule_activation = copy.deepcopy(plan)
    unsafe_schedule_activation["surfaces"]["pipelines"]["schedule_mappings"] = {  # type: ignore[index]
        "4": {"name": "nightly", "enabled": True}
    }
    expect_error(unsafe_schedule_activation, "cannot enable a schedule during workspace import")

    members_without_users = copy.deepcopy(plan)
    members_without_users["surfaces"]["users"] = {"mode": "skip"}  # type: ignore[index]
    members_without_users["surfaces"]["groups"] = {"mode": "managed"}  # type: ignore[index]
    expect_error(members_without_users, "members_mode=skip|mapped|manual")

    insecure_source = copy.deepcopy(plan)
    insecure_source["source"]["api_url"] = "http://gitlab.example.test/api/v4"  # type: ignore[index]
    expect_error(insecure_source, "must use HTTPS")
    insecure_source["source"]["allow_insecure_http"] = True  # type: ignore[index]
    workspace.validate_plan(insecure_source)
    invalid_insecure_flag = copy.deepcopy(insecure_source)
    invalid_insecure_flag["source"]["allow_insecure_http"] = "flase"  # type: ignore[index]
    expect_error(invalid_insecure_flag, "source.allow_insecure_http must be a boolean")

    invalid_exclusions = copy.deepcopy(plan)
    invalid_exclusions["surfaces"]["users"]["excluded_usernames"] = "ghost"  # type: ignore[index]
    expect_error(invalid_exclusions, "excluded_usernames must contain non-empty strings")


def test_export_requires_gitlab_token_before_discovery() -> None:
    plan = base_plan()
    with mock.patch.dict("os.environ", {"GITLAB_TOKEN": ""}):
        with mock.patch.object(workspace, "discover_groups") as discover_groups:
            try:
                workspace.export_workspace(plan)
            except workspace.WorkspaceError as exc:
                assert "GITLAB_TOKEN" in str(exc)
            else:
                raise AssertionError("missing GitLab token must block export")
            discover_groups.assert_not_called()


def test_import_and_audit_require_forgejo_token() -> None:
    plan = base_plan()
    for argv, operation in (
        (["import", "plan.json", "--snapshot", "snapshot.json", "--work-dir", "work"], "import"),
        (["audit-users", "plan.json", "--snapshot", "snapshot.json"], "audit"),
    ):
        args = workspace.parse_args(argv)
        with (
            mock.patch.dict("os.environ", {"FORGEJO_TOKEN": ""}),
            mock.patch.object(workspace, "load_plan", return_value=plan),
            mock.patch.object(workspace, "require_snapshot", return_value={}),
            mock.patch.object(workspace, "import_workspace") as importer,
            mock.patch.object(workspace, "audit_users") as auditor,
        ):
            try:
                args.handler(args)
            except workspace.WorkspaceError as exc:
                if "FORGEJO_TOKEN" not in str(exc):
                    raise AssertionError(f"unexpected {operation} credential error: {exc}") from exc
            else:
                raise AssertionError(f"{operation} accepted an absent Forgejo token")
            importer.assert_not_called()
            auditor.assert_not_called()


def test_import_email_reconciliation_flag_preserves_saved_plan() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "send_notify": True,
    }
    original = copy.deepcopy(plan)
    args = workspace.parse_args(
        [
            "import",
            "plan.json",
            "--snapshot", "snapshot.json",
            "--work-dir", "work",
            "--reconcile-existing-emails",
            "--no-send-notify",
            "--password-file", "private/migrations/proof/new-passwords.json",
        ]
    )
    with (
        mock.patch.dict("os.environ", {"FORGEJO_TOKEN": "test-token"}),
        mock.patch.object(workspace, "load_plan", return_value=plan),
        mock.patch.object(workspace, "require_snapshot", return_value={}),
        mock.patch.object(workspace, "import_workspace", return_value={"verified": True, "surfaces": {}}) as importer,
        mock.patch("builtins.print"),
    ):
        assert args.handler(args) == 0
    runtime_plan = importer.call_args.args[0]
    runtime_users = runtime_plan["surfaces"]["users"]
    if runtime_users.get("reconcile_existing_emails") is not True or runtime_users.get("send_notify") is not False:
        raise AssertionError("import CLI did not pass both explicit user overrides")
    if plan != original or runtime_plan is plan:
        raise AssertionError("import CLI mutated the saved plan instead of a runtime copy")
    if importer.call_args.kwargs.get("password_output") != args.password_file:
        raise AssertionError("import CLI lost the private password handoff path")


def test_import_mail_confirmation_is_runtime_only() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "send_notify": True,
    }
    original = copy.deepcopy(plan)
    args = workspace.parse_args(
        ["import", "plan.json", "--snapshot", "snapshot.json", "--work-dir", "work", "--confirm-mail-delivery"]
    )
    with (
        mock.patch.dict("os.environ", {"FORGEJO_TOKEN": "test-token"}),
        mock.patch.object(workspace, "load_plan", return_value=plan),
        mock.patch.object(workspace, "require_snapshot", return_value={}),
        mock.patch.object(workspace, "import_workspace", return_value={"verified": True, "surfaces": {}}) as importer,
        mock.patch("builtins.print"),
    ):
        assert args.handler(args) == 0
    if importer.call_args.kwargs.get("mail_delivery_confirmed") is not True or plan != original:
        raise AssertionError("mail delivery confirmation was not a runtime-only import choice")

    args = workspace.parse_args(
        [
            "import", "plan.json", "--snapshot", "snapshot.json", "--work-dir", "work",
            "--confirm-mail-delivery", "--no-send-notify", "--password-file", "private/passwords.json",
        ]
    )
    with (
        mock.patch.dict("os.environ", {"FORGEJO_TOKEN": "test-token"}),
        mock.patch.object(workspace, "load_plan", return_value=plan),
        mock.patch.object(workspace, "require_snapshot", return_value={}),
        mock.patch.object(workspace, "import_workspace") as importer,
    ):
        try:
            args.handler(args)
        except workspace.WorkspaceError as exc:
            if "cannot be combined" not in str(exc):
                raise AssertionError(f"unexpected mail confirmation combination error: {exc}") from exc
        else:
            raise AssertionError("contradictory mail confirmation flags unexpectedly passed")
        importer.assert_not_called()


def test_membership_only_users_are_hydrated_before_email_export() -> None:
    plan = base_plan()
    plan["source"]["usernames"] = []  # type: ignore[index]
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "include_members": True,
        "include_email_for_account_creation": True,
    }

    def pages(_source: object, path: str, **kwargs: object) -> list[dict[str, object]]:
        if path == "users" and kwargs.get("query") == {"username": "member-only"}:
            return [{"username": "member-only", "email": "member-only@example.test"}]
        raise AssertionError(f"unexpected user hydration request: {path} {kwargs!r}")

    with mock.patch.object(workspace, "list_pages", side_effect=pages):
        users = workspace.discover_users(
            object(),
            plan,
            groups=[{"effective_members": [{"username": "member-only", "access_level": 30}]}],
            project_permissions=[],
        )
    if users != [{"username": "member-only", "email": "member-only@example.test"}]:
        raise AssertionError(f"membership-only user email was not hydrated: {users!r}")


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
    grouped_project = {
        "path_with_namespace": "engineering/platform/control-plane",
        "path": "control-plane",
        "namespace": {"kind": "group", "full_path": "engineering/platform"},
    }
    mapped["surfaces"]["subgroups"] = {"mode": "managed"}  # type: ignore[index]
    owner, repo = workspace.destination_name(mapped, grouped_project)
    if owner != "engineering-platform" or repo != "control-plane":
        raise AssertionError("managed group projects were not assigned to their deterministic Forgejo organization")


def test_long_group_targets_are_forgejo_compatible_and_stable() -> None:
    long_path = "rayli-sistemler-yolcu-bilgilendirme-merkez-yazilimi"
    target = workspace.default_group_target_name(long_path)
    if len(target) > workspace.FORGEJO_ORGANIZATION_USERNAME_MAX_LENGTH:
        raise AssertionError(f"long group target exceeded Forgejo's limit: {target!r}")
    if not target.endswith("-" + hashlib.sha256(long_path.encode("utf-8")).hexdigest()[:8]):
        raise AssertionError(f"long group target did not retain a stable hash suffix: {target!r}")
    if workspace.default_group_target_name("short-group") != "short-group":
        raise AssertionError("short group target was changed unexpectedly")
    other_path = long_path[:-1] + "x"
    if workspace.default_group_target_name(other_path) == target:
        raise AssertionError("different long group paths collided")
    plan = base_plan()
    plan["mappings"] = {"groups": {"platform": "x" * 41}}
    try:
        workspace.validate_unique_group_targets(plan, [{"full_path": "platform"}])
    except workspace.WorkspaceError as exc:
        if "40-character username limit" not in str(exc):
            raise AssertionError(f"unexpected long explicit group mapping diagnostic: {exc}") from exc
    else:
        raise AssertionError("overlong explicit group mapping unexpectedly passed")


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


def test_project_permission_discovery_materializes_invited_group_members() -> None:
    source = workspace.Endpoint("gitlab", "https://gitlab.example.test/api/v4", "GITLAB_TOKEN")
    project = {"id": 7, "path_with_namespace": "platform/control-plane"}
    invited = {
        "id": 42,
        "full_path": "shared/release",
        "group_access_level": 20,
    }
    with (
        mock.patch.object(
            workspace,
            "list_pages",
            side_effect=[
                [{"username": "direct", "access_level": 30}],
                [{"username": "inherited", "access_level": 40}],
                [
                    {"username": "release-owner", "access_level": 40},
                    {"username": "release-reporter", "access_level": 20},
                ],
            ],
        ) as list_pages,
        mock.patch.object(workspace, "list_pages_optional", return_value=[invited]),
    ):
        result = workspace.discover_project_permissions(source, project)
    invited_members = result.get("invited_group_members")
    if invited_members != [
        {
            "username": "release-owner",
            "access_level": 20,
            "invited_group_access_level": 20,
            "invited_group": "shared/release",
        },
        {
            "username": "release-reporter",
            "access_level": 20,
            "invited_group_access_level": 20,
            "invited_group": "shared/release",
        },
    ]:
        raise AssertionError(f"invited group access was not materialized at the invitation cap: {invited_members!r}")
    if list_pages.call_args_list[-1].args[1] != "groups/42/members/all":
        raise AssertionError("invited group members were not queried through the scoped group API")


def test_managed_import_rejects_missing_snapshot_surface_before_mutation() -> None:
    plan = copy.deepcopy(base_plan())
    plan["surfaces"] = {  # type: ignore[index]
        "users": {"mode": "managed", "default_password_env": "IMPORT_PASSWORD"},
    }
    snapshot = {"surfaces": {}}
    try:
        workspace.validate_import_snapshot_contract(plan, snapshot)  # type: ignore[arg-type]
    except workspace.WorkspaceError as exc:
        if "users snapshot surface is missing" not in str(exc):
            raise AssertionError(f"unexpected missing-surface diagnostic: {exc}") from exc
    else:
        raise AssertionError("managed import accepted a missing users snapshot surface")


def test_all_available_group_discovery_includes_top_level_groups() -> None:
    plan = base_plan()
    plan["source"]["group_paths"] = []  # type: ignore[index]
    plan["source"]["all_available_groups"] = True  # type: ignore[index]
    group = {"id": 9, "full_path": "platform", "path": "platform", "name": "Platform"}
    with mock.patch.object(
        workspace,
        "list_pages",
        side_effect=[[group], [{"username": "alice", "access_level": 40}], [{"username": "alice", "access_level": 40}]],
    ):
        result = workspace.discover_groups(workspace.Endpoint("gitlab", "https://gitlab.example.test/api/v4", "TOKEN"), plan)
    if [item.get("full_path") for item in result] != ["platform"]:
        raise AssertionError(f"all-available group discovery omitted a top-level group: {result!r}")
    if result[0].get("direct_members") != [{"username": "alice", "access_level": 40}]:
        raise AssertionError("all-available group discovery did not retain direct memberships")


def test_all_available_project_discovery_keeps_archived_and_inherited_projects() -> None:
    plan = base_plan()
    plan["source"]["project_paths"] = []  # type: ignore[index]
    plan["source"]["group_paths"] = []  # type: ignore[index]
    plan["source"]["all_available_projects"] = True  # type: ignore[index]
    captured: list[object] = []
    project = {
        "id": 11,
        "path_with_namespace": "platform/archived-repo",
        "namespace": {"full_path": "platform", "kind": "group"},
        "archived": True,
    }

    def pages(_source: object, path: str, **kwargs: object) -> list[dict[str, object]]:
        if path != "projects":
            raise AssertionError(f"unexpected project discovery path: {path}")
        captured.append((kwargs.get("query"), kwargs.get("page_size")))
        return [project]

    with (
        mock.patch.object(workspace, "list_pages", side_effect=pages),
        mock.patch.object(workspace, "get_endpoint_value", return_value=project),
    ):
        projects = workspace.discover_projects(object(), plan, [])  # type: ignore[arg-type]
    if [item["path_with_namespace"] for item in projects] != ["platform/archived-repo"]:
        raise AssertionError("all-available project discovery dropped an archived project")
    if captured != [({"simple": True}, 25)]:
        raise AssertionError(f"all-available projects did not use compact bounded pages: {captured!r}")


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


def test_existing_hash_strategy_fails_closed_before_mutation() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "existing_hash_compatibility_required",
        "password_env_by_username": {},
    }
    snapshot = {
        "surfaces": {
            "users": {
                "items": [{"username": "alice", "public_email": "alice@example.test"}]
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
            if "existing-password-hash compatibility migration" not in str(exc):
                raise AssertionError(f"unexpected hash-strategy diagnostic: {exc}") from exc
        else:
            raise AssertionError("existing-password-hash strategy unexpectedly entered API import")
    if user_probe.called or api_request.called:
        raise AssertionError("hash-strategy guard ran after destination access")


def test_generated_passwords_are_per_user_and_not_in_proof() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "include_email_for_account_creation": True,
        "send_notify": True,
    }
    snapshot = {
        "surfaces": {
            "users": {
                "items": [
                    {"username": "alice", "email": "alice@example.test"},
                    {"username": "bob", "email": "bob@example.test"},
                ]
            }
        }
    }
    with (
        mock.patch.object(
            workspace,
            "forgejo_user",
            side_effect=[(404, {}), (200, {"login": "alice"}), (404, {}), (200, {"login": "bob"})],
        ),
        mock.patch.object(workspace, "generated_user_password", side_effect=["one-time-alice", "one-time-bob"]),
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.import_users(plan, object(), snapshot, mail_delivery_confirmed=True)  # type: ignore[arg-type]
    create_calls = [call for call in api_request.call_args_list if call.args[1:3] == ("POST", "admin/users")]
    if len(create_calls) != 2:
        raise AssertionError(f"generated-password user creates were not requested: {create_calls!r}")
    bodies = [call.kwargs.get("body") or {} for call in create_calls]
    if [body.get("password") for body in bodies] != ["one-time-alice", "one-time-bob"]:
        raise AssertionError(f"passwords were not generated independently: {bodies!r}")
    if any(body.get("must_change_password") is not True or body.get("send_notify") is not True for body in bodies):
        raise AssertionError(f"generated-password accounts were not forced through notification/change flow: {bodies!r}")
    if result.get("verified") is not True or result.get("created") != 2:
        raise AssertionError(f"generated-password import was not verified: {result!r}")
    if (
        result.get("credential_delivery") != "forgejo_password_setup_instructions_requested"
        or result.get("initial_credentials_created") != 0
        or result.get("login_verified") is not False
    ):
        raise AssertionError("Forgejo welcome mail was mistaken for delivered credentials or verified login")
    evidence = workspace.proof("import", plan, result)
    if any(secret in json.dumps(evidence) for secret in ("one-time-alice", "one-time-bob")):
        raise AssertionError("generated passwords leaked into migration proof")


def test_generated_passwords_can_use_private_handoff_without_notification() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "include_email_for_account_creation": True,
        "send_notify": False,
    }
    snapshot = {
        "surfaces": {
            "users": {
                "items": [
                    {"username": "alice", "email": "alice@example.test"},
                    {"username": "bob", "email": "bob@example.test"},
                ]
            }
        }
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        password_file = Path(temp_dir) / "initial-user-passwords.json"
        with (
            mock.patch.object(workspace, "private_password_output_path", return_value=password_file),
            mock.patch.object(
                workspace,
                "forgejo_user",
                side_effect=[(404, {}), (200, {"login": "alice"}), (404, {}), (200, {"login": "bob"})],
            ),
            mock.patch.object(workspace, "generated_user_password", side_effect=["one-time-alice", "one-time-bob"]),
            mock.patch.object(workspace, "request") as api_request,
        ):
            result = workspace.import_users(plan, object(), snapshot, password_output=password_file)  # type: ignore[arg-type]
        create_calls = [call for call in api_request.call_args_list if call.args[1:3] == ("POST", "admin/users")]
        bodies = [call.kwargs.get("body") or {} for call in create_calls]
        if len(bodies) != 2 or [body.get("password") for body in bodies] != ["one-time-alice", "one-time-bob"]:
            raise AssertionError(f"private-handoff passwords were not generated independently: {bodies!r}")
        if any(body.get("must_change_password") is not True or body.get("send_notify") is not False for body in bodies):
            raise AssertionError(f"private-handoff accounts had the wrong notification policy: {bodies!r}")
        handoff = json.loads(password_file.read_text(encoding="utf-8"))
        if [entry["password"] for entry in handoff["entries"]] != ["one-time-alice", "one-time-bob"]:
            raise AssertionError(f"private password handoff was incomplete: {handoff!r}")
        if result.get("credential_delivery") != "private_file" or result.get("login_verified") is not False:
            raise AssertionError("private handoff was mistaken for a verified login")
        evidence = workspace.proof("import", plan, result)
        if any(password in json.dumps(evidence) for password in ("one-time-alice", "one-time-bob")):
            raise AssertionError("private handoff passwords leaked into migration proof")


def test_generated_password_mail_requires_confirmation_before_destination_access() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "include_email_for_account_creation": True,
        "send_notify": True,
    }
    snapshot = {"surfaces": {"users": {"items": [{"username": "alice", "email": "alice@example.test"}]}}}
    with (
        mock.patch.object(workspace, "forgejo_user") as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "--confirm-mail-delivery" not in str(exc):
                raise AssertionError(f"unexpected mail confirmation error: {exc}") from exc
        else:
            raise AssertionError("generated password notification proceeded without a mail test")
        user_probe.assert_not_called()
        api_request.assert_not_called()


def test_generated_password_preflight_fails_before_destination_access() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "include_email_for_account_creation": True,
        "send_notify": True,
    }
    snapshot = {
        "surfaces": {
            "users": {
                "items": [
                    {"username": "alice", "email": "alice@example.test"},
                    {"username": "bob"},
                ]
            }
        }
    }
    with (
        mock.patch.object(workspace, "forgejo_user") as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_users(plan, object(), snapshot, mail_delivery_confirmed=True)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "has no real private email" not in str(exc):
                raise AssertionError(f"unexpected generated-password preflight failure: {exc}") from exc
        else:
            raise AssertionError("missing generated-password delivery address unexpectedly passed")
    if user_probe.called or api_request.called:
        raise AssertionError("generated-password delivery preflight ran after destination access")


def test_generated_password_preflight_rejects_placeholder_address() -> None:
    plan = base_plan()
    plan["surfaces"]["users"] = {  # type: ignore[index]
        "mode": "managed",
        "password_strategy": "generated_per_user",
        "include_email_for_account_creation": True,
        "send_notify": True,
    }
    snapshot = {"surfaces": {"users": {"items": [{"username": "alice", "email": "alice@migration.invalid"}]}}}
    with (
        mock.patch.object(workspace, "forgejo_user") as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_users(plan, object(), snapshot, mail_delivery_confirmed=True)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "no real private email" not in str(exc):
                raise AssertionError(f"unexpected placeholder-email preflight error: {exc}") from exc
        else:
            raise AssertionError("placeholder email passed generated-password delivery preflight")
    user_probe.assert_not_called()
    api_request.assert_not_called()


def test_managed_user_reconciles_account_flags_when_enabled() -> None:
    plan = base_plan()
    plan["surfaces"]["users"]["preserve_account_flags"] = True  # type: ignore[index]
    snapshot = {
        "surfaces": {
            "users": {
                "items": [{"username": "alice", "state": "blocked", "is_admin": True}]
            }
        }
    }
    first_read = {"login": "alice", "is_admin": False, "prohibit_login": False}
    second_read = {"login": "alice", "is_admin": True, "prohibit_login": True}
    with (
        mock.patch.object(workspace, "forgejo_user", side_effect=[(200, first_read), (200, second_read)]),
        mock.patch.object(workspace, "request", return_value=(200, {})) as api_request,
    ):
        result = workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
    if result.get("verified") is not True or result.get("updated") != 1:
        raise AssertionError(f"account flags were not reconciled: {result!r}")
    patch_calls = [call for call in api_request.call_args_list if call.args[1:3] == ("PATCH", "admin/users/alice")]
    if len(patch_calls) != 1 or patch_calls[0].kwargs.get("body") != {"admin": True, "prohibit_login": True}:
        raise AssertionError(f"unexpected account-flag patch: {patch_calls!r}")


def test_existing_email_reconciliation_requires_opt_in_and_readback() -> None:
    plan = base_plan()
    snapshot = {"surfaces": {"users": {"items": [{"username": "alice", "email": "alice@example.test"}]}}}
    current = {"login": "alice", "email": "alice@migration.invalid"}
    with (
        mock.patch.object(workspace, "forgejo_user", return_value=(200, current)),
        mock.patch.object(workspace, "request") as api_request,
    ):
        workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
    api_request.assert_not_called()

    plan["surfaces"]["users"]["reconcile_existing_emails"] = True  # type: ignore[index]
    with (
        mock.patch.object(
            workspace,
            "forgejo_user",
            side_effect=[(200, current), (200, {"login": "alice", "email": "alice@example.test"})],
        ),
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
    patch_calls = [call for call in api_request.call_args_list if call.args[1:3] == ("PATCH", "admin/users/alice")]
    if len(patch_calls) != 1 or patch_calls[0].kwargs.get("body") != {"email": "alice@example.test"}:
        raise AssertionError(f"existing placeholder email was not reconciled: {patch_calls!r}")
    if (
        result.get("updated") != 1
        or result.get("emails_updated") != 1
        or result.get("existing") != 1
        or result.get("credential_delivery") != "none"
    ):
        raise AssertionError(f"existing email reconciliation was not recorded: {result!r}")


def test_existing_email_reconciliation_refuses_real_address_before_mutation() -> None:
    plan = base_plan()
    plan["surfaces"]["users"]["reconcile_existing_emails"] = True  # type: ignore[index]
    snapshot = {"surfaces": {"users": {"items": [{"username": "alice", "email": "alice@example.test"}]}}}
    with (
        mock.patch.object(
            workspace,
            "forgejo_user",
            return_value=(200, {"login": "alice", "email": "alice@other.test"}),
        ),
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "non-placeholder email" not in str(exc):
                raise AssertionError(f"unexpected email preflight error: {exc}") from exc
        else:
            raise AssertionError("existing real email was overwritten without review")
    api_request.assert_not_called()


def test_existing_email_reconciliation_preflights_all_users() -> None:
    plan = base_plan()
    plan["surfaces"]["users"]["reconcile_existing_emails"] = True  # type: ignore[index]
    snapshot = {
        "surfaces": {
            "users": {
                "items": [
                    {"username": "alice", "email": "alice@example.test"},
                    {"username": "bob", "email": "bob@example.test"},
                ]
            }
        }
    }
    with (
        mock.patch.object(
            workspace,
            "forgejo_user",
            side_effect=[
                (200, {"login": "alice", "email": "alice@migration.invalid"}),
                (200, {"login": "bob", "email": "bob@other.test"}),
            ],
        ),
        mock.patch.object(workspace, "request") as api_request,
    ):
        try:
            workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "non-placeholder email" not in str(exc):
                raise AssertionError(f"unexpected bulk preflight error: {exc}") from exc
        else:
            raise AssertionError("bulk email reconciliation began before every user was checked")
    api_request.assert_not_called()


def test_excluded_system_user_is_not_created() -> None:
    plan = base_plan()
    plan["surfaces"]["users"]["excluded_usernames"] = ["ghost"]  # type: ignore[index]
    snapshot = {
        "surfaces": {
            "users": {
                "items": [
                    {"username": "ghost", "state": "blocked"},
                    {"username": "alice", "public_email": "alice@example.test"},
                ]
            }
        }
    }
    with (
        mock.patch.dict(workspace.os.environ, {"IMPORT_PASSWORD": "temporary-password"}),
        mock.patch.object(workspace, "forgejo_user", side_effect=[(404, {}), (200, {"login": "alice"})]) as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.import_users(plan, object(), snapshot)  # type: ignore[arg-type]
    if result.get("targets") != ["alice"]:
        raise AssertionError(f"excluded system user was retained as an import target: {result!r}")
    if user_probe.call_count != 2:
        raise AssertionError("excluded system user was probed in Forgejo")
    if any(call.args[1:3] == ("POST", "admin/users") for call in api_request.call_args_list) is not True:
        raise AssertionError("ordinary user was not imported after excluding system user")


def test_user_audit_is_read_only_and_never_verifies_passwords() -> None:
    plan = base_plan()
    plan["surfaces"]["users"]["preserve_account_flags"] = True  # type: ignore[index]
    snapshot = {
        "surfaces": {
            "users": {
                "items": [{"username": "alice", "state": "blocked", "is_admin": True}]
            }
        }
    }
    current = {"login": "alice", "is_admin": False, "prohibit_login": False}
    with (
        mock.patch.object(workspace, "forgejo_user", return_value=(200, current)) as user_probe,
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.audit_users(plan, object(), snapshot)  # type: ignore[arg-type]
    if result.get("matched") != 1 or result.get("missing") != 0:
        raise AssertionError(f"user audit did not read the expected account: {result!r}")
    if result.get("account_flag_mismatches") != 2 or result.get("passwords_verified") is not False:
        raise AssertionError(f"user audit reported unsafe verification state: {result!r}")
    if result.get("verified") is not False or user_probe.call_count != 1 or api_request.called:
        raise AssertionError("user audit was not read-only or incorrectly reported success")


def test_user_audit_reports_email_mismatch_without_mutation() -> None:
    plan = base_plan()
    snapshot = {
        "surfaces": {
            "users": {
                "items": [{"username": "alice", "email": "alice@example.test"}]
            }
        }
    }
    current = {"login": "alice", "email": "alice@migration.invalid"}
    with (
        mock.patch.object(workspace, "forgejo_user", return_value=(200, current)),
        mock.patch.object(workspace, "request") as api_request,
    ):
        result = workspace.audit_users(plan, object(), snapshot)  # type: ignore[arg-type]
    if result.get("email_mismatches") != 1 or result.get("emails_verified") is not False:
        raise AssertionError(f"placeholder email was not reported: {result!r}")
    if api_request.called:
        raise AssertionError("user email audit attempted a mutation")


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


def test_gitlab_owner_maps_to_builtin_owners_team() -> None:
    plan = base_plan()
    plan["surfaces"]["memberships"] = {"mode": "managed"}  # type: ignore[index]
    resolved = workspace.resolve_member_role(
        plan,
        {"username": "alice", "access_level": 50},
        "memberships",
    )
    if resolved["permission"] != "owner" or resolved["team"] != "Owners":
        raise AssertionError(f"GitLab Owner was not mapped to Forgejo ownership: {resolved!r}")
    workspace.validate_plan(plan)

    invalid = copy.deepcopy(plan)
    invalid["surfaces"]["memberships"]["role_mappings"] = {  # type: ignore[index]
        "50": {"permission": "admin", "team": "gitlab-owners"}
    }
    expect_error(invalid, "built-in Forgejo Owners")

    invalid_custom = copy.deepcopy(plan)
    invalid_custom["surfaces"]["memberships"]["role_mappings"] = {  # type: ignore[index]
        "custom:9001": {"permission": "owner", "team": "gitlab-custom-owner"}
    }
    expect_error(invalid_custom, "map owner access to the built-in Forgejo Owners team")

    invalid_owners_team = copy.deepcopy(plan)
    invalid_owners_team["surfaces"]["memberships"]["role_mappings"] = {  # type: ignore[index]
        "40": {"permission": "write", "team": "Owners"}
    }
    expect_error(invalid_owners_team, "cannot assign non-owner access")

    with mock.patch.object(workspace, "list_pages", return_value=[]):
        try:
            workspace.ensure_team(object(), "platform", "Owners", "owner")  # type: ignore[arg-type]
        except workspace.WorkspaceError as exc:
            if "no built-in Owners team" not in str(exc):
                raise AssertionError(f"unexpected missing Owners-team diagnostic: {exc}") from exc
        else:
            raise AssertionError("missing built-in Owners team was silently replaced")


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
    capped = workspace.resolve_member_role(
        plan,
        {
            "username": "alice",
            "access_level": 40,
            "member_role_id": 9001,
            "invited_group_access_level": 20,
        },
        "memberships",
    )
    if capped["permission"] != "read" or capped["team"] != "gitlab-reporters" or not capped.get("invitation_capped"):
        raise AssertionError(f"invited-group access cap was bypassed by custom role mapping: {capped!r}")
    try:
        workspace.resolve_member_role(plan, {"username": "bob", "access_level": 30, "member_role_id": 9002}, "memberships")
    except workspace.WorkspaceError as exc:
        if "not mapped" not in str(exc):
            raise AssertionError(f"unexpected custom-role diagnostic: {exc}") from exc
    else:
        raise AssertionError("unmapped custom GitLab role was collapsed into a base role")


def test_legacy_subgroup_membership_policy_is_scoped_to_the_subgroup() -> None:
    plan = base_plan()
    plan["surfaces"]["subgroups"] = {  # type: ignore[index]
        "mode": "managed",
        "members_mode": "import",
        "unmapped_role": "skip",
        "role_mappings": {
            "30": {"permission": "read", "team": "subgroup-reviewers"}
        },
    }
    workspace.validate_plan(plan)

    subgroup_role = workspace.resolve_member_role(
        plan,
        {"username": "alice", "access_level": 30},
        "memberships",
        "platform/child",
    )
    if subgroup_role["permission"] != "read" or subgroup_role["team"] != "subgroup-reviewers":
        raise AssertionError(f"subgroup role mapping was ignored: {subgroup_role!r}")

    subgroup_custom_role = workspace.resolve_member_role(
        plan,
        {"username": "bob", "access_level": 0, "member_role_id": 9002},
        "memberships",
        "platform/child",
    )
    if not subgroup_custom_role.get("unmapped") or subgroup_custom_role["unmapped_behavior"] != "skip":
        raise AssertionError(f"subgroup unmapped-role policy was ignored: {subgroup_custom_role!r}")

    try:
        workspace.resolve_member_role(
            plan,
            {"username": "carol", "access_level": 0, "member_role_id": 9002},
            "memberships",
            "platform",
        )
    except workspace.WorkspaceError as exc:
        if "not mapped" not in str(exc):
            raise AssertionError(f"unexpected root-group unmapped-role diagnostic: {exc}") from exc
    else:
        raise AssertionError("root-group membership unexpectedly used subgroup unmapped-role policy")


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
        "Owners",
        "gitlab-maintainers",
        "gitlab-developers",
        "gitlab-reporters",
        "gitlab-guests",
    }:
        raise AssertionError("empty managed role teams were omitted from downgrade reconciliation")
    if user_probe.called:
        raise AssertionError("known imported user was probed unnecessarily")


def test_membership_selection_does_not_fallback_from_empty_direct_view() -> None:
    item = {
        "direct_members": [],
        "effective_members": [{"username": "inherited", "access_level": 40}],
        "members": [{"username": "inherited", "access_level": 40}],
    }
    direct = workspace.group_members_for_import(item, {"include_inherited": False})
    if direct:
        raise AssertionError(f"empty direct membership view unexpectedly used effective members: {direct!r}")

    permission_direct = workspace.permission_member_records(
        item,
        {"include_direct": True, "include_inherited": False},
    )
    if permission_direct:
        raise AssertionError(
            "permission selection unexpectedly fell back to effective members when direct access was empty"
        )


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


def test_workspace_repository_scratch_is_per_repo_and_preserves_existing_files() -> None:
    plan = base_plan()
    items = [
        {
            "project": {
                "path_with_namespace": f"platform/{name}",
                "http_url_to_repo": f"https://gitlab.example.test/platform/{name}.git",
                "visibility": "private",
            },
            "destination": {
                "owner": "platform",
                "repo": name,
                "git_url": f"ssh://git@forgejo.example.test/platform/{name}.git",
            },
        }
        for name in ("one", "two")
    ]
    snapshot = {
        "surfaces": {"repositories": {"items": items}},
        "indexes": {"projects": items},
    }
    seen: list[Path] = []

    with tempfile.TemporaryDirectory() as temp_dir:
        work_dir = Path(temp_dir) / "work"
        work_dir.mkdir()
        old_mirror = work_dir / "existing-work"
        old_mirror.mkdir()
        (old_mirror / "keep.txt").write_text("keep", encoding="utf-8")

        def migrate(_repo: object, scratch: Path) -> dict[str, bool]:
            if scratch.parent != work_dir or not scratch.name.startswith("forge-repo-"):
                raise AssertionError("repository scratch escaped the selected work directory")
            if any(path.exists() for path in seen):
                raise AssertionError("the previous repository scratch was retained")
            seen.append(scratch)
            (scratch / "repository.git").mkdir()
            return {"verified": True}

        with (
            mock.patch.object(workspace, "ensure_repository", return_value={"verified": True}),
            mock.patch.object(workspace.migration, "migrate_repo", side_effect=migrate),
        ):
            result = workspace.import_repositories(plan, snapshot, object(), work_dir)  # type: ignore[arg-type]
        if result.get("verified") is not True or len(seen) != 2:
            raise AssertionError("workspace repository migration did not verify both repositories")
        if any(path.exists() for path in seen) or not (old_mirror / "keep.txt").exists():
            raise AssertionError("scratch cleanup damaged pre-existing work or left completed mirrors")

        failed_scratch: list[Path] = []

        def fail(_repo: object, scratch: Path) -> dict[str, bool]:
            failed_scratch.append(scratch)
            (scratch / "partial.git").mkdir()
            raise RuntimeError("simulated repository failure")

        with (
            mock.patch.object(workspace, "ensure_repository", return_value={"verified": True}),
            mock.patch.object(workspace.migration, "migrate_repo", side_effect=fail),
        ):
            try:
                workspace.import_repositories(plan, snapshot, object(), work_dir)  # type: ignore[arg-type]
            except RuntimeError as exc:
                if "simulated repository failure" not in str(exc):
                    raise
            else:
                raise AssertionError("failed repository migration unexpectedly succeeded")
        if len(failed_scratch) != 1 or failed_scratch[0].exists() or not (old_mirror / "keep.txt").exists():
            raise AssertionError("failed repository scratch was retained or pre-existing work was removed")


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
    if migrate.call_args.kwargs.get("reviewed_source_protections") != [{"name": "main"}]:
        raise AssertionError("protected-branch import did not pass the reviewed snapshot records")


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
    test_export_requires_gitlab_token_before_discovery()
    test_import_and_audit_require_forgejo_token()
    test_import_email_reconciliation_flag_preserves_saved_plan()
    test_import_mail_confirmation_is_runtime_only()
    test_membership_only_users_are_hydrated_before_email_export()
    test_redaction_and_destination_url()
    test_long_group_targets_are_forgejo_compatible_and_stable()
    test_selected_nested_group_is_a_root()
    test_project_rules_discovery_is_redacted_and_scoped()
    test_project_permission_discovery_materializes_invited_group_members()
    test_managed_import_rejects_missing_snapshot_surface_before_mutation()
    test_all_available_group_discovery_includes_top_level_groups()
    test_all_available_project_discovery_keeps_archived_and_inherited_projects()
    test_ci_checkout_is_retryable()
    test_managed_user_requires_readback()
    test_existing_hash_strategy_fails_closed_before_mutation()
    test_generated_passwords_are_per_user_and_not_in_proof()
    test_generated_passwords_can_use_private_handoff_without_notification()
    test_generated_password_mail_requires_confirmation_before_destination_access()
    test_generated_password_preflight_fails_before_destination_access()
    test_generated_password_preflight_rejects_placeholder_address()
    test_managed_user_reconciles_account_flags_when_enabled()
    test_existing_email_reconciliation_requires_opt_in_and_readback()
    test_existing_email_reconciliation_refuses_real_address_before_mutation()
    test_existing_email_reconciliation_preflights_all_users()
    test_excluded_system_user_is_not_created()
    test_user_audit_is_read_only_and_never_verifies_passwords()
    test_user_audit_reports_email_mismatch_without_mutation()
    test_user_mapping_collision_fails_before_mutation()
    test_variable_environment_collision_fails_before_mutation()
    test_mapped_variable_is_non_mutating()
    test_team_membership_is_reconciled_and_verified()
    test_team_permission_fails_closed()
    test_gitlab_owner_maps_to_builtin_owners_team()
    test_recursive_group_discovery_keeps_direct_and_effective_members()
    test_role_mapping_supports_custom_roles_and_fails_closed()
    test_legacy_subgroup_membership_policy_is_scoped_to_the_subgroup()
    test_permission_surface_validation_requires_safe_exact_confirmation()
    test_membership_import_uses_direct_members_by_default()
    test_permission_import_merges_effective_access_and_verifies_repo_teams()
    test_exact_permission_reconciliation_does_not_remove_unmanaged_collaborators()
    test_permission_readback_fails_closed()
    test_workspace_repository_scratch_is_per_repo_and_preserves_existing_files()
    test_rule_import_runs_after_repository_exists_and_passes_policy()
    test_ci_destination_and_remote_proof()
    test_ci_commit_is_idempotent()
    test_pipeline_schedule_import_is_not_history_import()
    print("Forge workspace migration self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
