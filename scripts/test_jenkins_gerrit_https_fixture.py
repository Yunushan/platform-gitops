#!/usr/bin/env python3
"""Optional loopback TLS test for separate Jenkins and Gerrit credentials."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs

import trigger_jenkins_gerrit as trigger
import verify_jenkins_gerrit as verifier
import report_jenkins_gerrit as result_reporter


@unittest.skipUnless(os.name == "posix" and shutil.which("openssl") and os.environ.get("JENKINS_GERRIT_HTTPS_FIXTURE") == "1",
                     "disposable HTTPS fixture not requested")
class JenkinsGerritHTTPS(unittest.TestCase):
    def test_reporter_cli_preflight_publish_and_retry_over_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cert = root / "ca.crt"; key = root / "tls.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                            "-keyout", str(key), "-out", str(cert)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=True)
            config = b"reviewed-job-config"; revision = "a" * 40
            label = {"name": "Verified", "default_value": 0, "values": {"-1": "Failed", " 0": "None", "+1": "Passed"}}
            change = {"project": "team/project", "branch": "main", "status": "NEW", "_number": 123,
                      "current_revision": revision, "revisions": {revision: {"_number": 2, "ref": "refs/changes/23/123/2"}},
                      "messages": [], "labels": {"Verified": {"all": []}}}
            credentials = {"jenkins": ("reader", "synthetic-jenkins-token"), "gerrit": ("gerrit-read", "synthetic-gerrit-token"),
                           "reviewer": ("reviewbot", "synthetic-review-token")}
            writes = []
            class Handler(BaseHTTPRequestHandler):
                origin = ""
                def log_message(self, *args): pass
                def respond(self):
                    kind = self.server.kind
                    if kind == "gerrit" and self.path in {"/a/accounts/self", "/a/projects/team%2Fproject/labels/?inherited",
                                                         "/a/changes/123/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES",
                                                         "/a/changes/123/revisions/" + revision + "/review"}:
                        kind = "reviewer"
                    u, auth = credentials[kind]
                    if self.headers.get("Authorization") != "Basic " + base64.b64encode((u + ":" + auth).encode()).decode():
                        self.send_error(403); return
                    if self.command == "POST":
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        writes.append(payload)
                        change["messages"] = [{"tag": payload["tag"], "author": {"_account_id": 42}, "_revision_number": 2,
                                               "id": "fixture", "message": payload["message"]}]
                        change["labels"]["Verified"]["all"] = [{"_account_id": 42, "value": 1}]
                        body = {}
                    elif kind == "jenkins" and self.path.endswith("/config.xml"):
                        body = config
                    elif kind == "jenkins" and "/api/json?tree=builds[" in self.path:
                        body = {"builds": [{"number": 10, "queueId": 17, "building": False, "result": "SUCCESS"}]}
                    elif kind == "jenkins" and "/10/api/json?" in self.path:
                        body = {"number": 10, "queueId": 17, "building": False, "result": "SUCCESS", "actions": [
                            {"parameters": [{"name": "GERRIT_CHANGE_NUMBER", "value": "123"},
                                            {"name": "GERRIT_PATCHSET_NUMBER", "value": "2"},
                                            {"name": "GERRIT_PATCHSET_REVISION", "value": revision}]},
                            {"lastBuiltRevision": {"SHA1": revision}, "remoteUrls": [self.origin + "/a/team/project.git"]}]}
                    elif self.path == "/a/accounts/self": body = {"_account_id": 42, "username": "reviewbot"}
                    elif "/labels/?inherited" in self.path: body = [label]
                    elif self.path.startswith("/a/changes/123"): body = change
                    elif "/branches/main" in self.path:
                        body = {"ref": "refs/heads/main", "revision": revision if change["status"] == "MERGED" else "b" * 40}
                    else: self.send_error(404); return
                    if not isinstance(body, bytes):
                        body = (b")]}'\n" if self.server.kind == "gerrit" else b"") + json.dumps(body).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                do_GET = respond
                do_POST = respond
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert, key)
            servers = []; threads = []
            try:
                for kind in ("jenkins", "gerrit"):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.kind = kind
                    server.socket = tls.wrap_socket(server.socket, server_side=True)
                    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                    servers.append(server); threads.append(thread)
                Handler.origin = "https://localhost:" + str(servers[1].server_port)
                trigger_plan = {"schema_version": 1, "jenkins_origin": "https://localhost:" + str(servers[0].server_port),
                                "job": ["team", "build"], "job_config_sha256": hashlib.sha256(config).hexdigest(),
                                "gerrit_origin": Handler.origin, "project": "team/project", "branch": "main",
                                "change": 123, "patchset": 2, "revision": revision}
                plan = {"schema_version": 1, "trigger": trigger_plan, "reviewer_username": "reviewbot", "reviewer_account_id": 42,
                        "label_definition_sha256": result_reporter.reporter.fingerprint(label), "trust_jenkins_controller": True}
                plan_path = root / "plan.json"; plan_path.write_text(json.dumps(plan))
                journal = root / "queue.json"; journal.write_text(json.dumps({"schema_version": 2, "state": "queued",
                    "identity_sha256": trigger.reservation_key(trigger_plan), "job_config_sha256": trigger_plan["job_config_sha256"], "queue_id": 17}))
                private = root / "private"; private.mkdir(mode=0o700); reviews = private / "reviews"; reviews.mkdir(mode=0o700)
                for name, (user, token) in credentials.items():
                    for suffix, value in (("user", user), ("token", token)):
                        p = root / (name + "-" + suffix); p.write_text(value); p.chmod(0o600)
                cert.chmod(0o600)
                args = ["report", "--plan", str(plan_path), "--queue-journal", str(journal),
                        "--review-journal-dir", str(reviews), "--input-dir", str(root)]
                with patch.object(trigger, "ROOT", root), patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                    for extra, expected in (([], "ready"), (["--apply"], "reported"), (["--apply"], "already-reported")):
                        output = io.StringIO()
                        with patch.object(sys, "argv", args + extra), contextlib.redirect_stdout(output):
                            self.assertEqual(result_reporter.main(), 0)
                        self.assertEqual(json.loads(output.getvalue())["status"], expected)
                        self.assertNotIn("synthetic-", output.getvalue())
                    change["status"] = "MERGED"
                    output = io.StringIO()
                    with patch.object(sys, "argv", args + ["--verify-merged"]), contextlib.redirect_stdout(output):
                        self.assertEqual(result_reporter.main(), 0)
                    self.assertEqual(json.loads(output.getvalue())["status"], "merged-evidence-matched")
                    self.assertFalse(json.loads(output.getvalue())["deployment_authorized"])
                self.assertEqual(len(writes), 1)
            finally:
                for server in servers: server.shutdown(); server.server_close()
                for thread in threads: thread.join(timeout=5)

    def test_reporter_transport_scopes_credentials_and_refuses_redirects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / "ca.crt", root / "tls.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                            "-keyout", str(key), "-out", str(cert)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
            seen = []
            class Handler(BaseHTTPRequestHandler):
                redirect = False
                def log_message(self, *args): pass
                def respond(self):
                    seen.append((self.command, self.path))
                    expected = "Basic " + base64.b64encode(b"reviewbot:synthetic-review-token").decode()
                    if self.headers.get("Authorization") != expected:
                        self.send_error(403); return
                    if self.redirect:
                        self.send_response(302); self.send_header("Location", "/leaked-credential"); self.end_headers(); return
                    if self.command == "POST":
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        if payload.get("labels") != {"Verified": 1}: self.send_error(400); return
                    body = b")]}'\n{}"
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                do_GET = respond
                do_POST = respond
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                plan = {"reviewer_username": "reviewbot", "trigger": {"project": "team/project", "change": 123,
                        "revision": "a" * 40, "gerrit_origin": "https://localhost:" + str(server.server_port)}}
                api = result_reporter.ReviewAPI(plan, b"synthetic-review-token")
                payload = {"message": "fixture", "tag": "autogenerated:fixture", "labels": {"Verified": 1},
                           "notify": "NONE", "drafts": "KEEP", "omit_duplicate_comments": True,
                           "ignore_automatic_attention_set_rules": True}
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                    self.assertEqual(api.request("/a/accounts/self"), {})
                    self.assertEqual(api.request("/a/changes/123/revisions/" + "a" * 40 + "/review", payload), {})
                    Handler.redirect = True
                    with self.assertRaises(Exception): api.request("/a/accounts/self")
                self.assertFalse(any(path == "/leaked-credential" for _, path in seen))
                self.assertEqual(sum(method == "POST" for method, _ in seen), 1)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_separate_credentials_gerrit_prefix_and_redirect_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / "ca.crt", root / "tls.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
            config = b"<project><description>fixture</description></project>"
            revision = "a" * 40
            credentials = {"jenkins": ("reader", "synthetic-jenkins-token"), "gerrit": ("reviewer", "synthetic-gerrit-token")}
            seen = []
            class Handler(BaseHTTPRequestHandler):
                redirect = False
                gerrit_origin = ""
                jenkins_origin = ""
                change_status = "MERGED"
                def log_message(self, *args): pass
                def do_POST(self):
                    seen.append((self.server.provider, self.path))
                    user, auth = credentials["jenkins"]
                    expected = "Basic " + base64.b64encode((user + ":" + auth).encode()).decode()
                    if self.server.provider != "jenkins" or self.headers.get("Authorization") != expected:
                        self.send_error(403); return
                    size = int(self.headers.get("Content-Length", "0"))
                    fields = parse_qs(self.rfile.read(size).decode())
                    if self.path != "/job/team/job/build/buildWithParameters" or fields.get("GERRIT_REFSPEC") != ["refs/changes/23/123/2"]:
                        self.send_error(400); return
                    self.send_response(201); self.send_header("Location", self.jenkins_origin + "/queue/item/17/")
                    self.send_header("Content-Length", "0"); self.end_headers()
                def do_GET(self):
                    provider = self.server.provider
                    seen.append((provider, self.path))
                    user, auth = credentials[provider]
                    expected = "Basic " + base64.b64encode((user + ":" + auth).encode()).decode()
                    if self.headers.get("Authorization") != expected: self.send_error(403); return
                    if self.redirect:
                        self.send_response(302); self.send_header("Location", "/leaked-credential"); self.end_headers(); return
                    if provider == "jenkins" and self.path == "/job/team/job/build/config.xml":
                        body = config
                    elif provider == "jenkins" and self.path.startswith("/job/team/job/build/10/api/json?"):
                        body = json.dumps({"number": 10, "building": False, "result": "SUCCESS", "actions": [
                            {"parameters": [{"name": "GERRIT_CHANGE_NUMBER", "value": "123"}, {"name": "GERRIT_PATCHSET_NUMBER", "value": "2"},
                                            {"name": "GERRIT_PATCHSET_REVISION", "value": revision}]},
                            {"lastBuiltRevision": {"SHA1": revision}, "remoteUrls": [self.gerrit_origin + "/a/team/project.git"]}]}).encode()
                    elif provider == "gerrit" and self.path == "/a/changes/123?o=CURRENT_REVISION":
                        body = b")]}'\n" + json.dumps({"project": "team/project", "branch": "main", "status": self.change_status, "_number": 123,
                            "current_revision": revision, "revisions": {revision: {"_number": 2, "ref": "refs/changes/23/123/2"}}}).encode()
                    elif provider == "gerrit" and self.path == "/a/projects/team%2Fproject/branches/main":
                        body = b")]}'\n" + json.dumps({"ref": "refs/heads/main", "revision": revision}).encode()
                    else: self.send_error(404); return
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
            servers, threads = [], []
            try:
                for provider in ("jenkins", "gerrit"):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.provider = provider
                    server.socket = context.wrap_socket(server.socket, server_side=True)
                    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                    servers.append(server); threads.append(thread)
                jenkins_origin = "https://localhost:" + str(servers[0].server_port)
                Handler.jenkins_origin = jenkins_origin
                Handler.gerrit_origin = "https://localhost:" + str(servers[1].server_port)
                plan = {"schema_version": 1, "jenkins_origin": jenkins_origin, "job": ["team", "build"], "build": 10,
                        "job_config_sha256": hashlib.sha256(config).hexdigest(), "gerrit_origin": Handler.gerrit_origin,
                        "project": "team/project", "branch": "main", "change": 123, "patchset": 2, "revision": revision}
                plan_path = root / "plan.json"; plan_path.write_text(json.dumps(plan), encoding="utf-8")
                for provider, (user, token) in credentials.items():
                    for suffix, value in (("user", user), ("token", token)):
                        path = root / (provider + "-" + suffix); path.write_text(value, encoding="ascii"); path.chmod(0o600)
                cert.chmod(0o600)
                env = {"PATH": "/usr/bin:/bin", "HOME": directory, "LANG": "C.UTF-8"}
                command = [sys.executable, "-B", str(Path(__file__).with_name("verify_jenkins_gerrit.py")), "--plan", str(plan_path), "--input-dir", str(root)]
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, check=True)
                self.assertTrue(json.loads(result.stdout)["jenkins_build_and_gerrit_match"])
                self.assertNotIn("synthetic-", result.stdout + result.stderr)
                Handler.change_status = "NEW"
                trigger_plan = {k: v for k, v in plan.items() if k != "build"}
                private = root / "private"; private.mkdir(mode=0o700)
                with patch.object(trigger, "ROOT", root), patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                    os.environ.pop("SSLKEYLOGFILE", None)
                    jget = verifier.requester(jenkins_origin, credentials["jenkins"][0], credentials["jenkins"][1].encode())
                    gget = verifier.requester(Handler.gerrit_origin, credentials["gerrit"][0], credentials["gerrit"][1].encode(), prefix=True)
                    post = trigger.post_requester(jenkins_origin, credentials["jenkins"][0], credentials["jenkins"][1].encode())
                    queued = trigger.submit(trigger_plan, jget, gget, post, private, apply=True)
                    self.assertEqual(queued["queue_id"], 17)
                    with self.assertRaises(FileExistsError): trigger.submit(trigger_plan, jget, gget, post, private, apply=True)
                    self.assertEqual(sum(path.endswith("buildWithParameters") for _, path in seen), 1)
                Handler.redirect = True
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("synthetic-", result.stdout + result.stderr)
                self.assertFalse(any(path == "/leaked-credential" for _, path in seen))
            finally:
                for server in servers: server.shutdown(); server.server_close()
                for thread in threads: thread.join(timeout=5)


if __name__ == "__main__": unittest.main()
