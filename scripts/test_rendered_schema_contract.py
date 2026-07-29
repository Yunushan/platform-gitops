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
    workflow = read(ROOT / ".github/workflows/validate.yml")
    makefile = read(ROOT / "Makefile")
    supply_chain = read(ROOT / "docs/SUPPLY_CHAIN.md")
    readiness = read(ROOT / "docs/PRODUCTION_READINESS.md")

    for needle in (
        '"base": ROOT / "gitops/clusters/rke2-main/platform-apps.yaml"',
        '"premium-3node": ROOT',
        "required_tool(\"KUSTOMIZE_BIN\", \"kustomize\")",
        "required_tool(\"KUBECONFORM_BIN\", \"kubeconform\")",
        "required_tool(\"HELM_BIN\", \"helm\")",
        "shutil.copytree(ROOT / \"gitops\"",
        '"--enable-helm"',
        '"--helm-command"',
        '"--helm-kube-version"',
        '"LoadRestrictionsNone"',
        '"-strict"',
        '"-ignore-missing-schemas"',
        '"-kubernetes-version"',
        "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE",
        "unresolved-placeholders",
        "summary.json",
        "rendered schema verification produced no validated applications",
    ):
        require(validator, needle, "rendered schema validator")

    for needle in (
        "sigs.k8s.io/kustomize/kustomize/v5@v5.8.1",
        "helm.sh/helm/v3/cmd/helm@v3.21.0",
        "github.com/yannh/kubeconform/cmd/kubeconform@v0.7.0",
        "PLATFORM_RENDERED_SCHEMA_PROFILES: base,premium-3node",
        "PLATFORM_RENDERED_SCHEMA_ALLOW_INCOMPLETE: \"true\"",
        "make rendered-schema-verify",
        "rendered/schema-validation",
    ):
        require(workflow, needle, "GitHub validation workflow")

    require(makefile, "rendered-schema-verify:", "Makefile")
    require(
        makefile,
        "platform-image-inventory-verify: rendered-schema-verify supply-chain-verify",
        "production readiness gate",
    )
    require(supply_chain, "make rendered-schema-verify", "supply-chain documentation")
    require(readiness, "make rendered-schema-verify", "production readiness documentation")

    print("Rendered Kubernetes schema validation contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
