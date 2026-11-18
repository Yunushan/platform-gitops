#!/usr/bin/env python3
"""Fresh MFA realm and real application acceptance through an activated member."""

import hashlib
import json
import os
from urllib.parse import urlencode
from urllib.request import Request
from unittest import mock

import bootstrap_keycloak_realm as bootstrap
import render_keycloak_ha as renderer


def exercise(test, context, *, bundle, selected, contract_sha256, application_check, name):
    if application_check is None: return {}
    if name not in {"native", "docker"}: raise ValueError("Use one scoped fixture realm operation")
    origin = renderer.public_origin(selected)
    request = Request(origin + "/realms/master/protocol/openid-connect/token", data=urlencode({"grant_type": "client_credentials", "client_id": "fixture-admin", "client_secret": context["admin_secret"]}).encode())
    with context["http"].open(request, timeout=15) as response:
        raw = response.read(128 * 1024 + 1)
        if response.status != 200 or len(raw) > 128 * 1024: raise ValueError("Activated member administration failed")
        token = json.loads(raw)["access_token"]
    realm_plan = {"schema_version": 1, "contract_sha256": contract_sha256, "server": {"url": origin,
        "master_realm_id": "00000000-0000-0000-0000-000000000000", "token_env": "KEYCLOAK_FIXTURE_ADMIN_TOKEN",
        "ca_file": str(context["root"] / "ca.crt"), "ca_sha256": hashlib.sha256((context["root"] / "ca.crt").read_bytes()).hexdigest()}}
    operation = context["root"] / ("private/" + name + "-realm")
    with mock.patch.dict(os.environ, {"KEYCLOAK_FIXTURE_ADMIN_TOKEN": token}), mock.patch.object(bootstrap, "ROOT", context["root"]):
        client = bootstrap.Client(realm_plan["server"])
        realm_plan["server"]["master_realm_id"] = client.call("/admin/realms/master")["id"]
        test.assertTrue(bootstrap.operate(bundle, realm_plan, mode="check-server", output=context["root"] / ("private/" + name + "-realm-check"))["server_checked"])
        test.assertTrue(bootstrap.operate(bundle, realm_plan, mode="apply", output=operation)["realm_policy_verified"])
        test.assertTrue(bootstrap.operate(bundle, realm_plan, mode="resume", output=operation)["realm_policy_verified"])
        for resource, value in (("groups", {"name": "fixture-readers"}), ("users", {"username": "fixture-user", "enabled": True,
            "email": "fixture-user@example.invalid", "emailVerified": True, "firstName": "Fixture", "lastName": "Host",
            "groups": ["/fixture-readers"], "credentials": [{"type": "password", "value": context["login_password"], "temporary": False}]})):
            request = Request(origin + "/admin/realms/" + selected["realm"] + "/" + resource, data=json.dumps(value).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with context["http"].open(request, timeout=15) as response:
                if response.status != 201: raise ValueError("Activated member identity preparation failed")
    print("Disposable Keycloak " + name + " fixture: testing Argo CD against the activated member", flush=True)
    result = application_check(test, {**context, "origin": origin, "bundle": bundle, "realm_plan": realm_plan, "realm_operation": operation,
        "realm_name": selected["realm"], "issuer": origin + "/realms/" + selected["realm"], "totp_secret": None})
    for key in ("argocd_mfa_login_verified", "argocd_reader_project_access_verified", "argocd_reader_project_write_denied", "argocd_admin_project_write_verified", "argocd_unassigned_user_denied"):
        test.assertIs(result[key], True)
    context["private_logs"]([token, context["login_password"]])
    return {**result, "keycloak_host_argocd_mfa_and_rbac_verified": True, "keycloak_host_guarded_application_realm_verified": True}
