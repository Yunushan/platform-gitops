#!/usr/bin/env python3
"""Exercise launcher packaging and handoff guards without registering workers."""

import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import render_nodepool_services as renderer
import start_nodepool as launcher
from test_nodepool_static import plans


def provision(root: Path):
    root.mkdir()
    (root / "private").mkdir()
    payload = b"fixture-nodepool-configuration"
    (root / "nodepool.yaml").write_bytes(payload)
    for name in launcher.TLS_FILES:
        (root / "private" / name).write_bytes(b"non-secret-test-fixture")
    return hashlib.sha256(payload).hexdigest()


class NodepoolServicesTests(unittest.TestCase):
    def test_kubernetes_launchers_are_replicated_without_worker_or_api_credentials(self):
        plan, ci = plans()
        for runtime in ("rke2", "k3s", "k8s"):
            objects = list(yaml.safe_load_all(renderer.render(plan, ci, runtime)["nodepool-services.yaml"]))
            self.assertEqual([o["kind"] for o in objects], ["ConfigMap", "Deployment", "PodDisruptionBudget"])
            config, deployment, pdb = objects
            self.assertTrue(config["immutable"])
            self.assertEqual(deployment["spec"]["replicas"], 2)
            pod = deployment["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
            container = pod["containers"][0]
            self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertNotIn("ports", container)
            self.assertIn("--config-sha256", container["command"])
            hook = container["lifecycle"]["preStop"]["exec"]["command"][2]
            ast.parse(hook)
            self.assertIn("signal.SIGINT", hook)
            private = next(v for v in pod["volumes"] if v["name"] == "private")["secret"]
            self.assertEqual({i["key"] for i in private["items"]}, set(launcher.TLS_FILES))
            self.assertEqual(pdb["spec"]["minAvailable"], 1)

    def test_native_and_docker_use_sigint_and_guarded_commands(self):
        plan, ci = plans()
        native = renderer.render(plan, ci, "raw")["nodepool-launcher.service"]
        self.assertIn("KillSignal=SIGINT", native)
        self.assertIn("User=nodepool", native)
        self.assertIn("--config-sha256", native)
        service = yaml.safe_load(renderer.render(plan, ci, "docker")["nodepool-compose.yaml"])["services"]["nodepool-launcher"]
        self.assertEqual(service["stop_signal"], "SIGINT")
        self.assertEqual(service["command"], [])
        self.assertNotIn("ports", service)
        self.assertFalse(service.get("privileged", False))
        self.assertTrue(all(not volume["bind"]["create_host_path"] for volume in service["volumes"]))

    def test_local_check_does_not_write_or_accept_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            digest = provision(root)
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            report = launcher.local_inputs(digest, root=root)
            self.assertTrue(report["read_only"])
            self.assertFalse(report["registered"])
            self.assertFalse(report["connections_verified"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})
            (root / "nodepool.yaml").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                launcher.local_inputs(digest, root=root)

    def test_tool_checks_and_validation_precede_fixed_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            digest = provision(root)
            state = Path(directory) / "state"
            state.mkdir()
            def result(arguments, **kwargs):
                text = "Nodepool 11.0.0" if "--version" in arguments else ""
                return subprocess.CompletedProcess(arguments, 0, text, "")
            with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(launcher.shutil, "which", side_effect=lambda name: "/reviewed/" + name), \
                    mock.patch.object(launcher, "run_bounded", side_effect=result) as runner, \
                    mock.patch.object(launcher.os, "execv") as execute, mock.patch.dict(os.environ, {}, clear=False):
                launcher.launch("11.0.0", digest, root=root, state=state)
                self.assertEqual(runner.call_count, 3)
                self.assertEqual(runner.call_args_list[-1].args[0][-1], "config-validate")
                execute.assert_called_once_with("/reviewed/nodepool-launcher", ["/reviewed/nodepool-launcher", "-f", "-c",
                    str(root / "nodepool.yaml"), "-p", str(state / "launcher.pid"), "--no-webapp"])

    def test_failed_schema_validation_prevents_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            digest = provision(root)
            state = Path(directory) / "state"
            state.mkdir()
            def result(arguments, **kwargs):
                return subprocess.CompletedProcess(arguments, 0 if "--version" in arguments else 1, "Nodepool 11.0.0", "")
            with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(launcher.shutil, "which", side_effect=lambda name: "/reviewed/" + name), \
                    mock.patch.object(launcher, "run_bounded", side_effect=result), mock.patch.object(launcher.os, "execv") as execute:
                with self.assertRaises(ValueError):
                    launcher.launch("11.0.0", digest, root=root, state=state)
                execute.assert_not_called()

    def test_validator_cannot_change_configuration_before_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            digest = provision(root)
            state = Path(directory) / "state"
            state.mkdir()
            def result(arguments, **kwargs):
                if "config-validate" in arguments:
                    (root / "nodepool.yaml").write_bytes(b"changed-during-validation")
                return subprocess.CompletedProcess(arguments, 0, "Nodepool 11.0.0", "")
            with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=1000, create=True), \
                    mock.patch.object(launcher.shutil, "which", side_effect=lambda name: "/reviewed/" + name), \
                    mock.patch.object(launcher, "run_bounded", side_effect=result), mock.patch.object(launcher.os, "execv") as execute:
                with self.assertRaises(ValueError):
                    launcher.launch("11.0.0", digest, root=root, state=state)
                execute.assert_not_called()

    def test_empty_tls_file_and_invalid_hash_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            digest = provision(root)
            (root / "private/zk-client.key").write_bytes(b"")
            with self.assertRaises(ValueError):
                launcher.local_inputs(digest, root=root)
            with self.assertRaises(ValueError):
                launcher.local_inputs("not-a-digest", root=root)

    def test_root_and_unreviewed_versions_are_rejected(self):
        with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch.object(launcher.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(launcher, "run_bounded") as runner:
            with self.assertRaises(ValueError):
                launcher.launch("11.0.0", "a" * 64)
            runner.assert_not_called()
        for output in ("12.0.0", "11.0.0.dev1", "11.0.0 Python 3.12.0"):
            with mock.patch.object(launcher, "run_bounded", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaises(ValueError):
                    launcher.check_release("fixture-tool", "11.0.0")

    def test_cli_requires_explicit_start(self):
        with mock.patch.object(launcher, "local_inputs", return_value={"read_only": True}), \
                mock.patch.object(launcher, "launch") as start, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["--config-sha256", "a" * 64]), 0)
            start.assert_not_called()
        with mock.patch.object(launcher, "launch") as start, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launcher.main(["--start", "--config-sha256", "a" * 64]), 1)
            start.assert_not_called()

    def test_all_runtime_reports_remain_unverified(self):
        plan, ci = plans()
        for runtime in renderer.RUNTIMES:
            report = json.loads(renderer.render(plan, ci, runtime)["render-status.json"])
            self.assertFalse(report["deployed"])
            self.assertFalse(report["registered"])
            self.assertFalse(report["allocation_verified"])
            self.assertFalse(report["ha_verified"])

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI unavailable")
    def test_docker_compose_parser_without_engine(self):
        version = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, timeout=20, check=False)
        if version.returncode:
            self.skipTest("Compose plugin unavailable")
        plan, ci = plans()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.yaml"
            path.write_text(renderer.render(plan, ci, "docker")["nodepool-compose.yaml"], encoding="utf-8")
            result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--quiet"],
                                    capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, "Compose rejected the synthetic launcher configuration")


if __name__ == "__main__":
    unittest.main()
