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


def scan(text: str, include_internal_markers: bool = True, path: str = "fixture.yaml") -> list[str]:
    return [
        message
        for _, message in scanner.scan_text(
            Path(path),
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
    default_rke2_pod_cidr = ".".join(("10", "42", "0", "0")) + "/16"
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
    assert_clean(f"podCIDR: {default_rke2_pod_cidr}\n")
    assert_problem(f"vip: {private_ip}\n", "private IP-like value")
    assert_problem(f"ansible_user: {private_user}\n", "private node username")
    assert_problem(f"password: {fake_secret}\n", "possible plaintext secret")
    assert_clean("secret = kube.json('get', 'secret', secret_name)\n")
    assert_clean("secret:\n  secretName: platform-managed-secret\n")
    assert_clean("privateKey:\n  algorithm: ECDSA\n  rotationPolicy: Always\n")
    for schema_identifier in (
        "repository-secret:DEPLOY_KEY",
        "environment-secret:REGISTRY_TOKEN",
        "organization-secret:SIGNING_KEY",
    ):
        assert_clean(f"source: {schema_identifier}\n")

    # Assemble fixture assignments so this test file does not itself contain
    # a fake plaintext credential that the repository scanner should reject.
    key = "password"
    for value in ("runtime_password", "self.admin_password", "credentials.admin.password"):
        for expression in (f"{key} = {value}", f"client({key}={value})",
                           f"{key}: str = {value}", f"({key} := {value})"):
            if scan(expression + "\n", path="fixture.py"):
                raise AssertionError("unquoted Python reference was treated as a literal")
        for quote in ("'", '"'):
            problems = scan(f"{key} = {quote}{value}{quote}\n", path="fixture.py")
            if not any("possible plaintext secret" in problem for problem in problems):
                raise AssertionError("quoted reference-looking literal was exempted")
        for suffix in ("yaml", "md", "sh"):
            problems = scan(f"{key}: {value}\n", path="fixture." + suffix)
            if not any("possible plaintext secret" in problem for problem in problems):
                raise AssertionError("non-Python literal was exempted")
        problems = scan(f"broken(\n{key}={value}\n", path="fixture.py")
        if not any("possible plaintext secret" in problem for problem in problems):
            raise AssertionError("invalid Python bypassed conservative scanning")
    # Unicode before a reference on the same line changes AST byte columns.
    prefix = 'label = "caf' + chr(233) + ' example.com"; '
    reference = f"client({key}=self.admin_password)"
    if scan(prefix + reference + "\n", path="fixture.py"):
        raise AssertionError("reference offsets changed after Unicode or allow-fragment masking")
    for separator in (chr(0x85), chr(0x2028), chr(0x2029)):
        unicode_line = 'label = "' + separator + '"; ' + reference
        if scan(unicode_line + "\n", path="fixture.py"):
            raise AssertionError("Unicode string separator changed Python line offsets")
    for newline in ("\n", "\r\n", "\r"):
        multiline = "label = 1" + newline + reference + newline
        if scan(multiline, path="fixture.py"):
            raise AssertionError("Python line ending changed reference offsets")
    mixed = prefix + reference + f'; client({key}="{fake_secret}")\n'
    problems = scan(mixed, path="fixture.py")
    if len(problems) != 1 or "possible plaintext secret at line 1" not in problems[0]:
        raise AssertionError("literal credential next to a reference was exempted")
    for expression in (f'{key} = "{fake_secret}"', f'# {key}={fake_secret}',
                       f'notice = """{key}={fake_secret}"""',
                       f'client({key}="{fake_secret}")'):
        if not any("possible plaintext secret" in p for p in scan(expression + "\n", path="fixture.py")):
            raise AssertionError("literal in Python source was exempted")
    # Masking must not fuse previously separated text into a different token.
    masked = scanner.scan_text(Path("fixture.yaml"), "token: <TOKEN>\n" + f"{key}: {fake_secret}\n")
    if len(masked) != 1 or "line 2" not in masked[0][1]:
        raise AssertionError("placeholder masking changed line attribution")

    if scanner.should_scan(root / ".shell-syntax-leftover" / "script.sh"):
        raise AssertionError("expected stale shell syntax temp directories to be skipped")
    if scanner.should_scan(root / ".ansible-shell-syntax-leftover" / "block.sh"):
        raise AssertionError("expected stale Ansible shell syntax temp directories to be skipped")
    if scanner.should_scan(root / "scripts" / "__pycache__" / "validate_no_secrets.pyc"):
        raise AssertionError("expected Python bytecode cache directories to be skipped")
    for local_dir in ("private", "rendered", "secrets"):
        if scanner.should_scan(root / local_dir / "fixture.yaml"):
            raise AssertionError(f"expected {local_dir} directory to be skipped")
    if scanner.should_scan(root / "gitops/apps/example/charts/vendor/README.md"):
        raise AssertionError("expected vendored chart README files to be skipped")
    for relative_path in scanner.vendored_document_exceptions:
        if scanner.should_scan(root / relative_path):
            raise AssertionError(
                f"expected reviewed vendored documentation to be skipped: {relative_path}"
            )
    if not scanner.should_scan(root / "gitops/apps/example/charts/vendor/EXAMPLES.md"):
        raise AssertionError("expected unlisted vendored example documentation to remain scanned")
    if not scanner.should_scan(root / "docs/Changelog.md"):
        raise AssertionError("expected first-party changelog files to remain scanned")
    if not scanner.should_scan(root / "docs/README.md"):
        raise AssertionError("expected first-party README files to remain scanned")
    if not scanner.should_scan(root / "gitops/apps/example/charts/vendor/credentials.txt"):
        raise AssertionError("expected non-README vendored chart files to remain scanned")
    manual = scanner.gitea_yq_manual
    citation = scanner.yaml_boolean_citation + "\n"
    if not scanner.should_scan(root / manual) or scan(citation, path=manual):
        raise AssertionError("only the reviewed citation should be exempt in the vendored manual")
    for path, text in ((manual, citation + f"vip: {private_ip}\n"), ("docs/example.md", citation)):
        if not any("private IP-like value" in problem for problem in scan(text, path=path)):
            raise AssertionError("citation masking exempted unrelated content")

    print("Secret/privacy scanner self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
