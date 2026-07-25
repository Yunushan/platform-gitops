#!/usr/bin/env python3
"""Self-test the opt-in GitLab-to-Forgejo cutover orchestrator."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_cutover as cutover


def fail(message: str) -> None:
    raise AssertionError(message)


def base_plan() -> dict[str, object]:
    return {
        "version": 1,
        "direction": "gitlab-to-forgejo",
        "repositories": [
            {
                "name": "platform-app",
                "source": {
                    "url": "https://gitlab.example.invalid/source/platform-app.git",
                    "api_url": "https://gitlab.example.invalid/api/v4",
                    "api_repository": "source/platform-app",
                    "token_env": "GITLAB_SOURCE_TOKEN",
                },
                "destination": {
                    "url": "https://forgejo.example.invalid/platform/platform-app.git",
                    "api_url": "https://forgejo.example.invalid/api/v1",
                    "api_repository": "platform/platform-app",
                    "token_env": "FORGEJO_DESTINATION_TOKEN",
                    "create": "required",
                    "private": True,
                },
                "wiki": False,
                "lfs": False,
                "metadata": {
                    "labels": "skip",
                    "milestones": "skip",
                    "releases": "skip",
                    "issues": "skip",
                    "merge_requests": "skip",
                },
                "cutover": {
                    "pipelines": {
                        "unmapped": "fail",
                        "config_file": ".woodpecker.yml",
                        "deployment_gate_marker": "FORGE_CUTOVER_DEPLOYMENT_ENABLED",
                        "mappings": [
                            {
                                "source": ".gitlab-ci.yml",
                                "destinations": [".woodpecker.yml"],
                                "mode": "managed",
                            }
                        ],
                        "external_includes": [],
                    },
                    "variables": {
                        "unmapped": "fail",
                        "group_ids": [],
                        "group_hierarchy": {
                            "mode": "managed",
                        },
                        "instance_scope": {
                            "mode": "manual",
                            "accepted": True,
                            "reason": "Instance variables require a separately approved GitLab administrator token.",
                        },
                        "mappings": [
                            {
                                "source": "project:DEPLOY_KEY:*",
                                "target": "woodpecker_secret",
                                "target_name": "DEPLOY_KEY",
                                "mode": "managed",
                            }
                        ],
                    },
                    "schedules": {
                        "unmapped": "fail",
                        "mappings": [
                            {
                                "source": "nightly",
                                "target_name": "nightly",
                                "schedule": "0 2 * * *",
                                "branch": "main",
                                "mode": "managed",
                            }
                        ],
                    },
                    "runner_tags": {
                        "unmapped": "fail",
                        "mappings": [
                            {
                                "source": "linux",
                                "target_labels": {"platform": "linux"},
                                "mode": "mapped",
                            }
                        ],
                    },
                    "protections": {
                        "unmapped": "fail",
                        "mappings": [
                            {
                                "source": "main",
                                "target": "main",
                                "settings": {
                                    "enable_push": False,
                                    "required_approvals": 1,
                                },
                                "mode": "managed",
                            }
                        ],
                    },
                    "integrations": {
                        "unmapped": "fail",
                        "mappings": [
                            {
                                "source": "hook:Woodpecker",
                                "target": "woodpecker_webhook",
                                "mode": "managed",
                            }
                        ],
                    },
                },
            }
        ],
        "services": {
            "woodpecker": {
                "mode": "managed",
                "api_url": "https://ci.example.invalid",
                "token_env": "WOODPECKER_API_TOKEN",
                "shadow_gate_secret": "FORGE_CUTOVER_DEPLOYMENT_ENABLED",
                "canary_timeout_seconds": 60,
            },
            "harbor": {
                "mode": "managed",
                "api_url": "https://registry.example.invalid",
                "username_env": "HARBOR_USERNAME",
                "password_env": "HARBOR_PASSWORD",
                "project": "platform",
                "registry_host": "registry.example.invalid",
                "create_project": True,
                "canary": {
                    "repository": "platform-app-cutover-canary",
                    "reference": "shadow",
                },
            },
            "argocd": {
                "mode": "managed",
                "api_url": "https://argocd.example.invalid",
                "token_env": "ARGOCD_API_TOKEN",
                "applications": [
                    {
                        "name": "platform-app",
                        "expected_repo_url": "https://forgejo.example.invalid/platform/gitops.git",
                    }
                ],
            },
        },
        "activation": {
            "freeze": "archive",
            "prepare_confirmation_env": "FORGE_CUTOVER_PREPARE_CONFIRM",
            "confirmation_env": "FORGE_CUTOVER_CONFIRM",
            "rollback_confirmation_env": "FORGE_CUTOVER_ROLLBACK_CONFIRM",
            "live_env": "FORGE_CUTOVER_LIVE",
            "change_ticket_env": "FORGE_CUTOVER_CHANGE_TICKET",
            "max_verification_age_seconds": 3600,
            "cancel_active_pipelines": False,
        },
    }


def parsed_plan() -> cutover.CutoverPlan:
    return cutover.parse_cutover_plan(copy.deepcopy(base_plan()))


def expect_cutover_error(plan: dict[str, object], expected: str) -> None:
    try:
        cutover.parse_cutover_plan(plan)
    except cutover.CutoverError as exc:
        if expected not in str(exc):
            fail(f"expected {expected!r} in {exc!r}")
    else:
        fail(f"expected plan validation failure containing {expected!r}")


def test_plan_validation() -> None:
    plan = parsed_plan()
    if plan.sha256 != cutover.canonical_digest(plan.raw):
        fail("plan digest is not canonical")

    unsafe = copy.deepcopy(base_plan())
    unsafe["services"]["woodpecker"]["token"] = "plaintext"  # type: ignore[index]
    expect_cutover_error(unsafe, "must not contain credential")

    unsupported = copy.deepcopy(base_plan())
    unsupported["repositories"][0]["cutover"]["schedules"]["mappings"][0]["mode"] = "unsupported"  # type: ignore[index]
    expect_cutover_error(unsupported, "marked unsupported")

    weak_runner = copy.deepcopy(base_plan())
    weak_runner["repositories"][0]["cutover"]["runner_tags"]["mappings"][0]["target_labels"] = {}  # type: ignore[index]
    expect_cutover_error(weak_runner, "target_labels")

    accepted = copy.deepcopy(base_plan())
    accepted_mapping = accepted["repositories"][0]["cutover"]["runner_tags"]["mappings"][0]  # type: ignore[index]
    accepted_mapping.clear()
    accepted_mapping.update(
        {
            "source": "linux",
            "mode": "manual",
            "accepted": True,
            "reason": "Dedicated agent validation is recorded in the change ticket.",
        }
    )
    cutover.parse_cutover_plan(accepted)


def test_pipeline_accounting_and_gate() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    source = [
        {
            "path": ".gitlab-ci.yml",
            "sha": "source-sha",
            "external_includes": [],
        }
    ]
    destination = [{"path": ".woodpecker.yml", "sha": "destination-sha"}]
    with mock.patch(
        "forge_cutover.forgejo_file_text",
        return_value="when: FORGE_CUTOVER_DEPLOYMENT_ENABLED\n",
    ):
        result = cutover.account_pipeline_files(repo, source, destination, "main")
    if result["verified"] is not True:
        fail(f"gated pipeline mapping should verify: {result}")

    with mock.patch("forge_cutover.forgejo_file_text", return_value="steps: [test]\n"):
        missing_gate = cutover.account_pipeline_files(repo, source, destination, "main")
    if missing_gate["verified"] is not False:
        fail("pipeline without the deployment gate marker must fail")

    manual_plan = copy.deepcopy(base_plan())
    mapping = manual_plan["repositories"][0]["cutover"]["pipelines"]["mappings"][0]  # type: ignore[index]
    mapping.clear()
    mapping.update(
        {
            "source": ".gitlab-ci.yml",
            "mode": "manual",
            "accepted": True,
            "reason": "Pipeline retirement is approved separately.",
        }
    )
    manual_repo = cutover.parse_cutover_plan(manual_plan).repositories[0]
    result = cutover.account_pipeline_files(manual_repo, source, destination, "main")
    if not result["verified"] or result["unaccounted_source_files"]:
        fail("accepted manual pipeline mapping must account for its matched source")


def test_external_include_detection() -> None:
    content = """
