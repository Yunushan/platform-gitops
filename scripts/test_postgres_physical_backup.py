#!/usr/bin/env python3
"""Exercise physical backup identity, isolated archive handling, seals and offline verification."""

from contextlib import ExitStack
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import postgres_physical_backup as backup


def fixture():
    return {"schema_version": 1, "runtime": "rke2", "postgres_version": "18.6", "system_identifier": "1234567890123456789",
        "source": {"host": "db.example.com", "hostaddr": "192.0.2.40", "port": 5432, "user": "platform_pg_backup"},
        "runner": {"uid": 65534, "gid": 65534, "machine_id_sha256": "a" * 64, "declared_database_machine_ids_sha256": [c * 64 for c in "bcd"]},
        "client": {"bin_dir": "/opt/postgres/bin", "artifacts_sha256": {tool: "e" * 64 for tool in backup.TOOLS}, "library_dir": None, "libraries_sha256": {}},
        "tls": {"inputs_dir": "/etc/postgres-backup/inputs", "ca_sha256": "f" * 64, "client_certificate_sha256": "1" * 64},
        "limits": {"timeout_seconds": 30, "max_backup_bytes": 128 * 1024 ** 2, "max_files": 65536, "max_rate_kib": 65536}}


def make_tar(path, files, *, bad=None):
    with tarfile.open(path, "w") as archive:
        for name, payload in files.items():
            item = tarfile.TarInfo(name); item.size = len(payload); archive.addfile(item, io.BytesIO(payload))
        if bad: archive.addfile(bad)
    path.chmod(0o600)


class FakeCommands:
    def __init__(self):
        self.calls = []; self.change_source = False; self.extra_tablespace = False; self.dangerous = None; self.bad_verify = False; self.change_during_verify = False
        self.extra_ignored = False; self.identifications = 0; self.capture_fail = False

    def __call__(self, plan, tool, args, **kwargs):
        self.calls.append((tool, list(args), kwargs))
        if tool == "psql":
            statement = next(value for value in args if value.startswith("--command="))
            if statement == "--command=IDENTIFY_SYSTEM;":
                self.identifications += 1
                identity = "2222222222222222222" if self.change_source and self.identifications > 1 else plan["system_identifier"] or "1234567890123456789"
                return identity + "|1|0/4000000|\n"
            if statement == "--command=SHOW server_version_num;": return "180006\n"
            return "on\n"
        if tool == "pg_basebackup":
            if self.capture_fail: raise TimeoutError("Private fixture error text")
            destination = Path(next(value.split("=", 1)[1] for value in args if value.startswith("--pgdata=")))
            files = {"PG_VERSION": b"18\n", "global/pg_control": b"fixture control", "base/1/123": b"fixture data"}
            if self.extra_ignored: files["postgresql.auto.conf"] = b"fixture ignored configuration"
            make_tar(destination / "base.tar", files, bad=self.dangerous)
            make_tar(destination / "pg_wal.tar", {"000000010000000000000003": b"fixture WAL"})
            manifest = {"PostgreSQL-Backup-Manifest-Version": 2, "System-Identifier": int(plan["system_identifier"] or "1234567890123456789"),
                "Files": [{"Path": name, "Size": len(data), "Checksum-Algorithm": "SHA256", "Checksum": hashlib.sha256(data).hexdigest()} for name, data in files.items()],
                "WAL-Ranges": [{"Timeline": 1, "Start-LSN": "0/3000000", "End-LSN": "0/3000100"}], "Manifest-Checksum": "0" * 64}
            path = destination / "backup_manifest"; path.write_text(json.dumps(manifest)); path.chmod(0o600)
            if self.extra_tablespace:
                path = destination / "12345.tar"; path.write_bytes(b"unreviewed tablespace"); path.chmod(0o600)
            return ""
        if tool == "pg_verifybackup":
            if self.bad_verify: raise ValueError("Fixture invalid manifest")
            if self.change_during_verify:
                path = Path(args[-1]) / "postgresql.auto.conf"; path.write_bytes(b"changed after sealing"); path.chmod(0o600)
            return "backup successfully verified\n"
        raise AssertionError("Unexpected backup tool")


