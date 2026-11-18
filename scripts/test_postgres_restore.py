#!/usr/bin/env python3
"""Test fresh-copy restore drills, witness scope, original-backup preservation and process cleanup."""

from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import postgres_physical_backup as backup
import restore_postgres_backup as restore
import test_postgres_physical_backup as backups


def fixture(original, receipt="a" * 64):
    return {"schema_version": 1, "backup_plan_sha256": backup.fingerprint(original), "backup_receipt_sha256": receipt,
        "runner_machine_id_sha256": "a" * 64, "server": {"bin_dir": "/opt/postgres/bin", "artifacts_sha256": {tool: "d" * 64 for tool in restore.TOOLS},
            "library_dir": None, "libraries_sha256": {}, "asset_trees_sha256": {"/opt/postgres/share": "e" * 64}},
        "runtime_parent": "/run/postgres-drill", "database_user": "postgres", "port": 16543,
        "limits": {"startup_seconds": 5, "query_seconds": 5, "stop_seconds": 5, "max_memory_bytes": 1024 ** 3, "max_log_bytes": 1024 ** 2},
        "witnesses": [{"database": "postgres", "rows": 2, "sha256": "f" * 64}]}


class InputTests(unittest.TestCase):
    def test_default_validates_pinned_receipt_without_runtime_or_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); original = backups.fixture()
            receipt = {"schema_version": 1, "phase": "completed", "command": "capture", "plan_sha256": backup.fingerprint(original), "toolset_sha256": backup.toolset(),
                "captured": True, "backup_integrity_verified": True, "source_after": {"system_identifier": original["system_identifier"]}, **backup.UNVERIFIED}
            raw = json.dumps(receipt).encode(); (root / "postgres-backup-proof.json").write_bytes(raw)
            plan = fixture(original, hashlib.sha256(raw).hexdigest())
            with mock.patch.object(backup, "operate", side_effect=AssertionError("backup data read")), mock.patch.object(restore, "launch", side_effect=AssertionError("spawned")):
                result = restore.operate(original, plan, root)
            self.assertTrue(result["local_inputs_verified"]); self.assertFalse(result["database_restore_drill_verified"])
            self.assertEqual(len(list(root.iterdir())), 1)
            (root / "postgres-backup-proof.json").write_bytes(raw + b"\n")
            with self.assertRaises(ValueError): restore.operate(original, plan, root)

    def test_scope_relaxations_credentials_and_arbitrary_queries_refuse(self):
        original = backups.fixture()
        changes = [lambda p: p.update(database_user="postgres;bad"), lambda p: p.update(runtime_parent="/tmp/unsafe"),
            lambda p: p.update(runner_machine_id_sha256="b" * 64), lambda p: p["server"]["artifacts_sha256"].pop("postgres"),
            lambda p: p["server"].update(asset_trees_sha256={}), lambda p: p["witnesses"][0].update(sql="SELECT 1"),
            lambda p: p["witnesses"][0].update(rows=0), lambda p: p["limits"].update(startup_seconds=0), lambda p: p.update(password="dummy")]
        for change in changes:
            with self.subTest(change=change):
                plan = fixture(original); change(plan)
                with self.assertRaises(ValueError): restore.validate(plan, original)

    def test_sql_identity_and_witness_checks_reject_wrong_data_roles_and_unsafe_relations(self):
        original = backups.fixture(); plan = fixture(original); output = Path("/approved/private/drill")
        identity = {"system_identifier": original["system_identifier"], "version": 180006, "database": "postgres", "user": "postgres", "superuser": True,
            "in_recovery": False, "server_address": None, "data_directory": str(output / "data"), "listen_addresses": "", "read_only": "on", "encoding": "UTF8", "checksums": "on", "replay_lsn": "0/4000000"}
        metadata = {"kind": "r", "am": "heap", "rules": False, "rls": False, "columns": [["id", 20, "", True], ["payload", 25, "", True]]}
        witness = {"rows": 2, "bytes": 20, "sha256": "f" * 64}
        proof = {"required_wal": {"End-LSN": "0/3000100"}}
        with mock.patch.object(restore, "sql", side_effect=[identity, metadata, witness]):
            self.assertEqual(restore.observe(original, plan, Path("/run/socket"), output, proof)[0]["rows"], 2)
        mutations = [(0, "in_recovery", True), (0, "system_identifier", "2"), (0, "server_address", "127.0.0.1"), (0, "replay_lsn", "0/1"),
                     (1, "kind", "v"), (1, "am", "custom"), (1, "rls", True), (2, "sha256", "0" * 64), (2, "bytes", 1048577)]
        for index, key, value in mutations:
            with self.subTest(key=key):
                rows = copy.deepcopy([identity, metadata, witness]); rows[index][key] = value
                with mock.patch.object(restore, "sql", side_effect=rows), self.assertRaises(ValueError): restore.observe(original, plan, Path("/run/socket"), output, proof)