include:
  - remote: 'https://ci.example.invalid/common.yml'
  - project: platform/shared-ci
  - template: Security/SAST.gitlab-ci.yml
  - local: '.ci/local.yml'
"""
    detected = cutover.gitlab_external_includes(content)
    expected = {
        "remote:https://ci.example.invalid/common.yml",
        "project:platform/shared-ci",
        "template:Security/SAST.gitlab-ci.yml",
    }
    if set(detected) != expected:
        fail(f"external include detection mismatch: {detected}")
    if cutover.gitlab_local_includes(content) != [".ci/local.yml"]:
        fail("local GitLab include detection mismatch")

    plan = parsed_plan()
    repo = plan.repositories[0]
    tree = [
        {"type": "blob", "path": ".gitlab-ci.yml", "id": "root-sha"},
        {"type": "blob", "path": ".ci/local.yml", "id": "local-sha"},
    ]

    def file_content(_repo, path, _branch):
        return "include:\n  - local: '.ci/local.yml'\n" if path == ".gitlab-ci.yml" else "stages: [test]\n"

    with (
        mock.patch("forge_cutover.gitlab_list", return_value=tree),
        mock.patch("forge_cutover.gitlab_file_text", side_effect=file_content),
    ):
        files = cutover.inventory_gitlab_pipeline_files(repo, {"default_branch": "main"})
    if [item["path"] for item in files] != [".ci/local.yml", ".gitlab-ci.yml"]:
        fail(f"recursive local pipeline inventory is incomplete: {files}")
    with mock.patch(
        "forge_cutover.forgejo_file_text",
        return_value="FORGE_CUTOVER_DEPLOYMENT_ENABLED\n",
    ):
        accounting = cutover.account_pipeline_files(
            repo,
            files,
            [{"path": ".woodpecker.yml", "sha": "destination-sha"}],
            "main",
        )
    if accounting["verified"] is not False or ".ci/local.yml" not in accounting["unaccounted_source_files"]:
        fail("unmapped recursively included pipeline file did not block cutover")


def test_discovery_accounts_every_surface() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    state = {"destination_missing": False}

    def fake_api(_target, method, path, **kwargs):
        if method != "GET":
            fail(f"discovery unexpectedly mutated remote state with {method} {path}")
        if path.endswith("source%2Fplatform-app"):
            return {
                "id": 10,
                "path_with_namespace": "source/platform-app",
                "default_branch": "main",
                "archived": False,
                "builds_access_level": "enabled",
                "namespace": {"kind": "group", "full_path": "source"},
            }
        if path.endswith("repos/platform/platform-app"):
            status = 404 if state["destination_missing"] else 200
            payload = (
                {}
                if state["destination_missing"]
                else {"id": 20, "full_name": "platform/platform-app", "default_branch": "main"}
            )
            return (status, payload) if kwargs.get("return_status") else payload
        fail(f"unexpected API request: {path}")

    def fake_gitlab_list(_target, path, query=None):
        del query
        if path.endswith("/repository/tree"):
            return [{"type": "blob", "path": ".gitlab-ci.yml", "id": "pipeline-sha"}]
        if path.startswith("groups/") and path.endswith("/variables"):
            return []
        if path == "admin/ci/variables":
            return []
        if path.endswith("/variables"):
            return [
                {
                    "key": "DEPLOY_KEY",
                    "value": "must-not-appear-in-proof",
                    "environment_scope": "*",
                    "variable_type": "env_var",
                }
            ]
        if path.endswith("/pipeline_schedules"):
            return [{"id": 31, "description": "nightly", "active": True}]
        if path.endswith("/runners"):
            return [{"id": 41, "tag_list": ["linux"]}]
        if path.endswith("/protected_branches"):
            return [{"name": "main"}]
        if path.endswith("/hooks"):
            return [{"id": 51, "name": "Woodpecker", "url": "https://ci.example.invalid/hook"}]
        if path.endswith("/remote_mirrors"):
            return []
        fail(f"unexpected GitLab list endpoint: {path}")

    with (
        mock.patch("forge_cutover.migration.api_request", side_effect=fake_api),
        mock.patch("forge_cutover.gitlab_list", side_effect=fake_gitlab_list),
        mock.patch("forge_cutover.gitlab_file_text", return_value="stages: [test]\n"),
        mock.patch(
            "forge_cutover.inventory_forgejo_pipeline_files",
            return_value=[{"path": ".woodpecker.yml", "sha": "destination-sha"}],
        ),
        mock.patch(
            "forge_cutover.forgejo_file_text",
            return_value="FORGE_CUTOVER_DEPLOYMENT_ENABLED\n",
        ),
    ):
        result = cutover.discover_repository(repo)
    if result["verified"] is not True:
        fail(f"complete discovery should verify: {result}")
    serialized = json.dumps(cutover.sanitize_for_proof(result))
    if "must-not-appear-in-proof" in serialized:
        fail("GitLab variable value leaked into discovery proof")

    state["destination_missing"] = True
    with (
        mock.patch("forge_cutover.migration.api_request", side_effect=fake_api),
        mock.patch("forge_cutover.gitlab_list", side_effect=fake_gitlab_list),
        mock.patch("forge_cutover.gitlab_file_text", return_value="stages: [test]\n"),
    ):
        missing_destination = cutover.discover_repository(repo)
    if missing_destination["verified"] is not True:
        fail("read-only discovery must allow an explicitly managed missing destination")
    if missing_destination["destination"]["exists"] is not False:
        fail("missing Forgejo destination was not reported")
    if missing_destination["pipelines"]["destination_verification_deferred"] is not True:
        fail("missing destination workflow verification was not deferred")


def test_gitlab_variable_scope_inventory() -> None:
    plan_data = base_plan()
    variables = plan_data["repositories"][0]["cutover"]["variables"]
    variables["group_ids"] = ["explicit/team"]
    variables["instance_scope"] = {"mode": "managed"}
    repo = cutover.parse_cutover_plan(plan_data).repositories[0]
    requested_paths: list[str] = []

    def fake_gitlab_list(_target, path, query=None):
        del query
        requested_paths.append(path)
        return []

    with mock.patch("forge_cutover.gitlab_list", side_effect=fake_gitlab_list):
        cutover.list_gitlab_variables(
            repo,
            include_values=False,
            project={
                "namespace": {
                    "kind": "group",
                    "full_path": "parent/subgroup",
                }
            },
        )

    for expected in (
        "groups/explicit%2Fteam/variables",
        "groups/parent/variables",
        "groups/parent%2Fsubgroup/variables",
        "admin/ci/variables",
    ):
        if expected not in requested_paths:
            fail(f"GitLab variable scope was not inventoried: {expected}; got {requested_paths}")


def test_proof_integrity_and_redaction() -> None:
    plan = parsed_plan()
    cutover._KNOWN_SECRET_VALUES.clear()
    cutover.register_secret("super-sensitive-value")
    proof = cutover.proof_base(
        plan,
        "verify",
        [
            {
                "name": "platform-app",
                "url": "https://user:password@example.invalid/repo.git",
                "value": "super-sensitive-value",
                "verified": True,
            }
        ],
    )
    with tempfile.TemporaryDirectory(prefix="forge-cutover-proof-") as temp:
        path = Path(temp) / "proof.json"
        written = cutover.write_proof(path, proof)
        text = path.read_text(encoding="utf-8")
        if "super-sensitive-value" in text or "user:password" in text:
            fail("proof contains a credential value")
        loaded = cutover.load_verified_proof(path, plan, "verify")
        if loaded["proof_sha256"] != written["proof_sha256"]:
            fail("proof digest changed during load")
        tampered = json.loads(text)
        tampered["direction"] = "tampered"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        try:
            cutover.load_verified_proof(path, plan, "verify")
        except cutover.CutoverError:
            pass
        else:
            fail("tampered proof was accepted")


def test_shadow_prepare_keeps_destination_dormant() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    secret_calls: list[tuple[str, str]] = []
    cron_states: list[bool] = []

    def fake_secret(_target, _repo_id, name, secret_value, **_kwargs):
        secret_calls.append((name, secret_value))
        return {"name": name, "action": "created", "verified": True}

    def fake_cron(_target, _repo_id, name, _schedule, _branch, enabled):
        cron_states.append(enabled)
        return {"id": 1, "name": name, "enabled": enabled, "verified": True}

    with (
        mock.patch(
            "forge_cutover.migration.migrate_repo",
            return_value={
                "destination_repository": {"verified": True},
                "verified": True,
            },
        ),
        mock.patch(
            "forge_cutover.migration.api_request",
            return_value={"id": 20, "full_name": "platform/platform-app", "default_branch": "main"},
        ),
        mock.patch(
            "forge_cutover.woodpecker_lookup",
            return_value={"id": 30, "full_name": "platform/platform-app"},
        ),
        mock.patch("forge_cutover.service_request", return_value={"id": 30}),
        mock.patch(
            "forge_cutover.list_gitlab_variables",
            return_value=[
                {
                    "source_scope": "project",
                    "key": "DEPLOY_KEY",
                    "environment_scope": "*",
                    "value": "runtime-only-secret",
                }
            ],
        ),
        mock.patch("forge_cutover.woodpecker_secret_upsert", side_effect=fake_secret),
        mock.patch("forge_cutover.woodpecker_cron_upsert", side_effect=fake_cron),
        mock.patch(
            "forge_cutover.prepare_destination_protections",
            return_value={"verified": True},
        ),
        mock.patch("forge_cutover.prepare_harbor", return_value={"verified": True}),
        mock.patch("forge_cutover.verify_argocd", return_value={"verified": True}),
    ):
        result = cutover.prepare_repository(plan, repo)
    if result["verified"] is not True:
        fail(f"shadow preparation should verify: {result}")
    if secret_calls != [
        ("FORGE_CUTOVER_DEPLOYMENT_ENABLED", "false"),
        ("DEPLOY_KEY", "runtime-only-secret"),
    ]:
        fail(f"unexpected shadow secret writes: {secret_calls}")
    if cron_states != [False]:
        fail(f"shadow preparation enabled a schedule: {cron_states}")
    if "runtime-only-secret" in json.dumps(cutover.proof_base(plan, "prepare", [result])):
        fail("shadow preparation proof leaked a GitLab variable value")


def test_activation_confirmation_is_fail_closed() -> None:
    plan = parsed_plan()
    proof = {
        "proof_sha256": "verification-digest",
        "generated_at": cutover.utc_now(),
    }
    env_names = [
        "FORGE_CUTOVER_LIVE",
        "FORGE_CUTOVER_CONFIRM",
        "FORGE_CUTOVER_CHANGE_TICKET",
    ]
    saved = {name: os.environ.get(name) for name in env_names}
    try:
        for name in env_names:
            os.environ.pop(name, None)
        try:
            cutover.require_activation_confirmation(plan, proof)
        except cutover.CutoverError as exc:
            if "live cutover is disabled" not in str(exc):
                fail(f"unexpected live-gate failure: {exc}")
        else:
            fail("activation ran without the live gate")
        os.environ["FORGE_CUTOVER_LIVE"] = "1"
        os.environ["FORGE_CUTOVER_CONFIRM"] = "wrong"
        os.environ["FORGE_CUTOVER_CHANGE_TICKET"] = "CHG-1234"
        try:
            cutover.require_activation_confirmation(plan, proof)
        except cutover.CutoverError as exc:
            if "approval mismatch" not in str(exc):
                fail(f"unexpected digest-gate failure: {exc}")
        else:
            fail("activation ran with the wrong proof digest")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_prepare_rejects_inventory_changed_after_approval() -> None:
    plan = parsed_plan()
    approved_inventory = {
        "name": "platform-app",
        "source_state": {"archived": False, "builds_access_level": "enabled"},
        "verified": True,
    }
    discovery = cutover.proof_base(plan, "discover", [approved_inventory])
    discovery["proof_sha256"] = cutover.migration.proof_digest(discovery)
    previous = os.environ.get("FORGE_CUTOVER_PREPARE_CONFIRM")
    os.environ["FORGE_CUTOVER_PREPARE_CONFIRM"] = str(discovery["proof_sha256"])
    try:
        with tempfile.TemporaryDirectory(prefix="forge-cutover-prepare-approval-") as temp:
            root = Path(temp)
            args = argparse.Namespace(
                plan=root / "plan.json",
                discovery=root / "discovery.json",
                proof=root / "prepared.json",
            )
            changed_inventory = {
                **approved_inventory,
                "source_state": {"archived": False, "builds_access_level": "enabled"},
                "pipelines": {"changed": True},
            }
            with (
                mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
                mock.patch("forge_cutover.require_provider_credentials"),
                mock.patch("forge_cutover.load_verified_proof", return_value=discovery),
                mock.patch("forge_cutover.discover_repository", return_value=changed_inventory),
                mock.patch("forge_cutover.prepare_repository") as prepare_mock,
            ):
                result = cutover.command_prepare(args)
            if result != 1:
                fail("changed post-approval inventory did not block shadow preparation")
            if prepare_mock.called:
                fail("shadow mutation started after the approved inventory changed")
    finally:
        if previous is None:
            os.environ.pop("FORGE_CUTOVER_PREPARE_CONFIRM", None)
        else:
            os.environ["FORGE_CUTOVER_PREPARE_CONFIRM"] = previous


def verification_proof(plan: cutover.CutoverPlan) -> dict[str, object]:
    inventory = {
        "verified": True,
        "source_state": {"archived": False, "builds_access_level": "enabled"},
    }
    proof = cutover.proof_base(
        plan,
        "verify",
        [{"name": "platform-app", "inventory": inventory, "verified": True}],
    )
    proof["proof_sha256"] = cutover.migration.proof_digest(proof)
    return proof


def test_activation_sequence_and_automatic_rollback() -> None:
    plan = parsed_plan()
    verification = verification_proof(plan)
    repo_name = plan.repositories[0].migration.name
    events: list[str] = []
    checkpoints: list[str] = []

    def fake_freeze(_repo, _cancel, callback):
        snapshot = {"archived": False, "builds_access_level": "enabled", "schedules": []}
        callback(snapshot)
        events.append("source-frozen")
        return snapshot

    def fake_authority(_plan, _repo, enabled):
        events.append(f"authority:{enabled}")
        return {"verified": True, "deployment_enabled": enabled}

    def fake_checkpoint(_path, _plan, _verification, _snapshots, _attempted, state, **_kwargs):
        checkpoints.append(state)
        return {"proof_sha256": f"checkpoint-{len(checkpoints)}"}

    with tempfile.TemporaryDirectory(prefix="forge-cutover-activate-") as temp:
        temp_path = Path(temp)
        args = argparse.Namespace(
            plan=temp_path / "plan.json",
            verification=temp_path / "verify.json",
            proof=temp_path / "activate.json",
            checkpoint=temp_path / "checkpoint.json",
            work_dir=temp_path / "work",
        )
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.load_verified_proof", return_value=verification),
            mock.patch("forge_cutover.require_activation_confirmation"),
            mock.patch(
                "forge_cutover.discover_repository",
                return_value={
                    "verified": True,
                    "source_state": {"archived": False, "builds_access_level": "enabled"},
                },
            ),
            mock.patch("forge_cutover.freeze_gitlab", side_effect=fake_freeze),
            mock.patch(
                "forge_cutover.migration.migrate_repo",
                side_effect=lambda *_args: events.append("final-sync") or {"verified": True},
            ),
            mock.patch("forge_cutover.set_destination_authority", side_effect=fake_authority),
            mock.patch(
                "forge_cutover.verify_repository",
                side_effect=lambda *_args: events.append("post-canary") or {"verified": True},
            ),
            mock.patch("forge_cutover.write_activation_checkpoint", side_effect=fake_checkpoint),
        ):
            result = cutover.command_activate(args)
        if result != 0:
            fail("successful activation returned nonzero")
        if events != ["source-frozen", "final-sync", "authority:True", "post-canary"]:
            fail(f"activation ordering changed: {events}")
        if checkpoints[-1] != "completed":
            fail(f"activation did not close its checkpoint: {checkpoints}")
        activation = json.loads(args.proof.read_text(encoding="utf-8"))
        activated = cutover.find_proof_repo(activation, repo_name)
        if activated.get("source_before", {}).get("archived") is not False:
            fail("activation proof omitted the source rollback snapshot")

    events.clear()
    checkpoints.clear()

    def failing_verify(*_args):
        events.append("post-canary-failed")
        raise cutover.CutoverError("canary failed")

    with tempfile.TemporaryDirectory(prefix="forge-cutover-rollback-") as temp:
        temp_path = Path(temp)
        args = argparse.Namespace(
            plan=temp_path / "plan.json",
            verification=temp_path / "verify.json",
            proof=temp_path / "activate.json",
            checkpoint=temp_path / "checkpoint.json",
            work_dir=temp_path / "work",
        )
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.load_verified_proof", return_value=verification),
            mock.patch("forge_cutover.require_activation_confirmation"),
            mock.patch(
                "forge_cutover.discover_repository",
                return_value={
                    "verified": True,
                    "source_state": {"archived": False, "builds_access_level": "enabled"},
                },
            ),
            mock.patch("forge_cutover.freeze_gitlab", side_effect=fake_freeze),
            mock.patch("forge_cutover.migration.migrate_repo", return_value={"verified": True}),
            mock.patch("forge_cutover.set_destination_authority", side_effect=fake_authority),
            mock.patch("forge_cutover.verify_repository", side_effect=failing_verify),
            mock.patch(
                "forge_cutover.restore_gitlab",
                side_effect=lambda *_args: events.append("source-restored") or {"verified": True},
            ),
            mock.patch("forge_cutover.write_activation_checkpoint", side_effect=fake_checkpoint),
        ):
            result = cutover.command_activate(args)
        if result != 1:
            fail("failed post-cutover canary did not fail activation")
        if events[-2:] != ["authority:False", "source-restored"]:
            fail(f"automatic rollback ordering changed: {events}")
        if checkpoints[-1] != "automatic-rollback-complete":
            fail(f"automatic rollback checkpoint is incomplete: {checkpoints}")


def test_manual_rollback_from_checkpoint() -> None:
    plan = parsed_plan()
    repo_name = plan.repositories[0].migration.name
    snapshot = {
        "archived": False,
        "builds_access_level": "enabled",
        "schedules": [{"id": 31, "active": True}],
    }
    verification = verification_proof(plan)
    calls: list[str] = []
    with tempfile.TemporaryDirectory(prefix="forge-cutover-manual-rollback-") as temp:
        root = Path(temp)
        checkpoint = root / "checkpoint.json"
        cutover.write_activation_checkpoint(
            checkpoint,
            plan,
            verification,
            {repo_name: snapshot},
            {repo_name},
            "destination-authority-attempted:platform-app",
        )
        args = argparse.Namespace(
            plan=root / "plan.json",
            activation=checkpoint,
            proof=root / "rollback.json",
        )
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.require_rollback_confirmation"),
            mock.patch(
                "forge_cutover.set_destination_authority",
                side_effect=lambda *_args: calls.append("destination-disabled")
                or {"verified": True},
            ),
            mock.patch(
                "forge_cutover.restore_gitlab",
                side_effect=lambda *_args: calls.append("source-restored")
                or {"verified": True},
            ),
        ):
            result = cutover.command_rollback(args)
        if result != 0:
            fail("checkpoint-driven rollback returned nonzero")
        if calls != ["destination-disabled", "source-restored"]:
            fail(f"checkpoint rollback order changed: {calls}")
        proof = json.loads(args.proof.read_text(encoding="utf-8"))
        if proof.get("verified") is not True:
            fail("checkpoint rollback proof is not verified")


def test_example_and_dormant_contract() -> None:
    example = ROOT / "examples" / "migrations" / "gitlab-to-forgejo.cutover.example.json"
    if not example.exists():
        fail(f"missing cutover example: {example}")
    cutover.load_cutover_plan(example)
    for path in (
        ROOT / "scripts" / "bootstrap" / "private-first-deploy.sh",
        ROOT / "scripts" / "bootstrap" / "seed-first-deploy.sh",
        ROOT / "scripts" / "bootstrap" / "sync-seed-git.sh",
    ):
        if "forge_cutover.py" in path.read_text(encoding="utf-8"):
            fail(f"cutover must remain dormant during bootstrap: {path.relative_to(ROOT)}")


def main() -> int:
    test_plan_validation()
    test_pipeline_accounting_and_gate()
    test_external_include_detection()
    test_discovery_accounts_every_surface()
    test_gitlab_variable_scope_inventory()
    test_proof_integrity_and_redaction()
    test_shadow_prepare_keeps_destination_dormant()
    test_activation_confirmation_is_fail_closed()
    test_prepare_rejects_inventory_changed_after_approval()
    test_activation_sequence_and_automatic_rollback()
    test_manual_rollback_from_checkpoint()
    test_example_and_dormant_contract()
    print("Forge cutover orchestrator self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
