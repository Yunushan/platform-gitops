#!/usr/bin/env python3
"""Explicitly adopt the compact premium budget in an existing private checkout.

Only resource and rollout scalar leaves change; private endpoints, credentials,
replicas, storage classes and PVC sizes are preserved. No cluster or Git writes.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from strict_yaml import loads_strict_yaml_all


BUDGET = {
    'argocd-ha': {
        'repoServer.resources.requests.cpu': '200m',
        'controller.resources.requests.cpu': '250m',
    },
    'loki': {
        'write.resources.requests.cpu': '150m',
        'read.resources.requests.cpu': '100m',
        'backend.resources.requests.cpu': '100m',
        'gateway.resources.requests.cpu': '50m',
        'gateway.deploymentStrategy.type': 'RollingUpdate',
        'gateway.deploymentStrategy.rollingUpdate.maxSurge': 0,
        'gateway.deploymentStrategy.rollingUpdate.maxUnavailable': 1,
        'chunksCache.allocatedMemory': 512,
        'chunksCache.allocatedCPU': '100m',
        'resultsCache.allocatedMemory': 128,
        'resultsCache.allocatedCPU': '100m',
    },
    'velero': {'nodeAgent.resources.requests.cpu': '100m'},
    'monitoring': {
        'grafana.deploymentStrategy.type': 'RollingUpdate',
        'grafana.deploymentStrategy.rollingUpdate.maxSurge': 0,
        'grafana.deploymentStrategy.rollingUpdate.maxUnavailable': 1,
    },
}


def values(text: str) -> dict:
    docs = list(loads_strict_yaml_all(text))
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise ValueError('expected one values mapping')
    return docs[0]


def set_leaf(document: dict, parts: list[str], value: str | int) -> None:
    for key in parts[:-1]:
        document = document.setdefault(key, {})
        if not isinstance(document, dict):
            raise ValueError('resource budget path is not a mapping')
    document[parts[-1]] = value


def replace_leaf(text: str, parts: list[str], value: str | int) -> str:
    """Use YAML node spans to preserve unrelated formatting and comments."""
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    for depth, key in enumerate(parts):
        if not isinstance(node, yaml.MappingNode) or node.flow_style:
            raise ValueError('resource budget requires block-style mappings')
        child = next((v for k, v in node.value if k.value == key), None)
        if child is None:
            addition = value
            for remaining in reversed(parts[depth:]):
                addition = {remaining: addition}
            indent = node.value[0][0].start_mark.column if node.value else node.start_mark.column
            pos = node.end_mark.index - node.end_mark.column
            if node.end_mark.index == len(text):
                pos = len(text)
            block = yaml.safe_dump(addition, sort_keys=False)
            block = ''.join(' ' * indent + line + '\n' for line in block.splitlines())
            prefix = '\n' if pos and text[pos - 1] != '\n' else ''
            return text[:pos] + prefix + block + text[pos:]
        if depth == len(parts) - 1:
            if not isinstance(child, yaml.ScalarNode):
                raise ValueError('resource budget leaf is not a scalar')
            # All accepted replacements are fixed safe scalar literals above.
            return text[:child.start_mark.index] + str(value) + text[child.end_mark.index:]
        node = child
    raise ValueError('empty resource budget path')


def refresh(text: str, app: str) -> str:
    original = values(text)
    expected = copy.deepcopy(original)
    for top in {key.split('.')[0] for key in BUDGET[app]} - {'chunksCache', 'resultsCache'}:
        if not isinstance(original.get(top), dict):
            raise ValueError(f'{app}.{top} is missing; render the premium profile first')
    if app == 'monitoring':
        grafana = original['grafana']
        if grafana.get('replicas', 1) < 2 or grafana.get('persistence', {}).get('enabled', True):
            raise ValueError('Grafana requires multiple replicas with an external database')
    # Existing explicit cache resources override allocatedCPU/allocatedMemory.
    # Refuse to silently apply a budget that Helm would ignore.
    if app == 'loki':
        for cache in ('chunksCache', 'resultsCache'):
            if original.get(cache) is not None and not isinstance(original[cache], dict):
                raise ValueError(f'{cache} must be a mapping')
            if (original.get(cache) or {}).get('resources'):
                raise ValueError(f'{cache}.resources overrides cache sizing; review it explicitly')
    for key, value in BUDGET[app].items():
        parts = key.split('.')
        set_leaf(expected, parts, value)
        text = replace_leaf(text, parts, value)
    if values(text) != expected:
        raise ValueError('resource budget would change unrelated values')
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apps-root', type=Path, required=True)
    parser.add_argument('--app', action='append', choices=sorted(BUDGET),
                        help='limit a staged adoption to selected applications')
    parser.add_argument('--apply', action='store_true', help='write reviewed changes; default is read-only')
    args = parser.parse_args()
    plans = []
    for app in dict.fromkeys(args.app or BUDGET):
        path = args.apps_root / app / 'values.yaml'
        old = read_bounded_text(path, encoding='utf-8')
        new = refresh(old, app)
        if old != new:
            plans.append((path, new))
    # Validate all files before writing any file.
    for path, new in plans:
        if args.apply:
            atomic_write_text(path, new)
        print(f"resource_budget={'updated' if args.apply else 'planned'} path={path}")
    if not plans:
        print('resource_budget=already-present')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
