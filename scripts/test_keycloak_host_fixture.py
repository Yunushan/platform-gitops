#!/usr/bin/env python3
"""Disposable systemd/TLS mechanics, not a Keycloak or HA acceptance test."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import ssl
import sys
import tempfile
from unittest import mock
from uuid import uuid4

import activate_keycloak_host as activate
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture
from test_keycloak_host import host_plan


def exercise(test):
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").is_dir(): raise ValueError("Use the explicit disposable root Linux systemd runner")
    identity = "kchost-fixture-" + uuid4().hex[:12]; account = pwd.getpwnam("nobody"); host = activate.Host()
    temporary = tempfile.mkdtemp(prefix="platform-keycloak-host-", dir="/run"); root = Path(temporary); root.chmod(0o755)
    reservation = socket.socket(); reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
    unit = Path("/etc/systemd/system") / ("platform-" + identity + ".service"); created = False; unit_text = None; unit_inode = None
    if unit.exists() or unit.is_symlink(): raise ValueError("A fixture unit already exists")
    def write(name, value, mode=0o644):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value); path.chmod(mode); return path
    try:
        host.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-keyout", str(root / "tls.key"), "-out", str(root / "tls.crt")])
        (root / "tls.key").chmod(0o640); os.chown(root / "tls.key", 0, account.pw_gid)
        shutil.copyfile(root / "tls.crt", root / "health-ca.crt")
        program = write("server.py", '''import http.server,ssl
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header("Content-Type","application/json");self.end_headers();self.wfile.write(b'{"status":"UP","checks":[{"name":"fixture","status":"UP"}]}')
 def log_message(self,*args):pass
server=http.server.HTTPServer(("127.0.0.1",PORT),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(CERT,KEY)
server.socket=context.wrap_socket(server.socket,server_side=True);server.serve_forever()
'''.replace("PORT", str(port)).replace("CERT", repr(str(root / "tls.crt"))).replace("KEY", repr(str(root / "tls.key"))))
        # Actual root-owned immutable file checks, including extra/missing file
        # refusal; this tiny inventory does not stand in for a Java distribution.
        home = root / "release"; home.mkdir(); binary = write("release/reviewed.txt", "fixture payload\n")
        manifest = write("files.sha256", hashlib.sha256(binary.read_bytes()).hexdigest() + "  reviewed.txt\n")
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest(); activate.manifest(home, manifest, digest)
        binary.chmod(0o600)
        with test.assertRaises(ValueError): activate.manifest(home, manifest, digest, reader={"service_uid": account.pw_uid, "service_gid": account.pw_gid})
        binary.chmod(0o644)
        extra = write("release/unreviewed.txt", "extra\n")
        with test.assertRaises(ValueError): activate.manifest(home, manifest, digest)
        extra.unlink(); binary.write_text("changed\n")
        with test.assertRaises(ValueError): activate.manifest(home, manifest, digest)
        selected = fixture(); selected["members"][0]["name"] = identity
        bundle = root / "bundle"; bundle.mkdir()
        for name, value in renderer.render(selected, "raw").items(): write("bundle/" + name, value)
        plan = host_plan(hashlib.sha256((bundle / "keycloak-contract.json").read_bytes()).hexdigest(), "raw"); plan["member"] = identity
        plan["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        plan["machine_id_sha256"] = hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest()
        # Only the service-controller fixture replaces prerequisites/workload
        # body. Actual create, reload, start, properties, cgroup and TLS code run.
        def body(contract, original_plan, state):
            return "# operation=" + state["operation_id"] + "\n[Unit]\nDescription=Disposable Keycloak host mechanics fixture\n[Service]\nType=simple\nUser=" + account.pw_name + "\nGroup=" + str(account.pw_gid) + "\nExecStart=/usr/bin/python3 -B " + str(program) + "\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
        class SystemHost(activate.Host):
            def prerequisites(self, contract, plan, *, starting=False): return {"disposable_systemd_fixture": identity, "boot_id": plan["boot_id"]}
            def create(self, contract, plan, state):
                nonlocal created, unit_text, unit_inode
                result = super().create(contract, plan, state); created = True; unit_text = body(contract, plan, state)
                info = unit.stat(); unit_inode = (info.st_dev, info.st_ino); return result
            def healthy(self, contract, plan, original):
                if not activate.owns_listener(original, "127.0.0.1", port): return False
                connection = activate.DirectHTTPS("127.0.0.1", "localhost", port, ssl.create_default_context(cafile=str(root / "health-ca.crt")))
                try:
                    connection.request("GET", "/health/ready"); response = connection.getresponse()
                    return response.status == 200 and json.loads(response.read(4096)).get("status") == "UP"
                finally: connection.close()
        reservation.close(); operation = root / "private/operation"
        with mock.patch.object(activate, "ROOT", root), mock.patch.object(activate, "unit_text", side_effect=body):
            backend = SystemHost()
            test.assertTrue(activate.operate(bundle, plan, mode="check-host", host=backend)["host_checked"])
            result = activate.operate(bundle, plan, mode="activate", output=operation, host=backend, wait_seconds=30)
            test.assertTrue(result["tls_readiness_observed"])
            before = backend.properties(plan)["InvocationID"]
            test.assertTrue(activate.operate(bundle, plan, mode="resume", output=operation, host=backend, wait_seconds=5)["read_only"])
            test.assertEqual(backend.properties(plan)["InvocationID"], before)
            props = backend.properties(plan); original = {"pid": int(props["MainPID"]), "cgroup": props["ControlGroup"]}
            test.assertTrue(activate.owns_listener(original, "127.0.0.1", port))
            test.assertFalse(activate.owns_listener(original, "127.0.0.1", port + 1 if port < 65535 else port - 1))
            wrong = activate.DirectHTTPS("127.0.0.1", "wrong.example.invalid", port, ssl.create_default_context(cafile=str(root / "health-ca.crt")))
            try:
                with test.assertRaises(ssl.SSLCertVerificationError): wrong.connect()
            finally: wrong.close()
        return {"systemd_create_start_resume_verified": True, "kernel_listener_ownership_verified": True, "tls_hostname_verification_checked": True, "manifest_drift_refused": True,
            "keycloak_runtime_verified": False, "docker_runtime_verified": False, "ha_verified": False}
    finally:
        reservation.close()
        if created:
            # The unit was created exclusively by this fixture. Never remove a
            # changed file or operate on an unrelated service during cleanup.
            if unit.is_symlink() or not unit.is_file() or unit.read_text() != unit_text or (unit.stat().st_dev, unit.stat().st_ino) != unit_inode: raise ValueError("Fixture unit ownership changed; inspect original handles")
            host.run(["/usr/bin/systemctl", "stop", unit.name]); unit.unlink(); host.reload()
        if root.resolve().parent != Path("/run") or not root.name.startswith("platform-keycloak-host-"): raise ValueError("Unexpected fixture cleanup directory")
        shutil.rmtree(root)
