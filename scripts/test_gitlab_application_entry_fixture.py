"""Opt-in real GitLab entry VIP probe with synthetic HTTPS and PROXY-v2 SSH backends."""

from contextlib import ExitStack
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
import struct
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import gitlab_application_entry as entry
from render_gitlab_repository_hosts import render
from test_gitlab_application_entry import plan
from test_gateway_edge_vip_fixture import HAPROXY_SHA, KEEPALIVED_SHA, OPTIONS_SHA, PACKAGES, require_scope


VIP = "192.0.2.40"
HOST = "git.example.org"


def exercise(test):
    group, scope = require_scope()
    output = Path(os.environ["GITLAB_ENTRY_TEST_REPORT"]).resolve()
    output.relative_to((Path.cwd() / "private/validation").resolve())
    assert not output.exists() and output.suffix == ".json"

    def run(args, **kwargs):
        return run_bounded(list(map(str, args)), text=True, timeout=15, output_max_bytes=65536, check=True, **kwargs)

    assert [row["ifname"] for row in json.loads(run(["ip", "-j", "link"]).stdout)] == ["lo"]
    assert not json.loads(run(["ip", "-j", "route", "show", "default"]).stdout)
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("scripts").glob("*.py")}
    children, servers, threads, observations, attestations = [], [], [], [], []
    stage = "prepare"
    with TemporaryDirectory(prefix="platform-gitlab-entry-", dir="/run") as temporary, ExitStack() as files:
        root = Path(temporary)
        root.chmod(0o755)
        public, private = root / "public", root / "private"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        private.mkdir(mode=0o700)

        def spawn(name, args, *, env=None):
            log = private / (name + ".log")
            stream = files.enter_context(log.open("wb"))
            process = subprocess.Popen(list(map(str, args)), stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                       env=env, start_new_session=True)
            child = {"process": process, "name": name, "stopped": False, "log": log}
            children.append(child)
            return child

        def stop(child):
            child["stopped"] = True
            process = child["process"]
            if process.poll() is None:
                assert os.getpgid(process.pid) == process.pid
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

        def wait(predicate, seconds=35):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                for child in children:
                    if not child["stopped"] and child["process"].poll() is not None:
                        raise AssertionError("Disposable process stopped: " + child["name"])
                try:
                    value = predicate()
                    if value:
                        return value
                except (OSError, ssl.SSLError):
                    pass
                time.sleep(0.2)
            raise TimeoutError("GitLab entry fixture readiness deadline: " + stage)

        try:
            stage = "pinned-daemons"
            assets = Path("private/validation/gateway-edge")
            libraries = public / "libraries"
            for name, expected in PACKAGES.items():
                source = assets / "keepalived-2.3.4-debian" / ("" if name.startswith("keepalived_") else "dependencies") / name
                payload = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
                assert hashlib.sha256(payload).hexdigest() == expected and not source.is_symlink()
                archive = private / name
                archive.write_bytes(payload)
                run(["dpkg-deb", "-x", archive, libraries])
            for target in [libraries, *[p for p in libraries.rglob("*") if p.is_dir() and not p.is_symlink()]]:
                target.chmod(0o755)
            hp, ka = public / "haproxy", public / "keepalived"
            for target, source, expected in (
                (hp, assets / "haproxy-3.2.26/haproxy", HAPROXY_SHA),
                (ka, libraries / "usr/sbin/keepalived", KEEPALIVED_SHA),
                (public / "keepalived.config-opts", libraries / "etc/keepalived/keepalived.config-opts", OPTIONS_SHA),
            ):
                payload = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
                assert hashlib.sha256(payload).hexdigest() == expected
                target.write_bytes(payload)
                target.chmod(0o555)
            environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
                           "LD_LIBRARY_PATH": str(libraries / "usr/lib/x86_64-linux-gnu")}
            stage = "isolated-network"
            for args in (["ip", "link", "set", "lo", "up"], ["ip", "link", "add", "fixturebr0", "type", "bridge"],
                         ["ip", "addr", "add", "192.0.2.1/24", "dev", "fixturebr0"], ["ip", "link", "set", "fixturebr0", "up"]):
                run(args)
            for address in ("192.0.2.31", "192.0.2.32"):
                run(["ip", "addr", "add", address + "/32", "dev", "fixturebr0"])
            edges = []
            parent_namespace = Path("/proc/self/ns/net").stat().st_ino
            for host in plan()["application_vip"]["entry_hosts"]:
                keeper = spawn(host["name"] + "-namespace", ["unshare", "--net", "--", "sleep", "290"])
                pid = keeper["process"].pid
                wait(lambda: Path(f"/proc/{pid}/ns/net").stat().st_ino != parent_namespace, seconds=5)
                prefix = ["nsenter", "-t", str(pid), "--net", "--"]
                link = host["name"]
                for args in (["ip", "link", "add", link, "type", "veth", "peer", "name", "eth0", "netns", str(pid)],
                             ["ip", "link", "set", link, "master", "fixturebr0"], ["ip", "link", "set", link, "up"],
                             [*prefix, "ip", "link", "set", "lo", "up"],
                             [*prefix, "ip", "addr", "add", host["address"] + "/24", "dev", "eth0"],
                             [*prefix, "ip", "link", "set", "eth0", "up"]):
                    run(args)
                edges.append({"host": host, "prefix": prefix, "pid": pid})
            stage = "synthetic-backend-certificates"
            run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Disposable entry CA",
                 "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                 "-keyout", private / "ca.key", "-out", public / "ca.crt"])
            (public / "ca.crt").chmod(0o444)
            contexts = {}
            for index, hostname in enumerate((HOST, "wrong.example.org"), 2):
                run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + hostname,
                     "-keyout", private / f"{index}.key", "-out", private / f"{index}.csr"])
                extension = private / "leaf.ext"
                extension.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                     "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:" + hostname + "\n")
                run(["openssl", "x509", "-req", "-in", private / f"{index}.csr", "-CA", public / "ca.crt",
                     "-CAkey", private / "ca.key", "-set_serial", str(index), "-days", "1", "-extfile", extension,
                     "-out", public / f"{index}.crt"])
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(public / f"{index}.crt", private / f"{index}.key")
                contexts[hostname] = context
            pem = public / "front.pem"
            pem.write_bytes((public / "2.crt").read_bytes() + (private / "2.key").read_bytes())
            pem.chmod(0o444)  # Disposable generated key, readable by the unprivileged fixture HAProxy.
            state = {"https": True, "ssh": True, "certificate": HOST}
            proxy_clients = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    body = json.dumps({"host": self.headers.get("Host"), "client": self.headers.get("X-Forwarded-For")}).encode()
                    self.send_response(200 if state["https"] else 503)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            class HTTPS(ThreadingHTTPServer):
                daemon_threads = True

                def handle_error(self, request, client_address):
                    pass

                def get_request(self):
                    wire, address = super().get_request()
                    wire.settimeout(3)
                    try:
                        return contexts[state["certificate"]].wrap_socket(wire, server_side=True), address
                    except Exception:
                        wire.close()
                        raise

            class SSH(BaseRequestHandler):
                def handle(self):
                    self.request.settimeout(3)
                    with self.request.makefile("rb") as stream:
                        header = stream.read(16)
                        if len(header) != 16 or header[:12] != b"\r\n\r\n\x00\r\nQUIT\n" or header[12] not in (0x20, 0x21):
                            return
                        length = struct.unpack("!H", header[14:])[0]
                        if length > 1024:
                            return
                        address = stream.read(length)
                        if len(address) != length:
                            return
                        if header[12:14] == b"\x21\x11" and length >= 12:
                            proxy_clients.append(socket.inet_ntoa(address[:4]))
                        if state["ssh"]:
                            self.request.sendall(b"SSH-2.0-gitlab-entry-fixture\r\n")

            class SSHServer(ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

                def handle_error(self, request, client_address):
                    pass

            for address in ("192.0.2.31", "192.0.2.32"):
                for server in (HTTPS((address, 8443), Handler), SSHServer((address, 2222), SSH)):
                    servers.append(server)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    threads.append(thread)
                    thread.start()
            stage = "rendered-entry-startup-without-vip"
            rendered = render(plan(), "raw")
            config = public / "haproxy.cfg"
            config.write_text("global\n    nbthread 1\n" + rendered["gitlab-application-haproxy.cfg"].replace(
                "/etc/haproxy/gitlab-public.pem", str(pem)).replace("/etc/haproxy/gitlab-rails-ca.crt", str(public / "ca.crt")))
            config.chmod(0o444)
            helper = public / "health.py"
            helper.write_text(rendered["gitlab-entry-health.py"])
            config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
            run([hp, "-c", "-f", config])

            def attest(edge, child, role):
                proc = Path("/proc") / str(child["process"].pid)
                if (proc / "exe").resolve() != (hp if role == "haproxy" else ka):
                    return False
                status = dict(row.split(":", 1) for row in (proc / "status").read_text().splitlines() if ":" in row)
                caps = 0 if role == "haproxy" else (1 << 6) | (1 << 12) | (1 << 13)
                assert set(status["Uid"].split()) == {"65534" if role == "haproxy" else "0"}
                assert int(status["CapEff"].strip(), 16) == caps
                assert int(status["CapBnd"].strip(), 16) == caps
                assert status["NoNewPrivs"].strip() == "1"
                namespace = (proc / "ns/net").stat().st_ino
                assert namespace == Path(f"/proc/{edge['pid']}/ns/net").stat().st_ino != parent_namespace
                attestations.append({"role": role, "peer": edge["host"]["name"], "uid": int(status["Uid"].split()[0]),
                                     "effective_capabilities": caps, "network_namespace": namespace})
                return True

            def start_haproxy(edge, suffix=""):
                edge["haproxy"] = spawn(edge["host"]["name"] + "-haproxy" + suffix,
                    [*edge["prefix"], "setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs",
                     "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", hp, "-db", "-f", config], env=environment)
                wait(lambda: attest(edge, edge["haproxy"], "haproxy"), seconds=5)

            def ready(edge):
                return run_bounded([*edge["prefix"], "/usr/bin/python3", "-I", "-B", str(helper), "--port", "8404",
                                    "--identity", entry.identity(plan())], text=True, timeout=3,
                                   output_max_bytes=8192, check=False).returncode == 0

            for edge in edges:
                start_haproxy(edge)
            wait(lambda: all(ready(edge) for edge in edges))
            for edge in edges:
                name = edge["host"]["name"]
                keep = public / (name + ".conf")
                keep.write_text(rendered[f"keepalived-{name}.conf"].replace(entry.HELPER, str(helper)))
                child = spawn(name + "-keepalived", [*edge["prefix"], "setpriv", "--reuid=0", "--regid=0", "--clear-groups",
                    "--no-new-privs", "--bounding-set=-all,+net_admin,+net_raw,+setgid", "--inh-caps=-all", "--ambient-caps=-all",
                    ka, "--dont-fork", "--log-console", "--vrrp", "--no_bfd", "--use-file", keep,
                    "--pid=" + str(private / (name + ".pid")), "--vrrp_pid=" + str(private / (name + "-vrrp.pid"))], env=environment)
                wait(lambda: attest(edge, child, "keepalived"), seconds=5)

            def owners():
                found = []
                for edge in edges:
                    addresses = json.loads(run([*edge["prefix"], "ip", "-j", "-4", "addr", "show", "dev", "eth0"]).stdout)
                    if any(ip["local"] == VIP for interface in addresses for ip in interface.get("addr_info", [])):
                        found.append(edge["host"]["name"])
                assert len(found) <= 1, "Two reachable entry peers own the VIP"
                observations.append({"stage": stage, "owners": found})
                assert len(observations) <= 1000
                return found

            trust = ssl.create_default_context(cafile=str(public / "ca.crt"))

            class Client(HTTPSConnection):
                def connect(self):
                    self.sock = self._context.wrap_socket(socket.create_connection((VIP, 9443), timeout=2), server_hostname=HOST)

            def request(context=trust):
                connection = Client(HOST, 9443, context=context, timeout=2)
                try:
                    connection.request("GET", "/", headers={"X-Forwarded-For": "203.0.113.99", "Connection": "close"})
                    response = connection.getresponse()
                    assert response.status == 200
                    assert json.loads(response.read(1024)) == {"host": HOST + ":9443", "client": "192.0.2.1"}
                    return True
                finally:
                    connection.close()

            def ssh():
                with socket.create_connection((VIP, 2222), timeout=2) as wire, wire.makefile("rb") as stream:
                    assert stream.readline(256) == b"SSH-2.0-gitlab-entry-fixture\r\n"
                assert "192.0.2.1" in proxy_clients

            stage = "initial-verified-entry"
            owner = wait(lambda: owners())
            wait(request)
            ssh()
            with test.assertRaises(ssl.SSLCertVerificationError):
                request(ssl.create_default_context())
            stage = "active-proxy-loss"
            failed = next(edge for edge in edges if edge["host"]["name"] == owner[0])
            peer = next(edge for edge in edges if edge is not failed)
            stop(failed["haproxy"])
            wait(lambda: owners() == [peer["host"]["name"]] and request())
            ssh()
            start_haproxy(failed, "-recovered")
            wait(lambda: ready(failed))

            def recovered_standby():
                path = private / (failed["host"]["name"] + "-keepalived.log")
                log = read_bounded_bytes(path, max_bytes=65536).decode(errors="replace")
                states = re.findall(r"Entering ([A-Z]+) STATE", log)
                return states and states[-1] == "BACKUP" and owners() == [peer["host"]["name"]]

            wait(recovered_standby)
            stable_until = time.monotonic() + 10
            while time.monotonic() < stable_until:
                assert owners() == [peer["host"]["name"]]
                time.sleep(0.25)
            assert recovered_standby()
            for reason, field, value, original in (
                ("https-only-loss", "https", False, True),
                ("ssh-only-loss", "ssh", False, True),
                ("upstream-hostname-mismatch", "certificate", "wrong.example.org", HOST),
            ):
                stage = reason
                state[field] = value
                wait(lambda: not owners() and all(not ready(edge) for edge in edges))
                state[field] = original
                stage = reason + "-recovered"
                wait(lambda: all(ready(edge) for edge in edges) and owners() and request())
                ssh()
            assert hashlib.sha256(config.read_bytes()).hexdigest() == config_hash
            assert not json.loads(run(["ip", "-j", "route", "show", "default"]).stdout)
            events = dict((k, int(v)) for k, v in (row.split() for row in (group / "memory.events").read_text().splitlines()))
            assert not events.get("oom_kill") and not events.get("oom")
        except Exception:
            print(json.dumps({"failed_stage": stage, "synthetic_process_messages": {
                child["name"]: child["log"].read_text(errors="replace")[-1800:].replace(str(root), "<fixture>")
                for child in children if "namespace" not in child["name"]}}, sort_keys=True), flush=True)
            raise
        finally:
            for child in reversed(children):
                stop(child)
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)
        assert {int(pid) for pid in (group / "cgroup.procs").read_text().split()} == {os.getpid()}
    assert not root.exists()
    assert sources == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("scripts").glob("*.py")}
    report = {"gitlab_entry_vip_process_failover_verified": True, "gitlab_entry_https_and_proxy_v2_ssh_verified": True,
              "gitlab_entry_forwarded_client_spoofing_overwritten": True, "gitlab_entry_https_only_loss_withdraws_vip": True,
              "gitlab_entry_ssh_only_loss_withdraws_vip": True, "gitlab_entry_wrong_upstream_hostname_withdraws_vip": True,
              "gitlab_entry_untrusted_frontend_ca_refused": True, "temporary_processes_and_scope_removed": True,
              "scope": scope, "observations": observations, "source_files_sha256": sources, "process_attestations": attestations,
              "haproxy_sha256": HAPROXY_SHA, "keepalived_sha256": KEEPALIVED_SHA, "memory_events": events,
              "real_gitlab_rails_verified": False, "physical_host_failure_verified": False, "partition_fencing_verified": False}
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(report, stream, sort_keys=True)
    return report


@unittest.skipUnless(os.environ.get("GITLAB_ENTRY_TEST_RUNTIME") == "1", "Select the isolated root Linux VIP fixture explicitly")
class RuntimeTest(unittest.TestCase):
    def test_real_entry_pair(self):
        report = exercise(self)
        print(json.dumps({k: v for k, v in report.items() if k not in {"source_files_sha256", "observations"}}, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
