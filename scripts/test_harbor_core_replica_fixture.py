#!/usr/bin/env python3
"""Two original Harbor cores with verified HTTPS health routing and deliberate peer loss."""

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

from bounded_file import read_bounded_bytes

from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_jwt_runtime_fixture import CoreRegistry, JWTChecks
from test_harbor_registry_runtime_fixture import RegistryChecks, retrieve
from test_harbor_service_runtime_fixture import ServiceChecks


class CoreReplicaChecks:
    core_bind_address = "192.0.2.94"
    core_extra_sans = ("IP:192.0.2.94", "IP:192.0.2.95", "DNS:harbor-core-fixture.example.invalid")
    expected_service_jobs = 4
    expected_jwt_phases = {"initial", "core-peer-loss", "promoted", "database-promoted"}

    def __init__(self):
        super().__init__()
        self.intentional_service_losses = set()
        self.core_primary = None; self.core_peer = None
        self.pair_ready = False; self.core_loss_verified = False

    def start_services(self, context, gateway_address):
        for address in ("192.0.2.94", "192.0.2.95"):
            context["run"](["/usr/sbin/ip", "address", "add", address + "/32", "dev", "lo"])
        super().start_services(context, gateway_address)

    def proxy_state(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3); connection.connect(str(self.core_route_socket))
            connection.sendall(b"show servers state harbor_cores\n"); connection.shutdown(socket.SHUT_WR)
            raw = bytearray()
            while True:
                part = connection.recv(4096)
                if not part: break
                raw.extend(part)
                if len(raw) > 32768: raise ValueError("Bound original core-route state")
        lines = [line for line in raw.decode("ascii").splitlines() if line.strip()]
        if len(lines) != 4 or lines[0] != "1" or not lines[1].startswith("# "): raise ValueError("Original core-route state changed")
        keys = lines[1][2:].split(); result = {}
        for line in lines[2:]:
            values = line.split()
            if len(values) != len(keys): raise ValueError("Core-route state is incomplete")
            row = dict(zip(keys, values)); name = row.get("srv_name")
            expected = {"core_a": "192.0.2.94", "core_b": "192.0.2.95"}
            if name not in expected or name in result or row.get("be_name") != "harbor_cores" or row.get("srv_addr") != expected[name] or row.get("srv_port") != "8443" or row.get("srv_admin_state") != "0":
                raise ValueError("Core-route member identity or administrative state changed")
            result[name] = row.get("srv_op_state")
        if set(result) != {"core_a", "core_b"}: raise ValueError("Original core-route member missing")
        return result

    def service_started(self, context, name, child, environment):
        if name != "harbor-core": return
        from test_valkey_gateway_runtime_fixture import HAPROXY_SHA
        from test_valkey_runtime_fixture import CLEAN
        root, write = context["root"], context["write"]
        self.core_primary = child; self.primary_core_environment = dict(environment)
        binary = root / "haproxy"
        if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != HAPROXY_SHA: raise ValueError("Use the original pinned proxy")
        control = root / "core-route-control"; control.mkdir(mode=0o700); os.chown(control, 65534, 65534)
        self.core_route_socket = control / "runtime.sock"
        configuration = ("global\n  stats socket " + str(self.core_route_socket) + " mode 600 level operator\n  stats timeout 2s\n"
            "defaults\n  mode tcp\n  timeout connect 2s\n  timeout client 120s\n  timeout server 120s\n  retries 0\n"
            "frontend harbor_core\n  bind 192.0.2.91:8443\n  default_backend harbor_cores\n"
            "backend harbor_cores\n  balance roundrobin\n  option httpchk\n"
            "  http-check send meth GET uri /api/v2.0/ping ver HTTP/1.1 hdr Host harbor-core-fixture.example.invalid\n"
            "  http-check expect status 200\n")
        for server, address in (("core_a", "192.0.2.94"), ("core_b", "192.0.2.95")):
            configuration += ("  server " + server + " " + address + ":8443 check check-ssl verify required ca-file " + str(root / "ca.crt")
                + " verifyhost harbor-core-fixture.example.invalid check-sni harbor-core-fixture.example.invalid ssl-min-ver TLSv1.2 inter 200ms fall 2 rise 2\n")
        path = write("service-inputs/core-route.cfg", configuration)
        context["run"]([binary, "-c", "-f", path])
        proxy = context["spawn"]("harbor-core-route", [binary, "-W", "-db", "-f", path], CLEAN)
        self.service_children.append(proxy)
        self.service_identities.append(Path("/proc", str(proxy.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])

    def service_ready(self, context, name):
        if name != "harbor-core": return
        root = context["root"]
        context["write"]("conf/peer.app.conf", "appname = Harbor\nrunmode = peer\n[peer]\nhttpsaddr = 192.0.2.95\n")
        environment = {**self.primary_core_environment, "BEEGO_RUNMODE": "peer", "CORE_LOCAL_URL": "https://192.0.2.95:8443"}
        self.core_peer = context["spawn"]("harbor-core-peer", [root / "harbor-core"], environment)
        self.service_children.append(self.core_peer)
        self.service_identities.append(Path("/proc", str(self.core_peer.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])
        context["wait"](lambda: self.proxy_state() == {"core_a": "2", "core_b": "2"}, seconds=30)
        public = self.service_clients["harbor-core"]
        self.core_direct = [CoreRegistry(SimpleNamespace(address=address, port=8443, trust=public.trust, leaf=public.leaf)) for address in ("192.0.2.94", "192.0.2.95")]
        authorization = "Basic " + base64.b64encode(("admin:" + self.admin_password).encode()).decode()
        for direct in self.core_direct:
            direct.authorization = authorization
            context["test"].assertEqual(direct.request("GET", "/v2/")[0], 200)
        self.pair_ready = True
        del self.primary_core_environment

    def service_workload(self, context, phase):
        super().service_workload(context, phase)
        if phase != "initial": return
        if not self.pair_ready or self.core_primary is None or self.core_peer is None: raise ValueError("Require both original cores before deliberate loss")
        token, tag, parts = self.jwt_witness
        for direct in self.core_direct:
            direct.authorization = "Bearer " + token
            retrieve(direct, tag, parts)
        context["test"].assertIsNone(self.core_primary.poll())
        # Only the exact Popen handle spawned by this fixture may be killed.
        self.core_primary.kill(); self.core_primary.wait(timeout=10)
        context["stop"](self.core_primary); self.intentional_service_losses.add(self.core_primary)
        context["wait"](lambda: self.proxy_state() == {"core_a": "0", "core_b": "2"}, seconds=10)
        retrieve(CoreRegistry(self.service_clients["harbor-core"], "Bearer " + token), tag, parts)
        self.service_workload(context, "core-peer-loss")
        context["test"].assertIsNone(self.core_peer.poll())
        self.core_loss_verified = True

    def exercise(self, context):
        try: result = super().exercise(context)
        except Exception:
            # Preserve only bounded, scrubbed diagnostics in the original
            # ignored output directory before the outer fixture removes logs.
            output = Path(os.environ["VALKEY_TEST_REPORT"]).resolve().parent
            private = Path(__file__).resolve().parents[1] / "private/validation"
            if not output.is_relative_to(private.resolve()): raise ValueError("Keep diagnostics in private validation storage") from None
            messages = {}
            for path in context["root"].glob("harbor-core*.log"):
                text = read_bounded_bytes(path, max_bytes=4 * 1024 ** 2).decode(errors="replace")
                lines = [line for line in text.splitlines() if "error" in line.casefold() or "failed" in line.casefold()]
                text = "\n".join(lines[-120:])
                for secret in sorted((value for value in context["private_values"] if isinstance(value, str) and value), key=len, reverse=True): text = text.replace(secret, "[redacted]")
                messages[path.name] = text
            target = output / (Path(os.environ["VALKEY_TEST_REPORT"]).stem + ".private-core-errors.json")
            with target.open("x", encoding="utf-8") as stream: json.dump(messages, stream)
            target.chmod(0o600)
            raise
        if not self.pair_ready or not self.core_loss_verified or self.core_primary.poll() is None: raise ValueError("Core peer loss was not verified")
        result.update(valkey_gateway_harbor_two_core_processes_verified=True,
            valkey_gateway_harbor_core_verified_https_health_routing=True,
            valkey_gateway_harbor_core_peer_sigkill_continuity_verified=True,
            valkey_gateway_harbor_original_core_survivor_after_promotions_verified=True,
            valkey_gateway_harbor_core_independent_host_ha_verified=False,
            valkey_gateway_harbor_jobservice_replica_failover_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
