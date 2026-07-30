#!/usr/bin/env python3
"""Self-test bounded HTTP handling across first-party production clients."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import mock
from urllib.request import ProxyHandler, Request

import http_transport
from http_transport import (
    DEFAULT_HTTP_REQUEST_MAX_BYTES,
    DEFAULT_HTTP_RESPONSE_MAX_BYTES,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    HTTP_REQUEST_LIMIT_ENV,
    HTTP_RESPONSE_LIMIT_ENV,
    HTTP_TIMEOUT_ENV,
    HttpRedirectRejected,
    HttpRequestTooLarge,
    HttpResponseTooLarge,
    HttpTransportPolicyError,
    RejectRedirectHandler,
    http_request_limit_bytes,
    http_response_limit_bytes,
    http_timeout_seconds,
    open_http_request,
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
REDIRECT_SAFE_CLIENTS = (
    *BOUNDED_RESPONSE_CLIENTS,
    "run_forgejo_recovery_drill.py",
)
FORBIDDEN_URLLIB_TRANSPORT_NAMES = {
    "HTTPRedirectHandler",
    "ProxyHandler",
    "build_opener",
    "urlopen",
}


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.requests: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset:end]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


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


def forbidden_transport_imports(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
            if any(
                alias.name in FORBIDDEN_URLLIB_TRANSPORT_NAMES
                for alias in node.names
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
            alias.name == "urllib.request" for alias in node.names
        ):
            lines.append(node.lineno)
    return lines


def forbidden_transport_calls(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {
            "build_opener",
            "urlopen",
        }:
            lines.append(node.lineno)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "build_opener",
            "urlopen",
        }:
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
        for line in forbidden_transport_imports(tree):
            failures.append(
                f"{path.relative_to(ROOT)}:{line}: direct urllib transport import"
            )
        for line in forbidden_transport_calls(tree):
            failures.append(
                f"{path.relative_to(ROOT)}:{line}: direct urllib transport call"
            )
    if failures:
        raise AssertionError("\n".join(failures))


def test_direct_transport_detection() -> None:
    import_cases = (
        "from urllib.request import urlopen as open_url\n",
        "from urllib.request import build_opener as make_opener\n",
        "import urllib.request as request_api\n",
    )
    for source in import_cases:
        tree = ast.parse(source)
        if not forbidden_transport_imports(tree):
            raise AssertionError(f"direct urllib transport import was missed: {source!r}")

    call_cases = (
        "urlopen(request, timeout=10)\n",
        "urllib.request.urlopen(request, timeout=10)\n",
        "build_opener().open(request, timeout=10)\n",
    )
    for source in call_cases:
        tree = ast.parse(source)
        if not forbidden_transport_calls(tree):
            raise AssertionError(f"direct urllib transport call was missed: {source!r}")


def test_shared_policy_adoption() -> None:
    for name in BOUNDED_RESPONSE_CLIENTS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        if "read_bounded_response" not in text or "http_timeout_seconds" not in text:
            raise AssertionError(f"{name} does not use the shared HTTP response policy")
    for name in BOUNDED_CLI_RESPONSE_CLIENTS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        if "require_bounded_text(result.stdout)" not in text:
            raise AssertionError(f"{name} does not size-check GitHub CLI responses")
    for name in REDIRECT_SAFE_CLIENTS:
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        if "open_http_request" not in text:
            raise AssertionError(f"{name} bypasses the shared redirect policy")


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


def test_request_limit_policy() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        assert http_request_limit_bytes() == DEFAULT_HTTP_REQUEST_MAX_BYTES
    with mock.patch.dict(os.environ, {HTTP_REQUEST_LIMIT_ENV: "1024"}, clear=True):
        assert http_request_limit_bytes() == 1024
    for value in ("0", "-1", "1.5", "67108865", "not-an-integer"):
        with mock.patch.dict(os.environ, {HTTP_REQUEST_LIMIT_ENV: value}, clear=True):
            try:
                http_request_limit_bytes()
            except HttpTransportPolicyError:
                pass
            else:
                raise AssertionError(f"unsafe HTTP request limit was accepted: {value}")


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


def test_redirect_policy_rejects_before_forwarding() -> None:
    request = Request(
        "https://user:password@api.example.test/private?access_token=secret",
        headers={"Authorization": "Bearer secret"},
    )
    handler = RejectRedirectHandler()
    for code in (301, 302, 303, 307, 308):
        response = FakeResponse(b"redirect body")
        try:
            handler.redirect_request(
                request,
                response,
                code,
                "redirect",
                {"Location": "https://attacker.example.test/capture"},
                "https://attacker.example.test/capture",
            )
        except HttpRedirectRejected as exc:
            assert exc.code == code
            assert exc.url == "https://api.example.test/"
            assert exc.fp is response
            assert "secret" not in str(exc)
            assert "password" not in str(exc)
            assert "attacker" not in str(exc)
        else:
            raise AssertionError(f"HTTP {code} redirect was followed")


def test_shared_opener_policy() -> None:
    request = Request("https://api.example.test/resource")
    opener = mock.Mock()
    opener.open.return_value = object()
    with mock.patch.object(http_transport, "build_opener", return_value=opener) as build:
        result = open_http_request(request, timeout=12.5)
    assert result is opener.open.return_value
    opener.open.assert_called_once_with(request, timeout=12.5)
    assert any(
        isinstance(handler, RejectRedirectHandler)
        for handler in build.call_args.args
    )

    direct_opener = mock.Mock()
    direct_opener.open.return_value = object()
    with mock.patch.object(
        http_transport,
        "build_opener",
        return_value=direct_opener,
    ) as direct_build:
        open_http_request(
            request,
            timeout=10,
            use_environment_proxy=False,
        )
    assert any(isinstance(handler, ProxyHandler) for handler in direct_build.call_args.args)
    assert any(
        isinstance(handler, RejectRedirectHandler)
        for handler in direct_build.call_args.args
    )

    for timeout in (True, 0, -1, float("nan"), float("inf"), 301):
        try:
            open_http_request(request, timeout=timeout)
        except HttpTransportPolicyError:
            pass
        else:
            raise AssertionError(f"unsafe explicit HTTP timeout was accepted: {timeout}")

    try:
        open_http_request(request, timeout=10, use_environment_proxy="false")  # type: ignore[arg-type]
    except HttpTransportPolicyError:
        pass
    else:
        raise AssertionError("non-boolean proxy policy was accepted")


def test_request_safety_policy() -> None:
    def expect_rejected(request: Request, message: str) -> None:
        with mock.patch.object(http_transport, "build_opener") as build:
            try:
                open_http_request(request, timeout=10)
            except HttpTransportPolicyError as exc:
                assert message in str(exc)
                assert "do-not-leak" not in str(exc)
            else:
                raise AssertionError(f"unsafe HTTP request was accepted: {request.full_url}")
        build.assert_not_called()

    for header in ("Authorization", "Cookie", "PRIVATE-TOKEN", "X-API-Key"):
        expect_rejected(
            Request(
                "http://api.example.test/resource",
                headers={header: "do-not-leak"},
            ),
            "require HTTPS",
        )

    expect_rejected(
        Request("https://user:do-not-leak@api.example.test/resource"),
        "must not embed credentials",
    )
    alternate_credential_key = "refresh" + "-" + "token"
    for url in (
        "https://api.example.test/resource?access%5Ftoken=do-not-leak",
        f"https://api.example.test/resource?page=1;{alternate_credential_key}=do-not-leak",
    ):
        expect_rejected(Request(url), "must not carry credentials")
    expect_rejected(
        Request("ftp://api.example.test/resource"),
        "absolute http or https",
    )
    expect_rejected(
        Request("https://api.example.test/?" + "&".join(f"p{i}=x" for i in range(257))),
        "exceeds 256 fields",
    )
    expect_rejected(
        Request("https://api.example.test/" + ("x" * (16 * 1024))),
        "URL exceeds",
    )
    expect_rejected(
        Request("https://api.example.test/", headers={"X-Large": "x" * (64 * 1024)}),
        "headers exceed",
    )

    with mock.patch.dict(
        os.environ,
        {HTTP_REQUEST_LIMIT_ENV: "4"},
        clear=True,
    ):
        expect_rejected(
            Request("https://api.example.test/", data=b"12345"),
            "body exceeds",
        )

    opener = mock.Mock()
    opener.open.return_value = object()
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(http_transport, "build_opener", return_value=opener),
    ):
        open_http_request(
            Request("http://api.example.test/healthz?page=1"),
            timeout=10,
            use_environment_proxy=False,
        )
        open_http_request(
            Request(
                "https://api.example.test/resource?page=1",
                data=b"{}",
                headers={"Authorization": "Bearer do-not-leak"},
            ),
            timeout=10,
        )
    assert opener.open.call_count == 2


def main() -> int:
    test_static_http_contract()
    test_direct_transport_detection()
    test_shared_policy_adoption()
    test_timeout_policy()
    test_request_limit_policy()
    test_response_limit_policy()
    test_bounded_response_reader()
    test_bounded_text_uses_encoded_bytes()
    test_redirect_policy_rejects_before_forwarding()
    test_shared_opener_policy()
    test_request_safety_policy()
    print("HTTP transport contract self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
