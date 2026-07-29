#!/usr/bin/env python3
"""Self-test the optional GitLab/GitHub to Forgejo transition controller."""

from __future__ import annotations

import argparse
import base64
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

import forge_transition as transition


def fail(message: str) -> None:
    raise AssertionError(message)


def base_plan(provider: str = "gitlab") -> dict[str, object]:
    github = provider == "github"
    source_host = "github.com" if github else "gitlab.example.invalid"
    source_api = "https://api.github.com" if github else "https://gitlab.example.invalid/api/v4"
    source_repo = "source/platform-app"
    pipeline_source = ".github/workflows/ci.yml" if github else ".gitlab-ci.yml"
    variables: dict[str, object]
    if github:
        variables = {
            "unmapped": "fail",
            "organization_scope": {
                "mode": "skipped",
                "accepted": True,
                "reason": "No organization-scoped values are used by this repository.",
            },
            "environment_scope": {
                "mode": "skipped",
                "accepted": True,
                "reason": "No environment-scoped values are used by this repository.",
            },
            "mappings": [
                {
                    "source": "repository-secret:DEPLOY_KEY",
                    "target": "woodpecker_secret",
                    "target_name": "DEPLOY_KEY",
                    "value_env": "GITHUB_DEPLOY_KEY_VALUE",
                    "mode": "managed",
                }
            ],
        }
    else:
        variables = {
            "unmapped": "fail",
            "group_ids": [],
            "group_hierarchy": {"mode": "managed"},
            "instance_scope": {
                "mode": "skipped",
                "accepted": True,
                "reason": "No instance variables are used by this repository.",
            },
            "mappings": [
                {
                    "source": "project:DEPLOY_KEY:*",
                    "target": "woodpecker_secret",
                    "target_name": "DEPLOY_KEY",
                    "mode": "managed",
                }
            ],
        }
    return {
        "version": 2,
        "transition_version": 1,
        "direction": f"{provider}-to-forgejo",
        "repositories": [
            {
                "name": "platform-app",
                "source": {
                    "url": f"https://{source_host}/{source_repo}.git",
                    "api_url": source_api,
                    "api_repository": source_repo,
                    "token_env": f"{provider.upper()}_SOURCE_TOKEN",
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
                    "merge_requests" if not github else "pull_requests": "skip",
                },
                "cutover": {
                    "pipelines": {
                        "unmapped": "fail",
                        "config_file": ".woodpecker.yml",
                        "deployment_gate_marker": "FORGE_TRANSITION_DEPLOYMENT_ENABLED",
                        "mappings": [
                            {
                                "source": pipeline_source,
                                "destinations": [".woodpecker.yml"],
                                "mode": "managed",
                            }
                        ],
                        "external_includes": [],
                    },
                    "variables": variables,
                    "schedules": {"unmapped": "fail", "mappings": []},
                    "runner_tags": {"unmapped": "fail", "mappings": []},
                    "protections": {"unmapped": "fail", "mappings": []},
                    "integrations": {"unmapped": "fail", "mappings": []},
                },
                "transition": {
                    "relay": {
                        "mode": "managed",
                        "driver": "external",
                        "sync_interval_seconds": 60,
                        "max_lag_seconds": 300,
                        "sync_timeout_seconds": 900,
                    },
                    "source_ci": {
                        "mode": "managed",
                        "keep_repository_writable": True,
                        "cancel_active": False,
                        "shutdown_timeout_seconds": 60,
                    },
                    "destination_access": {
                        "mode": "managed",
                        "mirror_actor": "forge-transition-bot",
                        "protection_pattern": "**",
                        "shadow_settings": {
                            "enable_push": True,
                            "enable_push_whitelist": True,
                            "push_whitelist_usernames": ["forge-transition-bot"],
                        },
                        "final_settings": {
                            "enable_push": True,
                            "enable_push_whitelist": True,
                            "push_whitelist_usernames": ["platform-maintainers"],
                        },
                    },
                },
            }
        ],
        "services": {
            "woodpecker": {
                "mode": "managed",
                "api_url": "https://ci.example.invalid",
                "token_env": "WOODPECKER_API_TOKEN",
                "shadow_gate_secret": "FORGE_TRANSITION_DEPLOYMENT_ENABLED",
                "canary_timeout_seconds": 60,
            },
            "harbor": {
                "mode": "skipped",
                "accepted": True,
                "reason": "This fixture does not publish an image.",
            },
            "argocd": {
                "mode": "skipped",
                "accepted": True,
                "reason": "This fixture does not deploy an application.",
            },
        },
        "transition_control": {
            "prepare_confirmation_env": "FORGE_TRANSITION_PREPARE_CONFIRM",
            "enter_confirmation_env": "FORGE_TRANSITION_ENTER_CONFIRM",
            "finalize_confirmation_env": "FORGE_TRANSITION_FINALIZE_CONFIRM",
            "fallback_confirmation_env": "FORGE_TRANSITION_FALLBACK_CONFIRM",
            "rollback_confirmation_env": "FORGE_TRANSITION_ROLLBACK_CONFIRM",
            "failback_confirmation_env": "FORGE_TRANSITION_FAILBACK_CONFIRM",
            "live_env": "FORGE_TRANSITION_LIVE",
            "change_ticket_env": "FORGE_TRANSITION_CHANGE_TICKET",
            "max_proof_age_seconds": 3600,
            "relay_failure_threshold": 3,
            "auto_rollback": True,
        },
    }


def parsed_plan(provider: str = "gitlab") -> transition.TransitionPlan:
    return transition.parse_transition_plan(copy.deepcopy(base_plan(provider)))


def expect_error(plan: dict[str, object], expected: str) -> None:
    try:
        transition.parse_transition_plan(plan)
    except transition.migration.MigrationError as exc:
        if expected not in str(exc):
            fail(f"expected {expected!r} in {exc!r}")
    else:
        fail(f"expected validation error containing {expected!r}")


def test_plan_contract() -> None:
    for provider in ("gitlab", "github"):
        plan = parsed_plan(provider)
        if plan.source_provider != provider:
            fail(f"wrong source provider for {provider}")

    unsafe = copy.deepcopy(base_plan())
    unsafe["services"]["woodpecker"]["token"] = "plaintext"  # type: ignore[index]
    expect_error(unsafe, "must not contain credential")

    wrong_actor = copy.deepcopy(base_plan())
    wrong_actor["repositories"][0]["transition"]["destination_access"]["shadow_settings"][  # type: ignore[index]
        "push_whitelist_usernames"
    ] = ["someone-else"]
    expect_error(wrong_actor, "must contain only mirror_actor")

    no_rollback = copy.deepcopy(base_plan())
    no_rollback["transition_control"]["auto_rollback"] = False  # type: ignore[index]
    expect_error(no_rollback, "auto_rollback must be true")

    native_github = copy.deepcopy(base_plan("github"))
    native_github["repositories"][0]["transition"]["relay"]["driver"] = "gitlab-push"  # type: ignore[index]
    expect_error(native_github, "requires a GitLab source")

    reverse = transition.reverse_migration_plan(parsed_plan("gitlab").repositories[0])
    if reverse.source_provider != "forgejo" or reverse.destination_provider != "gitlab":
        fail("GitLab failback plan did not reverse provider authority")
    if reverse.metadata.get("pull_requests") != "skip" or reverse.metadata.get("merge_requests") != "skip":
        fail("GitLab failback plan did not normalize change-request metadata")


