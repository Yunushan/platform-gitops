#!/usr/bin/env python3
"""Self-test the fail-closed GitLab/GitHub to Woodpecker converter."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import tempfile

import yaml


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_pipeline as pipeline


def fail(message: str) -> None:
    raise AssertionError(message)


def test_gitlab_conversion() -> None:
    source = """
stages: [build, test, deploy]
default:
  tags: [kubernetes]
variables:
  PUBLIC_MODE: "safe"
  DEPLOY_TOKEN: "$DEPLOY_TOKEN"
build:
  stage: build
  image: python:3.12
  script:
    - python -m compileall .
test:
  stage: test
  needs: [build]
  script: pytest -q
deploy:
  stage: deploy
  script:
    - kubectl apply -f deploy.yaml
"""
    rendered, report = pipeline.convert_pipeline(
        "gitlab",
        source,
        ".gitlab-ci.yml",
        {
            "deployment_gate_marker": "FORGE_GATE",
            "secret_names": ["DEPLOY_TOKEN"],
            "deployment_jobs": ["deploy"],
            "runner_labels": {"kubernetes": {"platform": "linux/amd64"}},
        },
    )
    if not report["supported"]:
        fail(f"supported GitLab fixture was rejected: {json.dumps(report)}")
    for expected in (
        "FORGE_GATE",
        "from_secret: \"DEPLOY_TOKEN\"",
        "depends_on:",
        "platform: \"linux/amd64\"",
        "kubectl apply",
    ):
        if expected not in rendered:
            fail(f"GitLab conversion omitted {expected!r}:\n{rendered}")
    if "safe" not in rendered:
        fail("public GitLab variable was not retained")
    if "DEPLOY_TOKEN" not in report["required_secrets"]:
        fail("GitLab secret reference was not recorded")


def test_github_conversion() -> None:
    source = """
name: CI
on:
  push:
    branches: [main]
env:
  REGISTRY_TOKEN: ${{ secrets.REGISTRY_TOKEN }}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm test
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: helm upgrade --install app ./chart
"""
    rendered, report = pipeline.convert_pipeline(
        "github",
        source,
        ".github/workflows/ci.yml",
        {
            "deployment_gate_marker": "FORGE_GATE",
            "secret_names": ["REGISTRY_TOKEN"],
            "deployment_jobs": ["deploy"],
            "runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}},
        },
    )
    if not report["supported"]:
        fail(f"supported GitHub fixture was rejected: {json.dumps(report)}")
    for expected in (
        "event: \"push\"",
        "image: \"node:20\"",
        "from_secret: \"REGISTRY_TOKEN\"",
        "depends_on:",
        "helm upgrade",
    ):
        if expected not in rendered:
            fail(f"GitHub conversion omitted {expected!r}:\n{rendered}")


def test_secret_values_never_render() -> None:
    source = """
build:
  script: echo "$DEPLOY_PASSWORD"
variables:
  DEPLOY_PASSWORD: super-secret-value
