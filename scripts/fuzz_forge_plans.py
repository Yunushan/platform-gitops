#!/usr/bin/env python3
"""Pure-Python fuzz surface for forge migration plan parsers."""

from __future__ import annotations

import copy
import json
from typing import Any

import forge_cutover
import forge_migration
import forge_transition
from strict_json import loads_strict_json


MAX_INPUT_BYTES = 128 * 1024
MAX_STRUCTURE_DEPTH = 64


def structure_is_bounded(value: Any, depth: int = 0) -> bool:
    if depth > MAX_STRUCTURE_DEPTH:
        return False
    if isinstance(value, dict):
        return all(
            structure_is_bounded(key, depth + 1) and structure_is_bounded(child, depth + 1)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return all(structure_is_bounded(child, depth + 1) for child in value)
    return True


def exercise_input(data: bytes) -> None:
    if len(data) > MAX_INPUT_BYTES:
        return
    try:
        decoded = data.decode("utf-8")
        plan = loads_strict_json(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return
    if not isinstance(plan, dict) or not structure_is_bounded(plan):
        return

    parsers = (
        forge_migration.parse_plan,
        forge_cutover.parse_cutover_plan,
        forge_transition.parse_transition_plan,
    )
    for parser in parsers:
        try:
            parser(copy.deepcopy(plan))
        except forge_migration.MigrationError:
            pass
