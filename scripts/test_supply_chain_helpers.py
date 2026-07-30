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
INSTALLATION = ROOT / "docs/INSTALLATION.md"
EVIDENCE_VALIDATOR = ROOT / "scripts/verify_supply_chain_evidence.py"
EVIDENCE_TEST = ROOT / "scripts/test_supply_chain_evidence.py"
IMAGE_INVENTORY_TEST = ROOT / "scripts/test_image_inventory_evidence.py"
IMAGE_INVENTORY_RECONCILER = ROOT / "scripts/reconcile_image_inventory.py"
IMAGE_INVENTORY_VERIFIER = ROOT / "scripts/verify_image_inventory_evidence.py"
IMAGE_INVENTORY_WRAPPER = ROOT / "scripts/bootstrap/run-platform-image-inventory.sh"
POSTURE_SCRIPT = ROOT / "scripts/supply-chain-posture.sh"
SECURITY_SCAN_SCRIPT = ROOT / "scripts/security-scan.sh"
GITHUB_VALIDATION = ROOT / ".github/workflows/validate.yml"
SCORECARD_WORKFLOW = ROOT / ".github/workflows/scorecard.yml"
MAKEFILE = ROOT / "Makefile"
GITHUB_WORKFLOWS = ROOT / ".github/workflows"
GITLEAKS_CONFIG = ROOT / ".gitleaks.toml"
SEMGREP_CONFIG = ROOT / ".semgrep.yml"
SEMGREP_IGNORE = ROOT / ".semgrepignore"
RKE2_BOOTSTRAP_SCRIPTS = (
    ROOT / "scripts/bootstrap/install-rke2-first-server.sh",
    ROOT / "scripts/bootstrap/install-rke2-server.sh",
)


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

    for script in RKE2_BOOTSTRAP_SCRIPTS:
        try:
            script_text = read(script)
            label = str(script.relative_to(ROOT))
            assert_contains(
                script_text,
                ': "${RKE2_VERSION:?',
                ': "${RKE2_INSTALL_SCRIPT_SHA256:?',
                "umask 077",
                'export INSTALL_RKE2_TYPE="server"',
                'export INSTALL_RKE2_VERSION="${RKE2_VERSION}"',
                "unset INSTALL_RKE2_CHANNEL",
                "10#${timeout_value}",
                "--proto '=https'",
                "--proto-redir '=https'",
                "sha256sum --check --strict",
                'chmod 0700 "${installer}"',
                "mktemp /etc/rancher/rke2/.config.yaml.XXXXXX",
                'chmod 0600 "${config_tmp}"',
                'mv -f -- "${config_tmp}" /etc/rancher/rke2/config.yaml',
                "quoted_cluster_credential=",
                label=label,
            )
            for insecure_pattern in (
                'export INSTALL_RKE2_TYPE="${INSTALL_RKE2_TYPE:-server}"',
                "cat >/etc/rancher/rke2/config.yaml",
                "curl -sfL https://get.rke2.io |",
            ):
                if insecure_pattern in script_text:
                    problems.append(
                        f"{label} retains insecure manual bootstrap pattern: "
                        f"{insecure_pattern}"
                    )

            checksum_index = script_text.index("sha256sum --check --strict")
            config_install_index = script_text.index(
                'mv -f -- "${config_tmp}" /etc/rancher/rke2/config.yaml'
            )
            execution_index = script_text.index(
                'timeout "${RKE2_INSTALL_TIMEOUT}" "${installer}"'
            )
            if not checksum_index < config_install_index < execution_index:
                problems.append(
                    f"{label} must verify the installer before installing config "
                    "and executing it"
                )
        except (AssertionError, ValueError) as exc:
            problems.append(str(exc))

    try:
        installation_text = read(INSTALLATION)
        assert_contains(
            installation_text,
            "Manual bootstrap scripts always require an exact RKE2 release",
            "RKE2_INSTALL_SCRIPT_SHA256=<REVIEWED_INSTALLER_SHA256>",
            "`/etc/rancher/rke2/config.yaml`",
            "atomically with mode `0600`",
            label=str(INSTALLATION.relative_to(ROOT)),
        )
        if installation_text.count(
            "RKE2_INSTALL_SCRIPT_SHA256=<REVIEWED_INSTALLER_SHA256>"
        ) < len(RKE2_BOOTSTRAP_SCRIPTS):
            problems.append(
                "docs/INSTALLATION.md must show the reviewed installer digest "
                "for both manual RKE2 bootstrap commands"
            )
    except AssertionError as exc:
        problems.append(str(exc))

    for workflow in sorted(GITHUB_WORKFLOWS.glob("*.yml")):
        if "runs-on: ubuntu-latest" in read(workflow):
            problems.append(
                f"{workflow.relative_to(ROOT)} must pin the Ubuntu runner release"
            )

    try:
        gitleaks_text = read(GITLEAKS_CONFIG)
        assert_contains(
            gitleaks_text,
            'description = "Longhorn dm-crypt cipher algorithm constant"',
            'targetRules = ["generic-api-key"]',
            "aes-xts-plain64",
            "^ansible/playbooks/configure-platform-app-secrets[.]yml$",
            'description = "Literal-ellipsis private-key examples in exact vendored chart documentation"',
            'condition = "AND"',
            'targetRules = ["private-key"]',
            'regexTarget = "match"',
            ".*[.][.][.].*",
            "apps/step-ca/charts/step-certificates-1[.]30[.]1/",
            "premium-3node/apps/argocd-ha/charts/argo-cd-10[.]0[.]0/",
            "premium-3node/apps/keycloak/charts/keycloak-25[.]2[.]0/",
            label=str(GITLEAKS_CONFIG.relative_to(ROOT)),
        )
        if "paths = [\n  '''.*charts" in gitleaks_text:
            problems.append(".gitleaks.toml must not broadly allow every vendored chart path")
    except AssertionError as exc:
        problems.append(str(exc))
    try:
        semgrep_text = read(SEMGREP_CONFIG)
        assert_contains(
            semgrep_text,
            "id: shell-curl-pipe-shell",
            "id: kubernetes-latest-image-tag",
            "id: kubernetes-privileged-container",
            '        - "**/*.yaml"',
            '        - "**/*.yml"',
            "longhorn/charts/longhorn-1.12.0/longhorn/templates/daemonset-sa.yaml",
            "longhorn/charts/longhorn-1.12.0/longhorn/templates/preupgrade-job.yaml",
            "longhorn/charts/longhorn-1.12.0/longhorn/templates/psp.yaml",
            label=str(SEMGREP_CONFIG.relative_to(ROOT)),
        )
        if '        - "**/charts/**"' in semgrep_text:
            problems.append(".semgrep.yml must not broadly exclude vendored charts")

        semgrep_ignored = {
            line.strip()
            for line in read(SEMGREP_IGNORE).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected_ignored = {".git/", "private/", "rendered/", "secrets/"}
        if semgrep_ignored != expected_ignored:
            problems.append(
                ".semgrepignore must contain only the reviewed generated/private paths; "
                f"expected={sorted(expected_ignored)} actual={sorted(semgrep_ignored)}"
            )
    except AssertionError as exc:
        problems.append(str(exc))
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
        "apiVersion: policies.kyverno.io/v1",
        "kind: ImageValidatingPolicy",
        "name: verify-signed-platform-images",
        "background:\n      enabled: true",
        "webhookConfiguration:",
        "failurePolicy: Fail",
        "matchImageReferences:",
        "image.registry == '<REGISTRY>'",
        "validationActions:",
        "- Audit",
        "mutateDigest: true",
        "required: true",
        "verifyDigest: true",
        "attestors:",
        "<COSIGN_PUBLIC_KEY>",
        "https://rekor.sigstore.dev",
        "insecureIgnoreTlog: false",
        "verifyImageSignatures(image, [attestors.approvedCosignKey])",
    ):
        if needle not in policy_text:
            problems.append(f"{COSIGN_POLICY.relative_to(ROOT)} is missing required text: {needle}")
    if "validationActions:\n    - Deny" in policy_text:
        problems.append("Cosign/Kyverno policy example must not default image verification to Deny")

    for path, required in (
        (
            EVIDENCE_VALIDATOR,
            (
                "validate_sbom",
                "validate_scorecard",
                "validate_signature_report",
                "strict evidence requires an OpenSSF Scorecard report",
                "strict evidence requires a Cosign signature report",
                "@sha256:",
            ),
        ),
        (
            EVIDENCE_TEST,
            (
                "Supply-chain evidence validator self-test passed.",
                "below-threshold Scorecard",
                "tag-only Cosign image",
                "empty SBOM",
            ),
        ),
        (
            IMAGE_INVENTORY_TEST,
            (
                "Rendered/live image inventory reconciliation self-test passed.",
                "unsigned private-registry image",
                "outside-registry image",
                "expired image exception",
            ),
        ),
        (
            IMAGE_INVENTORY_RECONCILER,
            (
                "rendered images were neither observed live nor resolved by exception",
                "image coverage is incomplete",
                "vulnerability report hash does not match",
                "must expire within 90 days",
            ),
        ),
        (
            IMAGE_INVENTORY_VERIFIER,
            (
                "private-registry image lacks signature or admission coverage",
                "outside-registry image lacks an admission-scope exception",
                "Image inventory evidence accepted:",
            ),
        ),
        (
            IMAGE_INVENTORY_WRAPPER,
            (
                "capture-platform-image-inventory.yml",
                "reconcile_image_inventory.py",
                "verify_image_inventory_evidence.py",
            ),
        ),
        (
            POSTURE_SCRIPT,
            (
                "SUPPLY_CHAIN_STRICT",
                "SUPPLY_CHAIN_MIN_SCORE",
                "COSIGN_IMAGES_FILE",
                "verify_supply_chain_evidence.py",
                "Strict supply-chain evidence did not verify any images.",
                "\nPath(sys.argv[2]).write_text(",
            ),
        ),
        (
            SECURITY_SCAN_SCRIPT,
            (
                "gitleaks_args=(\n  dir",
                'gitleaks_args+=("${ROOT}")',
                "trivy_args=(\n  fs",
                "semgrep",
            ),
        ),
        (
            GITHUB_VALIDATION,
            (
                "anchore/sbom-action/download-syft@",
                "gitleaks/gitleaks-action@",
                "semgrep==",
                "aquasecurity/trivy-action@",
                "github.com/rhysd/actionlint/cmd/actionlint@v1.7.12",
                "verify_supply_chain_evidence.py",
            ),
        ),
        (
            SCORECARD_WORKFLOW,
            (
                "permissions: read-all",
                "contents: read",
                "runs-on: ubuntu-24.04",
                "ossf/scorecard-action@",
                "publish_results: true",
                "github/codeql-action/upload-sarif@",
            ),
        ),
        (
            MAKEFILE,
            (
                "supply-chain-verify: security-scan",
                "SUPPLY_CHAIN_STRICT=true bash scripts/supply-chain-posture.sh",
                "platform-image-inventory-verify: rendered-schema-verify rendered-private-schema-verify supply-chain-verify",
                "@$(MAKE) platform-image-inventory-verify",
            ),
        ),
    ):
        try:
            assert_contains(read(path), *required, label=str(path.relative_to(ROOT)))
        except AssertionError as exc:
            problems.append(str(exc))

    if "gitleaks_args=(\n  detect" in read(SECURITY_SCAN_SCRIPT):
        problems.append("security-scan.sh must use the supported gitleaks dir command")
    posture_text = read(POSTURE_SCRIPT)
    if "\n  Path(sys.argv[2]).write_text(" in posture_text:
        problems.append("supply-chain-posture.sh contains an invalid indented top-level Python statement")
    try:
        embedded_python = posture_text.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        compile(embedded_python, str(POSTURE_SCRIPT), "exec")
    except (IndexError, SyntaxError) as exc:
        problems.append(f"supply-chain-posture.sh embedded Python is invalid: {exc}")

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

    print(
        "Supply-chain helper validation passed for manual RKE2 bootstrap, CI "
        "scans, narrowed Gitleaks exceptions, SBOM evidence, Scorecard, "
        "Renovate, and Cosign."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
