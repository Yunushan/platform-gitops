#!/usr/bin/env python3
"""Actual application reconnection after a controlled database promotion, not operator HA."""

import base64
import hashlib
import os
from pathlib import Path
import socket

from test_harbor_service_runtime_fixture import ServiceChecks
from test_harbor_registry_runtime_fixture import RegistryChecks


class DatabasePromotionChecks:
    planned_database_promotion = True
    database_client_port = 6432

    def route(self, command):
        if command not in {"show servers state db", "set server db/primary addr 127.0.0.12 port 5432",
                "set server db/primary state maint", "shutdown sessions server db/primary", "set server db/primary state ready"}: raise ValueError("Bound test routing commands")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3); connection.connect(str(self.routing_socket)); connection.sendall((command + "\n").encode()); connection.shutdown(socket.SHUT_WR)
            raw = bytearray()
            while True:
                part = connection.recv(4096)
                if not part: break
                raw.extend(part)
                if len(raw) > 16384: raise ValueError("Bound original routing response")
            return raw.decode("ascii")

    def route_state(self):
        lines = [line for line in self.route("show servers state db").splitlines() if line.strip()]
        if len(lines) != 3 or lines[0] != "1" or not lines[1].startswith("# "): raise ValueError("Original proxy state format changed")
        keys, values = lines[1][2:].split(), lines[2].split()
        if len(keys) != len(values): raise ValueError("Original proxy state is incomplete")
        return dict(zip(keys, values))

    def routed_to(self, host):
        if host not in {"127.0.0.11", "127.0.0.12"}: raise ValueError("Use an original database member")
        row = self.route_state()
        return (row.get("be_name") == "db" and row.get("srv_name") == "primary" and row.get("srv_addr") == host
            and row.get("srv_port") == "5432" and row.get("srv_admin_state") == "0" and row.get("srv_op_state") == "2")

    def start_services(self, context, gateway_address):
        from test_valkey_gateway_runtime_fixture import HAPROXY_SHA
        from test_valkey_runtime_fixture import CLEAN
        root, write = context["root"], context["write"]
        binary = root / "haproxy"
        if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != HAPROXY_SHA: raise ValueError("Use the original reviewed routing executable")
        control = root / "database-route-control"; control.mkdir(mode=0o700); os.chown(control, 65534, 65534)
        self.routing_socket = control / "runtime.sock"
        # Fixed TCP endpoint models endpoint movement independently of the
        # database. It never selects a primary or terminates application TLS.
        configuration = ("global\n  stats socket " + str(self.routing_socket) + " mode 600 level admin\n  stats timeout 2s\n"
            "defaults\n  mode tcp\n  timeout connect 2s\n  timeout client 120s\n  timeout server 120s\n  retries 0\n"
            "frontend application_database\n  bind 127.0.0.1:6432\n  default_backend db\n"
            "backend db\n  server primary 127.0.0.11:5432\n")
        path = write("service-inputs/database-route.cfg", configuration)
        context["run"]([binary, "-c", "-f", path])
        child = context["spawn"]("harbor-database-route", [binary, "-W", "-db", "-f", path], CLEAN)
        self.service_children.append(child)
        self.service_identities.append(Path("/proc", str(child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])
        context["wait"](lambda: self.routed_to("127.0.0.11"), seconds=10)
        super().start_services(context, gateway_address)

    def client_check(self, context, phase, gateway_address):
        super().client_check(context, phase, gateway_address)
        if phase != "promoted": return
        context["test"].assertTrue(self.routed_to("127.0.0.11"))
        # Block new connections and close existing sessions before the old
        # writer can rejoin as a standby. Changing its address alone preserves
        # existing connections and can strand application pools on a replica.
        self.route("set server db/primary state maint")
        context["test"].assertFalse(self.routed_to("127.0.0.11"))
        self.route("shutdown sessions server db/primary")
        context["wait"](lambda: self.sql("SELECT count(*) FROM pg_stat_activity WHERE usename='harbor_fixture';") == "0", seconds=10)
        self.database_route_fenced = True
        self.promotion = self.pg["promote"]()
        if self.promotion.get("host") != "127.0.0.12" or self.promotion.get("port") != 5432: raise ValueError("The reviewed promotion target changed")
        if not all(self.promotion.get(k) is True for k in ("source_fenced", "shutdown_checkpoint_replayed", "target_original_process_preserved", "strict_three_member_quorum_restored", "timeline_advanced")):
            raise ValueError("Do not route to an unverified new writer")
        self.route("set server db/primary addr 127.0.0.12 port 5432")
        context["test"].assertFalse(self.routed_to("127.0.0.12"))
        self.route("set server db/primary state ready")
        context["test"].assertTrue(self.routed_to("127.0.0.12"))
        core = self.service_clients["harbor-core"]
        auth = "Basic " + base64.b64encode(("admin:" + self.admin_password).encode()).decode()
        context["wait"](lambda: core.request("GET", "/api/v2.0/projects/service-initial", authorization=auth)[0] == 200, seconds=20)
        self.service_workload(context, "database-promoted")

    def exercise(self, context):
        result = super().exercise(context)
        if not getattr(self, "promotion", None) or not getattr(self, "database_route_fenced", False): raise ValueError("Require an actual route-fenced promotion")
        result.update(valkey_gateway_harbor_planned_database_promotion_component_verified=True,
            valkey_gateway_harbor_database_old_route_sessions_fenced_verified=True,
            valkey_gateway_harbor_database_shutdown_checkpoint_replayed=True, valkey_gateway_harbor_database_strict_quorum_after_promotion_verified=True,
            valkey_gateway_harbor_original_services_after_database_promotion_verified=True,
            valkey_gateway_harbor_automatic_database_failover_verified=False, valkey_gateway_harbor_database_operator_integration_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
