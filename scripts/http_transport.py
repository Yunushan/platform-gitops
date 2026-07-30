#!/usr/bin/env python3
"""Shared timeout and response-size policy for first-party HTTP clients."""

from __future__ import annotations

import math
import os
from typing import Any, BinaryIO
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


HTTP_TIMEOUT_ENV = "PLATFORM_HTTP_TIMEOUT_SECONDS"
HTTP_RESPONSE_LIMIT_ENV = "PLATFORM_HTTP_RESPONSE_MAX_BYTES"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
MAX_HTTP_TIMEOUT_SECONDS = 300.0
DEFAULT_HTTP_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 64 * 1024 * 1024


class HttpTransportPolicyError(ValueError):
    """Raised when HTTP transport configuration or a response is unsafe."""


class HttpResponseTooLarge(HttpTransportPolicyError):
    """Raised when an HTTP response exceeds the configured byte limit."""


class HttpRedirectRejected(HTTPError):
    """Raised before following an HTTP redirect to another request target."""


class RejectRedirectHandler(HTTPRedirectHandler):
    """Fail closed instead of forwarding request headers through redirects."""

    def redirect_request(
        self,
        request: Request,
        response: BinaryIO,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        del new_url
        raise HttpRedirectRejected(
            _request_origin(request.full_url),
            code,
            "HTTP redirect rejected by transport policy",
            headers,
            response,
        )


def _request_origin(url: str) -> str:
    """Return an origin-only URL so transport errors cannot expose query data."""
    parts = urlsplit(url)
    hostname = parts.hostname or "invalid"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    authority = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit((parts.scheme or "https", authority, "/", "", ""))


def _validated_timeout(raw_value: object, label: str) -> float:
    if isinstance(raw_value, bool):
        raise HttpTransportPolicyError(f"{label} must be a number of seconds")
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise HttpTransportPolicyError(
            f"{label} must be a number of seconds"
        ) from exc
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_HTTP_TIMEOUT_SECONDS
    ):
        raise HttpTransportPolicyError(
            f"{label} must be greater than zero and no more than "
            f"{int(MAX_HTTP_TIMEOUT_SECONDS)} seconds"
        )
    return timeout


def http_timeout_seconds(default: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> float:
    """Return a finite positive HTTP timeout with a five-minute hard ceiling."""
    raw_value = os.environ.get(HTTP_TIMEOUT_ENV, "").strip() or str(default)
    return _validated_timeout(raw_value, HTTP_TIMEOUT_ENV)


def open_http_request(
    request: Request,
    *,
    timeout: float | None = None,
    use_environment_proxy: bool = True,
) -> Any:
    """Open one bounded request while rejecting every redirect response."""
    selected_timeout = (
        http_timeout_seconds()
        if timeout is None
        else _validated_timeout(timeout, "explicit HTTP timeout")
    )
    if not isinstance(use_environment_proxy, bool):
        raise HttpTransportPolicyError(
            "use_environment_proxy must be a boolean"
        )
    handlers: tuple[Any, ...] = (RejectRedirectHandler(),)
    if not use_environment_proxy:
        handlers = (ProxyHandler({}), *handlers)
    return build_opener(*handlers).open(request, timeout=selected_timeout)


def http_response_limit_bytes(
    default: int = DEFAULT_HTTP_RESPONSE_MAX_BYTES,
) -> int:
    """Return a positive response limit with a 64 MiB hard ceiling."""
    configured = os.environ.get(HTTP_RESPONSE_LIMIT_ENV, "").strip()
    raw_value = configured or str(default)
    label = HTTP_RESPONSE_LIMIT_ENV if configured else "default HTTP response limit"
    return _validated_response_limit(raw_value, label)


def _validated_response_limit(raw_value: str, label: str) -> int:
    """Validate a configured or explicit response byte limit."""
    try:
        limit = int(raw_value, 10)
    except ValueError as exc:
        raise HttpTransportPolicyError(
            f"{label} must be an integer number of bytes"
        ) from exc
    if limit <= 0 or limit > MAX_HTTP_RESPONSE_BYTES:
        raise HttpTransportPolicyError(
            f"{label} must be greater than zero and no more than "
            f"{MAX_HTTP_RESPONSE_BYTES} bytes"
        )
    return limit


def _selected_response_limit(max_bytes: int | None) -> int:
    if max_bytes is None:
        return http_response_limit_bytes()
    return _validated_response_limit(str(max_bytes), "explicit HTTP response limit")


def require_bounded_payload(
    payload: bytes | bytearray,
    max_bytes: int | None = None,
) -> bytes:
    """Return bytes after enforcing the configured response-size policy."""
    limit = _selected_response_limit(max_bytes)
    value = bytes(payload)
    if len(value) > limit:
        raise HttpResponseTooLarge(
            f"HTTP response exceeds the configured limit of {limit} bytes"
        )
    return value


def require_bounded_text(text: str, max_bytes: int | None = None) -> str:
    """Reject already-captured text that exceeds the response byte limit."""
    require_bounded_payload(text.encode("utf-8"), max_bytes=max_bytes)
    return text


def read_bounded_response(
    response: BinaryIO,
    max_bytes: int | None = None,
) -> bytes:
    """Read at most one byte beyond the limit and reject oversized responses."""
    limit = _selected_response_limit(max_bytes)
    remaining = limit + 1
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = response.read(remaining)
        if not isinstance(chunk, (bytes, bytearray)):
            raise HttpTransportPolicyError("HTTP response reader did not return bytes")
        if not chunk:
            break
        value = bytes(chunk)
        chunks.append(value)
        remaining -= len(value)
    return require_bounded_payload(b"".join(chunks), max_bytes=limit)
