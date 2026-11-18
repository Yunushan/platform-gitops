#!/usr/bin/env python3
"""Observe pinned OpenBao members, canary identity scope and audit configuration."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
import openbao_snapshot as backup
import render_openbao_ha as renderer
from http_transport import read_bounded_response
from strict_json import loads_strict_json


FALSE_FLAGS = ("physical_failure_domains_verified", "quorum_failover_verified", "audit_delivery_verified",
               "all_workload_identities_verified", "unseal_recovery_verified", "off_platform_restore_verified", "ha_verified", "full_stack_verified")
OBSERVER_READS = {"sys/storage/raft/configuration": {"read"}, "sys/audit": {"read", "sudo"}}
DENIED = ("sys/storage/raft/configuration", "sys/audit", "auth/token/create", "sys/storage/raft/snapshot-force", "sys/policies/acl/runtime-probe")
PRIVILEGED = ("auth/token/create", "sys/storage/raft/snapshot-force", "sys/policies/acl/runtime-probe")


def reference(value): return isinstance(value, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", value))
def data_path(value): return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,62}/data/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value)) and len(value) <= 256


def client_plan(plan, member, credential):
    cluster = plan["cluster"]
    return {"schema_version": 1, "source": {"url": member["api_url"], "cluster_id": cluster["id"], "version": cluster["version"],
        "raft": {m["name"]: m["raft_address"] for m in cluster["members"]}, "ca_file": cluster["ca_file"], "ca_sha256": cluster["ca_sha256"], "token_env": credential},
        "limits": {"request_seconds": plan["request_seconds"], "transfer_seconds": 5, "max_bytes": 1024}}


def validate(plan):
    renderer.exact(plan, "schema_version cluster observer probe audit_devices request_seconds")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported observation plan")
    cluster = plan["cluster"]; renderer.exact(cluster, "id version members ca_file ca_sha256")
    members = cluster["members"]
    if not isinstance(members, list) or len(members) != 3: raise ValueError("Pin three direct member endpoints")
    renderer.exact(plan["observer"], "token_env")
    probe = plan["probe"]; renderer.exact(probe, "token_env path value_sha256 denied_paths")
    if not reference(plan["observer"]["token_env"]) or not reference(probe["token_env"]) or probe["token_env"] == plan["observer"]["token_env"]:
        raise ValueError("Select separate observer and restricted canary credentials")
    if not data_path(probe["path"]) or not backup.digest(probe["value_sha256"]): raise ValueError("Pin a non-sensitive KV-v2 canary value by its UTF-8 SHA-256")
    denied = probe["denied_paths"]
    if (not isinstance(denied, list) or not 1 <= len(denied) <= 8 or not all(data_path(p) for p in denied)
            or len(set(denied)) != len(denied) or probe["path"] in denied): raise ValueError("Select exact denied canary paths without wildcards")
    for member in members:
        renderer.exact(member, "name api_url raft_address")
        backup.validate(client_plan(plan, member, plan["observer"]["token_env"]))
    if len({m["name"] for m in members}) != 3 or len({m["api_url"] for m in members}) != 3:
        raise ValueError("Member IDs and direct API origins must differ")
    devices = plan["audit_devices"]
    if not isinstance(devices, dict) or not 1 <= len(devices) <= 8: raise ValueError("Inventory every enabled audit device")
    for name, device in devices.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}/", name): raise ValueError("Use exact audit mount names")
        renderer.exact(device, "type file_path")
        if (device["type"] != "file" or not isinstance(device["file_path"], str) or not renderer.PATH.fullmatch(device["file_path"])
                or any(part in {".", ".."} for part in device["file_path"].split("/"))): raise ValueError("Pin each reviewed file-audit location")
    if len({d["file_path"] for d in devices.values()}) != len(devices): raise ValueError("Audit devices must not share one log file")
    return plan


class Client(backup.Client):
    def read(self, path, *, capabilities=None, denied=False):
        connection = self.connect()
        try:
            method = "POST" if capabilities is not None else "GET"
            if capabilities is not None and path != "sys/capabilities-self": raise ValueError("Only capability queries may POST")
            body = backup.canonical({"paths": capabilities}).encode() if capabilities is not None else None
            connection.request(method, "/v1/" + path, body=body, headers={"X-Vault-Token": self.bearer, "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != (403 if denied else 200): raise ValueError("Pinned read or denial failed; redirects are never followed")
            raw = read_bounded_response(response, max_bytes=256 * 1024)
            if denied: return None
            result = loads_strict_json(raw)
            if not isinstance(result, dict): raise ValueError("Invalid API observation")
            return result
        finally: connection.close()


def health(clients, plan):
    states = {}
    for member in plan["cluster"]["members"]:
        row = clients[member["name"]].read("sys/health?standbyok=true")
        if (row.get("cluster_id") != plan["cluster"]["id"] or row.get("version") != plan["cluster"]["version"] or row.get("initialized") is not True
                or row.get("sealed") is not False or type(row.get("standby")) is not bool): raise ValueError("A direct member is not the reviewed unsealed cluster")
        states[member["name"]] = row["standby"]
    if sum(v is False for v in states.values()) != 1: raise ValueError("Require exactly one active member")
    return states


def capabilities(client, expected):
    observed = client.read("sys/capabilities-self", capabilities=list(expected))
    for path, rights in expected.items():
        actual = observed.get(path)
        if not isinstance(actual, list) or len(actual) != len(set(actual)) or set(actual) != rights:
            raise ValueError("An observed credential has broader or missing capabilities")


def voting(client, plan, active):
    rows = client.read("sys/storage/raft/configuration").get("data", {}).get("config", {}).get("servers")
    expected = {m["name"]: m["raft_address"] for m in plan["cluster"]["members"]}
    if (not isinstance(rows, list) or len(rows) != 3 or any(not isinstance(r, dict) for r in rows)
            or {r.get("node_id"): r.get("address") for r in rows} != expected or any(r.get("voter") is not True for r in rows)
            or sum(r.get("leader") is True for r in rows) != 1 or next((r.get("node_id") for r in rows if r.get("leader") is True), None) != active):
        raise ValueError("Voting membership or active identity differs")


def audits(client, plan):
    devices = client.read("sys/audit").get("data")
    if not isinstance(devices, dict) or set(devices) != set(plan["audit_devices"]): raise ValueError("Enabled audit inventory differs")
    for name, expected in plan["audit_devices"].items():
        row = devices[name]; options = row.get("options", {})
        if (row.get("type") != expected["type"] or options.get("file_path") != expected["file_path"] or options.get("mode") != "0600"
                or options.get("log_raw") != "false" or options.get("hmac_accessor", "true") != "true"):
            raise ValueError("Audit configuration differs from the reviewed HMAC/private-file policy")


def operate(plan, *, live=False, factory=Client):
    validate(plan)
    result = {"schema_version": 1, "plan_sha256": backup.fingerprint(plan), "read_only": True, "local_inputs_verified": True,
        "live_observations_verified": False, **{k: False for k in FALSE_FLAGS}}
    if not live: return result
    tools = backup.sources(Path(__file__).stem)
    observers = {m["name"]: factory(client_plan(plan, m, plan["observer"]["token_env"])) for m in plan["cluster"]["members"]}
    before = health(observers, plan); active = next(name for name, standby in before.items() if not standby); observer = observers[active]
    observer_scope = {**OBSERVER_READS, **{p: {"deny"} for p in PRIVILEGED}}
    capabilities(observer, observer_scope); voting(observer, plan, active); audits(observer, plan)
    probe = plan["probe"]; scope = {probe["path"]: {"read"}, **{p: {"deny"} for p in (*DENIED, *probe["denied_paths"])}}
    for member in plan["cluster"]["members"]:
        restricted = factory(client_plan(plan, member, probe["token_env"]))
        capabilities(restricted, scope)
        value = restricted.read(probe["path"]).get("data", {}).get("data", {}).get("value")
        if not isinstance(value, str) or len(value.encode()) > 4096 or hashlib.sha256(value.encode()).hexdigest() != probe["value_sha256"]:
            raise ValueError("The restricted KV-v2 canary differs")
        for path in probe["denied_paths"]: restricted.read(path, denied=True)
    capabilities(observer, observer_scope); voting(observer, plan, active); audits(observer, plan)
    if health(observers, plan) != before or backup.sources(Path(__file__).stem) != tools: raise ValueError("Member leadership or executing code changed during verification")
    result.update(live_observations_verified=True, tls_three_member_identity_verified=True, three_voters_verified=True,
        canary_read_and_denial_verified=True, canary_capability_scope_verified=True, audit_configuration_verified=True,
        recorded_at=datetime.now(timezone.utc).isoformat(), toolset_sha256=tools)
    result["receipt_sha256"] = backup.fingerprint(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--verify", action="store_true"); parser.add_argument("--proof", type=Path); args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        result = operate(plan, live=args.verify)
        if args.proof is not None:
            if not args.verify: raise ValueError("Only live observations may write a receipt")
            atomic_write_text(args.proof, backup.canonical(result))
        print(backup.canonical(result)); return 0
    except Exception:
        print("OpenBao runtime observations failed; API and private values suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
