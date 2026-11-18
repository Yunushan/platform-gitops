#!/usr/bin/env python3
"""Run the pinned Runner Kubernetes executor against an isolated workerless API."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest

import yaml

import test_rook_api_fixture as api_fixture
from test_gitlab_runner_execution_fixture import RUNNER_SHA256


@unittest.skipUnless(sys.platform == "linux" and all(os.environ.get(k) for k in
    ("ROOK_BOOTSTRAP_TEST_BIN", "GITLAB_RUNNER_KUSTOMIZE", "GITLAB_RUNNER_TEST_BINARY")), "isolated Runner/API fixture not requested")
class RunnerKubernetesFixtureTest(unittest.TestCase):
    def test_real_runner_creates_tokenless_job_pod_with_scoped_identity(self):
        if os.geteuid() == 0: self.fail("Run the fixture as an unprivileged user")
        source = Path(os.environ["GITLAB_RUNNER_TEST_BINARY"]).resolve(strict=True)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), RUNNER_SHA256)
        documents = [x for x in yaml.safe_load_all(Path(os.environ["GITLAB_RUNNER_KUSTOMIZE"]).read_text(encoding="utf-8")) if isinstance(x, dict)]

        def exercise(client, root, kubectl):
            supported = {"Namespace", "ServiceAccount", "Role", "RoleBinding", "NetworkPolicy"}
            objects = [d for d in documents if d["kind"] in supported]
            for obj in [d for d in objects if d["kind"] == "Namespace"] + [d for d in objects if d["kind"] != "Namespace"]:
                kubectl(["create", "-f", "-", "-o", "json"], obj)
            request = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest", "spec": {"expirationSeconds": 600}}
            token = kubectl(["create", "--raw", "/api/v1/namespaces/gitlab-runner/serviceaccounts/gitlab-runner/token", "-f", "-"], request)["status"]["token"]
            kubeconfig = json.loads((root / "kubeconfig.json").read_text(encoding="utf-8"))
            kubeconfig["users"][0]["user"] = {"token": token}
            config_path = root / "runner-kubeconfig.json"
            config_path.write_text(json.dumps(kubeconfig), encoding="utf-8")
            config_path.chmod(0o600)
            runner_template = next(d["data"]["config.template.toml"] for d in documents if d["kind"] == "ConfigMap" and "config.template.toml" in d.get("data", {}))
            self.assertIn('namespace = "gitlab-jobs"', runner_template)
            self.assertIn("automount_service_account_token = false", runner_template)
            binary = root / "gitlab-runner"
            shutil.copyfile(source, binary); binary.chmod(0o700)
            state = {"offered": False}

            class Coordinator(BaseHTTPRequestHandler):
                def log_message(self, *_args): pass
                def reply(self, code, content=b"", extra=None):
                    self.send_response(code)
                    for key, value in (extra or {}).items(): self.send_header(key, value)
                    self.send_header("Content-Length", str(len(content))); self.end_headers()
                    if content: self.wfile.write(content)
                def do_POST(self):
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if self.path != "/api/v4/jobs/request": return self.reply(404)
                    if state["offered"]: return self.reply(204)
                    state["offered"] = True
                    job = {"id": 1001, "token": "fixture-job-token", "allow_git_fetch": False,
                           "job_info": {"name": "fixture", "stage": "test", "project_id": 1, "project_name": "fixture"},
                           "git_info": {"repo_url": "http://127.0.0.1/fixture/repo.git", "sha": "0" * 40, "before_sha": "0" * 40, "ref": "main", "ref_type": "branch"},
                           "variables": [{"key": "GIT_STRATEGY", "value": "none", "public": True}],
                           "steps": [{"name": "script", "script": ["true"], "timeout": 30, "when": "on_success", "allow_failure": False}],
                           "artifacts": [], "cache": [], "credentials": [], "dependencies": [], "features": {}}
                    self.reply(201, json.dumps(job).encode(), {"Content-Type": "application/json"})
                def do_PUT(self):
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))); self.reply(200, b"{}")
                def do_PATCH(self):
                    payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    self.reply(202, extra={"Range": f"0-{max(0, len(payload)-1)}", "Job-Status": "running"})

            server = ThreadingHTTPServer(("127.0.0.1", 0), Coordinator)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            url = f"http://127.0.0.1:{server.server_port}/"
            runner_auth = secrets.token_urlsafe(24)
            runner_config = "concurrent = 1\ncheck_interval = 1\n" + runner_template.replace(
                "[[runners]]", f'[[runners]]\n  url = "{url}"\n  token = "{runner_auth}"\n  executor = "kubernetes"', 1)
            runner_path = root / "config.toml"; runner_path.write_text(runner_config, encoding="utf-8"); runner_path.chmod(0o600)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LC_ALL": "C", "KUBECONFIG": str(config_path)}
            log = (root / "runner-kubernetes.log").open("w+b")
            process = subprocess.Popen([str(binary), "run", "--config", str(runner_path)], env=env, cwd=root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 60; pod = None
                while time.monotonic() < deadline:
                    pods = kubectl(["get", "pods", "-n", "gitlab-jobs", "-o", "json"])["items"]
                    if pods:
                        self.assertEqual(len(pods), 1); pod = pods[0]; break
                    if process.poll() is not None: break
                    time.sleep(0.5)
                if pod is None:
                    log.flush(); log.seek(0); output = log.read(30000).decode("utf-8", errors="replace")
                    for secret in (token, str(root), runner_auth, "fixture-job-token"):
                        output = output.replace(secret, "[fixture]")
                    self.fail("Runner did not create a pod in the isolated API: " + output[-5000:])
                self.assertEqual(pod["metadata"]["namespace"], "gitlab-jobs")
                self.assertEqual(pod["spec"]["serviceAccountName"], "gitlab-job")
                self.assertFalse(pod["spec"]["automountServiceAccountToken"])
                self.assertTrue(all(not c.get("securityContext", {}).get("privileged", False) for c in pod["spec"]["containers"]))
                self.assertEqual(pod["status"]["phase"], "Pending")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=8)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=8)
                log.close(); server.shutdown(); server.server_close(); thread.join(timeout=5)

        api_fixture.exercise(self, management_action=exercise)


if __name__ == "__main__":
    unittest.main()
