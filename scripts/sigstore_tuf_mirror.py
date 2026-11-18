#!/usr/bin/env python3
"""Serve a pinned public TUF bootstrap snapshot over TLS; private signing keys stay offline."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile

from atomic_file import atomic_write_bytes
from bounded_file import read_bootstrap_bytes
from create_sigstore_tuf import KEY_HEADER, TARGETS, exact, read_ref
from strict_json import loads_strict_json

FILES = {"root.json", "1.root.json", "targets.json", "snapshot.json", "timestamp.json", *TARGETS}


def inspect(plan):
    exact(plan, ("schema_version", "address", "port", "certificate", "private_key", "files"))
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported mirror plan")
    if not isinstance(plan["address"], str) or str(ipaddress.IPv4Address(plan["address"])) != plan["address"]:
        raise ValueError("Use one explicit canonical IPv4 listener")
    if type(plan["port"]) is not int or not 1024 <= plan["port"] <= 65535: raise ValueError("Use an unprivileged explicit TLS port")
    exact(plan["files"], FILES)
    content = {name: read_ref(ref) for name, ref in plan["files"].items()}
    if any(b"PRIVATE KEY" in value for value in content.values()): raise ValueError("Refuse private material in mirror data")
    if sum(map(len, content.values())) > 2 * 1024 * 1024: raise ValueError("Bound the public mirror snapshot")
    info = Path(plan["private_key"]["path"]).stat()
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Restrict the TLS key to its mirror identity")
    certificate = read_ref(plan["certificate"], maximum=65536); key = read_ref(plan["private_key"], maximum=65536)
    if not certificate.startswith(b"-----BEGIN CERTIFICATE-----") or not key.startswith(KEY_HEADER.rstrip()):
        raise ValueError("Require reviewed TLS certificate/key inputs")
    paths = [v["path"] for v in plan["files"].values()]
    if plan["private_key"]["path"] in paths or plan["certificate"]["path"] in paths: raise ValueError("Keep TLS inputs outside public data")
    return content, certificate, key


def handler(content):
    routes = {"/" + name: content[name] for name in FILES - set(TARGETS)}
    routes.update({"/targets/" + name: content[name] for name in TARGETS})
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        def log_message(self, *args): pass
        def do_GET(self): self.respond(False)
        def do_HEAD(self): self.respond(True)
        def respond(self, head):
            # Exact immutable routes: no query handling, URL decoding, directory
            # listing, filesystem access or private key path can be requested.
            self.connection.settimeout(5)
            raw = routes.get(self.path)
            if raw is None:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head: self.wfile.write(raw)
    return Handler


class BoundedServer(HTTPServer):
    request_queue_size = 16
    def __init__(self, address, selected_handler, context):
        self.context = context
        super().__init__(address, selected_handler)
    def get_request(self):
        sock, address = super().get_request(); sock.settimeout(5)
        try: return self.context.wrap_socket(sock, server_side=True), address
        except Exception:
            sock.close(); raise


def serve(plan):
    if os.name != "posix" or os.geteuid() == 0: raise ValueError("Use a separate non-root mirror identity")
    content, cert, key = inspect(plan)
    with tempfile.TemporaryDirectory(prefix="platform-tuf-tls-") as directory:
        root = Path(directory); root.chmod(0o700)
        for name, raw in (("certificate", cert), ("key", key)):
            atomic_write_bytes(root / name, raw, mode=0o400, replace=False)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(root / "certificate", root / "key")
        with BoundedServer((plan["address"], plan["port"]), handler(content), context) as server:
            server.serve_forever(poll_interval=0.25)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bootstrap_bytes(args.plan, 1024 * 1024, follow_symlinks=False))
        if not args.serve:
            content, _, _ = inspect(plan)
            print(json.dumps({"public_snapshot_verified": True, "files": len(content), "server_started": False})); return 0
        serve(plan); return 0
    except Exception:
        print("Private TUF mirror stopped; certificate paths and private details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
