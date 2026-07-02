#!/usr/bin/env python3
"""Validate optional policy examples stay documented and safe by default."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "policies"
README = POLICIES / "README.md"

EXPECTED_FILES = {
    "kyverno/no-plaintext-secrets.example.yaml": {
        "apiVersion: kyverno.io/v1",
        "kind: ClusterPolicy",
        "validationFailureAction: Audit",
        "background: true",
        "- Secret",
        "platform.gitops/secret-source",
        "external-secrets",
        "sealed-secrets",
        "sops",
        "manual-bootstrap",
    },
    "kyverno/require-workload-baseline.example.yaml": {
        "apiVersion: kyverno.io/v1",
        "kind: ClusterPolicy",
        "validationFailureAction: Audit",
        "require-resource-requests",
        "require-non-root",
        "require-no-privilege-escalation",
        "runAsNonRoot: true",
        "allowPrivilegeEscalation: false",
    },
    "kyverno/verify-signed-images.example.yaml": {
        "apiVersion: kyverno.io/v1",
        "kind: ClusterPolicy",
        "verifyImages:",
        "imageReferences:",
        '"<REGISTRY>/<PROJECT>/*"',
        "failureAction: Audit",
        "mutateDigest: true",
        "verifyDigest: true",
        "attestors:",
        "publicKeys: k8s://<NAMESPACE>/<COSIGN_PUBLIC_KEY_SECRET>",
        "https://rekor.sigstore.dev",
    },
    "network/default-deny.example.yaml": {
        "apiVersion: networking.k8s.io/v1",
        "kind: NetworkPolicy",
        "namespace: <NAMESPACE>",
        "podSelector: {}",
        "- Ingress",
        "- Egress",
    },
    "network/allow-platform-dns-and-ingress.example.yaml": {
        "apiVersion: networking.k8s.io/v1",
        "kind: NetworkPolicy",
        "namespace: <NAMESPACE>",
        "allow-dns-and-traefik",
        "kubernetes.io/metadata.name: traefik",
        "kubernetes.io/metadata.name: kube-system",
        "port: 53",
    },
}


def fail(message: str) -> int:
    print(f"Policy example validation failed: {message}", file=sys.stderr)
    return 1


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AssertionError(f"missing required policy file: {path.relative_to(ROOT)}")


def main() -> int:
    if not POLICIES.is_dir():
        return fail("missing policies directory")

    readme = read(README)
    problems: list[str] = []
    for rel_path, required_text in EXPECTED_FILES.items():
        path = POLICIES / rel_path
        text = read(path)
        if not rel_path.endswith(".example.yaml"):
            problems.append(f"{rel_path} must remain an opt-in .example.yaml file")
        if "validationFailureAction: Enforce" in text:
            problems.append(f"{rel_path} must not default Kyverno examples to Enforce")
        if "kind: Secret" in text and "validationFailureAction: Audit" not in text:
            problems.append(f"{rel_path} must keep Secret policy examples in Audit mode")
        for needle in required_text:
            if needle not in text:
                problems.append(f"{rel_path} is missing required text: {needle}")
        if rel_path not in readme:
            problems.append(f"policies/README.md does not list {rel_path}")

    for needle in (
        "not applied by default",
        "audit/starter posture",
        "replace placeholders such as `<NAMESPACE>`",
        "python scripts/test_policy_examples.py",
    ):
        if needle not in readme:
            problems.append(f"policies/README.md is missing guidance: {needle}")

    policy_files = sorted(
        str(path.relative_to(POLICIES)).replace("\\", "/")
        for path in POLICIES.rglob("*.yaml")
    )
    unexpected = sorted(set(policy_files) - set(EXPECTED_FILES))
    if unexpected:
        problems.append(f"unexpected policy example(s) without contract coverage: {', '.join(unexpected)}")
    missing = sorted(set(EXPECTED_FILES) - set(policy_files))
    if missing:
        problems.append(f"missing contracted policy example(s): {', '.join(missing)}")

    if problems:
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        return 1

    print(f"Policy example validation passed for {len(EXPECTED_FILES)} examples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
