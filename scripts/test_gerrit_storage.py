#!/usr/bin/env python3
"""Exercise CephFS identity checks and their wiring into Gerrit preparation/startup."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import gerrit_storage as guard
import initialize_gerrit_sites as initialization
import render_gerrit_sites as sites
from test_gerrit_sites import fixture, prepare_site


def scope():
    return {"schema_version": 1, "fsid": "00000000-0000-0000-0000-000000000001", "filesystem": "platform-shared", "path": "/gerrit-shared"}


def line(*, legacy=False, root="/", source_path="/gerrit-shared"):
    source = "192.0.2.31:3300:" + source_path if legacy else "gerrit@" + scope()["fsid"] + ".platform-shared=" + source_path
    return "50 20 0:53 " + root + " /shared rw,nosuid,nodev - ceph " + source + " rw,ms_mode=secure,fsid=" + scope()["fsid"] + ",mds_namespace=platform-shared\n"


def health_configuration():
    return "".join('[healthcheck "' + name + '"]\n enabled = true\n' for name in guard.REQUIRED_HEALTHCHECKS) + \
        '[healthcheck "auth"]\n username = probe-reader\n password = <PRIVATE_TEST_CREDENTIAL>\n'


class HealthGuardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "Health policy checks require Git's actual parser")
    def test_actual_git_parser_requires_enabled_probes_and_private_auth_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary); (site / "etc").mkdir(); path = site / "etc/healthcheck.config"
            original = health_configuration(); path.write_text(original)
            guard.check_health_configuration(site)
            cases = [original.replace('enabled = true', 'enabled = false', 1),
                original.replace('[healthcheck "jgit"]\n enabled = true\n', ''),
                original + '[healthcheck "jgit"]\n enabled = true\n',
                original + '[healthcheck "jgit"]\n timeout = 0\n',
                original + '[healthcheck]\n timeout = 6000\n',
                original + '[healthcheck]\n timeout = 500\n timeout = 250\n',
                original + '[healthcheck]\n timeout = 0\n' + ''.join('[healthcheck "' + name + '"]\n timeout = 500\n' for name in guard.REQUIRED_HEALTHCHECKS),
                original + '[healthcheck "gitspace"]\n minFreeDiskPercent = 0\n',
                original + '[healthcheck "auth"]\n password = <OTHER_PRIVATE_TEST_CREDENTIAL>\n',
                original.replace('password = <PRIVATE_TEST_CREDENTIAL>', 'password = "  "'),
                original + '[include]\n path = /private/credential-file\n',
                original + '[includeIf "gitdir:/private/"]\n path = /private/credential-file\n']
            for value in cases:
                with self.subTest(case=cases.index(value)):
                    path.write_text(value)
                    with self.assertRaises(ValueError) as caught: guard.check_health_configuration(site)
                    self.assertNotIn('PRIVATE_TEST_CREDENTIAL', str(caught.exception))
                    self.assertNotIn('/private', str(caught.exception))
            path.write_text(original)
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "healthcheck.jgit.enabled", "GIT_CONFIG_VALUE_0": "false"}):
                guard.check_health_configuration(site)
            # Exercise the exact copied runtime helper in isolated mode, where
            # importing a helper from cwd must not shadow the locked adjacent one.
            package = site / 'package'; package.mkdir()
            for name, value in sites.guard_files({'shared_storage': scope()}, str(package)).items():
                (package / name).write_text(value, newline='\n')
            hostile = site / 'hostile'; hostile.mkdir(); (hostile / 'bounded_subprocess.py').write_text('raise RuntimeError("wrong helper")\n')
            from bounded_subprocess import run_bounded
            program = 'import importlib.util,sys; s=importlib.util.spec_from_file_location("guard",sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.check_health_configuration(sys.argv[2])'
            result = run_bounded([sys.executable, '-I', '-B', '-c', program, str(package / 'check_storage.py'), str(site)], cwd=hostile,
                text=True, timeout=30, output_max_bytes=16384, check=False)
            self.assertEqual(result.returncode, 0)

    def test_version_two_pins_health_config_and_guards_every_runtime_before_java(self):
        plan = fixture(); plan["schema_version"] = 2; plan["shared_storage"] = scope()
        for site in plan["sites"]: site["config_hashes"]["etc/healthcheck.config"] = hashlib.sha256(health_configuration().encode()).hexdigest()
        for runtime in sites.RUNTIMES:
            files = sites.render(plan, runtime)
            self.assertIn('--require-healthcheck', files['start.sh'])
            self.assertLess(files['start.sh'].index('--require-healthcheck'), files['start.sh'].index('exec java'))
            self.assertTrue(json.loads(files['render-status.json'])['healthcheck_configuration_guarded'])
            self.assertTrue(json.loads(files['render-status.json'])['artifact_metadata_guarded'])
            self.assertIn('--gerrit-version ' + plan['gerrit_version'], files['start.sh'])
            self.assertIn('gerrit_artifacts.py', files['shared-storage.sha256'])
            self.assertFalse(json.loads(files['render-status.json'])['ha_verified'])
            self.assertIn('etc/healthcheck.config', files['gerrit-a.sha256'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'site'; prepare_site(root)
            from test_gerrit_artifacts import populate_artifacts
            plan['artifacts'] = populate_artifacts(root, plan['gerrit_version'])
            path = root / 'etc/healthcheck.config'; path.write_text(health_configuration(), newline='\n')
            self.assertEqual(sites.verify_site(plan, 'gerrit-a', root)['files_verified'], 8)
            path.write_text(health_configuration().replace('enabled = true', 'enabled = false', 1), newline='\n')
            with self.assertRaises(sites.SiteError): sites.verify_site(plan, 'gerrit-a', root)
        del plan['sites'][0]['config_hashes']['etc/healthcheck.config']
        with self.assertRaises(sites.SiteError): sites.render(plan, 'raw')

    def test_legacy_profile_stays_explicitly_without_health_policy_acceptance(self):
        files = sites.render(fixture(), 'raw')
        self.assertFalse(json.loads(files['render-status.json'])['healthcheck_configuration_guarded'])
        self.assertNotIn('--require-healthcheck', files['start.sh'])


class GuardTests(unittest.TestCase):
    def test_current_and_csi_sources_bind_the_same_encrypted_cephfs_subtree(self):
        for legacy in (False, True):
            self.assertEqual(guard.inspect(scope(), "/shared", line(legacy=legacy).encode())["mount_id"], 50)
            value = line(legacy=legacy, source_path="/", root="/gerrit-shared")
            self.assertEqual(guard.inspect(scope(), "/shared", value.encode())["device"], "0:53")

    def test_ordinary_missing_foreign_readonly_weak_and_stacked_mounts_refuse(self):
        changes = [lambda s: s.replace(" - ceph ", " - ext4 "), lambda s: s.replace("/shared ", "/other "), lambda s: s.replace("ms_mode=secure", "ms_mode=prefer-secure"),
            lambda s: s.replace("rw,nosuid", "ro,nosuid"), lambda s: s.replace("=/gerrit-shared", "=/wrong"), lambda s: s.replace(".platform-shared=", ".foreign-shared="),
            lambda s: s.replace("ms_mode=secure", "ms_mode=secure,ms_mode=crc"), lambda s: s.replace("ms_mode=secure", "ms_mode=secure,recover_session=clean"),
            lambda s: s.replace(",fsid=" + scope()["fsid"], ",fsid=00000000-0000-0000-0000-000000000002"), lambda s: s + s,
            lambda s: s + '51 50 0:54 / /shared/git rw - ext4 fixture rw\n']
        for change in changes:
            with self.assertRaises(ValueError): guard.inspect(scope(), "/shared", change(line()).encode())
        with self.assertRaises(ValueError): guard.inspect(scope(), "/shared", b"")

    def test_legacy_sources_require_explicit_cluster_and_filesystem_identity(self):
        for remove in (",fsid=" + scope()["fsid"], ",mds_namespace=platform-shared"):
            with self.assertRaises(ValueError): guard.inspect(scope(), "/shared", line(legacy=True).replace(remove, "").encode())

    def test_invalid_policy_cannot_disable_the_guard_or_escape_the_subtree(self):
        for key, value in (("schema_version", True), ("fsid", "not-an-id"), ("filesystem", "a;id"), ("path", "/"), ("path", "/data/../git")):
            policy = scope(); policy[key] = value
            with self.assertRaises(ValueError): guard.validate(policy)
        policy = scope(); policy["disabled"] = True
        with self.assertRaises(ValueError): guard.validate(policy)

    def test_every_runtime_packages_the_policy_program_and_checked_launch_arguments(self):
        plan = fixture(); plan["shared_storage"] = scope()
        for runtime in sites.RUNTIMES:
            files = sites.render(plan, runtime)
            self.assertIn("check_storage.py", files); self.assertIn("shared-storage.json", files)
            for name in ("check_storage.py", "shared-storage.json", "bounded_subprocess.py"):
                self.assertIn(hashlib.sha256(files[name].encode()).hexdigest(), files["shared-storage.sha256"])
            self.assertIn('sha256sum --check --strict "$guard_lock"', files["start.sh"])
            self.assertLess(files["start.sh"].index("python3 -I -B"), files["start.sh"].index("exec java"))
            self.assertIn('--site-dir "$site_dir"', files["start.sh"])
            if runtime in {"rke2", "k3s", "k8s"}:
                objects = list(yaml.safe_load_all(files["gerrit-a.yaml"])); config = objects[0]
                self.assertTrue(config["immutable"]); self.assertIn("check_storage.py", config["data"])
                self.assertEqual(len(objects[1]["spec"]["template"]["spec"]["containers"][0]["args"]), 6)
            elif runtime == "docker":
                service = yaml.safe_load(files["gerrit-a-compose.yaml"])["services"]["gerrit-a"]
                self.assertTrue(all(m["read_only"] for m in service["volumes"] if "/opt/gerrit-startup/" in m["target"]))
                self.assertEqual(len(service["command"]), 6)
            else:
                self.assertIn("ConditionPathIsMountPoint=/srv/gerrit-shared", files["gerrit-a.service"])
                self.assertIn("/etc/gerrit/check_storage.py", files["gerrit-a.service"])

    def test_changed_storage_identity_generates_another_immutable_startup_configmap(self):
        plan = fixture(); plan["shared_storage"] = scope()
        before = sites.deployment(plan, plan["sites"][0])[0]["metadata"]["name"]
        plan["shared_storage"]["path"] = "/new-reviewed-subtree"
        self.assertNotEqual(before, sites.deployment(plan, plan["sites"][0])[0]["metadata"]["name"])

    def test_initialization_refuses_storage_before_creating_site_directories(self):
        plan = fixture(); plan["shared_storage"] = scope()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shared = root / "shared"; shared.mkdir()
            targets = [root / "new-a", root / "new-b"]
            with mock.patch.object(initialization, "targets", return_value=(targets, shared)), mock.patch.object(guard, "check", side_effect=ValueError("unmounted")):
                with self.assertRaises(ValueError): initialization.initialize(plan, root / "inputs", java="java")
            self.assertFalse(any(t.exists() for t in targets)); self.assertFalse(list(shared.iterdir()))

    @unittest.skipUnless(shutil.which("git"), "Configuration checks require Git's actual parser")
    def test_git_parser_binds_both_configuration_paths_and_rejects_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); site = root / "site"; (site / "etc").mkdir(parents=True)
            shared = root / "shared"; shared.mkdir()
            expected = {"gerrit.config": '[gerrit]\n basePath = ' + json.dumps(str(shared / "git")) + '\n',
                        "high-availability.config": '[main]\n sharedDirectory = ' + json.dumps(str(shared)) + '\n',
                        "secure.config": '[auth]\n registerEmailPrivateKey = <NON_SECRET_TEST_KEY>\n'}
            def restore():
                for name, value in expected.items(): (site / "etc" / name).write_text(value)
            restore(); guard.check_configuration(site, shared)
            files = sites.guard_files({"shared_storage": scope()}, str(root))
            for name, value in files.items(): (root / name).write_text(value)
            # The runtime invokes Python in isolated mode. Exercise the copied
            # guard/helper too, with a hostile current-directory module present.
            hostile = root / "cwd"; hostile.mkdir()
            (hostile / "bounded_subprocess.py").write_text('raise RuntimeError("Wrong helper loaded")\n')
            from bounded_subprocess import run_bounded
            program = 'import importlib.util,sys; s=importlib.util.spec_from_file_location("checked",sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.check_configuration(sys.argv[2],sys.argv[3]); print("checked")'
            result = run_bounded([sys.executable, "-I", "-B", "-c", program, str(root / "check_storage.py"), str(site), str(shared)], cwd=hostile,
                                 text=True, timeout=30, output_max_bytes=16384, check=False)
            self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(result.stdout.strip(), "checked")
            # Ambient Git overrides must not affect explicit-file queries.
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "gerrit.basePath", "GIT_CONFIG_VALUE_0": "/foreign"}):
                guard.check_configuration(site, shared)
            cases = [("gerrit.config", '[gerrit]\n basePath = /local/git\n'),
                     ("gerrit.config", expected["gerrit.config"] + expected["gerrit.config"]),
                     ("high-availability.config", '[main]\n sharedDirectory = shared\n'),
                     ("high-availability.config", '[main]\n'),
                     ("high-availability.config", expected["high-availability.config"] + '[include]\n path = /foreign\n'),
                     ("gerrit.config", expected["gerrit.config"] + '[includeIf "gitdir:/any/"]\n path = /foreign\n'),
                     ("secure.config", expected["secure.config"] + '[gerrit]\n basePath = /local/git\n'),
                     ("secure.config", expected["secure.config"] + '[include]\n path = /foreign\n')]
            for name, value in cases:
                with self.subTest(name=name, case=value.splitlines()[-1]):
                    restore(); (site / "etc" / name).write_text(value)
                    with self.assertRaises(ValueError): guard.check_configuration(site, shared)
            restore()
            with mock.patch.object(shutil, "which", return_value=None):
                with self.assertRaises(ValueError): guard.check_configuration(site, shared)

    @unittest.skipUnless(os.name == "posix" and shutil.which("git"), "Symlink checks require Linux and Git")
    def test_git_directory_redirect_is_rejected_even_with_matching_configured_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shared = root / "shared"; shared.mkdir()
            foreign = root / "foreign"; foreign.mkdir(); (shared / "git").symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(ValueError): guard.check_configuration(root, shared)

    @unittest.skipUnless(os.name == "posix" and shutil.which("git"), "Symlink checks require Linux and Git")
    def test_configuration_ancestor_redirect_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); shared = root / "shared"; shared.mkdir()
            site = root / "site"; site.mkdir(); foreign = root / "foreign"; foreign.mkdir()
            (site / "etc").symlink_to(foreign, target_is_directory=True)
            (foreign / "gerrit.config").write_text('[gerrit]\n basePath = /shared/git\n')
            with self.assertRaises(ValueError): guard.check_configuration(site, shared)

    @unittest.skipUnless(os.name == "posix", "Actual startup refusal requires a Linux shell")
    def test_real_launcher_does_not_start_java_on_an_ordinary_directory(self):
        from bounded_subprocess import run_bounded
        plan = fixture(); plan["shared_storage"] = scope()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); site = root / "site"; prepare_site(site)
            for filename in ("secure.config", "keystore", "ssh_host_ed25519_key"): (site / "etc" / filename).write_text("non-secret-fixture")
            shared = root / "shared"
            for name in ("All-Projects.git", "All-Users.git"): (shared / "git" / name).mkdir(parents=True)
            files = sites.guard_files(plan, str(root)); files["start.sh"] = sites.startup_script(True); files["artifacts.sha256"] = sites.lock_text(plan, plan["sites"][0])
            for name, text in files.items(): (root / name).write_text(text)
            marker = root / "started"; java = root / "java"; java.write_text('#!/bin/sh\nprintf started > "$JAVA_TEST_MARKER"\n'); java.chmod(0o700)
            environment = {**os.environ, "PATH": str(root) + ":" + os.environ["PATH"], "JAVA_TEST_MARKER": str(marker)}
            result = run_bounded(["/bin/sh", str(root / "start.sh"), str(site), str(shared), str(root / "artifacts.sha256"), *sites.guard_arguments(plan, str(root))], text=True, env=environment, timeout=20, output_max_bytes=16384)
            self.assertNotEqual(result.returncode, 0); self.assertIn("Shared Gerrit storage refused", result.stderr); self.assertFalse(marker.exists())


if __name__ == "__main__": unittest.main()
