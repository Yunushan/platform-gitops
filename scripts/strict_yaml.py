#!/usr/bin/env python3
"""Parse YAML with deterministic, JSON-compatible, resource-bounded semantics."""

from __future__ import annotations

import math
from typing import Any

import yaml


MAX_YAML_DEPTH = 128
MAX_YAML_NODES = 1_000_000
MAX_YAML_DOCUMENTS = 10_000
ALLOWED_YAML_TAGS = frozenset(
    {
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:map",
    }
)


class StrictYamlError(ValueError):
    """Raised when YAML is ambiguous, unsafe, or exceeds parser limits."""


class _StrictSafeLoader(yaml.SafeLoader):
    def __init__(self, stream: str | bytes | bytearray) -> None:
        super().__init__(stream)
        self._strict_depth = 0
        self._strict_nodes = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.nodes.Node:
        if self.check_event(yaml.events.AliasEvent):
            raise StrictYamlError("YAML anchors and aliases are not allowed")

        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise StrictYamlError("YAML anchors and aliases are not allowed")

        self._strict_nodes += 1
        if self._strict_nodes > MAX_YAML_NODES:
            raise StrictYamlError("YAML structure exceeds the node limit")

        self._strict_depth += 1
        if self._strict_depth > MAX_YAML_DEPTH:
            self._strict_depth -= 1
            raise StrictYamlError("YAML structure exceeds the nesting limit")
        try:
            node = super().compose_node(parent, index)
        except RecursionError as exc:
            raise StrictYamlError("YAML structure exceeds the decoder nesting limit") from exc
        finally:
            self._strict_depth -= 1

        if node.tag not in ALLOWED_YAML_TAGS:
            raise StrictYamlError("YAML contains a non-JSON scalar or collection type")
        return node

    def construct_mapping(
        self,
        node: yaml.nodes.MappingNode,
        deep: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(node, yaml.nodes.MappingNode):
            raise StrictYamlError("YAML mapping construction received an invalid node")
        self.flatten_mapping(node)
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise StrictYamlError("YAML mapping keys must be strings")
            if key in result:
                raise StrictYamlError("duplicate YAML mapping keys are not allowed")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _validate_json_compatible(document: Any) -> None:
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise StrictYamlError("YAML mapping keys must be strings")
                pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise StrictYamlError("YAML numbers must remain finite")
        elif value is None or isinstance(value, (str, bool, int)):
            continue
        else:
            raise StrictYamlError("YAML contains a non-JSON scalar or collection type")


def loads_strict_yaml_all(document: str | bytes | bytearray) -> list[Any]:
    """Decode all YAML documents with deterministic Kubernetes-safe semantics."""
    documents: list[Any] = []
    payload = bytes(document) if isinstance(document, bytearray) else document
    try:
        stream = yaml.load_all(payload, Loader=_StrictSafeLoader)
        for index, decoded in enumerate(stream, start=1):
            if index > MAX_YAML_DOCUMENTS:
                raise StrictYamlError("YAML stream exceeds the document limit")
            _validate_json_compatible(decoded)
            documents.append(decoded)
    except StrictYamlError:
        raise
    except RecursionError as exc:
        raise StrictYamlError("YAML structure exceeds the decoder nesting limit") from exc
    except (UnicodeError, yaml.YAMLError) as exc:
        raise StrictYamlError("YAML input is invalid or uses an unsupported tag") from exc
    return documents