def test_state_integrity_and_lock() -> None:
    plan = parsed_plan()
    with tempfile.TemporaryDirectory(prefix="forge-transition-state-test-") as temp:
        path = Path(temp) / "state.json"
        transition.write_state(path, transition.initial_state(plan))
        loaded = transition.load_state(path, plan)
        if loaded["phase"] != "planned" or not loaded.get("state_sha256"):
            fail("durable state did not round-trip")
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["phase"] = "transition"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        try:
            transition.load_state(path, plan)
        except transition.TransitionError as exc:
            if "integrity" not in str(exc):
                raise
        else:
            fail("tampered transition state was accepted")

        lock_state = Path(temp) / "lock-state.json"
        with transition.StateLock(lock_state):
            try:
                with transition.StateLock(lock_state):
                    pass
            except transition.TransitionError:
                pass
            else:
                fail("concurrent state lock was accepted")


def test_github_secret_contract() -> None:
    plan = parsed_plan("github")
    environment = {
        "GITHUB_SOURCE_TOKEN": "source-token",
        "FORGEJO_DESTINATION_TOKEN": "destination-token",
        "WOODPECKER_API_TOKEN": "woodpecker-token",
    }
    with mock.patch.dict(os.environ, environment, clear=True):
        try:
            transition.require_credentials(plan)
        except transition.TransitionError as exc:
            if "GITHUB_DEPLOY_KEY_VALUE" not in str(exc):
                raise
        else:
            fail("missing GitHub secret source value was accepted")
    environment["GITHUB_DEPLOY_KEY_VALUE"] = "secret-value"
    with mock.patch.dict(os.environ, environment, clear=True):
        transition.require_credentials(plan)


def test_source_ci_controls() -> None:
    gitlab_repo = parsed_plan("gitlab").repositories[0]
    gitlab_snapshot = {
        "provider": "gitlab",
        "archived": False,
        "builds_access_level": "enabled",
        "schedules": [{"id": 7, "active": True}],
    }
    calls: list[tuple[str, str, object]] = []

    def record_request(_target: object, method: str, path: str, **kwargs: object) -> dict[str, object]:
        calls.append((method, path, kwargs.get("body")))
        return {}

    with (
        mock.patch("forge_transition.migration.api_request", side_effect=record_request),
        mock.patch("forge_transition.wait_for_source_ci_idle", return_value=[]),
        mock.patch(
            "forge_transition.verify_source_authority",
            return_value={"repository_writable": True, "ci_enabled": False, "verified": True},
        ),
    ):
        result = transition.disable_source_ci(gitlab_repo, gitlab_snapshot)
    if not result["verified"]:
        fail("GitLab CI disable did not verify")
    if not any(method == "PUT" and body == {"builds_access_level": "disabled"} for method, _path, body in calls):
        fail("GitLab CI access was not disabled")

    github_repo = parsed_plan("github").repositories[0]
    github_snapshot = {
        "provider": "github",
        "archived": False,
        "actions_enabled": True,
        "allowed_actions": "all",
    }
    calls.clear()
    with (
        mock.patch("forge_transition.migration.api_request", side_effect=record_request),
        mock.patch("forge_transition.wait_for_source_ci_idle", return_value=[]),
        mock.patch(
            "forge_transition.verify_source_authority",
            return_value={"repository_writable": True, "ci_enabled": False, "verified": True},
        ),
    ):
        result = transition.disable_source_ci(github_repo, github_snapshot)
    if not result["verified"]:
        fail("GitHub Actions disable did not verify")
    if not any(
        method == "PUT" and path.endswith("/actions/permissions") and body == {"enabled": False}
        for method, path, body in calls
    ):
        fail("GitHub Actions was not disabled")

    with (
        mock.patch("forge_transition.migration.api_request", side_effect=record_request),
        mock.patch(
            "forge_transition.source_ci_snapshot",
            return_value={"provider": "github", "archived": False, "actions_enabled": True},
        ),
    ):
        restored = transition.restore_source_ci(github_repo, github_snapshot)
    if not restored["verified"]:
        fail("GitHub Actions restoration did not verify")


def simple_inventory(plan: transition.TransitionPlan) -> dict[str, object]:
    repo = plan.repositories[0]
    return {
        "name": repo.migration.name,
        "source_url": transition.migration.redact_url(repo.migration.source_url),
        "source_state": {"archived": False},
        "pipelines": {
            "source_files": [{"path": ".gitlab-ci.yml", "sha": "abc"}],
            "mappings": [
                {
                    "source": ".gitlab-ci.yml",
                    "destinations": [".woodpecker.yml"],
                    "mode": "managed",
                    "matched_sources": [".gitlab-ci.yml"],
                }
            ],
            "unaccounted_source_files": [],
            "ambiguous_source_files": [],
            "unresolved_local_includes": [],
            "external_includes": {"verified": True},
            "deployment_gate_marker": "FORGE_TRANSITION_DEPLOYMENT_ENABLED",
            "verified": True,
        },
        "variables": {"items": [], "verified": True},
        "schedules": {"items": [], "verified": True},
        "runner_tags": {"items": [], "verified": True},
        "protections": {"items": [], "verified": True},
        "integrations": {"items": [], "verified": True},
        "destination": {"exists": True},
        "verified": True,
    }


def write_plan(path: Path, raw: dict[str, object]) -> transition.TransitionPlan:
    path.write_text(json.dumps(raw), encoding="utf-8")
    return transition.load_transition_plan(path)


def write_command_proof(
    path: Path,
    plan: transition.TransitionPlan,
    command: str,
    repositories: list[dict[str, object]],
    **extra: object,
) -> dict[str, object]:
    proof = transition.proof_base(plan, command, repositories)
    proof.update(extra)
    return transition.write_proof(path, proof)


