#!/usr/bin/env python3
"""Validate supply-chain helper examples for Renovate and Cosign/Kyverno."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RENOVATE = ROOT / "renovate.json"
COSIGN_POLICY = ROOT / "policies/kyverno/verify-signed-images.example.yaml"
POLICY_README = ROOT / "policies/README.md"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs/ARCHITECTURE.md"
PREMIUM = ROOT / "docs/PREMIUM_3NODE.md"


def fail(message: str) -> int:
    print(f"Supply-chain helper validation failed: {message}", file=sys.stderr)
    return 1


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AssertionError(f"missing required file: {path.relative_to(ROOT)}")


def load_renovate() -> dict[str, object]:
    try:
        data = json.loads(read(RENOVATE))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"renovate.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssertionError("renovate.json must contain a JSON object")
    return data


def assert_contains(text: str, *needles: str, label: str) -> None:
    for needle in needles:
        if needle not in text:
            raise AssertionError(f"{label} is missing required text: {needle}")


def main() -> int:
    problems: list[str] = []
    try:
        renovate = load_renovate()
    except AssertionError as exc:
        problems.append(str(exc))
        renovate = {}

    expected_schema = "https://docs.renovatebot.com/renovate-schema.json"
    if renovate.get("$schema") != expected_schema:
        problems.append("renovate.json must use the official Renovate JSON schema")
    extends = renovate.get("extends")
    if not isinstance(extends, list) or "config:recommended" not in extends:
        problems.append("renovate.json must extend config:recommended")
    if renovate.get("dependencyDashboard") is not True:
        problems.append("renovate.json must enable the dependency dashboard")
    if renovate.get("automerge") is not False:
        problems.append("renovate.json must keep automerge disabled by default")
    if not isinstance(renovate.get("prConcurrentLimit"), int) or int(renovate["prConcurrentLimit"]) < 1:
        problems.append("renovate.json must set a positive prConcurrentLimit")
    if not isinstance(renovate.get("prHourlyLimit"), int) or int(renovate["prHourlyLimit"]) < 1:
        problems.append("renovate.json must set a positive prHourlyLimit")

    rules = renovate.get("packageRules", [])
    if not isinstance(rules, list) or not rules:
        problems.append("renovate.json must define packageRules")
        rules = []
    if not any(
        isinstance(rule, dict)
        and "docker" in rule.get("matchDatasources", [])
        and rule.get("pinDigests") is True
        for rule in rules
    ):
        problems.append("renovate.json must pin Docker/container image digests")
    if not any(
        isinstance(rule, dict)
        and "helm" in rule.get("matchDatasources", [])
        and "helm" in str(rule.get("groupName", "")).lower()
        for rule in rules
    ):
        problems.append("renovate.json must group Helm chart updates")
    if not any(
        isinstance(rule, dict)
        and "major" in rule.get("matchUpdateTypes", [])
        and rule.get("dependencyDashboardApproval") is True
        for rule in rules
    ):
        problems.append("renovate.json must require dashboard approval for major updates")

    policy_text = read(COSIGN_POLICY)
    for needle in (
        "kind: ClusterPolicy",
        "name: verify-signed-platform-images",
        "background: false",
        "webhookConfiguration:",
        "failurePolicy: Fail",
        "verifyImages:",
        "imageReferences:",
        '"<REGISTRY>/<PROJECT>/*"',
        "failureAction: Audit",
        "mutateDigest: true",
        "verifyDigest: true",
        "attestors:",
        "publicKeys: k8s://<NAMESPACE>/<COSIGN_PUBLIC_KEY_SECRET>",
        "https://rekor.sigstore.dev",
    ):
        if needle not in policy_text:
            problems.append(f"{COSIGN_POLICY.relative_to(ROOT)} is missing required text: {needle}")
    if "failureAction: Enforce" in policy_text:
        problems.append("Cosign/Kyverno policy example must not default image verification to Enforce")

    for path, text, required in (
        (
            POLICY_README,
            read(POLICY_README),
            (
                "kyverno/verify-signed-images.example.yaml",
                "Cosign",
                "Renovate",
                "renovate.json",
            ),
        ),
        (
            README,
            read(README),
            (
                "Cosign + Renovate supply-chain helpers",
                "renovate.json",
                "verify-signed-images.example.yaml",
            ),
        ),
        (
            ARCHITECTURE,
            read(ARCHITECTURE),
            ("Cosign", "Renovate", "image signature", "dependency update"),
        ),
        (
            PREMIUM,
            read(PREMIUM),
            ("renovate.json", "Cosign", "verify-signed-images.example.yaml", "pinDigests"),
        ),
    ):
        try:
            assert_contains(text, *required, label=str(path.relative_to(ROOT)))
        except AssertionError as exc:
            problems.append(str(exc))

    if problems:
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1

    print("Supply-chain helper validation passed for Renovate and Cosign/Kyverno examples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
