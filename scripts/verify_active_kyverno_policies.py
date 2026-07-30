#!/usr/bin/env python3
"""Compile and behavior-test the active Kyverno CEL policies."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from subprocess_timeout import bounded_timeout_seconds, timeout_stream_text


ROOT = Path(__file__).resolve().parents[1]
KYVERNO_TIMEOUT_SECONDS = 120
POLICY_ROOT = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies"
IMAGE_POLICY = (
    ROOT
    / "gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity/verify-platform-images.yaml"
)
FIXTURE_ROOT = ROOT / "scripts/fixtures/platform-policies"
POLICIES = (
    POLICY_ROOT / "no-plaintext-secrets.yaml",
    POLICY_ROOT / "require-workload-baseline.yaml",
    POLICY_ROOT / "require-pod-security-baseline.yaml",
)
TEST_COSIGN_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6QsNef3SKYhJVYSVj+ZfbPwJd0pv
DLYNHXITZkhIzfE+apcxDjCCkDPcJ3A3zvhPATYOIsCxYPch7Q2JdJLsDQ==
-----END PUBLIC KEY-----"""


def resolve_binary(configured: str | None) -> Path:
    candidate = configured or os.environ.get("KYVERNO_BIN") or shutil.which("kyverno")
    if not candidate:
        raise SystemExit("Kyverno CLI is required; set KYVERNO_BIN to the v1.18.1 binary")
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"Kyverno CLI does not exist: {path}")
    return path


def apply(kyverno: Path, fixture: str, policies: tuple[Path, ...] = POLICIES) -> subprocess.CompletedProcess[str]:
    command = [
        str(kyverno),
        "apply",
        *(str(policy) for policy in policies),
        "--resource",
        str(FIXTURE_ROOT / fixture),
        "--detailed-results",
        "--remove-color",
    ]
    try:
        timeout = bounded_timeout_seconds(
            KYVERNO_TIMEOUT_SECONDS,
            "PLATFORM_KYVERNO_COMMAND_TIMEOUT_SECONDS",
        )
    except ValueError as exc:
        return subprocess.CompletedProcess(command, 2, "", str(exc))
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = timeout_stream_text(exc.stdout)
        stderr = timeout_stream_text(exc.stderr)
        detail = f"Kyverno CLI timed out after {timeout:g} seconds"
        stderr = f"{stderr.rstrip()}\n{detail}" if stderr else detail
        return subprocess.CompletedProcess(command, 124, stdout, stderr)


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}".strip()


def render_image_policy(destination: Path) -> Path:
    rendered = IMAGE_POLICY.read_text(encoding="utf-8")
    rendered = rendered.replace("<PLATFORM_IMAGE_REGISTRY>", "signature.invalid")
    rendered = rendered.replace(
        "<PLATFORM_COSIGN_PUBLIC_KEY>",
        TEST_COSIGN_PUBLIC_KEY.replace("\n", "\n            "),
    )
    rendered = rendered.replace(
        "<PLATFORM_COSIGN_REKOR_URL>",
        "https://rekor.sigstore.dev",
    )
    destination.write_text(rendered, encoding="utf-8", newline="\n")
    return destination


def require_result(
    label: str,
    result: subprocess.CompletedProcess[str],
    expected_returncode: int,
    markers: tuple[str, ...],
) -> None:
    output = combined_output(result)
    if result.returncode != expected_returncode:
        raise AssertionError(
            f"{label} returned {result.returncode}, expected {expected_returncode}:\n{output}"
        )
    for marker in markers:
        if marker not in output:
            raise AssertionError(f"{label} output is missing {marker!r}:\n{output}")
    if "error: 0" not in output:
        raise AssertionError(f"{label} reported a CEL evaluation error:\n{output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kyverno-bin", help="Path to the Kyverno v1.18.1 CLI")
    args = parser.parse_args()
    kyverno = resolve_binary(args.kyverno_bin)

    require_result(
        "compliant fixtures",
        apply(kyverno, "compliant.yaml"),
        0,
        ("pass: 3", "fail: 0"),
    )
    require_result(
        "violating fixtures",
        apply(kyverno, "violating.yaml"),
        1,
        (
            "Secrets must declare an approved platform.gitops/secret-source annotation.",
            "Workloads must set CPU and memory requests for every container.",
            "Pods must opt into non-root execution.",
            "fail: 3",
        ),
    )
    require_result(
        "privilege-escalation fixture",
        apply(
            kyverno,
            "privilege-escalation.yaml",
            (POLICY_ROOT / "require-pod-security-baseline.yaml",),
        ),
        1,
        ("Containers must explicitly disable privilege escalation.", "fail: 1"),
    )
    require_result(
        "privileged namespace fixture",
        apply(
            kyverno,
            "privileged-namespace.yaml",
            (POLICY_ROOT / "require-pod-security-baseline.yaml",),
        ),
        0,
        ("fail: 0",),
    )
    with tempfile.TemporaryDirectory(prefix="platform-kyverno-image-policy-") as temp_dir:
        image_policy = render_image_policy(Path(temp_dir) / "verify-platform-images.yaml")
        require_result(
            "stable image validating policy",
            apply(kyverno, "compliant.yaml", (image_policy,)),
            0,
            ("pass: 0", "fail: 0", "skip: 0"),
        )

    print("Active Kyverno CEL and image policy verification passed with Kyverno CLI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