class PlanTests(unittest.TestCase):
    def test_default_all_five_runtimes_only_validate_local_plan(self):
        for runtime in backup.policy.RUNTIMES:
            plan = fixture(); plan["runtime"] = runtime
            with mock.patch.object(backup, "command", side_effect=AssertionError("executed")), mock.patch.object(backup, "machine_identity", side_effect=AssertionError("read")):
                result = backup.operate(plan)
            self.assertTrue(result["local_plan_verified"]); self.assertFalse(result["server_contacted"])
            for key in backup.UNVERIFIED: self.assertIs(result[key], False)

    def test_relaxed_tls_unpinned_tools_credentials_and_local_host_declarations_refuse(self):
        changes = [lambda p: p["tls"].update(insecure=True), lambda p: p["client"]["artifacts_sha256"].pop("pg_waldump"), lambda p: p["source"].update(password="dummy"),
            lambda p: p["source"].update(user="postgres"), lambda p: p["runner"].update(uid=0), lambda p: p["runner"].update(machine_id_sha256="b" * 64),
            lambda p: p["client"].update(library_dir="/unreviewed"), lambda p: p["source"].update(host="db.example.com password=bad"), lambda p: p.update(system_identifier="0")]
        for change in changes:
            with self.subTest(change=change):
                plan = fixture(); change(plan)
                with self.assertRaises(ValueError): backup.validate(plan)

    def test_capture_requires_identity_pin_before_any_runtime_action(self):
        plan = fixture(); plan["system_identifier"] = None
        with mock.patch.object(backup, "require_runner", side_effect=AssertionError("checked")), self.assertRaises(ValueError): backup.operate(plan, mode="capture", output=Path("private/test"))

    def test_environment_has_no_inherited_password_plugins_or_loader_injection(self):
        with mock.patch.dict(os.environ, {"PGPASSWORD": "do-not-copy", "PGSERVICE": "foreign", "LD_PRELOAD": "foreign", "SSLKEYLOGFILE": "foreign"}, clear=True):
            env = backup.environment(fixture(), source=True)
        self.assertEqual(env["PGSSLMODE"], "verify-full"); self.assertEqual(env["PGPASSFILE"], "/dev/null")
        self.assertEqual(env["PGSSLMINPROTOCOLVERSION"], "TLSv1.2")
        for key in ("PGPASSWORD", "PGSERVICE", "LD_PRELOAD", "SSLKEYLOGFILE", "HOME"): self.assertNotIn(key, env)

    def test_actual_runner_identity_is_enforced(self):
        if os.name != "posix": self.skipTest("Linux runtime identity check")
        plan = fixture(); plan["runner"]["uid"] = 1 if os.geteuid() != 1 else 2
        with self.assertRaises(ValueError): backup.require_runner(plan)


