#!/usr/bin/env python3
"""Exercise the verifier over disposable authenticated TLS, without GitLab credentials."""
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import verify_gitlab_pipeline as verifier


@unittest.skipUnless(shutil.which("openssl") and os.environ.get("GITLAB_PIPELINE_HTTPS_FIXTURE") == "1",
                     "Set GITLAB_PIPELINE_HTTPS_FIXTURE=1 and install OpenSSL for the disposable TLS fixture")
class GitLabTLSFixture(unittest.TestCase):
    def test_authenticated_https_and_redirect_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / "fixture.crt", root / "fixture.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                            "-keyout", str(key), "-out", str(cert)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            sha = "a" * 40
            token = secrets.token_urlsafe(24)
            requested = []
            class Server(BaseHTTPRequestHandler):
                redirect = False
                def log_message(self, *args): pass
                def do_GET(self):
                    requested.append(self.path)
                    if self.headers.get("PRIVATE-TOKEN") != token:
                        self.send_error(403)
                        return
                    if self.redirect:
                        self.send_response(302)
                        self.send_header("Location", "/stolen-token")
                        self.end_headers()
                        return
                    prefix = "/api/v4/projects/7"
                    rows = {prefix: {"id": 7, "path_with_namespace": "group/project"},
                            prefix + "/repository/branches/main": {"name": "main", "protected": True, "commit": {"id": sha}},
                            prefix + "/pipelines/23": {"id": 23, "project_id": 7, "sha": sha, "ref": "main", "status": "success", "source": "push", "updated_at": "fixture"},
                            prefix + "/pipelines/23/jobs?per_page=100&page=1&include_retried=false": [{"id": 31, "name": "build", "status": "success", "allow_failure": False}],
                            prefix + "/jobs/31": {"id": 31, "name": "build", "status": "success", "allow_failure": False, "pipeline": {"id": 23}, "commit": {"id": sha}}}
                    if self.path not in rows:
                        self.send_error(404)
                        return
                    body = json.dumps(rows[self.path]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Next-Page", "")
                    self.end_headers()
                    self.wfile.write(body)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            with ThreadingHTTPServer(("127.0.0.1", 0), Server) as server:
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    plan = {"schema_version": 1, "api_url": f"https://localhost:{server.server_port}/api/v4", "project_id": 7,
                            "project_path": "group/project", "branch": "main", "pipeline_id": 23, "revision": sha, "required_jobs": ["build"]}
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                        os.environ.pop("SSLKEYLOGFILE", None)
                        self.assertTrue(verifier.verify(plan, verifier.requester(plan, token))["verified"])
                        Server.redirect = True
                        with self.assertRaises(Exception) as failure: verifier.verify(plan, verifier.requester(plan, token))
                        if hasattr(failure.exception, "close"): failure.exception.close()
                    self.assertNotIn("/stolen-token", requested)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
