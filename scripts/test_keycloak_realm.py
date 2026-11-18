#!/usr/bin/env python3
"""Exercise new-realm policy checks and original-handle recovery boundaries."""

import copy
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest import mock

import bootstrap_keycloak_realm as bootstrap
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture


def uid(number): return "00000000-0000-0000-0000-" + str(number).zfill(12)


class FakeClient:
    def __init__(self):
        self.calls = []; self.realm = None; self.version = "26.4.7"; self.master = uid(1)
        self.fail_before_write = False; self.lose_ack = False; self.mutate = None; self.flow_offset = 0

    def call(self, path, *, payload=None, missing=False):
        self.calls.append((path, payload is not None))
        if payload is not None:
            if self.fail_before_write: raise TimeoutError("Unknown create result")
            if self.realm is not None: raise ValueError("Already occupied")
            self.realm = copy.deepcopy(payload)
            if self.lose_ack: self.lose_ack = False; raise TimeoutError("Lost acknowledgement")
            return None
        if path == "/admin/serverinfo": return {"systemInfo": {"version": self.version}}
        if path == "/admin/realms/master": return {"id": self.master, "realm": "master", "enabled": True}
        if self.realm is None:
            if missing: return None
            raise ValueError("Realm missing")
        suffix = path.removeprefix("/admin/realms/" + self.realm["realm"])
        top, forms = uid(20 + self.flow_offset), uid(21 + self.flow_offset)
        definitions = self.realm.get("authenticationFlows", [])
        if suffix == "": value = {k: v for k, v in self.realm.items() if k not in {"authenticationFlows", "requiredActions"}}
        elif suffix == "/authentication/flows": value = [{**{k: v for k, v in definitions[0].items() if k != "authenticationExecutions"}, "id": top}]
        elif suffix in {"/authentication/flows/" + top, "/authentication/flows/" + forms}:
            definition = definitions[0 if suffix.endswith(top) else 1]
            value = {**{k: v for k, v in definition.items() if k != "authenticationExecutions"}, "id": top if suffix.endswith(top) else forms}
        elif suffix.endswith("/executions"):
            leaf = [{"id": uid(30 + self.flow_offset + i), "providerId": provider, "requirement": "REQUIRED", "priority": 10 + i * 10,
                "index": i, "level": 0, "authenticationFlow": False, "displayName": provider} for i, provider in enumerate(("auth-username-password-form", "auth-otp-form"))]
            value = leaf if "platform-browser-mfa-forms" in suffix else [
                {"id": uid(32 + self.flow_offset), "providerId": "auth-cookie", "requirement": "ALTERNATIVE", "priority": 10, "index": 0, "level": 0, "authenticationFlow": False, "displayName": "Cookie"},
                {"id": uid(33 + self.flow_offset), "displayName": "platform-browser-mfa-forms", "flowId": forms, "requirement": "ALTERNATIVE", "priority": 20, "index": 1, "level": 0, "authenticationFlow": True},
                *[{**e, "level": 1} for e in leaf]]
        elif suffix == "/authentication/required-actions/CONFIGURE_TOTP": value = self.realm["requiredActions"][0]
        else: raise AssertionError("Unexpected API route")
        value = copy.deepcopy(value)
        if self.mutate: self.mutate(suffix, value)
        return value


class RealmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.patch = mock.patch.object(bootstrap, "ROOT", self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        self.bundle, self.plan = self.package("rke2", "bundle"); self.client = FakeClient(); self.count = 0

    def package(self, runtime, name):
        bundle = self.root / "private" / name; bundle.mkdir(parents=True)
        for filename, content in renderer.render(fixture(), runtime).items(): (bundle / filename).write_text(content, encoding="utf-8", newline="\n")
        return bundle, {"schema_version": 1, "contract_sha256": hashlib.sha256((bundle / "keycloak-contract.json").read_bytes()).hexdigest(),
            "server": {"url": "https://identity.example.com", "master_realm_id": uid(1), "token_env": "KEYCLOAK_ADMIN_ACCESS_TOKEN", "ca_file": str((self.root / "ca.crt").absolute()), "ca_sha256": "a" * 64}}

    def run_action(self, mode, output=None):
        self.count += 1
        return bootstrap.operate(self.bundle, self.plan, mode=mode, output=output or self.root / "private" / ("attempt-" + str(self.count)), wait_seconds=5, client=self.client)

    def test_local_validation_does_not_construct_a_client_or_read_a_token(self):
        with mock.patch.object(bootstrap, "Client", side_effect=AssertionError("No server access")):
            self.assertTrue(bootstrap.operate(self.bundle, self.plan)["local_inputs_verified"])
        self.assertFalse(self.client.calls)

    def test_read_only_server_check_does_not_import_or_generate_credentials(self):
        self.assertTrue(self.run_action("check-server")["server_checked"])
        self.assertFalse(any(write for _, write in self.client.calls)); self.assertIsNone(self.client.realm)

    def test_all_five_packages_create_only_one_new_realm_with_no_accounts(self):
        for runtime in renderer.RUNTIMES:
            self.bundle, self.plan = self.package(runtime, "bundle-" + runtime); self.client = FakeClient()
            result = self.run_action("apply")
            self.assertTrue(result["realm_policy_verified"]); self.assertFalse(result["read_only"])
            self.assertEqual(sum(write for _, write in self.client.calls), 1)
            self.assertFalse(set(self.client.realm) & {"users", "clients", "smtpServer", "identityProviders"})
            for key in bootstrap.UNVERIFIED: self.assertIs(result[key], False)

    def test_changed_artifact_and_unknown_fields_fail_before_api_access(self):
        (self.bundle / "new-realm-mfa.json").write_text("{}")
        with self.assertRaises(ValueError): self.run_action("apply")
        self.assertFalse(self.client.calls)

    def test_existing_realm_is_never_adopted_overwritten_or_deleted(self):
        self.client.realm = {"realm": "platform", "id": uid(99)}
        for mode in ("check-server", "apply"):
            with self.assertRaises(ValueError): self.run_action(mode)
        self.assertFalse(any(write for _, write in self.client.calls))

    def test_wrong_version_or_original_master_identity_refuses_creation(self):
        for field, value in (("version", "26.4.8"), ("master", uid(2))):
            self.client = FakeClient(); setattr(self.client, field, value)
            with self.assertRaises(ValueError): self.run_action("apply")
            self.assertFalse(any(write for _, write in self.client.calls))

    def test_endpoint_credentials_queries_paths_and_other_hosts_are_rejected(self):
        for value in ("http://identity.example.com", "https://identity.example.com/", "https://identity.example.com/admin", "https://identity.example.com?token=x", "https://identity.example.com#x", "https://a:b@identity.example.com", "https://foreign.example.com"):
            self.plan["server"]["url"] = value
            with self.assertRaises(ValueError): self.run_action("apply")
        self.assertFalse(self.client.calls)

    def test_lost_acknowledgement_observes_original_realm_without_repeating_post(self):
        output = self.root / "private/lost"; self.client.lose_ack = True
        with self.assertRaises(TimeoutError): self.run_action("apply", output)
        self.assertTrue(self.run_action("resume", output)["realm_policy_verified"])
        self.assertEqual(sum(write for _, write in self.client.calls), 1)

    def test_absent_unknown_create_outcome_is_observed_and_never_retried(self):
        output = self.root / "private/unknown"; self.client.fail_before_write = True
        with self.assertRaises(TimeoutError): self.run_action("apply", output)
        with mock.patch.object(bootstrap.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaises(TimeoutError): self.run_action("resume", output)
        self.assertEqual(sum(write for _, write in self.client.calls), 1)

    def test_deleted_or_replaced_original_realm_is_never_recreated(self):
        output = self.root / "private/deleted"; self.run_action("apply", output); original = copy.deepcopy(self.client.realm)
        self.client.realm = None
        with self.assertRaises(TimeoutError): self.run_action("resume", output)
        self.client.realm = original; self.client.realm["id"] = uid(999)
        with self.assertRaises(ValueError): self.run_action("resume", output)
        self.assertEqual(sum(write for _, write in self.client.calls), 1)

    def test_altered_browser_requirements_nested_graph_or_otp_action_is_not_verified(self):
        def weaken(path, value):
            if path.endswith("/executions"): value[-1]["requirement"] = "ALTERNATIVE"
        def enroll(path, value):
            if path.endswith("/CONFIGURE_TOTP"): value["defaultAction"] = False
        def graph(path, value):
            if path.endswith("platform-browser-mfa/executions"): value[-1]["id"] = uid(900)
        def cookie(path, value):
            if path.endswith("platform-browser-mfa/executions"): value[0]["authenticationConfig"] = uid(900)
        def policy(path, value):
            if path == "": value["registrationAllowed"] = True
        for mutation in (weaken, enroll, graph, cookie, policy):
            self.client = FakeClient(); self.client.mutate = mutation
            with self.assertRaises(ValueError): self.run_action("apply")

    def test_resume_requires_original_source_plan_flow_handles_and_markers(self):
        output = self.root / "private/original"; self.run_action("apply", output)
        self.client.flow_offset = 100
        with self.assertRaises(ValueError): self.run_action("resume", output)
        self.client.flow_offset = 0; self.client.realm["attributes"][bootstrap.OPERATION] = uid(999)
        with self.assertRaises(ValueError): self.run_action("resume", output)
        self.plan["server"]["token_env"] = "OTHER_ADMIN_TOKEN"
        before = len(self.client.calls)
        with self.assertRaises(ValueError): self.run_action("resume", output)
        self.assertEqual(len(self.client.calls), before)

    def test_original_source_hash_is_required_before_resume_network_access(self):
        output = self.root / "private/source"; self.run_action("apply", output)
        before = len(self.client.calls)
        with mock.patch.object(bootstrap, "sources", return_value="f" * 64):
            with self.assertRaises(ValueError): self.run_action("resume", output)
        self.assertEqual(len(self.client.calls), before)

    def test_ca_hash_and_bearer_token_never_enter_a_journal(self):
        output = self.root / "private/redacted"; self.run_action("apply", output)
        state = json.loads((output / "keycloak-realm-proof.json").read_text())
        self.assertNotIn("server", state); self.assertNotIn("token", state); self.assertNotIn("realm", state)
        with self.assertRaises((ValueError, FileNotFoundError)): bootstrap.Client(self.plan["server"])

    def test_api_transport_disables_proxies_and_redirects_and_keeps_tls_verification(self):
        server = self.plan["server"]; raw = b"public-test-ca-placeholder"; server["ca_sha256"] = hashlib.sha256(raw).hexdigest()
        with mock.patch.object(bootstrap, "read_bounded_bytes", return_value=raw), mock.patch.object(bootstrap.ssl, "create_default_context") as trust, mock.patch.dict(bootstrap.os.environ, {server["token_env"]: secrets.token_urlsafe(36)}):
            client = bootstrap.Client(server)
        trust.assert_called_once_with(cadata=raw.decode("ascii"))
        self.assertIs(client.context, trust.return_value)
        with mock.patch.object(bootstrap, "open_http_request") as transport:
            for path in ("https://foreign.example.com", "/admin/realms/platform?x=1", "/admin/../credentials", "/admin/realms/platform\n"):
                with self.assertRaises(ValueError): client.call(path)
            with self.assertRaises(ValueError): client.call("/admin/realms/platform", payload={})
            transport.assert_not_called()

    def test_api_responses_are_bounded_and_errors_do_not_echo_private_values(self):
        client = bootstrap.Client.__new__(bootstrap.Client); client.server = self.plan["server"]; client.token = secrets.token_urlsafe(36); client.context = mock.sentinel.tls
        response = mock.MagicMock(); response.__enter__.return_value = response; response.status = 200
        with mock.patch.object(bootstrap, "open_http_request", return_value=response) as transport:
            response.read.side_effect = [b"{}", b""]
            self.assertEqual(client.call("/admin/realms/platform"), {})
            self.assertEqual(transport.call_args.kwargs, {"timeout": 15, "use_environment_proxy": False, "tls_context": client.context})
            self.assertEqual(response.read.call_args_list[0].args, (2 * 1024 ** 2 + 1,))
            response.read.side_effect = [b"x" * (2 * 1024 ** 2 + 1)]
            with self.assertRaisesRegex(ValueError, "preserve the original journal"): client.call("/admin/realms/platform")
            transport.side_effect = RuntimeError(client.token)
            with self.assertRaises(ValueError) as error: client.call("/admin/realms/platform")
            self.assertNotIn(client.token, str(error.exception))


if __name__ == "__main__": unittest.main()
