#!/usr/bin/env python3
"""Check container preparation packaging without applying Jobs or running init."""

import ast
import contextlib
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import render_gerrit_preparation as preparation
import render_gerrit_sites as sites
from test_gerrit_sites import fixture


IMAGE = "registry.example.com/platform/gerrit-preparation@sha256:" + "c" * 64


def container_plan():
    plan = fixture()
    plan["shared_host_path"] = "/shared"
    for site in plan["sites"]:
        site["host_path"] = f"/volumes/{site['name']}/site"
        site["claim_subpath"] = "site"
    return plan


class GerritPreparationTests(unittest.TestCase):
    def test_version_two_health_configuration_is_private_input_and_staged_for_each_primary(self):
        plan = container_plan(); plan["schema_version"] = 2
        plan["shared_storage"] = {"schema_version": 1, "fsid": "00000000-0000-0000-0000-000000000001", "filesystem": "platform-shared", "path": "/gerrit-shared"}
        for site in plan["sites"]: site["config_hashes"]["etc/healthcheck.config"] = "d" * 64
        for runtime in ("rke2", "k3s", "k8s"):
            files = preparation.render(plan, runtime, IMAGE, artifact_claim="gerrit-release", report_claim="gerrit-reports")
            config, job = list(yaml.safe_load_all(files["preparation.yaml"]))
            secrets = [v["secret"] for v in job["spec"]["template"]["spec"]["volumes"] if "secret" in v]
            self.assertTrue(all("healthcheck.config" in {i["key"] for i in s["items"]} for s in secrets))
            self.assertIn('/bootstrap/gerrit-a/etc/healthcheck.config', config["data"]["stage.sh"])
            self.assertIn('/bootstrap/gerrit-b/etc/healthcheck.config', config["data"]["stage.sh"])
            self.assertNotIn('healthcheck.config', config["data"])
        native = preparation.render(plan, 'docker', IMAGE, bootstrap_path='/srv/bootstrap', report_path='/srv/reports')
        self.assertIn('etc/healthcheck.config', native['site-plan.json'])

    def kube(self, runtime="rke2"):
        return preparation.render(container_plan(), runtime, IMAGE, artifact_claim="gerrit-release", report_claim="gerrit-reports")

    def test_all_kubernetes_runtimes_have_single_attempt_persistent_preparation(self):
        for runtime in ("rke2", "k3s", "k8s"):
            with self.subTest(runtime=runtime):
                files = self.kube(runtime)
                objects = list(yaml.safe_load_all(files["preparation.yaml"]))
                self.assertEqual([o["kind"] for o in objects], ["ConfigMap", "Job"])
                config, job = objects
                self.assertTrue(config["immutable"])
                self.assertEqual(job["spec"]["backoffLimit"], 0)
                self.assertNotIn("ttlSecondsAfterFinished", job["spec"])
                self.assertEqual(job["spec"]["activeDeadlineSeconds"], 900)
                pod = job["spec"]["template"]["spec"]
                self.assertEqual(pod["restartPolicy"], "Never")
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
                self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
                volumes = {v["name"]: v for v in pod["volumes"]}
                self.assertEqual(volumes["report"]["persistentVolumeClaim"]["claimName"], "gerrit-reports")
                self.assertTrue(volumes["release"]["persistentVolumeClaim"]["readOnly"])
                self.assertEqual(volumes["code"]["configMap"]["name"], config["metadata"]["name"])
                self.assertFalse(json.loads(files["render-status.json"])["executed"])

    def test_secret_references_only_and_bounded_input_keys(self):
        config, job = list(yaml.safe_load_all(self.kube()["preparation.yaml"]))
        secrets = [v["secret"] for v in job["spec"]["template"]["spec"]["volumes"] if "secret" in v]
        self.assertEqual({s["secretName"] for s in secrets}, {"gerrit-a-bootstrap", "gerrit-b-bootstrap"})
        for secret in secrets:
            self.assertEqual({item["key"] for item in secret["items"]}, set(preparation.SECRET_FILES))
        self.assertNotIn("secure.config", config["data"])
        self.assertNotIn("keystore", config["data"])

    def test_embedded_python_modules_are_a_closed_first_party_import_set(self):
        files = preparation.code_bundle(container_plan())
        first_party = {path.stem for path in (preparation.ROOT / "scripts").glob("*.py")}
        embedded = {Path(name).stem for name in preparation.MODULES}
        for name in preparation.MODULES:
            for node in ast.walk(ast.parse(files[name])):
                if isinstance(node, ast.ImportFrom) and node.module in first_party:
                    self.assertIn(node.module, embedded, (name, node.module))
                if isinstance(node, ast.Import):
                    for imported in node.names:
                        if imported.name in first_party:
                            self.assertIn(imported.name, embedded, (name, imported.name))

    def test_initializer_uses_report_mount_beside_projected_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            scripts = project / "scripts"
            revision = scripts / "..fixture-revision"
            revision.mkdir(parents=True)
            (project / "private").mkdir()
            name = "initialize_gerrit_sites.py"
            source = preparation.ROOT / "scripts" / name
            shutil.copyfile(source, revision / name)
            try:
                (scripts / "..data").symlink_to(revision.name, target_is_directory=True)
                (scripts / name).symlink_to(Path("..data") / name)
            except OSError:
                self.skipTest("Creating temporary ConfigMap-style symlinks requires OS permission")
            initializer = runpy.run_path(str(scripts / name))
            self.assertEqual(initializer["ROOT"] / "private", project.resolve() / "private")

    def test_code_plan_and_image_changes_are_content_addressed(self):
        def config_name(files):
            return next(yaml.safe_load_all(files["preparation.yaml"]))["metadata"]["name"]
        original = config_name(self.kube())
        plan = container_plan()
        plan["artifacts"]["bin/gerrit.war"] = "d" * 64
        updated = preparation.render(plan, "k8s", IMAGE, artifact_claim="gerrit-release", report_claim="gerrit-reports")
        self.assertNotEqual(original, config_name(updated))
        self.assertIn("--apply --confirm INITIALIZE_NEW_GERRIT_SITES", preparation.stage_script(plan))

    def test_runtime_mounts_match_prepared_site_subdirectory(self):
        plan = container_plan()
        rendered = sites.render(plan, "rke2")
        for site in plan["sites"]:
            deployment = list(yaml.safe_load_all(rendered[site["name"] + ".yaml"]))[1]
            volumes = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
            mount = next(v for v in volumes if v["name"] == "site")
            self.assertEqual(mount["subPath"], "site")

    def test_existing_site_plan_root_mount_is_preserved(self):
        plan = fixture()
        deployment = sites.deployment(plan, plan["sites"][0])[1]
        mount = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]
        self.assertNotIn("subPath", mount)

    def test_docker_is_one_shot_no_ports_and_only_explicit_mounts(self):
        files = preparation.render(container_plan(), "docker", IMAGE,
                                   bootstrap_path="/srv/gerrit-bootstrap", report_path="/srv/gerrit-reports")
        service = yaml.safe_load(files["preparation-compose.yaml"])["services"]["gerrit-prepare"]
        self.assertEqual(service["restart"], "no")
        self.assertEqual(service["command"], [])
        self.assertEqual(service["entrypoint"], preparation.initializer_command())
        self.assertNotIn("ports", service)
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["user"], "1000:1000")
        self.assertTrue(all(not v["bind"]["create_host_path"] for v in service["volumes"]))
        bootstrap = next(v for v in service["volumes"] if v["target"] == "/bootstrap")
        self.assertTrue(bootstrap["read_only"])
        self.assertTrue(set(preparation.MODULES) <= set(files))

    def test_unsafe_or_ambiguous_layout_is_rejected(self):
        cases = [fixture()]
        plan = container_plan()
        plan["sites"][0]["claim_subpath"] = "../escape"
        cases.append(plan)
        for plan in cases:
            with self.assertRaises(ValueError):
                preparation.render(plan, "k8s", IMAGE, artifact_claim="gerrit-release", report_claim="gerrit-reports")
        with self.assertRaises(ValueError):
            preparation.render(container_plan(), "k8s", "image:latest", artifact_claim="gerrit-release", report_claim="gerrit-reports")
        with self.assertRaises(ValueError):
            preparation.render(container_plan(), "k8s", IMAGE, artifact_claim="gerrit-shared", report_claim="gerrit-reports")
        with self.assertRaises(ValueError):
            preparation.render(container_plan(), "docker", IMAGE, bootstrap_path="/shared/input", report_path="/srv/reports")
        with self.assertRaises(ValueError):
            preparation.render(container_plan(), "raw", IMAGE)

    def test_cli_render_only_and_private_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_file = root / "plan.json"
            plan_file.write_text(json.dumps(container_plan()), encoding="utf-8")
            output = root / "private" / "generated"
            with mock.patch.object(preparation, "ROOT", root), mock.patch.object(preparation, "code_bundle", return_value={"site-plan.json": "{}"}), contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(preparation.main(["--plan", str(plan_file), "--runtime", "docker",
                    "--preparation-image", IMAGE, "--bootstrap-host-path", "/srv/bootstrap",
                    "--report-host-path", "/srv/reports", "--output-dir", str(output)]), 0)
            self.assertFalse(json.loads(stream.getvalue())["executed"])
            self.assertNotIn(str(root), stream.getvalue())
            self.assertTrue((output / "preparation-compose.yaml").is_file())

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is not installed")
    def test_docker_compose_parser_accepts_preparation_and_daemon(self):
        version = subprocess.run(["docker", "compose", "version", "--short"],
                                 capture_output=True, timeout=20, check=False)
        if version.returncode:
            self.skipTest("Docker Compose plugin is unavailable")
        plan = container_plan()
        prepared = preparation.render(plan, "docker", IMAGE,
                                      bootstrap_path="/srv/bootstrap", report_path="/srv/reports")
        daemon = sites.render(plan, "docker")
        # 'config --quiet' only parses configuration; no engine, pull, start,
        # credential, network, or live cluster access is requested.
        for name, text in {**prepared, **daemon}.items():
            if not name.endswith("compose.yaml"):
                continue
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / name
                path.write_text(text, encoding="utf-8")
                result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--quiet"],
                                        capture_output=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, "Compose rejected a synthetic Gerrit manifest")


if __name__ == "__main__":
    unittest.main()
