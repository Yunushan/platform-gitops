#!/usr/bin/env python3
"""Explicit disposable Linux Keycloak/PostgreSQL browser-MFA runtime fixture.

No downloads, borrowed services, default kubeconfig, production realm or private
operator credentials are used. Two processes on one host do not prove node HA.
"""

import base64
from http.cookiejar import CookieJar
import ctypes
import hashlib
import hmac
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import build_opener, HTTPSHandler, HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request
from unittest import mock

from bounded_subprocess import run_bounded
import bootstrap_keycloak_realm as bootstrap
import configure_keycloak_argocd as application_identity
import render_keycloak_ha as identity
from test_keycloak_ha import fixture


ARTIFACTS = {
    "keycloak-26.4.7.tar.gz": "aca60ab72ae4d67adc6b8fe7fca5e08d16a6bb5e4a4f2ebb54050e3f5503fd2b",
    "OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz": "a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd",
}
CALLBACK = "https://localhost:1/auth/callback"


def failure_signals(text):
    """Only allowlisted classifications leave private subprocess/log capture."""
    lower = text.lower()
    return {"build_time_option_rejected": "build time option:" in lower,
        "implicit_rebuild_requested": "changes detected in configuration" in lower,
        "unknown_cli_option": "unknown option" in lower,
        "permission_denied": "permission denied" in lower or "accessdeniedexception" in lower,
        "address_in_use": "address already in use" in lower,
        "out_of_memory": "outofmemory" in lower or "out of memory" in lower,
        "sqlstates": sorted(set(re.findall(r"SQLState[:= ]+([A-Z0-9]{5})\b", text)))}


def source_inventory():
    directory = Path(__file__).resolve().parent; result = {}
    for module in list(sys.modules.values()):
        value = getattr(module, "__file__", None)
        if value and Path(value).resolve().parent == directory and Path(value).suffix == ".py":
            path = Path(value).resolve(); result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def loopback_listeners(processes, required):
    inodes = set()
    for process, _ in processes:
        if process.poll() is not None: raise ValueError("A fixture process stopped during socket observation")
        for path in Path("/proc", str(process.pid), "fd").iterdir():
            try: value = os.readlink(path)
            except FileNotFoundError: continue
            if value.startswith("socket:["): inodes.add(value[8:-1])
    observed = set()
    for family in ("tcp", "tcp6"):
        for line in Path("/proc/net", family).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] != "0A" or fields[9] not in inodes: continue
            address, port = fields[1].split(":"); raw = bytes.fromhex(address)
            value = ipaddress.ip_address(raw[::-1] if family == "tcp" else b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
            if not value.is_loopback and not (value.version == 6 and value.ipv4_mapped and value.ipv4_mapped.is_loopback): raise ValueError("A disposable fixture listener escaped loopback")
            observed.add(int(port, 16))
    if not required <= observed: raise ValueError("The expected listeners are not owned by the original fixture processes")


def child():
    parent = int(sys.argv[2])
    if os.getppid() != parent: raise SystemExit(1)
    environment = json.loads(Path(sys.argv[3]).read_text())
    os.setgroups([]); os.setgid(65534); os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0) or os.getppid() != parent: raise SystemExit(1)
    os.execve(sys.argv[4], sys.argv[4:], environment)


class Forms(HTMLParser):
    def __init__(self, page):
        super().__init__(); self.forms = []; self.current = None; self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""), "fields": {}}; self.forms.append(self.current)
        if tag == "input" and self.current is not None and attrs.get("name") and attrs.get("type") not in {"checkbox", "radio", "submit"}:
            self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form": self.current = None

    def select(self, field):
        values = [form for form in self.forms if field in form["fields"]]
        if len(values) != 1: raise ValueError("The disposable browser flow did not present the expected unique form")
        return values[0]


def otp(secret, *, now=None):
    secret = re.sub(r"\s+", "", secret)
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() if now is None else now) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


