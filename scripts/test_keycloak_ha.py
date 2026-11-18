#!/usr/bin/env python3
"""Check Keycloak's five runtime packages without borrowing a deployment."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

import render_keycloak_ha as identity


def fixture():
    return {"schema_version": 1, "version": "26.4.7", "image": "registry.example.com/platform/keycloak@sha256:" + "a" * 64,
        "namespace": "identity", "hostname": "identity.example.com", "realm": "platform", "tls_secret": "keycloak-tls", "database_ca_secret": "keycloak-db-ca",
        "database": {"host": "postgres-rw.example.com", "port": 5432, "name": "keycloak", "username": "keycloak", "postgres_major": 17, "secret": "keycloak-database", "password_key": "password"},
        "private_dir": "/etc/platform-keycloak", "native_home": "/opt/keycloak-release", "native_manifest_sha256": "b" * 64,
        "members": [{"name": "keycloak-" + c, "node": "identity-" + c, "address": "192.0.2." + str(31 + i), "failure_domain": "domain-" + c} for i, c in enumerate("abc")],
        "network": {"ingress_namespaces": ["ingress"], "monitoring_namespace": "monitoring", "database_cidrs": ["192.0.2.0/24"]}}


class KeycloakTests(unittest.TestCase):
    def test_explicit_public_https_port_is_bound_in_all_runtime_contracts(self):
        for port in (443, 8443):
            plan = fixture(); plan["public_port"] = port
            origin = "https://identity.example.com" + (":8443" if port == 8443 else "")
            for runtime in identity.RUNTIMES:
                with self.subTest(port=port, runtime=runtime):
                    files = identity.render(plan, runtime)
                    self.assertEqual(json.loads(files["keycloak-contract.json"])["plan"]["public_port"], port)
                    self.assertIn("--hostname=" + origin, identity.arguments(plan))
                    self.assertIn("--https-port=8443", identity.arguments(plan))
        for value in (True, False, 0, -1, 65536, "8443", None, "8443/path"):
            plan = fixture(); plan["public_port"] = value
            with self.subTest(invalid=value), self.assertRaises(ValueError): identity.render(plan, "raw")

    def test_all_five_packages_retain_unverified_acceptance_and_bind_their_contents(self):
        for runtime in identity.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = identity.render(fixture(), runtime); status = json.loads(files["render-status.json"])
                self.assertTrue(status["rendered"]); self.assertEqual(status["runtime"], runtime)
                for key in ("deployed", "realm_imported", "mfa_verified", "database_verified", "cache_cluster_verified", "ha_verified", "full_stack_verified"): self.assertIs(status[key], False)
                contract = json.loads(files["keycloak-contract.json"])
                self.assertEqual(contract["runtime"], runtime)
                self.assertEqual(set(contract["files_sha256"]), set(files) - {"keycloak-contract.json", "render-status.json"})
                for name, digest in contract["files_sha256"].items(): self.assertEqual(digest, hashlib.sha256(files[name].encode()).hexdigest())

    def test_database_tls_cluster_and_no_automatic_schema_change(self):
        arguments = identity.arguments(fixture())
        self.assertEqual(arguments[:2], ["start", "--optimized"])
        self.assertFalse(any(a.startswith(("--db=", "--health-enabled=", "--metrics-enabled=", "--transaction-xa-enabled=")) for a in arguments))
        self.assertEqual(identity.build_arguments(), ["build", "--db=postgres", "--health-enabled=true", "--metrics-enabled=true", "--transaction-xa-enabled=false"])
        self.assertIn("--cache-stack=jdbc-ping", arguments); self.assertIn("--cache-embedded-mtls-enabled=true", arguments)
        self.assertIn("--spi-connections-jpa--quarkus--migration-strategy=validate", arguments)
        self.assertIn("--spi-connections-jpa--quarkus--initialize-empty=false", arguments)
        url = next(a for a in arguments if a.startswith("--db-url="))
        self.assertIn("sslmode=verify-full&sslrootcert=/run/platform-keycloak/db-ca/ca.crt", url)
        self.assertIn("--hostname=https://identity.example.com", arguments)
        self.assertIn("--http-enabled=false", arguments); self.assertFalse(any("db-password=" in a or "import-realm" in a or a == "start-dev" for a in arguments))
        self.assertIn("--log-level=info,org.hibernate.orm.jdbc.error:off,org.hibernate.orm.jdbc.batch:off", arguments)

    def test_runtime_diagnostic_never_echoes_sql_payloads_and_totp_matches_rfc_vector(self):
        from test_keycloak_runtime_fixture import failure_signals, otp
        marker = "fixture-value-that-must-never-be-echoed"
        report = failure_signals("SQLState: 23505\nBatch parameter prvKey=" + marker + "\nBuild time option: --db not usable")
        self.assertTrue(report["build_time_option_rejected"]); self.assertEqual(report["sqlstates"], ["23505"])
        self.assertNotIn(marker, json.dumps(report)); self.assertNotIn("prvKey", json.dumps(report))
        self.assertEqual(otp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", now=59), "287082")

    def test_three_separate_domains_and_secret_references_only(self):
        deployment, service, disruption, network = identity.kubernetes(fixture())
        self.assertEqual(deployment["spec"]["replicas"], 3); self.assertEqual(disruption["spec"]["minAvailable"], 2)
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"]); self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertEqual(pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"], "topology.kubernetes.io/zone")
        self.assertEqual(len(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]), 3)
        container = pod["containers"][0]
        self.assertEqual(container["env"][0]["valueFrom"]["secretKeyRef"], {"name": "keycloak-database", "key": "password"})
        self.assertEqual(container["readinessProbe"]["httpGet"]["scheme"], "HTTPS")
        self.assertEqual(service["spec"]["ports"], [{"name": "https", "port": 8443, "targetPort": "https"}])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertTrue(all(v["readOnly"] for v in container["volumeMounts"] if v["name"] in {"tls", "db-ca"}))

    def test_network_policy_limits_database_peers_and_management(self):
        network = identity.kubernetes(fixture())[-1]["spec"]
        self.assertEqual(network["policyTypes"], ["Ingress", "Egress"])
        peer = network["ingress"][-1]
        self.assertEqual({p["port"] for p in peer["ports"]}, {7800, 57800})
        self.assertEqual(peer["from"], [{"podSelector": {"matchLabels": identity.LABELS}}])
        self.assertEqual(network["egress"][-1]["to"], [{"ipBlock": {"cidr": "192.0.2.0/24"}}])
        self.assertEqual(network["egress"][-1]["ports"], [{"protocol": "TCP", "port": 5432}])
        self.assertEqual(network["ingress"][1]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], "monitoring")

    def test_two_member_placement_remains_redundant(self):
        plan = fixture(); plan["members"].pop()
        objects = identity.kubernetes(identity.validate(plan))
        self.assertEqual(objects[0]["spec"]["replicas"], 2); self.assertEqual(objects[2]["spec"]["minAvailable"], 1)

    def test_docker_uses_separate_hosts_and_explicit_cluster_addresses(self):
        plan = fixture(); files = identity.render(plan, "docker")
        for member in plan["members"]:
            service = yaml.safe_load(files[member["name"] + "-compose.yaml"])["services"][member["name"]]
            self.assertEqual(service["network_mode"], "host"); self.assertNotIn("ports", service)
            self.assertIn("--cache-embedded-network-bind-address=" + member["address"], service["command"])
            self.assertEqual(service["env_file"], [{"path": "/etc/platform-keycloak/database.env", "required": True, "format": "raw"}])
            self.assertTrue(service["read_only"]); self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertTrue(all(v["read_only"] and not v["bind"]["create_host_path"] for v in service["volumes"]))
            self.assertTrue(any(v.startswith("/opt/keycloak/data:") for v in service["tmpfs"]))

    def test_native_java_has_release_locks_private_credentials_and_local_data(self):
        plan = fixture(); files = identity.render(plan, "raw")
        for member in plan["members"]:
            unit = files[member["name"] + ".service"]
            self.assertIn("User=keycloak", unit); self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("EnvironmentFile=/etc/platform-keycloak/database.env", unit)
            self.assertIn("/opt/keycloak-release/bin/kc.sh start --optimized", unit)
            self.assertIn("BindPaths=/var/lib/platform-keycloak-" + member["name"] + ":/opt/keycloak-release/data", unit)
            self.assertLess(unit.index("ExecStartPre="), unit.index("ExecStart="))
            self.assertNotIn("docker", unit.casefold())
        self.assertEqual(files["native-manifest.sha256"], "b" * 64 + "  /etc/platform-keycloak/native-files.sha256\n")

    def test_fresh_realm_requires_password_and_otp_but_contains_no_users_or_clients(self):
        realm = identity.new_realm(fixture()); self.assertEqual(realm["browserFlow"], "platform-browser-mfa")
        forms = next(f for f in realm["authenticationFlows"] if f["alias"].endswith("-forms"))
        self.assertEqual([(e["authenticator"], e["requirement"]) for e in forms["authenticationExecutions"]], [("auth-username-password-form", "REQUIRED"), ("auth-otp-form", "REQUIRED")])
        self.assertTrue(realm["requiredActions"][0]["defaultAction"]); self.assertTrue(realm["bruteForceProtected"])
        self.assertNotIn("users", realm); self.assertNotIn("clients", realm); self.assertFalse(realm["registrationAllowed"])

    def test_malformed_or_weakened_plans_refuse(self):
        cases = []
        for key, value in (("schema_version", True), ("version", "latest"), ("version", "27.0.0"), ("image", "keycloak:26.4.7"), ("realm", "master"), ("hostname", "https://identity.example.com/?x=1"), ("private_dir", "/tmp/../etc"), ("native_manifest_sha256", "unknown")):
            p = fixture(); p[key] = value; cases.append(p)
        for field, value in (("postgres_major", 18), ("port", True), ("host", "host/db?sslmode=disable"), ("name", "keycloak?x=y"), ("password", "literal-test-only")):
            p = fixture(); p["database"][field] = value; cases.append(p)
        for key in ("node", "address", "failure_domain"):
            p = fixture(); p["members"][1][key] = p["members"][0][key]; cases.append(p)
        p = fixture(); p["members"] = p["members"][:1]; cases.append(p)
        p = fixture(); p["network"]["database_cidrs"] = ["0.0.0.0/0"]; cases.append(p)
        p = fixture(); p["network"]["database_cidrs"] = ["192.0.2.1/24"]; cases.append(p)
        for p in cases:
            with self.subTest(plan=p), self.assertRaises(ValueError): identity.validate(p)

    def test_private_render_is_fresh_and_never_reads_an_ambient_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); plan = root / "plan.json"; plan.write_text(json.dumps(fixture()))
            args = ["--plan", str(plan), "--runtime", "raw", "--output-dir", str(root / "private" / "package")]
            with mock.patch.object(identity, "ROOT", root), mock.patch.dict(os.environ, {"KC_DB_PASSWORD": "dummy-value-that-must-not-be-copied"}), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(identity.main(args), 0)
            self.assertNotIn(str(root), output.getvalue())
            before = {p.name: p.read_bytes() for p in (root / "private/package").iterdir()}
            self.assertTrue(all(b"dummy-value-that-must-not-be-copied" not in v for v in before.values()))
            with mock.patch.object(identity, "ROOT", root), contextlib.redirect_stderr(io.StringIO()): self.assertEqual(identity.main(args), 1)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (root / "private/package").iterdir()})

    def test_unknown_runtime_refuses(self):
        with self.assertRaises(ValueError): identity.render(fixture(), "hidden-cluster")

    def test_plan_mapping_order_does_not_change_the_rendered_contract(self):
        def reverse(value):
            if isinstance(value, dict): return {key: reverse(item) for key, item in reversed(list(value.items()))}
            if isinstance(value, list): return [reverse(item) for item in value]
            return value
        original = fixture()
        for runtime in identity.RUNTIMES: self.assertEqual(identity.render(original, runtime), identity.render(reverse(copy.deepcopy(original)), runtime))

    @unittest.skipUnless(os.environ.get("KEYCLOAK_TEST_COMPOSE") == "1", "Explicit opt-in to actual Docker Compose config validation")
    def test_actual_compose_configuration_needs_no_daemon_credentials_or_host_files(self):
        from bounded_subprocess import run_bounded
        compose = shutil.which("docker-compose")
        if not compose: self.fail("The opt-in check requires a reviewed standalone Docker Compose CLI on PATH")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "docker-config").mkdir()
            environment = {**os.environ, "DOCKER_CONFIG": str(root / "docker-config"), "COMPOSE_DISABLE_ENV_FILE": "1"}
            plan = fixture(); files = identity.render(plan, "docker")
            for member in plan["members"]:
                path = root / (member["name"] + ".yaml"); path.write_text(files[member["name"] + "-compose.yaml"])
                result = run_bounded([compose, "--project-directory", str(root), "--project-name", "keycloak-fixture", "-f", str(path),
                    "config", "--no-env-resolution", "--no-path-resolution", "--format", "json"], env=environment, text=True, timeout=30, output_max_bytes=128 * 1024, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                service = json.loads(result.stdout)["services"][member["name"]]
                self.assertEqual(service["command"], identity.arguments(plan, member=member))
                self.assertEqual(service["env_file"][0]["format"], "raw")
                self.assertEqual(service["network_mode"], "host"); self.assertTrue(service["read_only"])
                self.assertEqual(service["image"], plan["image"])

    @unittest.skipUnless(os.environ.get("KEYCLOAK_TEST_API") == "1", "Explicit opt-in to the disposable loopback API fixture")
    def test_actual_kubernetes_admission_and_readback_without_running_keycloak(self):
        from test_rook_api_fixture import exercise
        exercise(self, resource_objects=identity.kubernetes(fixture()))

    @unittest.skipUnless(os.environ.get("KEYCLOAK_TEST_RUNTIME") == "1", "Explicit opt-in to disposable Keycloak/PostgreSQL browser-MFA processes")
    def test_real_tls_runtime_mfa_and_refresh_through_second_process(self):
        from test_keycloak_runtime_fixture import exercise
        report = exercise(self)
        for key in ("two_process_tls_startup_verified", "owned_loopback_listeners_verified", "guarded_realm_creation_and_readback_verified", "existing_realm_and_resume_boundaries_verified", "guarded_oidc_client_and_group_claim_verified", "oidc_callback_pkce_and_disabled_grants_verified", "signed_id_token_claims_verified", "otp_enrollment_and_login_verified", "password_only_and_bad_otp_rejected", "refresh_after_first_process_stopped", "generated_private_values_absent_from_logs", "temporary_processes_and_data_removed"):
            self.assertTrue(report[key])
        self.assertFalse(report["ha_verified"]); self.assertFalse(report["physical_node_loss_verified"]); self.assertFalse(report["full_stack_verified"])


if __name__ == "__main__": unittest.main()
