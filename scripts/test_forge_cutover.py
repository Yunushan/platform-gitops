#!/usr/bin/env python3
"""Self-test the opt-in GitLab-to-Forgejo cutover orchestrator."""

from __future__ import annotations

import argparse
import copy
import io
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


def expect_action_error(action: object, expected: str) -> None:
    try:
        action()  # type: ignore[operator]
    except cutover.CutoverError as exc:
        if expected not in str(exc):
            fail(f"expected {expected!r} in {exc!r}")
    else:
        fail(f"expected cutover failure containing {expected!r}")


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


def test_service_http_and_content_helpers() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    saved = {
        name: os.environ.get(name)
        for name in ("SERVICE_TOKEN", "HARBOR_TEST_USER", "HARBOR_TEST_PASSWORD")
    }
    os.environ["SERVICE_TOKEN"] = "runtime-token"
    os.environ["HARBOR_TEST_USER"] = "admin"
    os.environ["HARBOR_TEST_PASSWORD"] = "secret"

    class Response:
        def __init__(self, status: int, payload: bytes) -> None:
            self.status = status
            self.payload = payload
            self.offset = 0

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            if self.offset >= len(self.payload):
                return b""
            end = len(self.payload) if size < 0 else self.offset + size
            chunk = self.payload[self.offset:end]
            self.offset += len(chunk)
            return chunk

    try:
        bearer = cutover.service_target(
            "woodpecker",
            {"api_url": "https://ci.example.invalid/", "token_env": "SERVICE_TOKEN"},
        )
        if cutover.service_headers(bearer)["Authorization"] != "Bearer runtime-token":
            fail("bearer service authentication header is incorrect")
        token = cutover.ServiceTarget(
            "forgejo-service",
            "https://forgejo.example.invalid",
            token_env="SERVICE_TOKEN",
            auth="token",
        )
        if cutover.service_headers(token)["Authorization"] != "token runtime-token":
            fail("token service authentication header is incorrect")
        basic = cutover.ServiceTarget(
            "harbor",
            "https://registry.example.invalid",
            username_env="HARBOR_TEST_USER",
            password_env="HARBOR_TEST_PASSWORD",
            auth="basic",
        )
        if cutover.service_headers(basic)["Authorization"] != "Basic YWRtaW46c2VjcmV0":
            fail("basic service authentication header is incorrect")
        expect_action_error(
            lambda: cutover.service_headers(
                cutover.ServiceTarget("invalid", "https://invalid.example", auth="digest")
            ),
            "unsupported auth mode",
        )

        with mock.patch(
            "forge_cutover.open_http_request",
            return_value=Response(201, b'{"created": true}'),
        ) as open_mock:
            status, payload = cutover.service_request(
                bearer,
                "POST",
                "/api/repos",
                body={"name": "platform-app"},
                query={"owner": ["platform", "team"]},
                expected=(201,),
                return_status=True,
            )
        if status != 201 or payload != {"created": True}:
            fail("service request did not decode a successful response")
        requested_url = open_mock.call_args.args[0].full_url
        if "owner=platform" not in requested_url or "owner=team" not in requested_url:
            fail(f"service request did not encode repeated query values: {requested_url}")

        expected_error = cutover.HTTPError(
            "https://ci.example.invalid/missing",
            404,
            "not found",
            {},
            io.BytesIO(b'{"message":"missing"}'),
        )
        with mock.patch("forge_cutover.open_http_request", side_effect=expected_error):
            status, payload = cutover.service_request(
                bearer,
                "GET",
                "missing",
                expected=(200, 404),
                return_status=True,
            )
        if status != 404 or payload.get("message") != "missing":
            fail("expected HTTP status was not returned safely")

        server_error = cutover.HTTPError(
            "https://ci.example.invalid/fail",
            500,
            "failed",
            {},
            io.BytesIO(b'{"message":"failed"}'),
        )
        with mock.patch("forge_cutover.open_http_request", side_effect=server_error):
            expect_action_error(
                lambda: cutover.service_request(bearer, "GET", "fail"),
                "HTTP 500",
            )
        with mock.patch(
            "forge_cutover.open_http_request",
            side_effect=cutover.URLError("offline"),
        ):
            expect_action_error(
                lambda: cutover.service_request(bearer, "GET", "offline"),
                "offline",
            )
        with mock.patch(
            "forge_cutover.open_http_request",
            return_value=Response(200, b"not-json"),
        ):
            expect_action_error(
                lambda: cutover.service_request(bearer, "GET", "invalid-json"),
                "invalid JSON",
            )

        pages = [[{"id": index} for index in range(100)], [{"id": 100}]]
        with mock.patch("forge_cutover.migration.api_request", side_effect=pages) as api_mock:
            items = cutover.gitlab_list(mock.sentinel.target, "projects/1/variables")
        if len(items) != 101 or api_mock.call_count != 2:
            fail("GitLab pagination did not consume every page")
        with mock.patch("forge_cutover.migration.api_request", return_value={}):
            expect_action_error(
                lambda: cutover.gitlab_list(mock.sentinel.target, "projects/1/variables"),
                "non-array",
            )

        encoded = cutover.base64.b64encode(b"stages: [test]\n").decode("ascii")
        with (
            mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/1")),
            mock.patch(
                "forge_cutover.migration.api_request",
                return_value={"encoding": "base64", "content": encoded},
            ),
        ):
            if cutover.gitlab_file_text(repo, ".gitlab-ci.yml", "main") != "stages: [test]\n":
                fail("GitLab file content was not decoded")
            if cutover.forgejo_file_text(repo, ".woodpecker.yml", "main") != "stages: [test]\n":
                fail("Forgejo file content was not decoded")
        with (
            mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/1")),
            mock.patch("forge_cutover.migration.api_request", return_value=[]),
        ):
            expect_action_error(
                lambda: cutover.gitlab_file_text(repo, ".gitlab-ci.yml", "main"),
                "invalid content",
            )
            expect_action_error(
                lambda: cutover.forgejo_file_text(repo, ".woodpecker.yml", "main"),
                "invalid content",
            )
        with (
            mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/1")),
            mock.patch(
                "forge_cutover.migration.api_request",
                return_value={"encoding": "base64", "content": "////"},
            ),
        ):
            expect_action_error(
                lambda: cutover.gitlab_file_text(repo, ".gitlab-ci.yml", "main"),
                "unreadable",
            )
            expect_action_error(
                lambda: cutover.forgejo_file_text(repo, ".woodpecker.yml", "main"),
                "unreadable",
            )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_destination_service_preparation_helpers() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    woodpecker = cutover.service_target("woodpecker", plan.services["woodpecker"])

    with mock.patch("forge_cutover.service_request", return_value=(404, {})):
        if cutover.woodpecker_lookup(woodpecker, "platform/platform-app", required=False) is not None:
            fail("optional absent Woodpecker repository was not reported as absent")
        expect_action_error(
            lambda: cutover.woodpecker_lookup(woodpecker, "platform/platform-app"),
            "not active",
        )
    with mock.patch("forge_cutover.service_request", return_value=(200, [])):
        expect_action_error(
            lambda: cutover.woodpecker_lookup(woodpecker, "platform/platform-app"),
            "invalid data",
        )

    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[(404, {}), {}, (200, {})],
    ):
        created_secret = cutover.woodpecker_secret_upsert(
            woodpecker,
            30,
            "DEPLOY_KEY",
            "runtime-secret",
        )
    if created_secret != {"name": "DEPLOY_KEY", "action": "created", "verified": True}:
        fail(f"Woodpecker secret create result is incorrect: {created_secret}")
    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[(200, {"name": "DEPLOY_KEY"}), {}, (200, {})],
    ):
        updated_secret = cutover.woodpecker_secret_upsert(
            woodpecker,
            30,
            "DEPLOY_KEY",
            "rotated-secret",
        )
    if updated_secret["action"] != "updated" or not updated_secret["verified"]:
        fail("Woodpecker secret update did not verify")

    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[[], {"id": 41}],
    ):
        created_cron = cutover.woodpecker_cron_upsert(
            woodpecker,
            30,
            "nightly",
            "0 2 * * *",
            "main",
            False,
        )
    if created_cron["action"] != "created" or created_cron["id"] != 41:
        fail("Woodpecker cron was not created")
    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[[{"id": 42, "name": "nightly"}], {"id": 42}],
    ):
        updated_cron = cutover.woodpecker_cron_upsert(
            woodpecker,
            30,
            "nightly",
            "0 3 * * *",
            "main",
            True,
        )
    if updated_cron["action"] != "updated" or not updated_cron["enabled"]:
        fail("Woodpecker cron was not updated")
    with mock.patch("forge_cutover.service_request", return_value={}):
        expect_action_error(
            lambda: cutover.woodpecker_cron_upsert(
                woodpecker,
                30,
                "nightly",
                "0 3 * * *",
                "main",
                True,
            ),
            "cron list returned invalid data",
        )

    saved = {
        name: os.environ.get(name)
        for name in ("HARBOR_USERNAME", "HARBOR_PASSWORD")
    }
    os.environ["HARBOR_USERNAME"] = "admin"
    os.environ["HARBOR_PASSWORD"] = "runtime-password"

    def harbor_create_request(target, method, path, **_kwargs):
        if target.name == "harbor" and method == "GET" and path.endswith("projects/platform"):
            if harbor_create_request.seen:
                return {"project_id": 50}
            harbor_create_request.seen = True
            return 404, {}
        if target.name == "woodpecker" and method == "GET" and "/registries/" in path:
            return 404, {}
        return {}

    harbor_create_request.seen = False
    try:
        with mock.patch("forge_cutover.service_request", side_effect=harbor_create_request):
            harbor = cutover.prepare_harbor(plan, woodpecker, 30)
        if harbor["project_action"] != "created" or harbor["registry_action"] != "created":
            fail(f"managed Harbor preparation did not create missing state: {harbor}")

        def harbor_update_request(target, method, path, **_kwargs):
            if target.name == "harbor" and method == "GET":
                return (200, {"project_id": 50}) if _kwargs.get("return_status") else {"project_id": 50}
            if target.name == "woodpecker" and method == "GET" and "/registries/" in path:
                return 200, {"address": "registry.example.invalid"}
            return {}

        with mock.patch("forge_cutover.service_request", side_effect=harbor_update_request):
            harbor = cutover.prepare_harbor(plan, woodpecker, 30)
        if harbor["project_action"] != "existing" or harbor["registry_action"] != "updated":
            fail(f"managed Harbor preparation did not update existing state: {harbor}")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    manual = parsed_plan()
    manual.services["harbor"]["mode"] = "manual"
    if cutover.prepare_harbor(manual, woodpecker, 30) != {"mode": "manual", "verified": True}:
        fail("manual Harbor preparation was not accepted without mutation")

    healthy_app = {
        "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
        "spec": {
            "source": {
                "repoURL": "https://forgejo.example.invalid/platform/gitops.git",
            }
        },
    }
    with mock.patch("forge_cutover.service_request", return_value=healthy_app):
        argocd = cutover.verify_argocd(plan)
    if not argocd["verified"]:
        fail(f"healthy Argo CD application did not verify: {argocd}")
    mismatched_app = copy.deepcopy(healthy_app)
    mismatched_app["spec"]["source"]["repoURL"] = "https://wrong.example.invalid/repo.git"
    with mock.patch("forge_cutover.service_request", return_value=mismatched_app):
        if cutover.verify_argocd(plan)["verified"]:
            fail("Argo CD repository mismatch was accepted")
    manual.services["argocd"]["mode"] = "skipped"
    if not cutover.verify_argocd(manual)["verified"]:
        fail("explicitly skipped Argo CD verification was not accepted")

    protection_calls: list[tuple[str, str]] = []

    def create_protection(_target, method, path, **_kwargs):
        protection_calls.append((method, path))
        if method == "GET":
            return [] if len(protection_calls) == 1 else [{"rule_name": "main"}]
        return {}

    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch("forge_cutover.migration.api_request", side_effect=create_protection),
    ):
        protections = cutover.prepare_destination_protections(repo)
    if protections["mappings"][0]["action"] != "created" or not protections["verified"]:
        fail(f"Forgejo branch protection was not created and verified: {protections}")