class Browser:
    def __init__(self, trust):
        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, url):
                if url.startswith(CALLBACK + "?"): return None
                target = urlsplit(url)
                if target.scheme != "https" or target.hostname not in {"localhost", "127.0.0.1"}: raise ValueError("The fixture redirect escaped loopback HTTPS")
                return super().redirect_request(req, fp, code, msg, headers, url)
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=trust), HTTPCookieProcessor(CookieJar()), Redirect())

    def fetch(self, url, fields=None):
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in {"localhost", "127.0.0.1"}: raise ValueError("The fixture browser escaped loopback HTTPS")
        req = Request(url, data=None if fields is None else urlencode(fields).encode())
        try:
            with self.opener.open(req, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024: raise ValueError("Excessive fixture HTML")
                return response.url, raw.decode()
        except HTTPError as error:
            location = error.headers.get("Location", "")
            status = error.code; error.close()
            if status == 302 and location.startswith(CALLBACK + "?"): return location, ""
            raise ValueError("The disposable browser request failed") from None

    def submit(self, current, form, **values):
        return self.fetch(urljoin(current, form["action"]), {**form["fields"], **values})


def exercise(test, *, application_check=None):
    if sys.platform != "linux" or os.geteuid() != 0 or os.uname().machine != "x86_64": raise ValueError("The explicit fixture requires a disposable root Linux amd64 runner")
    if not shutil.which("openssl"): raise ValueError("The runtime fixture needs OpenSSL")
    if any(not ipaddress.ip_address(entry[4][0]).is_loopback for entry in socket.getaddrinfo("localhost", None)): raise ValueError("The fixture localhost name must resolve only to loopback")
    sources = source_inventory()
    artifacts = Path(os.environ["KEYCLOAK_TEST_ARTIFACTS"]).resolve(strict=True)
    pg = Path(os.environ["KEYCLOAK_TEST_PG_ROOT"]).resolve(strict=True)
    libraries = Path(os.environ["KEYCLOAK_TEST_PG_LIBS"]).resolve(strict=True)
    for name, digest in ARTIFACTS.items():
        path = artifacts / name
        if path.is_symlink(): raise ValueError("Use the exact reviewed public runtime archives")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest: raise ValueError("Use the exact reviewed public runtime archives")
    for name in ("postgres", "initdb", "psql"):
        if not (pg / "bin" / name).is_file(): raise ValueError("The explicit PostgreSQL 17 toolset is incomplete")
    report = {"keycloak_version": "26.4.7", "java_version": "21.0.13+9", "postgres_version": "17.11",
        "artifact_sha256": ARTIFACTS, "runtime": "raw-linux-single-host-fixture", "database_instances": 1, "application_processes": 2,
        "two_process_tls_startup_verified": False, "owned_loopback_listeners_verified": False,
        "generated_private_values_absent_from_logs": False,
        "guarded_realm_creation_and_readback_verified": False, "existing_realm_and_resume_boundaries_verified": False,
        "guarded_oidc_client_and_group_claim_verified": False, "oidc_callback_pkce_and_disabled_grants_verified": False, "signed_id_token_claims_verified": False,
        "otp_enrollment_and_login_verified": False, "password_only_and_bad_otp_rejected": False,
        "refresh_after_first_process_stopped": False, "physical_node_loss_verified": False, "ha_verified": False, "full_stack_verified": False}
    reservations = []
    def reserve(port=0):
        handle = socket.socket(); handle.bind(("127.0.0.1", port)); reservations.append(handle); return handle.getsockname()[1]
    pg_port, https_a, https_b, health_a, health_b = [reserve() for _ in range(5)]
    peer_ports = []
    for _ in range(2):
        for _attempt in range(100):
            port = 10000 + secrets.randbelow(4000)
            before = len(reservations)
            try: reserve(port); reserve(port + 50000); peer_ports.append(port); break
            except OSError:
                for handle in reservations[before:]: handle.close()
                del reservations[before:]
        else: raise ValueError("No fresh fixture cache port pair is available")
    with tempfile.TemporaryDirectory(prefix="platform-keycloak-runtime-", dir="/tmp") as temporary:
        root = Path(temporary); root.chmod(0o700); processes = []; deadline = time.monotonic() + 600
        db_password, login_password, client_secret, admin_secret = (secrets.token_urlsafe(36) for _ in range(4))
        def stage_owner(path):
            os.chown(path, 65534, 65534)
            if path.is_dir():
                for item in path.rglob("*"):
                    if not item.is_symlink(): os.chown(item, 65534, 65534)
        def write(name, value):
            path = root / name; path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(value); path.chmod(0o600); os.chown(path.parent, 65534, 65534); os.chown(path, 65534, 65534); return path
        def clean_environment(**values):
            return {"PATH": str(java / "bin") + ":/usr/bin:/bin", "JAVA_HOME": str(java), "HOME": str(root), "LC_ALL": "C", "LD_LIBRARY_PATH": str(root / "pg-libs"),
                "KC_HTTP_HOST": "127.0.0.1", "KC_HTTP_MANAGEMENT_HOST": "127.0.0.1",
                "JAVA_OPTS": "-Xms64m -Xmx768m -XX:MaxMetaspaceSize=256m -Djava.net.preferIPv4Stack=true", **values}
        def foreground(arguments, environment):
            # The archive's kc.sh forks Java in the background. Use its exact
            # reviewed classpath/properties so the parent-death guard owns the
            # actual JVM, rather than an intermediate shell that can orphan it.
            if Path(arguments[0]).name != "kc.sh": return list(map(str, arguments))
            directory = Path(arguments[0]).parent.parent
            return [str(java / "bin/java"), *shlex.split(environment["JAVA_OPTS"]),
                "-Djava.util.concurrent.ForkJoinPool.common.threadFactory=io.quarkus.bootstrap.forkjoin.QuarkusForkJoinWorkerThreadFactory",
                "-Dkc.home.dir=" + str(directory), "-Djboss.server.config.dir=" + str(directory / "conf"),
                "-Djava.util.logging.manager=org.jboss.logmanager.LogManager", "-Dpicocli.disable.closures=true", "-Dquarkus-log-max-startup-records=10000",
                "-cp", str(directory / "lib/quarkus-run.jar"), "io.quarkus.bootstrap.runner.QuarkusEntryPoint", *map(str, arguments[1:])]
        def run(arguments, *, environment=None, payload=None, timeout=60):
            environment = environment or clean_environment(); environment_file = write("command-environment.json", json.dumps(environment))
            actual = foreground(arguments, environment)
            result = run_bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(os.getpid()), str(environment_file), *actual],
                cwd=root, input=payload, env={"PATH": "/usr/bin:/bin"}, text=True, timeout=timeout, output_max_bytes=1024 * 1024, check=False)
            diagnostic = failure_signals(result.stderr + result.stdout)
            test.assertEqual(result.returncode, 0, "Disposable runtime command failed: " + json.dumps(diagnostic, sort_keys=True))
            return result.stdout
        def start(name, arguments, environment):
            environment_file = write(name + "-environment.json", json.dumps(environment)); log = (root / (name + ".log")).open("w+b")
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(os.getpid()), str(environment_file), *foreground(arguments, environment)],
                cwd=root, env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            processes.append((process, log)); return process
        def stop(process):
            if process.poll() is None:
                if os.getpgid(process.pid) != process.pid: raise ValueError("A fixture process escaped its original group")
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=20)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
        def wait(callback):
            while True:
                for process, log in processes:
                    if process.poll() is not None:
                        log.seek(0); diagnostic = log.read(8 * 1024 * 1024).decode(errors="replace")
                        raise ValueError("Disposable " + Path(log.name).name + " exited " + str(process.returncode) + " before readiness: " + json.dumps(failure_signals(diagnostic), sort_keys=True))
                    if os.fstat(log.fileno()).st_size > 8 * 1024 ** 2: raise ValueError("Excessive private fixture logging")
                try:
                    if callback(): return
                except (OSError, URLError, ValueError): pass
                if time.monotonic() >= deadline: raise TimeoutError("Disposable Keycloak runtime deadline")
                time.sleep(0.25)
        def private_logs(values):
            for _, log in processes:
                with Path(log.name).open("rb") as stream: content = stream.read(8 * 1024 ** 2 + 1)
                if len(content) > 8 * 1024 ** 2 or b"prvKey" in content or any(value.encode() in content for value in values):
                    raise ValueError("Private fixture logs contain a generated sensitive value or exceed the capture limit")
        try:
            for name in ARTIFACTS:
                with tarfile.open(artifacts / name) as archive: archive.extractall(root, filter="data")
            home = root / "keycloak-26.4.7"; java = root / "jdk-21.0.13+9-jre"
            if not (java / "bin/java").is_file() or not (home / "bin/kc.sh").is_file(): raise ValueError("The reviewed archive layout differs")
            shutil.copytree(pg, root / "pg"); shutil.copytree(libraries, root / "pg-libs"); stage_owner(root)
            run([root / "pg/bin/postgres", "--version"])
            test.assertIn("17.11", run([root / "pg/bin/postgres", "--version"]))
            run([java / "bin/java", "-version"])
            print("Disposable Keycloak fixture: building optimized runtime", flush=True)
            run([home / "bin/kc.sh", *identity.build_arguments()], timeout=120)
            other = root / "keycloak-b"; shutil.copytree(home, other); stage_owner(other)
            run([root / "pg/bin/initdb", "-D", root / "pg-data", "-U", "postgres", "--auth-local=trust", "--auth-host=reject", "--no-locale", "--encoding=UTF8"])
            # All keys and credentials below are newly generated fixture data.
            run(["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1", "-subj", "/CN=fixture-keycloak-root",
                "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", root / "ca.key", "-out", root / "ca.crt"])
            for target in ("tls", "pg-tls"):
                (root / target).mkdir(mode=0o700); stage_owner(root / target)
                run(["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=localhost", "-keyout", root / target / "tls.key", "-out", root / target / "tls.csr"])
                extension = write(target + "/tls.ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
                run(["/usr/bin/openssl", "x509", "-req", "-days", "1", "-in", root / target / "tls.csr", "-CA", root / "ca.crt", "-CAkey", root / "ca.key", "-CAcreateserial", "-extfile", extension, "-out", root / target / "tls.crt"])
                (root / target / "tls.key").chmod(0o600)
            (root / "db-ca").mkdir(mode=0o700); shutil.copyfile(root / "ca.crt", root / "db-ca/ca.crt"); stage_owner(root / "db-ca")
            write("pg-data/postgresql.conf", f"listen_addresses='127.0.0.1'\nport={pg_port}\nunix_socket_directories='{root}'\nssl=on\nssl_cert_file='{root}/pg-tls/tls.crt'\nssl_key_file='{root}/pg-tls/tls.key'\npassword_encryption='scram-sha-256'\n")
            write("pg-data/pg_hba.conf", "local all all trust\nhostssl all keycloak_fixture 127.0.0.1/32 scram-sha-256\nhost all all 127.0.0.1/32 reject\n")
            for handle in reservations: handle.close()
            start("postgres", [root / "pg/bin/postgres", "-D", root / "pg-data"], clean_environment())
            def database_listens():
                with socket.create_connection(("127.0.0.1", pg_port), timeout=0.5): return True
            wait(database_listens)
            run([root / "pg/bin/psql", "-XAtq", "--no-password", "-v", "ON_ERROR_STOP=1", "-h", root, "-p", str(pg_port), "-U", "postgres", "-d", "postgres", "-f", "-"],
                payload="CREATE ROLE keycloak_fixture LOGIN PASSWORD '" + db_password + "';\nCREATE DATABASE keycloak_fixture OWNER keycloak_fixture;\n")
            plan = fixture(); plan["private_dir"] = str(root); plan["hostname"] = "localhost"
            plan["database"].update(host="127.0.0.1", port=pg_port, name="keycloak_fixture", username="keycloak_fixture")
            realm = identity.new_realm(plan)
            realm["users"] = [{"username": "fixture-user", "enabled": True, "email": "fixture-user@example.invalid", "emailVerified": True,
                "firstName": "Fixture", "lastName": "User", "credentials": [{"type": "password", "value": login_password, "temporary": False}]}]
            realm["users"][0]["groups"] = ["/fixture-readers"]
            # Prepare only a disposable schema seed offline. The actual MFA
            # realm is created through the production helper after TLS startup.
            realm_file = write("new-fixture-realm.json", json.dumps({"realm": "fixture-schema-seed", "enabled": False}))
            common = identity.arguments(plan, native=True, member={"address": "127.0.0.1"})
            db_options = [arg for arg in common[2:] if arg.startswith(("--db-url=", "--db-username="))]
            print("Disposable Keycloak fixture: importing new test realm", flush=True)
            run([home / "bin/kc.sh", "import", "--optimized", "--file=" + str(realm_file), "--override=false", *db_options], environment=clean_environment(KC_DB_PASSWORD=db_password), timeout=120)
            run([home / "bin/kc.sh", "bootstrap-admin", "service", "--optimized", "--no-prompt", "--client-id=fixture-admin", "--client-secret:env", "KC_FIXTURE_ADMIN_SECRET", *db_options],
                environment=clean_environment(KC_DB_PASSWORD=db_password, KC_FIXTURE_ADMIN_SECRET=admin_secret), timeout=120)
            first = None; print("Disposable Keycloak fixture: starting both TLS members", flush=True)
            for index, directory in enumerate((home, other)):
                replacements = {"hostname": "https://localhost:" + str(https_a), "https-port": str((https_a, https_b)[index]),
                    "http-management-port": str((health_a, health_b)[index])}
                arguments = [next(("--" + k + "=" + v for k, v in replacements.items() if arg.startswith("--" + k + "=")), arg) for arg in common]
                arguments.append("--cache-embedded-network-bind-port=" + str(peer_ports[index]))
                arguments.append("--http-host=127.0.0.1")
                process = start("keycloak-" + str(index), [directory / "bin/kc.sh", *arguments], clean_environment(KC_DB_PASSWORD=db_password))
                if index == 0: first = process
            trust = ssl.create_default_context(cafile=str(root / "ca.crt"))
            http = build_opener(ProxyHandler({}), HTTPSHandler(context=trust))
            def health(port):
                with http.open("https://localhost:" + str(port) + "/health/ready", timeout=2) as response: return response.status == 200
            wait(lambda: health(health_a) and health(health_b)); report["two_process_tls_startup_verified"] = True
            loopback_listeners(processes, {pg_port, https_a, https_b, health_a, health_b}); report["owned_loopback_listeners_verified"] = True
            origin = "https://localhost:" + str(https_a); issuer = origin + "/realms/" + plan["realm"]
            print("Disposable Keycloak fixture: creating and verifying a fresh MFA realm", flush=True)
            with http.open(Request(origin + "/realms/master/protocol/openid-connect/token", data=urlencode({"grant_type": "client_credentials", "client_id": "fixture-admin", "client_secret": admin_secret}).encode()), timeout=15) as response:
                raw = response.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024: raise ValueError("Excessive fixture admin token response")
                admin_token = json.loads(raw)["access_token"]
            package_plan = fixture(); package_plan.update(hostname="localhost", public_port=https_a)
            bundle = root / "private/bundle"; bundle.mkdir(mode=0o700, parents=True)
            for filename, content in identity.render(package_plan, "raw").items(): (bundle / filename).write_text(content, encoding="utf-8", newline="\n")
            realm_plan = {"schema_version": 1, "contract_sha256": hashlib.sha256((bundle / "keycloak-contract.json").read_bytes()).hexdigest(),
                "server": {"url": origin, "master_realm_id": "00000000-0000-0000-0000-000000000000", "token_env": "KEYCLOAK_FIXTURE_ADMIN_TOKEN", "ca_file": str(root / "ca.crt"), "ca_sha256": hashlib.sha256((root / "ca.crt").read_bytes()).hexdigest()}}
            with mock.patch.dict(os.environ, {"KEYCLOAK_FIXTURE_ADMIN_TOKEN": admin_token}), mock.patch.object(bootstrap, "ROOT", root):
                realm_client = bootstrap.Client(realm_plan["server"])
                realm_plan["server"]["master_realm_id"] = realm_client.call("/admin/realms/master")["id"]
                test.assertTrue(bootstrap.operate(bundle, realm_plan, mode="check-server", output=root / "private/check")["server_checked"])
                operation = root / "private/create"
                test.assertTrue(bootstrap.operate(bundle, realm_plan, mode="apply", output=operation)["realm_policy_verified"])
                test.assertTrue(bootstrap.operate(bundle, realm_plan, mode="resume", output=operation)["realm_policy_verified"])
                original_id = realm_client.call("/admin/realms/" + plan["realm"])["id"]
                with test.assertRaises(ValueError): bootstrap.operate(bundle, realm_plan, mode="apply", output=root / "private/refuse-existing")
                test.assertEqual(realm_client.call("/admin/realms/" + plan["realm"])["id"], original_id)
            report["guarded_realm_creation_and_readback_verified"] = True; report["existing_realm_and_resume_boundaries_verified"] = True
            print("Disposable Keycloak fixture: creating and verifying the application OIDC client", flush=True)
            realm_proof = operation / "keycloak-realm-proof.json"
            client_plan = {"schema_version": 1, "realm_plan": realm_plan, "realm_proof": {"path": str(realm_proof), "sha256": hashlib.sha256(realm_proof.read_bytes()).hexdigest()},
                "application": {"kind": "argocd", "version": "3.5.0", "client_id": "fixture-client", "origin": "https://localhost:1", "client_secret_env": "KEYCLOAK_FIXTURE_CLIENT_CREDENTIAL",
                    "namespace": "argocd", "secret_name": "fixture-argocd-identity", "secret_key": "client-secret", "group_roles": [{"group": "/fixture-readers", "role": "readonly"}]}}
            with mock.patch.dict(os.environ, {"KEYCLOAK_FIXTURE_ADMIN_TOKEN": admin_token, "KEYCLOAK_FIXTURE_CLIENT_CREDENTIAL": client_secret}), mock.patch.object(application_identity, "ROOT", root):
                client_operation = root / "private/client-create"
                test.assertTrue(application_identity.operate(bundle, client_plan, mode="check-server", output=root / "private/client-check")["server_checked"])
                test.assertTrue(application_identity.operate(bundle, client_plan, mode="apply", output=client_operation)["client_policy_verified"])
                test.assertTrue(application_identity.operate(bundle, client_plan, mode="resume", output=client_operation)["client_policy_verified"])
                with test.assertRaises(ValueError): application_identity.operate(bundle, client_plan, mode="apply", output=root / "private/client-refuse-existing")
                for generated in client_operation.iterdir():
                    if generated.is_file() and client_secret.encode() in generated.read_bytes(): raise ValueError("Client credentials entered a generated application artifact")
            for resource, value in (("groups", {"name": "fixture-readers"}), ("users", realm["users"][0])):
                request = Request(origin + "/admin/realms/" + plan["realm"] + "/" + resource, data=json.dumps(value).encode(), headers={"Authorization": "Bearer " + admin_token, "Content-Type": "application/json"})
                with http.open(request, timeout=15) as response:
                    if response.status != 201: raise ValueError("Disposable browser identity preparation failed")
            print("Disposable Keycloak fixture: testing browser MFA", flush=True)
            verifier = secrets.token_urlsafe(48); challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            state = secrets.token_urlsafe(24)
            nonce = secrets.token_urlsafe(24)
            auth_fields = {"client_id": "fixture-client", "redirect_uri": CALLBACK, "response_type": "code", "scope": "openid profile email", "state": state,
                "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
            authorization = issuer + "/protocol/openid-connect/auth?" + urlencode(auth_fields)
            class CaptureRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs): return None
            rejection_http = build_opener(ProxyHandler({}), HTTPSHandler(context=trust), CaptureRedirect())
            def rejected(request, status, *, oidc_error_redirect=False, oauth_error=None):
                try:
                    with rejection_http.open(request, timeout=15): raise ValueError("An invalid OIDC request unexpectedly succeeded")
                except HTTPError as error:
                    actual, location = error.code, error.headers.get("Location", "")
                    try: body = error.read(16 * 1024 + 1) if oauth_error else b""
                    finally: error.close()
                    if oauth_error:
                        if len(body) > 16 * 1024: raise ValueError("Excessive OAuth rejection response")
                        rejection = json.loads(body)
                        if rejection.get("error") != oauth_error or any(k in rejection for k in ("access_token", "refresh_token", "id_token")): raise ValueError("The disabled grant did not return its expected OAuth rejection")
                    if oidc_error_redirect and actual == 302:
                        target, callback = urlsplit(location), urlsplit(CALLBACK); query = parse_qs(target.query)
                        if (target.scheme, target.netloc, target.path) != (callback.scheme, callback.netloc, callback.path) or query.get("error") != ["invalid_request"] or query.get("code") or query.get("state") != [state]: raise ValueError("Unexpected OIDC error redirect")
                    else: test.assertEqual(actual, status)
            rejected(issuer + "/protocol/openid-connect/auth?" + urlencode({**auth_fields, "redirect_uri": "https://localhost:1/unapproved"}), 400)
            rejected(issuer + "/protocol/openid-connect/auth?" + urlencode({k: v for k, v in auth_fields.items() if k not in {"code_challenge", "code_challenge_method"}}), 400, oidc_error_redirect=True)
            # Versioned Keycloak grant implementations use 400 for disabled
            # direct-password grants and 401 for disabled service accounts.
            # Both must identify unauthorized_client, not invalid credentials.
            for grant, status in (("password", 400), ("client_credentials", 401)):
                rejected(Request(issuer + "/protocol/openid-connect/token", data=urlencode({"grant_type": grant, "client_id": "fixture-client", "client_secret": client_secret, "username": "fixture-user", "password": login_password}).encode()), status, oauth_error="unauthorized_client")
            report["oidc_callback_pkce_and_disabled_grants_verified"] = True
            login_fields = {"username": "fixture-user"}; login_fields["password"] = login_password
            browser = Browser(trust); current, page = browser.fetch(authorization)
            current, page = browser.submit(current, Forms(page).select("username"), **login_fields)
            form = Forms(page).select("totpSecret")
            # Keycloak's hidden field holds the raw UTF-8 secret, whereas its
            # manual/QR authenticator seed is Base32 (TotpBean/TotpUtils).
            raw_seed = form["fields"]["totpSecret"]
            totp_secret = base64.b32encode(raw_seed.encode()).decode().rstrip("=")
            current, page = browser.submit(current, form, totp=otp(totp_secret), userLabel="disposable-fixture")
            test.assertTrue(current.startswith(CALLBACK + "?"), "OTP enrollment did not finish the fixture login")
            code = parse_qs(urlsplit(current).query)
            test.assertEqual(code.get("state"), [state]); test.assertEqual(len(code.get("code", [])), 1)
            def api(url, fields=None, bearer=None):
                request = Request(url, data=None if fields is None else urlencode(fields).encode(), headers={} if bearer is None else {"Authorization": "Bearer " + bearer})
                with http.open(request, timeout=15) as response:
                    raw = response.read(128 * 1024 + 1)
                    if len(raw) > 128 * 1024: raise ValueError("Excessive fixture API response")
                    return json.loads(raw)
            tokens = api(issuer + "/protocol/openid-connect/token", {"grant_type": "authorization_code", "client_id": "fixture-client", "client_secret": client_secret,
                "redirect_uri": CALLBACK, "code": code["code"][0], "code_verifier": verifier})
            test.assertTrue(tokens.get("refresh_token")); test.assertTrue(tokens.get("access_token"))
            user = api(issuer + "/protocol/openid-connect/userinfo", bearer=tokens["access_token"])
            test.assertEqual(user.get("preferred_username"), "fixture-user"); test.assertEqual(user.get("groups"), ["/fixture-readers"])
            report["otp_enrollment_and_login_verified"] = True; report["guarded_oidc_client_and_group_claim_verified"] = True
            encoded_header, encoded_claims, encoded_signature = tokens["id_token"].split(".")
            def decode(value): return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            header, claims = (json.loads(decode(value)) for value in (encoded_header, encoded_claims))
            test.assertEqual(header["alg"], "RS256"); test.assertEqual(claims["iss"], issuer); test.assertEqual(claims["nonce"], nonce)
            test.assertIn("fixture-client", claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]); test.assertGreater(claims["exp"], time.time())
            test.assertEqual(claims["sub"], user["sub"]); test.assertEqual(claims["groups"], ["/fixture-readers"])
            matches = [key for key in api(issuer + "/protocol/openid-connect/certs")["keys"] if key.get("kid") == header["kid"] and key.get("alg") == "RS256" and key.get("use") == "sig"]
            test.assertEqual(len(matches), 1); certificate = matches[0]["x5c"][0]
            cert_file = write("id-token-signing.crt", "-----BEGIN CERTIFICATE-----\n" + "\n".join(certificate[i:i + 64] for i in range(0, len(certificate), 64)) + "\n-----END CERTIFICATE-----\n")
            public_key = write("id-token-signing.pub", run(["/usr/bin/openssl", "x509", "-in", cert_file, "-pubkey", "-noout"]))
            message = write("id-token-message.txt", encoded_header + "." + encoded_claims)
            signature = root / "id-token-signature.bin"; signature.write_bytes(decode(encoded_signature)); signature.chmod(0o600); stage_owner(signature)
            run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", public_key, "-signature", signature, message])
            report["signed_id_token_claims_verified"] = True
            if application_check is not None:
                extra = application_check({"root": root, "write": write, "run": run, "start": start, "stop": stop, "wait": wait, "environment": clean_environment,
                    "private_logs": private_logs, "processes": processes, "trust": trust, "http": http, "origin": origin, "bundle": bundle, "realm_plan": realm_plan,
                    "realm_operation": operation, "admin_secret": admin_secret, "login_password": login_password, "totp_secret": totp_secret, "reader_subject": user["sub"], "reader_email": user["email"], "issuer": issuer,
                    "native_home": home, "java_home": java, "database_password": db_password, "database_port": pg_port, "realm_name": plan["realm"]})
                if not isinstance(extra, dict) or any(not k.startswith(("argocd_", "keycloak_host_")) or not (type(v) is bool or k in {"argocd_artifacts_sha256", "keycloak_host_artifacts_sha256"} and isinstance(v, dict) and all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) for x in v.values())) for k, v in extra.items()): raise ValueError("Application fixture returned invalid aggregate evidence")
                report.update(extra)
            browser = Browser(trust); current, page = browser.fetch(authorization)
            current, page = browser.submit(current, Forms(page).select("username"), **login_fields)
            test.assertFalse(current.startswith(CALLBACK + "?")); form = Forms(page).select("otp")
            valid_codes = {otp(totp_secret, now=time.time() + delta) for delta in (-30, 0, 30)}
            wrong = next(value for value in ("000000", "111111", "222222", "333333") if value not in valid_codes)
            current, page = browser.submit(current, form, otp=wrong)
            test.assertFalse(current.startswith(CALLBACK + "?")); Forms(page).select("otp")
            report["password_only_and_bad_otp_rejected"] = True
            print("Disposable Keycloak fixture: testing refresh after first member stops", flush=True)
            stop(first)
            other_issuer = "https://localhost:" + str(https_b) + "/realms/" + plan["realm"]
            refreshed = api(other_issuer + "/protocol/openid-connect/token", {"grant_type": "refresh_token", "client_id": "fixture-client", "client_secret": client_secret, "refresh_token": tokens["refresh_token"]})
            test.assertEqual(api(other_issuer + "/protocol/openid-connect/userinfo", bearer=refreshed["access_token"])["sub"], user["sub"])
            report["refresh_after_first_process_stopped"] = True
            private_logs([db_password, login_password, client_secret, admin_secret, admin_token, raw_seed, nonce, tokens["id_token"], tokens["access_token"], tokens["refresh_token"], refreshed["access_token"]])
            report["generated_private_values_absent_from_logs"] = True
            if source_inventory() != sources: raise ValueError("First-party runtime fixture sources changed during execution")
            report["source_sha256"] = sources
            report["database_tools_sha256"] = {name: hashlib.sha256((root / "pg/bin" / name).read_bytes()).hexdigest() for name in ("postgres", "initdb", "psql")}
        finally:
            for handle in reservations: handle.close()
            for process, log in reversed(processes): stop(process); log.close()
    report["temporary_processes_and_data_removed"] = True
    selected_report = os.environ.get("KEYCLOAK_TEST_REPORT")
    if selected_report:
        path = Path(selected_report).absolute()
        if path.resolve() != path or not path.is_relative_to((identity.ROOT / "private").resolve()) or not path.parent.is_dir(): raise ValueError("Use a new report file inside existing ignored private storage")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, sort_keys=True, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    return report


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--child": child()
