#!/usr/bin/env python3
"""Validate the immutable, attested GitHub release workflow contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"
GUIDE = ROOT / "docs/RELEASE_GUIDE.md"
ACTION_SHA_RE = re.compile(r"uses:\s*[^\s@]+@(?P<sha>[0-9a-f]{40})(?:\s|$)")


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required release artifact is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def main() -> int:
    workflow = read(WORKFLOW)
    for needle in (
        "tags:\n      - 'v*.*.*'",
        "  verify:",
        "  release:",
        "needs: verify",
        "environment: production-release",
        "contents: read",
        "attestations: write",
        "contents: write",
        "id-token: write",
        "runs-on: ubuntu-24.04",
        "persist-credentials: false",
        "python scripts/run_validation.py",
        "scripts/bootstrap/install-kyverno-cli.sh",
        "python scripts/verify_active_kyverno_policies.py",
        "KYVERNO_BIN: ${{ runner.temp }}/platform-tools/kyverno",
        "github.com/rhysd/actionlint/cmd/actionlint@v1.7.12",
        "gitleaks/gitleaks-action@",
        "semgrep==1.171.0",
        "aquasecurity/trivy-action@",
        "make rendered-schema-verify",
        "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}",
        "git merge-base --is-ancestor",
        "python scripts/verify_github_release_ref.py",
        "rendered/governance/github-release-evidence.json",
        ".github-release.json",
        "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        "python scripts/verify_github_governance.py",
        "GITHUB_TOKEN: ${{ secrets.GOVERNANCE_AUDIT_TOKEN }}",
        "GITHUB_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}",
        "rendered/governance/github-governance-evidence.json",
        ".github-governance.json",
        "git archive --format=tar",
        "gzip -n",
        "spdx-json=dist/",
        "cyclonedx-json=dist/",
        "python scripts/verify_supply_chain_evidence.py",
        "sha256sum",
        "actions/upload-artifact@",
        "actions/download-artifact@",
        "sha256sum -c SHA256SUMS",
        "actions/attest-build-provenance@",
        "actions/attest-sbom@",
        "cosign sign-blob --yes",
        "--bundle dist/SHA256SUMS.sigstore.json",
        "gh release create",
        "--verify-tag",
        "--generate-notes",
    ):
        require(workflow, needle, "release workflow")

    uses_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]
    if not uses_lines:
        raise AssertionError("release workflow has no pinned actions")
    for line in uses_lines:
        if not ACTION_SHA_RE.search(line):
            raise AssertionError(f"release workflow action is not pinned to a full commit SHA: {line}")

    verify_section, release_section = workflow.split("\n  release:\n", 1)
    for permission in ("attestations: write", "contents: write", "id-token: write"):
        if permission in verify_section:
            raise AssertionError(f"read-only release verification job must not receive {permission}")
        require(release_section, permission, "privileged release publication job")
    for verifier in (
        "gitleaks/gitleaks-action@",
        "aquasecurity/trivy-action@",
        "anchore/sbom-action/download-syft@",
        "semgrep scan",
        "make rendered-schema-verify",
        "python scripts/verify_active_kyverno_policies.py",
    ):
        require(verify_section, verifier, "read-only release verification job")
        if verifier in release_section:
            raise AssertionError(f"privileged release publication job must not run verifier {verifier}")

    guide = read(GUIDE)
    for needle in (
        "Attested tag release",
        "sha256sum -c SHA256SUMS",
        "cosign verify-blob",
        "gh attestation verify",
        "platform-production-check",
        "platform-production-score",
        "production-release",
        "Gitleaks, Semgrep, Trivy",
        "Kyverno CEL",
        "annotated and GitHub-verified",
        "signed commit",
        "*.github-release.json",
    ):
        require(guide, needle, "release guide")

    print("Attested GitHub release workflow contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
