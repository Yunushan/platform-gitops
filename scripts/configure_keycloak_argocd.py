#!/usr/bin/env python3
"""Create one MFA-bound Argo CD OIDC client and private, unapplied app settings."""

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
import bootstrap_keycloak_realm as bootstrap
from deploy_keycloak_ha import canonical, fingerprint, includes, lock, sha, uid
import render_keycloak_ha as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops.client-operation"
CONTRACT = "platform.gitops.client-contract"
UNVERIFIED = (*bootstrap.UNVERIFIED, "argocd_configuration_applied", "argocd_login_verified", "argocd_authorization_verified")


def tools(): return bootstrap.sources(Path(__file__).stem)


def validate(plan, contract):
    renderer.exact(plan, "schema_version realm_plan realm_proof application")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported application identity plan")
    bootstrap.validate(plan["realm_plan"], contract)
    provider = urlsplit(plan["realm_plan"]["server"]["url"])
    if (provider.port or 443) != contract["plan"].get("public_port", 443): raise ValueError("The application identity endpoint must use the advertised canonical HTTPS port")
    proof = plan["realm_proof"]; renderer.exact(proof, "path sha256")
    if not isinstance(proof["path"], str) or not Path(proof["path"]).is_absolute() or not sha(proof["sha256"]): raise ValueError("Pin the original completed private realm proof")
    app = plan["application"]; renderer.exact(app, "kind version client_id origin client_secret_env namespace secret_name secret_key group_roles")
    if app["kind"] != "argocd" or not isinstance(app["version"], str) or not re.fullmatch(r"3[.]5[.][0-9]+", app["version"]): raise ValueError("Review an exact Argo CD 3.5 patch")
    for name in ("client_id", "namespace", "secret_name", "secret_key"):
        if not renderer.label(app[name]): raise ValueError("Use explicit client and Secret reference labels")
    origin = urlsplit(app["origin"])
    if (origin.scheme != "https" or not renderer.dns_name(origin.hostname) or origin.username is not None or origin.password is not None or origin.query or origin.fragment or origin.path
        or origin.netloc != origin.hostname + (":" + str(origin.port) if origin.port is not None else "") or origin.port is not None and not 1 <= origin.port <= 65535): raise ValueError("Use an exact canonical HTTPS application origin")
    if not isinstance(app["client_secret_env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", app["client_secret_env"]) or app["client_secret_env"] == plan["realm_plan"]["server"]["token_env"]: raise ValueError("Reference a separate private OIDC client credential")
    groups = app["group_roles"]
    if not isinstance(groups, list) or not 1 <= len(groups) <= 32: raise ValueError("Review explicit group-to-role bindings")
    for group in groups:
        renderer.exact(group, "group role")
        if not isinstance(group["group"], str) or len(group["group"]) > 256 or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", group["group"]) or group["role"] not in {"admin", "readonly"}: raise ValueError("Use exact full group paths and supported Argo CD roles")
    if len({g["group"] for g in groups}) != len(groups): raise ValueError("A group must have one unambiguous role")


def prepared(bundle, plan):
    contract, realm = bootstrap.package(bundle, plan["realm_plan"]["contract_sha256"]); validate(plan, contract)
    raw = read_bounded_bytes(Path(plan["realm_proof"]["path"]), max_bytes=128 * 1024)
    if hashlib.sha256(raw).hexdigest() != plan["realm_proof"]["sha256"]: raise ValueError("The original realm proof changed")
    proof = loads_strict_json(raw)
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1 or proof.get("action") != "apply" or proof.get("phase") != "completed" or proof.get("attempted") is not True
        or proof.get("realm_policy_verified") is not True or proof.get("plan_sha256") != fingerprint(plan["realm_plan"]) or proof.get("toolset_sha256") != bootstrap.sources()
        or not uid(proof.get("realm_id")) or not uid(proof.get("operation_id")) or not sha(proof.get("observed_sha256")) or any(proof.get(k) is not False for k in bootstrap.UNVERIFIED)): raise ValueError("Require the completed original realm operation, not a name-only identity assertion")
    realm.update(id=proof["realm_id"], attributes={bootstrap.OPERATION: proof["operation_id"], bootstrap.CONTRACT: plan["realm_plan"]["contract_sha256"]})
    return contract, realm, proof


