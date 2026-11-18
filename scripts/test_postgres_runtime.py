#!/usr/bin/env python3
"""Test fixed SQL, original-instance fencing, runtime quorum checks and honest evidence."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_postgres_ha as deploy
import verify_postgres_runtime as verify
import test_postgres_installation as installation

uid = installation.uid


def sample(primary=True):
    values = {key: "on" for key in ("fsync", "full_page_writes", "data_checksums", "ssl")}
    values.update(wal_level="logical", password_encryption="scram-sha-256", ssl_min_protocol_version="TLSv1.2",
                  synchronous_commit="remote_apply", synchronous_standby_names='ANY 1 ("platform-postgres-2","platform-postgres-3")' if primary else "")
    return {"schema_version": 1, "transaction_read_only": "on", "user": "postgres", "database": "postgres", "server_version_num": 180004,
        "postmaster_started_at": "1790000000.123456", "in_recovery": not primary, "system_identifier": "1234567890123456789", "timeline_id": 1,
        "settings": {key: {"value": value, "pending_restart": False} for key, value in values.items()},
        "senders": [{"application_name": "platform-postgres-" + str(i), "client_addr": "198.51.100." + str(i), "state": "streaming", "sync_state": "quorum", "sync_priority": 0,
                     "flush_lsn": "0/3000000", "replay_lsn": "0/2FFFFFF", "tls": True, "tls_version": "TLSv1.3", "client_certificate_present": True} for i in (2, 3)] if primary else [],
        "receivers": [] if primary else [{"status": "streaming", "sender_host": "platform-postgres-rw", "sender_port": 5432, "received_tli": 1, "flushed_lsn": "0/3000000", "last_message_age_seconds": 0.1}]}


class SQLClient(installation.FakeClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.samples = {"platform-postgres-" + str(i): sample(i == 1) for i in (1, 2, 3)}
        self.execs = 0; self.on_query = None; self.final_primary = None

    def make_ready(self):
        super().make_ready()
        self.cluster["status"]["currentPrimaryTimestamp"] = "2026-11-07T00:00:00Z"
        for i, pod in enumerate(self.collections["pods", self.namespace], 1):
            pod["spec"]["containers"][0]["name"] = "postgres"
            status = pod["status"]["containerStatuses"][0]
            status.update(name="postgres", restartCount=0, containerID="containerd://" + str(i) * 64, state={"running": {"startedAt": "2026-11-07T00:00:00Z"}})
            self.objects["pod/" + pod["metadata"]["name"]] = pod

    def call(self, args, payload=None, seconds=30):
        if args[0] != "exec": return super().call(args, payload, seconds)
        self.calls.append(tuple(args)); self.execs += 1
        name = next(value.removeprefix("pod/") for value in args if value.startswith("pod/"))
        result = copy.deepcopy(self.final_primary if self.execs == 4 and self.final_primary is not None else self.samples[name])
        if self.on_query: self.on_query(self, name, result)
        return json.dumps(result)


class RuntimeTests(unittest.TestCase):
    def prepare(self, root, runtime="rke2"):
        helper = installation.InstallationTests()
        bundle, plan, original = helper.prepare(root, runtime)
        client = SQLClient(plan, original.selected)
        helper.run_install(root, bundle, plan, client)
        receipt = root / "private/operation/postgres-install-proof.json"
        return bundle, plan, client, receipt, hashlib.sha256(receipt.read_bytes()).hexdigest()

    def observe(self, root, prepared, **kwargs):
        bundle, plan, client, receipt, digest = prepared
        with mock.patch.object(verify, "ROOT", root):
            return verify.operate(bundle, plan, receipt, digest, observe=True, output=root / "private/runtime", client=client, **kwargs)

    def test_default_never_constructs_client_or_reads_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, _, receipt, digest = self.prepare(root)
            with mock.patch.object(verify, "Client", side_effect=AssertionError("constructed")), mock.patch.dict(os.environ, {}, clear=True):
                result = verify.operate(bundle, plan, receipt, digest)
            self.assertTrue(result["local_inputs_verified"]); self.assertFalse(result["runtime_configuration_observed"])
            self.assertFalse((root / "private/runtime").exists())

    def test_three_runtimes_observe_fixed_sql_but_never_claim_write_durability_or_ha(self):
        for runtime in ("rke2", "k3s", "k8s"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root, runtime); client = prepared[2]; client.calls.clear()
                result = self.observe(root, prepared)
                self.assertTrue(result["runtime_configuration_observed"]); self.assertTrue(result["streaming_quorum_observed"])
                self.assertTrue(result["read_only"])
                for key in verify.UNVERIFIED: self.assertIs(result[key], False)
                commands = [c for c in client.calls if c[0] == "exec"]
                self.assertEqual(len(commands), 4); self.assertTrue(all(c[-1] == "--command=" + verify.SQL for c in commands))
                self.assertTrue(all("--container" in c and "postgres" in c and "/usr/bin/env" in c and "-i" in c and "-X" in c for c in commands))
                self.assertFalse(any(c[0] in {"create", "apply", "patch", "delete", "replace"} for c in client.calls))
                proof = json.loads((root / "private/runtime/postgres-runtime-proof.json").read_text())
                self.assertEqual(proof["phase"], "completed"); self.assertEqual(proof["instances_observed"], 3)
                self.assertNotIn("samples", proof); self.assertNotIn("settings", proof)

    def test_source_receipt_must_be_pinned_completed_and_exact_scope_before_client_access(self):
        changes = [lambda s: s.update(phase="failed"), lambda s: s.update(action="check-server"), lambda s: s.update(cluster_uid=None),
                   lambda s: s.update(deployment_plan_sha256="a" * 64), lambda s: s.update(ha_verified=True), lambda s: s.update(operation_id="bad")]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client, receipt, digest = self.prepare(root)
                state = json.loads(receipt.read_text()); change(state); receipt.write_text(json.dumps(state))
                with mock.patch.object(verify, "Client", side_effect=AssertionError("constructed")), self.assertRaises(ValueError):
                    verify.operate(bundle, plan, receipt, hashlib.sha256(receipt.read_bytes()).hexdigest())
                with self.assertRaises(ValueError): verify.operate(bundle, plan, receipt, digest)

    def test_runtime_policy_version_identity_and_restart_fail_closed(self):
        changes = [lambda s: s.update(server_version_num=170004), lambda s: s.update(transaction_read_only="off"), lambda s: s.update(user="other"),
                   lambda s: s.update(timeline_id=True), lambda s: s.update(system_identifier="0"), lambda s: s.update(server_version_num=True),
                   lambda s: s["settings"]["fsync"].update(value="off"), lambda s: s["settings"]["synchronous_commit"].update(value="local"),
                   lambda s: s["settings"]["ssl"].update(value="off"), lambda s: s["settings"]["data_checksums"].update(value="off"),
                   lambda s: s["settings"]["wal_level"].update(pending_restart=True), lambda s: s.update(conninfo="private-value")]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); change(prepared[2].samples["platform-postgres-2"])
                with self.assertRaises(ValueError): self.observe(root, prepared)
                proof = json.loads((root / "private/runtime/postgres-runtime-proof.json").read_text())
                self.assertEqual(proof["phase"], "failed"); self.assertFalse(proof["runtime_configuration_observed"])
                self.assertNotIn("private-value", json.dumps(proof))

    def test_quorum_cannot_reduce_or_wildcard_or_use_another_replica(self):
        for value in ('ANY 0 ("platform-postgres-2","platform-postgres-3")', 'ANY 1 (*)', 'FIRST 1 ("platform-postgres-2","platform-postgres-3")',
                      'ANY 1 ("platform-postgres-2","other")', 'ANY 1 ("platform-postgres-2","platform-postgres-2")', 'ANY 2 ("platform-postgres-2","platform-postgres-3")'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root)
                prepared[2].samples["platform-postgres-1"]["settings"]["synchronous_standby_names"]["value"] = value
                with self.assertRaises(ValueError): self.observe(root, prepared)

    def test_two_required_standbys_and_both_supported_commit_modes(self):
        from test_postgres_ha import fixture
        for commit in ("on", "remote_apply"):
            selected = fixture(); selected["replication"] = {"standbys": 2, "synchronous_commit": commit}
            samples = {"platform-postgres-" + str(i): sample(i == 1) for i in (1, 2, 3)}
            for row in samples.values(): row["settings"]["synchronous_commit"]["value"] = commit
            samples["platform-postgres-1"]["settings"]["synchronous_standby_names"]["value"] = 'ANY 2 ("platform-postgres-2","platform-postgres-3")'
            samples["primary_recheck"] = copy.deepcopy(samples["platform-postgres-1"])
            view = {"primary": "platform-postgres-1", "pods": {"platform-postgres-" + str(i): {"addresses": ["198.51.100." + str(i)]} for i in (1, 2, 3)}}
            self.assertEqual(verify.compare(samples, selected, view)["synchronous_standbys_required"], 2)

    def test_replication_connections_require_tls_peer_ips_certificates_and_quorum_state(self):
        changes = [lambda s: s["senders"][0].update(client_addr="192.0.2.250"), lambda s: s["senders"][0].update(sync_state="async"),
                   lambda s: s["senders"][0].update(tls=False), lambda s: s["senders"][0].update(client_certificate_present=False),
                   lambda s: s["senders"][0].update(tls_version="TLSv1"), lambda s: s["senders"][0].update(flush_lsn="0/0"),
                   lambda s: s["senders"][0].update(replay_lsn="0/4000000"), lambda s: s.update(in_recovery=True)]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); change(prepared[2].samples["platform-postgres-1"])
                with self.assertRaises(ValueError): self.observe(root, prepared)

    def test_receiver_requires_same_system_timeline_current_primary_freshness_and_progress(self):
        changes = [lambda s: s.update(system_identifier="2222222222222222222"), lambda s: s.update(timeline_id=2), lambda s: s.update(in_recovery=False),
                   lambda s: s["receivers"][0].update(sender_host="other"), lambda s: s["receivers"][0].update(status="stopped"),
                   lambda s: s["receivers"][0].update(received_tli=2), lambda s: s["receivers"][0].update(last_message_age_seconds=31),
                   lambda s: s["receivers"][0].update(last_message_age_seconds=-1), lambda s: s["receivers"][0].update(last_message_age_seconds=None),
                   lambda s: s["receivers"][0].update(last_message_age_seconds=float("inf")), lambda s: s["receivers"][0].update(flushed_lsn="0/1000000")]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); change(prepared[2].samples["platform-postgres-3"])
                with self.assertRaises(ValueError): self.observe(root, prepared)

    def test_same_primary_process_and_monotonic_sender_progress_are_required_on_second_sample(self):
        changes = [lambda s: s.update(postmaster_started_at="1790000001.123456"), lambda s: s.update(timeline_id=2),
                   lambda s: s["senders"][0].update(flush_lsn="0/1000000", replay_lsn="0/1000000")]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); prepared[2].final_primary = sample(); change(prepared[2].final_primary)
                with self.assertRaises(ValueError): self.observe(root, prepared)

    def test_replaced_pod_container_restart_or_primary_change_invalidates_observation(self):
        def replacement(client, name, result): client.objects["pod/" + name]["metadata"]["uid"] = uid(999)
        def restart(client, name, result): client.objects["pod/" + name]["status"]["containerStatuses"][0]["restartCount"] += 1
        def primary_change(client, name, result): client.cluster["status"]["currentPrimaryTimestamp"] = "2026-11-07T01:00:00Z"
        def spec_change(client, name, result): client.cluster["spec"]["instances"] = 4
        def storage_change(client, name, result): client.objects["persistentvolume/volume-1"]["metadata"]["uid"] = uid(999)
        for change in (replacement, restart, primary_change, spec_change, storage_change):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); prepared = self.prepare(root); prepared[2].on_query = change
                with self.assertRaises(ValueError): self.observe(root, prepared)

    def test_replaced_original_cluster_is_refused_before_any_sql(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); prepared[2].cluster["metadata"]["uid"] = uid(999)
            with self.assertRaises(ValueError): self.observe(root, prepared)
            self.assertEqual(prepared[2].execs, 0)

    def test_existing_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root); self.observe(root, prepared)
            receipt = root / "private/runtime/postgres-runtime-proof.json"; original = receipt.read_bytes()
            with self.assertRaises(FileExistsError): self.observe(root, prepared)
            self.assertEqual(receipt.read_bytes(), original)

    def test_failed_completion_write_cannot_leave_runtime_flags_true(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = self.prepare(root)
            writer = verify.atomic_write_text
            def fail_completion(path, value):
                if json.loads(value)["phase"] == "completed": raise OSError("Fixture completion write failed")
                return writer(path, value)
            with mock.patch.object(verify, "atomic_write_text", side_effect=fail_completion), self.assertRaises(OSError): self.observe(root, prepared)
            proof = json.loads((root / "private/runtime/postgres-runtime-proof.json").read_text())
            self.assertEqual(proof["phase"], "failed")
            self.assertFalse(proof["runtime_configuration_observed"]); self.assertFalse(proof["streaming_quorum_observed"])

    def test_fixed_query_uses_rollback_read_only_no_connection_strings_or_user_tables(self):
        self.assertTrue(verify.SQL.startswith("BEGIN READ ONLY;")); self.assertTrue(verify.SQL.endswith("ROLLBACK;\n"))
        for value in ("conninfo", "pg_authid", "rolpassword", "pg_promote", "pg_switch_wal", "COPY ", "INSERT ", "CREATE ", "DELETE ", "UPDATE "):
            self.assertNotIn(value, verify.SQL)
        self.assertIn("default_transaction_read_only=on", " ".join(verify.psql_arguments(18)))
        self.assertIn("PGPASSFILE=/dev/null", verify.psql_arguments(18))

    def test_optional_real_postgres_streaming_queries(self):
        if not os.environ.get("POSTGRES_RUNTIME_TEST_BIN"): self.skipTest("Opt in to three disposable local PostgreSQL processes")
        from test_postgres_runtime_fixture import exercise
        exercise(self)


if __name__ == "__main__": unittest.main()
