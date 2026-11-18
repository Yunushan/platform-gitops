import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

import gitlab_application_entry as entry
from render_gitlab_repository_hosts import render
from test_gitlab_repository_hosts import replicated_application


def plan():
    return dict(replicated_application(), schema_version=4, application_vip={
        "vip": "192.0.2.40/24", "vrid": 83, "health_port": 8404,
        "entry_hosts": [
            {"name": "entry-a", "address": "192.0.2.41", "interface": "eth0", "failure_domain": "rack-a"},
            {"name": "entry-b", "address": "192.0.2.42", "interface": "eth0", "failure_domain": "rack-b"},
        ],
        "client_cidrs": ["192.0.2.0/24"],
    })


class ApplicationEntryTest(unittest.TestCase):
    def test_opt_in_entry_is_health_gated_without_requiring_local_vip_to_start(self):
        for runtime in ("raw", "docker"):
            files = render(plan(), runtime)
            config = files["gitlab-application-haproxy.cfg"]
            self.assertIn("bind :9443 ssl", config)
            self.assertIn("bind :2222", config)
            self.assertEqual(config.count("tcp-request connection reject unless { src -m ip 192.0.2.0/24 }"), 2)
            self.assertIn("if { nbsrv(gitlab_https) gt 0 } { nbsrv(gitlab_ssh) gt 0 }", config)
            self.assertEqual(config.count("init-state fully-down"), 2)
            self.assertIn("verify required ca-file", config)
            self.assertIn("send-proxy-v2 check", config)
            for name in ("entry-a", "entry-b"):
                keep = files[f"keepalived-{name}.conf"]
                self.assertIn("    init_fail", keep)
                self.assertIn("    nopreempt", keep)
                self.assertIn("    version 3", keep)
                self.assertIn("--identity " + entry.identity(plan()), keep)
            self.assertIn("User=haproxy", files["gitlab-entry-haproxy.service"])
            self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SETGID", files["gitlab-entry-vrrp@.service"])
            self.assertFalse(json.loads(files["render-status.json"])["ha_verified"])

    def test_rejects_ambiguous_peer_and_client_contracts(self):
        mutations = (
            lambda p: p.update(schema_version=3),
            lambda p: p["application_vip"].update(vip="192.0.2.50/24"),
            lambda p: p["application_vip"].update(vip="192.0.2.40/32"),
            lambda p: p["application_vip"].update(vip=4),
            lambda p: p["application_vip"].update(vrid=True),
            lambda p: p["application_vip"].update(health_port=9443),
            lambda p: p["application_vip"]["entry_hosts"].pop(),
            lambda p: p["application_vip"]["entry_hosts"][0].update(address="192.0.3.41"),
            lambda p: p["application_vip"]["entry_hosts"][0].update(interface="eth0\nunsafe"),
            lambda p: p["application_vip"]["entry_hosts"][1].update(failure_domain="rack-a"),
            lambda p: p["application_vip"]["entry_hosts"].reverse(),
            lambda p: p["application_vip"].update(client_cidrs=[]),
            lambda p: p["application_vip"].update(client_cidrs=[{}]),
            lambda p: p["application_vip"].update(client_cidrs=["192.0.2.1/24"]),
            lambda p: p["application_vip"].update(client_cidrs=["::/0"]),
            lambda p: p["application_vip"].update(client_cidrs=["192.0.2.0/24", "192.0.2.0/24"]),
        )
        for number, mutate in enumerate(mutations):
            candidate = copy.deepcopy(plan())
            mutate(candidate)
            with self.subTest(case=number), self.assertRaises(ValueError):
                render(candidate, "raw")

    def test_delivered_isolated_health_helper_refuses_stale_identity_and_unready_listener(self):
        expected = entry.identity(plan())
        state = {"body": "ready-" + expected, "status": 200}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(state["status"])
                body = state["body"].encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with HTTPServer(("127.0.0.1", 0), Handler) as server, TemporaryDirectory() as temporary:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                helper = Path(temporary) / "health.py"
                helper.write_text(render(plan(), "raw")["gitlab-entry-health.py"], encoding="utf-8")
                args = [sys.executable, "-I", "-B", str(helper), "--port", str(server.server_port), "--identity", expected]
                result = subprocess.run(args, timeout=5, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                state["body"] = "ready-" + "0" * 64
                self.assertEqual(subprocess.run(args, timeout=5, capture_output=True).returncode, 1)
                state.update(body="ready-" + expected, status=503)
                self.assertEqual(subprocess.run(args, timeout=5, capture_output=True).returncode, 1)
            finally:
                server.shutdown()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
