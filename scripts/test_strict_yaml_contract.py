#!/usr/bin/env python3
"""Validate deterministic, resource-bounded first-party YAML parsing."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import strict_yaml  # noqa: E402
from strict_yaml import StrictYamlError, loads_strict_yaml_all  # noqa: E402


YAML_PARSER_NAMES = {
    "full_load",
    "full_load_all",
    "load",
    "load_all",
    "safe_load",
    "safe_load_all",
    "unsafe_load",
    "unsafe_load_all",
}


def production_python_files() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.rglob("*.py")
        if not path.name.startswith("test_") and path.name != "strict_yaml.py"
    )


def direct_yaml_parser_calls(document: ast.Module) -> list[ast.Call]:
    module_aliases = {"yaml"}
    function_aliases: set[str] = set()
    for node in ast.walk(document):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "yaml":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "yaml":
            for alias in node.names:
                if alias.name in YAML_PARSER_NAMES:
                    function_aliases.add(alias.asname or alias.name)

    calls: list[ast.Call] = []
    for node in ast.walk(document):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in YAML_PARSER_NAMES
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in function_aliases
        ):
            calls.append(node)
    return calls


def expect_rejected(document: str, classification: str) -> None:
    try:
        loads_strict_yaml_all(document)
    except StrictYamlError as exc:
        if classification not in str(exc):
            raise AssertionError(
                f"YAML rejection lost classification {classification!r}: {exc}"
            ) from exc
    else:
        raise AssertionError(f"strict YAML decoder accepted {classification} input")


def test_valid_documents() -> None:
    expected_single = [{"value": True}]
    for document in (
        "value: true\n",
        b"value: true\n",
        bytearray(b"value: true\n"),
    ):
        if loads_strict_yaml_all(document) != expected_single:
            raise AssertionError("strict YAML decoder changed a supported input type")

    documents = loads_strict_yaml_all(
        """---
apiVersion: v1
kind: Pod
metadata:
  name: example
spec:
  containers:
    - name: main
      image: example.invalid/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
---
enabled: true
replicas: 3
ratio: 1.25
optional: null
"""
    )
    if len(documents) != 2:
        raise AssertionError("strict YAML decoder changed a valid multi-document stream")
    if documents[0]["metadata"]["name"] != "example":
        raise AssertionError("strict YAML decoder changed valid mapping content")
    if documents[1] != {"enabled": True, "replicas": 3, "ratio": 1.25, "optional": None}:
        raise AssertionError("strict YAML decoder changed valid scalar semantics")

    provider_documents = loads_strict_yaml_all(
        "on:\n  push: {}\nenabled: true\nlegacy: on\n",
        yaml_12=True,
    )
    if provider_documents != [{"on": {"push": {}}, "enabled": True, "legacy": "on"}]:
        raise AssertionError("YAML 1.2 provider mode did not preserve GitHub keys and values")


def test_duplicate_keys_are_rejected() -> None:
    for document in (
        "duplicate: sensitive-value\nduplicate: replacement\n",
        "outer:\n  duplicate: sensitive-value\n  duplicate: replacement\n",
    ):
        try:
            loads_strict_yaml_all(document)
        except StrictYamlError as exc:
            message = str(exc)
            if "duplicate" not in message:
                raise AssertionError("duplicate-key rejection lost its classification")
            if "sensitive-value" in message or "replacement" in message:
                raise AssertionError("duplicate-key rejection leaked document content")
        else:
            raise AssertionError("strict YAML decoder accepted duplicate mapping keys")


def test_anchors_and_aliases_are_rejected() -> None:
    expect_rejected("shared: &shared\n  value: one\ncopy: *shared\n", "anchors and aliases")


def test_non_json_types_are_rejected() -> None:
    for document in (
        "created: 2026-07-31\n",
        "payload: !!binary SGVsbG8=\n",
        "items: !!set\n  one: null\n",
        "1: numeric-key\n",
    ):
        expect_rejected(document, "YAML")


def test_non_finite_numbers_are_rejected() -> None:
    for document in ("value: .nan\n", "value: .inf\n", "value: -.inf\n"):
        expect_rejected(document, "finite")


def test_structure_limits_are_enforced() -> None:
    with mock.patch.object(strict_yaml, "MAX_YAML_DEPTH", 2):
        expect_rejected("first:\n  second:\n    third: true\n", "nesting")

    with mock.patch.object(strict_yaml, "MAX_YAML_NODES", 3):
        expect_rejected("items: [one, two, three]\n", "node limit")

    with mock.patch.object(strict_yaml, "MAX_YAML_DOCUMENTS", 1):
        expect_rejected("---\nfirst: true\n---\nsecond: true\n", "document limit")


def test_invalid_syntax_is_classified_without_content() -> None:
    try:
        loads_strict_yaml_all("marker: sentinel-redacted-value\nitems: [one, two\n")
    except StrictYamlError as exc:
        message = str(exc)
        if "invalid" not in message:
            raise AssertionError("invalid-YAML rejection lost its classification")
        if "sentinel-redacted-value" in message:
            raise AssertionError("invalid-YAML rejection leaked document content")
    else:
        raise AssertionError("strict YAML decoder accepted invalid syntax")


def test_decoder_recursion_is_classified() -> None:
    with mock.patch.object(strict_yaml.yaml, "load_all", side_effect=RecursionError):
        expect_rejected("[]", "decoder nesting")


def test_production_parsers_use_shared_policy() -> None:
    direct_calls: list[str] = []
    strict_calls = 0
    for path in production_python_files():
        document = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in direct_yaml_parser_calls(document):
            direct_calls.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        for node in ast.walk(document):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "loads_strict_yaml_all"
            ):
                strict_calls += 1

    if direct_calls:
        raise AssertionError("direct production YAML parsing remains:\n" + "\n".join(direct_calls))
    if strict_calls < 1:
        raise AssertionError(f"strict YAML scan covered too few calls: {strict_calls}")


def test_direct_parser_detection() -> None:
    for source in (
        'import yaml\nyaml.safe_load("value: true")',
        'import yaml as codec\ncodec.load_all("---")',
        'from yaml import safe_load\nsafe_load("value: true")',
        'from yaml import load as decode\ndecode("value: true")',
    ):
        document = ast.parse(source)
        if not direct_yaml_parser_calls(document):
            raise AssertionError(f"direct YAML parser escaped static detection: {source}")


def main() -> int:
    test_valid_documents()
    test_duplicate_keys_are_rejected()
    test_anchors_and_aliases_are_rejected()
    test_non_json_types_are_rejected()
    test_non_finite_numbers_are_rejected()
    test_structure_limits_are_enforced()
    test_invalid_syntax_is_classified_without_content()
    test_decoder_recursion_is_classified()
    test_direct_parser_detection()
    test_production_parsers_use_shared_policy()
    print("Strict first-party YAML parsing contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
