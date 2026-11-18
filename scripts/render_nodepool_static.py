#!/usr/bin/env python3
"""Render private static worker supply; never contact, reset or register workers."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import struct
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from render_gerrit_sites import label
import render_zuul_config as zuul
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
TLS_FILES = ("zk-client.pem", "zk-client.key", "zk-ca.pem")


def host_key(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 16384 or len(value.split()) != 2:
        return False
    algorithm, encoded = value.split()
    if value != algorithm + " " + encoded:
        return False
    try:
        payload = base64.b64decode(encoded, validate=True)
        parts = []
        while payload:
            if len(payload) < 4:
                return False
            length = struct.unpack(">I", payload[:4])[0]
            if length > len(payload) - 4:
                return False
            parts.append(payload[4:4 + length])
            payload = payload[4 + length:]
        if not parts or parts[0] != algorithm.encode("ascii"):
            return False
        if algorithm == "ssh-ed25519":
            return len(parts) == 2 and len(parts[1]) == 32
        if algorithm == "ssh-rsa":
            if len(parts) != 3:
                return False
            for integer in parts[1:]:
                if not integer or integer[0] & 0x80 or (len(integer) > 1 and integer[0] == 0 and not integer[1] & 0x80):
                    return False
            exponent = int.from_bytes(parts[1], "big")
            modulus_bits = int.from_bytes(parts[2], "big").bit_length()
            return 3 <= exponent < 2 ** 32 and exponent % 2 == 1 and 2048 <= modulus_bits <= 8192
        curves = {"ecdsa-sha2-nistp256": (b"nistp256", 65), "ecdsa-sha2-nistp384": (b"nistp384", 97), "ecdsa-sha2-nistp521": (b"nistp521", 133)}
        if algorithm in curves:
            curve, length = curves[algorithm]
            return len(parts) == 3 and parts[1] == curve and len(parts[2]) == length and parts[2][0] == 4
    except (ValueError, UnicodeError, struct.error):
        return False
    return False


def validate(plan: object, zuul_plan: dict) -> dict:
    zuul.validate(zuul_plan)
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "nodepool_version", "image", "namespace", "pools"}:
        raise ValueError("Missing or unsupported Nodepool plan fields")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Unsupported Nodepool plan schema")
    if not isinstance(plan["nodepool_version"], str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", plan["nodepool_version"]):
        raise ValueError("Declare an exact reviewed Nodepool version")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]) or not label(plan["namespace"]):
        raise ValueError("Declare the Nodepool namespace and digest-pinned image")
    pools = plan["pools"]
    if not isinstance(pools, list) or not 1 <= len(pools) <= 100:
        raise ValueError("Declare bounded static worker pools")
    tenants = {tenant["name"]: tenant for tenant in zuul_plan["tenants"]}
    if any("allowed_labels" not in tenant for tenant in tenants.values()):
        raise ValueError("Every tenant must declare explicit worker-label restrictions before static supply is rendered")
    all_labels = [item for tenant in tenants.values() for item in tenant["allowed_labels"]]
    if len(set(all_labels)) != len(all_labels):
        raise ValueError("Static labels must not be shared across tenant trust domains")
    declared = set()
    names = set()
    hosts = set()
    for pool in pools:
        if not isinstance(pool, dict) or set(pool) != {"name", "tenant", "label", "nodes"}:
            raise ValueError("Invalid static pool fields")
        if not label(pool["name"]) or pool["name"] in names or not label(pool["label"]) or pool["label"] in declared:
            raise ValueError("Pool names and worker labels must be unique")
        names.add(pool["name"])
        declared.add(pool["label"])
        if not label(pool["tenant"]) or pool["tenant"] not in tenants or pool["label"] not in tenants[pool["tenant"]]["allowed_labels"]:
            raise ValueError("Static label must belong to its declared tenant")
        nodes = pool["nodes"]
        if not isinstance(nodes, list) or not 2 <= len(nodes) <= 1000:
            raise ValueError("Declare at least two independent workers per static pool")
        domains = set()
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {"host", "port", "username", "host_key", "failure_domain"}:
                raise ValueError("Invalid static worker fields")
            if not dns_name(node["host"]) or node["host"] in hosts or not label(node["failure_domain"]):
                raise ValueError("Static host names must be unique with explicit failure domains")
            if (type(node["port"]) is not int or not 1 <= node["port"] <= 65535 or not label(node["username"])
                    or node["username"] == "root" or not host_key(node["host_key"])):
                raise ValueError("Provide an unprivileged worker login and approved SSH public-key pin")
            hosts.add(node["host"])
            domains.add(node["failure_domain"])
        if len(domains) < 2:
            raise ValueError("Worker pool must declare at least two failure domains")
    if declared != set(all_labels):
        raise ValueError("Tenant worker labels and supplied static pool labels must match exactly")
    if len(hosts) > 4096:
        raise ValueError("Static worker inventory exceeds the total host limit")
    return plan


def configuration(plan: dict, zuul_plan: dict) -> str:
    validate(plan, zuul_plan)
    pools = []
    for pool in plan["pools"]:
        pools.append({"name": pool["name"], "node-attributes": {"platform-tenant": pool["tenant"]}, "nodes": [
            {"name": node["host"], "connection-port": node["port"], "connection-type": "ssh", "username": node["username"],
             "host-key": node["host_key"], "host-key-checking": True, "labels": [pool["label"]],
             "python-path": "/usr/bin/python3", "max-parallel-jobs": 1, "timeout": 10}
            for node in pool["nodes"]]})
    return yaml.safe_dump({"zookeeper-servers": zuul_plan["zookeeper"],
        "zookeeper-tls": {"cert": "/etc/nodepool/private/zk-client.pem", "key": "/etc/nodepool/private/zk-client.key", "ca": "/etc/nodepool/private/zk-ca.pem"},
        "labels": [{"name": pool["label"], "min-ready": 0} for pool in plan["pools"]],
        "providers": [{"name": "platform-static", "driver": "static", "pools": pools}]}, sort_keys=False)


def render(plan: dict, zuul_plan: dict, runtime: str) -> dict[str, str]:
    validate(plan, zuul_plan)
    if runtime not in RUNTIMES:
        raise ValueError("Unknown Nodepool runtime")
    files = {"nodepool.yaml": configuration(plan, zuul_plan), "zuul-tenants.yaml": zuul.tenant_configuration(zuul_plan)}
    files["render-status.json"] = json.dumps({"component": "nodepool-static-configuration", "runtime": runtime,
        "rendered": True, "registered": False, "workers_modified": False, "allocation_verified": False, "ha_verified": False,
        "required_tls_files": list(TLS_FILES), "remaining": ["launcher-runtime-and-release-compatibility-acceptance",
            "zookeeper-connectivity-and-acls", "static-host-identity-and-physical-placement",
            "worker-reset-and-trust-domain-policy", "real-allocation-release-concurrency-and-node-loss-tests"]}, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--zuul-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        zuul_plan = loads_strict_json(read_bounded_text(args.zuul_plan, encoding="utf-8"))
        write_bundle(render(plan, zuul_plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Static worker render failed; inspect private plans, tenant restrictions, host pins and output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "registered": False, "workers_modified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
