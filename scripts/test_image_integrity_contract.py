#!/usr/bin/env python3
"""Validate the managed admission-time image signature contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREMIUM = ROOT / "gitops/clusters/rke2-main/premium-3node"
POLICY = PREMIUM / "apps/platform-image-integrity/verify-platform-images.yaml"
POLICY_KUSTOMIZATION = PREMIUM / "apps/platform-image-integrity/kustomization.yaml"
KYVERNO_VALUES = PREMIUM / "apps/kyverno/values.yaml"
PLATFORM_PROJECT = ROOT / "gitops/clusters/rke2-main/projects/platform-project.yaml"
PLATFORM_APPS = PREMIUM / "platform-apps.yaml"
PROFILE = ROOT / "profiles/premium-3node.yaml"
RENDERER = ROOT / "scripts/render_private_platform_values.py"
RENDERER_TEST = ROOT / "scripts/test_private_values_renderer.py"
SYNTHETIC_FIXTURE = ROOT / "scripts/synthetic_private_profile.py"
READINESS = ROOT / "ansible/playbooks/verify-platform-policy-readiness.yml"
MAKEFILE = ROOT / "Makefile"
ENV_EXAMPLE = ROOT / "config/seed-git.env.example"
SUPPLY_CHAIN_DOC = ROOT / "docs/SUPPLY_CHAIN.md"
READINESS_DOC = ROOT / "docs/PRODUCTION_READINESS.md"


def read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required image-integrity file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} is missing {needle!r}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} must not contain {needle!r}")


def main() -> int:
    policy = read(POLICY)
    for needle in (
        "apiVersion: policies.kyverno.io/v1",
        "kind: ImageValidatingPolicy",
        "name: verify-platform-image-signatures",
        "admission:\n      enabled: true",
        "background:\n      enabled: true",
        "validationActions:\n    - Audit",
        "failurePolicy: Fail",
        "timeoutSeconds: 15",
        "- CREATE",
        "- UPDATE",
        "- pods",
        'image.registry == \'<PLATFORM_IMAGE_REGISTRY>\'',
        "name: platformCosign",
        "<PLATFORM_COSIGN_PUBLIC_KEY>",
        'url: "<PLATFORM_COSIGN_REKOR_URL>"',
        "insecureIgnoreTlog: false",
        "insecureIgnoreSCT: false",
        "mutateDigest: true",
        "required: true",
        "verifyDigest: true",
        "verifyImageSignatures(image, [attestors.platformCosign])",
    ):
        require(policy, needle, "managed image validating policy")
    for needle in (
        "apiVersion: kyverno.io/v1",
        "kind: ClusterPolicy",
        "verifyImages:",
        "insecureIgnoreTlog: true",
        "insecureIgnoreSCT: true",
        "PRIVATE KEY",
    ):
        forbid(policy, needle, "managed image validating policy")

    kustomization = read(POLICY_KUSTOMIZATION)
    require(kustomization, "- verify-platform-images.yaml", "image-integrity kustomization")
    forbid(kustomization, "namespace:", "cluster-scoped image-integrity kustomization")

    kyverno_values = read(KYVERNO_VALUES)
    secret_reader = "resources:\n            - secrets\n          verbs:\n            - get"
    if kyverno_values.count(secret_reader) != 2:
        raise AssertionError(
            "Kyverno admission and background controllers must each receive exactly get-only "
            "Secret access for imagePullSecrets"
        )
    for unsafe in ("- list", "- watch", "- create", "- update", "- patch", "- delete"):
        for block in kyverno_values.split("extraResources:")[1:3]:
            scoped = block.split("podDisruptionBudget:", 1)[0]
            forbid(scoped, unsafe, "Kyverno imagePullSecret RBAC")

    project = read(PLATFORM_PROJECT)
    require(
        project,
        "- group: policies.kyverno.io\n      kind: ImageValidatingPolicy",
        "platform AppProject",
    )

    apps = read(PLATFORM_APPS)
    for needle in (
        "name: platform-image-integrity",
        'argocd.argoproj.io/sync-wave: "6"',
        "path: gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity",
        "- Prune=confirm",
        "- PruneLast=true",
        "- ServerSideApply=true",
    ):
        require(apps, needle, "premium Argo CD application set")
    require(read(PROFILE), "platform-image-integrity", "premium profile inventory")

    renderer = read(RENDERER)
    for needle in (
        "def validate_cosign_public_key(",
        "def render_platform_image_integrity(",
        'PLATFORM_IMAGE_INTEGRITY_MODE", "disabled"',
        'PLATFORM_COSIGN_PUBLIC_KEY_FILE',
        'PLATFORM_COSIGN_REKOR_URL", "https://rekor.sigstore.dev"',
        'PLATFORM_IMAGE_REGISTRY',
        'mode not in {"disabled", "audit", "enforce"}',
        '"Deny" if mode == "enforce" else "Audit"',
        "must contain exactly one PEM PUBLIC KEY block",
    ):
        require(renderer, needle, "private values renderer")
    renderer_test = read(RENDERER_TEST)
    for needle in (
        "TEST_COSIGN_PUBLIC_KEY",
        '"validationActions:\\n    - Deny"',
        "<PLATFORM_COSIGN_PUBLIC_KEY>",
        "image-integrity renderer accepted an unsupported mode",
    ):
        require(renderer_test, needle, "private renderer self-test")
    synthetic_fixture = read(SYNTHETIC_FIXTURE)
    for needle in (
        "TEST_COSIGN_PUBLIC_KEY",
        '"PLATFORM_IMAGE_INTEGRITY_MODE": "Audit"',
        '"PLATFORM_COSIGN_PUBLIC_KEY_FILE"',
        '"PLATFORM_COSIGN_REKOR_URL": "https://rekor.example.test"',
    ):
        require(synthetic_fixture, needle, "synthetic private profile fixture")

    readiness = read(READINESS)
    for needle in (
        "PLATFORM_IMAGE_INTEGRITY_REQUIRED",
        "PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE",
        "PLATFORM_IMAGE_INTEGRITY_CANARY_PULL_SECRET",
        "get imagevalidatingpolicy.policies.kyverno.io",
        ".status.conditionStatus.ready",
        ".spec.failurePolicy",
        ".spec.validationConfigurations.mutateDigest",
        "signed IMAGE@sha256:DIGEST",
        "create --dry-run=server",
        "unsigned-image-was-admitted",
        "unsigned-image-rejection-was-not-attributed-to-policy",
        "signed_image_admission=passed",
        "unverifiable_image_rejection=passed",
    ):
        require(readiness, needle, "live policy readiness gate")

    makefile = read(MAKEFILE)
    require(
        makefile,
        "PLATFORM_IMAGE_INTEGRITY_MODE=Enforce PLATFORM_IMAGE_INTEGRITY_REQUIRED=true",
        "production gate",
    )
    env_example = read(ENV_EXAMPLE)
    for needle in (
        "PLATFORM_IMAGE_INTEGRITY_MODE=disabled",
        "PLATFORM_COSIGN_PUBLIC_KEY_FILE",
        "PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE",
    ):
        require(env_example, needle, "seed Git environment example")
    for path, label in (
        (SUPPLY_CHAIN_DOC, "supply-chain guide"),
        (READINESS_DOC, "production-readiness guide"),
    ):
        doc = read(path)
        require(doc, "PLATFORM_IMAGE_INTEGRITY_MODE", label)
        require(doc, "PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE", label)

    print("Admission-time image integrity contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
