#!/usr/bin/env python3
"""Validate the rendered Kubernetes schema gate contract."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import validate_synthetic_private_profile as synthetic_validator


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required rendered-schema file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def test_sanitized_artifact_retention() -> None:
    with tempfile.TemporaryDirectory(prefix="platform-rendered-artifact-test-") as temporary:
        root = Path(temporary)
        schema_output = root / "source"
        reports = schema_output / "reports"
        reports.mkdir(parents=True)
        (reports / "app.render.log").write_text("stdout_sha256=test\n", encoding="utf-8")
        (reports / "app.kubeconform.json").write_text("{}\n", encoding="utf-8")
        raw_manifests = schema_output / "manifests"
        raw_manifests.mkdir()
        (raw_manifests / "secret.yaml").write_text("kind: Secret\n", encoding="utf-8")
        summary = {
            "rendered": [
                {
                    "application": "app",
                    "report": "rendered/schema-validation/reports/app.kubeconform.json",
                }
            ],
            "skipped": [],
            "failures": [],
        }
        destination = root / "retained"
        synthetic_validator.retain_sanitized_artifacts(
            schema_output,
            destination,
            summary,
        )
        retained = json.loads(
            (destination / "schema-validation/summary.json").read_text(encoding="utf-8")
        )
        if retained.get("artifactPolicy") != "sanitized-reports-only":
            raise AssertionError("retained schema summary omitted its artifact policy")
        if retained["rendered"][0].get("report") != "reports/app.kubeconform.json":
            raise AssertionError("retained schema summary contains a temporary report path")
        if (destination / "schema-validation/manifests").exists():
            raise AssertionError("raw schema manifests crossed the retained artifact boundary")

        (reports / "unexpected.yaml").write_text("kind: Secret\n", encoding="utf-8")
        try:
            synthetic_validator.retain_sanitized_artifacts(
                schema_output,
                destination,
                summary,
            )
        except RuntimeError as exc:
            if "unexpected rendered-schema report artifact" not in str(exc):
                raise
        else:
            raise AssertionError("unexpected rendered-schema report artifact was retained")


def main() -> int:
    test_sanitized_artifact_retention()
    validator = read(ROOT / "scripts/validate_rendered_manifests.py")
    synthetic_fixture = read(ROOT / "scripts/synthetic_private_profile.py")
    synthetic_validator = read(ROOT / "scripts/validate_synthetic_private_profile.py")
    workflow = read(ROOT / ".github/workflows/validate.yml")
    makefile = read(ROOT / "Makefile")
    supply_chain = read(ROOT / "docs/SUPPLY_CHAIN.md")
    readiness = read(ROOT / "docs/PRODUCTION_READINESS.md")

    for needle in (
        '"base": Path("gitops/clusters/rke2-main/platform-apps.yaml")',
        '"premium-3node": Path(',
        "required_tool(\"KUSTOMIZE_BIN\", \"kustomize\")",
        "required_tool(\"KUBECONFORM_BIN\", \"kubeconform\")",
        "required_tool(\"HELM_BIN\", \"helm\")",
        'shutil.copytree(root / "gitops"',
        '"--enable-helm"',
        '"--helm-command"',
        '"--helm-kube-version"',
        '"LoadRestrictionsNone"',
        '"-strict"',
        '"-ignore-missing-schemas"',
        '"-kubernetes-version"',
        'manifests_dir = temporary_root / "manifests"',
        "stdout_sha256=",
        '"manifestSha256"',
        "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE",
        '"--require-complete"',
        '"--repo-root"',
        "unresolved-placeholders",
        "summary.json",
        "rendered schema verification produced no validated applications",
    ):
        require(validator, needle, "rendered schema validator")

    for needle in (
        "prepare_synthetic_private_profile",
        'platform_argocd_host=argocd.example.test',
        '"OBJECT_STORAGE_ENDPOINT": "https://object.example.test"',
        '"STEP_CA_MODE": "bootstrap"',
        '"PLATFORM_IMAGE_INTEGRITY_MODE": "Audit"',
        "sanitized_environment",
    ):
        require(synthetic_fixture, needle, "synthetic premium fixture")

    for needle in (
        'profile=["premium-3node"]',
        "require_complete=True",
        'output_dir="rendered/schema-validation"',
        'application_sources("premium-3node", repo_root)',
        "rendered != expected",
        'tempfile.TemporaryDirectory(prefix="platform-synthetic-private-schema-")',
        "retain_sanitized_artifacts",
        'retained["artifactPolicy"] = "sanitized-reports-only"',
        "unexpected rendered-schema report artifact",
        "synthetic_private_profile=passed",
    ):
        require(synthetic_validator, needle, "synthetic premium validator")

    for needle in (
        "sigs.k8s.io/kustomize/kustomize/v5@v5.8.1",
        "helm.sh/helm/v3/cmd/helm@v3.21.0",
        "github.com/yannh/kubeconform/cmd/kubeconform@v0.7.0",
        "PLATFORM_RENDERED_SCHEMA_PROFILES: base,premium-3node",
        "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE: \"true\"",
        "make rendered-schema-verify",
        "make rendered-private-schema-verify",
        "rendered/schema-validation",
        "rendered/synthetic-private-schema/schema-validation",
    ):
        require(workflow, needle, "GitHub validation workflow")

    require(makefile, "rendered-schema-verify:", "Makefile")
    require(makefile, "rendered-private-schema-verify:", "Makefile")
    require(
        makefile,
        "platform-image-inventory-verify: rendered-schema-verify rendered-private-schema-verify supply-chain-verify",
        "production readiness gate",
    )
    require(supply_chain, "make rendered-schema-verify", "supply-chain documentation")
    require(supply_chain, "make rendered-private-schema-verify", "supply-chain documentation")
    require(readiness, "make rendered-schema-verify", "production readiness documentation")
    require(readiness, "make rendered-private-schema-verify", "production readiness documentation")

    print("Rendered Kubernetes schema validation contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