"""
    rendered, report = pipeline.convert_pipeline(
        "gitlab",
        source,
        ".gitlab-ci.yml",
        {"secret_names": ["DEPLOY_PASSWORD"], "deployment_jobs": []},
    )
    if not report["supported"]:
        fail("secret-only fixture should be convertible")
    if "super-secret-value" in rendered or "super-secret-value" in json.dumps(report):
        fail("secret value leaked into converter output or report")
    if "from_secret: \"DEPLOY_PASSWORD\"" not in rendered:
        fail("secret variable was not converted to a Woodpecker secret reference")

    github = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo \"${{ secrets.REGISTRY_TOKEN }}\"
"""
    rendered, report = pipeline.convert_pipeline(
        "github",
        github,
        ".github/workflows/ci.yml",
        {"runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}}},
    )
    if not report["supported"] or "from_secret: \"REGISTRY_TOKEN\"" not in rendered:
        fail(f"GitHub secret expression was not converted: {json.dumps(report)}\n{rendered}")


def test_unsupported_features_fail_closed() -> None:
    github = """
jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: [3.11, 3.12]
    steps:
      - uses: docker/build-push-action@v6
"""
    _, report = pipeline.convert_pipeline(
        "github",
        github,
        ".github/workflows/ci.yml",
        {"runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}}},
    )
    codes = {item["code"] for item in report["unsupported"]}
    if not {"github-strategy", "github-action"}.issubset(codes):
        fail(f"unsupported GitHub features were not blocked: {codes}")

    gitlab = """
deploy:
  script: ./deploy.sh
  artifacts:
    paths: [dist]
"""
    _, report = pipeline.convert_pipeline("gitlab", gitlab, ".gitlab-ci.yml", {})
    codes = {item["code"] for item in report["unsupported"]}
    if "gitlab-artifacts" not in codes or "deployment-job-unmapped" not in codes:
        fail(f"unsupported GitLab features were not blocked: {codes}")

    permissions = """
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
"""
    _, report = pipeline.convert_pipeline(
        "github",
        permissions,
        ".github/workflows/ci.yml",
        {"runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}}},
    )
    if "github-workflow-permissions" not in {item["code"] for item in report["unsupported"]}:
        fail("GitHub workflow permissions were not blocked")


def test_github_step_scope_and_filters() -> None:
    source = """
on:
  push:
    branches: [main]
    paths: [src/**]
  pull_request:
    branches: [main]
    paths: [src/**]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: scoped
        run: echo scoped
        env:
          ONLY_THIS_STEP: yes
        if: github.ref == 'refs/heads/main'
        working-directory: src
      - name: unscoped
        run: echo unscoped
"""
    rendered, report = pipeline.convert_pipeline(
        "github",
        source,
        ".github/workflows/ci.yml",
        {"runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}}},
    )
    if not report["supported"]:
        fail(f"scoped GitHub fixture was rejected: {json.dumps(report)}")
    document = yaml.safe_load(rendered)
    if document["when"] != {"branch": ["main"], "event": ["push", "pull_request"], "path": ["src/**"]}:
        fail(f"GitHub trigger filters were not preserved: {document['when']}")
    steps = document["steps"]
    if steps[0]["environment"]["ONLY_THIS_STEP"] != "yes" or "ONLY_THIS_STEP" in steps[1].get("environment", {}):
        fail("GitHub step environment leaked across step boundaries")
    if steps[0].get("directory") != "src" or steps[0].get("when") != [{"branch": "main"}]:
        fail("GitHub step working directory or condition was not preserved")


def test_github_schedule_mapping() -> None:
    source = """
on:
  schedule:
    - cron: '0 3 * * *'
jobs:
  nightly:
    runs-on: ubuntu-latest
    steps:
      - run: echo nightly
"""
    rendered, report = pipeline.convert_pipeline(
        "github",
        source,
        ".github/workflows/nightly.yml",
        {
            "runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}},
            "schedule_mappings": {"0 3 * * *": "nightly"},
        },
    )
    if not report["supported"] or report["schedules"] != [{"source": "0 3 * * *", "target": "nightly"}]:
        fail(f"mapped GitHub schedule was not accepted: {json.dumps(report)}")
    if 'event: "cron"' not in rendered or 'cron: "nightly"' not in rendered:
        fail(f"Woodpecker cron filter was not rendered: {rendered}")
    _, unmapped = pipeline.convert_pipeline(
        "github",
        source,
        ".github/workflows/nightly.yml",
        {"runner_labels": {"ubuntu-latest": {"platform": "linux/amd64"}}},
    )
    if "github-schedule-unmapped" not in {item["code"] for item in unmapped["unsupported"]}:
        fail("unmapped GitHub schedule was not blocked")


def test_gitlab_schedule_mapping() -> None:
    source = """
nightly:
  only: [schedules]
  script: echo nightly
"""
    rendered, report = pipeline.convert_pipeline(
        "gitlab",
        source,
        ".gitlab-ci.yml",
        {"schedule_mappings": {"0 2 * * *": "nightly"}},
    )
    if not report["supported"] or 'event: "cron"' not in rendered:
        fail(f"mapped GitLab schedule was not accepted: {json.dumps(report)}\n{rendered}")


def test_runner_scope_is_fail_closed() -> None:
    source = """
build:
  tags: [linux]
  script: echo build
deploy:
  tags: [kubernetes]
  script: echo deploy
"""
    _, report = pipeline.convert_pipeline(
        "gitlab",
        source,
        ".gitlab-ci.yml",
        {"runner_labels": {"linux": {"platform": "linux"}, "kubernetes": {"platform": "kubernetes"}}},
    )
    if "runner-label-scope" not in {item["code"] for item in report["unsupported"]}:
        fail("different GitLab runner scopes were not blocked")


def test_cli_mapping_options() -> None:
    source = """
on:
  schedule:
    - cron: '0 4 * * *'
jobs:
  nightly:
    runs-on: ubuntu-latest
    steps:
      - run: echo nightly
"""
    with tempfile.TemporaryDirectory(prefix="forge-pipeline-cli-") as directory:
        root = Path(directory)
        source_path = root / "workflow.yml"
        output_path = root / ".woodpecker.yml"
        report_path = root / "report.json"
        source_path.write_text(source, encoding="utf-8")
        result = pipeline.main(
            [
                "github",
                str(source_path),
                "--output",
                str(output_path),
                "--report",
                str(report_path),
                "--runner-label",
                "ubuntu-latest=platform:linux/amd64",
                "--schedule-mapping",
                "0 4 * * *=nightly",
            ]
        )
        if result != 0 or "cron: \"nightly\"" not in output_path.read_text(encoding="utf-8"):
            fail("CLI runner-label and schedule mappings were not applied")


def test_deterministic_report() -> None:
    source = "build:\n  script: echo ok\n"
    rendered_a, report_a = pipeline.convert_pipeline("gitlab", source, "ci.yml", {})
    rendered_b, report_b = pipeline.convert_pipeline("gitlab", source, "ci.yml", {})
    if rendered_a != rendered_b or report_a != report_b:
        fail("pipeline conversion is not deterministic")
    if report_a["rendered_sha256"] != hashlib.sha256(rendered_a.encode()).hexdigest():
        fail("rendered pipeline digest is incorrect")


def main() -> int:
    test_gitlab_conversion()
    test_github_conversion()
    test_secret_values_never_render()
    test_unsupported_features_fail_closed()
    test_github_step_scope_and_filters()
    test_github_schedule_mapping()
    test_gitlab_schedule_mapping()
    test_runner_scope_is_fail_closed()
    test_cli_mapping_options()
    test_deterministic_report()
    print("Forge pipeline conversion self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