class Client(bootstrap.Client):
    def __init__(self, server, realm, client_id):
        super().__init__(server)
        self.base = "/admin/realms/" + quote(realm, safe="") + "/clients"
        self.query = self.base + "?" + urlencode({"clientId": client_id, "max": 2, "search": "false"})

    def call(self, path, *, payload=None, missing=False):
        if payload is not None:
            if path != self.base: raise ValueError("Only the selected new client may be created")
            return self._request(path, payload=payload, missing=missing)
        if path == self.query: return self._request(path, missing=missing)
        return super().call(path, missing=missing)


def collection(realm, client_id):
    base = "/admin/realms/" + quote(realm, safe="") + "/clients"
    return base, base + "?" + urlencode({"clientId": client_id, "max": 2, "search": "false"})


def lookup(client, realm, client_id):
    base, query = collection(realm, client_id); rows = client.call(query)
    if not isinstance(rows, list) or len(rows) > 1 or any(row.get("clientId") != client_id or not uid(row.get("id")) for row in rows): raise ValueError("Client lookup is incomplete, ambiguous or changed")
    return client.call(base + "/" + rows[0]["id"]) if rows else None


def realm_ready(client, plan, contract, realm, proof):
    bootstrap.target(client, plan["realm_plan"], contract)
    if bootstrap.verify(client, realm, proof["realm_id"]) != proof["observed_sha256"]: raise ValueError("The original realm's MFA flow or policy changed")
    flows = client.call("/admin/realms/" + quote(realm["realm"], safe="") + "/authentication/flows")
    found = [f for f in flows if f.get("alias") == realm["browserFlow"]]
    if len(found) != 1 or not uid(found[0].get("id")): raise ValueError("Pin the original browser flow")
    return found[0]["id"]


def policy(plan, flow, state):
    app = plan["application"]
    return {"id": state["client_id"], "clientId": app["client_id"], "name": app["client_id"], "enabled": True, "protocol": "openid-connect",
        "clientAuthenticatorType": "client-secret", "publicClient": False, "bearerOnly": False, "standardFlowEnabled": True, "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False, "authorizationServicesEnabled": False, "fullScopeAllowed": False, "consentRequired": False,
        "redirectUris": [app["origin"] + "/auth/callback"], "webOrigins": [], "defaultClientScopes": ["profile", "email"], "optionalClientScopes": [],
        "authenticationFlowBindingOverrides": {"browser": flow},
        "attributes": {OPERATION: state["operation_id"], CONTRACT: fingerprint(plan), "pkce.code.challenge.method": "S256", "post.logout.redirect.uris": app["origin"],
            "oauth2.device.authorization.grant.enabled": "false", "oidc.ciba.grant.enabled": "false", "standard.token.exchange.enabled": "false", "use.refresh.tokens": "true"},
        "protocolMappers": [{"id": state["mapper_id"], "name": "platform-full-group-paths", "protocol": "openid-connect", "protocolMapper": "oidc-group-membership-mapper", "consentRequired": False,
            "config": {"claim.name": "groups", "full.path": "true", "id.token.claim": "true", "access.token.claim": "true", "userinfo.token.claim": "true"}}]}


