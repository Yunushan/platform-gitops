"""Opt-in real authenticated TLS Git branch checks; no CI/registry qualification."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

from bounded_subprocess import run_bounded
import source_bound_static_image as binding


def exercise(test, publication=None):
    if (publication is not None and not callable(publication)) or os.name != "posix" or os.geteuid() == 0 and publication is None or not all(shutil.which(name) for name in ("git", "openssl")):
        raise ValueError("Select an explicit non-root Linux Git/TLS fixture")
    with tempfile.TemporaryDirectory(prefix="zuul-static-git-") as temporary:
        root = Path(temporary)
        clean = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        def run(args, payload=None):
            return run_bounded(args, input=payload, text=True, env=clean, timeout=30, output_max_bytes=2 * 1024 ** 2, check=True).stdout.strip()
        cert, key = root / "ca.crt", root / "key.pem"
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=disposable-git-fixture",
            "-addext", "subjectAltName=IP:127.0.0.1", "-addext", "basicConstraints=critical,CA:TRUE", "-keyout", str(key), "-out", str(cert)])
        key.chmod(0o600)
        work, repositories = root / "work", root / "repositories"
        work.mkdir(); (repositories / "applications").mkdir(parents=True)
        run(["git", "init", "--quiet", "--initial-branch=main", str(work)])
        (work / "site").mkdir(); (work / "site/index.html").write_text("exact-source-v1\n")
        run(["git", "-C", str(work), "add", "site"])
        commit = ["git", "-C", str(work), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m"]
        run([*commit, "initial"])
        revision = run(["git", "-C", str(work), "rev-parse", "HEAD"])
        bare = repositories / "applications/example.git"
        run(["git", "clone", "--bare", "--quiet", str(work), str(bare)])
        token = secrets.token_urlsafe(32)
        expected_auth = "Basic " + base64.b64encode(("source-read:" + token).encode()).decode()
        counters = {"authorized": 0, "denied": 0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.serve_git()
            def do_POST(self): self.serve_git()
            def serve_git(self):
                if self.headers.get("Authorization") != expected_auth:
                    counters["denied"] += 1
                    self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="fixture"'); self.end_headers(); return
                url = urlsplit(self.path)
                if url.path not in {"/a/applications/example.git/info/refs", "/a/applications/example.git/git-upload-pack"}:
                    self.send_error(403); return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 256 * 1024:
                    self.send_error(413); return
                environment = {**clean, "GIT_PROJECT_ROOT": str(repositories), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": url.path[2:],
                    "REQUEST_METHOD": self.command, "QUERY_STRING": url.query, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(size), "REMOTE_USER": "source-read", "REMOTE_ADDR": "127.0.0.1",
                    "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
                response = run_bounded(["git", "http-backend"], input=self.rfile.read(size), env=environment,
                    timeout=30, output_max_bytes=2 * 1024 ** 2, check=True).stdout
                head, marker, body = response.partition(b"\r\n\r\n")
                if not marker: head, marker, body = response.partition(b"\n\n")
                if not marker: raise ValueError("Invalid disposable CGI response")
                headers = [line.split(b":", 1) for line in head.splitlines()]
                status = next((int(value.strip().split()[0]) for name, value in headers if name.lower() == b"status"), 200)
                self.send_response(status)
                for name, value in headers:
                    if name.lower() != b"status": self.send_header(name.decode(), value.strip().decode())
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                counters["authorized"] += 1
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key); server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            credentials = root / "inputs"; credentials.mkdir(mode=0o700)
            (credentials / "ca.crt").write_bytes(cert.read_bytes()); (credentials / "token").write_text(token)
            for item in credentials.iterdir(): item.chmod(0o600)
            source = {"schema_version": 2, "clone_url": f"https://127.0.0.1:{server.server_port}/a/applications/example.git",
                "read_username": "source-read", "revision": revision, "branch_ref": "refs/heads/main",
                "ca_sha256": hashlib.sha256(cert.read_bytes()).hexdigest(), "checkout_program_sha256": binding.checkout_program_sha256()}
            proof = root / "proof"; proof.mkdir()
            binding.checkout_with_witness(source, root / "checkout", proof / "checkout.json", credentials)
            test.assertEqual((root / "checkout/site/index.html").read_text(), "exact-source-v1\n")
            test.assertEqual((proof / "checkout.json").read_bytes(), binding.witness_bytes(source))
            published = publication({"source": source, "source_ca": cert.read_bytes(), "source_token": token}) if publication else {}
            (work / "site/index.html").write_text("moved-source-v2\n")
            run(["git", "-C", str(work), "add", "site"]); run([*commit, "advance"])
            run(["git", "--git-dir=" + str(bare), "fetch", "--quiet", str(work), "main:refs/heads/main"])
            second = root / "second-proof"; second.mkdir()
            with test.assertRaises(subprocess.SubprocessError):
                binding.checkout_with_witness(source, root / "stale", second / "checkout.json", credentials)
            test.assertFalse((second / "checkout.json").exists())
            latest = {**source, "revision": run(["git", "-C", str(work), "rev-parse", "HEAD"])}
            (credentials / "token").write_text(secrets.token_urlsafe(32))
            third = root / "third-proof"; third.mkdir()
            with test.assertRaises(subprocess.SubprocessError):
                binding.checkout_with_witness(latest, root / "denied", third / "checkout.json", credentials)
            test.assertFalse((third / "checkout.json").exists())
            test.assertGreater(counters["authorized"], 0); test.assertGreater(counters["denied"], 0)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            test.assertFalse(thread.is_alive())
    return {**published, "git_tls_branch_checkout_verified": True, "moved_branch_refused": True, "wrong_source_credential_refused": True,
            "temporary_scope_removed": True, "actual_zuul_job_executed": False, "harbor_publication_verified": False, "full_stack_verified": False}
