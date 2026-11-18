#!/usr/bin/env python3
"""Run the opt-in actual KVM/K3s worker acceptance fixture."""

import os
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace


class WorkerImageArchiveTests(unittest.TestCase):
    """Adversarial OCI inputs must fail before a worker import is attempted."""

    def archive(self, root, *, extra=False, corrupt=False, missing=False, link=False, flood=False):
        blobs = {}
        def blob(value, media):
            content = json.dumps(value, sort_keys=True).encode()
            digest = hashlib.sha256(content).hexdigest()
            blobs["blobs/sha256/" + digest] = content
            return {"digest": "sha256:" + digest, "size": len(content), "mediaType": media}
        config = blob({"architecture": "amd64", "os": "linux"}, "application/vnd.oci.image.config.v1+json")
        manifest = blob({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json", "config": config, "layers": []}, "application/vnd.oci.image.manifest.v1+json")
        image = "registry.example.invalid/probe@" + manifest["digest"]
        manifest["annotations"] = {"io.containerd.image.name": image, "org.opencontainers.image.ref.name": image}
        files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}', "index.json": json.dumps({"schemaVersion": 2, "manifests": [manifest]}).encode(), **blobs}
        if missing: del files["blobs/sha256/" + config["digest"][7:]]
        if corrupt: files["blobs/sha256/" + config["digest"][7:]] += b" "
        if extra: files["blobs/sha256/" + hashlib.sha256(b"unreferenced").hexdigest()] = b"unreferenced"
        path = root / "fixture.tar"
        with tarfile.open(path, "w") as archive:
            for name, content in files.items():
                item = tarfile.TarInfo(name); item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
            if link:
                item = tarfile.TarInfo("outside"); item.type = tarfile.SYMTYPE; item.linkname = "/etc/passwd"; archive.addfile(item)
            if flood:
                for index in range(81):
                    item = tarfile.TarInfo("extra-" + str(index)); item.size = 1; archive.addfile(item, io.BytesIO(b"x"))
        return path, image

    def test_accepts_original_reachable_linux_image(self):
        from test_tekton_worker_runtime_fixture import verify_archive
        with tempfile.TemporaryDirectory() as temporary:
            path, image = self.archive(Path(temporary))
            self.assertEqual(verify_archive(path, image), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_rejects_corruption_missing_content_links_and_header_flood(self):
        from test_tekton_worker_runtime_fixture import verify_archive
        for option in ("extra", "corrupt", "missing", "link", "flood"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temporary:
                path, image = self.archive(Path(temporary), **{option: True})
                with self.assertRaises(ValueError): verify_archive(path, image)

    def test_rejects_another_selected_image(self):
        from test_tekton_worker_runtime_fixture import verify_archive
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self.archive(Path(temporary))
            with self.assertRaises(ValueError): verify_archive(path, "registry.example.invalid/probe@sha256:" + "f" * 64)

    @unittest.skipUnless(os.name == "posix", "Linux disk-backed VM workspace")
    def test_rejects_ram_backed_or_insufficient_workspace(self):
        from test_k3s_vm_fixture import disk_workspace
        for filesystem, free in (("tmpfs", 64 * 1024 ** 3), ("ext4", 1024 ** 3)):
            with self.subTest(filesystem=filesystem), mock.patch("test_k3s_vm_fixture.run_bounded", return_value=SimpleNamespace(stdout=filesystem)), mock.patch("test_k3s_vm_fixture.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
                with self.assertRaises(ValueError): disk_workspace()

    def test_seven_guest_memory_profile_preserves_hard_cap_and_swap_isolation(self):
        from test_k3s_vm_fixture import native_entry_memory_profile
        expected = {"memory.high": 11776 * 1024 ** 2, "memory.max": 12 * 1024 ** 3, "memory.swap.max": 0}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in expected.items(): (root / name).write_text(str(value))
            self.assertEqual(native_entry_memory_profile(root), expected)
            for name, value in (("memory.high", "max"), ("memory.high", str(11 * 1024 ** 3)),
                    ("memory.high", str(12 * 1024 ** 3)), ("memory.max", str(13 * 1024 ** 3)),
                    ("memory.max", "max"), ("memory.swap.max", "4096")):
                with self.subTest(name=name, value=value):
                    (root / name).write_text(value)
                    with self.assertRaises(ValueError): native_entry_memory_profile(root)
                    (root / name).write_text(str(expected[name]))


class NativeProcessObservationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Observe only an original Linux child process")
    def test_original_process_observation_excludes_arguments_and_rejects_reused_identity(self):
        import subprocess
        import sys
        from test_argocd_reconciliation_fixture import process_sample
        marker = "private-marker-should-never-enter-diagnostics"
        process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(5)", marker], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            starttime = (Path("/proc") / str(process.pid) / "stat").read_text().split(") ", 1)[1].split()[19]
            value = process_sample(process, starttime, "repository", Path(sys.executable))
            self.assertTrue(value["executable_matches"]); self.assertNotIn(marker, json.dumps(value))
            self.assertEqual(value["pid"], process.pid); self.assertEqual(value["starttime"], starttime)
            with self.assertRaises(ValueError): process_sample(process, str(int(starttime) + 1), "repository", Path(sys.executable))
        finally: process.terminate(); process.wait(timeout=3)
        ended = process_sample(process, starttime, "repository", Path(sys.executable))
        self.assertIsNotNone(ended["return_code"]); self.assertNotIn("uid", ended)


class GatewayProbeTLSTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Linux fixture HTTP readiness; Windows sandbox denies loopback client sockets")
    def test_gerrit_rest_readiness_rejects_open_port_error_and_wrong_version(self):
        import http.server
        import threading
        from test_gerrit_review_runtime_fixture import backend_ready
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("Host"), self.headers.get("X-Forwarded-Proto"), self.headers.get("X-Forwarded-Port")))
                status, body = self.server.reply
                if self.headers.get("Host") != "127.0.0.1:43210" or self.headers.get("X-Forwarded-Proto") != "https" or self.headers.get("X-Forwarded-Port") != "43210":
                    status, body = 301, b""
                self.send_response(status); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for status, body, expected in ((503, b"starting", False), (200, b"not-json", False),
                    (200, b")]}'\n\"3.13.9\"", False), (200, b")]}'\n\"3.13.10\"", True)):
                server.reply = status, body
                self.assertEqual(backend_ready(server.server_port, "127.0.0.1:43210"), expected)
            self.assertEqual(requests, [("GET", "/config/server/version", None, "127.0.0.1:43210", "https", "43210")] * 4)
        finally: server.shutdown(); server.server_close(); thread.join(timeout=3)

    @unittest.skipUnless(os.name == "posix", "Actual TLS transport with an uncertain fixture write")
    def test_gerrit_fixture_does_not_retry_a_write_when_acknowledgment_is_lost(self):
        import http.client
        import http.server
        import shutil
        import ssl
        import subprocess
        import threading
        from test_cilium_worker_runtime_fixture import certificates
        from test_gerrit_review_runtime_fixture import API
        if not shutil.which("openssl"): self.skipTest("Explicit local OpenSSL")
        def run(args): return subprocess.run([str(a) for a in args], capture_output=True, check=True, timeout=20)
        committed = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                committed.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.close_connection = True  # Commit once, deliberately lose the response.
            def log_message(self, *args): pass
        with tempfile.TemporaryDirectory() as directory:
            pki = certificates({"root": Path(directory), "run": run}, hostname="localhost")
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(pki / "server.crt", pki / "server.key")
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            api = API("https://localhost:" + str(server.server_port), ssl.create_default_context(cafile=str(pki / "ca.crt")),
                {"synthetic-admin": "synthetic-token"}, {}, request_timeout=60)
            try:
                with self.assertRaises(http.client.RemoteDisconnected): api.call("synthetic-admin", "/a/groups/synthetic", {}, method="PUT")
                self.assertEqual(committed, [b"{}"])
                self.assertEqual(len(api.request_observations), 1)
                self.assertEqual({k: api.request_observations[0][k] for k in ("method", "resource", "completed")},
                    {"method": "PUT", "resource": "groups", "completed": False})
            finally: server.shutdown(); server.server_close(); thread.join(timeout=3)

    @unittest.skipUnless(os.name == "posix", "Local OpenSSL/HTTPS probe regression")
    def test_actual_probe_pki_verifies_strict_tls_and_rejects_wrong_name_or_trust(self):
        import http.server
        import shutil
        import ssl
        import subprocess
        import threading
        from test_cilium_worker_runtime_fixture import BODY, HOST, DirectTLS, certificates
        if not shutil.which("openssl"): self.skipTest("Explicit local OpenSSL")
        def run(args): return subprocess.run([str(a) for a in args], capture_output=True, check=True, timeout=20)
        with tempfile.TemporaryDirectory() as directory:
            pki = certificates({"root": Path(directory), "run": run})
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.send_header("Content-Length", str(len(BODY))); self.end_headers(); self.wfile.write(BODY)
                def log_message(self, *args): pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(pki / "server.crt", pki / "server.key")
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            trusted = ssl.create_default_context(cafile=str(pki / "ca.crt"))
            trusted.verify_flags |= ssl.VERIFY_X509_STRICT
            self.assertTrue(trusted.check_hostname); self.assertEqual(trusted.verify_mode, ssl.CERT_REQUIRED)
            try:
                for hostname, context, expected in ((HOST, trusted, True), ("wrong.example.invalid", trusted, False),
                        (HOST, ssl.create_default_context(), False)):
                    with self.subTest(hostname=hostname, expected=expected):
                        connection = DirectTLS(hostname, server.server_port, context=context, timeout=3); connection.address = "127.0.0.1"
                        try:
                            if expected:
                                connection.request("GET", "/"); response = connection.getresponse()
                                self.assertEqual(response.status, 200); self.assertEqual(response.read(1024), BODY)
                            else:
                                with self.assertRaises(ssl.SSLCertVerificationError): connection.request("GET", "/")
                        finally: connection.close()
            finally: server.shutdown(); server.server_close(); thread.join(timeout=3)


class QuorumIdentityTests(unittest.TestCase):
    def test_native_watch_failure_capture_excludes_arbitrary_log_values(self):
        from test_gerrit_native_watch_fixture import failure_records
        record = {"gerrit_reporter_failure": {"exception_type": "KubernetesOperationError",
            "code_locations": ["tekton_gerrit_reporter.py:236 poll", "/private/token.py:7 secret"],
            "message": "private-password-9384", "kubernetes": {"return_code": 1,
                "signals": {"timeout": True, "private-password-9384": True, "forbidden": "private-password-9384"}}}}
        result = failure_records((json.dumps(record) + "\nnot json\n").encode())
        self.assertEqual(result, [{"exception_type": "KubernetesOperationError",
            "code_locations": ["tekton_gerrit_reporter.py:236 poll"],
            "kubernetes": {"return_code": 1, "signals": {"timeout": True}}}])
        self.assertNotIn("private-password-9384", json.dumps(result))

    def test_time_relay_refuses_parent_clock_privilege_in_any_capability_set(self):
        from test_k3s_time_fixture import no_parent_time_capability
        names = ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")
        status = "\n".join(name + ": 0000000000000000" for name in names)
        self.assertEqual(len(no_parent_time_capability(status)), 5)
        for name in names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                no_parent_time_capability(status.replace(name + ": 0000000000000000", name + ": 0000000002000000"))
        with self.assertRaises(ValueError): no_parent_time_capability("CapEff: 0")

    def test_time_tracking_refuses_other_sources_unsynchronized_state_and_unsettled_clock(self):
        from test_k3s_time_fixture import tracking
        good = "C0000201,192.0.2.1,11,1794104359.948588615,0.000000675,0.000000939,0.000005994,0.166,0.009,1.653,0.000288385,0.000020309,2.0,Normal"
        self.assertEqual(tracking(good)["source"], "192.0.2.1")
        for value in (good.replace("192.0.2.1", "192.0.2.99"), good.replace("C0000201", "C0000209"),
                good.replace(",11,", ",0,"), good.replace(",0.000000675,", ",1.0,"),
                good.replace(",0.000000675,", ",nan,"), good.replace(",Normal", ",Not synchronised"),
                good.replace(",192.0.2.1", "")):
            with self.assertRaises(ValueError): tracking(value)

    def test_control_health_requires_each_running_unit_and_direct_ready_api(self):
        from test_k3s_quorum_fixture import control_services_ready
        nodes = [{"name": "fixture-control" + suffix, "address": address} for suffix, address in (("", "192.0.2.2"), ("-2", "192.0.2.5"), ("-3", "192.0.2.6"))]
        good = "ActiveState=active\nSubState=running\nMainPID=812\nNRestarts=1\nInvocationID=" + "a" * 32 + "\nRestart=on-failure\n"
        client = SimpleNamespace(call=mock.Mock(return_value="ok\n"))
        ssh = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=good))
        context = {"client": client, "ssh": ssh}
        result = control_services_ready(context, nodes)
        self.assertEqual(set(result), {node["name"] for node in nodes})
        self.assertEqual([call.args[0] for call in client.call.call_args_list],
            [["get", "--server=https://" + node["address"] + ":6443", "--raw=/readyz"] for node in nodes])
        for failed in (good.replace("active", "failed"), good.replace("running", "auto-restart"),
                good.replace("812", "0"), good.replace("NRestarts=1", "NRestarts=4"),
                good.replace("a" * 32, "invalid"), good.replace("Restart=on-failure", "Restart=no")):
            client.call.reset_mock()
            ssh.return_value = SimpleNamespace(returncode=0, stdout=failed)
            self.assertFalse(control_services_ready(context, nodes))
            client.call.assert_not_called()
        ssh.return_value = SimpleNamespace(returncode=0, stdout=good)
        client.call.return_value = "not ready"
        self.assertFalse(control_services_ready(context, nodes))

    def test_process_injection_refuses_unbound_process_before_remote_access(self):
        from test_k3s_quorum_fixture import signal_original_control
        ssh = mock.Mock()
        for value in ({"pid": True, "invocation_id": "a" * 32}, {"pid": 1, "invocation_id": "a" * 32},
                {"pid": 812, "invocation_id": "invalid"}):
            with self.assertRaises(ValueError): signal_original_control({"ssh": ssh}, {}, value)
        ssh.assert_not_called()

    def test_native_worker_inventory_accepts_only_original_three_or_five_guests(self):
        from test_gerrit_review_runtime_fixture import worker_inventory
        base = [{"name": name, "address": address} for name, address in (("fixture-control", "192.0.2.2"), ("fixture-worker-1", "192.0.2.3"), ("fixture-worker-2", "192.0.2.4"))]
        self.assertEqual(len(worker_inventory({"nodes": base})), 3)
        controls = [base[0], {"name": "fixture-control-2", "address": "192.0.2.5"}, {"name": "fixture-control-3", "address": "192.0.2.6"}]
        self.assertEqual(len(worker_inventory({"nodes": base, "control_nodes": controls})), 5)
        for bad in (controls[:2], [*controls, controls[-1]], [{"name": "borrowed", "address": "192.0.2.5"}]):
            with self.assertRaises(ValueError): worker_inventory({"nodes": base, "control_nodes": bad})

    def test_rke2_inventory_requires_two_dedicated_original_agents(self):
        import copy
        from test_gerrit_review_runtime_fixture import worker_inventory
        from test_k3s_vm_fixture import application_workers
        nodes = [{"name": name, "address": address, "memory": memory} for name, address, memory in (
            ("fixture-control", "192.0.2.2", 2560), ("fixture-worker-1", "192.0.2.3", 2560),
            ("fixture-worker-2", "192.0.2.4", 2560), ("fixture-agent", "192.0.2.8", 1536),
            ("fixture-agent-2", "192.0.2.9", 1536))]
        context = {"runtime": "rke2", "nodes": nodes, "control_nodes": nodes[:3], "application_workers": nodes[3:]}
        self.assertEqual(worker_inventory(context), nodes)
        self.assertEqual(application_workers(context), nodes[3:])
        for mode in ("borrowed", "extra_memory", "control_worker", "missing", "foreign_network"):
            changed = copy.deepcopy(context)
            if mode == "borrowed": changed["nodes"][-1]["name"] = "borrowed"
            elif mode == "extra_memory": changed["nodes"][-1]["memory"] = 2048
            elif mode == "control_worker": changed["application_workers"] = changed["nodes"][2:4]
            elif mode == "missing": changed["nodes"].pop()
            else: changed["nodes"][-1]["address"] = "198.51.100.9"
            with self.subTest(mode=mode), self.assertRaises(ValueError): worker_inventory(changed)

    def test_second_server_agent_refuses_unbounded_or_implicit_profiles_before_boot(self):
        from test_k3s_vm_fixture import exercise
        callback = mock.Mock()
        for options in ({"server_fixture_extra_agent": True},
                        {"server_fixture_agent": True, "server_fixture_extra_agent": 1},
                        {"server_fixture_agent": True, "server_fixture_extra_agent": True, "first_worker_memory_mib": 3328},
                        {"server_fixture_agent": True, "server_fixture_extra_agent": True, "native_edge_hosts": True}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "fixed five-guest"):
                exercise(self, server_fixture=callback, **options)
        callback.assert_not_called()

    def test_native_edge_inventory_is_separate_exact_and_requires_three_controls(self):
        import copy
        from test_gerrit_review_runtime_fixture import worker_inventory
        base = [{"name": name, "address": address} for name, address in (("fixture-control", "192.0.2.2"), ("fixture-worker-1", "192.0.2.3"), ("fixture-worker-2", "192.0.2.4"))]
        controls = [base[0], {"name": "fixture-control-2", "address": "192.0.2.5"}, {"name": "fixture-control-3", "address": "192.0.2.6"}]
        edges = [{"name": "edge-a", "address": "192.0.2.10", "memory": 512}, {"name": "edge-b", "address": "192.0.2.11", "memory": 512}]
        context = {"nodes": base, "control_nodes": controls, "native_edge_hosts": {"nodes": edges, "inputs": {"nodes": copy.deepcopy(edges)}}}
        self.assertEqual({node["name"] for node in worker_inventory(context)}, {node["name"] for node in [*controls, *base[1:], *edges]})
        for mode in ("extra-edge", "wrong-address", "wrong-memory", "changed-declaration", "one-control", "empty-profile"):
            changed = copy.deepcopy(context)
            if mode == "extra-edge": changed["native_edge_hosts"]["nodes"].append(dict(edges[0]))
            elif mode == "wrong-address": changed["native_edge_hosts"]["nodes"][0]["address"] = "192.0.2.12"
            elif mode == "wrong-memory": changed["native_edge_hosts"]["nodes"][0]["memory"] = 1024
            elif mode == "changed-declaration": changed["native_edge_hosts"]["inputs"]["nodes"][1]["address"] = "192.0.2.12"
            elif mode == "one-control": changed["control_nodes"] = controls[:1]
            else: changed["native_edge_hosts"] = {}
            with self.subTest(mode=mode), self.assertRaises(ValueError): worker_inventory(changed)

    def test_membership_requires_three_original_distinct_voters(self):
        import copy
        from test_k3s_quorum_fixture import membership
        controls = [{"name": "control-" + str(index), "address": "192.0.2." + str(index + 2)} for index in range(3)]
        document = {"members": [{"ID": str(index + 1), "peerURLs": ["https://" + node["address"] + ":2380"],
            "clientURLs": ["https://" + node["address"] + ":2379"]} for index, node in enumerate(controls)]}
        self.assertEqual(membership(document, controls), {"control-0": "1", "control-1": "2", "control-2": "3"})
        for mode in ("learner", "duplicate-id", "extra-peer", "extra-member", "wrong-client", "zero-id"):
            changed = copy.deepcopy(document)
            if mode == "learner": changed["members"][0]["isLearner"] = True
            if mode == "duplicate-id": changed["members"][0]["ID"] = "2"
            if mode == "extra-peer": changed["members"][0]["peerURLs"].append("https://192.0.2.99:2380")
            if mode == "extra-member": changed["members"].append(copy.deepcopy(changed["members"][0]))
            if mode == "wrong-client": changed["members"][0]["clientURLs"] = ["https://192.0.2.99:2379"]
            if mode == "zero-id": changed["members"][0]["ID"] = "0"
            with self.subTest(mode=mode), self.assertRaises(ValueError): membership(changed, controls)

    def test_server_commands_use_fixed_endpoint_and_consistent_critical_flags(self):
        from test_k3s_quorum_fixture import server_command
        first = server_command({"name": "fixture-control", "address": "192.0.2.2"}, first=True)
        peer = server_command({"name": "fixture-control-2", "address": "192.0.2.5"})
        self.assertIn("--cluster-init", first); self.assertNotIn("--disable-agent", first)
        self.assertIn("--server=https://192.0.2.1:6443", peer); self.assertNotIn("--cluster-init", peer)
        for value in (first, peer):
            for flag in ("--property=Restart=on-failure", "--property=StartLimitIntervalSec=120", "--property=StartLimitBurst=3"):
                self.assertIn(flag, value)
            for flag in ("--tls-san=192.0.2.1", "--secrets-encryption", "--cluster-cidr=198.18.0.0/16", "--service-cidr=198.19.0.0/16", "--disable-network-policy"):
                self.assertIn(flag, value)
            self.assertIn("--token-file=/etc/fixture/", value)
        external = server_command({"name": "fixture-control", "address": "192.0.2.2"}, first=True, external_cni=True)
        self.assertIn("--flannel-backend=none", external); self.assertIn("--disable-kube-proxy", external)
        self.assertNotIn("--flannel-backend=host-gw", external)
        extended = server_command({"name": "fixture-control", "address": "192.0.2.2"}, first=True, external_cni=True, runtime_seconds=1700)
        self.assertIn("--property=RuntimeMaxSec=1700", extended)
        for invalid in (0, 1701, "1700", True):
            with self.assertRaises(ValueError): server_command({"name": "fixture-control", "address": "192.0.2.2"}, external_cni=True, runtime_seconds=invalid)
        with self.assertRaises(ValueError): server_command({"name": "borrowed-node", "address": "192.0.2.99"})


class WorkerRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PATRONI_WATCHDOG_TEST_RUNTIME") == "1", "Explicit disposable KVM watchdog test")
    def test_real_patroni_watchdog_prerequisite(self):
        from test_k3s_vm_fixture import exercise
        from test_patroni_watchdog_fixture import exercise as watchdog
        result = exercise(self, server_fixture=lambda context: watchdog(self, context), server_fixture_watchdog=True)
        self.assertIs(result["watchdog_expiry_paused_original_guest"], True)

    @unittest.skipUnless(os.environ.get("K3S_TIME_TEST_RUNTIME") == "1", "Explicit isolated Chrony relay with parent clock capability removed")
    def test_real_time_relay_without_parent_clock_control(self):
        from test_k3s_time_fixture import relay_probe
        relay_probe(self)

    @unittest.skipUnless(os.environ.get("TEKTON_QUORUM_VM_TEST_RUNTIME") == "1", "Explicit five-VM quorum/process recovery then actual Tekton admission")
    def test_real_tekton_after_quorum_and_process_recovery(self):
        from test_k3s_vm_fixture import exercise
        from test_tekton_worker_runtime_fixture import exercise as pipeline
        exercise(self, quorum=True, workload=lambda context: pipeline(self, context))

    @unittest.skipUnless(os.environ.get("CILIUM_GERRIT_NATIVE_CICD_VM_TEST_RUNTIME") == "1", "Explicit native Gerrit/Tekton/Argo CD flow and original workload behind five-node Cilium TLS")
    def test_real_cilium_gerrit_native_cicd_and_application_gateway(self):
        from test_k3s_vm_fixture import exercise
        from test_cilium_worker_runtime_fixture import bootstrap
        from test_cilium_gerrit_runtime_fixture import exercise as application_flow
        exercise(self, quorum=True, network_bootstrap=lambda context: bootstrap(self, context),
            workload=lambda context: application_flow(self, context), guest_runtime_seconds=1700)

    @unittest.skipUnless(os.environ.get("CILIUM_GERRIT_NATIVE_EDGE_VM_TEST_RUNTIME") == "1", "Explicit seven-guest CI/CD and actual native external entry")
    def test_real_cilium_gerrit_native_cicd_with_external_entry(self):
        from test_k3s_vm_fixture import exercise
        from test_cilium_worker_runtime_fixture import bootstrap
        from test_cilium_gerrit_runtime_fixture import exercise as application_flow
        exercise(self, quorum=True, network_bootstrap=lambda context: bootstrap(self, context),
            workload=lambda context: application_flow(self, context), guest_runtime_seconds=1700, native_edge_hosts=True)

    @unittest.skipUnless(os.environ.get("CILIUM_VM_TEST_RUNTIME") == "1", "Explicit real Cilium CNI, policy and Gateway TLS on five KVM guests")
    def test_real_cilium_five_node_network_policy_and_gateway(self):
        from test_k3s_vm_fixture import exercise
        from test_cilium_worker_runtime_fixture import bootstrap, verify_datapath
        exercise(self, quorum=True, network_bootstrap=lambda context: bootstrap(self, context),
                 workload=lambda context: verify_datapath(self, context, check_dns=True))

    @unittest.skipUnless(os.environ.get("CILIUM_VM_TEST_PASSTHROUGH") == "1", "Explicit five-guest TLS passthrough test")
    def test_real_cilium_mixed_gateway_preserves_backend_certificate(self):
        from test_k3s_vm_fixture import exercise
        from test_cilium_worker_runtime_fixture import bootstrap, verify_datapath
        exercise(self, quorum=True, network_bootstrap=lambda context: bootstrap(self, context, tls_passthrough=True),
                 workload=lambda context: verify_datapath(self, context, tls_passthrough=True))

    @unittest.skipUnless(os.environ.get("K3S_QUORUM_VM_TEST_RUNTIME") == "1", "Explicit five KVM guests, embedded-etcd quorum and API entry")
    def test_real_three_control_plane_quorum_and_api_entry(self):
        from test_k3s_vm_fixture import exercise
        exercise(self, quorum=True)

    @unittest.skipUnless(os.environ.get("K3S_VM_TEST_RUNTIME") == "1", "Explicit disposable KVM guests and actual K3s worker Pods")
    def test_real_k3s_virtual_workers(self):
        from test_k3s_vm_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("GITLAB_RUNNER_VM_TEST_RUNTIME") == "1", "Explicit isolated Runner and K3s worker job")
    def test_real_gitlab_runner_job_on_virtual_workers(self):
        from test_k3s_vm_fixture import exercise
        from test_gitlab_runner_worker_fixture import exercise as runner_job
        exercise(self, workload=lambda context: runner_job(self, context))

    @unittest.skipUnless(os.environ.get("GITLAB_RUNNER_CILIUM_VM_TEST_RUNTIME") == "1", "Explicit Runner job and Cilium policies on isolated VMs")
    def test_real_gitlab_runner_job_with_cilium_policy(self):
        from test_k3s_vm_fixture import exercise
        from test_cilium_worker_runtime_fixture import bootstrap
        from test_gitlab_runner_worker_fixture import exercise as runner_job
        exercise(self, quorum=True, network_bootstrap=lambda context: bootstrap(self, context),
                 workload=lambda context: runner_job(self, context))

    @unittest.skipUnless(os.environ.get("TEKTON_VM_TEST_RUNTIME") == "1", "Explicit real Tekton controllers, admission and worker PipelineRun")
    def test_real_tekton_pipeline_on_virtual_workers(self):
        from test_k3s_vm_fixture import exercise
        from test_tekton_worker_runtime_fixture import exercise as pipeline
        exercise(self, workload=lambda context: pipeline(self, context))

    @unittest.skipUnless(os.environ.get("GERRIT_TEKTON_VM_TEST_RUNTIME") == "1", "Explicit real Gerrit review, Tekton build workers and scoped result vote")
    def test_real_gerrit_patchset_build_and_review_on_virtual_workers(self):
        from test_k3s_vm_fixture import exercise
        from test_tekton_worker_runtime_fixture import exercise as pipeline
        from test_gerrit_worker_runtime_fixture import exercise as application
        exercise(self, workload=lambda context: pipeline(self, context, workload=lambda actual: application(self, actual)))

    @unittest.skipUnless(os.environ.get("GERRIT_TEKTON_ARGOCD_VM_TEST_RUNTIME") == "1", "Explicit actual Gerrit, worker CI, merged handoff and serving Argo CD deployment")
    def test_real_gerrit_worker_ci_and_argocd_serving_handoff(self):
        from test_k3s_vm_fixture import exercise
        from test_tekton_worker_runtime_fixture import exercise as pipeline
        from test_gerrit_worker_runtime_fixture import exercise as application
        exercise(self, workload=lambda context: pipeline(self, context, workload=lambda actual: application(self, actual, argocd_check=True)))

    @unittest.skipUnless(os.environ.get("GERRIT_NATIVE_WATCH_VM_TEST_RUNTIME") == "1", "Explicit real native Gerrit watch services, restart and automatic worker execution")
    def test_real_gerrit_native_watch_services_and_worker_replay(self):
        from test_k3s_vm_fixture import exercise
        from test_tekton_worker_runtime_fixture import exercise as pipeline
        from test_gerrit_worker_runtime_fixture import exercise as application
        exercise(self, workload=lambda context: pipeline(self, context, workload=lambda actual: application(self, actual, native_watch=True)))

    @unittest.skipUnless(os.environ.get("GERRIT_NATIVE_CICD_VM_TEST_RUNTIME") == "1", "Explicit native polling/voting, non-root promoter CLI and actual Argo CD serving workload")
    def test_real_gerrit_native_watch_and_promotion_cli_serving_flow(self):
        from test_k3s_vm_fixture import exercise
        from test_tekton_worker_runtime_fixture import exercise as pipeline
        from test_gerrit_worker_runtime_fixture import exercise as application
        exercise(self, workload=lambda context: pipeline(self, context, workload=lambda actual: application(self, actual, native_watch=True, argocd_check=True)))


if __name__ == "__main__": unittest.main()