def test_operational_verification_helpers() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    woodpecker = cutover.service_target("woodpecker", plan.services["woodpecker"])

    runner = cutover.verify_runner_capabilities(
        repo,
        [
            {"name": "disabled", "custom_labels": {"platform": "linux"}, "no_schedule": True},
            {"name": "linux-1", "custom_labels": {"platform": "linux"}},
        ],
    )
    if not runner["verified"] or runner["mappings"][0]["matching_agents"] != ["linux-1"]:
        fail(f"matching Woodpecker runner was not verified: {runner}")
    if cutover.verify_runner_capabilities(repo, [])["verified"]:
        fail("missing Woodpecker runner capability was accepted")
    expect_action_error(
        lambda: cutover.expected_woodpecker_labels({"target_labels": ["linux"]}),
        "target_labels must be an object",
    )
    manual_runner = parsed_plan()
    manual_runner.repositories[0].cutover["runner_tags"]["mappings"][0] = {
        "source": "linux",
        "mode": "manual",
        "accepted": True,
        "reason": "Verified by the approved transition operator.",
    }
    if not cutover.verify_runner_capabilities(manual_runner.repositories[0], [])["verified"]:
        fail("accepted manual runner mapping did not verify")

    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch(
            "forge_cutover.migration.api_request",
            return_value=[{"rule_name": "main"}],
        ),
    ):
        if not cutover.verify_destination_protections(repo)["verified"]:
            fail("present Forgejo branch protection did not verify")
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch("forge_cutover.migration.api_request", return_value=[]),
    ):
        if cutover.verify_destination_protections(repo)["verified"]:
            fail("missing Forgejo branch protection was accepted")
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch("forge_cutover.migration.api_request", return_value={}),
    ):
        expect_action_error(
            lambda: cutover.verify_destination_protections(repo),
            "branch protection list returned invalid data",
        )

    hooks = [{"config": {"url": "https://ci.example.invalid/hook"}}]
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch("forge_cutover.migration.api_request", return_value=hooks),
    ):
        integrations = cutover.verify_destination_integrations(repo, "https://ci.example.invalid")
    if not integrations["verified"] or integrations["hook_hosts"] != ["ci.example.invalid"]:
        fail(f"Woodpecker webhook integration did not verify: {integrations}")
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch("forge_cutover.migration.api_request", return_value=[]),
    ):
        if cutover.verify_destination_integrations(repo, "https://ci.example.invalid")["verified"]:
            fail("missing Woodpecker webhook was accepted")
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch("forge_cutover.migration.api_request", return_value={}),
    ):
        expect_action_error(
            lambda: cutover.verify_destination_integrations(repo, "https://ci.example.invalid"),
            "hooks list returned invalid data",
        )

    shadow_inventory = [
        {"name": "FORGE_CUTOVER_DEPLOYMENT_ENABLED"},
        {"name": "DEPLOY_KEY"},
    ]
    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[shadow_inventory, [{"name": "nightly", "enabled": False}]],
    ):
        shadow = cutover.verify_woodpecker_configuration(plan, repo, woodpecker, 30, "shadow")
    if not shadow["verified"]:
        fail(f"valid shadow Woodpecker configuration did not verify: {shadow}")
    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[shadow_inventory, [{"name": "nightly", "enabled": True}]],
    ):
        active = cutover.verify_woodpecker_configuration(
            plan,
            repo,
            woodpecker,
            30,
            "post-cutover",
        )
    if not active["verified"]:
        fail(f"valid active Woodpecker configuration did not verify: {active}")
    with mock.patch(
        "forge_cutover.service_request",
        side_effect=[[{"name": "FORGE_CUTOVER_DEPLOYMENT_ENABLED"}], []],
    ):
        missing = cutover.verify_woodpecker_configuration(plan, repo, woodpecker, 30, "shadow")
    if missing["verified"] or missing["missing_secret_names"] != ["DEPLOY_KEY"]:
        fail(f"missing Woodpecker secret was accepted: {missing}")
    with mock.patch("forge_cutover.service_request", side_effect=[{}, []]):
        expect_action_error(
            lambda: cutover.verify_woodpecker_configuration(plan, repo, woodpecker, 30, "shadow"),
            "secret or cron inventory returned invalid data",
        )

    with (
        mock.patch(
            "forge_cutover.service_request",
            side_effect=[
                {"number": 91},
                {"status": "running"},
                {"status": "success", "commit": "abc123"},
            ],
        ),
        mock.patch("forge_cutover.time.monotonic", side_effect=[0.0, 1.0, 2.0]),
        mock.patch("forge_cutover.time.sleep"),
    ):
        canary = cutover.trigger_woodpecker_canary(woodpecker, 30, "main", 30, "shadow")
    if not canary["verified"] or canary["commit"] != "abc123":
        fail(f"successful Woodpecker canary did not verify: {canary}")
    with (
        mock.patch(
            "forge_cutover.service_request",
            side_effect=[{"id": 92}, {"status": "failure"}],
        ),
        mock.patch("forge_cutover.time.monotonic", side_effect=[0.0, 1.0]),
    ):
        failed_canary = cutover.trigger_woodpecker_canary(
            woodpecker,
            30,
            "main",
            30,
            "post-cutover",
        )
    if failed_canary["verified"] or failed_canary["status"] != "failure":
        fail("failed Woodpecker canary was accepted")
    with mock.patch("forge_cutover.service_request", return_value={}):
        expect_action_error(
            lambda: cutover.trigger_woodpecker_canary(woodpecker, 30, "main", 30, "shadow"),
            "did not return a pipeline number",
        )
    with (
        mock.patch("forge_cutover.service_request", return_value={"number": 93}),
        mock.patch("forge_cutover.time.monotonic", side_effect=[0.0, 31.0]),
    ):
        timed_out = cutover.trigger_woodpecker_canary(woodpecker, 30, "main", 30, "shadow")
    if timed_out["status"] != "timeout" or timed_out["verified"]:
        fail("Woodpecker canary timeout was not fail-closed")

    with mock.patch(
        "forge_cutover.service_request",
        return_value={"digest": "sha256:" + ("a" * 64)},
    ):
        harbor = cutover.verify_harbor_canary(plan)
    if not harbor["verified"] or not harbor["digest"].startswith("sha256:"):
        fail(f"Harbor canary artifact did not verify: {harbor}")
    with mock.patch("forge_cutover.service_request", return_value={}):
        if cutover.verify_harbor_canary(plan)["verified"]:
            fail("Harbor canary without a digest was accepted")
    manual_harbor = parsed_plan()
    manual_harbor.services["harbor"]["mode"] = "manual"
    if not cutover.verify_harbor_canary(manual_harbor)["verified"]:
        fail("manual Harbor canary was not treated as explicitly not configured")
    invalid_harbor = parsed_plan()
    invalid_harbor.services["harbor"]["canary"] = "invalid"  # type: ignore[assignment]
    expect_action_error(
        lambda: cutover.verify_harbor_canary(invalid_harbor),
        "canary must be an object",
    )

    inventory = {
        "verified": True,
        "pipelines": {"verified": True},
        "variables": {"verified": True},
    }
    with (
        mock.patch("forge_cutover.discover_repository", return_value=inventory),
        mock.patch("forge_cutover.migration.verify_repo", return_value={"verified": True}),
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch(
            "forge_cutover.migration.api_request",
            return_value={"full_name": "platform/platform-app", "default_branch": "main"},
        ),
        mock.patch("forge_cutover.woodpecker_lookup", return_value={"id": 30}),
        mock.patch(
            "forge_cutover.service_request",
            return_value=[{"name": "linux-1", "custom_labels": {"platform": "linux"}}],
        ),
        mock.patch("forge_cutover.verify_runner_capabilities", return_value={"verified": True}),
        mock.patch("forge_cutover.verify_woodpecker_configuration", return_value={"verified": True}),
        mock.patch("forge_cutover.verify_destination_protections", return_value={"verified": True}),
        mock.patch("forge_cutover.verify_destination_integrations", return_value={"verified": True}),
        mock.patch("forge_cutover.trigger_woodpecker_canary", return_value={"verified": True}),
        mock.patch("forge_cutover.verify_harbor_canary", return_value={"verified": True}),
        mock.patch("forge_cutover.verify_argocd", return_value={"verified": True}),
    ):
        verified_repo = cutover.verify_repository(
            plan,
            repo,
            {"name": "platform-app"},
            "shadow",
        )
    if not verified_repo["verified"] or verified_repo["prepared_repository"] != "platform-app":
        fail(f"complete repository verification did not pass: {verified_repo}")


