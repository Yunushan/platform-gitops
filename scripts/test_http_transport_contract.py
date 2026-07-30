#!/usr/bin/env python3
"""Self-test bounded HTTP handling across first-party production clients."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import mock

from http_transport import (
    DEFAULT_HTTP_RESPONSE_MAX_BYTES,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    HTTP_RESPONSE_LIMIT_ENV,
    HTTP_TIMEOUT_ENV,
    HttpResponseTooLarge,
    HttpTransportPolicyError,
    http_response_limit_bytes,
    http_timeout_seconds,
    read_bounded_response,
    require_bounded_text,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "http_transport.py"
BOUNDED_RESPONSE_CLIENTS = (
    "configure_github_governance.py",
    "forge_cutover.py",
    "forge_migration.py",
    "verify_github_governance.py",
    "verify_github_release_approval.py",
    "verify_github_release_ref.py",
)
BOUNDED_CLI_RESPONSE_CLIENTS = (
    "configure_github_governance.py",
    "verify_github_governance.py",
    "verify_github_release_approval.py",
)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.requests: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset:end]
        self.offset += len(chunk)
        return chunk


class ChunkedResponse(FakeResponse):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 2) if size >= 0 else 2)


def production_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.glob("*.py")
        if not path.name.startswith("test_") and path != HELPER
    )


def is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def direct_response_reads(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "read":
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id in {"response", "exc"}:
            lines.append(node.lineno)
    return lines


def json_load_response_calls(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 1:
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and is_name(node.func.value, "json")
            and node.func.attr == "load"
        ):
            continue
        if is_name(node.args[0], "response"):
            lines.append(node.lineno)
    return lines


def urlopen_calls_without_timeout(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not is_name(node.func, "urlopen"):
            continue
        timeout = next((item.value for item in node.keywords if item.arg == "timeout"), None)
        if timeout is None or (isinstance(timeout, ast.Constant) and timeout.value is None):
            lines.append(node.lineno)
    return lines


def opener_calls_without_timeout(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not (
            node.func.attr == "open"
            and isinstance(owner, ast.Call)
            and is_name(owner.func, "build_opener")
        ):
            continue
        timeout = next((item.value for item in node.keywords if item.arg == "timeout"), None)
        if timeout is None or (isinstance(timeout, ast.Constant) and timeout.value is None):
            lines.append(node.lineno)
    return lines


def test_static_http_contract() -> None:
    failures: list[str] = []
    for path in production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in direct_response_reads(tree):
            failures.append(f"{path.relative_to(ROOT)}:{line}: direct HTTP response read")
        for line in json_load_response_calls(tree):
            failures.append(f"{path.relative_to(ROOT)}:{line}: unbounded json.load(response)")
        for line in urlopen_calls_without_timeout(tree):
            failures.append(f"{path.relative_to(ROOT)}:{line}: urlopen lacks timeout")
        for line in opener_calls_without_timeout(tree):
            failures.append(f"{path.relative_to(ROOT)}:{line}: opener.open lacks timeout")
    if failures:
        raise AssertionError("\n".join(failures))


def test_shared_policy_adoption() -> None:
    for name in BOUNDED_RESPONSE_CLIENTS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        if "read_bounded_response" not in text or "http_timeout_seconds" not in text:
            raise AssertionError(f"{name} does not use the shared HTTP response policy")
    for name in BOUNDED_CLI_RESPONSE_CLIENTS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        if "require_bounded_text(result.stdout)" not in text:
            raise AssertionError(f"{name} does not size-check GitHub CLI responses")


def test_timeout_policy() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        assert http_timeout_seconds() == DEFAULT_HTTP_TIMEOUT_SECONDS
    with mock.patch.dict(os.environ, {HTTP_TIMEOUT_ENV: "45.5"}, clear=True):
        assert http_timeout_seconds() == 45.5
    for value in ("0", "-1", "nan", "inf", "301", "not-a-number"):
        with mock.patch.dict(os.environ, {HTTP_TIMEOUT_ENV: value}, clear=True):
            try:
                http_timeout_seconds()
            except HttpTransportPolicyError:
                pass
            else:
                raise AssertionError(f"unsafe HTTP timeout was accepted: {value}")


def test_response_limit_policy() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        assert http_response_limit_bytes() == DEFAULT_HTTP_RESPONSE_MAX_BYTES
    with mock.patch.dict(os.environ, {HTTP_RESPONSE_LIMIT_ENV: "1024"}, clear=True):
        assert http_response_limit_bytes() == 1024
    for value in ("0", "-1", "1.5", "67108865", "not-an-integer"):
        with mock.patch.dict(os.environ, {HTTP_RESPONSE_LIMIT_ENV: value}, clear=True):
            try:
                http_response_limit_bytes()
            except HttpTransportPolicyError:
                pass
            else:
                raise AssertionError(f"unsafe HTTP response limit was accepted: {value}")


def test_bounded_response_reader() -> None:
    with mock.patch.dict(os.environ, {HTTP_RESPONSE_LIMIT_ENV: "1024"}, clear=True):
        exact = FakeResponse(b"abcd")
        assert read_bounded_response(exact, max_bytes=4) == b"abcd"
        assert exact.requests[0] == 5

        oversized = FakeResponse(b"abcde")
        try:
            read_bounded_response(oversized, max_bytes=4)
        except HttpResponseTooLarge as exc:
            assert "4 bytes" in str(exc)
        else:
            raise AssertionError("oversized HTTP response was accepted")
        assert oversized.requests == [5]

        chunked = ChunkedResponse(b"abcd")
        assert read_bounded_response(chunked, max_bytes=4) == b"abcd"
        assert chunked.offset == 4


def test_bounded_text_uses_encoded_bytes() -> None:
    assert require_bounded_text("ab", max_bytes=2) == "ab"
    try:
        require_bounded_text("\u00e9", max_bytes=1)
    except HttpResponseTooLarge:
        pass
    else:
        raise AssertionError("UTF-8 response bytes were not counted")


def main() -> int:
    test_static_http_contract()
    test_shared_policy_adoption()
    test_timeout_policy()
    test_response_limit_policy()
    test_bounded_response_reader()
    test_bounded_text_uses_encoded_bytes()
    print("HTTP transport contract self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
