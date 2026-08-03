#!/usr/bin/env python3
"""Parse JSON without duplicate keys or non-finite numeric values."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any, NoReturn


MAX_JSON_DEPTH = 128
MAX_JSON_NODES = 1_000_000


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


def _validate_structure(document: Any) -> None:
    pending: list[Iterator[Any]] = [iter((document,))]
    node_count = 0
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue

        node_count += 1
        if isinstance(value, dict):
            node_count += len(value)
            children = iter(value.values())
        elif isinstance(value, list):
            children = iter(value)
        else:
            if node_count > MAX_JSON_NODES:
                raise StrictJsonError("JSON structure exceeds the node limit")
            continue

        if node_count > MAX_JSON_NODES:
            raise StrictJsonError("JSON structure exceeds the node limit")
        if len(pending) > MAX_JSON_DEPTH:
            raise StrictJsonError("JSON structure exceeds the nesting limit")
        pending.append(children)


def loads_strict_json(document: str | bytes | bytearray) -> Any:
    """Decode standards-compliant JSON with deterministic object semantics."""
    try:
        decoded = json.loads(
            document,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_standard_constant,
            parse_float=_finite_float,
        )
    except RecursionError as exc:
        raise StrictJsonError("JSON structure exceeds the decoder nesting limit") from exc
    _validate_structure(decoded)
    return decoded
