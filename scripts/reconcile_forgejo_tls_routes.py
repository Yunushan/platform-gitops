#!/usr/bin/env python3
"""Build JSON patches that complete TLS on an already-matching Forgejo route.

The Forgejo GitOps route is authoritative. A Woodpecker workload can lag
behind it during reconciliation, so this helper never rewrites route hosts
from a client-side OAuth URL. It only fills an empty TLS Secret binding on a
route that already matches the requested host.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from bounded_file import read_bounded_text
from strict_json import loads_strict_json


HOST_TERM_RE = re.compile(r"Host\s*\(\s*([`\"'])([^`\"']+)\1\s*\)")
MAX_ROUTE_RESOURCE_BYTES = 8 * 1024 * 1024


def _replace_or_add(patch: list[dict[str, Any]], path: str, value: Any, exists: bool) -> None:
    patch.append({"op": "replace" if exists else "add", "path": path, "value": value})


def _ingress_patch(
    resource: dict[str, Any], name: str, target_host: str, fallback_secret: str
) -> list[dict[str, Any]]:
    spec = resource.get("spec")
    if not isinstance(spec, dict):
        return []
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        return []

    del name
    target_rule = any(
        isinstance(rule, dict) and str(rule.get("host") or "").strip() == target_host
        for rule in rules
    )
    patch: list[dict[str, Any]] = []
    if not target_rule:
        return []

    raw_tls = spec.get("tls")
    tls_entries = raw_tls if isinstance(raw_tls, list) else []
    target_tls_index: int | None = None
    for index, entry in enumerate(tls_entries):
        if not isinstance(entry, dict):
            continue
        hosts = {str(host).strip() for host in entry.get("hosts", []) or []}
        # An empty hosts list is the Kubernetes Ingress wildcard form and is
        # also a useful place to add the concrete host without losing a custom
        # Secret binding.
        if not hosts or target_host in hosts:
            target_tls_index = index
            break
    if target_tls_index is None:
        if isinstance(raw_tls, list) and raw_tls:
            tls_value = list(raw_tls)
            tls_value.append({"hosts": [target_host], "secretName": fallback_secret})
            _replace_or_add(patch, "/spec/tls", tls_value, True)
        else:
            _replace_or_add(
                patch,
                "/spec/tls",
                [{"hosts": [target_host], "secretName": fallback_secret}],
                "tls" in spec,
            )
        return patch

    entry = tls_entries[target_tls_index]
    if not isinstance(entry, dict):
        return patch
    tls_entry = dict(entry)
    hosts = list(tls_entry.get("hosts", []) or [])
    if target_host not in {str(host).strip() for host in hosts}:
        hosts.append(target_host)
    if hosts != list(entry.get("hosts", []) or []):
        tls_entry["hosts"] = hosts
    if not str(tls_entry.get("secretName") or "").strip():
        tls_entry["secretName"] = fallback_secret
    if tls_entry != entry:
        tls_value = list(tls_entries)
        tls_value[target_tls_index] = tls_entry
        _replace_or_add(patch, "/spec/tls", tls_value, True)
    return patch


def _ingressroute_patch(
    resource: dict[str, Any], name: str, target_host: str, fallback_secret: str
) -> list[dict[str, Any]]:
    spec = resource.get("spec")
    if not isinstance(spec, dict):
        return []
    routes = spec.get("routes")
    if not isinstance(routes, list):
        return []

    del name
    patch: list[dict[str, Any]] = []
    target_route = False
    for route in routes:
        if not isinstance(route, dict):
            continue
        match = str(route.get("match") or "")
        terms = list(HOST_TERM_RE.finditer(match))
        matches_target = any(term.group(2).strip() == target_host for term in terms)
        if not terms:
            continue
        if matches_target:
            target_route = True

    if not target_route:
        return patch

    raw_tls = spec.get("tls")
    if isinstance(raw_tls, dict):
        tls = dict(raw_tls)
        if not str(tls.get("secretName") or "").strip():
            tls["secretName"] = fallback_secret
        if tls != raw_tls:
            _replace_or_add(patch, "/spec/tls", tls, True)
    else:
        _replace_or_add(patch, "/spec/tls", {"secretName": fallback_secret}, "tls" in spec)
    return patch


def build_patch(
    resource: dict[str, Any],
    kind: str,
    name: str,
    target_host: str,
    fallback_secret: str,
) -> list[dict[str, Any]]:
    """Return a Kubernetes JSON patch for one Forgejo route resource."""
    target_host = target_host.strip()
    fallback_secret = fallback_secret.strip()
    if not target_host or not fallback_secret:
        return []
    if kind == "Ingress":
        return _ingress_patch(resource, name, target_host, fallback_secret)
    if kind == "IngressRoute":
        return _ingressroute_patch(resource, name, target_host, fallback_secret)
    raise ValueError(f"unsupported resource kind: {kind}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("resource_json")
    parser.add_argument("kind", choices=("Ingress", "IngressRoute"))
    parser.add_argument("name")
    parser.add_argument("target_host")
    parser.add_argument("fallback_secret")
    args = parser.parse_args(argv)

    try:
        resource = loads_strict_json(
            read_bounded_text(
                args.resource_json,
                encoding="utf-8",
                max_bytes=MAX_ROUTE_RESOURCE_BYTES,
            )
        )
        patch = build_patch(
            resource,
            args.kind,
            args.name,
            args.target_host,
            args.fallback_secret,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"route reconciliation failed: {exc}", file=sys.stderr)
        return 1

    json.dump(patch, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
