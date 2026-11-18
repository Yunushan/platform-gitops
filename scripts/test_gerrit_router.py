#!/usr/bin/env python3
"""Validate Gerrit routing and all five packaging adapters without deployment."""

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import yaml

import check_gerrit_router_backends as backends
import check_gerrit_native_vip as native_vip
import render_gerrit_router as router


def fixture():
    return {"schema_version": 1, "canonical_host": "review.example.com",
            "primary_host": "gerrit-a.example.com", "backup_host": "gerrit-b.example.com",
            "https_port": 8443, "ssh_port": 29418, "namespace": "gerrit",
            "ca_secret": "gerrit-backend-ca",
            "haproxy_image": "docker.io/library/haproxy@sha256:" + "a" * 64}


class GerritRouterTests(unittest.TestCase):
    def test_external_ssh_service_is_opt_in_and_keeps_https_cluster_local(self):
        plan = {**fixture(), "ssh_node_port": 30294}
        for runtime in ("rke2", "k3s", "k8s"):
            resources = list(yaml.safe_load_all(router.render(plan, runtime)["router.yaml"]))
            services = {item["metadata"]["name"]: item["spec"] for item in resources if item["kind"] == "Service"}
            self.assertEqual(services["gerrit-router"]["type"], "ClusterIP")
            self.assertEqual(services["gerrit-router-ssh"], {
                "type": "NodePort", "externalTrafficPolicy": "Local", "selector": router.LABELS,
                "ports": [{"name": "ssh", "protocol": "TCP", "port": 29418, "targetPort": "ssh", "nodePort": 30294}]})
        for runtime in ("raw", "docker"):
            with self.assertRaises(router.RouterError):
                router.render(plan, runtime)
        with self.assertRaises(router.RouterError):
            router.render({**plan, "ssh_node_port": 29418}, "k3s")

    def test_native_vip_preflight_binds_interface_address_and_prefix(self):
        host = {"interface": "eth0", "address": "192.0.2.10"}
        address = {"family": "inet", "local": host["address"], "prefixlen": 24}
        interface = {"ifname": "eth0", "flags": ["UP"], "addr_info": [address]}
        self.assertTrue(native_vip.selected_interface(host, "192.0.2.50/24", [interface]))
        for invalid in ({**interface, "ifname": "eth1"},
                        {**interface, "flags": []},
                        {**interface, "addr_info": [{**address, "prefixlen": 25}]}):
            self.assertFalse(native_vip.selected_interface(host, "192.0.2.50/24", [invalid]))

    def test_native_vip_config_and_host_validation(self):
        topology = {"vip": "192.0.2.50/24", "vrid": 73, "entry_hosts": [
            {"name": "a", "address": "192.0.2.10", "interface": "eth0", "failure_domain": "rack-a"},
            {"name": "b", "address": "192.0.2.11", "interface": "eth0", "failure_domain": "rack-b"},
        ]}
        plan = {**fixture(), "native_vip": topology}
        files = router.render(plan, "raw")
        for name, peer in (("a", "192.0.2.11"), ("b", "192.0.2.10")):
            config = files[f"keepalived-{name}.conf"]
            self.assertIn(f"{peer} min_ttl 255 max_ttl 255", config)
            self.assertIn(f"--identity {router.route_config_id(plan)} --gerrit", config)
            self.assertIn("192.0.2.50/24 dev eth0", config)
        self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SETGID",
                      files["gerrit-router-vrrp.service"])
        self.assertIn("def healthy", files["gateway_edge_health.py"])
        for runtime in ("docker", "rke2", "k3s", "k8s"):
            with self.assertRaises(router.RouterError):
                router.render(plan, runtime)
        for field, value in (("address", "192.0.2.50"), ("address", "192.0.2.10"),
                             ("address", "192.0.3.10"), ("interface", "lo"),
                             ("interface", "eth0\nscript injected"), ("failure_domain", "rack-a")):
            invalid = copy.deepcopy(plan)
            invalid["native_vip"]["entry_hosts"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(router.RouterError):
                router.render(invalid, "raw")

    @unittest.skipUnless(os.environ.get("GERRIT_ROUTER_TEST_API") == "1", "Explicit isolated Kubernetes API fixture")
    def test_kubernetes_router_admission(self):
        from test_rook_api_fixture import exercise

        files = router.render({**fixture(), "hold_primary": True, "ssh_node_port": 30294}, "rke2")
        exercise(self, resource_objects=list(yaml.safe_load_all(files["router.yaml"])))

    @unittest.skipUnless(os.environ.get("GERRIT_ROUTER_TEST_HOLD") == "1", "Explicit real HAProxy hold fixture")
    def test_real_haproxy_persistent_primary_hold(self):
        from test_gerrit_router_runtime_fixture import exercise

        exercise(self, hold_primary=True)

    @unittest.skipUnless(os.environ.get('GERRIT_ROUTER_TEST_RUNTIME') == '1', 'Explicit real pinned HAProxy combined-health protocol fixture')
    def test_real_haproxy_combined_health_and_write_route_selection(self):
        from test_gerrit_router_runtime_fixture import exercise
        exercise(self)

    def test_shared_health_and_tls_passthrough(self):
        config = router.haproxy_config(fixture())
        self.assertIn("tcp-check connect port 29418", config)
        self.assertIn("tcp-check expect rstring ^SSH-2.0-", config)
        self.assertIn("tcp-check connect port 8443 ssl sni review.example.com", config)
        self.assertEqual(config.count("verify required ca-file"), 2)
        self.assertEqual(config.count("verifyhost review.example.com"), 2)
        self.assertIn("track gerrit_https/primary", config)
        self.assertIn("track gerrit_https/backup backup", config)
        self.assertEqual(config.count("on-marked-down shutdown-sessions"), 2)
        self.assertIn("default-server init-state fully-down inter 3s fall 3 rise 2 ", config)
        self.assertIn("monitor fail if { nbsrv(gerrit_https) lt 1 } || { nbsrv(gerrit_ssh) lt 1 }", config)
        self.assertIn("tcp-request connection reject if { nbsrv(gerrit_https) lt 1 }", config)
        self.assertIn("use-server backup if { srv_is_up(gerrit_https/backup) }", config)
        self.assertTrue(all(line.endswith("on-marked-up shutdown-backup-sessions")
                            for line in config.splitlines() if line.strip().startswith("server primary ")))
        self.assertEqual(config.count("on-marked-up shutdown-backup-sessions"), 2)
        self.assertNotIn("verify none", config)
        self.assertNotIn("balance roundrobin", config)
        self.assertNotIn("ssl crt", config)
        self.assertIn("GET /config/server/healthcheck~status HTTP/1.1".encode().hex(), config)
        # Never wrap the already encrypted application stream in another TLS
        # connection: 'ssl' belongs only on the health-check connect line.
        self.assertFalse(any(" ssl " in line for line in config.splitlines() if line.strip().startswith("server ")))

    def test_preflight_rejects_missing_disabled_and_failed_probes_without_printing_details(self):
        checks = {name: {"result": "passed"} for name in backends.REQUIRED_HEALTHCHECKS}
        healthy = backends.parse_health(200, b")]}'\n" + json.dumps(checks).encode())
        self.assertTrue(healthy["health_verified"])
        del checks["gitspace"]
        checks["querychanges"] = {"result": "failed", "error": "private repository name"}
        checks["auth"] = {"result": "disabled"}
        checks["private-other-check"] = {"result": "failed"}
        report = backends.parse_health(500, json.dumps(checks).encode())
        self.assertEqual(report["check_failures"], ["auth", "gitspace", "querychanges"])
        self.assertTrue(report["additional_checks_failed"])
        self.assertFalse(report["health_verified"])
        self.assertNotIn("private", json.dumps(report))
        self.assertFalse(backends.parse_health(200, b'{"ts":1,"ts":2}')["health_verified"])

    def test_preflight_requires_two_distinct_healthy_backends(self):
        good = {"tls_verified": True, "health_verified": True, "ssh_banner_verified": True}
        for addresses, reports, expected in (
            (("192.0.2.2", "192.0.2.3"), (good, good), True),
            (("192.0.2.2", "192.0.2.2"), (good, good), False),
            (("192.0.2.2", "192.0.2.3"), (good, {**good, "ssh_banner_verified": False}), False),
        ):
            with self.subTest(addresses=addresses, expected=expected):
                with mock.patch.object(backends.ssl, "SSLContext"), mock.patch.object(
                        backends, "probe", side_effect=zip(addresses, reports)):
                    result = backends.check(fixture(), b"dummy CA")
                self.assertIs(result["backends_ready"], expected)
                self.assertNotIn("192.0.2.", json.dumps(result))

    def test_ssh_identification_accepts_following_binary_packet(self):
        with mock.patch.object(backends.socket, "socket") as constructor:
            connection = constructor.return_value.__enter__.return_value
            connection.recv.side_effect = [b"SSH-2.", b"0-Gerrit\r\n\x00\xff"]
            self.assertTrue(backends.ssh_banner("192.0.2.2", 29418, socket.AF_INET))
            connection.connect.assert_called_once_with(("192.0.2.2", 29418))

    def test_recovery_evidence_requires_unique_positive_numbers_and_exact_revisions(self):
        change = {"number": 123, "revision": "a" * 40}
        evidence = {"username": "recovery-reader", "changes": [change]}
        self.assertEqual(backends.recovery_evidence(evidence), ("recovery-reader", {123: "a" * 40}))
        for changes in ([], [change, change], [{**change, "number": True}],
                        [{**change, "revision": "a" * 39}], [{**change, "revision": "0" * 40}]):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                backends.recovery_evidence({**evidence, "changes": changes})
        with self.assertRaises(ValueError):
            backends.check(fixture(), b"dummy CA", evidence=evidence)

    def test_route_inventory_rejects_shared_addresses_and_ports_when_duplicated(self):
        first = {"address": "192.0.2.1", "port": 8404}
        for second in ({**first, "address": "::ffff:192.0.2.1"},
                       {**first, "address": "0.0.0.0"},
                       {**first, "port": False}):
            with self.subTest(second=second), self.assertRaises(ValueError):
                backends.check_routers(fixture(), [first, second])

    def test_route_inventory_checks_configuration_and_role(self):
        held = {**fixture(), "hold_primary": True}
        held_id = router.route_config_id(held)
        normal_id = router.route_config_id(fixture())
        self.assertNotEqual(held_id, normal_id)
        inventory = [{"address": f"192.0.2.{index}", "port": 8404} for index in (1, 2, 3)]
        with mock.patch.object(backends.http.client, "HTTPConnection") as connect:
            response = connect.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.side_effect = [f"{held_id} backup".encode(), b"", f"{normal_id} backup".encode(), b"",
                                         f"{held_id} primary".encode()]
            report = backends.check_routers(held, inventory)
        self.assertFalse(report["router_rollout_verified"])
        self.assertEqual(report["routers_matching"], 1)
        self.assertEqual(report["routers_not_matching"], [2, 3])
        self.assertNotIn("192.0.2.", json.dumps(report))

    def test_every_runtime_has_real_artifacts_and_no_acceptance_claim(self):
        for runtime in router.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = router.render(fixture(), runtime)
                self.assertIn("haproxy.cfg", files)
                self.assertIn('"ha_verified": false', files["render-status.json"])
                self.assertIn('"deployed": false', files["render-status.json"])
                if runtime in {"k8s", "rke2", "k3s"}:
                    self.assertIn("router.yaml", files)
                elif runtime == "docker":
                    self.assertIn("compose.yaml", files)
                else:
                    self.assertIn("gerrit-router.service", files)

    def test_primary_hold_is_explicit_and_applies_to_both_protocols(self):
        self.assertEqual(router.haproxy_config(fixture()), router.haproxy_config({**fixture(), "hold_primary": False}))
        for runtime in router.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = router.render({**fixture(), "hold_primary": True}, runtime)
                held = [line for line in files["haproxy.cfg"].splitlines() if line.lstrip().startswith("server ")]
                self.assertEqual([line.split()[1] for line in held if line.endswith(" disabled")], ["primary", "primary"])
                self.assertTrue(json.loads(files["render-status.json"])["hold_primary"])
                self.assertEqual(files["haproxy.cfg"].count("check verify required"), 2)
                if "router.yaml" in files:
                    config = next(yaml.safe_load_all(files["router.yaml"]))
                    self.assertEqual(config["data"]["haproxy.cfg"], files["haproxy.cfg"])

    def test_kubernetes_replication_is_node_separated_and_secret_backed(self):
        files = router.render(fixture(), "k8s")
        resources = list(yaml.safe_load_all(files["router.yaml"]))
        by_kind = {r["kind"]: r for r in resources}
        self.assertEqual(set(by_kind), {"ConfigMap", "Deployment", "PodDisruptionBudget", "Service"})
        deployment = by_kind["Deployment"]["spec"]
        self.assertEqual(deployment["replicas"], 2)
        self.assertEqual(deployment["strategy"]["rollingUpdate"], {"maxUnavailable": 1, "maxSurge": 0})
        pod = deployment["template"]["spec"]
        self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(pod["volumes"][1]["secret"]["secretName"], "gerrit-backend-ca")
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/readyz", "port": "readiness"})
        self.assertEqual(container["livenessProbe"]["tcpSocket"], {"port": "https"})
        self.assertTrue(by_kind["ConfigMap"]["immutable"])
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], by_kind["ConfigMap"]["metadata"]["name"])
        self.assertNotIn("Secret", by_kind)
        self.assertEqual(by_kind["Service"]["spec"]["type"], "ClusterIP")
        self.assertEqual([p["name"] for p in by_kind["Service"]["spec"]["ports"]], ["https", "ssh"])
        self.assertEqual(by_kind["PodDisruptionBudget"]["spec"]["minAvailable"], 1)
        self.assertEqual(by_kind["ConfigMap"]["data"]["haproxy.cfg"], files["haproxy.cfg"])

    def test_kubernetes_config_changes_replace_the_pod_template_config_reference(self):
        plan = fixture()
        original = router.kubernetes_resources(plan, router.haproxy_config(plan))
        changed_plan = {**plan, "primary_host": "replacement.example.com"}
        changed = router.kubernetes_resources(changed_plan, router.haproxy_config(changed_plan))
        before, after = [objects[1]["spec"]["template"] for objects in (original, changed)]
        self.assertNotEqual(before["spec"]["volumes"][0], after["spec"]["volumes"][0])
        self.assertEqual(original[1]["spec"]["selector"], changed[1]["spec"]["selector"])

    def test_compose_is_one_explicit_host_without_privileged_mounts(self):
        service = yaml.safe_load(router.render(fixture(), "docker")["compose.yaml"])["services"]["gerrit-router"]
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertNotIn("privileged", service)
        self.assertFalse(any("docker.sock" in str(v) for v in service["volumes"]))
        self.assertTrue(all(v["read_only"] and not v["bind"]["create_host_path"] for v in service["volumes"]))

    def test_native_unit_runs_unprivileged_and_validates_config(self):
        unit = router.render(fixture(), "raw")["gerrit-router.service"]
        self.assertIn("User=haproxy", unit)
        self.assertIn("ExecStartPre=/usr/sbin/haproxy -c", unit)
        self.assertIn("ProtectSystem=strict", unit)

    def test_reject_bad_inputs_before_writing(self):
        cases = [None, [], {}, {**fixture(), "password": "not-allowed"}]
        for key, value in (
            ("primary_host", "gerrit-a.example.com\nserver injected"),
            ("backup_host", "gerrit-a.example.com"),
            ("canonical_host", "user:password@example.com"),
            ("namespace", "../escape"), ("ca_secret", "secret/name"),
            ("haproxy_image", "haproxy:latest"), ("https_port", True),
            ("https_port", 443), ("ssh_port", 8443), ("schema_version", True),
            ("hold_primary", "false"), ("hold_primary", 1), ("hold_primary", None),
        ):
            plan = fixture()
            plan[key] = value
            cases.append(plan)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(router.RouterError):
                router.render(case, "rke2")
        with self.assertRaises(router.RouterError):
            router.render(fixture(), "invalid")

    def test_new_private_output_only_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            files = router.render(fixture(), "docker")
            with self.assertRaises(router.RouterError):
                router.write_bundle(files, base / "public", private_root=base / "private")
            self.assertFalse((base / "public").exists())
            output = base / "private" / "router"
            router.write_bundle(files, output, private_root=base / "private")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(router.RouterError):
                router.write_bundle(files, output, private_root=base / "private")
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_no_private_material_in_error_messages(self):
        plan = copy.deepcopy(fixture())
        plan["primary_host"] = "a-private-value\n"
        with self.assertRaises(router.RouterError) as caught:
            router.validate(plan)
        self.assertNotIn("a-private-value", str(caught.exception))

    def test_file_name_escape_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("../escape", "/absolute", "dir/file", "..", "a\\escape"):
                output = base / "private" / "out"
                with self.assertRaises(router.RouterError):
                    router.write_bundle({name: "bad"}, output, private_root=base / "private")
                self.assertFalse(output.exists())

    def test_cli_writes_private_artifacts_without_echoing_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_file = root / "plan.json"
            plan_file.write_text(json.dumps(fixture()), encoding="utf-8")
            output = root / "private" / "generated"
            args = ["--plan", str(plan_file), "--runtime", "k3s", "--output-dir", str(output)]
            with mock.patch.object(router, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(router.main(args), 0)
            result = json.loads(stream.getvalue())
            self.assertTrue(result["rendered"])
            self.assertFalse(result["deployed"])
            self.assertFalse(result["ha_verified"])
            self.assertNotIn("example.com", stream.getvalue())
            self.assertTrue((output / "router.yaml").is_file())
            # Re-running cannot replace even one file in this bundle.
            before = (output / "haproxy.cfg").read_bytes()
            with mock.patch.object(router, "ROOT", root), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(router.main(args), 1)
            self.assertNotIn(str(output), error.getvalue())
            self.assertEqual((output / "haproxy.cfg").read_bytes(), before)

    def test_duplicate_input_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_file = root / "plan.json"
            plan_file.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
            output = root / "private" / "generated"
            with mock.patch.object(router, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(router.main(["--plan", str(plan_file), "--runtime", "raw",
                                              "--output-dir", str(output)]), 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
