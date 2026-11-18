#!/usr/bin/env python3
"""Fail before service execution when artifacts or credential destinations drift."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_harbor_service_runtime_fixture as fixture


class ServiceContractTests(unittest.TestCase):
    def test_api_authentication_cannot_be_redirected(self):
        client = fixture.ServiceClient("192.0.2.91", 8443, None, "a" * 64)
        for path in ("https://other.example.com/api/v2.0/projects", "//other.example.com/api/v2.0/projects", "/api/v2.0/projects?next=x", "/api/v2.0/projects\r\nHeader:value", "/api/v2.0/../config", "/config"):
            with self.subTest(path=path), self.assertRaises(ValueError): client.request("POST", path, authorization="synthetic-placeholder")
        with self.assertRaises(ValueError): fixture.ServiceClient("192.0.2.94", 8443, None, "a" * 64)
        with self.assertRaises(ValueError): fixture.ServiceClient("192.0.2.91", 80, None, "a" * 64)

    def test_service_build_status_types_and_executable_drift_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); name = "harbor-registryctl"; raw = b"synthetic artifact; never executed"
            digest = hashlib.sha256(raw).hexdigest(); (root / name).write_bytes(raw)
            receipt = {"exit": {"exit_code": 0}, "original_sources_unchanged": True, "original_process_and_cgroup_removed": True,
                "source_revision": fixture.REVISION, "source_archive_sha256": fixture.SOURCE_SHA, "go_archive_sha256": fixture.GO_SHA, "runtime_verified": False, "binary_sha256": digest}
            with mock.patch.dict(fixture.BINARIES, {name: digest}):
                for change in ({"exit": {"exit_code": False}}, {"original_sources_unchanged": 1}, {"original_process_and_cgroup_removed": False}, {"runtime_verified": 0}, {"source_revision": "0" * 40}, {"binary_sha256": "a" * 64}):
                    (root / "build-inputs.json").write_text(json.dumps({**receipt, **change}))
                    with self.assertRaises(ValueError): fixture.stage_binary(root, name, root / "staged")
                    self.assertFalse((root / "staged").exists())
                (root / "build-inputs.json").write_text(json.dumps(receipt)); (root / name).write_bytes(b"changed")
                with self.assertRaises(ValueError): fixture.stage_binary(root, name, root / "staged")
                (root / name).write_bytes(raw); fixture.stage_binary(root, name, root / "staged")
                with self.assertRaises(FileExistsError): fixture.stage_binary(root, name, root / "staged")
                self.assertEqual((root / "staged").read_bytes(), raw)

    def test_database_test_route_refuses_unreviewed_commands_before_socket_access(self):
        from test_harbor_database_promotion_fixture import DatabasePromotionChecks
        route = DatabasePromotionChecks()
        with mock.patch("test_harbor_database_promotion_fixture.socket.socket") as connect:
            for value in ("shutdown sessions server unreviewed/primary", "set server db/primary addr 192.0.2.99 port 5432", "show servers state db\nshutdown frontend application_database", "set server db/primary addr 127.0.0.12 port 80"):
                with self.subTest(command=value), self.assertRaises(ValueError): route.route(value)
            connect.assert_not_called()

    def test_database_test_route_checks_original_backend_and_preserves_cli_blank_lines(self):
        from test_harbor_database_promotion_fixture import DatabasePromotionChecks
        route = DatabasePromotionChecks()
        state = "1\n# be_id be_name srv_id srv_name srv_addr srv_port srv_admin_state srv_op_state\n1 db 1 primary 127.0.0.12 5432 0 2\n\n"
        with mock.patch.object(route, "route", return_value=state) as read:
            self.assertTrue(route.routed_to("127.0.0.12")); self.assertFalse(route.routed_to("127.0.0.11"))
            self.assertTrue(all(call.args == ("show servers state db",) for call in read.call_args_list))
        for value in (state.replace("1 db 1", "1 another_backend 1"), state.replace("primary 127", "standby 127"), state.replace(" 5432 ", " 6432 ")):
            with mock.patch.object(route, "route", return_value=value): self.assertFalse(route.routed_to("127.0.0.12"))
        for value in (state.replace("5432 0 2", "5432 1 2"), state.replace("5432 0 2", "5432 0 0")):
            with mock.patch.object(route, "route", return_value=value): self.assertFalse(route.routed_to("127.0.0.12"))
        for value in (state.replace("1\n#", "2\n#"), state + "1 db 2 primary 127.0.0.12 5432\n", state.replace("srv_port", "srv_port extra")):
            with mock.patch.object(route, "route", return_value=value), self.assertRaises(ValueError): route.routed_to("127.0.0.12")

    def test_jwt_upload_state_cannot_redirect_bearer_credentials(self):
        from test_harbor_jwt_runtime_fixture import ORIGIN, REPOSITORY, original_location
        path = REPOSITORY + "/blobs/uploads/00000000-0000-0000-0000-000000000001?_state=original"
        self.assertEqual(original_location(ORIGIN + path), path)
        self.assertEqual(original_location(path), path)
        for value in ("https://other.example.com" + path, "http://192.0.2.91:8443" + path, "https://user@192.0.2.91:8443" + path,
                "https://192.0.2.91:443" + path, "//other.example.com" + path, ORIGIN + path + "#fragment",
                ORIGIN + path + "&_state=second", ORIGIN + path.replace("fixture/ha", "other/ha")):
            with self.subTest(value=value), self.assertRaises(ValueError): original_location(value)

    def test_jwt_shape_inspection_refuses_unbounded_and_non_rsa_documents(self):
        from test_harbor_jwt_runtime_fixture import encoded, token_parts
        cases = [None, "", "x" * 32769, "header.payload", "header.payload.signature.extra"]
        cases += [encoded(json.dumps(header).encode()) + "." + encoded(json.dumps(claims).encode()) + ".c2ln" for header, claims in (({"alg": "none"}, {}), ({"alg": "HS256"}, {}), ({"alg": "RS256"}, []), ([], {}))]
        for value in cases:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError): token_parts(value)

    def test_jwt_requests_refuse_unreviewed_destinations_and_header_injection(self):
        from types import SimpleNamespace
        from test_harbor_jwt_runtime_fixture import CoreRegistry
        service = SimpleNamespace(address="192.0.2.91", port=8443, trust=None, leaf="a" * 64)
        client = CoreRegistry(service)
        with mock.patch("test_harbor_jwt_runtime_fixture.http.client.HTTPSConnection") as connect:
            for path in ("https://other.example.com/v2/fixture/ha/manifests/tag", "//other.example.com/v2/fixture/ha", "/v2/unreviewed/repository", "/v2/fixture/ha/manifests/tag\r\nHeader:value"):
                with self.subTest(path=path), self.assertRaises(ValueError): client.request("GET", path)
            with self.assertRaises(ValueError): client._request("GET", "/v2/", authorization="Bearer value\r\nHeader: injected")
            with self.assertRaises(ValueError): client.token("unreviewed-user", "synthetic")
            with self.assertRaises(ValueError): client.token("admin", "synthetic", repository="unreviewed/ha")
            connect.assert_not_called()
        with self.assertRaises(ValueError): CoreRegistry(SimpleNamespace(address="192.0.2.99", port=8443))

    def test_core_proxy_state_requires_exact_members_and_read_only_observation(self):
        from test_harbor_core_replica_fixture import CoreReplicaChecks
        fixture = CoreReplicaChecks(); fixture.core_route_socket = Path("synthetic-owned-core.sock")
        text = "1\n# be_id be_name srv_id srv_name srv_addr srv_port srv_admin_state srv_op_state\n1 harbor_cores 1 core_a 192.0.2.94 8443 0 2\n1 harbor_cores 2 core_b 192.0.2.95 8443 0 2\n\n"
        def observe(value):
            with mock.patch("test_harbor_core_replica_fixture.socket.AF_UNIX", 1, create=True), mock.patch("test_harbor_core_replica_fixture.socket.socket") as factory:
                connection = factory.return_value.__enter__.return_value
                connection.recv.side_effect = [value.encode(), b""]
                result = fixture.proxy_state()
                connection.sendall.assert_called_once_with(b"show servers state harbor_cores\n")
                return result
        self.assertEqual(observe(text), {"core_a": "2", "core_b": "2"})
        self.assertEqual(observe(text.replace("8443 0 2\n1", "8443 0 0\n1")), {"core_a": "0", "core_b": "2"})
        for value in (text.replace("192.0.2.95", "192.0.2.99"), text.replace("8443 0 2", "8443 1 2"),
                text.replace("core_b", "core_a"), text.replace(" 8443 ", " 9443 "), text.replace("harbor_cores", "unreviewed_backend"), text.replace("1\n#", "2\n#")):
            with self.subTest(value_shape=len(value)), self.assertRaises(ValueError): observe(value)

    def test_core_peer_client_targets_its_reviewed_peer_instead_of_the_frontend(self):
        from types import SimpleNamespace
        from test_harbor_jwt_runtime_fixture import CoreRegistry
        for address in ("192.0.2.94", "192.0.2.95"):
            service = SimpleNamespace(address=address, port=8443, trust=None, leaf="a" * 64)
            response = mock.Mock(status=200); response.read.return_value = b"{}"; response.getheaders.return_value = []
            with mock.patch("http.client.HTTPSConnection.__init__", return_value=None) as construct, mock.patch("http.client.HTTPSConnection.request"), mock.patch("http.client.HTTPSConnection.getresponse", return_value=response), mock.patch("http.client.HTTPSConnection.close"):
                self.assertEqual(CoreRegistry(service).request("GET", "/v2/")[0], 200)
                self.assertEqual(construct.call_args.args, (address, 8443))

    def test_job_stats_refuse_duplicate_stale_replaced_or_untyped_worker_evidence(self):
        from test_harbor_jobservice_replica_fixture import pool_stats
        row = {"worker_pool_id": "owned-pool-a", "started_at": 900, "heartbeat_at": 999,
            "concurrency": 2, "job_names": ["PURGE_AUDIT_LOG"], "status": "Healthy"}
        def parse(rows): return pool_stats(json.dumps({"worker_pools": rows}).encode(), 1000)
        self.assertEqual(set(parse([row])), {"owned-pool-a"})
        self.assertEqual(len(parse([row, {**row, "worker_pool_id": "owned-pool-b"}])), 2)
        self.assertEqual(parse([{**row, "status": "Dead", "heartbeat_at": 950}])["owned-pool-a"]["status"], "Dead")
        invalid = [[], [row, row], [row] * 3, [None]]
        invalid += [[{**row, key: value}] for key, value in (("started_at", True), ("heartbeat_at", 1003),
            ("heartbeat_at", 980), ("heartbeat_at", 899), ("concurrency", True), ("concurrency", 3),
            ("job_names", []), ("status", "Unknown"), ("worker_pool_id", "injected\nvalue"))]
        for rows in invalid:
            with self.subTest(shape=str(rows)[:100]), self.assertRaises(ValueError): parse(rows)

    def test_job_survivor_requires_original_pool_start_and_no_second_healthy_worker(self):
        from test_harbor_jobservice_replica_fixture import JobReplicaChecks
        checks = JobReplicaChecks(); checks.job_clients = [None, None]
        checks.peer_pool_id = "original-peer"; checks.peer_pool_started = 900
        row = {"started_at": 900, "status": "Healthy"}
        checks.read_pools = mock.Mock(return_value={"original-peer": row})
        self.assertTrue(checks.survivor_healthy())
        checks.read_pools.return_value = {"original-peer": row, "old-primary": {"status": "Dead"}}
        self.assertTrue(checks.survivor_healthy())
        for pools in (None, {"replacement": row}, {"original-peer": {**row, "status": "Dead"}},
                {"original-peer": row, "replacement": row}):
            checks.read_pools.return_value = pools; self.assertFalse(checks.survivor_healthy())
        checks.read_pools.return_value = {"original-peer": {**row, "started_at": 901}}
        with self.assertRaises(ValueError): checks.survivor_healthy()

    def test_job_peer_client_bounds_credential_destinations_and_response_size(self):
        from types import SimpleNamespace
        from test_harbor_jobservice_replica_fixture import JobPeerClient
        service = SimpleNamespace(address="192.0.2.92", port=9443, trust=None, leaf="a" * 64)
        client = JobPeerClient(9445, service)
        for port in (True, 9443, 80):
            with self.assertRaises(ValueError): JobPeerClient(port, service)
        with self.assertRaises(ValueError): JobPeerClient(9445, SimpleNamespace(address="other.example.invalid", port=9443))
        with mock.patch("http.client.HTTPSConnection.__init__", return_value=None) as construct, mock.patch("http.client.HTTPSConnection.request") as send, mock.patch("http.client.HTTPSConnection.close"), mock.patch("http.client.HTTPSConnection.getresponse") as receive:
            for path in ("https://other.example.invalid/api/v1/stats", "//other.example.invalid/api/v1/stats", "/api/v1/stats?next=x",
                    "/api/v1/jobs/../log", "/api/v1/stats\r\nHeader:value", "/api/v1/jobs"):
                with self.subTest(path=path), self.assertRaises(ValueError): client.request(path, "synthetic")
            with self.assertRaises(ValueError): client.request("/api/v1/stats", "value\r\nHeader:injected")
            construct.assert_not_called(); send.assert_not_called()
            receive.return_value.status = 200; receive.return_value.read.return_value = b"{}"
            self.assertEqual(client.request("/api/v1/stats"), (200, b"{}"))
            self.assertEqual(construct.call_args.args, ("127.0.0.1", 9445))
            self.assertEqual(send.call_args.args, ("GET", "/api/v1/stats"))
            receive.return_value.read.return_value = b"x" * (1024 ** 2 + 1)
            with self.assertRaises(ValueError): client.request("/api/v1/stats")

    def test_job_proxy_state_requires_only_original_members_and_read_only_command(self):
        from test_harbor_jobservice_replica_fixture import JobReplicaChecks
        checks = JobReplicaChecks(); checks.job_route_socket = Path("synthetic-owned-job.sock")
        text = "1\n# be_id be_name srv_id srv_name srv_addr srv_port srv_admin_state srv_op_state\n1 harbor_jobs 1 job_a 127.0.0.1 9446 0 2\n1 harbor_jobs 2 job_b 127.0.0.1 9445 0 2\n\n"
        def observe(value):
            with mock.patch("test_harbor_jobservice_replica_fixture.socket.AF_UNIX", 1, create=True), mock.patch("test_harbor_jobservice_replica_fixture.socket.socket") as factory:
                connection = factory.return_value.__enter__.return_value; connection.recv.side_effect = [value.encode(), b""]
                result = checks.job_route_state(); connection.sendall.assert_called_once_with(b"show servers state harbor_jobs\n")
                return result
        self.assertEqual(observe(text), {"job_a": "2", "job_b": "2"})
        for value in (text.replace("127.0.0.1", "192.0.2.99"), text.replace("9445 0 2", "9445 1 2"),
                text.replace("job_b", "job_a"), text.replace("9446", "9443"), text.replace("harbor_jobs", "unreviewed")):
            with self.subTest(shape=len(value)), self.assertRaises(ValueError): observe(value)

    def test_audit_job_includes_an_operation_and_refuses_plaintext_or_failed_sql_evidence(self):
        checks = fixture.ServiceChecks()
        job = mock.Mock(); checks.service_clients = {"harbor-jobservice": job}
        success = (200, b'{"job":{"status":"Success"}}')
        def setup(state=success, counts=("2", "0")):
            job.request.reset_mock(); job.request.side_effect = [(401, b""), (403, b""),
                (202, b'{"job":{"id":"owned-job"}}'), state, (200, b"[DRYRUN]Purged 1 rows of audit logs")]
            checks.sql = mock.Mock(side_effect=counts); checks.completed_jobs = []
        def wait(predicate, **kwargs): self.assertTrue(predicate())
        setup(); self.assertEqual(checks.submit_dry_run_job({"test": self, "wait": wait})[0], "owned-job")
        submitted = job.request.call_args_list[2].args[2]["job"]["parameters"]
        self.assertEqual(submitted["include_event_types"], "pull"); self.assertIs(submitted["dry_run"], True)
        self.assertEqual(job.request.call_count, 5); self.assertEqual(checks.completed_jobs, ["owned-job"])
        for counts in (("1", "0"), ("2", "1")):
            setup(counts=counts)
            with self.assertRaises(AssertionError): checks.submit_dry_run_job({"test": self, "wait": wait})
            self.assertEqual(checks.completed_jobs, [])
        setup(state=(200, b'{"job":{"status":"Error"}}'))
        with self.assertRaisesRegex(ValueError, "terminated unsuccessfully"): checks.submit_dry_run_job({"test": self, "wait": wait})
        self.assertEqual(job.request.call_count, 4); self.assertEqual(checks.completed_jobs, [])

    def test_candidate_artifacts_require_explicit_selection_and_exact_patch_provenance(self):
        import copy
        import test_harbor_pool_health_fixture as candidate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = b"synthetic candidate; never executed"; name = "harbor-core"
            (root / name).write_bytes(raw); binaries = {**candidate.BINARIES, name: hashlib.sha256(raw).hexdigest()}
            receipt = {"variant": candidate.VARIANT, "patch_sha256": candidate.PATCH_SHA,
                "base_generated_build_receipt_sha256": candidate.BASE_RECEIPT_SHA, "patched_files": candidate.PATCHED_FILES,
                "module_inputs_sha256": candidate.MODULE_INPUTS, "source_revision": candidate.REVISION,
                "source_archive_sha256": candidate.SOURCE_SHA, "go_archive_sha256": candidate.GO_SHA,
                "binaries_sha256": binaries, "exit": {"core": {"exit_code": 0}, "jobservice": {"exit_code": 0}},
                "original_sources_unchanged": True, "patched_sources_unchanged_during_build": True,
                "original_process_and_cgroup_removed": True, "runtime_verified": False, "publisher_signatures_verified": False}
            def write(value): (root / "build-inputs.json").write_text(json.dumps(value))
            write(receipt)
            with mock.patch.dict(candidate.BINARIES, binaries), mock.patch.dict(candidate.os.environ, {"HARBOR_POOL_HEALTH_TEST_RUNTIME": "0"}):
                with self.assertRaises(ValueError): candidate.stage_candidate(root, name, root / "refused")
                self.assertFalse((root / "refused").exists())
            changes = [("variant", "unreviewed"), ("patch_sha256", "0" * 64), ("patched_files", {}),
                ("module_inputs_sha256", {}), ("original_sources_unchanged", 1), ("patched_sources_unchanged_during_build", False),
                ("original_process_and_cgroup_removed", False), ("runtime_verified", True),
                ("exit", {"core": {"exit_code": False}, "jobservice": {"exit_code": 0}})]
            with mock.patch.dict(candidate.BINARIES, binaries), mock.patch.dict(candidate.os.environ, {"HARBOR_POOL_HEALTH_TEST_RUNTIME": "1"}):
                for key, value in changes:
                    changed = copy.deepcopy(receipt); changed[key] = value; write(changed)
                    with self.subTest(field=key), self.assertRaises(ValueError): candidate.stage_candidate(root, name, root / "refused")
                    self.assertFalse((root / "refused").exists())
                write(receipt); self.assertEqual(candidate.stage_candidate(root, name, root / "accepted").read_bytes(), raw)
                (root / name).write_bytes(raw + b"changed")
                with self.assertRaises(ValueError): candidate.stage_candidate(root, name, root / "refused")

    def test_candidate_report_uses_only_bounded_aggregate_evidence(self):
        import test_harbor_pool_health_fixture as candidate
        class Base:
            def exercise(self, context):
                return {"valkey_gateway_harbor_service_binaries_sha256": dict(candidate.BINARIES)}
        class Checks(candidate.PoolHealthChecks, Base): pass
        report = Checks().exercise({"test": self})
        self.assertEqual(report["valkey_gateway_harbor_local_pool_health_patch_sha256"], {"patch": candidate.PATCH_SHA})
        self.assertTrue(report["valkey_gateway_harbor_local_pool_health_patch_verified"])
        self.assertFalse(report["valkey_gateway_harbor_candidate_upstream_supported"])
        for key, value in report.items():
            self.assertTrue(key.startswith("valkey_gateway_"))
            self.assertTrue(type(value) is bool or isinstance(value, dict) and all(isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v) for v in value.values()))

    def test_original_service_staging_cannot_select_candidate_via_environment(self):
        checks = fixture.ServiceChecks()
        with mock.patch.dict(fixture.os.environ, {"HARBOR_POOL_HEALTH_TEST_RUNTIME": "1"}), mock.patch.object(fixture, "stage_binary", side_effect=ValueError("original pin enforced")) as original:
            with self.assertRaisesRegex(ValueError, "original pin enforced"):
                checks.stage_service_binary(Path("synthetic"), "harbor-core", Path("target"))
            original.assert_called_once_with(Path("synthetic"), "harbor-core", Path("target"))

    def test_migration_archive_changes_refuse_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"; archive.write_bytes(b"unreviewed")
            write = mock.Mock()
            with self.assertRaises(ValueError): fixture.stage_migrations(archive, {"write": write})
            write.assert_not_called()


if __name__ == "__main__": unittest.main()
