#!/usr/bin/env python3
"""Opt-in real health-gated VRRP/TLS probe in two owned network namespaces."""

import hashlib
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import ssl
import subprocess
import tempfile
import threading
import time

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import render_cilium_gateway as gateway
import render_gateway_edge as renderer
from start_gateway_edge import verify_daemon_version


KEEPALIVED_SHA = "885d32a9bc86e69441d13c8ce9ead7ebc86df60f379421f59477568c86d94bae"
HAPROXY_SHA = "3137dc28d1fa8095ce2610e5736d67cbbdec3a3b15af49bb3ba8631ebe5904ed"
OPTIONS_SHA = "0a71bf7d887fd38ef63d486c7acea36ff9dc7f45a3b20a178c50173cd64097b7"
PACKAGES = {
    "keepalived_2.3.4-1+b2_amd64.deb": "a3c960f7c25c962e91a26e73e1821e14192aba2ff41473613fedd5da4f6c5cd4",
    "libnl-3-200_3.12.0-2+b1_amd64.deb": "a5ca78bbb9d5cc067b78952c5d38b7caac831d42e94b85b91edf1f6f00dfd48a",
    "libnl-genl-3-200_3.12.0-2+b1_amd64.deb": "a4d295d6c273216a2dd6de7890f0f0da0a02963cb3cd366c726028922dc6ee15",
    "libnftnl11_1.3.2-1_amd64.deb": "409b62c0d383ae181e3550d0ec9279359856d98a449d8bce9983f69e1995dbc6",
    "libsnmp45_5.9.5.2+dfsg-3_amd64.deb": "092a0115911eddf0af72e7dae1b4ccf0e70072bd118a4080c1d90ff975b1ffc0",
    "libpci3_3.15.0-2_amd64.deb": "7874977761bfe7adb91847fbb8d82b9c1d9abfc7ad9cc55f361153cac57a5aee",
    "libnl-route-3-200_3.12.0-2+b1_amd64.deb": "e4912a93b339f9f6e72e6330edca1e60d47f64c52d2574b192337a1052656055",
    "libwrap0_7.6.q-37_amd64.deb": "94541fed2d2ecc4654d5cf446c25153ec3908c8f3138c6155717dad69f6528f2",
    "libperl5.42_5.42.3-1_amd64.deb": "f16e5e3de1aefc52cdc95c2132c5939e8af620a0418a01774914d290d813fe7f",
}
VIP = "192.0.2.50"
HOST = "forge.example.com"
REVIEW_HOST = "review.example.invalid"
REVIEW_SSH = {"hostname": REVIEW_HOST, "port": 29418, "node_port": 30294}


def sha(path): return hashlib.sha256(read_bounded_bytes(path, max_bytes=64 * 1024 ** 2)).hexdigest()


