#!/usr/bin/env python3
"""Shared bounded timeout policy for first-party child processes."""

from __future__ import annotations

import math
import os


GLOBAL_TIMEOUT_ENV = "PLATFORM_SUBPROCESS_TIMEOUT_SECONDS"
MAX_TIMEOUT_SECONDS = 86_400.0


def bounded_timeout_seconds(
    default: float,
    specific_env: str | None = None,
) -> float:
    """Return a finite positive timeout with a one-day hard ceiling."""
    selected_name = "default"
    raw_value = str(default)
    if specific_env and os.environ.get(specific_env, "").strip():
        selected_name = specific_env
        raw_value = os.environ[specific_env].strip()
    elif os.environ.get(GLOBAL_TIMEOUT_ENV, "").strip():
        selected_name = GLOBAL_TIMEOUT_ENV
        raw_value = os.environ[GLOBAL_TIMEOUT_ENV].strip()

    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{selected_name} must be a number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"{selected_name} must be greater than zero and no more than "
            f"{int(MAX_TIMEOUT_SECONDS)} seconds"
        )
    return timeout


def timeout_stream_text(value: str | bytes | None) -> str:
    """Normalize partial TimeoutExpired output for deterministic diagnostics."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