class FakeProcess:
    pid = 99999999
    returncode = None
    def poll(self): return self.returncode


@unittest.skipUnless(os.name == "posix", "POSIX filesystem/process orchestration fixture")
class DrillTests(unittest.TestCase):
    def setUp(self):
        self.capture = backups.CaptureTests(); self.capture.setUp(); self.addCleanup(self.capture.doCleanups)
        self.capture.capture(); self.original = self.capture.plan; self.root = self.capture.root
        self.source = self.capture.output; self.output = self.root / "private/drill"
        raw = (self.source / "postgres-backup-proof.json").read_bytes(); self.plan = fixture(self.original, hashlib.sha256(raw).hexdigest())
        self.process = FakeProcess(); self.process.returncode = None
        self.identity = {"pid": self.process.pid, "process_group": self.process.pid, "session": self.process.pid, "start_ticks": 1, "boot_id": "fixture"}
        self.context = ExitStack(); self.addCleanup(self.context.close)
        self.context.enter_context(mock.patch.object(restore, "ROOT", self.root))
        self.context.enter_context(mock.patch.object(restore, "runtime_inputs"))
        self.context.enter_context(mock.patch.object(restore, "process_identity", return_value=self.identity))
        self.context.enter_context(mock.patch.object(restore, "check_shutdown"))
        self.context.enter_context(mock.patch.object(restore, "observe", return_value=[{"rows": 2}]))
        self.stopped = self.context.enter_context(mock.patch.object(restore, "stop", side_effect=self.stop))
        self.launched = self.context.enter_context(mock.patch.object(restore, "launch", side_effect=self.launch))
        def allocate(plan, operation):
            path = self.root / operation; path.mkdir(mode=0o700); return path
        self.context.enter_context(mock.patch.object(restore, "allocate_runtime", side_effect=allocate))

    def stop(self, process, identity, seconds, operation=None):
        self.assertIs(process, self.process); self.assertEqual(identity, self.identity); process.returncode = 0

    def launch(self, original, plan, arguments, log):
        log.write_bytes(b""); log.chmod(0o600)
        socket = next(line.split("=", 1)[1].strip().strip("'") for line in (self.output / "drill.conf").read_text().splitlines() if line.startswith("unix_socket_directories"))
        (self.output / "data/postmaster.pid").write_text("\n".join([str(self.process.pid), str(self.output / "data"), "1790000000", str(plan["port"]), socket, "", "", "ready"]) + "\n")
        return self.process

    def run_drill(self): return restore.operate(self.original, self.plan, self.source, run=True, output=self.output)

    def test_fresh_copy_preserves_original_and_keeps_platform_recovery_gates_false(self):
        original = backup.inventory(self.source / "data", self.original["limits"])
        result = self.run_drill()
        self.assertTrue(result["database_restore_drill_verified"]); self.assertTrue(result["server_stopped"])
        for key in restore.UNVERIFIED: self.assertFalse(result[key])
        self.assertEqual(backup.inventory(self.source / "data", self.original["limits"]), original)
        self.stopped.assert_called_once(); self.launched.assert_called_once()
        control = (self.output / "drill.conf").read_text()
        for setting in ("listen_addresses = ''", "shared_preload_libraries = ''", "primary_conninfo = ''", "archive_command = ''", "default_transaction_read_only = 'on'", "jit = 'off'"):
            self.assertIn(setting, control)

    def test_existing_output_is_never_adopted_or_restarted(self):
        self.run_drill(); raw = (self.output / "postgres-restore-proof.json").read_bytes()
        with self.assertRaises(FileExistsError): self.run_drill()
        self.assertEqual(self.launched.call_count, 1); self.assertEqual((self.output / "postgres-restore-proof.json").read_bytes(), raw)

    def test_source_configuration_hooks_are_quarantined_only_in_the_copy(self):
        # A source file already covered by its original seal is copied first;
        # the fresh server uses only the newly generated trusted configuration.
        self.capture.output = self.root / "private/configured-capture"; self.capture.fake.extra_ignored = True; self.capture.capture(); self.source = self.capture.output
        self.plan["backup_receipt_sha256"] = hashlib.sha256((self.source / "postgres-backup-proof.json").read_bytes()).hexdigest()
        original = (self.source / "data/postgresql.auto.conf").read_bytes()
        self.run_drill()
        self.assertEqual((self.output / "quarantine/postgresql.auto.conf").read_bytes(), original)
        self.assertEqual((self.source / "data/postgresql.auto.conf").read_bytes(), original)
        self.assertNotEqual((self.output / "data/postgresql.auto.conf").read_bytes(), original)

    def test_corrupt_original_refuses_before_start(self):
        (self.source / "data/global/pg_control").write_bytes(b"changed")
        with self.assertRaises(ValueError): self.run_drill()
        self.launched.assert_not_called(); self.assertFalse(self.output.exists())

    def test_witness_failure_stops_the_same_process_and_records_no_success(self):
        with mock.patch.object(restore, "observe", side_effect=ValueError("fixture witness mismatch")), self.assertRaises(ValueError): self.run_drill()
        self.stopped.assert_called_once()
        proof = json.loads((self.output / "postgres-restore-proof.json").read_text())
        self.assertEqual(proof["phase"], "failed"); self.assertTrue(proof["server_stopped"]); self.assertFalse(proof["database_restore_drill_verified"])

    def test_cleanup_uncertainty_cannot_record_a_verified_drill(self):
        with mock.patch.object(restore, "stop", side_effect=TimeoutError("fixture unobserved shutdown")), self.assertRaises(TimeoutError): self.run_drill()
        proof = json.loads((self.output / "postgres-restore-proof.json").read_text())
        self.assertFalse(proof["database_restore_drill_verified"]); self.assertFalse(proof["server_stopped"])

    def test_runtime_asset_inventory_detects_changes(self):
        tree = self.root / "assets"; tree.mkdir(mode=0o755); path = tree / "timezone"; path.write_bytes(b"fixture timezone"); path.chmod(0o644)
        before = restore.asset_tree(tree); path.write_bytes(b"changed timezone")
        self.assertNotEqual(before, restore.asset_tree(tree))

    def test_final_implementation_change_refuses_success_after_shutdown(self):
        initial = restore.implementation(); changed = {**initial, "child_sha256": "0" * 64}
        with mock.patch.object(restore, "implementation", side_effect=[initial, changed]), self.assertRaises(ValueError): self.run_drill()
        proof = json.loads((self.output / "postgres-restore-proof.json").read_text())
        self.assertEqual(proof["phase"], "failed"); self.assertTrue(proof["server_stopped"]); self.assertFalse(proof["database_restore_drill_verified"])

    def test_failed_completion_publication_records_failure_and_preserves_original(self):
        saved = restore.atomic_write_text
        def publish(path, value):
            if path.name == "postgres-restore-proof.json" and json.loads(value).get("phase") == "completed": raise OSError("fixture evidence write failure")
            return saved(path, value)
        original = backup.inventory(self.source / "data", self.original["limits"])
        with mock.patch.object(restore, "atomic_write_text", side_effect=publish), self.assertRaises(OSError): self.run_drill()
        proof = json.loads((self.output / "postgres-restore-proof.json").read_text())
        self.assertEqual(proof["phase"], "failed"); self.assertTrue(proof["server_stopped"]); self.assertFalse(proof["database_restore_drill_verified"])
        self.assertEqual(backup.inventory(self.source / "data", self.original["limits"]), original)


class LiveTests(unittest.TestCase):
    def test_optional_real_captured_backup_restore_and_wrong_witness(self):
        if os.environ.get("POSTGRES_RESTORE_TEST_LIVE") != "1": self.skipTest("Opt in to a real disposable backup/restore drill")
        from test_postgres_runtime_fixture import exercise
        from test_postgres_backup_fixture import capture
        from test_postgres_restore_fixture import exercise as restored
        exercise(self, hook=lambda state: capture(self, state, restore=lambda context: restored(self, context)))

    def test_optional_real_parent_death_guard(self):
        if os.environ.get("POSTGRES_RESTORE_TEST_LIVE") != "1": self.skipTest("Opt in to the Linux kernel parent-death guard fixture")
        from test_postgres_restore_fixture import parent_death
        parent_death(self)


if __name__ == "__main__": unittest.main()
