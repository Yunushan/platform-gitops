#!/usr/bin/env python3
"""Opt-in local Runner shell-executor job against a synthetic loopback coordinator."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


RUNNER_SHA256 = "8c8ea572b9f0d40e93b876b90c7d093eb403a41bbadbbf9a0fcf27b6b75c2800"


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("GITLAB_RUNNER_TEST_BINARY"), "local runner fixture not requested")
class GitLabRunnerExecutionTest(unittest.TestCase):
    def test_real_runner_executes_synthetic_job_and_reports_success(self):
        if os.geteuid() == 0:
            self.fail("Run this disposable shell-executor fixture as an unprivileged user")
        source = Path(os.environ["GITLAB_RUNNER_TEST_BINARY"]).resolve(strict=True)
        self.assertTrue(source.is_file() and not source.is_symlink())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), RUNNER_SHA256)
        marker = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="gitlab-runner-fixture-") as temporary:
            root = Path(temporary)
            binary = root / "gitlab-runner"
            shutil.copyfile(source, binary)
            binary.chmod(0o700)
            marker_path = root / "job-marker"
            state = {"offered": False, "updates": [], "trace": bytearray(), "paths": []}
            lock = threading.Lock()

            class Coordinator(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def send(self, code, payload=b"", headers=None):
                    self.send_response(code)
                    for key, value in (headers or {}).items(): self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if payload: self.wfile.write(payload)

                def do_POST(self):
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    with lock: state["paths"].append(("POST", self.path))
                    if self.path != "/api/v4/jobs/request": return self.send(404)
                    with lock:
                        if state["offered"]: return self.send(204)
                        state["offered"] = True
                    job = {"id": 1001, "token": "fixture-job-token", "allow_git_fetch": False,
                           "job_info": {"name": "fixture", "stage": "test", "project_id": 1, "project_name": "fixture"},
                           "git_info": {"repo_url": "http://127.0.0.1/fixture/repo.git", "sha": "0" * 40,
                                        "before_sha": "0" * 40, "ref": "main", "ref_type": "branch"},
                           "variables": [{"key": "GIT_STRATEGY", "value": "none", "public": True, "internal": False, "file": False, "masked": False}],
                           "steps": [{"name": "script", "script": [f"printf %s {marker} > {marker_path}"],
                                      "timeout": 30, "when": "on_success", "allow_failure": False}],
                           "artifacts": [], "cache": [], "credentials": [], "dependencies": [], "features": {}}
                    self.send(201, json.dumps(job).encode(), {"Content-Type": "application/json"})

                def do_PATCH(self):
                    payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    with lock: state["paths"].append(("PATCH", self.path))
                    if self.path.split("?", 1)[0] != "/api/v4/jobs/1001/trace": return self.send(404)
                    with lock:
                        state["trace"].extend(payload)
                        last = len(state["trace"]) - 1
                    self.send(202, headers={"Range": f"0-{last}", "Job-Status": "running"})

                def do_PUT(self):
                    payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    with lock: state["paths"].append(("PUT", self.path))
                    if self.path != "/api/v4/jobs/1001": return self.send(404)
                    try: update = json.loads(payload)
                    except ValueError: return self.send(400)
                    with lock: state["updates"].append(update.get("state"))
                    self.send(200, b'{}', {"Content-Type": "application/json"})

            server = ThreadingHTTPServer(("127.0.0.1", 0), Coordinator)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/"
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LC_ALL": "C", "CI_SERVER_URL": url,
                   "CI_SERVER_TOKEN": "fixture-runner-token", "RUNNER_EXECUTOR": "shell", "RUNNER_BUILDS_DIR": str(root / "builds")}
            log = (root / "runner.log").open("w+b")
            process = subprocess.Popen([str(binary), "run-single", "--shell", "bash"], cwd=root,
                                       env=env, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 50
                while time.monotonic() < deadline:
                    with lock: finished = any(item in {"success", "failed"} for item in state["updates"])
                    if finished: break
                    if process.poll() is not None: break
                    time.sleep(0.2)
                if not marker_path.is_file():
                    log.flush(); log.seek(0)
                    output = log.read(65536).decode("utf-8", errors="replace")
                    for value in (str(root), marker, socket.gethostname(), "fixture-runner-token", "fixture-job-token"):
                        output = output.replace(value, "[fixture]")
                    with lock:
                        observed = list(state["paths"]), list(state["updates"])
                        trace = state["trace"].decode("utf-8", errors="replace")
                    for value in (str(root), marker, socket.gethostname(), "fixture-runner-token", "fixture-job-token"):
                        trace = trace.replace(value, "[fixture]")
                    self.fail(f"Runner did not create the controlled marker; observed={observed}; fixture log: {output[-6000:]}; trace: {trace[-4000:]}")
                self.assertEqual(marker_path.read_text(), marker)
                with lock:
                    self.assertIn("success", state["updates"], "Runner did not report a successful job")
                    self.assertTrue(state["trace"], "Runner did not upload a job trace")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=8)
                log.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
