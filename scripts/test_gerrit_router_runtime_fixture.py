#!/usr/bin/env python3
"""Real pinned HAProxy routing with explicitly modeled HTTPS/SSH backends."""

import hashlib
from contextlib import ExitStack, contextmanager
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time

from bounded_subprocess import run_bounded
import render_gerrit_router as router
import check_gerrit_router_backends as preflight


HAPROXY_SHA = "3137dc28d1fa8095ce2610e5736d67cbbdec3a3b15af49bb3ba8631ebe5904ed"
HOST = "review.example.invalid"


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def exercise(test, *, hold_primary=False):
    if os.geteuid() != 0 or os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise ValueError('Require an owned isolated Linux network namespace')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if status['NoNewPrivs'].strip() != '1': raise ValueError('Require inherited no-new-privileges for the router process')
    def run(args):
        value = run_bounded(args, timeout=20, text=True, output_max_bytes=256 * 1024, check=False)
        if value.returncode: raise ValueError('Router fixture setup command failed; details retained privately')
        return value.stdout
    if json.loads(run(['ip', '-j', 'route', 'show', 'default'])): raise ValueError('Refuse an upstream route')
    test.assertEqual([row['ifname'] for row in json.loads(run(['ip', '-j', 'link', 'show']))], ['lo'])
    run(['ip', 'link', 'set', 'lo', 'up'])
    repository = Path(__file__).resolve().parents[1]
    sources = {p.name: digest(p) for p in (repository / 'scripts').glob('*.py')}
    binary = Path(os.environ['GERRIT_ROUTER_TEST_HAPROXY']).resolve(strict=True)
    test.assertEqual(digest(binary), HAPROXY_SHA)
    servers = []; threads = []; process = None; log = None; outcomes = []
    with tempfile.TemporaryDirectory(prefix='platform-gerrit-router-', dir='/var/tmp') as temporary:
        root = Path(temporary); root.chmod(0o700)
        run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=router-fixture-ca',
            '-keyout', str(root / 'ca.key'), '-out', str(root / 'ca.crt'), '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign'])
        for name, hostname in [('valid', HOST), ('wrong-name', 'foreign.example.invalid')]:
            run(['openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-subj', '/CN=' + hostname,
                '-keyout', str(root / (name + '.key')), '-out', str(root / (name + '.csr'))])
            extensions = root / (name + '.ext')
            extensions.write_text('subjectAltName=DNS:' + hostname + '\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n')
            run(['openssl', 'x509', '-req', '-in', str(root / (name + '.csr')), '-CA', str(root / 'ca.crt'), '-CAkey', str(root / 'ca.key'), '-CAcreateserial',
                '-days', '1', '-extfile', str(extensions), '-out', str(root / (name + '.crt'))])
        contexts = {}
        for name in ('valid', 'wrong-name'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(root / (name + '.crt'), root / (name + '.key')); contexts[name] = context

        class HTTPHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == preflight.HEALTH_PATH:
                    self.server.release_health.wait(15)
                    status = self.server.health
                    body = json.dumps({name: {"result": "failed" if status != 200 and name == "querychanges" else "passed"}
                                       for name in preflight.REQUIRED_HEALTHCHECKS}).encode()
                else:
                    status = 200
                    body = (self.server.label + '\n').encode()
                self.send_response(status); self.send_header('Content-Length', str(len(body)))
                if self.path != '/session': self.send_header('Connection', 'close')
                self.end_headers(); self.wfile.write(body)
            def do_PUT(self): self.do_GET()

        class HTTPS(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True; allow_reuse_address = True
            def get_request(self):
                connection, address = self.socket.accept(); connection.settimeout(45)
                try: return contexts[self.certificate].wrap_socket(connection, server_side=True), address
                except Exception: connection.close(); raise
            def handle_error(self, *_): pass

        class SSHHandler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(self.server.banner)
                self.request.settimeout(45)
                if self.request.recv(1) == b'p':
                    self.request.sendall(b'p')
                    self.request.recv(1)

        class SSH(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True; allow_reuse_address = True
            def handle_error(self, *_): pass

        backends = {}
        for name, address in [('primary', '127.0.0.2'), ('backup', '127.0.0.3')]:
            https = HTTPS((address, 9443), HTTPHandler); https.label = name; https.health = 200; https.certificate = 'valid'
            https.release_health = threading.Event()
            https.release_health.set()
            ssh = SSH((address, 30222), SSHHandler); ssh.banner = ('SSH-2.0-' + name + '\r\n').encode()
            backends[name] = (https, ssh)
            for server in (https, ssh):
                servers.append(server); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); threads.append(thread)
        plan = {'schema_version': 1, 'canonical_host': HOST, 'primary_host': '127.0.0.2', 'backup_host': '127.0.0.3',
            'hold_primary': hold_primary,
            'https_port': 9443, 'ssh_port': 30222, 'namespace': 'gerrit', 'ca_secret': 'fixture-ca', 'haproxy_image': 'registry.example.invalid/haproxy@sha256:' + 'a' * 64}
        ca = (root / 'ca.crt').read_bytes()
        test.assertTrue(preflight.check(plan, ca)["backends_ready"])
        original = router.haproxy_config(plan)
        # Relocate only the CA file for this private laboratory. All routing,
        # timing, TLS verification, frontend and backend directives are original.
        configuration = original.replace(router.CA_FILE, str(root / 'ca.crt'))
        test.assertEqual(configuration.replace(str(root / 'ca.crt'), router.CA_FILE), original)
        config = root / 'haproxy.cfg'; config.write_text(configuration)
        local_binary = root / 'haproxy'; shutil.copyfile(binary, local_binary); local_binary.chmod(0o500)
        test.assertEqual(digest(local_binary), HAPROXY_SHA)
        for path in [root, *root.iterdir()]:
            os.chown(path, 65534, 65534)
            if path.is_file() and path != local_binary: path.chmod(0o600)
        run([str(local_binary), '-c', '-f', str(config)])
        trusted = ssl.create_default_context(cafile=str(root / 'ca.crt'))
        trusted.verify_flags |= ssl.VERIFY_X509_STRICT
        def https_request(context=trusted, method='GET'):
            connection = http.client.HTTPSConnection(HOST, 8443, timeout=1, context=context)
            connection.sock = context.wrap_socket(socket.create_connection(('127.0.0.1', 8443), timeout=1), server_hostname=HOST)
            try:
                connection.request(method, '/'); response = connection.getresponse(); return response.status, response.read(128).decode().strip()
            finally: connection.close()
        def ssh_request():
            with socket.create_connection(('127.0.0.1', 29418), timeout=1) as connection: return connection.recv(128).decode().strip()

        def readiness():
            connection = http.client.HTTPConnection('127.0.0.1', router.READINESS_PORT, timeout=2)
            try:
                connection.request('GET', '/readyz')
                response = connection.getresponse()
                response.read(2048)
                return response.status
            finally:
                connection.close()

        def assert_route(selection):
            connection = http.client.HTTPConnection('127.0.0.1', router.READINESS_PORT, timeout=2)
            try:
                connection.request('GET', router.ROUTE_PATH)
                response = connection.getresponse()
                test.assertEqual(response.status, 503 if selection == 'unavailable' else 200)
                expected = f'{router.route_config_id(plan)} {selection}'.encode('ascii')
                test.assertEqual(response.read(160), expected)
            finally:
                connection.close()

        @contextmanager
        def open_sessions(label):
            with ExitStack() as stack:
                transport = stack.enter_context(socket.create_connection(('127.0.0.1', 8443), timeout=2))
                tls = stack.enter_context(trusted.wrap_socket(transport, server_hostname=HOST))
                connection = http.client.HTTPSConnection(HOST, 8443)
                connection.sock = tls
                connection.request('GET', '/session')
                response = connection.getresponse()
                test.assertEqual((response.status, response.read().decode().strip()), (200, label))
                test.assertFalse(response.will_close)
                ssh = stack.enter_context(socket.create_connection(('127.0.0.1', 29418), timeout=2))
                with ssh.makefile('rb') as stream:
                    test.assertEqual(stream.readline(128), ('SSH-2.0-' + label + '\r\n').encode())
                    ssh.sendall(b'p')
                    test.assertEqual(stream.read(1), b'p')
                yield tls, ssh

        def assert_closed(sockets):
            for connection in sockets:
                try:
                    test.assertEqual(connection.recv(1), b'')
                except (ConnectionResetError, ssl.SSLEOFError):
                    pass
        def observe():
            row = {'https': None, 'ssh': None}
            try: row['https'] = https_request()
            except (OSError, http.client.HTTPException): pass
            try: row['ssh'] = ssh_request()
            except OSError: pass
            return row
        def await_selection(label):
            started = time.monotonic(); deadline = started + 30; consecutive = 0
            while True:
                if process.poll() is not None: raise AssertionError('Original HAProxy exited')
                row = observe()
                matches = row['https'] == (200, label) and row['ssh'] == 'SSH-2.0-' + label
                consecutive = consecutive + 1 if matches else 0
                if consecutive == 2:
                    assert_route(label)
                    outcomes.append({'selected': label, 'elapsed_seconds': round(time.monotonic() - started, 3)}); return
                if time.monotonic() >= deadline: raise AssertionError('Combined HTTPS/SSH selection was not verified')
                time.sleep(0.25)
        try:
            log = (root / 'haproxy.log').open('wb')
            for https, _ in backends.values():
                https.release_health.clear()
            process = subprocess.Popen([str(local_binary), '-W', '-db', '-f', str(config)], stdout=log, stderr=subprocess.STDOUT,
                user=65534, group=65534, extra_groups=[], start_new_session=True)
            pid = process.pid; original_start = (Path('/proc') / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19]
            deadline = time.monotonic() + 5
            while True:
                try:
                    test.assertEqual(readiness(), 503)
                    break
                except ConnectionRefusedError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            cold = observe()
            test.assertIsNone(cold['https'])
            test.assertIn(cold['ssh'], (None, ''))
            assert_route('unavailable')
            backends['backup'][0].release_health.set()
            await_selection('backup')
            test.assertEqual(readiness(), 200)
            backends['primary'][0].release_health.set()
            if hold_primary:
                # Both backends are healthy and directly reachable. Exercise
                # enough check cycles that the normal config would prefer primary.
                test.assertTrue(preflight.check(plan, ca)["backends_ready"])
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    test.assertEqual(observe(), {"https": (200, "backup"), "ssh": "SSH-2.0-backup"})
                    time.sleep(0.25)
                backends['backup'][0].health = 503
                deadline = time.monotonic() + 30
                while readiness() != 503:
                    observation = observe()
                    test.assertIn(observation["https"], (None, (200, "backup")))
                    test.assertIn(observation["ssh"], (None, "", "SSH-2.0-backup"))
                    if time.monotonic() >= deadline:
                        test.fail("A held primary became eligible after the backup failed")
                    time.sleep(0.25)
                observation = observe()
                test.assertIsNone(observation["https"])
                test.assertIn(observation["ssh"], (None, ""))
                assert_route('unavailable')
                backends['backup'][0].health = 200
                await_selection('backup')
                test.assertEqual(process.pid, pid)
                test.assertEqual(readiness(), 200)
                print(json.dumps({"real_haproxy_primary_hold_verified": True,
                                  "both_protocols_fail_closed_on_backup_loss": True,
                                  "physical_ha_verified": False}), flush=True)
                return
            await_selection('primary')
            test.assertEqual(readiness(), 200)
            test.assertEqual(https_request(method='PUT'), (200, 'primary'))
            with test.assertRaises(ssl.SSLCertVerificationError): https_request(ssl.create_default_context())
            for fault in ('ssh-banner', 'tls-hostname', 'http-health'):
                with open_sessions('primary') as prior:
                    if fault == 'ssh-banner': backends['primary'][1].banner = b'not-an-ssh-server\r\n'
                    elif fault == 'tls-hostname': backends['primary'][0].certificate = 'wrong-name'
                    else: backends['primary'][0].health = 503
                    checked = preflight.check(plan, ca)
                    test.assertFalse(checked["backends_ready"])
                    test.assertTrue(checked["backends"]["backup"]["health_verified"])
                    primary = checked["backends"]["primary"]
                    if fault == 'ssh-banner': test.assertFalse(primary["ssh_banner_verified"])
                    elif fault == 'tls-hostname': test.assertFalse(primary["tls_verified"])
                    else: test.assertEqual(primary["check_failures"], ["querychanges"])
                    await_selection('backup')
                    test.assertEqual(readiness(), 200)
                    assert_closed(prior)
                test.assertEqual(https_request(method='PUT'), (200, 'backup'))
                with open_sessions('backup') as prior:
                    backends['primary'][1].banner = b'SSH-2.0-primary\r\n'
                    backends['primary'][0].certificate = 'valid'
                    backends['primary'][0].health = 200
                    await_selection('primary')
                    assert_closed(prior)
            for https, _ in backends.values(): https.health = 503
            started = time.monotonic(); closed = 0
            while closed < 2:
                row = observe(); closed = closed + 1 if row['https'] is None and row['ssh'] in (None, '') else 0
                if time.monotonic() - started > 30: raise AssertionError('All unhealthy backends did not fail closed')
                time.sleep(0.25)
            test.assertEqual(readiness(), 503)
            assert_route('unavailable')
            for https, _ in backends.values(): https.health = 200
            await_selection('primary')
            test.assertEqual(readiness(), 200)
            test.assertEqual((Path('/proc') / str(pid) / 'stat').read_text().split(') ', 1)[1].split()[19], original_start)
            status = dict(line.split(':', 1) for line in (Path('/proc') / str(pid) / 'status').read_text().splitlines() if ':' in line)
            test.assertEqual(status['Uid'].split(), ['65534'] * 4); test.assertEqual(int(status['CapEff'], 16), 0); test.assertEqual(status['NoNewPrivs'].strip(), '1')
            report = {'real_haproxy_process_verified': True, 'combined_https_ssh_failover_verified': True, 'tls_hostname_rejection_verified': True,
                'untrusted_client_ca_rejected': True, 'modeled_write_request_routing_verified': True, 'all_unhealthy_fail_closed_verified': True,
                'modeled_gerrit_backends': True, 'direct_backend_preflight_verified': True,
                'https_and_ssh_session_shutdown_on_failover_and_failback_verified': True,
                'startup_requires_fresh_health_checks_verified': True, 'backend_aware_readiness_verified': True,
                'routez_matches_actual_automatic_selection': True,
                'actual_gerrit_ha_verified': False, 'ceph_storage_verified': False, 'physical_ha_verified': False, 'full_stack_verified': False,
                'haproxy_sha256': HAPROXY_SHA, 'rendered_configuration_sha256': hashlib.sha256(original.encode()).hexdigest(),
                'only_configuration_relocation': 'private-ca-path', 'selection_observations': outcomes, 'original_pid': pid, 'original_starttime': original_start}
        finally:
            for https, _ in backends.values():
                https.release_health.set()
            if process is not None:
                if process.poll() is None:
                    if os.getpgid(process.pid) != process.pid: raise ValueError('Original router group changed')
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            if log is not None: log.close()
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=5); test.assertFalse(thread.is_alive())
    test.assertFalse(root.exists()); report['temporary_processes_and_scope_removed'] = True
    test.assertEqual({p.name: digest(p) for p in (repository / 'scripts').glob('*.py')}, sources)
    report['source_files_sha256'] = sources
    output = Path(os.environ['GERRIT_ROUTER_TEST_REPORT']).resolve(); output.relative_to(repository / 'private')
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream: json.dump(report, stream, sort_keys=True)
    return report