def verify_client(client, expected, realm, credential):
    base, _ = collection(realm, expected["clientId"])
    actual = client.call(base + "/" + expected["id"])
    if not isinstance(actual, dict): raise ValueError("The original client is unavailable")
    # Keycloak 26.4 ModelToRepresentation sets this nullable flag only when
    # an authorization resource server exists. Absence/null represents off;
    # never normalize true or an arbitrary false-looking value to disabled.
    if actual.get("authorizationServicesEnabled") is None: actual = {**actual, "authorizationServicesEnabled": False}
    unordered = {"redirectUris", "webOrigins", "defaultClientScopes", "optionalClientScopes", "protocolMappers"}
    changed = [k for k, v in expected.items() if k not in unordered and not includes(v, actual.get(k))]
    if changed: raise ValueError("The original OIDC policy fields changed: " + ", ".join(sorted(changed)))
    for name in unordered - {"protocolMappers"}:
        if not isinstance(actual.get(name), list) or sorted(actual[name]) != sorted(expected[name]): raise ValueError("Client redirects or granted scopes changed")
    mappers = actual.get("protocolMappers")
    if not isinstance(mappers, list) or len(mappers) != 1 or not includes(expected["protocolMappers"][0], mappers[0]) or mappers[0].get("config") != expected["protocolMappers"][0]["config"]: raise ValueError("The original group claim mapper changed")
    if actual.get("authenticationFlowBindingOverrides") != expected["authenticationFlowBindingOverrides"]: raise ValueError("Client authentication overrides changed")
    secret = client.call(base + "/" + expected["id"] + "/client-secret")
    if not isinstance(secret, dict) or secret.get("type") != "secret" or not isinstance(secret.get("value"), str) or not hmac.compare_digest(secret["value"], credential): raise ValueError("The original client credential differs from its private environment input")
    # Provider defaults may add fields; pin the declared policy and original
    # mapper/flow handles, not a raw response that can contain credentials.
    return fingerprint(expected)


def write_app(directory, plan, realm):
    server, app = plan["realm_plan"]["server"], plan["application"]
    raw = read_bounded_bytes(Path(server["ca_file"]), max_bytes=512 * 1024)
    if hashlib.sha256(raw).hexdigest() != server["ca_sha256"]: raise ValueError("The application trust anchor changed")
    pem = raw.decode("ascii")
    if not re.fullmatch(r"(?:-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+", pem): raise ValueError("Application trust must contain certificates only")
    ssl.create_default_context(cadata=pem)
    provider = urlsplit(server["url"])
    # The renderer omits the default HTTPS port from its fixed advertised URL.
    # An explicit :443 administration URL must not create a different issuer.
    origin = "https://" + provider.hostname + (":" + str(provider.port) if provider.port not in {None, 443} else "")
    oidc = {"name": "Platform Keycloak", "issuer": origin + "/realms/" + realm["realm"], "clientID": app["client_id"],
        "clientSecret": "$" + app["secret_name"] + ":" + app["secret_key"], "enablePKCEAuthentication": True,
        "requestedScopes": ["openid", "profile", "email"], "requestedIDTokenClaims": {"groups": {"essential": True}}, "rootCA": pem}
    values = {"configs": {"cm": {"url": app["origin"], "oidc.config": yaml.safe_dump(oidc, sort_keys=False)},
        "rbac": {"policy.default": "role:platform-no-access", "scopes": "[groups]", "policy.matchMode": "glob",
            "policy.csv": "".join("g, " + group["group"] + ", role:" + group["role"] + "\n" for group in app["group_roles"])}}}
    reference = {"namespace": app["namespace"], "name": app["secret_name"], "key": app["secret_key"], "labels": {"app.kubernetes.io/part-of": "argocd"},
        "credential_env": app["client_secret_env"], "provisioned": False}
    for filename, content in {"argocd-values.yaml": yaml.safe_dump(values, sort_keys=False), "secret-reference.json": canonical(reference)}.items():
        path = directory / filename
        if path.exists() and (path.is_symlink() or read_bounded_bytes(path, max_bytes=2 * 1024 ** 2) != content.encode()): raise ValueError("A generated application artifact changed")
        if not path.exists(): atomic_write_text(path, content)


