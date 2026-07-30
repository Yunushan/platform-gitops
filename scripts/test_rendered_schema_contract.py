#!/usr/bin/env python3
"""Validate the rendered Kubernetes schema gate contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required rendered-schema file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def main() -> int:
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
        "remove_generated_sources(repo_root)",
        "shutil.rmtree(generated_source)",
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
        "rendered/synthetic-private-schema/repo/rendered/schema-validation",
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