def test_enter_and_automatic_rollback() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-transition-enter-test-") as temp:
        root = Path(temp)
        plan_path = root / "plan.json"
        plan = write_plan(plan_path, base_plan())
        inventory = simple_inventory(plan)
        shadow_path = root / "shadow.json"
        shadow = write_command_proof(
            shadow_path,
            plan,
            "verify-shadow",
            [{"name": "platform-app", "inventory": inventory, "verified": True}],
        )
        state_path = root / "state.json"
        state = transition.initial_state(plan)
        state["phase"] = "shadow"
        state["last_shadow_proof_sha256"] = shadow["proof_sha256"]
        transition.write_state(state_path, state)
        proof_path = root / "enter.json"
        args = argparse.Namespace(
            plan=plan_path,
            verification=shadow_path,
            state=state_path,
            work_dir=root / "work",
            proof=proof_path,
        )
        environment = {
            "FORGE_TRANSITION_LIVE": "1",
            "FORGE_TRANSITION_ENTER_CONFIRM": str(shadow["proof_sha256"]),
            "FORGE_TRANSITION_CHANGE_TICKET": "CHG-123",
        }
        relay_result = {"name": "platform-app", "synced_at": transition.utc_now(), "verified": True}
        snapshot = {
            "provider": "gitlab",
            "archived": False,
            "builds_access_level": "enabled",
            "schedules": [],
        }
        events: list[str] = []
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.discover_repository", return_value=inventory),
            mock.patch(
                "forge_transition.verify_source_authority",
                side_effect=lambda _repo, phase: {
                    "phase": phase,
                    "repository_writable": phase != "finalized",
                    "verified": True,
                },
            ),
            mock.patch(
                "forge_transition.reconcile_plan",
                side_effect=lambda _plan, current, _work: ([relay_result], copy.deepcopy(current)),
            ),
            mock.patch("forge_transition.source_ci_snapshot", return_value=snapshot),
            mock.patch(
                "forge_transition.disable_source_ci",
                side_effect=lambda _repo, _snapshot: events.append("source-off") or {"verified": True},
            ),
            mock.patch(
                "forge_transition.set_destination_authority",
                side_effect=lambda _plan, _repo, enabled: events.append(f"destination-{enabled}")
                or {"verified": True},
            ),
            mock.patch(
                "forge_transition.verify_transition_repository",
                return_value={"verified": True},
            ),
        ):
            if transition.command_enter(args) != 0:
                fail("successful handover returned failure")
        if events[:2] != ["source-off", "destination-True"]:
            fail(f"handover authority ordering is wrong: {events}")
        entered = transition.load_state(state_path, plan)
        if entered["phase"] != "transition" or entered["destination_authority_enabled"] is not True:
            fail("successful handover did not persist transition authority")

        rollback_state = copy.deepcopy(entered)
        rollback_state["phase"] = "shadow"
        rollback_state["destination_authority_enabled"] = False
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.discover_repository", return_value=inventory),
            mock.patch(
                "forge_transition.verify_source_authority",
                return_value={"repository_writable": True, "verified": True},
            ),
            mock.patch(
                "forge_transition.reconcile_plan",
                side_effect=lambda _plan, current, _work: ([relay_result], copy.deepcopy(current)),
            ),
            mock.patch("forge_transition.source_ci_snapshot", return_value=snapshot),
            mock.patch("forge_transition.disable_source_ci", return_value={"verified": True}),
            mock.patch("forge_transition.set_destination_authority", return_value={"verified": True}),
            mock.patch(
                "forge_transition.verify_transition_repository",
                side_effect=transition.TransitionError("canary failed"),
            ),
            mock.patch(
                "forge_transition.rollback_transition_state",
                return_value=([{"name": "platform-app", "verified": True}], rollback_state),
            ),
        ):
            entered["phase"] = "shadow"
            entered["last_shadow_proof_sha256"] = shadow["proof_sha256"]
            transition.write_state(state_path, entered)
            if transition.command_enter(args) != 1:
                fail("failed handover did not return failure")
        rolled_back = transition.load_state(state_path, plan)
        if rolled_back["phase"] != "shadow":
            fail("failed handover did not restore shadow state")


def test_manual_fallback_keeps_relay() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-transition-fallback-test-") as temp:
        root = Path(temp)
        plan_path = root / "plan.json"
        plan = write_plan(plan_path, base_plan())
        evidence_path = root / "status.json"
        evidence = write_command_proof(
            evidence_path,
            plan,
            "status",
            [{"name": "platform-app", "verified": True}],
            phase="transition",
        )
        state_path = root / "state.json"
        state = transition.initial_state(plan)
        state["phase"] = "transition"
        state["source_ci_snapshots"] = {"platform-app": {"provider": "gitlab"}}
        transition.write_state(state_path, state)
        proof_path = root / "fallback.json"
        args = argparse.Namespace(
            plan=plan_path,
            evidence=evidence_path,
            state=state_path,
            proof=proof_path,
        )
        recovered = copy.deepcopy(state)
        recovered["phase"] = "shadow"
        recovered["destination_authority_enabled"] = False
        environment = {
            "FORGE_TRANSITION_LIVE": "1",
            "FORGE_TRANSITION_FALLBACK_CONFIRM": str(evidence["proof_sha256"]),
            "FORGE_TRANSITION_CHANGE_TICKET": "CHG-321",
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch(
                "forge_transition.rollback_transition_state",
                return_value=([{"name": "platform-app", "verified": True}], recovered),
            ) as rollback,
        ):
            if transition.command_fallback(args) != 0:
                fail("manual fallback returned failure")
        if rollback.call_args.kwargs.get("stop_relay") is not False:
            fail("manual fallback stopped the relay instead of preserving shadow synchronization")
        fallback_state = transition.load_state(state_path, plan)
        if fallback_state["phase"] != "shadow" or not fallback_state.get(
            "fallback_proof_sha256"
        ):
            fail("manual fallback did not persist shadow recovery evidence")


