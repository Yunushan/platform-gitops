#!/usr/bin/env python3
"""Opt-in Runner Kubernetes execution on original isolated K3s VM workers."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid

import yaml

from test_gitlab_runner_execution_fixture import RUNNER_SHA256
from test_k3s_vm_fixture import PROBE_IMAGE
from test_tekton_worker_runtime_fixture import verify_archive

HELPER = "registry.gitlab.com/gitlab-org/gitlab-runner/gitlab-runner-helper@sha256:6a21bd8f1971c7e7ab63feb450b1d72dc97ad55655246f9a2642f8687018327c"


def exercise(test, context):
    runner = Path(os.environ["GITLAB_RUNNER_TEST_BINARY"]).resolve(strict=True)
    archive = Path(os.environ["GITLAB_RUNNER_HELPER_OCI"]).resolve(strict=True)
    if runner.is_symlink() or not runner.is_file() or hashlib.sha256(runner.read_bytes()).hexdigest() != RUNNER_SHA256:
        raise ValueError("Require the reviewed Runner binary")
    helper_sha = verify_archive(archive, HELPER)
    docs = [d for d in yaml.safe_load_all(Path(os.environ["GITLAB_RUNNER_KUSTOMIZE"]).read_text(encoding="utf-8")) if isinstance(d, dict)]
    client = context["client"]
    for obj in [d for d in docs if d["kind"] == "Namespace"] + [d for d in docs if d["kind"] in {"ServiceAccount", "Role", "RoleBinding", "NetworkPolicy"}]:
        client.call(["create", "-f", "-"], payload=json.dumps(obj).encode())
    for node in context["nodes"][1:]:
        destination = "/tmp/fixture-image-" + HELPER.rsplit("sha256:", 1)[1] + ".tar"
        context["upload"](node, archive, destination)
        context["ssh"](node, "sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import --digests " + destination, seconds=120)
        context["ssh"](node, "rm -- " + destination)
    request = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest", "spec": {"expirationSeconds": 600}}
    token = json.loads(client.call(["create", "--raw", "/api/v1/namespaces/gitlab-runner/serviceaccounts/gitlab-runner/token", "-f", "-"], payload=json.dumps(request).encode()))["status"]["token"]
    admin_config = json.loads(Path(os.environ[context["target"]["kubeconfig_env"]]).read_text(encoding="utf-8"))
    admin_config["users"][0]["user"] = {"token": token}
    template = next(d["data"]["config.template.toml"] for d in docs if d["kind"] == "ConfigMap" and "config.template.toml" in d.get("data", {}))
    if 'image = "alpine:3.22"' not in template: raise ValueError("Unexpected runner template")
    template = template.replace('image = "alpine:3.22"', f'image = "{PROBE_IMAGE}"\n    helper_image = "{HELPER}"')
    marker = uuid.uuid4().hex
    state = {"offered": False, "updates": [], "trace": bytearray()}; lock = threading.Lock()
    repo_scope = tempfile.TemporaryDirectory(prefix="gitlab-runner-repo-", dir="/tmp")
    repo_root = Path(repo_scope.name); repo_root.chmod(0o755)
    worktree = repo_root / "work"; worktree.mkdir()
    def git(*args):
        return subprocess.run(["/usr/bin/git", *args], cwd=worktree, env={"PATH": "/usr/bin:/bin", "HOME": str(repo_root), "LC_ALL": "C"},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=True).stdout.strip()
    git("init", "--initial-branch=main")
    (worktree / "fixture.txt").write_text(marker + "\n", encoding="utf-8")
    git("add", "fixture.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    git("clone", "--bare", str(worktree), str(repo_root / "fixture.git"))
    for directory, subdirs, files in os.walk(repo_root / "fixture.git"):
        os.chown(directory, 65534, 65534)
        os.chmod(directory, 0o755)
        for name in files:
            os.chown(Path(directory) / name, 65534, 65534)
            os.chmod(Path(directory) / name, 0o644)
    if context.get("cilium_runtime"):
        git_port = 9418
    else:
        with socket.socket() as reserve:
            reserve.bind(("192.0.2.1", 0)); git_port = reserve.getsockname()[1]
    git_log = (repo_root / "git-daemon.log").open("w+b")
    daemon = subprocess.Popen(["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/git", "daemon",
                               "--reuseaddr", "--export-all", "--base-path=" + str(repo_root), "--listen=192.0.2.1", "--port=" + str(git_port), str(repo_root)],
                              stdin=subprocess.DEVNULL, stdout=git_log, stderr=subprocess.STDOUT, start_new_session=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LC_ALL": "C"})

    class RejectedPort(socketserver.BaseRequestHandler):
        def handle(self): self.request.sendall(b"fixture-unapproved-port\n")

    class ReusableTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    blocked = None; blocked_thread = None
    if context.get("cilium_runtime"):
        blocked = ReusableTCPServer(("192.0.2.1", 19000), RejectedPort)
        blocked_thread = threading.Thread(target=blocked.serve_forever, daemon=True); blocked_thread.start()
        with socket.create_connection(("192.0.2.1", 19000), timeout=3) as connection:
            test.assertEqual(connection.recv(100), b"fixture-unapproved-port\n")

    class Coordinator(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def reply(self, code, data=b"", headers=None):
            self.send_response(code)
            for key, value in (headers or {}).items(): self.send_header(key, value)
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            if data: self.wfile.write(data)
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path != "/api/v4/jobs/request": return self.reply(404)
            with lock:
                if state["offered"]: return self.reply(204)
                state["offered"] = True
            job = {"id": 1001, "token": "fixture-job-token", "allow_git_fetch": False, "runner_info": {"timeout": 180},
                   "job_info": {"name": "fixture", "stage": "test", "project_id": 1, "project_name": "fixture"},
                   "git_info": {"repo_url": f"git://192.0.2.1:{git_port}/fixture.git", "sha": commit, "before_sha": "0" * 40,
                                "ref": "main", "ref_type": "branch", "refspecs": ["+refs/heads/main:refs/remotes/origin/main"]},
                   "variables": [{"key": "GIT_STRATEGY", "value": "clone", "public": True}],
                   "steps": [{"name": "script", "script": ["test \"$(cat fixture.txt)\" = " + marker,
                       *(["test -z \"$(nc -w 3 192.0.2.1 19000 </dev/null || true)\""] if blocked else []),
                       "echo " + marker, "sleep 10"], "timeout": 60, "when": "on_success", "allow_failure": False}],
                   "artifacts": [], "cache": [], "credentials": [], "dependencies": [], "features": {}}
            self.reply(201, json.dumps(job).encode(), {"Content-Type": "application/json"})
        def do_PUT(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            with lock: state["updates"].append(data.get("state"))
            self.reply(200, b"{}")
        def do_PATCH(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                state["trace"].extend(data); end = len(state["trace"]) - 1
            self.reply(202, headers={"Range": f"0-{end}", "Job-Status": "running"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Coordinator); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            if daemon.poll() is not None: raise ValueError("Synthetic Git daemon exited before readiness")
            try:
                with socket.create_connection(("192.0.2.1", git_port), timeout=1): break
            except OSError:
                if time.monotonic() >= deadline: raise TimeoutError("Synthetic Git daemon did not start")
                time.sleep(0.1)
        with tempfile.TemporaryDirectory(prefix="gitlab-runner-worker-", dir="/tmp") as temporary:
            root = Path(temporary); os.chown(root, 65534, 65534); root.chmod(0o700)
            binary = root / "gitlab-runner"; shutil.copyfile(runner, binary); os.chown(binary, 65534, 65534); binary.chmod(0o700)
            config = root / "kubeconfig.json"; config.write_text(json.dumps(admin_config)); os.chown(config, 65534, 65534); config.chmod(0o600)
            runner_auth = secrets.token_urlsafe(24)
            text = "concurrent = 1\ncheck_interval = 1\n" + template.replace("[[runners]]", f'[[runners]]\n  url = "http://127.0.0.1:{server.server_port}/"\n  token = "{runner_auth}"\n  executor = "kubernetes"', 1)
            config_toml = root / "config.toml"; config_toml.write_text(text); os.chown(config_toml, 65534, 65534); config_toml.chmod(0o600)
            log = (root / "runner.log").open("w+b")
            process = subprocess.Popen(["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", str(binary), "run", "--config", str(config_toml)],
                cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root), "KUBECONFIG": str(config), "LC_ALL": "C"},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 180; observed = None; last_status = None
                while time.monotonic() < deadline:
                    rows = json.loads(client.call(["get", "pods", "-n", "gitlab-jobs", "-o", "json"]))["items"]
                    for pod in rows:
                        last_status = pod.get("status", {})
                        if pod["status"].get("phase") == "Running": observed = pod
                    with lock: done = "success" in state["updates"]; failed = "failed" in state["updates"]
                    if done or failed or process.poll() is not None: break
                    time.sleep(1)
                with lock: success, trace = "success" in state["updates"], bytes(state["trace"])
                if not success:
                    log.flush(); log.seek(0); raw = log.read(65536).decode("utf-8", errors="replace")
                    diagnostics = "trace=" + trace[-3500:].decode("utf-8", errors="replace")
                    for value in (token, str(root), marker, socket.gethostname(), runner_auth, "fixture-job-token"):
                        diagnostics = diagnostics.replace(value, "[fixture]")
                    raise ValueError("Disposable Runner job failed: " + diagnostics)
                test.assertIsNotNone(observed); test.assertIn(marker.encode(), trace)
                test.assertEqual(observed["spec"]["serviceAccountName"], "gitlab-job")
                test.assertFalse(observed["spec"]["automountServiceAccountToken"])
                test.assertIn(observed["spec"]["nodeName"], {n["name"] for n in context["nodes"][1:]})
                test.assertTrue(all(not c.get("securityContext", {}).get("privileged", False) for c in observed["spec"]["containers"]))
                if blocked:
                    with socket.create_connection(("192.0.2.1", 19000), timeout=3) as connection:
                        test.assertEqual(connection.recv(100), b"fixture-unapproved-port\n")
                return {"gitlab_runner_real_kubernetes_job_verified": True, "runner_sha256": RUNNER_SHA256,
                        "helper_image": HELPER, "helper_archive_sha256": helper_sha,
                        "real_gitlab_server_verified": False, "cilium_policy_verified": False,
                        "gitlab_job_git_port_allowed_and_unapproved_port_denied": bool(blocked),
                        "synthetic_repository_checkout_verified": True, "repository_checkout_from_gitlab_verified": False,
                        "gitops_handoff_verified": False}
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=8)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=8)
                log.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        if blocked:
            blocked.shutdown(); blocked.server_close(); blocked_thread.join(timeout=5)
        if daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGTERM)
            try: daemon.wait(timeout=8)
            except subprocess.TimeoutExpired: os.killpg(daemon.pid, signal.SIGKILL); daemon.wait(timeout=8)
        git_log.close(); repo_scope.cleanup()