@unittest.skipUnless(os.name == "posix", "Physical backup filesystem invariants require POSIX")
class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); (self.root / "private").mkdir(mode=0o700)
        self.output = self.root / "private/capture"; self.plan = fixture(); self.fake = FakeCommands()
        self.context = ExitStack(); self.addCleanup(self.context.close)
        for name, value in (("ROOT", self.root), ("require_runner", mock.Mock()), ("directory", mock.Mock()), ("client_inputs", mock.Mock()),
                            ("tls_inputs", mock.Mock()), ("machine_identity", mock.Mock(return_value="a" * 64)), ("command", self.fake)):
            self.context.enter_context(mock.patch.object(backup, name, value))

    def capture(self): return backup.operate(self.plan, mode="capture", output=self.output)
    def verify(self):
        receipt = hashlib.sha256((self.output / "postgres-backup-proof.json").read_bytes()).hexdigest()
        return backup.operate(self.plan, mode="verify", output=self.output, receipt_sha256=receipt)

    def test_capture_and_offline_verification_keep_recovery_claims_false(self):
        result = self.capture(); self.assertTrue(result["backup_integrity_verified"])
        for key in backup.UNVERIFIED: self.assertIs(result[key], False)
        self.assertTrue(self.verify()["backup_integrity_verified"])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "pg_basebackup"]), 1)
        self.assertEqual([c for c in self.fake.calls if c[0] == "pg_basebackup"][0][2]["source"], True)
        self.assertIn("--format=tar", [c for c in self.fake.calls if c[0] == "pg_basebackup"][0][1])
        self.assertTrue(all(not any("--no-parse-wal" in arg for arg in c[1]) for c in self.fake.calls if c[0] == "pg_verifybackup"))
        self.assertEqual((self.output / "data/global/pg_control").stat().st_mode & 0o077, 0)

    def test_source_probe_writes_private_identity_without_capture(self):
        self.plan["system_identifier"] = None
        self.assertTrue(backup.operate(self.plan, mode="check-source", output=self.output)["server_contacted"])
        self.assertFalse(any(c[0] == "pg_basebackup" for c in self.fake.calls))
        proof = json.loads((self.output / "postgres-backup-proof.json").read_text())
        self.assertEqual(proof["phase"], "source-check-completed"); self.assertIn("system_identifier", proof["source_before"])

    def test_source_history_change_or_client_failure_preserves_failed_capture(self):
        self.fake.change_source = True
        with self.assertRaises(ValueError): self.capture()
        proof = json.loads((self.output / "postgres-backup-proof.json").read_text())
        self.assertEqual(proof["phase"], "failed"); self.assertFalse(proof["backup_integrity_verified"])
        self.assertTrue((self.output / "transport/base.tar").is_file())
        with self.assertRaises(ValueError): self.verify()

    def test_external_tablespace_transport_is_rejected_before_extraction(self):
        self.fake.extra_tablespace = True
        with self.assertRaises(ValueError): self.capture()
        self.assertFalse((self.output / "data").exists())

    def test_archive_traversal_symlinks_hardlinks_and_devices_are_rejected(self):
        for name, kind in (("../../escape", tarfile.REGTYPE), ("/outside", tarfile.REGTYPE), ("linked", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE), ("device", tarfile.CHRTYPE)):
            with self.subTest(name=name):
                self.output = self.root / ("private/capture-" + str(len(list((self.root / "private").iterdir()))))
                bad = tarfile.TarInfo(name); bad.type = kind; bad.linkname = "/outside"
                self.fake.dangerous = bad
                with self.assertRaises(ValueError): self.capture()
        self.assertFalse((self.root / "escape").exists())

    def test_whole_tree_seal_catches_files_pg_verifybackup_ignores(self):
        self.fake.extra_ignored = True; self.capture()
        (self.output / "data/postgresql.auto.conf").write_bytes(b"modified configuration")
        with self.assertRaises(ValueError): self.verify()

    def test_changes_during_verification_cannot_be_sealed_as_success(self):
        self.fake.extra_ignored = True; self.fake.change_during_verify = True
        with self.assertRaises(ValueError): self.capture()
        proof = json.loads((self.output / "postgres-backup-proof.json").read_text())
        self.assertFalse(proof["captured"]); self.assertFalse(proof["backup_integrity_verified"])

    def test_offline_verification_needs_no_source_certificate_or_host_contact(self):
        self.capture(); self.fake.calls.clear()
        with mock.patch.object(backup, "tls_inputs", side_effect=AssertionError("read private credentials")), mock.patch.object(backup, "machine_identity", side_effect=AssertionError("read capture host")):
            self.assertTrue(self.verify()["backup_integrity_verified"])
        self.assertEqual([c[0] for c in self.fake.calls], ["pg_verifybackup"])

    def test_existing_output_and_partial_captures_are_not_reused(self):
        self.capture(); original = (self.output / "postgres-backup-proof.json").read_bytes()
        with self.assertRaises(FileExistsError): self.capture()
        self.assertEqual((self.output / "postgres-backup-proof.json").read_bytes(), original)

    def test_manifest_system_identity_or_checksum_relaxation_is_rejected(self):
        self.capture(); path = self.output / "data/backup_manifest"; original = json.loads(path.read_text())
        identity = {"system_identifier": self.plan["system_identifier"], "timeline": 1}
        for change in (lambda o: o.update({"System-Identifier": 2}), lambda o: o["Files"][0].update({"Checksum-Algorithm": "CRC32C"}), lambda o: o["WAL-Ranges"][0].update({"Timeline": 2})):
            altered = copy.deepcopy(original); change(altered); path.write_text(json.dumps(altered))
            with self.assertRaises(ValueError): backup.manifest(self.output / "data", identity, self.plan)

    def test_changed_receipt_or_tooling_refuses_offline_verification(self):
        self.capture(); path = self.output / "postgres-backup-proof.json"; original = path.read_bytes(); pin = hashlib.sha256(original).hexdigest()
        with mock.patch.object(backup, "toolset", return_value="9" * 64), self.assertRaises(ValueError): self.verify()
        path.write_bytes(original + b"\n")
        with self.assertRaises(ValueError): backup.operate(self.plan, mode="verify", output=self.output, receipt_sha256=pin)

    def test_wrong_capture_host_refuses_before_tools_and_credentials(self):
        self.plan["runner"]["machine_id_sha256"] = "e" * 64
        with self.assertRaises(ValueError): self.capture()
        backup.client_inputs.assert_not_called(); backup.tls_inputs.assert_not_called()
        self.assertFalse(self.fake.calls)

    def test_archive_size_and_inventory_bounds_stop_extraction(self):
        root = self.root / "unpack"; root.mkdir(mode=0o700)
        path = self.root / "oversized.tar"; bad = tarfile.TarInfo("oversized"); bad.size = self.plan["limits"]["max_backup_bytes"] + 1
        # A header-only oversized member exercises rejection before payload
        # reads without allocating a huge fixture file.
        path.write_bytes(bad.tobuf() + b"\0" * 1024); path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "archive size"): backup.unpack(path, root, self.plan["limits"], {"entries": 0, "bytes": 0})
        self.assertFalse((root / "oversized").exists())
        make_tar(path, {"extra": b"a"})
        with self.assertRaises(ValueError): backup.unpack(path, root, self.plan["limits"], {"entries": self.plan["limits"]["max_files"], "bytes": 0})

    def test_interrupted_completion_cannot_publish_a_successful_capture(self):
        writer = backup.atomic_write_text
        def fail_completion(path, value):
            if path.name.endswith("proof.json") and json.loads(value)["phase"] == "completed": raise OSError("Fixture completion write failed")
            return writer(path, value)
        with mock.patch.object(backup, "atomic_write_text", side_effect=fail_completion), self.assertRaises(OSError): self.capture()
        receipt = json.loads((self.output / "postgres-backup-proof.json").read_text())
        self.assertEqual(receipt["phase"], "failed"); self.assertFalse(receipt["captured"]); self.assertFalse(receipt["backup_integrity_verified"])
        self.assertTrue((self.output / "data/global/pg_control").exists())
        with self.assertRaises(ValueError): self.verify()


class LiveTests(unittest.TestCase):
    def test_optional_real_tls_capture_and_corruption_detection(self):
        if os.environ.get("POSTGRES_BACKUP_TEST_LIVE") != "1": self.skipTest("Opt in to disposable real PostgreSQL backup capture")
        from test_postgres_runtime_fixture import exercise
        from test_postgres_backup_fixture import capture
        exercise(self, hook=lambda state: capture(self, state))


if __name__ == "__main__": unittest.main()
