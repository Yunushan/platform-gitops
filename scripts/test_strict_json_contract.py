#!/usr/bin/env python3
"""Validate unambiguous first-party JSON parsing."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import strict_json  # noqa: E402
from strict_json import StrictJsonError, loads_strict_json  # noqa: E402


def production_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.rglob("*.py")
        if not path.name.startswith("test_") and path.name != "strict_json.py"
    )


def direct_json_parser_calls(document: ast.Module) -> list[ast.Call]:
    module_aliases = {"json"}
    function_aliases: set[str] = set()
    parser_names = {"JSONDecoder", "load", "loads"}
    for node in ast.walk(document):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            for alias in node.names:
                if alias.name in parser_names:
                    function_aliases.add(alias.asname or alias.name)

    calls: list[ast.Call] = []
    for node in ast.walk(document):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in parser_names
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in function_aliases
        ):
            calls.append(node)
    return calls


def test_valid_documents() -> None:
    expected = {"items": [{"name": "first"}, {"name": "second"}], "ratio": 1.25}
    for document in (
        '{"items":[{"name":"first"},{"name":"second"}],"ratio":1.25}',
        b'{"items":[{"name":"first"},{"name":"second"}],"ratio":1.25}',
        bytearray(b'{"items":[{"name":"first"},{"name":"second"}],"ratio":1.25}'),
    ):
        if loads_strict_json(document) != expected:
            raise AssertionError("strict JSON decoder changed a valid document")


def test_duplicate_keys_are_rejected() -> None:
    for document in (
        '{"duplicate":"sensitive-value","duplicate":2}',
        '{"nested":{"duplicate":"sensitive-value","duplicate":2}}',
    ):
        try:
            loads_strict_json(document)
        except StrictJsonError as exc:
            message = str(exc)
            if "duplicate" not in message:
                raise AssertionError("duplicate-key rejection lost its classification")
            if "sensitive-value" in message or '"duplicate"' in message:
                raise AssertionError("duplicate-key rejection leaked document content")
        else:
            raise AssertionError("strict JSON decoder accepted duplicate object keys")


def test_non_finite_numbers_are_rejected() -> None:
    for document in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
        try:
            loads_strict_json(document)
        except StrictJsonError:
            pass
        else:
            raise AssertionError(f"strict JSON decoder accepted non-finite input: {document}")


def test_standard_syntax_errors_are_preserved() -> None:
    for document in ("", "{", "{} trailing"):
        try:
            loads_strict_json(document)
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError(f"strict JSON decoder accepted invalid syntax: {document!r}")


def test_structure_limits_are_enforced() -> None:
    with mock.patch.object(strict_json, "MAX_JSON_DEPTH", 2):
        try:
            loads_strict_json('{"first":{"second":{"third":true}}}')
        except StrictJsonError as exc:
            if "nesting" not in str(exc):
                raise AssertionError("JSON depth rejection lost its classification")
        else:
            raise AssertionError("strict JSON decoder accepted excessive nesting")

    with mock.patch.object(strict_json, "MAX_JSON_NODES", 3):
        try:
            loads_strict_json("[0,1,2]")
        except StrictJsonError as exc:
            if "node limit" not in str(exc):
                raise AssertionError("JSON node rejection lost its classification")
        else:
            raise AssertionError("strict JSON decoder accepted too many nodes")


def test_decoder_recursion_is_classified() -> None:
    with mock.patch.object(strict_json.json, "loads", side_effect=RecursionError):
        try:
            loads_strict_json("[]")
        except StrictJsonError as exc:
            if "decoder nesting" not in str(exc):
                raise AssertionError("decoder recursion rejection lost its classification")
        else:
            raise AssertionError("strict JSON decoder leaked RecursionError")


def test_production_parsers_use_shared_policy() -> None:
    direct_calls: list[str] = []
    strict_calls = 0
    for path in production_python_files():
        document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in direct_json_parser_calls(document):
            direct_calls.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        for node in ast.walk(document):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "loads_strict_json"
            ):
                strict_calls += 1

    if direct_calls:
        raise AssertionError("direct production JSON parsing remains:\n" + "\n".join(direct_calls))
    if strict_calls < 30:
        raise AssertionError(f"strict JSON scan covered too few calls: {strict_calls}")


def test_direct_parser_detection() -> None:
    for source in (
        'import json\njson.loads("{}")',
        'import json as codec\ncodec.load(stream)',
        'from json import loads\nloads("{}")',
        'from json import load as decode\ndecode(stream)',
        'from json import JSONDecoder\nJSONDecoder()',
        'import json as codec\ncodec.JSONDecoder()',
    ):
        document = ast.parse(source)
        if not direct_json_parser_calls(document):
            raise AssertionError(f"direct JSON parser escaped static detection: {source}")


def main() -> int:
    test_valid_documents()
    test_duplicate_keys_are_rejected()
    test_non_finite_numbers_are_rejected()
    test_standard_syntax_errors_are_preserved()
    test_structure_limits_are_enforced()
    test_decoder_recursion_is_classified()
    test_direct_parser_detection()
    test_production_parsers_use_shared_policy()
    print("Strict first-party JSON parsing contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