def operate(bundle, plan, *, mode="local", output=None, wait_seconds=30, client=None):
    contract, realm, proof = prepared(bundle, plan); source_hash = tools()
    result = {"local_inputs_verified": True, "server_checked": False, "client_policy_verified": False, "app_settings_rendered": False, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"render", "check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 300: raise ValueError("Use one explicit action and bounded private journal")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use an original private operation directory")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume requires the original operation directory")
    receipt = directory / "keycloak-argocd-proof.json"
    with lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("Do not redirect the original journal")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=128 * 1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply" or state.get("plan_sha256") != fingerprint(plan) or state.get("toolset_sha256") != source_hash
                or any(not uid(state.get(k)) for k in ("operation_id", "client_id", "mapper_id")) or type(state.get("attempted")) is not bool
                or state.get("observed_sha256") is not None and not sha(state["observed_sha256"]) or not sha(state.get("credential_sha256")) or any(state.get(k) is not False for k in UNVERIFIED)): raise ValueError("Resume only the matching original client operation")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": str(uuid4()), "client_id": str(uuid4()), "mapper_id": str(uuid4()),
                "plan_sha256": fingerprint(plan), "toolset_sha256": source_hash, "attempted": False, "observed_sha256": None, "credential_sha256": None, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        record("local-validated", **result)
        if mode == "render":
            write_app(directory, plan, realm); record("rendered", app_settings_rendered=True); return {**result, "app_settings_rendered": True}
        wrote = False
        try:
            app = plan["application"]; network = client or Client(plan["realm_plan"]["server"], realm["realm"], app["client_id"])
            flow = realm_ready(network, plan, contract, realm, proof); expected = policy(plan, flow, state)
            current = lookup(network, realm["realm"], app["client_id"])
            if not state["attempted"] and current is not None: raise ValueError("An existing client requires separate configuration or recovery; never adopt it")
            if mode == "check-server": record("server-checked", server_checked=True); return {**result, "server_checked": True}
            credential = os.environ.get(app["client_secret_env"], "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credential): raise ValueError("Provide a generated private URL-safe client credential of at least 32 characters")
            commitment = hashlib.sha256(credential.encode()).hexdigest()
            if state["credential_sha256"] is not None and not hmac.compare_digest(commitment, state["credential_sha256"]): raise ValueError("Preserve the original private client credential for resume")
            if not state["attempted"]:
                if realm_ready(network, plan, contract, realm, proof) != flow or lookup(network, realm["realm"], app["client_id"]) is not None or tools() != source_hash: raise ValueError("The original realm or client scope changed")
                state.update(attempted=True, credential_sha256=commitment); record("before-client-create", read_only=False); wrote = True
                network.call(collection(realm["realm"], app["client_id"])[0], payload={**expected, "secret": credential})
            deadline = time.monotonic() + wait_seconds
            while current is None:
                current = lookup(network, realm["realm"], app["client_id"])
                if current is not None: break
                if state["observed_sha256"] is not None or time.monotonic() >= deadline: raise TimeoutError("Original client absent or create outcome unknown; never repeat POST")
                time.sleep(1)
            if current.get("id") != state["client_id"]: raise ValueError("A foreign client occupies the original handle")
            observed = verify_client(network, expected, realm["realm"], credential)
            if state["observed_sha256"] is not None and state["observed_sha256"] != observed: raise ValueError("The original client policy changed")
            if realm_ready(network, plan, contract, realm, proof) != flow or verify_client(network, expected, realm["realm"], credential) != observed or tools() != source_hash: raise ValueError("Client or realm identity changed during final checking")
            write_app(directory, plan, realm)
            result.update(server_checked=True, client_policy_verified=True, app_settings_rendered=True, read_only=not wrote)
            record("completed", observed_sha256=observed, **result); return result
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="keycloak-argocd-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--identity-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=30)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("render", "check-server", "apply", "resume"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.identity_plan, max_bytes=128 * 1024))
        mode = next((name.replace("_", "-") for name in ("render", "check_server", "apply", "resume") if getattr(args, name)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Keycloak Argo CD client setup stopped; preserve the private journal and original handles. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
