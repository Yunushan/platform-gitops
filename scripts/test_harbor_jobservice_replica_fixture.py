#!/usr/bin/env python3
"""Actual Harbor jobservice pool, queue and log continuity after owned peer SIGKILL."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import time

import yaml

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
from test_harbor_core_replica_fixture import CoreReplicaChecks
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_jwt_runtime_fixture import JWTChecks
from test_harbor_registry_runtime_fixture import RegistryChecks
from test_harbor_service_runtime_fixture import ServiceChecks


def pool_stats(raw, now):
    document = loads_strict_json(raw)
    rows = document.get("worker_pools") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2: raise ValueError("Require only the owned worker pools")
    pools = {}
    for row in rows:
        if not isinstance(row, dict): raise ValueError("Require typed worker statistics")
        identifier = row.get("worker_pool_id"); started, heartbeat = row.get("started_at"), row.get("heartbeat_at")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier) or identifier in pools
                or type(started) is not int or type(heartbeat) is not int or not 0 < started <= heartbeat <= now + 2
                or type(row.get("concurrency")) is not int or row["concurrency"] != 2
                or not isinstance(row.get("job_names"), list) or "PURGE_AUDIT_LOG" not in row["job_names"]
                or row.get("status") not in {"Healthy", "Dead"}
                or row["status"] == "Healthy" and now - heartbeat > 10):
            raise ValueError("Worker identity, freshness, concurrency or job registration changed")
        pools[identifier] = row
    return pools


class JobPeerClient:
    def __init__(self, port, service):
        if type(port) is not int or port not in {9445, 9446} or service.address != "192.0.2.92" or service.port != 9443: raise ValueError("Use a reviewed original jobservice peer")
        self.port, self.service = port, service

    def request(self, path, authorization=None):
        if not isinstance(path, str) or not re.fullmatch(r"/api/v1/(?:stats|jobs/[A-Za-z0-9_-]{1,128}(?:/log)?)", path): raise ValueError("Bound original jobservice reads")
        if authorization is not None and (not isinstance(authorization, str) or len(authorization) > 1024 or any(ord(c) < 32 or ord(c) > 126 for c in authorization)): raise ValueError("Bound original internal authorization")
        owner = self
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                if hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != owner.service.leaf:
                    self.close(); raise ValueError("Original jobservice TLS identity changed")
        connection = Pinned("127.0.0.1", self.port, context=self.service.trust, timeout=4)
        headers = {"Accept": "application/json"}
        if authorization: headers["Authorization"] = authorization
        try:
            connection.request("GET", path, headers=headers); response = connection.getresponse(); raw = response.read(1024 ** 2 + 1)
            if len(raw) > 1024 ** 2: raise ValueError("Bound original jobservice response")
            return response.status, raw
        finally: connection.close()


class JobReplicaChecks:
    jobservice_backend_port = 9446
    jobservice_extra_sans = ("IP:127.0.0.1", "DNS:harbor-job-fixture.example.invalid")
    expected_service_jobs = 6  # Primary-only witness plus five distinct workload phases.
    expected_jwt_phases = {"initial", "core-peer-loss", "job-peer-loss", "promoted", "database-promoted"}

    def __init__(self):
        super().__init__()
        self.job_primary = None; self.job_peer = None
        self.job_pair_ready = False; self.job_loss_verified = False

    def job_route_state(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3); connection.connect(str(self.job_route_socket))
            connection.sendall(b"show servers state harbor_jobs\n"); connection.shutdown(socket.SHUT_WR)
            raw = bytearray()
            while True:
                part = connection.recv(4096)
                if not part: break
                raw.extend(part)
                if len(raw) > 32768: raise ValueError("Bound original jobservice-route state")
        lines = [line for line in raw.decode("ascii").splitlines() if line.strip()]
        if len(lines) != 4 or lines[0] != "1" or not lines[1].startswith("# "): raise ValueError("Original jobservice-route format changed")
        keys = lines[1][2:].split(); result = {}
        for line in lines[2:]:
            values = line.split()
            if len(values) != len(keys): raise ValueError("Original jobservice-route state incomplete")
            row = dict(zip(keys, values)); name = row.get("srv_name"); expected = {"job_a": "9446", "job_b": "9445"}
            if name not in expected or name in result or row.get("be_name") != "harbor_jobs" or row.get("srv_addr") != "127.0.0.1" or row.get("srv_port") != expected[name] or row.get("srv_admin_state") != "0":
                raise ValueError("Original jobservice route member identity changed")
            result[name] = row.get("srv_op_state")
        if set(result) != {"job_a", "job_b"}: raise ValueError("Original jobservice route member missing")
        return result

    def service_started(self, context, name, child, environment):
        super().service_started(context, name, child, environment)
        if name != "harbor-jobservice": return
        from test_valkey_gateway_runtime_fixture import HAPROXY_SHA
        from test_valkey_runtime_fixture import CLEAN
        root, write = context["root"], context["write"]
        self.job_primary = child; self.primary_job_environment = dict(environment)
        binary = root / "haproxy"
        if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != HAPROXY_SHA: raise ValueError("Use the reviewed original proxy")
        control = root / "job-route-control"; control.mkdir(mode=0o700); os.chown(control, 65534, 65534); self.job_route_socket = control / "runtime.sock"
        configuration = ("global\n  stats socket " + str(self.job_route_socket) + " mode 600 level operator\n  stats timeout 2s\n"
            "defaults\n  mode tcp\n  timeout connect 2s\n  timeout client 120s\n  timeout server 120s\n  retries 0\n"
            "frontend jobservice\n  bind 192.0.2.92:9443\n  default_backend harbor_jobs\n"
            "backend harbor_jobs\n  balance roundrobin\n  option httpchk\n"
            "  http-check send meth GET uri /api/v1/stats ver HTTP/1.1 hdr Host harbor-job-fixture.example.invalid\n"
            "  http-check expect status 200\n")
        for server, port in (("job_a", 9446), ("job_b", 9445)):
            configuration += ("  server " + server + " 127.0.0.1:" + str(port) + " check check-ssl verify required ca-file " + str(root / "ca.crt")
                + " verifyhost harbor-job-fixture.example.invalid check-sni harbor-job-fixture.example.invalid ssl-min-ver TLSv1.2 inter 200ms fall 2 rise 2\n")
        path = write("service-inputs/job-route.cfg", configuration); context["run"]([binary, "-c", "-f", path])
        proxy = context["spawn"]("harbor-job-route", [binary, "-W", "-db", "-f", path], CLEAN)
        self.service_children.append(proxy); self.service_identities.append(Path("/proc", str(proxy.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])

    def read_pools(self, client):
        status, raw = client.request("/api/v1/stats")
        if status != 200: return None
        return pool_stats(raw, int(time.time()))

    def service_ready(self, context, name):
        super().service_ready(context, name)
        if name != "harbor-jobservice": return
        root, public = context["root"], self.service_clients["harbor-jobservice"]
        self.job_clients = [JobPeerClient(port, public) for port in (9446, 9445)]
        pools = self.read_pools(self.job_clients[0])
        if pools is None or len(pools) != 1: raise ValueError("Require the sole original primary worker before starting its peer")
        self.primary_pool_id = next(iter(pools)); self.primary_pool_started = pools[self.primary_pool_id]["started_at"]
        if pools[self.primary_pool_id]["status"] != "Healthy": raise ValueError("Original primary worker is not healthy")
        self.primary_job_witness, raw = self.submit_dry_run_job(context)
        self.job_witness_log_sha = hashlib.sha256(raw).hexdigest()
        configuration = yaml.safe_load(read_bounded_bytes(root / "service-inputs/harbor-jobservice.yaml", max_bytes=32768))
        configuration["port"] = 9445
        path = context["write"]("service-inputs/harbor-jobservice-peer.yaml", yaml.safe_dump(configuration), owner=True, mode=0o600)
        self.job_peer = context["spawn"]("harbor-jobservice-peer", [root / "harbor-jobservice", "-c", path], self.primary_job_environment)
        self.service_children.append(self.job_peer); self.service_identities.append(Path("/proc", str(self.job_peer.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])
        context["wait"](lambda: self.job_route_state() == {"job_a": "2", "job_b": "2"}, seconds=30)
        def joined():
            pools = self.read_pools(self.job_clients[1])
            if pools is None or len(pools) != 2 or any(row["status"] != "Healthy" for row in pools.values()): return False
            if self.primary_pool_id not in pools or pools[self.primary_pool_id]["started_at"] != self.primary_pool_started: raise ValueError("Original primary pool identity changed")
            self.peer_pool_id = next(iter(set(pools) - {self.primary_pool_id})); self.peer_pool_started = pools[self.peer_pool_id]["started_at"]
            return True
        context["wait"](joined, seconds=30)
        auth = "Harbor-Secret " + self.core_secret
        for client in self.job_clients:
            context["test"].assertEqual(set(self.read_pools(client)), {self.primary_pool_id, self.peer_pool_id})
            context["test"].assertIn(client.request("/api/v1/jobs/" + self.primary_job_witness + "/log")[0], (401, 403))
            status, raw = client.request("/api/v1/jobs/" + self.primary_job_witness + "/log", auth)
            context["test"].assertEqual(status, 200); context["test"].assertEqual(hashlib.sha256(raw).hexdigest(), self.job_witness_log_sha)
        self.job_pair_ready = True; del self.primary_job_environment

    def survivor_healthy(self):
        pools = self.read_pools(self.job_clients[1])
        if pools is None or self.peer_pool_id not in pools: return False
        row = pools[self.peer_pool_id]
        if row["started_at"] != self.peer_pool_started: raise ValueError("Original surviving worker pool changed")
        return {identifier for identifier, value in pools.items() if value["status"] == "Healthy"} == {self.peer_pool_id}

    def service_workload(self, context, phase):
        super().service_workload(context, phase)
        if phase == "core-peer-loss":
            if not self.job_pair_ready: raise ValueError("Require both original jobservice peers before deliberate loss")
            context["test"].assertIsNone(self.job_primary.poll())
            self.job_primary.kill(); self.job_primary.wait(timeout=10); context["stop"](self.job_primary); self.intentional_service_losses.add(self.job_primary)
            context["wait"](lambda: self.job_route_state() == {"job_a": "0", "job_b": "2"}, seconds=10)
            context["wait"](self.survivor_healthy, seconds=25)
            auth = "Harbor-Secret " + self.core_secret
            status, raw = self.job_clients[1].request("/api/v1/jobs/" + self.primary_job_witness, auth)
            context["test"].assertEqual(status, 200); context["test"].assertEqual(loads_strict_json(raw)["job"]["status"], "Success")
            status, raw = self.job_clients[1].request("/api/v1/jobs/" + self.primary_job_witness + "/log", auth)
            context["test"].assertEqual(status, 200); context["test"].assertEqual(hashlib.sha256(raw).hexdigest(), self.job_witness_log_sha)
            self.service_workload(context, "job-peer-loss")
            self.job_loss_verified = True
        if phase in {"job-peer-loss", "promoted", "database-promoted"}:
            context["test"].assertTrue(self.survivor_healthy()); context["test"].assertIsNone(self.job_peer.poll())

    def exercise(self, context):
        try: result = super().exercise(context)
        except Exception:
            output = Path(os.environ["VALKEY_TEST_REPORT"]).resolve().parent; private = Path(__file__).resolve().parents[1] / "private/validation"
            if not output.is_relative_to(private.resolve()): raise ValueError("Keep diagnostics in private validation storage") from None
            messages = {}
            for path in context["root"].glob("harbor-job*.log"):
                text = read_bounded_bytes(path, max_bytes=4 * 1024 ** 2).decode(errors="replace")
                text = "\n".join(line for line in text.splitlines()[-160:] if "error" in line.casefold() or "failed" in line.casefold())
                for secret in sorted((v for v in context["private_values"] if isinstance(v, str) and v), key=len, reverse=True): text = text.replace(secret, "[redacted]")
                messages[path.name] = text
            target = output / (Path(os.environ["VALKEY_TEST_REPORT"]).stem + ".private-jobservice-errors.json")
            with target.open("x", encoding="utf-8") as stream: json.dump(messages, stream)
            target.chmod(0o600); raise
        if not self.job_pair_ready or not self.job_loss_verified or self.job_primary.poll() is None: raise ValueError("Original jobservice loss was not verified")
        result.update(valkey_gateway_harbor_two_worker_pools_verified=True, valkey_gateway_harbor_jobservice_verified_https_health_routing=True,
            valkey_gateway_harbor_primary_worker_log_survives_peer_loss_verified=True, valkey_gateway_harbor_jobservice_peer_sigkill_continuity_verified=True,
            valkey_gateway_harbor_original_worker_pool_after_promotions_verified=True,
            valkey_gateway_harbor_jobservice_independent_host_ha_verified=False, valkey_gateway_harbor_jobservice_inflight_recovery_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