def test_gitlab_freeze_restore_and_authority_helpers() -> None:
    plan = parsed_plan()
    repo = plan.repositories[0]
    project = {
        "id": 10,
        "archived": False,
        "builds_access_level": "enabled",
    }
    current = {
        "id": 10,
        "archived": True,
        "builds_access_level": "disabled",
    }
    snapshots: list[dict[str, object]] = []
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/10")),
        mock.patch(
            "forge_cutover.gitlab_list",
            return_value=[{"id": 31, "active": True}],
        ),
        mock.patch(
            "forge_cutover.gitlab_active_pipelines",
            side_effect=[[{"id": 41, "status": "running"}], []],
        ),
        mock.patch(
            "forge_cutover.migration.api_request",
            side_effect=[project, {}, {}, {}, {}, current],
        ) as api_mock,
    ):
        snapshot = cutover.freeze_gitlab(
            repo,
            True,
            lambda value: snapshots.append(copy.deepcopy(value)),
        )
    if snapshot["archived"] or snapshot["builds_access_level"] != "enabled":
        fail(f"GitLab freeze snapshot lost rollback state: {snapshot}")
    if snapshots != [snapshot]:
        fail("GitLab rollback snapshot was not persisted before mutation")
    paths = [call.args[2] for call in api_mock.call_args_list]
    if not any(path.endswith("/cancel") for path in paths) or not any(path.endswith("/archive") for path in paths):
        fail(f"GitLab freeze did not cancel pipelines and archive the source: {paths}")

    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/10")),
        mock.patch("forge_cutover.gitlab_list", return_value=[]),
        mock.patch(
            "forge_cutover.gitlab_active_pipelines",
            return_value=[{"id": 42, "status": "running"}],
        ),
        mock.patch("forge_cutover.migration.api_request", return_value=project),
    ):
        expect_action_error(
            lambda: cutover.freeze_gitlab(repo, False),
            "active GitLab pipelines block cutover",
        )

    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/10")),
        mock.patch("forge_cutover.gitlab_list", return_value=[]),
        mock.patch("forge_cutover.gitlab_active_pipelines", return_value=[]),
        mock.patch(
            "forge_cutover.migration.api_request",
            side_effect=[project, {}, {}, {**current, "archived": False}],
        ),
    ):
        expect_action_error(
            lambda: cutover.freeze_gitlab(repo, False),
            "did not become archived",
        )

    rollback_snapshot = {
        "archived": False,
        "builds_access_level": "enabled",
        "schedules": [{"id": 31, "active": True}],
    }
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "projects/10")),
        mock.patch(
            "forge_cutover.migration.api_request",
            side_effect=[current, {}, {}, {}, project],
        ),
    ):
        restored = cutover.restore_gitlab(repo, rollback_snapshot)
    if not restored["verified"] or restored["archived"]:
        fail(f"GitLab source rollback did not restore authority: {restored}")

    with mock.patch(
        "forge_cutover.gitlab_list",
        return_value=[
            {"id": 1, "status": "success"},
            {"id": 2, "status": "running"},
            {"id": 3, "status": "pending"},
        ],
    ):
        active = cutover.gitlab_active_pipelines(repo)
    if [item["id"] for item in active] != [2, 3]:
        fail(f"active GitLab pipeline filtering is incorrect: {active}")

    authority_calls: list[tuple[str, object]] = []
    with (
        mock.patch("forge_cutover.repo_base", return_value=(mock.sentinel.target, "repos/platform/app")),
        mock.patch(
            "forge_cutover.migration.api_request",
            return_value={"full_name": "platform/platform-app"},
        ),
        mock.patch("forge_cutover.woodpecker_lookup", return_value={"id": 30}),
        mock.patch(
            "forge_cutover.woodpecker_secret_upsert",
            side_effect=lambda _target, _repo_id, _name, value, **_kwargs: authority_calls.append(
                ("gate", value)
            )
            or {"verified": True},
        ),
        mock.patch(
            "forge_cutover.woodpecker_cron_upsert",
            side_effect=lambda _target, _repo_id, name, _schedule, _branch, enabled: authority_calls.append(
                (name, enabled)
            )
            or {"verified": True},
        ),
    ):
        authority = cutover.set_destination_authority(plan, repo, True)
    if not authority["verified"] or authority_calls != [("gate", "true"), ("nightly", True)]:
        fail(f"destination authority was not enabled atomically: {authority_calls}")

    proof = {"proof_sha256": "rollback-proof"}
    env_names = [
        "FORGE_CUTOVER_LIVE",
        "FORGE_CUTOVER_ROLLBACK_CONFIRM",
        "FORGE_CUTOVER_CHANGE_TICKET",
    ]
    saved = {name: os.environ.get(name) for name in env_names}
    try:
        for name in env_names:
            os.environ.pop(name, None)
        expect_action_error(
            lambda: cutover.require_rollback_confirmation(plan, proof),
            "live rollback is disabled",
        )
        os.environ["FORGE_CUTOVER_LIVE"] = "1"
        expect_action_error(
            lambda: cutover.require_rollback_confirmation(plan, proof),
            "rollback approval mismatch",
        )
        os.environ["FORGE_CUTOVER_ROLLBACK_CONFIRM"] = "rollback-proof"
        expect_action_error(
            lambda: cutover.require_rollback_confirmation(plan, proof),
            "required for live rollback evidence",
        )
        os.environ["FORGE_CUTOVER_CHANGE_TICKET"] = "CHG-1234"
        cutover.require_rollback_confirmation(plan, proof)
        expect_action_error(
            lambda: cutover.proof_age_seconds({"generated_at": "invalid"}),
            "generated_at is invalid",
        )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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


