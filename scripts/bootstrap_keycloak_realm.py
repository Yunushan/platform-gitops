#!/usr/bin/env python3
"""Create one new Keycloak MFA realm; observe original handles on resume."""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from deploy_keycloak_ha import canonical, fingerprint, includes, lock, sha, uid
from http_transport import open_http_request, read_bounded_response
import render_keycloak_ha as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops.realm-operation"
CONTRACT = "platform.gitops.realm-contract"
UNVERIFIED = ("mfa_verified", "master_mfa_verified", "recovery_verified", "application_integration_verified", "database_ha_verified", "session_failover_verified", "ha_verified", "full_stack_verified")


def sources(entry=None):
    directory = Path(__file__).resolve().parent; found = {}; pending = [entry or Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_bytes(directory / (name + ".py"), max_bytes=2 * 1024 ** 2); found[name] = hashlib.sha256(raw).hexdigest()
        for item in ast.walk(ast.parse(raw)):
            modules = [n.name for n in item.names] if isinstance(item, ast.Import) else [item.module] if isinstance(item, ast.ImportFrom) and item.module else []
            for module in modules:
                first = module.split(".")[0]
                if (directory / (first + ".py")).is_file() and first not in found: pending.append(first)
    return fingerprint(found)


def package(bundle, digest):
    if not sha(digest): raise ValueError("Pin the original rendered contract")
    raw = read_bounded_bytes(
        bundle / "keycloak-contract.json",
        max_bytes=256 * 1024,
    )
    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError("The original Keycloak contract changed")
    contract = loads_strict_json(raw); renderer.exact(contract, "schema_version runtime plan files_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1: raise ValueError("Unsupported rendered contract")
    files = renderer.render(contract["plan"], contract["runtime"])
    for name, value in files.items():
        if read_bounded_bytes(bundle / name, max_bytes=2 * 1024 ** 2) != value.encode(): raise ValueError("The rendered Keycloak package differs from reviewed policy")
    return contract, renderer.new_realm(contract["plan"])


def validate(plan, contract):
    renderer.exact(plan, "schema_version contract_sha256 server")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not sha(plan["contract_sha256"]): raise ValueError("Unsupported realm bootstrap plan")
    server = plan["server"]; renderer.exact(server, "url master_realm_id token_env ca_file ca_sha256")
    parsed = urlsplit(server["url"])
    if (parsed.scheme != "https" or parsed.hostname != contract["plan"]["hostname"] or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path or parsed.netloc != parsed.hostname + (":" + str(parsed.port) if parsed.port is not None else "")
        or parsed.port is not None and not 1 <= parsed.port <= 65535): raise ValueError("Use the canonical direct HTTPS origin without paths or credentials")
    if not uid(server["master_realm_id"]) or not isinstance(server["token_env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", server["token_env"]): raise ValueError("Pin the original master realm and reference a private admin bearer token")
    if not isinstance(server["ca_file"], str) or not Path(server["ca_file"]).is_absolute() or not sha(server["ca_sha256"]): raise ValueError("Pin an explicit trusted CA file")


class Client:
    def __init__(self, server):
        self.server = dict(server)
        raw = read_bounded_bytes(Path(server["ca_file"]), max_bytes=512 * 1024)
        if hashlib.sha256(raw).hexdigest() != server["ca_sha256"]: raise ValueError("The reviewed CA changed")
        self.context = ssl.create_default_context(cadata=raw.decode("ascii"))
        self.token = os.environ.get(server["token_env"], "")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{20,16384}", self.token): raise ValueError("Provide a current private admin access token through its environment reference")

    def call(self, path, *, payload=None, missing=False):
        # Only the new-realm create endpoint may receive a write.
        if not path.startswith("/admin/") or any(c in path for c in "?#\\\r\n") or "/../" in path or payload is not None and path != "/admin/realms": raise ValueError("Unexpected realm API path or mutation")
        return self._request(path, payload=payload, missing=missing)

    def _request(self, path, *, payload=None, missing=False):
        request = Request(self.server["url"] + path, data=None if payload is None else canonical(payload).encode(),
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with open_http_request(request, timeout=15, use_environment_proxy=False, tls_context=self.context) as response:
                if response.status != (200 if payload is None else 201): raise ValueError("Unexpected Keycloak API response")
                raw = read_bounded_response(response, max_bytes=2 * 1024 ** 2)
                return loads_strict_json(raw) if payload is None else None
        except HTTPError as exc:
            status = exc.code; exc.close()
            if status == 404 and missing and payload is None: return None
            raise ValueError("The requested Keycloak API operation was not accepted") from None
        except Exception:
            raise ValueError("Keycloak API validation or transport failed; preserve the original journal") from None


def target(client, plan, contract):
    info = client.call("/admin/serverinfo")
    master = client.call("/admin/realms/master")
    if not isinstance(info, dict) or info.get("systemInfo", {}).get("version") != contract["plan"]["version"] or not isinstance(master, dict) or master.get("id") != plan["server"]["master_realm_id"] or master.get("realm") != "master" or master.get("enabled") is not True: raise ValueError("The original server version or master realm changed")


def verify(client, expected, identity):
    base = "/admin/realms/" + quote(expected["realm"], safe="")
    realm = client.call(base)
    if not isinstance(realm, dict) or realm.get("id") != identity: raise ValueError("The original realm was replaced")
    fields = {k: v for k, v in expected.items() if k not in {"authenticationFlows", "requiredActions"}}
    if not includes(fields, realm): raise ValueError("The original realm policy or operation identity changed")
    flows = client.call(base + "/authentication/flows")
    if not isinstance(flows, list) or len(flows) > 256: raise ValueError("Invalid authentication flow inventory")
    definitions = {f["alias"]: f for f in expected["authenticationFlows"]}
    roots = [f for f in flows if f.get("alias") == expected["browserFlow"]]
    if len(roots) != 1: raise ValueError("The original browser flow is missing or ambiguous")
    handles = {}; graph = []
    def walk(alias, flow_id, level=0):
        if alias in handles or level > 1 or not uid(flow_id): raise ValueError("Invalid or repeated authentication flow handle")
        definition = definitions[alias]; actual = client.call(base + "/authentication/flows/" + quote(flow_id, safe=""))
        if not includes({k: v for k, v in definition.items() if k != "authenticationExecutions"}, actual) or actual.get("id") != flow_id: raise ValueError("Authentication flow definition changed")
        handles[alias] = flow_id
        executions = client.call(base + "/authentication/flows/" + quote(alias, safe="") + "/executions")
        if not isinstance(executions, list) or len(executions) > 32: raise ValueError("Invalid authentication executions")
        direct = [e for e in executions if e.get("level") == 0]
        if len(direct) != len(definition["authenticationExecutions"]): raise ValueError("Authentication executions changed")
        flattened = []
        for index, (wanted, execution) in enumerate(zip(definition["authenticationExecutions"], direct)):
            if (not uid(execution.get("id")) or execution.get("index") != index or execution.get("priority") != wanted["priority"] or execution.get("requirement") != wanted["requirement"]
                or execution.get("authenticationFlow", False) is not wanted["authenticatorFlow"] or execution.get("authenticationConfig")): raise ValueError("Authentication execution requirements changed")
            if wanted["authenticatorFlow"]:
                if execution.get("displayName") != wanted["flowAlias"]: raise ValueError("Authentication subflow changed")
                nested = walk(wanted["flowAlias"], execution.get("flowId"), level + 1)
            else:
                if execution.get("providerId") != wanted["authenticator"] or execution.get("flowId"): raise ValueError("Authentication provider changed")
                nested = []
            item = {k: execution.get(k) for k in ("id", "requirement", "priority", "index", "providerId", "displayName", "authenticationFlow", "flowId", "authenticationConfig")}
            item["level"] = 0; flattened.append(item)
            flattened.extend([{**e, "level": e["level"] + 1} for e in nested])
        normalized = [{**{k: e.get(k) for k in ("id", "requirement", "priority", "index", "providerId", "displayName", "authenticationFlow", "flowId", "authenticationConfig")}, "level": e.get("level")} for e in executions]
        if normalized != flattened: raise ValueError("Flattened browser flow differs from its verified subflow")
        if level == 0: graph.extend(flattened)
        return flattened
    walk(expected["browserFlow"], roots[0].get("id"))
    action = client.call(base + "/authentication/required-actions/CONFIGURE_TOTP")
    if not includes(expected["requiredActions"][0], action): raise ValueError("OTP enrollment policy changed")
    final = client.call(base)
    if not isinstance(final, dict) or final.get("id") != identity or not includes(fields, final): raise ValueError("Realm identity or policy changed during observation")
    return fingerprint({"realm_id": identity, "policy": fields, "flow_ids": handles, "executions": graph, "otp_action": expected["requiredActions"][0]})


def operate(bundle, plan, *, mode="local", output=None, wait_seconds=30, client=None):
    contract, realm = package(bundle, plan["contract_sha256"]); validate(plan, contract); tools = sources()
    result = {"local_inputs_verified": True, "server_checked": False, "realm_policy_verified": False, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 300: raise ValueError("Select an explicit bounded action and private journal")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use an original private realm operation directory")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume needs the original journal directory")
    receipt = directory / "keycloak-realm-proof.json"
    with lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("The realm journal cannot redirect elsewhere")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=128 * 1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply" or state.get("plan_sha256") != fingerprint(plan) or state.get("toolset_sha256") != tools
                or not uid(state.get("realm_id")) or not uid(state.get("operation_id")) or type(state.get("attempted")) is not bool or state.get("observed_sha256") is not None and not sha(state["observed_sha256"])
                or any(state.get(k) is not False for k in UNVERIFIED)): raise ValueError("Resume only the matching original realm operation")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": str(uuid4()), "realm_id": str(uuid4()), "plan_sha256": fingerprint(plan), "toolset_sha256": tools, "attempted": False, "observed_sha256": None, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        record("local-validated", **result)
        realm.update(id=state["realm_id"], attributes={OPERATION: state["operation_id"], CONTRACT: plan["contract_sha256"]})
        path = "/admin/realms/" + quote(realm["realm"], safe=""); wrote = False
        try:
            network = client or Client(plan["server"]); target(network, plan, contract)
            current = network.call(path, missing=True)
            if not state["attempted"]:
                if current is not None: raise ValueError("An existing realm requires a separate recovery workflow; never adopt or replace")
                if mode == "check-server":
                    record("server-checked", server_checked=True); return {**result, "server_checked": True}
                target(network, plan, contract)
                if network.call(path, missing=True) is not None or sources() != tools: raise ValueError("Realm availability or helper sources changed")
                state["attempted"] = True; record("before-realm-create", read_only=False); wrote = True
                network.call("/admin/realms", payload=realm)
            deadline = time.monotonic() + wait_seconds
            while current is None:
                current = network.call(path, missing=True)
                if current is not None: break
                if state["observed_sha256"] is not None or time.monotonic() >= deadline: raise TimeoutError("Original realm missing or create outcome unknown; never repeat POST")
                time.sleep(1)
            if current.get("id") != state["realm_id"] or not includes(realm["attributes"], current.get("attributes", {})): raise ValueError("A foreign realm occupies the original handle")
            observed = verify(network, realm, state["realm_id"])
            if state["observed_sha256"] is not None and observed != state["observed_sha256"]: raise ValueError("The original flow handles changed")
            target(network, plan, contract)
            if verify(network, realm, state["realm_id"]) != observed or sources() != tools: raise ValueError("Realm or source identity changed during final observation")
            result.update(server_checked=True, realm_policy_verified=True, read_only=not wrote)
            record("completed", observed_sha256=observed, **result); return result
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="keycloak-realm-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--realm-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=30)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("check-server", "apply", "resume"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.realm_plan, max_bytes=64 * 1024))
        mode = next((name.replace("_", "-") for name in ("check_server", "apply", "resume") if getattr(args, name)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Keycloak realm bootstrap stopped; preserve the private journal and original realm handle. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
