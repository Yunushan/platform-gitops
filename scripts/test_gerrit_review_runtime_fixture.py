#!/usr/bin/env python3
"""Disposable real Gerrit HTTP-mode server; no company endpoint or worker."""

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from bounded_subprocess import run_bounded
from http_transport import RejectRedirectHandler, read_bounded_response

WAR_SHA = "3e7d3a354e07c5aa90d25a7a3863b3df615515f168afdaf5cff0357b066a9297"
JAVA_ARCHIVE = "OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz"
JAVA_SHA = "a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd"


def artifact(path, digest):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024 ** 2: raise ValueError("Require a pinned bounded public runtime artifact")
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != digest: raise ValueError("Runtime artifact changed")


def worker_inventory(context):
    base = [("fixture-control", "192.0.2.2"), ("fixture-worker-1", "192.0.2.3"), ("fixture-worker-2", "192.0.2.4")]
    selected = context["nodes"]
    if context.get("runtime") == "rke2":
        expected = [*base, ("fixture-agent", "192.0.2.8"), ("fixture-agent-2", "192.0.2.9")]
        edges = context.get("native_edge_hosts")
        if ([(n["name"], n["address"]) for n in selected] != expected
                or [n["memory"] for n in selected] != [2560, 2560, 2560, 1536, 1536]
                or context.get("control_nodes") != selected[:3]
                or context.get("application_workers") != selected[3:]
                or edges is not None and (not isinstance(edges, dict) or edges.get("nodes") != [] or edges.get("inputs") is not None)
                or context.get("admission_workers", False)):
            raise ValueError("Require the original five-guest RKE2 serving profile")
        return selected
    admission = context.get("admission_workers", False)
    if type(admission) is not bool: raise ValueError("Select the explicit three-worker admission profile")
    if admission:
        base.append(("fixture-worker-3", "192.0.2.7"))
        if "native_edge_hosts" in context or len(context.get("control_nodes", selected[:1])) != 1:
            raise ValueError("Admission workers require the separate original four-guest profile")
        if [node.get("memory") for node in selected] != [1536, 2048, 2048, 2048]:
            raise ValueError("Original admission guest resources changed")
    if [(n["name"], n["address"]) for n in selected] != base: raise ValueError("Original worker inventory changed")
    controls = context.get("control_nodes", selected[:1])
    control_ids = [(n["name"], n["address"]) for n in controls]
    if control_ids not in (base[:1], [base[0], ("fixture-control-2", "192.0.2.5"), ("fixture-control-3", "192.0.2.6")]):
        raise ValueError("Original control-plane inventory changed")
    inventory = [*controls, *selected[1:]]
    if "native_edge_hosts" in context:
        chosen = context["native_edge_hosts"]
        expected = [("edge-a", "192.0.2.10", 512), ("edge-b", "192.0.2.11", 512)]
        if len(controls) != 3 or not isinstance(chosen, dict): raise ValueError("Native edge guests require the explicit three-control-plane profile")
        edges = chosen.get("nodes")
        declared = chosen.get("inputs", {}).get("nodes")
        for rows in (edges, declared):
            if (not isinstance(rows, list) or any(not isinstance(node, dict) for node in rows)
                    or [(n.get("name"), n.get("address"), n.get("memory")) for n in rows] != expected):
                raise ValueError("Original native edge inventory changed")
        inventory.extend(edges)
    return inventory


def namespace(worker_context=None):
    if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use the explicit disposable root Linux network namespace")
    rows = json.loads(run_bounded(["/usr/sbin/ip", "-j", "link", "show"], text=True, timeout=10, check=True, output_max_bytes=1024 * 1024).stdout)
    if worker_context is None:
        if len(rows) != 1 or rows[0].get("ifname") != "lo": raise ValueError("Use a new empty network namespace")
    else:
        # This variant is only for the original disposable KVM worker fixture.
        # It admits its exact private LAN, never an ambient host interface.
        from test_k3s_vm_fixture import qmp, zuul_rollout_bridge
        scope = worker_context["root"]
        if scope.is_symlink() or scope.parent != Path("/var/tmp") or not scope.name.startswith("platform-k3s-vm-") or scope.stat().st_uid != 65534:
            raise ValueError("Require the original private VM scope")
        inventory = worker_inventory(worker_context)
        taps = {"fixturetap" + str(index) for index in range(len(inventory))}
        group = Path("/proc/self/cgroup").read_text()
        bridge = "fixturebr0"
        if worker_context.get("zuul_rollout") is True:
            bridge = zuul_rollout_bridge(rows, group.strip().removeprefix("0::"), taps)
        elif {r.get("ifname") for r in rows} != {"lo", bridge, *taps}:
            raise ValueError("Unexpected worker fixture interface")
        routes = json.loads(run_bounded(["/usr/sbin/ip", "-j", "route", "show", "default"], text=True, timeout=10, check=True).stdout)
        if routes: raise ValueError("The worker fixture must have no upstream route")
        addresses = json.loads(run_bounded(["/usr/sbin/ip", "-j", "address", "show", "dev", bridge], text=True, timeout=10, check=True).stdout)
        if len(addresses) != 1 or not any(a.get("local") == "192.0.2.1" and a.get("prefixlen") == 24 for a in addresses[0].get("addr_info", [])): raise ValueError("Original laboratory bridge address changed")
        if not any(line.startswith("0::/system.slice/platform-k3s-vm-") and line.endswith(".service") for line in group.splitlines()): raise ValueError("Require the original bounded VM service")
        for node in inventory:
            if qmp(scope / node["name"] / "qmp.sock", "query-kvm") != {"enabled": True, "present": True}: raise ValueError("Original KVM guest is unavailable")
    run_bounded(["/usr/sbin/ip", "link", "set", "lo", "up"], timeout=10, check=True)


