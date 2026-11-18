#!/usr/bin/env python3
"""Exercise site artifact locks and Kubernetes/Docker/native packaging."""

import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

import render_gerrit_sites as sites
from test_bash_support import BashRuntimeUnavailable, bash_executable, bash_path, run_bash_args


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture():
    return {"schema_version": 1, "gerrit_version": "3.13.1",
            "java_image": "docker.io/library/eclipse-temurin@sha256:" + "b" * 64,
            "namespace": "gerrit", "shared_claim": "gerrit-shared", "shared_host_path": "/srv/gerrit-shared",
            "ssh_public_key_sha256": digest(b"public-key-fixture"),
            "artifacts": {name: digest(name.encode()) for name in sites.ARTIFACTS},
            "sites": [{"name": name, "claim": name + "-site", "host_path": "/srv/" + name,
                       "config_hashes": {name: digest(name.encode()) for name in sites.SITE_LOCKS}}
                      for name in ("gerrit-a", "gerrit-b")]}


def prepare_site(directory: Path):
    directory.mkdir()
    for relative in (*sites.ARTIFACTS, *sites.SITE_LOCKS):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    (directory / "etc/ssh_host_ed25519_key.pub").write_bytes(b"public-key-fixture")


class GerritSitesTests(unittest.TestCase):
    def test_all_runtime_packages_include_startup_locks(self):
        for runtime in sites.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = sites.render(fixture(), runtime)
                for name in ("gerrit-a", "gerrit-b"):
                    self.assertIn(name + ".sha256", files)
                    self.assertEqual(len(files[name + ".sha256"].splitlines()), 7)
                    self.assertIn("lib/global-refdb.jar", files[name + ".sha256"])
                    extension = ".yaml" if runtime in {"rke2", "k3s", "k8s"} else "-compose.yaml" if runtime == "docker" else ".service"
                    self.assertIn(name + extension, files)
                status = json.loads(files["render-status.json"])
                self.assertFalse(status["deployed"])
                self.assertFalse(status["ha_verified"])
                self.assertFalse(status["initialization_performed"])
                self.assertFalse(status["shared_storage_guarded"])
                self.assertFalse(status["storage_acceptance_verified"])

    def test_kubernetes_sites_have_distinct_claims_shared_git_and_recreate(self):
        files = sites.render(fixture(), "k8s")
        local_claims = set()
        for name in ("gerrit-a", "gerrit-b"):
            objects = list(yaml.safe_load_all(files[name + ".yaml"]))
            resources = {o["kind"]: o for o in objects}
            self.assertEqual(set(resources), {"ConfigMap", "Deployment", "Service"})
            deployment = resources["Deployment"]["spec"]
            self.assertEqual(deployment["replicas"], 1)
            self.assertEqual(deployment["strategy"], {"type": "Recreate"})
            pod = deployment["template"]["spec"]
            claims = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v}
            self.assertEqual(claims["shared"], "gerrit-shared")
            local_claims.add(claims["site"])
            self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["containers"][0]["command"][0], "/bin/sh")
            startup = next(v for v in pod["volumes"] if v["name"] == "startup")
            self.assertEqual(startup["configMap"]["name"], resources["ConfigMap"]["metadata"]["name"])
            self.assertTrue(resources["ConfigMap"]["immutable"])
        self.assertEqual(len(local_claims), 2)

    def test_changed_locks_create_new_immutable_configmap(self):
        plan = fixture()
        before = sites.deployment(plan, plan["sites"][0])[0]["metadata"]["name"]
        plan["artifacts"]["bin/gerrit.war"] = "c" * 64
        after = sites.deployment(plan, plan["sites"][0])[0]["metadata"]["name"]
        self.assertNotEqual(before, after)

    def test_docker_uses_separate_initialized_host_mounts(self):
        files = sites.render(fixture(), "docker")
        for name in ("gerrit-a", "gerrit-b"):
            service = yaml.safe_load(files[name + "-compose.yaml"])["services"][name]
            self.assertEqual(service["entrypoint"], ["/bin/sh", "/opt/gerrit-startup/start.sh"])
            self.assertTrue(service["read_only"])
            self.assertEqual(service["volumes"][0]["source"], "/srv/" + name)
            self.assertTrue(all(not v["bind"]["create_host_path"] for v in service["volumes"]))
            self.assertNotIn("--init", str(service))

    def test_native_unit_is_not_a_docker_wrapper(self):
        unit = sites.render(fixture(), "raw")["gerrit-a.service"]
        self.assertIn("User=gerrit", unit)
        self.assertIn("RequiresMountsFor=/srv/gerrit-a /srv/gerrit-shared", unit)
        self.assertNotIn("docker", unit)
        self.assertIn("ReadWritePaths=/srv/gerrit-a /srv/gerrit-shared", unit)

    def test_verify_matches_without_writing_and_detects_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "site"
            prepare_site(root)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            report = sites.verify_site(fixture(), "gerrit-a", root)
            self.assertTrue(report["integrity_verified"])
            self.assertEqual(report["files_verified"], 7)
            self.assertFalse(report["compatibility_verified"])
            self.assertFalse(report["ha_verified"])
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
            (root / "bin/gerrit.war").write_bytes(b"changed-fixture")
            with self.assertRaises(sites.SiteError):
                sites.verify_site(fixture(), "gerrit-a", root)

    def test_each_required_artifact_is_verified(self):
        for relative in (*sites.ARTIFACTS, *sites.SITE_LOCKS, "etc/ssh_host_ed25519_key.pub"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "site"
                prepare_site(root)
                (root / relative).write_bytes(b"mismatch")
                with self.assertRaises(sites.SiteError):
                    sites.verify_site(fixture(), "gerrit-a", root)

    def test_invalid_plans_rejected(self):
        cases = [None, {}, {**fixture(), "password": "not-allowed"}]
        for key, value in (("gerrit_version", "latest"), ("gerrit_version", "3.13.1-rc1"),
                           ("java_image", "java:latest"), ("namespace", "../escape"),
                           ("shared_host_path", "/srv/gerrit-a"),
                           ("shared_host_path", "/srv/gerrit-a/subdirectory"),
                           ("shared_host_path", "/srv/gerrit-shared;id"),
                           ("artifacts", {}), ("sites", []), ("schema_version", True)):
            plan = fixture()
            plan[key] = value
            cases.append(plan)
        plan = fixture()
        plan["sites"][1]["claim"] = plan["sites"][0]["claim"]
        cases.append(plan)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(sites.SiteError):
                sites.render(case, "raw")

    def test_cli_reports_integrity_only_without_reading_private_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "site"
            prepare_site(root)
            plan_file = base / "plan.json"
            plan_file.write_text(json.dumps(fixture()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(sites.main(["verify-site", "--plan", str(plan_file),
                                            "--site-name", "gerrit-a", "--site-root", str(root)]), 0)
            report = json.loads(stream.getvalue())
            self.assertTrue(report["integrity_verified"])
            self.assertFalse(report["ha_verified"])
            # No secret or keystore files were even provisioned in this fixture.
            self.assertFalse((root / "etc/secure.config").exists())
            self.assertNotIn(str(root), stream.getvalue())

    def test_cli_invalid_plan_does_not_echo_input_or_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            plan_file = base / "plan.json"
            plan = fixture()
            plan["unapproved_private_field"] = "private-value-do-not-echo"
            plan_file.write_text(json.dumps(plan), encoding="utf-8")
            output = base / "private" / "new"
            with mock.patch.object(sites, "ROOT", base), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(sites.main(["render", "--plan", str(plan_file), "--runtime", "raw",
                                            "--output-dir", str(output)]), 1)
            self.assertNotIn("private-value-do-not-echo", error.getvalue())
            self.assertFalse(output.exists())

    def test_startup_guard_runs_before_java_and_fails_on_tampering(self):
        try:
            _, flavor = bash_executable()
        except BashRuntimeUnavailable as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "site"
            prepare_site(root)
            for name in ("secure.config", "keystore", "ssh_host_ed25519_key"):
                (root / "etc" / name).write_text("non-secret-test-fixture", encoding="utf-8")
            shared = base / "shared"
            for name in ("All-Projects.git", "All-Users.git"):
                (shared / "git" / name).mkdir(parents=True)
            lock = base / "artifacts.sha256"
            lock.write_text(sites.lock_text(fixture(), fixture()["sites"][0]), encoding="utf-8", newline="\n")
            script = base / "start.sh"
            script.write_text(sites.startup_script(), encoding="utf-8", newline="\n")
            fake_java = base / "java"
            fake_java.write_text('#!/bin/sh\nprintf started > "$JAVA_TEST_MARKER"\n', encoding="utf-8", newline="\n")
            fake_java.chmod(0o700)
            marker = base / "marker"
            command = 'PATH="$1:$PATH"; export PATH; JAVA_TEST_MARKER="$2"; export JAVA_TEST_MARKER; exec sh "$3" "$4" "$5" "$6"'
            arguments = ["-c", command, "gerrit-startup-test", *(bash_path(p, flavor) for p in (base, marker, script, root, shared, lock))]
            first = run_bash_args(arguments)
            self.assertEqual(first.returncode, 0)
            self.assertTrue(marker.is_file())
            marker.unlink()
            (root / "plugins/high-availability.jar").write_bytes(b"tampered")
            second = run_bash_args(arguments)
            self.assertNotEqual(second.returncode, 0)
            self.assertFalse(marker.exists())
            # A repaired artifact lock must not bypass missing private state or
            # shared-repository prerequisites.
            (root / "plugins/high-availability.jar").write_bytes(b"plugins/high-availability.jar")
            (root / "etc/keystore").unlink()
            third = run_bash_args(arguments)
            self.assertNotEqual(third.returncode, 0)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
