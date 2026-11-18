#!/usr/bin/env python3
"""Test client identity, MFA binding, credential handling and Argo CD settings."""

import copy
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest import mock

import yaml

import bootstrap_keycloak_realm as bootstrap
import configure_keycloak_argocd as configure
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture
from test_keycloak_realm import FakeClient as RealmClient, uid


class FakeClient(RealmClient):
    def __init__(self):
        super().__init__(); self.application_client = None; self.client_lost_ack = False; self.client_unknown = False; self.client_mutate = None; self.client_writes = 0

    def call(self, path, *, payload=None, missing=False):
        base = "/admin/realms/platform/clients"
        if not path.startswith(base): return super().call(path, payload=payload, missing=missing)
        self.calls.append((path, payload is not None))
        if payload is not None:
            self.client_writes += 1
            if self.client_unknown: raise TimeoutError("Unknown create outcome")
            if self.application_client is not None: raise ValueError("Client already exists")
            self.application_client = copy.deepcopy(payload)
            if self.client_lost_ack: self.client_lost_ack = False; raise TimeoutError("Lost acknowledgement")
            return None
        if path.startswith(base + "?"):
            return [{"id": self.application_client["id"], "clientId": self.application_client["clientId"]}] if self.application_client else []
        if self.application_client is None: return None
        if path.endswith("/client-secret"): return {"type": "secret", "value": self.application_client["secret"]}
        if path != base + "/" + self.application_client["id"]: raise AssertionError("Unexpected client handle")
        value = copy.deepcopy(self.application_client)
        if self.client_mutate: self.client_mutate(value)
        return value


class ArgoCDIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); self.root = Path(self.temporary.name)
        for module in (bootstrap, configure):
            patch = mock.patch.object(module, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop)
        self.trust_patch = mock.patch.object(configure.ssl, "create_default_context"); self.trust_patch.start(); self.addCleanup(self.trust_patch.stop)
        self.ca = self.root / "ca.crt"; self.ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nZHVtbXk=\n-----END CERTIFICATE-----\n")
        self.client = FakeClient(); self.bundle, self.plan = self.prepare("rke2", "initial"); self.counter = 0
        self.credential = secrets.token_urlsafe(48)
        self.env_patch = mock.patch.dict(configure.os.environ, {"KEYCLOAK_ARGOCD_SECRET": self.credential}); self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.client.calls.clear()

    def prepare(self, runtime, name, *, public_port=443, server_port=None):
        bundle = self.root / "private" / (name + "-bundle"); bundle.mkdir(parents=True)
        selected = fixture(); selected["public_port"] = public_port
        for filename, value in renderer.render(selected, runtime).items(): (bundle / filename).write_text(value, encoding="utf-8", newline="\n")
        server = "https://identity.example.com" + (":" + str(server_port) if server_port is not None else ":" + str(public_port) if public_port != 443 else "")
        realm_plan = {"schema_version": 1, "contract_sha256": hashlib.sha256((bundle / "keycloak-contract.json").read_bytes()).hexdigest(),
            "server": {"url": server, "master_realm_id": uid(1), "token_env": "KEYCLOAK_ADMIN_ACCESS_TOKEN", "ca_file": str(self.ca.absolute()), "ca_sha256": hashlib.sha256(self.ca.read_bytes()).hexdigest()}}
        operation = self.root / "private" / (name + "-realm")
        bootstrap.operate(bundle, realm_plan, mode="apply", output=operation, client=self.client)
        proof = operation / "keycloak-realm-proof.json"
        return bundle, {"schema_version": 1, "realm_plan": realm_plan, "realm_proof": {"path": str(proof.absolute()), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()},
            "application": {"kind": "argocd", "version": "3.5.0", "client_id": "argocd", "origin": "https://argocd.example.com", "client_secret_env": "KEYCLOAK_ARGOCD_SECRET",
                "namespace": "argocd", "secret_name": "platform-sso-argocd", "secret_key": "client-secret", "group_roles": [{"group": "/platform-admins", "role": "admin"}, {"group": "/platform-readers", "role": "readonly"}]}}

    def action(self, mode, output=None):
        self.counter += 1
        return configure.operate(self.bundle, self.plan, mode=mode, output=output or self.root / "private" / ("attempt-" + str(self.counter)), wait_seconds=5, client=self.client)

    def test_local_validation_has_no_api_or_credential_access(self):
        with mock.patch.object(configure, "Client", side_effect=AssertionError("No API")), mock.patch.dict(configure.os.environ, {}, clear=True):
            self.assertTrue(configure.operate(self.bundle, self.plan)["local_inputs_verified"])
        self.assertFalse(self.client.calls)

    def test_advertised_port_and_default_port_normalization_preserve_the_issuer(self):
        for public, server in ((8443, 8443), (443, 443)):
            with self.subTest(public=public, server=server):
                self.client = FakeClient(); self.bundle, self.plan = self.prepare("raw", "port-" + str(public), public_port=public, server_port=server)
                output = self.root / "private" / ("render-port-" + str(public)); self.action("render", output)
                values = yaml.safe_load((output / "argocd-values.yaml").read_text())
                oidc = yaml.safe_load(values["configs"]["cm"]["oidc.config"])
                self.assertEqual(oidc["issuer"], "https://identity.example.com" + (":8443" if public == 8443 else "") + "/realms/platform")

    def test_mismatched_advertised_port_is_refused_before_api_or_credential_access(self):
        for public, server in ((443, 8443), (8443, 443)):
            self.client = FakeClient(); self.bundle, self.plan = self.prepare("raw", "mismatch-" + str(public), public_port=public, server_port=server)
            self.client.calls.clear()
            with mock.patch.object(configure, "Client", side_effect=AssertionError("No API")), mock.patch.dict(configure.os.environ, {}, clear=True):
                for mode in ("local", "render", "check-server", "apply"):
                    with self.subTest(public=public, server=server, mode=mode), self.assertRaises(ValueError):
                        configure.operate(self.bundle, self.plan, mode=mode, output=self.root / "private" / ("mismatch-output-" + str(public) + mode))
            self.assertFalse(self.client.calls); self.assertEqual(self.client.client_writes, 0)

    def test_all_five_runtime_packages_can_create_one_bound_client(self):
        for runtime in renderer.RUNTIMES:
            self.client = FakeClient(); self.bundle, self.plan = self.prepare(runtime, "package-" + runtime)
            result = self.action("apply")
            self.assertTrue(result["client_policy_verified"]); self.assertTrue(result["app_settings_rendered"]); self.assertEqual(self.client.client_writes, 1)
            for key in configure.UNVERIFIED: self.assertIs(result[key], False)

    def test_generated_argocd_config_has_pkce_trust_exact_groups_and_no_default_grants(self):
        output = self.root / "private/render"
        with mock.patch.dict(configure.os.environ, {}, clear=True): self.assertTrue(self.action("render", output)["app_settings_rendered"])
        self.assertFalse(self.client.calls)
        values = yaml.safe_load((output / "argocd-values.yaml").read_text()); configs = values["configs"]; oidc = yaml.safe_load(configs["cm"]["oidc.config"])
        self.assertEqual(oidc["clientSecret"], "$platform-sso-argocd:client-secret"); self.assertTrue(oidc["enablePKCEAuthentication"])
        self.assertEqual(oidc["requestedScopes"], ["openid", "profile", "email"]); self.assertEqual(oidc["rootCA"], self.ca.read_text())
        self.assertNotEqual(configs["rbac"]["policy.default"], "role:readonly"); self.assertEqual(configs["rbac"]["scopes"], "[groups]")
        self.assertEqual(configs["rbac"]["policy.csv"], "g, /platform-admins, role:admin\ng, /platform-readers, role:readonly\n")
        self.assertNotIn("admin.enabled", configs["cm"])

    def test_server_check_never_creates_a_client_or_reads_its_secret(self):
        with mock.patch.dict(configure.os.environ, {}, clear=True): self.assertTrue(self.action("check-server")["server_checked"])
        self.assertEqual(self.client.client_writes, 0); self.assertFalse(any(path.endswith("/client-secret") for path, _ in self.client.calls))

    def test_existing_client_is_never_adopted_or_overwritten(self):
        self.client.application_client = {"id": uid(999), "clientId": "argocd"}
        for mode in ("check-server", "apply"):
            with self.assertRaises(ValueError): self.action(mode)
        self.assertEqual(self.client.client_writes, 0)

    def test_changed_realm_proof_is_rejected_before_api_reads(self):
        Path(self.plan["realm_proof"]["path"]).write_text("{}")
        with self.assertRaises(ValueError): self.action("apply")
        self.assertFalse(self.client.calls)

    def test_changed_realm_mfa_flow_is_not_accepted_as_client_readiness(self):
        self.client.flow_offset = 100
        with self.assertRaises(ValueError): self.action("apply")
        self.assertEqual(self.client.client_writes, 0)

    def test_lost_acknowledgement_observes_the_original_client_once(self):
        output = self.root / "private/lost"; self.client.client_lost_ack = True
        with self.assertRaises(TimeoutError): self.action("apply", output)
        self.assertTrue(self.action("resume", output)["client_policy_verified"])
        self.assertEqual(self.client.client_writes, 1)

    def test_unknown_absent_create_outcome_never_repeats_post(self):
        output = self.root / "private/unknown"; self.client.client_unknown = True
        with self.assertRaises(TimeoutError): self.action("apply", output)
        with mock.patch.object(configure.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaises(TimeoutError): self.action("resume", output)
        self.assertEqual(self.client.client_writes, 1)

    def test_deleted_or_replaced_original_client_is_never_recreated(self):
        output = self.root / "private/original"; self.action("apply", output); original = copy.deepcopy(self.client.application_client)
        self.client.application_client = None
        with self.assertRaises(TimeoutError): self.action("resume", output)
        self.client.application_client = original; self.client.application_client["id"] = uid(999)
        with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(self.client.client_writes, 1)

    def test_grant_callback_group_mapper_and_binding_drift_is_rejected(self):
        output = self.root / "private/drift"; self.action("apply", output)
        for mutation in (lambda c: c.update(directAccessGrantsEnabled=True), lambda c: c.update(authorizationServicesEnabled=True), lambda c: c["redirectUris"].append("https://foreign.example.com/*"),
                         lambda c: c["authenticationFlowBindingOverrides"].update(direct_grant=uid(90)), lambda c: c["protocolMappers"].append(copy.deepcopy(c["protocolMappers"][0])),
                         lambda c: c["protocolMappers"][0]["config"].update({"full.path": "false"}), lambda c: c["optionalClientScopes"].append("offline_access")):
            self.client.client_mutate = mutation
            with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(self.client.client_writes, 1)

    def test_provider_omitted_disabled_authorization_services_is_normalized_only_as_off(self):
        output = self.root / "private/nullable"; self.client.client_mutate = lambda c: c.pop("authorizationServicesEnabled")
        self.assertTrue(self.action("apply", output)["client_policy_verified"])
        self.client.client_mutate = lambda c: c.update(authorizationServicesEnabled=None)
        self.assertTrue(self.action("resume", output)["client_policy_verified"])
        for invalid in (True, 0, "false"):
            self.client.client_mutate = lambda c: c.update(authorizationServicesEnabled=invalid)
            with self.assertRaises(ValueError): self.action("resume", output)

    def test_changed_or_rotated_private_credential_is_refused_without_persisting_values(self):
        output = self.root / "private/credential"; self.action("apply", output)
        for path in output.iterdir():
            if path.is_file() and self.credential.encode() in path.read_bytes(): self.fail("A generated client credential entered a private application artifact")
        with mock.patch.dict(configure.os.environ, {"KEYCLOAK_ARGOCD_SECRET": secrets.token_urlsafe(48)}):
            with self.assertRaises(ValueError): self.action("resume", output)
        self.client.application_client["secret"] = secrets.token_urlsafe(48)
        with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(self.client.client_writes, 1)

    def test_callback_rbac_injection_and_unsupported_versions_fail_locally(self):
        original = copy.deepcopy(self.plan)
        for mutate in (lambda p: p["application"].update(origin="https://argocd.example.com/*"), lambda p: p["application"].update(origin="http://argocd.example.com"),
                       lambda p: p["application"]["group_roles"][0].update(group="/admins, role:admin\np, *"), lambda p: p["application"]["group_roles"].append(copy.deepcopy(p["application"]["group_roles"][0])),
                       lambda p: p["application"].update(version="latest"), lambda p: p["application"].update(client_secret_env=p["realm_plan"]["server"]["token_env"])):
            self.plan = copy.deepcopy(original); mutate(self.plan)
            with self.assertRaises(ValueError): self.action("apply")
        self.assertFalse(self.client.calls)

    def test_resume_rejects_changed_sources_or_plan_before_network_access(self):
        output = self.root / "private/source"; self.action("apply", output); before = len(self.client.calls)
        with mock.patch.object(configure, "tools", return_value="f" * 64):
            with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(len(self.client.calls), before)
        self.plan["application"]["group_roles"][0]["role"] = "readonly"
        with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(len(self.client.calls), before)

    def test_client_transport_allows_only_its_exact_create_and_lookup_paths(self):
        client = configure.Client.__new__(configure.Client); client.base, client.query = configure.collection("platform", "argocd"); client._request = mock.Mock()
        client.call(client.query); client.call(client.base, payload={})
        self.assertEqual(client._request.call_count, 2)
        for path in ("/admin/realms", "/admin/realms/other/clients", client.base + "/id/client-secret"):
            with self.assertRaises(ValueError): client.call(path, payload={})
        with self.assertRaises(ValueError): client.call(client.query + "&other=unsafe")
        self.assertEqual(client._request.call_count, 2)


if __name__ == "__main__": unittest.main()