def require_scope(*, gerrit=False, joined=False):
    if os.name != "posix" or os.geteuid() != 0: raise ValueError("Use a disposable root Linux runner")
    name = Path("/proc/self/cgroup").read_text().strip()
    prefix = "platform-k3s-vm-gerrit-cilium-vip-" if joined else "platform-gerrit-vip-" if gerrit else "platform-gateway-edge-vip-"
    match = re.fullmatch(r"0::(/system.slice/" + prefix + r"[a-z0-9-]+[.]service)", name)
    if match is None: raise ValueError("Require the original bounded VIP probe service")
    group = Path("/sys/fs/cgroup") / match[1].lstrip("/")
    memory, tasks = (group / "memory.max").read_text().strip(), (group / "pids.max").read_text().strip()
    max_memory, max_tasks, max_seconds = (12288, 4096, 1800) if joined else (4096, 512, 360) if gerrit else (512, 128, 300)
    if not memory.isdigit() or not 0 < int(memory) <= max_memory * 1024 ** 2 or not tasks.isdigit() or not 0 < int(tasks) <= max_tasks:
        raise ValueError("Require bounded memory and tasks for the disposable probe")
    unit = Path(match[1]).name
    observed = run_bounded(["systemctl", "show", unit, "--property=Transient,KillMode,RuntimeMaxUSec,MemorySwapMax,NoNewPrivileges"],
        text=True, check=True, timeout=10, output_max_bytes=65536).stdout
    properties = dict(line.split("=", 1) for line in observed.splitlines() if "=" in line)
    duration = properties.get("RuntimeMaxUSec", "")
    parts = re.findall(r"([0-9]+(?:[.][0-9]+)?)(us|ms|min|s|h)", duration)
    remainder = re.sub(r"[0-9]+(?:[.][0-9]+)?(?:us|ms|min|s|h)", "", duration).strip()
    seconds = sum(float(value) * {"us": 0.000001, "ms": 0.001, "s": 1, "min": 60, "h": 3600}[kind] for value, kind in parts)
    if (remainder or not 0 < seconds <= max_seconds or properties.get("Transient") != "yes"
            or properties.get("KillMode") != "control-group" or properties.get("MemorySwapMax") != "0"
            or properties.get("NoNewPrivileges") != "yes"):
        raise ValueError("Require finite original service ownership, no swap and control-group cleanup")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    caps = {key: status[key].strip() for key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")}
    if any(int(value, 16) & (1 << 25) for value in caps.values()): raise ValueError("The parent probe must not control host time")
    if Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino:
        raise ValueError("Use an original disposable network namespace")
    return group, {"unit": unit, "runtime_limit_seconds": seconds, "memory_limit_bytes": int(memory),
        "tasks_limit": int(tasks), "properties": properties, "parent_capabilities": caps}


class TLSConnection(HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection((VIP, self.port), self.timeout), server_hostname=self.host)


def exercise(test, fixture, *, gerrit=None, joined=None):
    if gerrit is not None and joined is not None:
        raise ValueError("Select one application for the private VIP test")
    group, service = require_scope(gerrit=gerrit is not None, joined=joined is not None)
    sources = {p.name: sha(p) for p in Path(__file__).parent.glob("*.py")}
    standalone = gerrit is None and joined is None
    if standalone:
        report_path = Path(os.environ["GATEWAY_EDGE_VIP_TEST_REPORT"]).resolve()
        private_cache = Path(__file__).resolve().parents[1] / "private"
        if not report_path.is_relative_to(private_cache.resolve()) or report_path.exists(): raise ValueError("Use a new ignored private receipt")
    assets = Path(os.environ["GATEWAY_EDGE_VIP_TEST_ARTIFACTS"]).resolve(strict=True)
    hp_source = Path(os.environ["GATEWAY_EDGE_TEST_HAPROXY"]).resolve(strict=True)
    if sha(hp_source) != HAPROXY_SHA: raise ValueError("Use the original pinned HAProxy")
    processes, servers, threads, samples, attestations = [], [], [], [], []
    stage = "initial-namespace"
    def run(args, *, seconds=15, environment=None, check=True):
        return run_bounded(list(map(str, args)), text=True, input="", check=check, timeout=seconds,
            output_max_bytes=1024 ** 2, env=environment or {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    if joined is None:
        test.assertEqual([row["ifname"] for row in json.loads(run(["ip", "-j", "link", "show"]).stdout)], ["lo"])
    else:
        bridge, = json.loads(run(["ip", "-d", "-j", "address", "show", "dev", "fixturebr0"]).stdout)
        test.assertEqual(bridge["linkinfo"]["info_kind"], "bridge")
        test.assertEqual([(row["local"], row["prefixlen"]) for row in bridge["addr_info"] if row["family"] == "inet"], [("192.0.2.1", 24)])
    test.assertEqual(json.loads(run(["ip", "-j", "route", "show", "default"]).stdout), [])
    with tempfile.TemporaryDirectory(prefix="platform-gateway-edge-vip-", dir="/run") as directory:
        root = Path(directory); root.chmod(0o755)
        public = root / "public"; public.mkdir(); public.chmod(0o755)
        secret = root / "private"; secret.mkdir(mode=0o700)
        clean = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(public), "LC_ALL": "C"}
        def write(path, payload, mode=0o444):
            with path.open("xb") as output: output.write(payload)
            path.chmod(mode)
        def spawn(tag, args, environment=clean):
            log = (secret / (tag + ".log")).open("w+b")
            process = subprocess.Popen(list(map(str, args)), env=environment, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
            row = {"tag": tag, "process": process, "log": log, "stopped": False}; processes.append(row)
            return row
        def alive():
            for row in processes:
                if not row["stopped"] and row["process"].poll() is not None: raise AssertionError("Original isolated process stopped: " + row["tag"])
        def wait(predicate, seconds=30):
            deadline = time.monotonic() + seconds
            while True:
                alive()
                try:
                    result = predicate()
                    if result: return result
                except (OSError, ValueError): pass
                if time.monotonic() > deadline: raise TimeoutError("Original private VIP readiness deadline")
                time.sleep(0.2)
        def stop(row):
            process = row["process"]; row["stopped"] = True
            if process.poll() is None:
                test.assertEqual(os.getpgid(process.pid), process.pid)
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
        try:
            stage = "pinned-artifacts"
            libraries = public / "dependencies"
            for filename, expected in PACKAGES.items():
                source = assets / filename if filename.startswith("keepalived_") else assets / "dependencies" / filename
                payload = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
                if source.is_symlink() or hashlib.sha256(payload).hexdigest() != expected: raise ValueError("Pinned Debian package changed")
                captured = secret / filename; write(captured, payload, 0o600)
                run(["dpkg-deb", "-x", captured, libraries])
            for path in libraries.rglob("*"):
                if path.is_dir() and not path.is_symlink(): path.chmod(0o755)
            libraries.chmod(0o755)
            bins = public / "bin"; bins.mkdir(); bins.chmod(0o755)
            for name, source, expected in (("haproxy", hp_source, HAPROXY_SHA),
                    ("keepalived", libraries / "usr/sbin/keepalived", KEEPALIVED_SHA),
                    ("keepalived.config-opts", libraries / "etc/keepalived/keepalived.config-opts", OPTIONS_SHA)):
                payload = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
                if hashlib.sha256(payload).hexdigest() != expected: raise ValueError("Original executable or build metadata changed")
                write(bins / name, payload, 0o444 if name.endswith("config-opts") else 0o555)
            environment = {**clean, "LD_LIBRARY_PATH": str(libraries / "usr/lib/x86_64-linux-gnu")}
            for name, version in (("haproxy", "3.2.26"), ("keepalived", "2.3.4")):
                verify_daemon_version(bins / name, name, version, environment)
            helper = renderer.health_program().encode("utf-8")
            write(public / "gateway_edge_health.py", helper)
            stage = "original-private-network"
            if joined is None:
                for args in (["ip", "link", "set", "lo", "up"], ["ip", "link", "add", "fixturebr0", "type", "bridge"],
                        ["ip", "addr", "add", "192.0.2.1/24", "dev", "fixturebr0"], ["ip", "link", "set", "fixturebr0", "up"]): run(args)
                for address in ("192.0.2.30", "192.0.2.31"):
                    run(["ip", "addr", "add", address + "/32", "dev", "fixturebr0"])
                    if gerrit is not None:
                        run(["ip", "addr", "del", address + "/32", "dev", "lo"])
            edges = []
            parent_network = Path("/proc/self/ns/net").stat().st_ino
            for suffix, address in (("a", "192.0.2.10"), ("b", "192.0.2.11")):
                keeper = spawn("namespace-" + suffix, ["unshare", "--net", "--", "sleep", "360" if not standalone else "240"])
                pid = keeper["process"].pid
                wait(lambda: Path("/proc", str(pid), "ns/net").stat().st_ino != parent_network, seconds=10)
                prefix = ["nsenter", "--target", str(pid), "--net", "--"]
                test.assertEqual([r["ifname"] for r in json.loads(run([*prefix, "ip", "-j", "link", "show"]).stdout)], ["lo"])
                run(["ip", "link", "add", "veth-" + suffix, "type", "veth", "peer", "name", "eth0", "netns", str(pid)])
                for args in (["ip", "link", "set", "veth-" + suffix, "master", "fixturebr0"], ["ip", "link", "set", "veth-" + suffix, "up"],
                        [*prefix, "ip", "link", "set", "lo", "up"], [*prefix, "ip", "addr", "add", address + "/24", "dev", "eth0"],
                        [*prefix, "ip", "link", "set", "eth0", "up"]): run(args)
                edges.append({"name": suffix, "pid": pid, "prefix": prefix, "network": Path("/proc", str(pid), "ns/net").stat().st_ino})
            test.assertEqual(len({parent_network, *[edge["network"] for edge in edges]}), 3)
            stage = "strict-tls-backends"
            if joined is not None:
                plan = joined["plan"]
                test.assertEqual([host["address"] for host in plan["entry_hosts"]], ["192.0.2.10", "192.0.2.11"])
                test.assertEqual([host["name"] for host in plan["entry_hosts"]], ["edge-a", "edge-b"])
                files = renderer.render(plan, joined["gateway"], "raw")
                joined["preflight"](files)
                write(public / "ca.crt", joined["ca"])
                write(public / "haproxy.cfg", files["haproxy.cfg"].replace(
                    renderer.native_ca(plan), str(public / "ca.crt")).replace("global\n", "global\n    nbthread 1\n", 1).encode())
                for edge in edges:
                    text = files[f"keepalived-edge-{edge['name']}.conf"].replace(
                        renderer.BASE + "/gateway_edge_health.py", str(public / "gateway_edge_health.py"))
                    write(public / f"keepalived-{edge['name']}.conf", text.encode())
                health_identity = renderer.edge_identity(plan)
                probe_flags = []
            elif standalone:
                run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Private edge fixture CA",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", secret / "ca.key", "-out", public / "ca.crt"])
                run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + HOST, "-keyout", secret / "server.key", "-out", secret / "server.csr"])
                write(secret / "server.ext", b"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:forge.example.com,DNS:rpc.example.com,DNS:review.example.invalid\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n", 0o600)
                run(["openssl", "x509", "-req", "-in", secret / "server.csr", "-CA", public / "ca.crt", "-CAkey", secret / "ca.key",
                    "-set_serial", "2", "-days", "1", "-extfile", secret / "server.ext", "-out", public / "server.crt"])
                (public / "ca.crt").chmod(0o444)
                server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); server_tls.load_cert_chain(public / "server.crt", secret / "server.key")
                backend_state = {"healthy": True, "ssh": True}
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def do_GET(self):
                        body = self.server.identity.encode()
                        self.send_response(503 if self.path == "/healthz" and not backend_state["healthy"] else 200)
                        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                class Server(ThreadingHTTPServer):
                    daemon_threads = True
                    def get_request(self):
                        client, address = self.socket.accept(); client.settimeout(2)
                        try: return server_tls.wrap_socket(client, server_side=True), address
                        except BaseException: client.close(); raise
                    def handle_error(self, *args): pass
                class SSHBanner(BaseRequestHandler):
                    def handle(self):
                        if backend_state["ssh"]:
                            self.request.sendall(b"SSH-2.0-fixture-gerrit\r\n")
                for suffix, address in (("a", "192.0.2.30"), ("b", "192.0.2.31")):
                    https = Server((address, 9443), Handler); https.identity = "backend-" + suffix
                    review = Server((address, 9444), Handler); review.identity = "review-" + suffix
                    ssh = ThreadingTCPServer((address, REVIEW_SSH["node_port"]), SSHBanner)
                    for server in (https, review, ssh):
                        servers.append(server)
                        thread = threading.Thread(target=server.serve_forever, daemon=True)
                        threads.append(thread)
                        thread.start()
                plan, original_gateway, _ = fixture(public, "raw", gerrit_ssh=REVIEW_SSH)
                gateway_plan = original_gateway["plan"]
                gateway_plan.update(https_port=9443, tls_passthrough_port=9444)
                gateway_files = gateway.render(gateway_plan, "raw"); gateway_contract = json.loads(gateway_files["deployment-contract.json"])
                plan.update(https_port=8443, client_cidrs=["192.0.2.0/24"], ca_sha256=sha(public / "ca.crt"),
                    gateway_contract_sha256=hashlib.sha256(gateway_files["deployment-contract.json"].encode()).hexdigest())
                plan["binaries"]["haproxy"]["sha256"] = HAPROXY_SHA; plan["binaries"]["keepalived"].update(sha256=KEEPALIVED_SHA, config_opts_sha256=OPTIONS_SHA)
                files = renderer.render(plan, gateway_contract, "raw")
                hp_config = files["haproxy.cfg"].replace(renderer.native_ca(plan), str(public / "ca.crt"))
                hp_config = hp_config.replace(renderer.CA, str(public / "ca.crt")).replace("global\n", "global\n    nbthread 1\n", 1)
                write(public / "haproxy.cfg", hp_config.encode())
                for edge in edges:
                    text = files["keepalived-edge-" + edge["name"] + ".conf"].replace(renderer.BASE + "/gateway_edge_health.py", str(public / "gateway_edge_health.py"))
                    write(public / ("keepalived-" + edge["name"] + ".conf"), text.encode())
                health_identity = renderer.edge_identity(plan)
                probe_flags = []
            else:
                import pwd

                from check_gerrit_native_vip import check as check_vip_host
                from render_gerrit_router import CA_FILE, HEALTH_HELPER, NATIVE_CONFIG, render as render_router, route_config_id

                vrrp = {"vip": VIP + "/24", "vrid": 73, "entry_hosts": [
                    {"name": edge["name"], "address": "192.0.2." + str(10 + index),
                     "interface": "eth0", "failure_domain": "fixture-" + edge["name"]}
                    for index, edge in enumerate(edges)]}
                plan = {**gerrit["plan"], "native_vip": vrrp}
                files = render_router(plan, "raw")
                write(public / "ca.crt", gerrit["ca"])
                hp_config = files["haproxy.cfg"].replace(CA_FILE, str(public / "ca.crt"))
                hp_config = hp_config.replace("global\n", "global\n    nbthread 1\n", 1)
                write(public / "haproxy.cfg", hp_config.encode())
                health_identity = route_config_id(plan)
                probe_flags = ["--gerrit"]
                test.assertEqual(files["gateway_edge_health.py"].encode("utf-8"), helper)
                for edge in edges:
                    filename = "keepalived-" + edge["name"] + ".conf"
                    config = files[filename].replace(HEALTH_HELPER, str(public / "gateway_edge_health.py"))
                    write(public / filename, config.encode())
                    # Map the native paths into a private, root-owned lab tree.
                    # Run the same host check with the original daemon binaries.
                    host_root = public / ("host-" + edge["name"])
                    host_root.mkdir(mode=0o755)
                    host_root.chmod(0o755)
                    trust_dir = host_root / "backend-ca"
                    trust_dir.mkdir(mode=0o755)
                    trust_dir.chmod(0o755)
                    write(trust_dir / "ca.crt", gerrit["ca"])
                    for name, value in (
                        ("haproxy.cfg", files["haproxy.cfg"]),
                        ("gateway_edge_health.py", files["gateway_edge_health.py"]),
                        ("gerrit-router-vrrp.conf", files[filename]),
                        ("gerrit-router.service", files["gerrit-router.service"]),
                        ("gerrit-router-vrrp.service", files["gerrit-router-vrrp.service"]),
                    ):
                        write(host_root / name, value.encode())

                    def host_command(argv, *, paths=host_root, selected=edge, keepalived_filename=filename):
                        # Only the parser invocation sees these two path
                        # relocations. Installed-file checks use original bytes.
                        relocated = {
                            str(paths / "haproxy.cfg"): str(public / "haproxy.cfg"),
                            str(paths / "gerrit-router-vrrp.conf"): str(public / keepalived_filename),
                        }
                        return run([*selected["prefix"], *[relocated.get(arg, arg) for arg in argv]],
                                   environment=environment, check=False)

                    report = check_vip_host(plan, edge["name"], gerrit["ca"], config=host_root, units=host_root,
                                            haproxy=bins / "haproxy", keepalived=bins / "keepalived",
                                            haproxy_user=pwd.getpwuid(65534), execute=host_command)
                    test.assertTrue(report["host_preflight_passed"], report)
                print(json.dumps({"gerrit_native_vip_host_preflight_verified": 2}), flush=True)
            original_configs = {name: sha(public / name) for name in ("haproxy.cfg", "keepalived-a.conf", "keepalived-b.conf", "ca.crt", "gateway_edge_health.py")}
            stage = "real-edge-processes"
            def attest(path, role, edge, row):
                status = dict(line.split(":", 1) for line in (path / "status").read_text().splitlines() if ":" in line)
                expected_uid = "65534" if role == "haproxy" else "0"
                expected_caps = 0 if role == "haproxy" else (1 << 6) | (1 << 12) | (1 << 13)
                test.assertEqual(set(status["Uid"].split()), {expected_uid})
                test.assertEqual(int(status["CapEff"], 16), expected_caps)
                test.assertEqual(int(status["CapBnd"], 16), expected_caps)
                test.assertEqual(status["NoNewPrivs"].strip(), "1")
                network = (path / "ns/net").stat().st_ino; test.assertEqual(network, edge["network"])
                attestations.append({"tag": row["tag"], "pid": row["process"].pid, "uid": int(expected_uid),
                    "effective_capabilities": status["CapEff"].strip(), "bounding_capabilities": status["CapBnd"].strip(),
                    "no_new_privileges": True, "network_namespace_inode": network})
            def hp_start(edge, suffix=""):
                row = spawn("haproxy-" + edge["name"] + suffix, [*edge["prefix"], "setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
                    "--no-new-privs", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", bins / "haproxy", "-W", "-db", "-f", public / "haproxy.cfg"], environment)
                edge["haproxy"] = row
                def identity():
                    process = row["process"]; path = Path("/proc", str(process.pid))
                    if (path / "exe").resolve() != bins / "haproxy": return False
                    attest(path, "haproxy", edge, row)
                    return True
                wait(identity, seconds=10)
            def ready(edge):
                return run([*edge["prefix"], "python3", "-I", "-B", public / "gateway_edge_health.py", "--port", "8404", "--identity", health_identity, *probe_flags], check=False).returncode == 0
            for edge in edges: hp_start(edge)
            wait(lambda: all(ready(edge) for edge in edges))
            for edge in edges:
                row = spawn("keepalived-" + edge["name"], [*edge["prefix"], "setpriv", "--reuid=0", "--regid=0", "--clear-groups", "--no-new-privs",
                    "--bounding-set=-all,+net_admin,+net_raw,+setgid", "--inh-caps=-all", "--ambient-caps=-all", bins / "keepalived", "--dont-fork", "--log-console", "--vrrp",
                    *(["--no_bfd"] if not standalone else []),
                    "--use-file", public / ("keepalived-" + edge["name"] + ".conf"), "--pid=" + str(secret / (edge["name"] + ".pid")),
                    "--vrrp_pid=" + str(secret / (edge["name"] + "-vrrp.pid"))], environment)
                def identity(edge=edge, row=row):
                    path = Path("/proc", str(row["process"].pid))
                    if (path / "exe").resolve() != bins / "keepalived": return False
                    attest(path, "keepalived", edge, row)
                    return True
                wait(identity, seconds=10)
            def current_owners():
                alive()
                owners = []
                for edge in edges:
                    rows = json.loads(run([*edge["prefix"], "ip", "-j", "-4", "addr", "show", "dev", "eth0"]).stdout)
                    if any(ip.get("local") == VIP for row in rows for ip in row.get("addr_info", [])):
                        owners.append(edge["name"])
                test.assertLessEqual(len(owners), 1, "Two live reachable edges own the VIP")
                return owners

            if joined is not None:
                stage = "joined-application-vip"
                report = joined["verify"](edges, current_owners, wait, stop, hp_start)
            elif gerrit is not None:
                stage = "gerrit-client-vip"
                report = gerrit["verify"](edges, current_owners, wait, stop, hp_start)
            else:
                trust = ssl.create_default_context(cafile=str(public / "ca.crt")); trust.verify_flags |= ssl.VERIFY_X509_STRICT
                def request(context=trust, host=HOST):
                    connection = TLSConnection(host, 8443, context=context, timeout=1)
                    try:
                        connection.request("GET", "/", headers={"Connection": "close"}); response = connection.getresponse()
                        test.assertEqual(response.status, 200); body = response.read(100)
                        expected = (b"review-a", b"review-b") if host == REVIEW_HOST else (b"backend-a", b"backend-b")
                        test.assertIn(body, expected); return body.decode()
                    finally: connection.close()
                def ssh_banner():
                    with socket.create_connection((VIP, REVIEW_SSH["port"]), timeout=2) as raw:
                        with raw.makefile("rb") as wire:
                            return wire.readline(256) == b"SSH-2.0-fixture-gerrit\r\n"
                def observe():
                    owners = current_owners()
                    try: body = request() if owners else None
                    except (OSError, ssl.SSLError): body = None
                    test.assertLess(len(samples), 600); samples.append({"stage": stage, "owners": owners, "https_ok": body is not None})
                    return owners, body
                def serving(expected=None):
                    owners, body = observe()
                    return owners[0] if len(owners) == 1 and body and (expected is None or owners == [expected]) else False
                stage = "single-owner-verified-tls"
                master = wait(serving)
                test.assertEqual({request() for _ in range(6)}, {"backend-a", "backend-b"})
                test.assertTrue(request(host=REVIEW_HOST))
                test.assertTrue(ssh_banner())
                with test.assertRaises(ssl.SSLCertVerificationError): request(ssl.create_default_context())
                stage = "health-gated-live-peer-failover"
                failed = next(edge for edge in edges if edge["name"] == master); other = next(edge for edge in edges if edge is not failed)
                started = time.monotonic(); stop(failed["haproxy"])
                wait(lambda: serving(other["name"])); failover_seconds = time.monotonic() - started
                test.assertTrue(request(host=REVIEW_HOST))
                test.assertTrue(ssh_banner())
                hp_start(failed, "-recovered"); wait(lambda: ready(failed))
                for _ in range(5): test.assertEqual(serving(other["name"]), other["name"]); time.sleep(0.2)
                stage = "route-health-threshold-loss-withdrawal"
                backend_state["healthy"] = False
                wait(lambda: observe()[0] == [])
                test.assertTrue(all(not ready(edge) for edge in edges))
                for _ in range(3): test.assertEqual(observe()[0], []); time.sleep(0.2)
                stage = "route-health-recovery"
                backend_state["healthy"] = True; wait(lambda: all(ready(edge) for edge in edges)); wait(serving)
                stage = "ssh-only-loss-withdrawal"
                backend_state["ssh"] = False
                wait(lambda: observe()[0] == [])
                test.assertTrue(all(not ready(edge) for edge in edges))
                stage = "ssh-health-recovery"
                backend_state["ssh"] = True
                wait(lambda: all(ready(edge) for edge in edges))
                wait(serving)
                test.assertTrue(ssh_banner())
            test.assertEqual(json.loads(run(["ip", "-j", "route", "show", "default"]).stdout), [])
            for edge in edges: test.assertEqual(json.loads(run([*edge["prefix"], "ip", "-j", "route", "show", "default"]).stdout), [])
            test.assertEqual((public / "gateway_edge_health.py").read_bytes(), helper)
            test.assertEqual(original_configs, {name: sha(public / name) for name in original_configs})
            memory_events = {row.split()[0]: int(row.split()[1]) for row in (group / "memory.events").read_text().splitlines()}
            test.assertEqual(memory_events.get("oom_kill", 0), 0); test.assertEqual(memory_events.get("oom", 0), 0)
            if standalone:
                traffic = {phase: {"samples": sum(s["stage"] == phase for s in samples),
                    "https_failed": sum(s["stage"] == phase and not s["https_ok"] for s in samples),
                    "no_vip_owner": sum(s["stage"] == phase and not s["owners"] for s in samples)} for phase in sorted({s["stage"] for s in samples})}
                report = {"private_vip_health_failover_verified": True, "actual_strict_tls_through_vip_verified": True,
                    "untrusted_client_ca_refused_verified": True, "route_health_threshold_loss_withdraws_vip_verified": True,
                    "gerrit_ssh_banner_through_vip_and_peer_failover_verified": True, "ssh_only_loss_withdraws_vip_verified": True,
                    "route_health_recovery_verified": True, "sampled_duplicate_vip_owners": 0, "samples": len(samples),
                    "https_samples_failed": sum(not sample["https_ok"] for sample in samples), "fault_to_peer_serving_seconds": round(failover_seconds, 3),
                    "edge_network_namespaces": 2, "haproxy_nonroot_zero_capabilities_verified": True, "keepalived_restricted_capabilities_verified": True,
                    "original_service": service, "process_attestations": attestations, "namespace_keeper_pids": [edge["pid"] for edge in edges],
                    "edge_namespace_inodes": [edge["network"] for edge in edges], "parent_namespace_inode": parent_network,
                    "observations_by_stage": traffic, "original_runtime_configs_sha256": original_configs,
                    "parent_cgroup_memory_events": memory_events, "original_scope_path": str(root),
                    "pinned_debian_packages_sha256": PACKAGES, "haproxy_sha256": HAPROXY_SHA, "keepalived_sha256": KEEPALIVED_SHA,
                    "original_build_metadata_sha256": OPTIONS_SHA, "rendered_edge_contract_sha256": hashlib.sha256(files["edge-contract.json"].encode()).hexdigest(),
                    "laboratory_config_relocations": ["private CA path", "private original health-helper path", "one HAProxy thread"],
                    "physical_node_ha_verified": False, "network_partition_fencing_verified": False, "grpc_application_verified": False,
                    "production_host_lifecycle_verified": False, "full_stack_verified": False, "publisher_signatures_verified": False}
            else:
                report.update(original_service=service, process_attestations=attestations,
                              original_runtime_configs_sha256=original_configs,
                              original_scope_path=str(root), edge_namespace_inodes=[edge["network"] for edge in edges],
                              namespace_keeper_pids=[edge["pid"] for edge in edges],
                              original_build_metadata_sha256=OPTIONS_SHA, pinned_debian_packages_sha256=PACKAGES,
                              haproxy_sha256=HAPROXY_SHA, keepalived_sha256=KEEPALIVED_SHA,
                              ceph_storage_verified=False, network_partition_fencing_verified=False,
                              physical_node_ha_verified=False, full_stack_verified=False)
        except Exception as error:
            signals, messages = {}, {}
            for row in processes:
                row["log"].flush(); row["log"].seek(0); text = row["log"].read(65536).decode(errors="replace").casefold()
                signals[row["tag"]] = {needle: needle in text for needle in ("permission denied", "not permitted", "unknown", "invalid", "failed", "entering master", "entering backup", "script")}
                # This isolated fixture's daemons receive only public synthetic
                # configuration and CA certificates, never keys or API tokens.
                messages[row["tag"]] = [line.replace(str(root), "<private-scope>")[:350]
                    for line in text.splitlines() if any(word in line for word in ("[alert]", "[warning]", "error", "failed", "unable", "cannot"))][-6:]
            print(json.dumps({"private_vip_failure_stage": stage, "exception_type": type(error).__name__, "log_signals": signals,
                "synthetic_component_messages": messages}), flush=True)
            raise
        finally:
            for row in reversed(processes): stop(row); row["log"].close()
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=3)
        if standalone:
            test.assertEqual({int(pid) for pid in (group / "cgroup.procs").read_text().split()}, {os.getpid()})
    test.assertFalse(root.exists())
    test.assertEqual(sources, {p.name: sha(p) for p in Path(__file__).parent.glob("*.py")})
    report["source_files_sha256"] = sources
    report["temporary_processes_and_scope_removed"] = True
    if not standalone:
        return report
    descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output: json.dump(report, output, sort_keys=True)
    return report
