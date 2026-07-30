#!/usr/bin/env python3
"""Parse JSON without duplicate keys or non-finite numeric values."""

from __future__ import annotations

import json
import math
from typing import Any, NoReturn


class StrictJsonError(json.JSONDecodeError):
    """Raised when otherwise accepted JSON is ambiguous or non-standard."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "", 0)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def _reject_non_standard_constant(_value: str) -> NoReturn:
    raise StrictJsonError("non-standard JSON numeric constants are not allowed")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StrictJsonError("JSON numbers must remain finite")
    return parsed


def loads_strict_json(document: str | bytes | bytearray) -> Any:
    """Decode standards-compliant JSON with deterministic object semantics."""
    return json.loads(
        document,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_standard_constant,
        parse_float=_finite_float,
    )
