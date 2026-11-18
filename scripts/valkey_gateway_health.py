#!/usr/bin/env python3
"""Fail-closed live Sentinel/primary checks for an optional TLS cache gateway."""

from concurrent.futures import ThreadPoolExecutor
import argparse
import http.server
from pathlib import Path
import ssl
import threading
import time

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
import verify_valkey_runtime as observer


def winner(network, specification):
    """Require two original authenticated Sentinels, then a healthy original primary."""
    contract, plan = specification["contract"], specification["observer"]
    names = set(plan["members"]); cache = contract["plan"]["cache_name"]
    aliases = {alias: name for name, member in plan["members"].items() for alias in member["addresses"]}
    def resolve(address, port):
        if address not in aliases or str(port) != "6380": raise ValueError("Discovery points outside the reviewed cache")
        return aliases[address]
    def vote(name):
        try:
            with network.connection(name, "sentinel") as client:
                if client.command("PING") != "PONG" or client.command("SENTINEL", "MYID") != plan["members"][name]["sentinel_id"]: return None
                state = observer.pairs(client.command("SENTINEL", "MASTER", cache))
                target = client.command("SENTINEL", "GET-MASTER-ADDR-BY-NAME", cache)
                quorum = client.command("SENTINEL", "CKQUORUM", cache)
            if (not isinstance(target, list) or len(target) != 2 or state.get("name") != cache or state.get("flags") != "master"
                    or not isinstance(quorum, str) or not quorum.startswith("OK ") or state.get("quorum") != "2"
                    or resolve(state.get("ip"), state.get("port")) != resolve(*target)): return None
            return resolve(*target), observer.number(state.get("config-epoch"))
        except (OSError, ValueError, observer.ReplyError): return None
    with ThreadPoolExecutor(max_workers=3) as executor:
        votes = list(executor.map(vote, sorted(names)))
    agreed = [value for value in set(v for v in votes if v is not None) if votes.count(value) >= 2]
    if len(agreed) != 1: return None
    primary = agreed[0][0]
    with network.connection(primary, "server") as client:
        server = observer.info(client.command("INFO", "server")); role = client.command("ROLE")
        replication = observer.info(client.command("INFO", "replication")); persistence = observer.info(client.command("INFO", "persistence"))
        memory = observer.info(client.command("INFO", "memory"))
    if (server.get("run_id") != plan["members"][primary]["server_run_id"] or server.get("valkey_version") != contract["plan"]["version"]
            or not isinstance(role, list) or len(role) != 3 or role[0] != "master" or not isinstance(role[2], list)
            or replication.get("role") != "master" or persistence.get("loading") != "0" or persistence.get("aof_enabled") != "1"
            or persistence.get("aof_last_write_status") != "ok" or persistence.get("aof_last_bgrewrite_status") != "ok"
            or memory.get("maxmemory_policy") != "noeviction" or observer.number(memory.get("maxmemory")) != contract["plan"]["settings"]["maxmemory_mib"] * 1024 ** 2): return None
    copies = {resolve(row[0], row[1]) for row in role[2] if isinstance(row, list) and len(row) == 3}
    if len(copies) != len(role[2]) or primary in copies or observer.number(replication.get("connected_slaves")) != len(copies): return None
    online = set()
    for index in range(len(copies)):
        raw = replication.get("slave" + str(index), ""); fields = raw.split(",")
        if any("=" not in field for field in fields): return None
        row = dict(field.split("=", 1) for field in fields)
        member = resolve(row.get("ip"), row.get("port"))
        if row.get("state") == "online" and observer.number(row.get("lag")) <= contract["plan"]["settings"]["max_lag_seconds"]: online.add(member)
    return primary if online <= copies and len(online) >= contract["plan"]["settings"]["min_replicas"] else None


def observe(specification):
    # Recreate the verifier for every check: no cached up state, stale credentials
    # or operation deadline can survive a stopped checker or changed TLS input.
    plan = specification["observer"]
    observer.validate(plan, specification["contract"])
    network = observer.Network(plan, specification["contract"])
    network.deadline = time.monotonic() + 5
    return winner(network, specification)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    def __init__(self, port, member, specification):
        self.member, self.specification = member, specification
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(("127.0.0.1", port), Handler)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False): self.shutdown_request(request); return
        try: super().process_request(request, address)
        except BaseException: self.slots.release(); raise
    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self): super().setup(); self.connection.settimeout(7)
    def log_message(self, *args): pass
    def do_GET(self):
        ready = False
        if self.path == "/ready":
            try: ready = observe(self.server.specification) == self.server.member
            except (OSError, ValueError, ssl.SSLError, observer.ReplyError): pass
        self.send_response(200 if ready else 503); self.send_header("Content-Length", "0"); self.end_headers()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--specification", type=Path, required=True)
    args = parser.parse_args(argv)
    servers = []
    try:
        specification = loads_strict_json(read_bounded_bytes(args.specification, max_bytes=256 * 1024))
        observer.validate(specification["observer"], specification["contract"])
        members = sorted(specification["observer"]["members"])
        if set(specification) != {"contract", "observer", "health_ports"} or specification["health_ports"] != {name: 19091 + i for i, name in enumerate(members)}: raise ValueError("Use the three fixed private health listeners")
        for member in members:
            server = Server(specification["health_ports"][member], member, specification); servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Event().wait()
    except BaseException:
        for server in servers: server.server_close()
        print("Valkey gateway checker stopped; no cached up state is retained.", file=__import__("sys").stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