def test_finalize_and_relay_failure_rollback() -> None:
    with tempfile.TemporaryDirectory(prefix="forge-transition-finalize-test-") as temp:
        root = Path(temp)
        plan_path = root / "plan.json"
        plan = write_plan(plan_path, base_plan())
        enter_path = root / "enter.json"
        enter = write_command_proof(
            enter_path,
            plan,
            "enter",
            [{"name": "platform-app", "verified": True}],
        )
        state_path = root / "state.json"
        state = transition.initial_state(plan)
        state["phase"] = "transition"
        state["enter_proof_sha256"] = enter["proof_sha256"]
        state["source_ci_snapshots"] = {"platform-app": {"provider": "gitlab"}}
        state["repositories"] = {
            "platform-app": {"synced_at": transition.utc_now(), "verified": True}
        }
        transition.write_state(state_path, state)
        final_path = root / "final.json"
        args = argparse.Namespace(
            plan=plan_path,
            evidence=enter_path,
            state=state_path,
            work_dir=root / "work",
            proof=final_path,
        )
        environment = {
            "FORGE_TRANSITION_LIVE": "1",
            "FORGE_TRANSITION_FINALIZE_CONFIRM": str(enter["proof_sha256"]),
            "FORGE_TRANSITION_CHANGE_TICKET": "CHG-456",
        }
        relay = {"name": "platform-app", "synced_at": transition.utc_now(), "verified": True}
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.verify_operational_repository", return_value={"verified": True}),
            mock.patch(
                "forge_transition.source_ci_snapshot",
                return_value={"provider": "gitlab", "archived": False, "builds_access_level": "disabled"},
            ),
            mock.patch("forge_transition.freeze_source_repository", return_value={"verified": True}),
            mock.patch(
                "forge_transition.reconcile_plan",
                side_effect=lambda _plan, current, _work: ([relay], copy.deepcopy(current)),
            ),
            mock.patch("forge_transition.set_native_relay_enabled", return_value={"verified": True}),
            mock.patch("forge_transition.set_destination_access", return_value={"verified": True}),
            mock.patch("forge_transition.verify_transition_repository", return_value={"verified": True}),
            mock.patch("forge_transition.verify_source_authority", return_value={"verified": True}),
        ):
            if transition.command_finalize(args) != 0:
                fail("successful finalization returned failure")
        finalized = transition.load_state(state_path, plan)
        if finalized["phase"] != "finalized":
            fail("finalization did not persist final authority")

        failback_path = root / "failback.json"
        failback_args = argparse.Namespace(
            plan=plan_path,
            evidence=final_path,
            state=state_path,
            work_dir=root / "failback-work",
            proof=failback_path,
        )
        failback_environment = {
            "FORGE_TRANSITION_LIVE": "1",
            "FORGE_TRANSITION_FAILBACK_CONFIRM": str(
                json.loads(final_path.read_text(encoding="utf-8"))["proof_sha256"]
            ),
            "FORGE_TRANSITION_CHANGE_TICKET": "CHG-789",
        }
        failback_events: list[str] = []
        with (
            mock.patch.dict(os.environ, failback_environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.verify_operational_repository", return_value={"verified": True}),
            mock.patch(
                "forge_transition.set_destination_authority",
                side_effect=lambda _plan, _repo, enabled: failback_events.append(
                    f"destination-{enabled}"
                )
                or {"verified": True},
            ),
            mock.patch(
                "forge_transition.set_destination_access",
                side_effect=lambda _repo, phase: failback_events.append(f"access-{phase}")
                or {"verified": True},
            ),
            mock.patch("forge_transition.set_native_relay_enabled", return_value={"verified": True}),
            mock.patch(
                "forge_transition.restore_source_repository",
                side_effect=lambda _repo, _snapshot: failback_events.append("source-unarchive")
                or {"verified": True},
            ),
            mock.patch(
                "forge_transition.reverse_sync_repository",
                side_effect=lambda _repo, _work: failback_events.append("reverse-sync")
                or {"verified": True},
            ),
            mock.patch(
                "forge_transition.restore_source_ci",
                side_effect=lambda _repo, _snapshot: failback_events.append("source-ci-on")
                or {"verified": True},
            ),
        ):
            if transition.command_failback(failback_args) != 0:
                fail("successful finalized failback returned failure")
        for earlier, later in (
            ("destination-False", "source-unarchive"),
            ("source-unarchive", "reverse-sync"),
            ("reverse-sync", "source-ci-on"),
        ):
            if failback_events.index(earlier) >= failback_events.index(later):
                fail(f"unsafe finalized failback ordering: {failback_events}")
        failed_back = transition.load_state(state_path, plan)
        if failed_back["phase"] != "rolled-back" or failed_back.get(
            "destination_authority_enabled"
        ) is not False:
            fail("finalized failback did not restore source authority")

        transition.write_state(state_path, finalized)
        failed_recovery = copy.deepcopy(finalized)
        failed_recovery["phase"] = "finalized"
        failed_recovery["destination_authority_enabled"] = True
        with (
            mock.patch.dict(os.environ, failback_environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.verify_operational_repository", return_value={"verified": True}),
            mock.patch("forge_transition.set_destination_authority", return_value={"verified": True}),
            mock.patch("forge_transition.set_destination_access", return_value={"verified": True}),
            mock.patch("forge_transition.set_native_relay_enabled", return_value={"verified": True}),
            mock.patch("forge_transition.restore_source_repository", return_value={"verified": True}),
            mock.patch(
                "forge_transition.reverse_sync_repository",
                side_effect=transition.TransitionError("reverse sync failed"),
            ),
            mock.patch(
                "forge_transition.restore_finalized_after_failback_failure",
                return_value=([{"name": "platform-app", "verified": True}], failed_recovery),
            ),
        ):
            if transition.command_failback(failback_args) != 1:
                fail("failed finalized failback did not return failure")
        recovered_finalized = transition.load_state(state_path, plan)
        if recovered_finalized["phase"] != "finalized":
            fail("failed finalized failback did not recover destination authority")

        transition_state = transition.initial_state(plan)
        transition_state["phase"] = "transition"
        transition_state["consecutive_failures"] = 0
        transition_state["source_ci_snapshots"] = {"platform-app": {"provider": "gitlab"}}
        transition.write_state(state_path, transition_state)
        relay_args = argparse.Namespace(
            plan=plan_path,
            state=state_path,
            proof_dir=root / "relay-proofs",
            interval=None,
            once=True,
        )
        failed_state = copy.deepcopy(transition_state)
        failed_state["consecutive_failures"] = 3
        rollback_state = copy.deepcopy(failed_state)
        rollback_state["phase"] = "shadow"
        with (
            mock.patch("forge_transition.require_credentials"),
            mock.patch(
                "forge_transition.reconcile_plan",
                return_value=([{"name": "platform-app", "verified": False}], failed_state),
            ),
            mock.patch(
                "forge_transition.rollback_transition_state",
                return_value=([{"name": "platform-app", "verified": True}], rollback_state),
            ),
        ):
            if transition.command_run_relay(relay_args) != 1:
                fail("relay threshold should return failure after automatic rollback")
        recovered = transition.load_state(state_path, plan)
        if recovered["phase"] != "shadow" or not recovered.get("automatic_rollback_proof_sha256"):
            fail("relay failure threshold did not persist automatic rollback evidence")


def test_proof_integrity() -> None:
    plan = parsed_plan()
    with tempfile.TemporaryDirectory(prefix="forge-transition-proof-test-") as temp:
        path = Path(temp) / "proof.json"
        transition.write_proof(
            path,
            transition.proof_base(plan, "status", [{"name": "platform-app", "verified": True}]),
        )
        if transition.command_verify_proof(argparse.Namespace(proof_file=path)) != 0:
            fail("valid transition proof was rejected")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["verified"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")
        if transition.command_verify_proof(argparse.Namespace(proof_file=path)) != 1:
            fail("tampered transition proof was accepted")


def expect_transition_error(action: object, expected: str) -> None:
    try:
        action()  # type: ignore[operator]
    except transition.TransitionError as exc:
        if expected not in str(exc):
            fail(f"expected {expected!r} in {exc!r}")
    else:
        fail(f"expected transition error containing {expected!r}")


def native_gitlab_plan(auth_method: str = "password", mode: str = "managed") -> transition.TransitionPlan:
    raw = copy.deepcopy(base_plan())
    relay = raw["repositories"][0]["transition"]["relay"]  # type: ignore[index]
    relay.update(  # type: ignore[union-attr]
        {
            "mode": mode,
            "driver": "gitlab-push",
            "auth_method": auth_method,
            "username_env": "FORGE_MIRROR_USERNAME",
            "password_env": "FORGE_MIRROR_PASSWORD",
            "host_keys": ["forgejo.example.invalid ssh-ed25519 AAAAexample"],
        }
    )
    return transition.parse_transition_plan(raw)


def test_validation_confirmation_and_proof_edges() -> None:
    expect_transition_error(
        lambda: transition.object_value({}, "missing", "fixture"),
        "must be an object",
    )
    expect_transition_error(
        lambda: transition.list_value({}, "missing", "fixture"),
        "must be an array",
    )
    expect_transition_error(
        lambda: transition.string_value({}, "missing", "fixture"),
        "is required",
    )
    expect_transition_error(
        lambda: transition.env_name({"name": "not-valid"}, "name", "fixture"),
        "must name an environment variable",
    )
    expect_transition_error(
        lambda: transition.accounted_mode({"mode": "unsupported"}, "fixture"),
        "marked unsupported",
    )
    expect_transition_error(
        lambda: transition.accounted_mode({"mode": "manual"}, "fixture"),
        "requires accepted=true",
    )

    invalid_version = copy.deepcopy(base_plan())
    invalid_version["transition_version"] = 99
    expect_error(invalid_version, "transition_version must be")
    invalid_direction = copy.deepcopy(base_plan())
    invalid_direction["direction"] = "forgejo-to-gitlab"
    expect_error(invalid_direction, "direction must be one of")
    unexpected_surface = copy.deepcopy(base_plan())
    unexpected_surface["repositories"][0]["cutover"]["unknown"] = {}  # type: ignore[index]
    expect_error(unexpected_surface, "unsupported surface")
    unexpected_transition = copy.deepcopy(base_plan())
    unexpected_transition["repositories"][0]["transition"]["unknown"] = {}  # type: ignore[index]
    expect_error(unexpected_transition, "unsupported setting")

    bad_relay = copy.deepcopy(base_plan())
    bad_relay["repositories"][0]["transition"]["relay"]["driver"] = "unknown"  # type: ignore[index]
    expect_error(bad_relay, "driver must be one of")
    bad_source_ci = copy.deepcopy(base_plan())
    bad_source_ci["repositories"][0]["transition"]["source_ci"][  # type: ignore[index]
        "keep_repository_writable"
    ] = False
    expect_error(bad_source_ci, "keep_repository_writable must be true")
    bad_control = copy.deepcopy(base_plan())
    bad_control["transition_control"]["live_env"] = "not-valid"  # type: ignore[index]
    expect_error(bad_control, "must name an environment variable")

    native = native_gitlab_plan()
    credentials = {
        "GITLAB_SOURCE_TOKEN": "source",
        "FORGEJO_DESTINATION_TOKEN": "destination",
        "WOODPECKER_API_TOKEN": "woodpecker",
    }
    with mock.patch.dict(os.environ, credentials, clear=True):
        expect_transition_error(
            lambda: transition.require_credentials(native),
            "relay credential FORGE_MIRROR_USERNAME",
        )
    credentials.update(
        {
            "FORGE_MIRROR_USERNAME": "mirror",
            "FORGE_MIRROR_PASSWORD": "secret",
        }
    )
    with mock.patch.dict(os.environ, credentials, clear=True):
        transition.require_credentials(native)

    plan = parsed_plan()
    with tempfile.TemporaryDirectory(prefix="forge-transition-proof-edges-") as temp:
        root = Path(temp)
        path = root / "proof.json"
        proof = transition.write_proof(
            path,
            transition.proof_base(
                plan,
                "status",
                [{"name": "platform-app", "verified": True}],
            ),
        )
        transition.load_proof(path, plan, ("status",))
        expect_transition_error(
            lambda: transition.load_proof(path, plan, ("prepare",)),
            "command is not accepted",
        )
        unverified_path = root / "unverified.json"
        transition.write_proof(
            unverified_path,
            transition.proof_base(
                plan,
                "status",
                [{"name": "platform-app", "verified": False}],
            ),
        )
        expect_transition_error(
            lambda: transition.load_proof(unverified_path, plan, ("status",)),
            "proof is not verified",
        )
        expect_transition_error(
            lambda: transition.proof_age_seconds({"generated_at": "not-a-date"}),
            "generated_at is invalid",
        )

        live = str(plan.control["live_env"])
        confirm = str(plan.control["rollback_confirmation_env"])
        ticket = str(plan.control["change_ticket_env"])
        expect_transition_error(
            lambda: transition.require_confirmation(
                plan,
                proof,
                "rollback_confirmation_env",
            ),
            "live transition is disabled",
        )
        with mock.patch.dict(os.environ, {live: "1"}, clear=True):
            expect_transition_error(
                lambda: transition.require_confirmation(
                    plan,
                    proof,
                    "rollback_confirmation_env",
                ),
                "approval mismatch",
            )
        with mock.patch.dict(
            os.environ,
            {live: "1", confirm: str(proof["proof_sha256"])},
            clear=True,
        ):
            expect_transition_error(
                lambda: transition.require_confirmation(
                    plan,
                    proof,
                    "rollback_confirmation_env",
                ),
                f"{ticket} is required",
            )
        with mock.patch.dict(
            os.environ,
            {
                live: "1",
                confirm: str(proof["proof_sha256"]),
                ticket: "CHG-100",
            },
            clear=True,
        ):
            transition.require_confirmation(plan, proof, "rollback_confirmation_env")
            stale = dict(proof)
            stale["generated_at"] = "2000-01-01T00:00:00Z"
            expect_transition_error(
                lambda: transition.require_confirmation(
                    plan,
                    stale,
                    "rollback_confirmation_env",
                ),
                "proof is stale",
            )


def test_github_inventory_and_destination_access() -> None:
    plan = parsed_plan("github")
    repo = plan.repositories[0]
    target = transition.api_target(repo, "source")

    pages = [
        [{"id": index} for index in range(100)],
        [{"id": 100}],
    ]
    with mock.patch(
        "forge_transition.migration.api_request",
        side_effect=lambda *_args, **_kwargs: pages.pop(0),
    ):
        if len(transition.paged_list(target, "items")) != 101:
            fail("GitHub pagination did not consume every page")
    with mock.patch("forge_transition.migration.api_request", return_value={"items": {}}):
        expect_transition_error(
            lambda: transition.paged_list(target, "items", key="items"),
            "returned an invalid list",
        )

    encoded = base64.b64encode(b"name: ci\n").decode("ascii")
    with mock.patch(
        "forge_transition.migration.api_request",
        return_value={"encoding": "base64", "content": encoded},
    ):
        if transition.github_file_text(repo, ".github/workflows/ci.yml", "main") != "name: ci\n":
            fail("GitHub workflow content was not decoded")
    with mock.patch(
        "forge_transition.migration.api_request",
        return_value={"encoding": "plain", "content": "text"},
    ):
        expect_transition_error(
            lambda: transition.github_file_text(repo, "bad.yml", "main"),
            "unreadable workflow content",
        )

    tree = {
        "tree": [
            "malformed-item",
            {"type": "tree", "path": ".github/workflows"},
            {
                "type": "blob",
                "path": ".github/workflows/ci.yml",
                "sha": "abc",
            },
        ]
    }
    with (
        mock.patch("forge_transition.migration.api_request", return_value=tree),
        mock.patch(
            "forge_transition.github_file_text",
            return_value="on:\n  schedule:\n    - cron: '0 3 * * *'\n",
        ),
    ):
        workflows = transition.github_workflows(repo, {"default_branch": "main"})
    if workflows != [
        {
            "path": ".github/workflows/ci.yml",
            "sha": "abc",
            "schedules": ["0 3 * * *"],
        }
    ]:
        fail(f"GitHub workflow inventory is wrong: {workflows}")

    scoped_raw = copy.deepcopy(base_plan("github"))
    variables = scoped_raw["repositories"][0]["cutover"]["variables"]  # type: ignore[index]
    variables["organization_scope"] = {"mode": "managed"}  # type: ignore[index]
    variables["environment_scope"] = {"mode": "managed"}  # type: ignore[index]
    scoped_repo = transition.parse_transition_plan(scoped_raw).repositories[0]

    def inventory_page(
        _target: object,
        path: str,
        key: str | None = None,
        query: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        del key, query
        if path.endswith("/environments"):
            return [{"name": "production"}]
        if path.endswith("/actions/variables"):
            return [{"name": "REGION", "value": "eu"}]
        if path.endswith("/actions/secrets"):
            return [{"name": "DEPLOY_KEY"}]
        if path.endswith("/variables"):
            return [{"name": "ENVIRONMENT", "value": "prod"}]
        if path.endswith("/secrets"):
            return [{"name": "ENV_SECRET"}]
        return []

    with mock.patch("forge_transition.paged_list", side_effect=inventory_page):
        inventory = transition.github_variable_inventory(scoped_repo, include_values=True)
    identities = {str(item["identity"]) for item in inventory}
    for expected in (
        "repository-variable:REGION",
        "repository-secret:DEPLOY_KEY",
        "environment-variable:production:ENVIRONMENT",
        "environment-secret:production:ENV_SECRET",
        "organization-variable:REGION",
        "organization-secret:DEPLOY_KEY",
    ):
        if expected not in identities:
            fail(f"GitHub variable inventory omitted {expected}")
    accounting = transition.account_github_variables(scoped_repo, inventory)
    if accounting["verified"] is not False or not accounting["unaccounted"]:
        fail("unmapped GitHub variables did not fail closed")

    with (
        mock.patch("forge_transition.github_variable_inventory", return_value=inventory),
        mock.patch.dict(os.environ, {"GITHUB_DEPLOY_KEY_VALUE": "secret-value"}, clear=True),
    ):
        values = transition.source_variable_values(scoped_repo)
    if values.get("repository-secret:DEPLOY_KEY") != "secret-value":
        fail("GitHub secret value environment mapping was not honored")

    expected_settings = copy.deepcopy(repo.transition["destination_access"]["shadow_settings"])
    actual = {"rule_name": "**", **expected_settings}
    create_responses = iter([[], {}, [actual]])
    with mock.patch(
        "forge_transition.migration.api_request",
        side_effect=lambda *_args, **_kwargs: next(create_responses),
    ):
        access = transition.set_destination_access(repo, "shadow")
    if access["action"] != "created" or access["verified"] is not True:
        fail("Forgejo shadow branch protection was not created and verified")

    update_responses = iter([[actual], {}, [actual]])
    with mock.patch(
        "forge_transition.migration.api_request",
        side_effect=lambda *_args, **_kwargs: next(update_responses),
    ):
        access = transition.set_destination_access(repo, "shadow")
    if access["action"] != "updated" or access["verified"] is not True:
        fail("Forgejo shadow branch protection was not updated and verified")
    expect_transition_error(
        lambda: transition.set_destination_access(repo, "invalid"),
        "unsupported destination access phase",
    )
    with mock.patch("forge_transition.migration.api_request", return_value={}):
        expect_transition_error(
            lambda: transition.verify_destination_access(repo, "shadow"),
            "returned invalid data",
        )

    def discover_request(
        _target: object,
        _method: str,
        path: str,
        **kwargs: object,
    ) -> object:
        if path == "source-base":
            return {
                "id": 1,
                "full_name": "source/platform-app",
                "default_branch": "main",
                "archived": False,
            }
        if path == "destination-base":
            return (404, {}) if kwargs.get("return_status") else {}
        if path.endswith("/actions/runners"):
            return {"runners": []}
        if path.endswith("/actions/permissions"):
            return {"enabled": True}
        return []

    with (
        mock.patch(
            "forge_transition.api_base",
            side_effect=lambda _repo, side: (
                target,
                "source-base" if side == "source" else "destination-base",
            ),
        ),
        mock.patch("forge_transition.migration.api_request", side_effect=discover_request),
        mock.patch("forge_transition.github_workflows", return_value=[]),
        mock.patch(
            "forge_transition.github_variable_inventory",
            return_value=[
                {
                    "identity": "repository-secret:DEPLOY_KEY",
                    "kind": "repository-secret",
                    "name": "DEPLOY_KEY",
                    "value": None,
                }
            ],
        ),
        mock.patch("forge_transition.paged_list", return_value=[]),
        mock.patch(
            "forge_transition.cutover.account_pipeline_files",
            return_value={"verified": True},
        ),
        mock.patch(
            "forge_transition.cutover.account_named_surface",
            return_value={"verified": True},
        ),
    ):
        discovered = transition.github_discover(repo)
    if discovered["destination"]["exists"] is not False or not discovered["verified"]:
        fail(f"GitHub discovery did not preserve an absent destination safely: {discovered}")


def test_source_authority_relay_and_recovery_helpers() -> None:
    gitlab_repo = parsed_plan("gitlab").repositories[0]
    github_repo = parsed_plan("github").repositories[0]

    with (
        mock.patch(
            "forge_transition.migration.api_request",
            return_value={"archived": False, "builds_access_level": "enabled"},
        ),
        mock.patch(
            "forge_transition.cutover.gitlab_list",
            return_value=[{"id": 7, "active": True}],
        ),
    ):
        snapshot = transition.source_ci_snapshot(gitlab_repo)
    if snapshot["provider"] != "gitlab" or snapshot["schedules"][0]["active"] is not True:
        fail("GitLab source CI snapshot is incomplete")

    github_responses = iter(
        [
            {"archived": False},
            {"enabled": True, "allowed_actions": "selected"},
        ]
    )
    with mock.patch(
        "forge_transition.migration.api_request",
        side_effect=lambda *_args, **_kwargs: next(github_responses),
    ):
        github_snapshot = transition.source_ci_snapshot(github_repo)
    if not github_snapshot["actions_enabled"] or github_snapshot["allowed_actions"] != "selected":
        fail("GitHub source CI snapshot is incomplete")

    calls: list[str] = []
    with (
        mock.patch(
            "forge_transition.cutover.gitlab_active_pipelines",
            side_effect=[[{"id": 9}], []],
        ),
        mock.patch(
            "forge_transition.migration.api_request",
            side_effect=lambda _target, _method, path, **_kwargs: calls.append(path) or {},
        ),
        mock.patch("forge_transition.time.sleep"),
    ):
        if transition.wait_for_source_ci_idle(gitlab_repo, True, 30):
            fail("cancelled GitLab pipeline remained active")
    if not any(path.endswith("/pipelines/9/cancel") for path in calls):
        fail("active GitLab pipeline was not cancelled")

    with (
        mock.patch(
            "forge_transition.github_active_runs",
            return_value=[{"id": 11}],
        ),
        mock.patch("forge_transition.time.monotonic", side_effect=[0.0, 31.0]),
    ):
        active = transition.wait_for_source_ci_idle(github_repo, False, 30)
    if active != [{"id": 11}]:
        fail("GitHub active-run timeout did not fail closed")

    for phase, expected in (("shadow", True), ("transition", True), ("finalized", True)):
        authority_snapshot = {
            "provider": "gitlab",
            "archived": phase == "finalized",
            "builds_access_level": "enabled" if phase == "shadow" else "disabled",
            "schedules": [{"active": phase == "shadow"}],
        }
        with mock.patch("forge_transition.source_ci_snapshot", return_value=authority_snapshot):
            result = transition.verify_source_authority(gitlab_repo, phase)
        if result["verified"] is not expected:
            fail(f"GitLab source authority phase {phase} was not verified")
    expect_transition_error(
        lambda: transition.verify_source_authority(gitlab_repo, "invalid"),
        "unsupported source authority phase",
    )
    with mock.patch(
        "forge_transition.source_ci_snapshot",
        return_value={"archived": True, "builds_access_level": "disabled"},
    ):
        expect_transition_error(
            lambda: transition.freeze_source_repository(gitlab_repo),
            "already archived",
        )

    state = transition.initial_state(parsed_plan())
    state["phase"] = "transition"
    state["source_ci_snapshots"] = {
        "platform-app": {"provider": "gitlab", "archived": False}
    }
    with (
        mock.patch("forge_transition.set_destination_authority", return_value={"verified": True}),
        mock.patch("forge_transition.set_destination_access", return_value={"verified": True}),
        mock.patch("forge_transition.restore_source_ci", return_value={"verified": True}),
        mock.patch("forge_transition.set_native_relay_enabled", return_value={"verified": True}),
    ):
        results, rolled_back = transition.rollback_transition_state(
            parsed_plan(),
            state,
            stop_relay=True,
        )
    if not results[0]["verified"] or rolled_back["phase"] != "rolled-back":
        fail("durable rollback helper did not restore source authority")

    if transition.normalized_repository_url(
        "HTTPS://Forgejo.Example.Invalid:443/platform/app.git/"
    ) != "https://forgejo.example.invalid:443/platform/app":
        fail("repository URL normalization is unstable")
    credentialed = transition.credentialed_url(
        "https://forgejo.example.invalid/platform/app.git",
        "mirror user",
        "p@ss word",
    )
    if "mirror%20user:p%40ss%20word@" not in credentialed:
        fail("mirror credentials were not URL encoded")
    expect_transition_error(
        lambda: transition.credentialed_url("ssh://forgejo/repo.git", "u", "p"),
        "require an HTTP(S)",
    )

    native = native_gitlab_plan().repositories[0]
    with mock.patch(
        "forge_transition.cutover.gitlab_list",
        return_value=[
            {"id": 1, "url": native.migration.destination_url},
            {"id": 2, "url": native.migration.destination_url},
        ],
    ):
        expect_transition_error(
            lambda: transition.find_gitlab_push_mirror(native),
            "multiple GitLab push mirrors",
        )

    with (
        mock.patch("forge_transition.paged_list", return_value=[]),
        mock.patch(
            "forge_transition.migration.api_request",
            return_value={"id": 21, "title": "transition"},
        ),
    ):
        key = transition.ensure_forgejo_deploy_key(native, "transition", "ssh-ed25519 AAAA")
    if key["status"] != "created" or not key["verified"]:
        fail("Forgejo deploy key was not created")
    with mock.patch(
        "forge_transition.paged_list",
        return_value=[{"id": 22, "key": "ssh-ed25519 AAAA", "read_only": True}],
    ):
        expect_transition_error(
            lambda: transition.ensure_forgejo_deploy_key(
                native,
                "transition",
                "ssh-ed25519 AAAA",
            ),
            "read-only",
        )

    mirror_calls: list[tuple[str, str]] = []

    def mirror_request(
        _target: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        mirror_calls.append((method, path))
        if method == "POST" and path.endswith("/remote_mirrors"):
            return {"id": 31}
        if method == "GET" and path.endswith("/remote_mirrors/31"):
            return {
                "id": 31,
                "enabled": True,
                "update_status": "finished",
                "last_error": "",
            }
        return {}

    with (
        mock.patch.dict(
            os.environ,
            {
                "FORGE_MIRROR_USERNAME": "mirror",
                "FORGE_MIRROR_PASSWORD": "secret",
            },
            clear=True,
        ),
        mock.patch("forge_transition.find_gitlab_push_mirror", return_value=None),
        mock.patch("forge_transition.migration.api_request", side_effect=mirror_request),
    ):
        mirror = transition.ensure_gitlab_push_mirror(native)
    if mirror["action"] != "created" or not mirror["verified"]:
        fail("GitLab push mirror was not created and verified")
    if not any(method == "POST" and path.endswith("/sync") for method, path in mirror_calls):
        fail("new GitLab push mirror was not synchronized")

    with mock.patch("forge_transition.find_gitlab_push_mirror", return_value=None):
        mapped = transition.ensure_gitlab_push_mirror(
            native_gitlab_plan(mode="mapped").repositories[0]
        )
    if mapped["verified"] is not False:
        fail("missing mapped GitLab mirror did not fail closed")

    external = transition.set_native_relay_enabled(gitlab_repo, True)
    if not external["managed_externally"] or not external["verified"]:
        fail("external relay ownership was not preserved")

    with (
        mock.patch("forge_transition.migration.prepare_mirror", return_value=Path("mirror.git")),
        mock.patch("forge_transition.migration.migrate_lfs", return_value={"verified": True}),
        mock.patch("forge_transition.migration.push_mirror"),
        mock.patch(
            "forge_transition.migration.verify_destination_repository",
            return_value={"verified": True},
        ),
        mock.patch(
            "forge_transition.migration.reconcile_destination_default_branch",
            side_effect=lambda _repo, result: result,
        ),
        mock.patch("forge_transition.migration.compare_refs", return_value={"verified": True}),
        mock.patch("forge_transition.migration.verify_lfs", return_value={"verified": True}),
        mock.patch("forge_transition.migration.migrate_wiki", return_value={"verified": True}),
        mock.patch("forge_transition.migration.migrate_metadata", return_value={"verified": True}),
    ):
        synced = transition.sync_git_data(gitlab_repo, Path("work"))
    if not synced["verified"]:
        fail("relay Git/LFS/wiki/metadata synchronization did not verify")

    recovery_plan = parsed_plan()
    recovery_state = transition.initial_state(recovery_plan)
    recovery_state["phase"] = "finalized"
    recovery_state["finalize_snapshots"] = {
        "platform-app": {"provider": "gitlab", "archived": False}
    }
    with (
        mock.patch(
            "forge_transition.source_ci_snapshot",
            return_value={"provider": "gitlab", "archived": False},
        ),
        mock.patch("forge_transition.restore_source_ci", return_value={"verified": True}),
        mock.patch("forge_transition.freeze_source_repository", return_value={"verified": True}),
        mock.patch("forge_transition.set_destination_access", return_value={"verified": True}),
        mock.patch("forge_transition.set_destination_authority", return_value={"verified": True}),
        mock.patch("forge_transition.set_native_relay_enabled", return_value={"verified": True}),
    ):
        recovered, recovered_state = transition.restore_finalized_after_failback_failure(
            recovery_plan,
            recovery_state,
        )
    if not recovered[0]["verified"] or recovered_state["phase"] != "finalized":
        fail("failed failback recovery did not restore finalized authority")

    with (
        mock.patch("forge_transition.set_destination_access", return_value={"verified": True}),
        mock.patch("forge_transition.set_native_relay_enabled", return_value={"verified": True}),
        mock.patch("forge_transition.restore_source_repository", return_value={"verified": True}),
    ):
        restored, restored_state = transition.restore_failed_finalization(
            recovery_plan,
            recovery_state,
        )
    if not restored[0]["verified"] or restored_state["phase"] != "transition":
        fail("failed finalization recovery did not restore transition authority")


def test_prepare_verify_and_command_surfaces() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    inventory = simple_inventory(plan)
    service = transition.cutover.ServiceTarget(
        "woodpecker",
        "https://ci.example.invalid",
        token_env="WOODPECKER_API_TOKEN",
    )

    def service_request(
        _target: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> object:
        if method == "POST" and path == "api/repos":
            return {"id": 41}
        if method == "GET" and path == "api/agents":
            return []
        return {}

    with (
        mock.patch(
            "forge_transition.migration.migrate_repo",
            return_value={
                "destination_repository": {"verified": True},
                "verified": True,
            },
        ),
        mock.patch(
            "forge_transition.migration.api_request",
            return_value={
                "id": 10,
                "full_name": "platform/platform-app",
                "default_branch": "main",
            },
        ),
        mock.patch("forge_transition.cutover.service_target", return_value=service),
        mock.patch("forge_transition.cutover.woodpecker_lookup", return_value=None),
        mock.patch("forge_transition.cutover.service_request", side_effect=service_request),
        mock.patch(
            "forge_transition.cutover.woodpecker_secret_upsert",
            return_value={"verified": True},
        ),
        mock.patch(
            "forge_transition.source_variable_values",
            return_value={"project:DEPLOY_KEY:*": "secret"},
        ),
        mock.patch(
            "forge_transition.cutover.prepare_destination_protections",
            return_value={"verified": True},
        ),
        mock.patch("forge_transition.set_destination_access", return_value={"verified": True}),
        mock.patch("forge_transition.cutover.prepare_harbor", return_value={"verified": True}),
        mock.patch("forge_transition.cutover.verify_argocd", return_value={"verified": True}),
    ):
        prepared = transition.prepare_transition_repository(plan, repo)
    if not prepared["verified"] or prepared["woodpecker"]["action"] != "activated":
        fail("transition preparation did not activate dormant Woodpecker safely")

    verified_child = {"verified": True}
    with (
        mock.patch("forge_transition.discover_repository", return_value=inventory),
        mock.patch("forge_transition.migration.verify_repo", return_value=verified_child),
        mock.patch(
            "forge_transition.migration.api_request",
            side_effect=lambda _target, _method, path, **_kwargs: (
                []
                if path == "api/agents"
                else {
                    "id": 10,
                    "full_name": "platform/platform-app",
                    "default_branch": "main",
                }
            ),
        ),
        mock.patch("forge_transition.cutover.service_target", return_value=service),
        mock.patch("forge_transition.cutover.woodpecker_lookup", return_value={"id": 41}),
        mock.patch("forge_transition.cutover.service_request", side_effect=service_request),
        mock.patch(
            "forge_transition.cutover.verify_runner_capabilities",
            return_value=verified_child,
        ),
        mock.patch(
            "forge_transition.cutover.verify_woodpecker_configuration",
            return_value=verified_child,
        ),
        mock.patch(
            "forge_transition.cutover.verify_destination_protections",
            return_value=verified_child,
        ),
        mock.patch(
            "forge_transition.cutover.verify_destination_integrations",
            return_value=verified_child,
        ),
        mock.patch("forge_transition.verify_destination_access", return_value=verified_child),
        mock.patch("forge_transition.cutover.trigger_woodpecker_canary", return_value=verified_child),
        mock.patch("forge_transition.cutover.verify_harbor_canary", return_value=verified_child),
        mock.patch("forge_transition.cutover.verify_argocd", return_value=verified_child),
    ):
        verification = transition.verify_transition_repository(
            plan,
            repo,
            prepared,
            "shadow",
        )
    if not verification["verified"]:
        fail("prepared transition repository did not verify")
    expect_transition_error(
        lambda: transition.verify_transition_repository(plan, repo, prepared, "invalid"),
        "unsupported verification phase",
    )

    with tempfile.TemporaryDirectory(prefix="forge-transition-command-surfaces-") as temp:
        root = Path(temp)
        plan_path = root / "plan.json"
        plan = write_plan(plan_path, base_plan())
        discovery_path = root / "discover.json"
        discovery = write_command_proof(
            discovery_path,
            plan,
            "discover",
            [simple_inventory(plan)],
        )
        state_path = root / "state.json"
        prepare_path = root / "prepare.json"
        environment = {
            "FORGE_TRANSITION_LIVE": "1",
            "FORGE_TRANSITION_PREPARE_CONFIRM": str(discovery["proof_sha256"]),
            "FORGE_TRANSITION_CHANGE_TICKET": "CHG-200",
        }
        prepared_repo = {
            "name": "platform-app",
            "verified": True,
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.discover_repository", return_value=simple_inventory(plan)),
            mock.patch(
                "forge_transition.verify_source_authority",
                return_value={"verified": True},
            ),
            mock.patch(
                "forge_transition.prepare_transition_repository",
                return_value=prepared_repo,
            ),
        ):
            rc = transition.command_prepare(
                argparse.Namespace(
                    plan=plan_path,
                    discovery=discovery_path,
                    state=state_path,
                    proof=prepare_path,
                )
            )
        if rc != 0 or transition.load_state(state_path, plan)["phase"] != "shadow":
            fail("prepare command did not persist shadow state")

        verify_path = root / "verify-shadow.json"
        relay = {"name": "platform-app", "synced_at": transition.utc_now(), "verified": True}
        with (
            mock.patch("forge_transition.require_credentials"),
            mock.patch(
                "forge_transition.reconcile_plan",
                side_effect=lambda _plan, state, _work: ([relay], copy.deepcopy(state)),
            ),
            mock.patch(
                "forge_transition.verify_transition_repository",
                return_value={"inventory": simple_inventory(plan), "verified": True},
            ),
            mock.patch(
                "forge_transition.verify_source_authority",
                return_value={"verified": True},
            ),
        ):
            rc = transition.command_verify_shadow(
                argparse.Namespace(
                    plan=plan_path,
                    prepared=prepare_path,
                    state=state_path,
                    work_dir=root / "verify-work",
                    proof=verify_path,
                )
            )
        if rc != 0:
            fail("shadow verification command failed")

        current = transition.load_state(state_path, plan)
        current["repositories"] = {
            "platform-app": {"synced_at": transition.utc_now(), "verified": True}
        }
        transition.write_state(state_path, current)
        status_path = root / "status.json"
        with (
            mock.patch("forge_transition.require_credentials"),
            mock.patch(
                "forge_transition.verify_operational_repository",
                return_value={"verified": True},
            ),
        ):
            rc = transition.command_status(
                argparse.Namespace(plan=plan_path, state=state_path, proof=status_path)
            )
        if rc != 0:
            fail("status command rejected a healthy shadow relay")

        reconcile_path = root / "reconcile.json"
        with (
            mock.patch("forge_transition.require_credentials"),
            mock.patch(
                "forge_transition.reconcile_plan",
                side_effect=lambda _plan, state, _work: ([relay], copy.deepcopy(state)),
            ),
        ):
            rc = transition.command_reconcile(
                argparse.Namespace(
                    plan=plan_path,
                    state=state_path,
                    work_dir=root / "reconcile-work",
                    proof=reconcile_path,
                )
            )
        if rc != 0:
            fail("reconcile command rejected a healthy relay result")

        status = json.loads(status_path.read_text(encoding="utf-8"))
        rollback_environment = {
            "FORGE_TRANSITION_LIVE": "1",
            "FORGE_TRANSITION_ROLLBACK_CONFIRM": str(status["proof_sha256"]),
            "FORGE_TRANSITION_CHANGE_TICKET": "CHG-201",
        }
        rolled_back = transition.load_state(state_path, plan)
        rolled_back["phase"] = "rolled-back"
        with (
            mock.patch.dict(os.environ, rollback_environment, clear=True),
            mock.patch("forge_transition.require_credentials"),
            mock.patch(
                "forge_transition.rollback_transition_state",
                return_value=([{"name": "platform-app", "verified": True}], rolled_back),
            ),
        ):
            rc = transition.command_rollback(
                argparse.Namespace(
                    plan=plan_path,
                    state=state_path,
                    evidence=status_path,
                    proof=root / "rollback.json",
                )
            )
        if rc != 0:
            fail("manual rollback command rejected verified evidence")

        with (
            mock.patch("forge_transition.require_credentials"),
            mock.patch("forge_transition.discover_repository", return_value=simple_inventory(plan)),
        ):
            rc = transition.command_discover(
                argparse.Namespace(plan=plan_path, proof=root / "discover-command.json")
            )
        if rc != 0:
            fail("discover command rejected a verified inventory")
        if transition.command_validate(
            argparse.Namespace(plan=plan_path, proof=root / "validate.json")
        ) != 0:
            fail("validate command rejected a valid transition plan")

        parsed = transition.parse_args(["validate-plan", str(plan_path)])
        if parsed.func is not transition.command_validate:
            fail("transition CLI did not dispatch validate-plan")
        if transition.main(["validate-plan", str(plan_path)]) != 0:
            fail("transition CLI main rejected a valid plan")


def main() -> int:
    test_plan_contract()
    test_state_integrity_and_lock()
    test_github_secret_contract()
    test_source_ci_controls()
    test_enter_and_automatic_rollback()
    test_manual_fallback_keeps_relay()
    test_finalize_and_relay_failure_rollback()
    test_proof_integrity()
    test_validation_confirmation_and_proof_edges()
    test_github_inventory_and_destination_access()
    test_source_authority_relay_and_recovery_helpers()
    test_prepare_verify_and_command_surfaces()
    print("Forge transition self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