def test_pipeline_conversion_accounting() -> None:
    raw_plan = copy.deepcopy(base_plan())
    raw_plan["repositories"][0]["cutover"]["pipelines"]["conversion"] = {  # type: ignore[index]
        "mode": "managed",
        "output": ".woodpecker.yml",
        "provider": "gitlab",
        "deployment_jobs": [],
    }
    plan = cutover.parse_cutover_plan(raw_plan)
    repo = plan.repositories[0]
    source_text = "build:\n  tags: [linux]\n  script: echo converted\n"
    expected, expected_report = cutover.pipeline.convert_pipeline(
        "gitlab",
        source_text,
        ".gitlab-ci.yml",
        {
            "deployment_gate_marker": "FORGE_CUTOVER_DEPLOYMENT_ENABLED",
            "default_image": cutover.pipeline.DEFAULT_IMAGE,
            "secret_names": [],
            "deployment_jobs": [],
            "runner_labels": {"linux": {"platform": "linux"}},
            "schedule_mappings": {"0 2 * * *": "nightly"},
        },
    )
    if not expected_report["supported"]:
        fail(f"conversion fixture unexpectedly failed: {expected_report}")
    source = [
        {
            "path": ".gitlab-ci.yml",
            "sha": "source-sha",
            "_content": source_text,
            "external_includes": [],
        }
    ]
    destination = [{"path": ".woodpecker.yml", "sha": "destination-sha"}]
    with mock.patch("forge_cutover.forgejo_file_text", return_value=expected):
        result = cutover.account_pipeline_files(repo, source, destination, "main")
    conversion = result.get("conversion") or {}
    if result["verified"] is not True or conversion.get("verified") is not True:
        fail(f"converted pipeline did not verify: {result}")
    public_source = result["source_files"][0]
    if "_content" in public_source:
        fail("pipeline source content leaked into migration proof")


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
        os.environ["FORGE_CUTOVER_CONFIRM"] = "verification-digest"
        os.environ.pop("FORGE_CUTOVER_CHANGE_TICKET", None)
        expect_action_error(
            lambda: cutover.require_activation_confirmation(plan, proof),
            "required for live cutover evidence",
        )
        os.environ["FORGE_CUTOVER_CHANGE_TICKET"] = "CHG-1234"
        stale = {**proof, "generated_at": "2000-01-01T00:00:00Z"}
        expect_action_error(
            lambda: cutover.require_activation_confirmation(plan, stale),
            "verification proof is stale",
        )
        cutover.require_activation_confirmation(plan, proof)
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