class API:
    def __init__(self, origin, context, users, credentials, *, request_timeout=20, token_lifetime="10m"):
        if type(request_timeout) is not int or request_timeout not in {20, 60}: raise ValueError("Select a bounded fixture request profile")
        if token_lifetime not in {"10m","30m"} and not (token_lifetime=="60m" and os.environ.get('ZUUL_RKE2_SERVING_TEST_RUNTIME')=='1'):
            raise ValueError("Require a finite fixture token lifetime")
        self.token_lifetime=token_lifetime
        self.origin, self.users, self.credentials = origin, users, credentials
        self.request_timeout, self.request_observations = request_timeout, []
        self.context = context
        self.opener = build_opener(ProxyHandler({}), RejectRedirectHandler(), HTTPSHandler(context=context))

    def bootstrap(self, username):
        origin = self.origin
        class LocalRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if not newurl.startswith(origin + "/"): raise ValueError("Bootstrap redirect left the disposable origin")
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        jar = CookieJar()
        opener = build_opener(ProxyHandler({}), LocalRedirect(), HTTPCookieProcessor(jar), HTTPSHandler(context=self.context))
        auth = "Basic " + base64.b64encode((username + ":" + self.users[username]).encode()).decode()
        with opener.open(Request(origin + "/login/", headers={"Authorization": auth}), timeout=self.request_timeout) as response:
            read_bounded_response(response, max_bytes=4 * 1024 ** 2)
        xsrf = [c.value for c in jar if c.name == "XSRF_TOKEN"]
        if len(xsrf) != 1 or not xsrf[0]: raise ValueError("Actual Gerrit bootstrap session has no XSRF token")
        request = Request(origin + "/accounts/self/tokens/fixture", data=json.dumps({"lifetime":self.token_lifetime}).encode(), method="PUT",
            headers={"Authorization": auth, "X-Gerrit-Auth": xsrf[0], "Accept": "application/json", "Content-Type": "application/json"})
        with opener.open(request, timeout=self.request_timeout) as response: raw = read_bounded_response(response, max_bytes=64 * 1024)
        if not raw.startswith(b")]}'\n"): raise ValueError("Generated token response has another format")
        token = json.loads(raw[5:]).get("token")
        if not isinstance(token, str) or not 16 <= len(token) <= 512: raise ValueError("Actual Gerrit generated no bounded token")
        self.users[username] = token
        self.credentials["Basic " + base64.b64encode((username + ":" + token).encode()).decode()] = username
        return self.call(username, "/a/accounts/self")

    def call(self, username, path, payload=None, *, method=None, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else None
        request = Request(self.origin + path, data=data, method=method, headers={"Authorization": "Basic " + base64.b64encode((username + ":" + self.users[username]).encode()).decode(), "Accept": "application/json", "Content-Type": content_type})
        if len(self.request_observations) >= 512: raise ValueError("Fixture request observation limit")
        resource = path.split("?", 1)[0].strip("/").split("/")
        resource = resource[1] if len(resource) > 1 and resource[0] == "a" else resource[0]
        observation = {"method": request.get_method(), "resource": resource if resource in {"accounts", "groups", "projects", "changes"} else "other", "completed": False}
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.request_timeout) as response: raw = read_bounded_response(response, max_bytes=4 * 1024 ** 2)
            observation["completed"] = True
        except HTTPError as error:
            error.close()
            raise
        finally:
            observation["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.request_observations.append(observation)
        if not raw: return None
        if not raw.startswith(b")]}'\n"): raise ValueError("Actual Gerrit response has no protection prefix")
        return json.loads(raw[5:])


def backend_ready(port, authority):
    """Probe the real, anonymous, read-only REST version before account writes."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/config/server/version", headers={"Host": authority, "X-Forwarded-Proto": "https", "X-Forwarded-Port": authority.rsplit(":", 1)[1]})
        response = connection.getresponse(); body = response.read(8193)
        return (response.status == 200 and len(body) <= 8192 and body.startswith(b")]}'\n")
                and json.loads(body[5:]) == "3.13.10")
    except (OSError, http.client.HTTPException, ValueError): return False
    finally: connection.close()


@contextmanager
def server(*, handoff=False, worker_context=None, active_processors=None, ssh=False, observe_config_requests=False,
           prepare_site=None, router_hostname=None, peer_port=None, source_site=None, vip_test=False, disk_workspace=False):
    if type(ssh) is not bool or (ssh and worker_context is not None):raise ValueError('SSH is an explicit loopback-only Gerrit fixture option')
    if type(observe_config_requests) is not bool or (observe_config_requests and worker_context is not None):raise ValueError('Configuration observations are an explicit loopback-only fixture option')
    if prepare_site is not None and (not callable(prepare_site) or worker_context is not None):
        raise ValueError("Post-init site preparation requires the loopback fixture")
    if router_hostname is not None and (router_hostname != "review.example.invalid" or prepare_site is None or not ssh):
        raise ValueError("Use the isolated router fixture hostname with prepared plugins and SSH")
    if (peer_port is not None and (peer_port not in {17081, 17082} or worker_context is not None or prepare_site is None)
            or source_site is not None and peer_port != 17082):
        raise ValueError("Peer forwarding is confined to the loopback fixture")
    routed_peer = source_site is not None and router_hostname is not None
    namespace(worker_context)
    if vip_test and (worker_context is not None or router_hostname is None or peer_port not in {17081, 17082}):
        raise ValueError("VIP backend addresses require the isolated paired fixture")
    if active_processors is not None and (type(active_processors) is not int or active_processors != 2): raise ValueError("Select the explicit two-processor JVM laboratory profile")
    if active_processors is None and worker_context is not None and "native_edge_hosts" in worker_context: active_processors = 2
    request_timeout = 60 if worker_context is not None and "native_edge_hosts" in worker_context else 20
    backend_timeout = 55 if request_timeout == 60 else 20
    artifacts = Path(os.environ["GERRIT_REVIEW_TEST_ARTIFACTS"]).resolve(strict=True)
    java_archive = Path(os.environ["GERRIT_REVIEW_TEST_JAVA_ARCHIVE"]).resolve(strict=True)
    artifact(artifacts / "gerrit-3.13.10.war", WAR_SHA); artifact(java_archive, JAVA_SHA)
    address = "192.0.2.1" if worker_context is not None else "127.0.0.2" if routed_peer else "127.0.0.1"
    if vip_test:
        address = "192.0.2.31" if routed_peer else "192.0.2.30"
    with ExitStack() as cleanup:
        temporary = cleanup.enter_context(tempfile.TemporaryDirectory(
            prefix="platform-gerrit-review-", dir="/var/tmp" if worker_context is not None or disk_workspace else "/tmp"))
        root = Path(temporary); root.chmod(0o700); os.chown(root, 65534, 65534)
        with tarfile.open(java_archive) as archive: archive.extractall(root, filter="data")
        runtime = root / "jdk-21.0.13+9-jre"
        if runtime.is_symlink() or not runtime.is_dir(): raise ValueError("Reviewed Java runtime directory changed")
        # The data filter clears directory modes. Under the fixture's 0077
        # umask those public runtime directories otherwise remain root-only.
        # Keep the surrounding private scope 0700 and normalize only this
        # original checksum-verified distribution's real directories.
        for path in (runtime, *runtime.rglob("*")):
            if not path.is_symlink() and path.is_dir(): path.chmod(0o755)
        java = runtime / "bin/java"
        if not java.is_file(): raise ValueError("Reviewed Java archive has another layout")
        war = root / "gerrit.war"; shutil.copyfile(artifacts / "gerrit-3.13.10.war", war); war.chmod(0o644)
        reservations = [socket.socket() for _ in range(3 if ssh else 2)]
        try:
            for sock, host in zip(reservations, ("127.0.0.1", address, address if vip_test else "127.0.0.1")): sock.bind((host, 0))
            ports = [sock.getsockname()[1] for sock in reservations]
            backend_port, public_port = ports[:2]
            ssh_port = ports[2] if ssh else None
        finally:
            for sock in reservations: sock.close()
        if routed_peer:
            public_port = urlsplit(source_site["origin"]).port
            ssh_port = source_site["ssh_endpoint"]["port"]
        ssh_address = address if routed_peer or vip_test else "127.0.0.1"
        ssh_listen = ssh_address + ":" + str(ssh_port) if ssh else "off"
        origin = "https://" + address + ":" + str(public_port)
        site = root / "site"; (site / "etc").mkdir(parents=True)
        os.chown(site, 65534, 65534); os.chown(site / "etc", 65534, 65534)
        config = site / "etc/gerrit.config"
        config.write_text('[gerrit]\n basePath = git\n canonicalWebUrl = ' + origin + '/\n[auth]\n type = HTTP\n httpHeader = X-Platform-User\n httpEmailHeader = X-Platform-Email\n gitBasicAuthPolicy = HTTP\n[httpd]\n listenUrl = proxy-http://127.0.0.1:' + str(backend_port) + '/\n[sshd]\n listenAddress = ' + ssh_listen + '\n[index]\n type = LUCENE\n[sendemail]\n enable = false\n[container]\n javaOptions = -Xmx1g\n', encoding="utf-8")
        config.chmod(0o600); os.chown(config, 65534, 65534)
        child_program = Path(__file__).with_name("test_rook_api_fixture.py")
        jvm_options = ["-Xms128m", "-Xmx1g", *(["-XX:ActiveProcessorCount=2"] if active_processors == 2 else [])]
        def command(arguments): return [sys.executable, "-B", str(child_program), "--child", str(os.getpid()), str(java), *jvm_options, *arguments]
        jvm_profile = {"active_processor_count": active_processors, "max_heap_bytes": 1024 ** 3, "flags_verified": False}
        if active_processors == 2:
            checked = run_bounded(command(["-XX:+PrintFlagsFinal", "-version"]), cwd=root, timeout=30, output_max_bytes=1024 ** 2, check=True)
            output = checked.stdout.decode() if isinstance(checked.stdout, bytes) else checked.stdout
            for option, expected in (("ActiveProcessorCount", 2), ("MaxHeapSize", 1024 ** 3)):
                values = re.findall(r"\b" + option + r"\s+:?=\s+([0-9]+)\b", output)
                if values != [str(expected)]: raise ValueError("Pinned JVM did not honor the explicit laboratory resource profile")
            jvm_profile["flags_verified"] = True
        # All Java output remains in this disposable private scope. It may
        # contain generated credentials and must never be returned verbatim.
        init = run_bounded(command(["-jar", str(war), "init", "--batch", "--no-auto-start", "--skip-all-downloads", "-d", str(site)]), cwd=root,
            timeout=180, output_max_bytes=4 * 1024 ** 2, check=False)
        if init.returncode:
            output = (init.stdout + init.stderr).decode("utf-8", errors="replace") if isinstance(init.stdout, bytes) else init.stdout + init.stderr
            print(json.dumps({"gerrit_init_return_code": init.returncode, "gerrit_init_error_signals": {signal: signal.casefold() in output.casefold() for signal in (
                "Permission denied", "AccessDeniedException", "UnsatisfiedLinkError", "NoClassDefFoundError", "No such file", "Cannot run program", "native thread", "OutOfMemoryError", "failed to map", "ModuleNotFoundError")}}), flush=True)
            raise ValueError("Actual Gerrit fresh initialization failed (private diagnostic suppressed)")
        # init may normalize configuration; keep explicit mail/auth/listeners.
        for option, value in (("auth.type", "HTTP"), ("auth.httpHeader", "X-Platform-User"), ("auth.httpEmailHeader", "X-Platform-Email"),
            ("auth.gitBasicAuthPolicy", "HTTP"), ("sshd.listenAddress", ssh_listen), ("sendemail.enable", "false")):
            run_bounded(["git", "config", "--file", str(config), option, value], timeout=10, check=True)
        os.chown(config, 65534, 65534)
        tls = root / "tls"; tls.mkdir(mode=0o700)
        san = "subjectAltName=IP:" + address
        if router_hostname is not None:
            san += ",DNS:" + router_hostname
        run_bounded(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=local-fixture", "-addext", san,
            "-keyout", str(tls / "tls.key"), "-out", str(tls / "ca.crt")], timeout=30, output_max_bytes=1024 * 1024, check=True)
        users = {name: secrets.token_urlsafe(36) for name in ("fixture-admin", "ci-read", "ci-review", *(("cd-writer",) if handoff else ()))}
        peer_password = None
        if peer_port is not None:
            peer_password = secrets.token_urlsafe(36) if source_site is None else source_site["peer_password"]
        if source_site is not None:
            users["fixture-admin"] = source_site["users"]["fixture-admin"]
        if prepare_site is not None:
            if peer_port is None:
                prepare_site(site, users["fixture-admin"])
            else:
                prepare_site(site, users["fixture-admin"], peer_password=peer_password)
        if source_site is not None:
            run_bounded(command(["-jar", str(war), "reindex", "-d", str(site), "--threads", "2"]),
                        cwd=root, timeout=90, output_max_bytes=4 * 1024 ** 2, check=True)
        if routed_peer:
            for key in (source_site["site"] / "etc").glob("ssh_host_*_key*"):
                copied = site / "etc" / key.name
                shutil.copyfile(key, copied)
                copied.chmod(0o600)
                os.chown(copied, 65534, 65534)
        credentials = {"Basic " + base64.b64encode((u + ":" + t).encode()).decode(): u for u, t in users.items()}
        peer_authorization = "Basic " + base64.b64encode(("fixture-admin:" + peer_password).encode()).decode() if peer_port else None
        peer_requests = []
        block_forwarding = threading.Event()
        push_interrupt = None
        proxy_errors, proxy_lock = {}, threading.Lock();config_observations=[]
        def proxy_error(stage, error):
            key = stage + "/" + type(error).__name__
            with proxy_lock: proxy_errors[key] = proxy_errors.get(key, 0) + 1
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def forward(self):
                name = credentials.get(self.headers.get("Authorization"))
                peer_request = peer_port is not None and self.server.server_port == peer_port
                if peer_request:
                    supplied = self.headers.get("Authorization", "").encode("latin-1")
                    name = ("fixture-admin" if secrets.compare_digest(supplied, peer_authorization.encode("ascii"))
                            and self.path.startswith(("/a/plugins/high-availability/", "/plugins/high-availability/")) else None)
                def observe_peer(code):
                    if peer_request:
                        with proxy_lock:
                            if len(peer_requests) >= 256:
                                raise ValueError("Peer request observation limit")
                            peer_requests.append((self.command, self.path, code))
                health = (router_hostname is not None and self.command == "GET"
                          and self.path == "/config/server/healthcheck~status" and self.headers.get("Host") == router_hostname)
                if name is None and not health:
                    observe_peer(401)
                    self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="fixture"'); self.send_header("Content-Length", "0"); self.end_headers(); return
                if peer_request and block_forwarding.is_set():
                    observe_peer(503)
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 4 * 1024 ** 2: raise ValueError("Fixture request bound")
                connection = http.client.HTTPConnection("127.0.0.1", backend_port, timeout=backend_timeout)
                try:
                    interrupt = push_interrupt if not peer_request and self.command == "POST" and (
                        self.path == "/a/plugin-canary/git-receive-pack"
                    ) else None
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection", "x-platform-user", "x-platform-email", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port"}}
                    headers.update({"Host": address + ":" + str(public_port), "X-Forwarded-Proto": "https", "X-Forwarded-Port": str(public_port)})
                    if name is not None:
                        headers.update({"X-Platform-User": name, "X-Platform-Email": name + "@example.invalid"})
                    if interrupt is not None and interrupt["phase"] == "request":
                        # Hold after receiving one byte, before opening an upstream
                        # connection. The remainder of the push is still unread.
                        if size <= 1 or len(self.rfile.read(1)) != 1:
                            raise ValueError("Missing interrupted request body")
                        interrupt["reached"].set()
                        interrupt["release"].wait(20)
                        return
                    connection.request(self.command, self.path, body=self.rfile.read(size), headers=headers)
                    response = connection.getresponse(); body = response.read(16 * 1024 ** 2 + 1)
                    if len(body) > 16 * 1024 ** 2: raise ValueError("Fixture response bound")
                    if interrupt is not None and interrupt["phase"] == "response" and response.status == 200:
                        interrupt["reached"].set()
                        interrupt["release"].wait(20)
                        return
                    observe_peer(response.status)
                    if observe_config_requests and self.command=='GET' and self.path in {'/a/config/server/version','/a/config/server/info'}:
                        with proxy_lock:
                            if len(config_observations)>=256:raise ValueError('Bounded configuration request observations exceeded')
                            config_observations.append({'resource':self.path.rsplit('/',1)[1],'user':name,'status':response.status,'observed_monotonic':time.monotonic()})
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"content-length", "transfer-encoding", "connection"}: self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                except Exception as error:
                    proxy_error("backend", error)
                    raise
                finally: connection.close()
            do_GET = forward
            do_POST = forward
            do_PUT = forward
            do_DELETE = forward
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(tls / "ca.crt", tls / "tls.key")
        class ProxyServer(ThreadingHTTPServer):
            def get_request(self):
                try: return super().get_request()
                except OSError as error:
                    proxy_error("tls-accept", error)
                    raise
            def handle_error(self, request, address):
                error = sys.exc_info()[1]
                proxy_error("handler", error)
        public = ProxyServer((address, public_port), Proxy); public.socket = context.wrap_socket(public.socket, server_side=True)
        if peer_port is not None:
            peer = cleanup.enter_context(ProxyServer(("127.0.0.1", peer_port), Proxy))
            peer_thread = threading.Thread(target=peer.serve_forever, daemon=True)
            peer_thread.start()
            cleanup.callback(peer_thread.join, 5)
            cleanup.callback(peer.shutdown)
        public.timeout = 5
        thread = threading.Thread(target=public.serve_forever, daemon=True)
        log = (root / "server.log").open("w+b")

        def start_daemon():
            return subprocess.Popen(command(["-jar", str(war), "daemon", "--console-log", "-d", str(site)]),
                                    cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)

        process = start_daemon()

        @contextmanager
        def interrupt_push(phase):
            nonlocal push_interrupt
            if push_interrupt is not None or phase not in {"request", "response"}:
                raise ValueError("Require exactly one supported interruption phase")
            barrier = {"phase": phase, "reached": threading.Event(), "release": threading.Event()}
            push_interrupt = barrier
            try:
                yield barrier["reached"]
            finally:
                push_interrupt = None
                barrier["release"].set()

        def restart_daemon(*, offline_reindex):
            nonlocal process
            if process.poll() is None:
                raise ValueError("The original fixture daemon must stop before restart")
            if offline_reindex:
                rebuilt = run_bounded(command(["-jar", str(war), "reindex", "-d", str(site),
                                               "--index", "changes", "--threads", "2"]),
                                      cwd=root, timeout=90, output_max_bytes=4 * 1024 ** 2)
                if rebuilt.returncode:
                    raise ValueError("Offline Gerrit change reindex failed (private diagnostic suppressed)")
            process = start_daemon()
            return process
        client_context = ssl.create_default_context(cafile=str(tls / "ca.crt"))
        from test_k3s_vm_fixture import guest_identity_lifetime
        token_lifetime = "60m" if handoff and worker_context is None and os.environ.get('ZUUL_RKE2_SERVING_TEST_RUNTIME')=='1' else guest_identity_lifetime(worker_context)
        if handoff and ssh and worker_context is None and os.environ.get('ZUUL_ARTIFACT_ROLLOUT_TEST_RUNTIME')=='1':
            token_lifetime = "30m"
        api = API(origin, client_context, users, credentials, request_timeout=request_timeout,token_lifetime=token_lifetime)
        print(json.dumps({"native_gerrit_request_profile": {"client_deadline_seconds": request_timeout, "backend_deadline_seconds": backend_timeout, "uncertain_write_retries": 0}}), flush=True)
        thread.start()
        try:
            deadline = time.monotonic() + 120
            ready_started = time.monotonic()
            while True:
                if process.poll() is not None: raise ValueError("Actual Gerrit daemon stopped (private diagnostic suppressed)")
                if backend_ready(backend_port, address + ":" + str(public_port)): break
                if time.monotonic() >= deadline: raise TimeoutError("Actual Gerrit REST readiness deadline")
                time.sleep(0.25)
            arguments = (Path("/proc") / str(process.pid) / "cmdline").read_bytes().decode().split("\0")
            if not all(option in arguments for option in jvm_options): raise ValueError("Original Gerrit daemon JVM arguments changed")
            ready_seconds = round(time.monotonic() - ready_started, 3)
            print(json.dumps({"native_gerrit_rest_ready": True, "elapsed_seconds": ready_seconds, "jvm_profile": jvm_profile}), flush=True)
            admin = api.bootstrap("fixture-admin") if source_site is None else api.call("fixture-admin", "/a/accounts/self")
            if not isinstance(admin, dict) or type(admin.get("_account_id")) is not int: raise ValueError("Actual Gerrit HTTP identity was not established")
            yield {"root": root, "site": site, "api": api, "origin": origin, "users": users, "tls": tls, "admin": admin, "process": process,
                "jvm_profile": jvm_profile, "rest_readiness_seconds": ready_seconds,
                **({"backend_port": backend_port} if prepare_site is not None else {}),
                **({"peer_requests": peer_requests, "peer_password": peer_password} if peer_port is not None else {}),
                **({"restart_daemon": restart_daemon,
                    "block_forwarding": block_forwarding} if peer_port == 17081 and router_hostname else {}),
                **({"interrupt_push": interrupt_push} if vip_test else {}),
                **({"config_request_observations":config_observations} if observe_config_requests else {}),
                **({"ssh_endpoint": {"host": ssh_address, "port": ssh_port}} if ssh else {})}
        except Exception:
            log.flush()
            with (root / "server.log").open("rb") as diagnostic:
                diagnostic.seek(0, os.SEEK_END)
                diagnostic.seek(max(0, diagnostic.tell() - 32000))
                value = diagnostic.read(32000).decode("utf-8", errors="replace")
            with proxy_lock: errors = dict(proxy_errors)
            print(json.dumps({"native_gerrit_failure": {"daemon_exit_code": process.poll(), "proxy_errors": errors,
                "request_observations": api.request_observations,
                "log_signals": {key: key.casefold() in value.casefold() for key in ("OutOfMemoryError", "Java heap space", "unable to create native thread",
                    "Too many open files", "Permission denied", "CorruptIndexException", "RejectedExecutionException",
                    "Connection refused", "could not forward", "forwarding failed", "Failed to connect", "No peers")}}}), flush=True)
            raise
        finally:
            public.shutdown(); public.server_close(); thread.join(timeout=5)
            stop_gerrit(process)
            log.close()


def stop_gerrit(process):
    if process.poll() is not None:
        return
    if os.getpgid(process.pid) != process.pid:
        raise ValueError("Gerrit escaped its original process group")
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def prepare(context, *, handoff=False, fast_forward_only=False, application_files=None):
    api = context["api"]
    users = {name: api.bootstrap(name) for name in ("ci-read", "ci-review")}
    project = "team/application"; encoded = quote(project, safe="")
    api.call("fixture-admin", "/a/projects/" + encoded, {"create_empty_commit": True, "branches": ["main"], **({"submit_type": "FAST_FORWARD_ONLY"} if handoff or fast_forward_only else {})}, method="PUT")
    api.call("fixture-admin", "/a/projects/" + encoded + "/labels/Verified", {"values": {"-1": "Failed", " 0": "No score", "+1": "Passed"}, "function": "NoBlock", "default_value": 0, "copy_condition": "", "allow_post_submit": False}, method="PUT")
    group = api.call("fixture-admin", "/a/groups/ci-result-bots", {}, method="PUT")
    group_id = group["id"]
    api.call("fixture-admin", "/a/groups/" + quote(group_id, safe="") + "/members/" + str(users["ci-review"]["_account_id"]), {}, method="PUT")
    api.call("fixture-admin", "/a/projects/All-Projects/access", {"add": {
        "refs/meta/config": {"permissions": {"read": {"rules": {group_id: {"action": "ALLOW"}}}}}}})
    api.call("fixture-admin", "/a/projects/" + encoded + "/access", {"add": {
        "refs/meta/config": {"permissions": {"read": {"rules": {group_id: {"action": "ALLOW"}}}}},
        "refs/heads/main": {"permissions": {"label-Verified": {"label": "Verified", "rules": {group_id: {"action": "ALLOW", "min": -1, "max": 1}}}}}}})
    if handoff:
        users["cd-writer"] = api.bootstrap("cd-writer")
        readers = api.call("fixture-admin", "/a/groups/ci-handoff-readers", {}, method="PUT")["id"]
        api.call("fixture-admin", "/a/groups/" + readers + "/members/" + str(users["ci-read"]["_account_id"]), {}, method="PUT")
        api.call("fixture-admin", "/a/projects/All-Projects/access", {"add": {"refs/meta/config": {"permissions": {"read": {"rules": {readers: {"action": "ALLOW"}}}}}}})
        writers = api.call("fixture-admin", "/a/groups/ci-deployment-writers", {}, method="PUT")["id"]
        api.call("fixture-admin", "/a/groups/" + writers + "/members/" + str(users["cd-writer"]["_account_id"]), {}, method="PUT")
        api.call("fixture-admin", "/a/projects/" + encoded + "/access", {"add": {"refs/heads/deploy/staging": {"permissions": {
            "create": {"rules": {writers: {"action": "ALLOW"}}}, "push": {"rules": {writers: {"action": "ALLOW", "force": False}}}}}}})
        initial = api.call("fixture-admin", "/a/projects/" + encoded + "/branches/main")["revision"]
        api.call("fixture-admin", "/a/projects/" + encoded + "/branches/deploy%2Fstaging", {"revision": initial}, method="PUT")
    change = api.call("fixture-admin", "/a/changes/", {"project": project, "branch": "main", "subject": "Synthetic application test", "work_in_progress": False})
    number = change["_number"]
    api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/test_application.py", b"import unittest\nclass Application(unittest.TestCase):\n def test_result(self): self.assertEqual(2+2,4)\n", method="PUT", content_type="text/plain")
    if application_files is None:
        application_files = {
            "deploy/staging/kustomization.yaml": b"resources:\n- configmap.yaml\n",
            "deploy/staging/configmap.yaml": b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: synthetic-application\ndata:\n  result: tested\n",
        } if handoff else {}
    for path, body in application_files.items():
        api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/" + quote(path, safe=""), body, method="PUT", content_type="text/plain")
    api.call("fixture-admin", "/a/changes/" + str(number) + "/edit:publish", {"notify": "NONE"})
    return {"users": users, "project": project, "change_number": number}


def exercise(test, *, handoff_check=False, argocd_check=False):
    if argocd_check and not handoff_check: raise ValueError("Argo CD reconciliation requires the merged handoff fixture")
    import tekton_checkout as checkout
    import tekton_gerrit_bridge as bridge
    import tekton_gerrit_reporter as reporter
    from test_tekton_gerrit_bridge import fixture as source_plan
    from test_tekton_gerrit_reporter import fixture as worker_fixture
    repository = Path(__file__).resolve().parents[1]
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repository / "scripts").glob("*.py")}
    with server(handoff=handoff_check) as context:
        prepared = prepare(context, handoff=handoff_check); api, root = context["api"], context["root"]
        project, number = prepared["project"], prepared["change_number"]
        plan = source_plan()
        plan["source"].update(api_url=context["origin"], clone_url=context["origin"] + "/a/" + project + ".git",
            project=project, ca_sha256=hashlib.sha256((context["tls"] / "ca.crt").read_bytes()).hexdigest())
        inputs = root / "reader"; inputs.mkdir(mode=0o700)
        for name, value in (("api-token", context["users"]["ci-read"].encode()), ("token", context["users"]["ci-read"].encode()), ("ca.crt", (context["tls"] / "ca.crt").read_bytes())):
            (inputs / name).write_bytes(value); (inputs / name).chmod(0o600)
        source = bridge.Gerrit(plan, inputs)
        items = bridge.discover(source, plan)
        test.assertEqual(len(items), 1); item = items[0]
        test.assertEqual(item["change"], number)
        bridge.verify_current(source, plan, item)
        workspace = root / "checkout"
        checkout.checkout(plan["source"]["clone_url"], item["revision"], workspace, "ci-read", patchset_ref=item["ref"], credentials=inputs)
        run_bounded([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(workspace)], timeout=30, check=True, output_max_bytes=1024 * 1024)
        test.assertNotIn(context["users"]["ci-read"], (workspace / ".git/config").read_text())
        labels = api.call("fixture-admin", "/a/projects/" + quote(project, safe="") + "/labels/?inherited")
        definition = [v for v in labels if v.get("name") == "Verified"]
        test.assertEqual(len(definition), 1)
        target = root / "application-fixture"; target.mkdir(mode=0o700)
        contract, template, digest, report_plan, report_digest, client, run, _, files = worker_fixture(target,
            source_configuration=plan, source_client=source, source_item=item,
            report_overrides={"account_id": prepared["users"]["ci-review"]["_account_id"], "label_definition_sha256": reporter.fingerprint(definition[0])})
        review_inputs = root / "reviewer"; review_inputs.mkdir(mode=0o700)
        for name, value in (("api-token", context["users"]["ci-review"].encode()), ("ca.crt", (context["tls"] / "ca.crt").read_bytes())):
            (review_inputs / name).write_bytes(value); (review_inputs / name).chmod(0o600)
        destination = reporter.GerritReview(plan, report_plan, review_inputs)
        reporter.source_policy(destination, plan, report_plan)
        path = "/a/changes/" + str(number) + "/revisions/" + item["revision"] + "/review"
        with test.assertRaises(HTTPError) as denied: api.call("ci-read", path, {"labels": {"Verified": 1}, "notify": "NONE"})
        test.assertIn(denied.exception.code, {400, 403})
        denied.exception.close()
        # The proxy accepts this synthetic credential only to let the real
        # Gerrit native-token validator reject it, rather than testing the proxy.
        invalid = secrets.token_urlsafe(36)
        api.credentials["Basic " + base64.b64encode(("ci-read:" + invalid).encode()).decode()] = "ci-read"
        original_reader = api.users["ci-read"]; api.users["ci-read"] = invalid
        try:
            with test.assertRaises(HTTPError) as rejected: api.call("ci-read", "/a/accounts/self")
            test.assertEqual(rejected.exception.code, 401)
            rejected.exception.close()
        finally: api.users["ci-read"] = original_reader
        test.assertEqual(reporter.report_run(client, destination, contract, template, digest, report_plan, report_digest, run), "reported")
        test.assertEqual(reporter.report_run(client, destination, contract, template, digest, report_plan, report_digest, run), "already-reported")
        obj = reporter.review(destination, plan, item)
        votes = [v for v in obj["labels"]["Verified"]["all"] if v.get("_account_id") == report_plan["account_id"]]
        test.assertEqual(len(votes), 1); test.assertEqual(votes[0]["value"], 1)
        with test.assertRaises(HTTPError) as policy_denied:
            api.call("ci-review", "/a/projects/" + quote(project, safe="") + "/labels/Verified", {"copy_condition": "is:ANY"}, method="PUT")
        test.assertEqual(policy_denied.exception.code, 403)
        policy_denied.exception.close()
        handoff_result = handoff_acceptance(test, context, prepared, plan, contract, template, report_plan, report_digest, client, run, argocd_check=argocd_check) if handoff_check else {}
        if not handoff_check:
            api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/replacement.txt", b"A new, untested patch set.\n", method="PUT", content_type="text/plain")
            api.call("fixture-admin", "/a/changes/" + str(number) + "/edit:publish", {"notify": "NONE"})
            with test.assertRaises(ValueError): reporter.report_run(client, destination, contract, template, digest, report_plan, report_digest, run)
            with test.assertRaises(ValueError): bridge.verify_current(source, plan, item)
            current = api.call("fixture-admin", "/a/changes/" + str(number) + "/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES")
            test.assertNotEqual(current["current_revision"], item["revision"])
            test.assertFalse(any(v.get("_account_id") == report_plan["account_id"] and v.get("value") == 1 for v in current["labels"].get("Verified", {}).get("all", [])))
        # The CI controller/worker records above are explicitly modeled. Only
        # actual local checkout/test and native Gerrit protocol are exercised.
        config_check = run_bounded(["git", "config", "--file", str(context["site"] / "etc/gerrit.config"), "--get-regexp", r"^label\..*\.labelcopyenforcement$"], timeout=10, check=False, output_max_bytes=64 * 1024)
        test.assertEqual(config_check.returncode, 1)
        result = {"real_gerrit_version": "3.13.10", "real_native_token_auth_verified": True, "real_change_discovery_verified": True,
            "real_git_https_patchset_checkout_verified": True, "local_application_test_verified": True, "real_gerrit_review_roundtrip_verified": True,
            "source_reader_vote_denied": True, "bot_label_policy_mutation_denied": True, "stale_patchset_refused": not handoff_check, "untested_patchset_approval_not_copied": not handoff_check,
            "modeled_ci_controller_worker_evidence": True, "gerrit_war_sha256": WAR_SHA, "java_archive_sha256": JAVA_SHA,
            "fixture_site_copy_enforcement_absent": True, "remote_site_copy_policy_verified": False, "publisher_signature_verified": False,
            **handoff_result, **{k: False for k in bridge.FALSE_FLAGS}}
        values = [*context["users"].values(), invalid]
        journals = [value for name, value in files.items() if name.endswith(".json")]
        test.assertFalse(any(value in text for value in values for text in journals))
    now = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repository / "scripts").glob("*.py")}
    if sources != now: raise ValueError("Source set changed during actual Gerrit acceptance")
    result["source_files_sha256"] = sources
    result["temporary_server_and_scope_removed"] = not root.exists()
    test.assertTrue(result["temporary_server_and_scope_removed"])
    output = os.environ.get("GERRIT_REVIEW_TEST_REPORT")
    if output:
        path = Path(output).resolve(); path.relative_to((repository / "private").resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream: json.dump(result, stream, sort_keys=True)
    return result


def handoff_acceptance(test, context, prepared, source_plan, contract, template, report_plan, report_digest, client, run, *, argocd_check=False, worker_context=None, reporter_bundle=None, native_cli=False, watch_pair=None):
    import copy
    from datetime import datetime
    from unittest import mock

    import gerrit_gitops_handoff as adapter
    import render_gerrit_gitops_handoff as renderer
    api, root = context["api"], context["root"]
    if native_cli and (worker_context is None or watch_pair is None or not argocd_check): raise ValueError("Native promotion requires the actual watch/worker/Argo CD case")
    number, revision = prepared["change_number"], run["spec"]["params"][0]["value"]
    cd = {"kind": "argocd", "management_target": {"kubeconfig_env": "DISPOSABLE_CD_KUBECONFIG", "context": "cd", "cluster_uid": "77777777-7777-4777-8777-777777777777"},
        "namespace": "argocd", "name": "application", "uid": "88888888-8888-4888-8888-888888888888", "spec_sha256": "", "path": "deploy/staging"}
    obj = {"metadata": {"name": cd["name"], "namespace": cd["namespace"], "uid": cd["uid"]},
        "spec": {"source": {"repoURL": source_plan["source"]["clone_url"], "targetRevision": "deploy/staging", "path": cd["path"]},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "synthetic-application"}, "syncPolicy": {"automated": {"prune": False}}}}
    inputs = root / "promoter-inputs"; inputs.mkdir(mode=0o700)
    for name, value in (("api-token", context["users"]["ci-read"].encode()), ("token", context["users"]["cd-writer"].encode()), ("ca.crt", (context["tls"] / "ca.crt").read_bytes())):
        (inputs / name).write_bytes(value); (inputs / name).chmod(0o600)
    class CDModel:
        # Explicitly modeled controller status; branch observations are real.
        def get(self, resource, namespace=None):
            test.assertEqual(resource, "application.argoproj.io/application")
            result = copy.deepcopy(obj)
            observed = api.call("ci-read", "/a/projects/team%2Fapplication/branches/deploy%2Fstaging")["revision"]
            result["status"] = {"reconciledAt": datetime.now(UTC).isoformat(), "health": {"status": "Healthy"}, "sync": {"status": "Synced", "revision": observed},
                "operationState": {"phase": "Succeeded", "syncResult": {"revision": observed}}}
            return result
    def promote(cd_client, application, target):
        chosen_cd = {**cd, "uid": application["metadata"]["uid"], "management_target": target, "spec_sha256": adapter.handoff.sha256(application["spec"])}
        plan = {"schema_version": 1, "runtime": contract["runtime"], "bridge_contract_sha256": report_plan["source_contract_sha256"], "reporter_contract_sha256": report_digest,
            "source_read_username": "ci-read", "source_read_account_id": prepared["users"]["ci-read"]["_account_id"], "deployment_branch": "deploy/staging",
            "allow_create_branch": False, "allow_prune": False, "git_username": "cd-writer", "timeout_seconds": 90 if argocd_check else 30, "cd": chosen_cd}
        files = renderer.render(reporter_bundle if reporter_bundle is not None else root / "application-fixture/reporter", plan)
        packed = root / "handoff-package"; packed.mkdir(mode=0o700)
        for name, value in files.items(): (packed / name).write_text(value, encoding="utf-8", newline="\n")
        package_digest = json.loads(files["handoff-status.json"])["contract_sha256"]
        _, report, _, _ = adapter.load_bundle(packed, package_digest)
        access_verified = False
        if worker_context is not None:
            import render_gerrit_promotion_access as access
            access_plan = {"schema_version": 1,
                "ci": {"account": "gerrit-promotion-ci-reader", "namespace": source_plan["submitter_namespace"], "workers": [n["name"] for n in worker_context["nodes"][1:]]},
                "cd": {"account": "fixture-promoter-read", "namespace": chosen_cd["namespace"]}}
            access_files = access.render(packed, package_digest, access_plan)
            access_bundle = root / "promotion-access-package"; access_bundle.mkdir(mode=0o700)
            for name, value in access_files.items():
                path = access_bundle / name; path.write_text(value, encoding="utf-8", newline="\n"); path.chmod(0o600)
            access.load_bundle(access_bundle, json.loads(access_files["access-status.json"])["contract_sha256"], packed, package_digest)
            for name in ("ci-reader-rbac.json", "cd-reader-rbac.json"):
                for expected in json.loads(access_files[name])["items"]:
                    resource = expected["kind"].lower() + (".rbac.authorization.k8s.io" if expected["kind"] != "ServiceAccount" else "") + "/" + expected["metadata"]["name"]
                    actual = worker_context["client"].get(resource, expected["metadata"].get("namespace"))
                    for field in set(expected) - {"metadata"}: test.assertEqual(actual.get(field), expected[field])
                    for field, value in expected["metadata"].items(): test.assertEqual(actual["metadata"].get(field), value)
                    test.assertFalse(actual.get("aggregationRule")); test.assertFalse(actual.get("secrets"))
            access_verified = True
        reader = adapter.ReadAPI(source_plan, plan, inputs)
        native_results = []
        def execute_native(*, apply=False, expected_code=0):
            from test_gerrit_native_promotion_fixture import execute
            native = execute(test, worker_context, root, packed, package_digest, inputs, contract, plan, client, cd_client, run["metadata"]["name"],
                apply=apply, expected_code=expected_code, excluded_uids=[v["uid"] for v in watch_pair.handles.values()])
            native_results.append(native)
            return native["result"]
        with mock.patch.object(adapter, "INPUTS", inputs):
            if native_cli:
                execute_native(apply=True, expected_code=1)
            else:
                with test.assertRaises(ValueError): adapter.execute(plan, report, contract, template, client, cd_client, reader, run["metadata"]["name"], apply=True)
            before = api.call("ci-read", "/a/projects/team%2Fapplication/branches/deploy%2Fstaging")["revision"]
            test.assertNotEqual(before, revision)
            api.call("fixture-admin", "/a/changes/" + str(number) + "/revisions/" + revision + "/review", {"labels": {"Code-Review": 2}, "notify": "NONE"})
            api.call("fixture-admin", "/a/changes/" + str(number) + "/submit", {"wait_for_merge": True, "notify": "NONE"})
            test.assertEqual(api.call("ci-read", "/a/projects/team%2Fapplication/branches/main")["revision"], revision)
            preflight = execute_native() if native_cli else adapter.execute(plan, report, contract, template, client, cd_client, reader, run["metadata"]["name"])
            test.assertTrue(preflight["read_only"])
            test.assertEqual(api.call("ci-read", "/a/projects/team%2Fapplication/branches/deploy%2Fstaging")["revision"], before)
            result = execute_native(apply=True) if native_cli else adapter.execute(plan, report, contract, template, client, cd_client, reader, run["metadata"]["name"], apply=True)
            test.assertTrue(result["branch_push_attempted"]); test.assertFalse(result["full_stack_verified"])
            test.assertEqual(api.call("ci-read", "/a/projects/team%2Fapplication/branches/deploy%2Fstaging")["revision"], revision)
            again = execute_native(apply=True) if native_cli else adapter.execute(plan, report, contract, template, client, cd_client, reader, run["metadata"]["name"], apply=True)
            test.assertFalse(again["branch_push_attempted"])
        result = {"real_merged_gerrit_handoff_git_protocol_verified": True, "unmerged_review_promotion_refused": True,
            "deployment_branch_preflight_read_only_verified": True, "deployment_branch_exact_fast_forward_verified": True,
            "already_promoted_commit_not_pushed_again": True, "argocd_status_modeled": True, "actual_argocd_reconciliation_verified": False}
        if worker_context is not None: result["rendered_promotion_access_live_verified"] = access_verified
        if native_cli:
            test.assertEqual(len(native_results), 4)
            result.update(native_promoter_cli_verified=True, native_promoter_nonroot_verified=True, native_promotion_journals_verified=True,
                native_promoter_units_removed=True, native_promoter_unit_names=[v["unit"] for v in native_results],
                native_promoter_journal_uids=[v["uid"] for v in native_results])
        return result
    if argocd_check:
        from test_argocd_reconciliation_fixture import exercise as real_reconciliation
        return real_reconciliation(test, context, source_plan, promote, worker_context=worker_context)
    if worker_context is not None: raise ValueError("A worker handoff requires actual Argo CD reconciliation")
    return promote(CDModel(), obj, cd["management_target"])
