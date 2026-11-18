#!/usr/bin/env python3
"""Actual TLS gateway forwarding and promotion; not a Harbor or physical-HA test."""

import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time

import materialize_valkey_auth as auth
import render_valkey_gateway as renderer
from valkey_protocol import Connection, ReplyError
import test_valkey_runtime_fixture as cache_fixture
from test_valkey_gateway import fixture


HAPROXY_SHA = "3137dc28d1fa8095ce2610e5736d67cbbdec3a3b15af49bb3ba8631ebe5904ed"


class Application:
    evidence_prefix = "valkey_gateway_"
    def configure(self, plan): plan["network"]["client_namespaces"] = ["cache-gateway"]
    def certificate_dns_names(self, name): return [name + ".gateway.example.invalid"]
    def auth_files(self, *args, **kwargs): return auth.files(*args, **kwargs)
    def prepare_gateways(self, context, binary, *, require_client_certificate=False, vrrp=None):
        root = context["root"]; run, write = context["run"], context["write"]
        binary = Path(binary).resolve(strict=True)
        if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != HAPROXY_SHA: raise ValueError("Use the exact reviewed HAProxy runtime")
        proxy_binary = root / "haproxy"; proxy_binary.write_bytes(binary.read_bytes()); proxy_binary.chmod(0o755)
        plan, _ = fixture(); plan["private_dir"] = str(root / "gateway"); plan["native_binary"] = str(proxy_binary); plan["native_binary_sha256"] = HAPROXY_SHA
        plan["observer"] = copy.deepcopy(context["runtime_plan"])
        plan["namespace"], = context["plan"]["network"]["client_namespaces"]
        if require_client_certificate:
            plan["client_namespaces"] = [plan["namespace"]]
        private = Path(plan["private_dir"])
        for kind in ("ca", "client_cert", "client_key"):
            source = Path(plan["observer"]["trust"][kind + "_file"])
            destination = write("gateway/" + kind + ".pem", source.read_text(), owner=True, mode=0o600)
            plan["observer"]["trust"][kind + "_file"] = str(destination)
        plan["tls_names"] = {name: name + ".gateway.example.invalid" for name in plan["observer"]["members"]}
        plan["frontend"]["hostname"] = "cache-gateway.example.invalid"
        if require_client_certificate:
            plan["frontend"]["require_client_certificate"] = True
        addresses = [instance["address"] for instance in plan["instances"]]
        if vrrp is not None:
            plan["vrrp"] = vrrp
            addresses.append(vrrp["vip"].split("/")[0])
        names = ",".join(["DNS:" + plan["frontend"]["hostname"], *("IP:" + address for address in addresses)])
        extension = write("gateway-extensions.cnf", "subjectAltName=" + names + "\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n")
        run(["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=cache-gateway.example.invalid",
            "-keyout", root / "gateway.key", "-out", root / "gateway.csr"])
        run(["/usr/bin/openssl", "x509", "-req", "-in", root / "gateway.csr", "-CA", root / "ca.crt", "-CAkey", root / "ca.key", "-CAcreateserial", "-days", "1",
            "-extfile", extension, "-out", root / "gateway.crt"])
        frontend = write("gateway/frontend.pem", (root / "gateway.crt").read_text() + (root / "gateway.key").read_text(), owner=True, mode=0o600)
        upstream = write("gateway/upstream.pem", (private / "client_cert.pem").read_text() + (private / "client_key.pem").read_text(), owner=True, mode=0o600)
        plan["private_sha256"] = {"frontend.pem": hashlib.sha256(frontend.read_bytes()).hexdigest(), "upstream.pem": hashlib.sha256(upstream.read_bytes()).hexdigest()}
        contract = json.loads((context["bundle"] / "valkey-contract.json").read_bytes())
        rendered = renderer.render(plan, contract, "raw")
        for name, content in rendered.items(): write("gateway/" + name, content)
        run(["/usr/bin/sha256sum", "--check", "--strict", private / "private.sha256"])
        run(["/usr/bin/sha256sum", "--check", "--strict", private / "native-binary.sha256"])
        environment = {**cache_fixture.CLEAN, **{context["references"][role]: context["environment"][context["references"][role]] for role in ("probe", "discovery")}}
        return plan, private, proxy_binary, environment

    def start_gateways(self, context, binary, *, require_client_certificate=False):
        run, write = context["run"], context["write"]
        plan, private, proxy_binary, environment = self.prepare_gateways(
            context, binary, require_client_certificate=require_client_certificate)
        checker = context["spawn"]("valkey-gateway-checker", [sys.executable, "-E", "-s", "-B", private / "valkey_gateway_health.py", "--specification", private / "gateway-checks.json"], environment)
        proxies = []
        for instance in plan["instances"]:
            run(["/usr/sbin/ip", "address", "add", instance["address"] + "/32", "dev", "lo"])
            # Both forwarders share this isolated test namespace. Relocate only
            # their read-only readiness listeners; discovery/data policy is exact.
            name = "gateway/" + instance["name"] + ".cfg"
            config = write(name, (context["root"] / name).read_text().replace("bind 127.0.0.1:19090", "bind " + instance["address"] + ":19090"))
            run([proxy_binary, "-c", "-f", config])
            proxies.append(context["spawn"]("valkey-" + instance["name"], [proxy_binary, "-W", "-db", "-f", config], cache_fixture.CLEAN))
        return plan, checker, proxies

    def exercise(self, context):
        test, root = context["test"], context["root"]
        plan, checker, proxies = self.start_gateways(context, os.environ["VALKEY_GATEWAY_HAPROXY"])
        bare = ssl.create_default_context(cafile=str(root / "ca.crt")); bare.minimum_version = ssl.TLSVersion.TLSv1_2
        leaf = hashlib.sha256(ssl.PEM_cert_to_DER_cert((root / "gateway.crt").read_text())).hexdigest()
        secret = context["environment"][context["references"]["probe"]]
        def connect(address):
            return Connection(address, plan["frontend"]["hostname"], 6381, bare, peer_sha256=leaf, seconds=2, deadline=time.monotonic() + 6,
                max_bytes=65536, username="probe", password=secret)
        def roundtrip(address, *, before=False):
            try:
                with connect(address) as client:
                    if client.command("SET", "platform-probe:gateway", "gateway-witness", "PX", 180000) != "OK": return False
                    if client.command("WAIT", 2 if before else 1, 1000) < (2 if before else 1): return False
                    return client.command("GET", "platform-probe:gateway") == "gateway-witness"
            except (OSError, TimeoutError, ReplyError): return False
        addresses = [instance["address"] for instance in plan["instances"]]
        context["wait"](lambda: all(roundtrip(address, before=True) for address in addresses), seconds=35)
        if hasattr(self, "client_check"): self.client_check(context, "initial", addresses[1])
        for child in proxies:
            status = dict(line.split(":", 1) for line in Path("/proc", str(child.pid), "status").read_text().splitlines() if ":" in line)
            test.assertEqual(status["Uid"].split()[0], "65534"); test.assertEqual(int(status["CapEff"].strip(), 16), 0)
        # A client without a certificate works only through the gateway. The
        # original backend still requires its mTLS identity before ACL AUTH.
        first = context["plan"]["members"][0]
        with test.assertRaises((OSError, ssl.SSLError)):
            with cache_fixture.RESP(first["address"], 6380, bare, "probe", secret) as direct: direct.command("PING")
        with socket.create_connection((addresses[1], 6381), timeout=2) as clear:
            clear.settimeout(2); clear.sendall(b"*1\r\n$4\r\nPING\r\n")
            try: response = clear.recv(1024)
            except OSError: response = b""
            test.assertNotIn(b"PONG", response)
        context["stop"](proxies[0])
        context["wait"](lambda: roundtrip(addresses[1]), seconds=10)
        context["primary_loss"]()
        context["wait"](lambda: roundtrip(addresses[1]), seconds=30)
        if hasattr(self, "client_check"): self.client_check(context, "promoted", addresses[1])
        test.assertIsNone(proxies[1].poll())
        context["stop"](checker)
        context["wait"](lambda: not roundtrip(addresses[1]), seconds=12)
        if hasattr(self, "client_check"): self.client_check(context, "checker-stopped", addresses[1])
        return {"valkey_gateway_real_tls_forwarding_verified": True, "valkey_gateway_upstream_mtls_preserved": True,
            "valkey_gateway_plaintext_refused": True, "valkey_gateway_peer_forwarder_survives_process_loss": True,
            "valkey_gateway_primary_loss_reconnect_verified": True, "valkey_gateway_checker_loss_fails_closed": True,
            "valkey_gateway_binaries_sha256": {"haproxy": HAPROXY_SHA}, "valkey_gateway_harbor_compatibility_verified": False}


def exercise(test): return cache_fixture.exercise(test, application=Application())
