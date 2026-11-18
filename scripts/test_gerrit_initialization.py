#!/usr/bin/env python3
"""Test destructive-boundary guards using dummy files and a simulated Gerrit init."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from bounded_subprocess import run_bounded
import initialize_gerrit_sites as initializer
from test_gerrit_sites import fixture, prepare_site, digest


class GerritInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.inputs = self.base / "inputs"
        self.inputs.mkdir()
        self.shared = self.base / "shared"
        self.shared.mkdir()
        self.directories = [self.base / "target-a", self.base / "target-b"]
        self.plan = fixture()
        for site in self.plan["sites"]:
            source = self.inputs / site["name"]
            prepare_site(source)
            for name in initializer.PRIVATE_INPUTS:
                (source / name).write_bytes(b"non-secret-test-fixture")
            (source / "etc/secure.config").write_text("[auth]\n registerEmailPrivateKey = <BOOTSTRAP_TEST_KEY>\n", encoding="utf-8")
            config = source / "etc/gerrit.config"
            config.write_text(
                f'[gerrit]\n basePath = {json.dumps(str(self.shared / "git"))}\n instanceId = {site["name"]}\n'
                ' serverId = 01234567-89ab-cdef-0123-456789abcdef\n'
                '[auth]\n type = LDAP\n', encoding="utf-8")
            site["config_hashes"]["etc/gerrit.config"] = digest(config.read_bytes())
        patcher = mock.patch.object(initializer, "targets", return_value=(self.directories, self.shared))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_init(self, arguments, **kwargs):
        if arguments[0] == "git":
            self.assertEqual(arguments[-1], "gerrit.serverId")
            return run_bounded(arguments, **kwargs)
        self.assertEqual(arguments[0], "test-java")
        self.assertIn("--batch", arguments)
        self.assertIn("--no-auto-start", arguments)
        self.assertIn("--skip-all-downloads", arguments)
        self.assertEqual(kwargs["timeout"], 600)
        for name in ("All-Projects.git", "All-Users.git"):
            (self.shared / "git" / name).mkdir(parents=True)
        index = self.directories[0] / "index"
        index.mkdir()
        (index / "initial-state").write_text("fixture-index", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def test_preflight_is_read_only(self):
        before = {str(p.relative_to(self.inputs)): p.read_bytes() for p in self.inputs.rglob("*") if p.is_file()}
        initializer.preflight(self.plan, self.inputs)
        self.assertFalse(any(p.exists() for p in self.directories))
        self.assertEqual(list(self.shared.iterdir()), [])
        self.assertEqual(before, {str(p.relative_to(self.inputs)): p.read_bytes() for p in self.inputs.rglob("*") if p.is_file()})

    def test_init_only_once_then_copy_local_state(self):
        with mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded", side_effect=self.fake_init) as runner:
            draft = initializer.initialize(self.plan, self.inputs, java="test-java")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual((self.directories[1] / "index/initial-state").read_text(), "fixture-index")
        self.assertEqual(draft["artifacts"], self.plan["artifacts"])
        self.assertEqual(draft["sites"][1]["config_hashes"], self.plan["sites"][1]["config_hashes"])
        self.assertTrue((self.shared / ".platform-gerrit-init").is_file())
        for site, directory in zip(draft["sites"], self.directories):
            self.assertTrue(initializer.sites.verify_site(draft, site["name"], directory)["integrity_verified"])

    def test_missing_empty_or_ambiguous_email_signing_key_stops_before_writes(self):
        secure = self.inputs / "gerrit-a/etc/secure.config"
        for data in ("# No signing key\n", "[auth]\n registerEmailPrivateKey = \" \"\n",
                     "[auth]\n registerEmailPrivateKey = one\n registerEmailPrivateKey = two\n"):
            with self.subTest(data=data):
                secure.write_text(data, encoding="utf-8")
                with self.assertRaisesRegex(initializer.InitializationError, "email signing key"):
                    initializer.initialize(self.plan, self.inputs, java="test-java")
                self.assertFalse(any(path.exists() for path in self.directories))
                self.assertEqual(list(self.shared.iterdir()), [])

    def enable_ceph_scope(self):
        self.plan["shared_storage"] = {"schema_version": 1, "fsid": "00000000-0000-0000-0000-000000000001", "filesystem": "platform-shared", "path": "/gerrit-shared"}
        for site in self.plan["sites"]:
            path = self.inputs / site["name"] / "etc/high-availability.config"
            path.write_text(f'[main]\n sharedDirectory = {json.dumps(str(self.shared))}\n', encoding="utf-8")
            site["config_hashes"]["etc/high-availability.config"] = digest(path.read_bytes())

    def test_ceph_scoped_initialization_preserves_the_reviewed_mount_identity(self):
        self.enable_ceph_scope()
        with mock.patch.object(initializer.sites.storage_guard, "check", return_value={"mount_id": 50}) as check, mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded", side_effect=self.fake_init) as runner:
            draft = initializer.initialize(self.plan, self.inputs, java="test-java")
        self.assertEqual(check.call_count, 3)
        self.assertEqual(sum(call.args[0][0] == "test-java" for call in runner.call_args_list), 1)
        self.assertEqual(draft["shared_storage"], self.plan["shared_storage"])

    def test_different_server_ids_refuse_before_initialization(self):
        self.enable_ceph_scope()
        site = self.plan["sites"][1]
        config = self.inputs / site["name"] / "etc/gerrit.config"
        config.write_text(config.read_text().replace("01234567-89ab-cdef-0123-456789abcdef", "fedcba98-7654-3210-fedc-ba9876543210"))
        site["config_hashes"]["etc/gerrit.config"] = digest(config.read_bytes())
        with mock.patch.object(initializer.sites.storage_guard, "check", return_value={"mount_id": 50}):
            with self.assertRaisesRegex(initializer.InitializationError, "same explicit server ID"):
                initializer.preflight(self.plan, self.inputs)
        self.assertFalse(any(path.exists() for path in self.directories))
        self.assertEqual(list(self.shared.iterdir()), [])

    def test_init_cannot_replace_a_shared_server_id(self):
        self.enable_ceph_scope()

        def change_identity(arguments, **kwargs):
            result = self.fake_init(arguments, **kwargs)
            if arguments[0] == "test-java":
                config = self.directories[0] / "etc/gerrit.config"
                config.write_text(config.read_text().replace(
                    "01234567-89ab-cdef-0123-456789abcdef", "fedcba98-7654-3210-fedc-ba9876543210"))
            return result

        with mock.patch.object(initializer.sites.storage_guard, "check", return_value={"mount_id": 50}), \
                mock.patch.object(initializer, "check_config"), \
                mock.patch.object(initializer, "run_bounded", side_effect=change_identity):
            with self.assertRaisesRegex(initializer.InitializationError, "same explicit server ID"):
                initializer.initialize(self.plan, self.inputs, java="test-java")
        self.assertFalse((self.directories[1] / "index").exists())

    def test_version_two_disabled_health_probe_refuses_before_target_or_shared_writes(self):
        from test_gerrit_storage import health_configuration
        from test_gerrit_artifacts import populate_artifacts
        self.enable_ceph_scope(); self.plan['schema_version'] = 2
        for site in self.plan['sites']:
            self.plan['artifacts'] = populate_artifacts(self.inputs / site['name'], self.plan['gerrit_version'])
            path = self.inputs / site['name'] / 'etc/healthcheck.config'
            content = health_configuration().replace('enabled = true', 'enabled = false', 1)
            path.write_text(content, newline='\n')
            site['config_hashes']['etc/healthcheck.config'] = digest(path.read_bytes())
        with mock.patch.object(initializer.sites.storage_guard, 'check', return_value={'mount_id': 50}), mock.patch.object(initializer, 'run_bounded') as runner:
            # Preserve real Git-based preflight checks. The Java runner must
            # remain unreachable even when the disabled file has a matching hash.
            from bounded_subprocess import run_bounded
            def commands(arguments, **kwargs):
                if arguments[0] != 'git': raise AssertionError('Java must not run')
                return run_bounded(arguments, **kwargs)
            runner.side_effect = commands
            with self.assertRaises(ValueError): initializer.initialize(self.plan, self.inputs, java='test-java')
        self.assertFalse(any(p.exists() for p in self.directories)); self.assertFalse(list(self.shared.iterdir()))

    def test_version_two_health_credentials_are_preserved_across_initialization(self):
        from test_gerrit_storage import health_configuration
        from test_gerrit_artifacts import populate_artifacts
        self.enable_ceph_scope(); self.plan['schema_version'] = 2
        for site in self.plan['sites']:
            self.plan['artifacts'] = populate_artifacts(self.inputs / site['name'], self.plan['gerrit_version'])
            path = self.inputs / site['name'] / 'etc/healthcheck.config'
            path.write_text(health_configuration(), newline='\n'); site['config_hashes']['etc/healthcheck.config'] = digest(path.read_bytes())
        def changed(arguments, **kwargs):
            result = self.fake_init(arguments, **kwargs)
            if arguments[0] != "test-java":
                return result
            path = self.directories[0] / 'etc/healthcheck.config'
            path.write_text(health_configuration().replace('<PRIVATE_TEST_CREDENTIAL>', '<CHANGED_PRIVATE_TEST_CREDENTIAL>'), newline='\n')
            return result
        with mock.patch.object(initializer.sites.storage_guard, 'check', return_value={'mount_id': 50}), mock.patch.object(initializer, 'check_config'), mock.patch.object(initializer, 'run_bounded', side_effect=changed):
            with self.assertRaises(initializer.InitializationError): initializer.initialize(self.plan, self.inputs, java='test-java')
        self.assertTrue((self.shared / '.platform-gerrit-init').exists())
        self.assertFalse((self.directories[1] / 'index').exists())

    def test_version_two_matching_hashes_cannot_authorize_snapshot_or_wrong_server_artifacts(self):
        from test_gerrit_storage import health_configuration
        from test_gerrit_artifacts import populate_artifacts
        self.enable_ceph_scope(); self.plan['schema_version'] = 2
        for site in self.plan['sites']:
            path = self.inputs / site['name'] / 'etc/healthcheck.config'
            path.write_text(health_configuration(), newline='\n'); site['config_hashes']['etc/healthcheck.config'] = digest(path.read_bytes())
        for mode in ('snapshot-api', 'different-war'):
            with self.subTest(mode=mode):
                for site in self.plan['sites']:
                    self.plan['artifacts'] = populate_artifacts(self.inputs / site['name'],
                        self.plan['gerrit_version'] if mode == 'snapshot-api' else '3.13.2',
                        api_version=self.plan['gerrit_version'] + '-SNAPSHOT' if mode == 'snapshot-api' else self.plan['gerrit_version'])
                with mock.patch.object(initializer.sites.storage_guard, 'check', return_value={'mount_id': 50}), mock.patch.object(initializer, 'run_bounded') as runner:
                    with self.assertRaises(ValueError): initializer.initialize(self.plan, self.inputs, java='test-java')
                runner.assert_not_called()
                self.assertFalse(any(p.exists() for p in self.directories)); self.assertFalse(list(self.shared.iterdir()))

    def test_ceph_mount_change_during_site_copy_stops_before_shared_git_writes(self):
        self.enable_ceph_scope()
        with mock.patch.object(initializer.sites.storage_guard, "check", side_effect=[{"mount_id": 50}, {"mount_id": 50}, {"mount_id": 51}]), mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded", wraps=run_bounded) as runner:
            with self.assertRaises(initializer.InitializationError): initializer.initialize(self.plan, self.inputs, java="test-java")
        self.assertTrue(all(call.args[0][0] == "git" for call in runner.call_args_list))
        self.assertFalse(list(self.shared.iterdir()))
        self.assertTrue(all(t.exists() for t in self.directories))

    def test_valid_mount_with_local_ha_configuration_refuses_before_target_creation(self):
        self.enable_ceph_scope()
        site = self.plan["sites"][0]
        path = self.inputs / site["name"] / "etc/high-availability.config"
        path.write_text('[main]\n sharedDirectory = shared\n', encoding="utf-8")
        site["config_hashes"]["etc/high-availability.config"] = digest(path.read_bytes())
        with mock.patch.object(initializer.sites.storage_guard, "check", return_value={"mount_id": 50}), mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded") as runner:
            with self.assertRaises(ValueError): initializer.initialize(self.plan, self.inputs, java="test-java")
        runner.assert_not_called(); self.assertFalse(any(t.exists() for t in self.directories))

    def test_existing_target_is_never_overwritten(self):
        self.directories[0].mkdir()
        material = self.directories[0] / "existing-data"
        material.write_text("retain", encoding="utf-8")
        with mock.patch.object(initializer, "run_bounded") as runner, self.assertRaises(initializer.InitializationError):
            initializer.initialize(self.plan, self.inputs, java="test-java")
        runner.assert_not_called()
        self.assertEqual(material.read_text(), "retain")
        self.assertFalse(self.directories[1].exists())

    def test_nonempty_shared_store_is_never_initialized(self):
        old = self.shared / "existing-git-data"
        old.write_text("retain", encoding="utf-8")
        with self.assertRaises(initializer.InitializationError):
            initializer.preflight(self.plan, self.inputs)
        self.assertEqual(old.read_text(), "retain")
        self.assertFalse(any(p.exists() for p in self.directories))

    def test_artifact_mismatch_stops_before_target_creation(self):
        (self.inputs / "gerrit-a/bin/gerrit.war").write_bytes(b"modified")
        with self.assertRaises(ValueError):
            initializer.preflight(self.plan, self.inputs)
        self.assertFalse(any(p.exists() for p in self.directories))

    def test_private_key_mismatch_stops_before_creation(self):
        (self.inputs / "gerrit-b/etc/ssh_host_ed25519_key").write_bytes(b"different-test-fixture")
        with self.assertRaises(initializer.InitializationError):
            initializer.preflight(self.plan, self.inputs)
        self.assertFalse(any(p.exists() for p in self.directories))

    def test_failure_retains_new_state_no_retry_on_existing_data(self):
        with mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded", return_value=subprocess.CompletedProcess([], 1, "private-log", "")):
            with self.assertRaises(initializer.InitializationError):
                initializer.initialize(self.plan, self.inputs, java="test-java")
        self.assertTrue(all(p.is_dir() for p in self.directories))
        self.assertTrue((self.shared / ".platform-gerrit-init").exists())
        with self.assertRaises(initializer.InitializationError):
            initializer.preflight(self.plan, self.inputs)

    def test_missing_shared_repositories_is_not_success(self):
        with mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaises(initializer.InitializationError):
                initializer.initialize(self.plan, self.inputs, java="test-java")

    def test_init_private_state_changes_require_review(self):
        def modified(arguments, **kwargs):
            result = self.fake_init(arguments, **kwargs)
            (self.directories[0] / "etc/secure.config").write_bytes(b"changed-test-fixture")
            return result
        with mock.patch.object(initializer, "check_config"), mock.patch.object(initializer, "run_bounded", side_effect=modified):
            with self.assertRaises(initializer.InitializationError):
                initializer.initialize(self.plan, self.inputs, java="test-java")
        self.assertTrue((self.directories[0] / "etc/secure.config").exists())

    def test_git_configuration_checks_shared_path_and_instance(self):
        config = self.inputs / "gerrit-a/etc/gerrit.config"
        initializer.check_config(config, self.shared, "gerrit-a")
        for source, replacement in (("LDAP", "DEVELOPMENT_BECOME_ANY_ACCOUNT"), ("gerrit-a", "gerrit-b")):
            original = config.read_text()
            config.write_text(original.replace(source, replacement))
            with self.assertRaises(initializer.InitializationError):
                initializer.check_config(config, self.shared, "gerrit-a")
            config.write_text(original)
        config.write_text(config.read_text() + '\n[include]\n path = private-other.conf\n')
        with self.assertRaises(initializer.InitializationError):
            initializer.check_config(config, self.shared, "gerrit-a")

    def test_private_configuration_cannot_override_checked_target(self):
        config = self.inputs / "gerrit-a/etc/gerrit.config"
        private = config.with_name("secure.config")
        private.write_text('[gerrit]\n basePath = /other-store\n', encoding="utf-8")
        with self.assertRaises(initializer.InitializationError):
            initializer.check_config(config, self.shared, "gerrit-a")
        self.assertFalse(any(p.exists() for p in self.directories))

    def test_cli_requires_explicit_apply_and_outputs_only_aggregate(self):
        plan_file = self.base / "plan.json"
        plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        args = ["--plan", str(plan_file), "--inputs", str(self.inputs)]
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(initializer.main(args), 0)
        report = json.loads(stream.getvalue())
        self.assertTrue(report["read_only"])
        self.assertFalse(report["initialization_performed"])
        self.assertNotIn(str(self.base), stream.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(initializer.main(args + ["--apply"]), 1)
        self.assertNotIn(str(self.base), error.getvalue())
        self.assertFalse(any(p.exists() for p in self.directories))


if __name__ == "__main__":
    unittest.main()