def test_command_and_cli_surfaces() -> None:
    plan = parsed_plan()
    repo_name = plan.repositories[0].migration.name
    inventory = {
        "name": repo_name,
        "source_state": {"archived": False, "builds_access_level": "enabled"},
        "pipelines": {"verified": True},
        "variables": {"verified": True},
        "verified": True,
    }
    with tempfile.TemporaryDirectory(prefix="forge-cutover-command-") as temp:
        root = Path(temp)
        plan_path = root / "plan.json"

        validate_args = argparse.Namespace(plan=plan_path, proof=root / "validate.json")
        with mock.patch("forge_cutover.load_cutover_plan", return_value=plan):
            if cutover.command_validate(validate_args) != 0:
                fail("validate-plan command returned nonzero")
        validated = json.loads(validate_args.proof.read_text(encoding="utf-8"))
        if validated["command"] != "validate-plan" or not validated["verified"]:
            fail("validate-plan command did not write verified evidence")

        discover_args = argparse.Namespace(plan=plan_path, proof=root / "discover.json")
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.discover_repository", return_value=inventory),
        ):
            if cutover.command_discover(discover_args) != 0:
                fail("discover command returned nonzero for verified inventory")
        discovered = json.loads(discover_args.proof.read_text(encoding="utf-8"))
        if discovered["command"] != "discover" or not discovered["verified"]:
            fail("discover command did not write verified evidence")
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch(
                "forge_cutover.discover_repository",
                side_effect=cutover.migration.MigrationError("provider unavailable"),
            ),
        ):
            if cutover.command_discover(discover_args) != 1:
                fail("discover command accepted a provider failure")

        discovery = cutover.proof_base(plan, "discover", [inventory])
        discovery["proof_sha256"] = cutover.migration.proof_digest(discovery)
        prepare_args = argparse.Namespace(
            plan=plan_path,
            discovery=root / "discover.json",
            proof=root / "prepare.json",
        )
        prepare_env = str(plan.activation["prepare_confirmation_env"])
        previous_prepare = os.environ.get(prepare_env)
        os.environ[prepare_env] = str(discovery["proof_sha256"])
        try:
            with (
                mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
                mock.patch("forge_cutover.require_provider_credentials"),
                mock.patch("forge_cutover.load_verified_proof", return_value=discovery),
                mock.patch("forge_cutover.discover_repository", return_value=inventory),
                mock.patch(
                    "forge_cutover.prepare_repository",
                    return_value={"name": repo_name, "verified": True},
                ),
            ):
                if cutover.command_prepare(prepare_args) != 0:
                    fail("prepare command returned nonzero for stable approved inventory")
            prepared_output = json.loads(prepare_args.proof.read_text(encoding="utf-8"))
            if not prepared_output["verified"] or not prepared_output["repositories"][0].get(
                "approved_inventory_sha256"
            ):
                fail("prepare command omitted approved inventory binding")

            with (
                mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
                mock.patch("forge_cutover.require_provider_credentials"),
                mock.patch("forge_cutover.load_verified_proof", return_value=discovery),
                mock.patch("forge_cutover.discover_repository", return_value=inventory),
                mock.patch(
                    "forge_cutover.prepare_repository",
                    side_effect=cutover.migration.MigrationError("preparation failed"),
                ),
            ):
                if cutover.command_prepare(prepare_args) != 1:
                    fail("prepare command accepted a provider mutation failure")
        finally:
            if previous_prepare is None:
                os.environ.pop(prepare_env, None)
            else:
                os.environ[prepare_env] = previous_prepare

        prepared = cutover.proof_base(
            plan,
            "prepare",
            [{"name": repo_name, "verified": True}],
        )
        prepared["proof_sha256"] = cutover.migration.proof_digest(prepared)
        verify_args = argparse.Namespace(
            plan=plan_path,
            prepared=root / "prepare.json",
            proof=root / "verify.json",
        )
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.load_verified_proof", return_value=prepared),
            mock.patch(
                "forge_cutover.verify_repository",
                return_value={"name": repo_name, "verified": True},
            ),
        ):
            if cutover.command_verify(verify_args) != 0:
                fail("verify command returned nonzero for a healthy shadow deployment")
        verified_output = json.loads(verify_args.proof.read_text(encoding="utf-8"))
        if not verified_output["verified"] or verified_output["prepare_proof_sha256"] != prepared[
            "proof_sha256"
        ]:
            fail("verify command did not bind its preparation proof")
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.load_verified_proof", return_value=prepared),
            mock.patch(
                "forge_cutover.verify_repository",
                side_effect=cutover.migration.MigrationError("verification failed"),
            ),
        ):
            if cutover.command_verify(verify_args) != 1:
                fail("verify command accepted a provider verification failure")

        proof_path = root / "accepted-proof.json"
        written = cutover.write_proof(
            proof_path,
            cutover.proof_base(plan, "verify", [{"name": repo_name, "verified": True}]),
        )
        if cutover.command_verify_proof(argparse.Namespace(proof_file=proof_path)) != 0:
            fail("valid cutover proof was rejected")
        tampered = json.loads(proof_path.read_text(encoding="utf-8"))
        tampered["direction"] = "tampered"
        proof_path.write_text(json.dumps(tampered), encoding="utf-8")
        if cutover.command_verify_proof(argparse.Namespace(proof_file=proof_path)) != 1:
            fail("tampered cutover proof was accepted")
        if not written["proof_sha256"]:
            fail("proof writer omitted its integrity digest")

        failed_activation = cutover.proof_base(
            plan,
            "activate",
            [{"name": repo_name, "verified": False}],
        )
        failed_activation["proof_sha256"] = cutover.migration.proof_digest(failed_activation)
        rollback_args = argparse.Namespace(
            plan=plan_path,
            activation=root / "activation.json",
            proof=root / "rollback.json",
        )
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.load_integrity_proof", return_value=failed_activation),
        ):
            expect_action_error(
                lambda: cutover.command_rollback(rollback_args),
                "failed activation proof cannot drive manual rollback",
            )

        checkpoint = cutover.proof_base(
            plan,
            "activation-checkpoint",
            [
                {
                    "name": repo_name,
                    "source_before": None,
                    "destination_authority_attempted": False,
                    "verified": True,
                }
            ],
        )
        checkpoint["verified"] = False
        checkpoint["proof_sha256"] = cutover.migration.proof_digest(checkpoint)
        with (
            mock.patch("forge_cutover.load_cutover_plan", return_value=plan),
            mock.patch("forge_cutover.require_provider_credentials"),
            mock.patch("forge_cutover.load_integrity_proof", return_value=checkpoint),
            mock.patch("forge_cutover.require_rollback_confirmation"),
        ):
            if cutover.command_rollback(rollback_args) != 0:
                fail("checkpoint rollback did not accept explicitly unattempted mutations")
        skipped_rollback = json.loads(rollback_args.proof.read_text(encoding="utf-8"))
        if not skipped_rollback["verified"]:
            fail("safe no-op rollback did not verify")

    cli_cases = (
        (["validate-plan", "plan.json"], cutover.command_validate),
        (["discover", "plan.json", "--proof", "discover.json"], cutover.command_discover),
        (
            ["prepare", "plan.json", "--discovery", "discover.json", "--proof", "prepare.json"],
            cutover.command_prepare,
        ),
        (
            ["verify", "plan.json", "--prepared", "prepare.json", "--proof", "verify.json"],
            cutover.command_verify,
        ),
        (
            [
                "activate",
                "plan.json",
                "--verification",
                "verify.json",
                "--proof",
                "activate.json",
                "--checkpoint",
                "checkpoint.json",
            ],
            cutover.command_activate,
        ),
        (
            ["rollback", "plan.json", "--activation", "activate.json", "--proof", "rollback.json"],
            cutover.command_rollback,
        ),
        (["verify-proof", "proof.json"], cutover.command_verify_proof),
    )
    for argv, expected_handler in cli_cases:
        parsed = cutover.parse_args(argv)
        if parsed.handler is not expected_handler:
            fail(f"CLI command did not select {expected_handler.__name__}: {argv}")

    with mock.patch(
        "forge_cutover.parse_args",
        return_value=argparse.Namespace(handler=lambda _args: 0),
    ):
        if cutover.main([]) != 0:
            fail("top-level cutover dispatch changed a successful result")

    def fail_handler(_args: argparse.Namespace) -> int:
        raise cutover.migration.MigrationError("provider failed")

    with mock.patch(
        "forge_cutover.parse_args",
        return_value=argparse.Namespace(handler=fail_handler),
    ):
        if cutover.main([]) != 1:
            fail("top-level cutover dispatch did not fail closed on provider error")


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
    test_service_http_and_content_helpers()
    test_destination_service_preparation_helpers()
    test_operational_verification_helpers()
    test_gitlab_freeze_restore_and_authority_helpers()
    test_pipeline_accounting_and_gate()
    test_pipeline_conversion_accounting()
    test_external_include_detection()
    test_discovery_accounts_every_surface()
    test_gitlab_variable_scope_inventory()
    test_proof_integrity_and_redaction()
    test_shadow_prepare_keeps_destination_dormant()
    test_activation_confirmation_is_fail_closed()
    test_prepare_rejects_inventory_changed_after_approval()
    test_activation_sequence_and_automatic_rollback()
    test_manual_rollback_from_checkpoint()
    test_command_and_cli_surfaces()
    test_example_and_dormant_contract()
    print("Forge cutover orchestrator self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
