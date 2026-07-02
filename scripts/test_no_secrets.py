#!/usr/bin/env python3
from pathlib import Path
import importlib.util
import sys

root = Path(__file__).resolve().parents[1]
module_path = root / "scripts/validate_no_secrets.py"
spec = importlib.util.spec_from_file_location("validate_no_secrets", module_path)
scanner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(scanner)


def scan(text: str, include_internal_markers: bool = True) -> list[str]:
    return [
        message
        for _, message in scanner.scan_text(
            Path("fixture.yaml"),
            text,
            include_internal_markers=include_internal_markers,
        )
    ]


def assert_clean(text: str, include_internal_markers: bool = True) -> None:
    problems = scan(text, include_internal_markers=include_internal_markers)
    if problems:
        raise AssertionError(f"expected clean fixture, got: {problems}")


def assert_problem(text: str, expected: str) -> None:
    problems = scan(text)
    if not any(expected in problem for problem in problems):
        raise AssertionError(f"expected {expected!r}, got: {problems}")


def main() -> int:
    private_host = "gitops-" + "arge." + "is" + "bak.com.tr"
    argocd_host = "argocd-" + "gitops-" + "arge." + "is" + "bak.com.tr"
    private_ip = "172.16." + "134.47"
    private_user = "git" + "lab1"
    fake_secret = "real" + "Secret" + "123"

    assert_clean(
        """
host: forgejo.<PLATFORM_DOMAIN>
externalURL: https://example.com
password: <GENERATE_WITH_PASSWORD_MANAGER>
token: ${PLATFORM_TOKEN}
"""
    )
    assert_problem(f"host: {private_host}\n", "company domain fragment")
    assert_problem(f"host: {argocd_host}\n", "private deployment hostname")
    assert_clean(f"host: {private_host}\n", include_internal_markers=False)
    assert_problem(f"vip: {private_ip}\n", "private IP-like value")
    assert_problem(f"ansible_user: {private_user}\n", "private node username")
    assert_problem(f"password: {fake_secret}\n", "possible plaintext secret")
    if scanner.should_scan(root / ".shell-syntax-leftover" / "script.sh"):
        raise AssertionError("expected stale shell syntax temp directories to be skipped")
    if scanner.should_scan(root / ".ansible-shell-syntax-leftover" / "block.sh"):
        raise AssertionError("expected stale Ansible shell syntax temp directories to be skipped")
    if scanner.should_scan(root / "scripts" / "__pycache__" / "validate_no_secrets.pyc"):
        raise AssertionError("expected Python bytecode cache directories to be skipped")
    for local_dir in ("private", "rendered", "secrets"):
        if scanner.should_scan(root / local_dir / "fixture.yaml"):
            raise AssertionError(f"expected {local_dir} directory to be skipped")

    print("Secret/privacy scanner self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
