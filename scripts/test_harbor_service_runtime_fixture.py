#!/usr/bin/env python3
"""Opt-in actual Harbor services in a fresh namespace; not full Harbor HA."""

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import tarfile

import yaml

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json
from test_harbor_registry_runtime_fixture import RegistryChecks


REVISION = "335c05f2b9507b4c7116fe69b84e31ef9f4b91dd"
SOURCE_SHA = "eb2b81020144653c50adb58894d831268dd934b0a38e376281e12548b276dfd0"
GO_SHA = "00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1"
BINARIES = {"harbor-core": "e8f62f7c58209e192c5bbcbc8f5fcf33f1e0815fab7cf75a9f2ac119ec81812f",
    "harbor-jobservice": "9890f53be225edc131c1e86c383ddd7210f7f96eef92939c57fa1fdfe6e7106e",
    "harbor-registryctl": "42ab076f5c8da5f5ce1fd8d9f88585c3d6355a28a69ffcda534207c010a1bad1"}


def stage_binary(directory, name, target):
    if name not in BINARIES: raise ValueError("Select an original service")
    source = directory / name
    if source.is_symlink(): raise ValueError("Do not substitute a service link")
    raw = read_bounded_bytes(source, max_bytes=96 * 1024 ** 2)
    if hashlib.sha256(raw).hexdigest() != BINARIES[name]: raise ValueError("The original service executable changed")
    receipt = loads_strict_json(read_bounded_bytes(directory / "build-inputs.json", max_bytes=1024 ** 2))
    if not isinstance(receipt, dict) or (name != "harbor-registryctl" and not isinstance(receipt.get("binaries_sha256"), dict)):
        raise ValueError("Use a typed original service receipt")
    exit_receipt = {"exit_code": 0} if name == "harbor-registryctl" else {k: {"exit_code": 0} for k in ("generator-build", "api-generation", "core", "jobservice")}
    binary_receipt = receipt.get("binary_sha256") if name == "harbor-registryctl" else receipt.get("binaries_sha256", {}).get(name)
    if (any(receipt.get(k) is not True for k in ("original_sources_unchanged", "original_process_and_cgroup_removed")) or receipt.get("source_revision") != REVISION or receipt.get("source_archive_sha256") != SOURCE_SHA
            or receipt.get("go_archive_sha256") != GO_SHA or receipt.get("runtime_verified") is not False or binary_receipt != BINARIES[name]
            or json.dumps(receipt.get("exit"), sort_keys=True) != json.dumps(exit_receipt, sort_keys=True)):
        raise ValueError("The original service build provenance changed")
    with target.open("xb") as stream: stream.write(raw)
    target.chmod(0o755)
    return target


def stage_migrations(archive, context):
    if archive.is_symlink(): raise ValueError("Use the original source archive")
    raw = read_bounded_bytes(archive, max_bytes=16 * 1024 ** 2)
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA: raise ValueError("The migration source archive changed")
    prefix = "harbor-" + REVISION + "/make/migrations/postgresql/"
    seen = set(); total = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for item in bundle:
            if not item.name.startswith(prefix) or not item.isfile(): continue
            name = item.name[len(prefix):]
            if not re.fullmatch(r"[0-9]+_[A-Za-z0-9_.-]+\.sql", name) or name in seen or not 0 < item.size <= 2 * 1024 ** 2:
                raise ValueError("Bound original migration members")
            total += item.size
            if total > 16 * 1024 ** 2: raise ValueError("Bound total migrations")
            seen.add(name)
            context["write"]("service-inputs/migrations/" + name, bundle.extractfile(item).read().decode(), owner=True, mode=0o600)
    if not seen: raise ValueError("Original migration scripts missing")
    return context["root"] / "service-inputs/migrations", max(int(name.split("_", 1)[0]) for name in seen if name.endswith(".up.sql"))


class ServiceClient:
    def __init__(self, address, port, trust, leaf):
        if (address, port) not in {("192.0.2.91", 8443), ("192.0.2.92", 9443), ("192.0.2.93", 9444)}: raise ValueError("Use original isolated service addresses")
        self.address, self.port, self.trust, self.leaf = address, port, trust, leaf

    def request(self, method, path, payload=None, authorization=None):
        if method not in {"GET", "POST"} or not re.fullmatch(r"/api/(?:v[12](?:\.0)?/)?[A-Za-z0-9_/-]+", path): raise ValueError("Bound service request")
        owner = self
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                if hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != owner.leaf:
                    self.close(); raise ValueError("Original service TLS identity changed")
        connection = Pinned(self.address, self.port, context=self.trust, timeout=4)
        body = b"" if payload is None else json.dumps(payload).encode()
        if len(body) > 65536: raise ValueError("Bound service payload")
        headers = {"Content-Type": "application/json"}
        if authorization: headers["Authorization"] = authorization
        try:
            connection.request(method, path, body=body, headers=headers)
            result = connection.getresponse(); raw = result.read(1024 ** 2 + 1)
            if len(raw) > 1024 ** 2: raise ValueError("Bound service response")
            return result.status, raw
        finally: connection.close()


class ServiceChecks:
    def __init__(self):
        super().__init__()
        self.service_children = []; self.service_identities = []; self.completed_jobs = []; self.service_binary_hashes = {}
        self.core_secret, self.job_secret, self.db_password, self.admin_password, self.core_cache_password, self.job_cache_password = (secrets.token_urlsafe(36) for _ in range(6))

    def stage_service_binary(self, directory, name, target):
        return stage_binary(directory, name, target)

    def auth_files(self, *args, **kwargs):
        files = super().auth_files(*args, **kwargs)
        # Core session SIDs are unprefixed. These temporary application users
        # intentionally share the isolated cache; this is not tenant isolation.
        commands = "ping select get set setnx setex psetex getset exists del unlink expire pexpire expireat pexpireat ttl pttl persist rename renamenx incr incrby decr decrby mget mset hget hset hmget hmset hgetall hsetnx hdel hincrby hkeys hvals hscan hlen hexists sadd srem smembers sismember scard spop srandmember zadd zrem zcard zscore zrange zrangebyscore zrevrange zrevrangebyscore zremrangebyscore zcount zincrby lpush rpush lpop rpop lrange llen lrem ltrim rpoplpush brpoplpush blpop brpop eval evalsha script|load script|exists time client|setname client|setinfo multi exec discard watch unwatch scan type"
        for name, password in (("harbor_core_service", self.core_cache_password), ("harbor_job_service", self.job_cache_password)):
            files["server.acl"] += "user " + name + " reset on #" + hashlib.sha256(password.encode()).hexdigest() + " allkeys resetchannels -@all " + " ".join("+" + c for c in commands.split()) + "\n"
        return files

    def sql(self, statement, database="postgres"):
        pg = self.pg
        environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(pg["root"])}
        if pg["libraries"]: environment["LD_LIBRARY_PATH"] = pg["libraries"]
        primary = pg.get("primary", {"index": 1, "port": 5432})
        command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", pg["binaries"] / "psql", "-XAtq", "--no-password", "-v", "ON_ERROR_STOP=1", "-h", pg["root"] / ("socket-" + str(primary["index"])), "-p", str(primary["port"]), "-U", "postgres", "-d", database]
        result = run_bounded(list(map(str, command)), input=statement, text=True, env=environment, timeout=30, check=False, output_max_bytes=256 * 1024)
        if result.returncode: raise ValueError("The isolated application SQL operation failed; private diagnostics suppressed")
        return result.stdout.strip()

    def prepare_database(self):
        self.sql("CREATE ROLE harbor_fixture LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '" + self.db_password + "';\nCREATE DATABASE registry OWNER harbor_fixture;\n")
        for index in (1, 2, 3):
            data = self.pg["root"] / ("data-" + str(index))
            path = data / "pg_hba.conf"
            with path.open("a") as stream: stream.write("hostssl registry harbor_fixture 127.0.0.1/32 scram-sha-256\n")
            command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", self.pg["binaries"] / "pg_ctl", "-D", data, "reload"]
            environment = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": self.pg["libraries"]}
            result = run_bounded(list(map(str, command)), text=True, env=environment, timeout=15, check=False, output_max_bytes=16384)
            if result.returncode: raise ValueError("The original application HBA reload failed")

    def start_services(self, context, gateway_address):
        from test_valkey_runtime_fixture import CLEAN
        root, write, run = context["root"], context["write"], context["run"]
        self.prepare_database()
        context["private_values"].extend((self.core_secret, self.job_secret, self.db_password, self.admin_password, self.core_cache_password, self.job_cache_password))
        migrations, self.schema_version = stage_migrations(Path(os.environ["HARBOR_SERVICE_TEST_SOURCE_ARCHIVE"]), context)
        signing = root / "service-signing.key"
        run(["/usr/bin/openssl", "genrsa", "-traditional", "-out", signing, "2048"])
        signing.chmod(0o600); os.chown(signing, 65534, 65534)
        encryption = secrets.token_hex(16); context["private_values"].append(encryption)
        key = write("service-inputs/key", encryption, owner=True, mode=0o600)
        core_bind = getattr(self, "core_bind_address", "192.0.2.91")
        if core_bind not in {"192.0.2.91", "192.0.2.94"}: raise ValueError("Use a reviewed core bind address")
        write("conf/app.conf", "appname = Harbor\nrunmode = prod\n[prod]\nhttpsaddr = " + core_bind + "\n")
        logdir = root / "service-job-logs"; logdir.mkdir(mode=0o700); os.chown(logdir, 65534, 65534)
        common = {**CLEAN, "SSL_CERT_FILE": str(root / "ca.crt"), "SSL_CERT_DIR": "/nonexistent", "PGSSLROOTCERT": str(self.pg["root"] / "ca.crt"),
            "KEY_PATH": str(key), "TOKEN_PRIVATE_KEY_PATH": str(signing), "CORE_SECRET": self.core_secret, "JOBSERVICE_SECRET": self.job_secret,
            "CORE_URL": "https://192.0.2.91:8443", "CORE_LOCAL_URL": "https://192.0.2.91:8443", "EXT_ENDPOINT": "https://192.0.2.91:8443",
            "JOBSERVICE_URL": "https://192.0.2.92:9443", "REGISTRY_CONTROLLER_URL": "https://192.0.2.93:9444", "REGISTRY_URL": "https://192.0.2.82:5443",
            "REGISTRY_CREDENTIAL_USERNAME": "registry-fixture", "REGISTRY_CREDENTIAL_PASSWORD": self.password,
            "DATABASE_TYPE": "postgresql", "POSTGRESQL_HOST": "127.0.0.1", "POSTGRESQL_PORT": str(getattr(self, "database_client_port", 5432)), "POSTGRESQL_USERNAME": "harbor_fixture", "POSTGRESQL_PASSWORD": self.db_password,
            "POSTGRESQL_DATABASE": "registry", "POSTGRESQL_SSLMODE": "verify-full", "POSTGRESQL_MAX_OPEN_CONNS": "8", "POSTGRESQL_MAX_IDLE_CONNS": "2",
            "POSTGRES_MIGRATION_SCRIPTS_PATH": str(migrations), "HARBOR_ADMIN_PASSWORD": self.admin_password, "AUTH_MODE": "db_auth", "WITH_TRIVY": "false", "METRIC_ENABLE": "false", "LOG_LEVEL": "error"}
        self.core_cache_url = "rediss://harbor_core_service:" + self.core_cache_password + "@" + gateway_address + ":6381/0"
        self.job_cache_url = "rediss://harbor_job_service:" + self.job_cache_password + "@" + gateway_address + ":6381/1"
        common.update(_REDIS_URL_CORE=self.core_cache_url, _REDIS_URL_HARBOR=self.core_cache_url,
            _REDIS_URL_REG=self.core_cache_url.rsplit("/", 1)[0] + "/2")
        self.service_clients = {}
        for name, address, port in (("harbor-registryctl", "192.0.2.93", 9444), ("harbor-core", "192.0.2.91", 8443), ("harbor-jobservice", "192.0.2.92", 9443)):
            directory = Path(os.environ["HARBOR_REGISTRYCTL_TEST_ARTIFACTS"] if name == "harbor-registryctl" else os.environ["HARBOR_SERVICE_TEST_ARTIFACTS"])
            binary = self.stage_service_binary(directory, name, root / name)
            self.service_binary_hashes[name] = hashlib.sha256(binary.read_bytes()).hexdigest()
            run(["/usr/sbin/ip", "address", "add", address + "/32", "dev", "lo"])
            extra_sans = getattr(self, "core_extra_sans", ()) if name == "harbor-core" else getattr(self, "jobservice_extra_sans", ()) if name == "harbor-jobservice" else ()
            extension = write("service-inputs/" + name + ".ext", "subjectAltName=" + ",".join(["IP:" + address, *extra_sans]) + "\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n")
            run(["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=" + name + ".example.invalid", "-keyout", root / (name + ".key"), "-out", root / (name + ".csr")])
            run(["/usr/bin/openssl", "x509", "-req", "-in", root / (name + ".csr"), "-CA", root / "ca.crt", "-CAkey", root / "ca.key", "-CAcreateserial", "-days", "1", "-extfile", extension, "-out", root / (name + ".crt")])
            private_key = root / (name + ".key"); private_key.chmod(0o600); os.chown(private_key, 65534, 65534)
            certificate = root / (name + ".crt")
            trust = ssl.create_default_context(cafile=str(root / "ca.crt"))
            trust.minimum_version = ssl.TLSVersion.TLSv1_2
            leaf = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()
            client = ServiceClient(address, port, trust, leaf); self.service_clients[name] = client
            environment = {**common, "INTERNAL_TLS_ENABLED": "true", "INTERNAL_TLS_CERT_PATH": str(certificate), "INTERNAL_TLS_KEY_PATH": str(private_key)}
            args = [binary]
            if name == "harbor-registryctl":
                configuration = {"protocol": "https", "port": str(port), "log_level": "error", "https_config": {"cert": str(certificate), "key": str(private_key)}, "registry_config": str(root / "registry-inputs/1.yaml")}
            elif name == "harbor-jobservice":
                backend_port = getattr(self, "jobservice_backend_port", port)
                if type(backend_port) is not int or backend_port not in {9443, 9446}: raise ValueError("Use a reviewed jobservice backend port")
                configuration = {"protocol": "https", "port": backend_port, "https_config": {"cert": str(certificate), "key": str(private_key)},
                    "worker_pool": {"workers": 2, "backend": "redis", "redis_pool": {"redis_url": self.job_cache_url, "namespace": "harbor_job_service_namespace", "idle_timeout_second": 30}},
                    "job_loggers": [{"name": "FILE", "level": "INFO", "settings": {"base_dir": str(logdir)}}], "loggers": [{"name": "STD_OUTPUT", "level": "ERROR"}]}
            if name != "harbor-core": args += ["-c", write("service-inputs/" + name + ".yaml", yaml.safe_dump(configuration), owner=True, mode=0o600)]
            child = context["spawn"](name, args, environment); self.service_children.append(child)
            self.service_identities.append(Path("/proc", str(child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])
            if hasattr(self, "service_started"): self.service_started(context, name, child, environment)
            path = "/api/v2.0/ping" if name == "harbor-core" else "/api/v1/stats" if name == "harbor-jobservice" else "/api/health"
            context["wait"](lambda: client.request("GET", path)[0] == 200, seconds=60)
            if hasattr(self, "service_ready"): self.service_ready(context, name)
            unrelated = ssl.create_default_context(cafile=str(root / "registry-other.crt"))
            with context["test"].assertRaises(ssl.SSLCertVerificationError): ServiceClient(address, port, unrelated, leaf).request("GET", path)
            with context["test"].assertRaisesRegex(ValueError, "TLS identity"): ServiceClient(address, port, trust, "0" * 64).request("GET", path)
            with socket.create_connection((address, port), timeout=3) as clear:
                clear.settimeout(3); clear.sendall(b"GET /api/health HTTP/1.1\r\nHost: fixture.example.invalid\r\nConnection: close\r\n\r\n")
                try: response = clear.recv(4096)
                except OSError: response = b""
                context["test"].assertTrue(not response or b"Client sent an HTTP request to an HTTPS server." in response)
        context["test"].assertEqual(self.sql("SELECT version::text || ':' || dirty::text FROM schema_migrations;", "registry"), str(self.schema_version) + ":false")

    def client_check(self, context, phase, gateway_address):
        super().client_check(context, phase, gateway_address)
        if phase == "initial": self.start_services(context, gateway_address)
        if phase == "checker-stopped": return  # Full service cache-outage recovery is a separate, unqualified gate.
        self.service_workload(context, phase)

    def service_workload(self, context, phase):
        if phase not in {"initial", "core-peer-loss", "job-peer-loss", "promoted", "database-promoted"}: raise ValueError("Use a reviewed workload phase")
        test = context["test"]; core = self.service_clients["harbor-core"]
        authorization = "Basic " + base64.b64encode(("admin:" + self.admin_password).encode()).decode()
        payload = {"project_name": "service-" + phase, "public": False}
        test.assertIn(core.request("POST", "/api/v2.0/projects", payload)[0], (401, 403))
        wrong_auth = "Basic " + base64.b64encode(("admin:" + secrets.token_urlsafe(36)).encode()).decode()
        test.assertIn(core.request("POST", "/api/v2.0/projects", payload, wrong_auth)[0], (401, 403))
        test.assertEqual(core.request("POST", "/api/v2.0/projects", payload, authorization)[0], 201)
        status, raw = core.request("GET", "/api/v2.0/projects/service-" + phase, authorization=authorization)
        test.assertEqual(status, 200); test.assertEqual(loads_strict_json(raw)["name"], "service-" + phase)
        test.assertEqual(self.sql("SELECT count(*) FROM project WHERE name='service-" + phase + "';", "registry"), "1")
        test.assertEqual(core.request("GET", "/api/v2.0/projects/service-initial", authorization=authorization)[0], 200)
        self.submit_dry_run_job(context)
        for child, ticks in zip(self.service_children, self.service_identities):
            if child in getattr(self, "intentional_service_losses", ()):
                test.assertIsNotNone(child.poll()); continue
            test.assertIsNone(child.poll()); test.assertEqual(Path("/proc", str(child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19], ticks)
            status = dict(line.split(":", 1) for line in Path("/proc", str(child.pid), "status").read_text().splitlines() if ":" in line)
            test.assertEqual(status["Uid"].split()[0], "65534"); test.assertEqual(int(status["CapEff"].strip(), 16), 0)
        for path in context["root"].glob("harbor-*.log"):
            if "NOPERM" in read_bounded_bytes(path, max_bytes=4 * 1024 ** 2).decode(errors="replace"):
                raise ValueError("Original services must not silently ignore cache permission errors; private diagnostics suppressed")

    def submit_dry_run_job(self, context):
        test, job = context["test"], self.service_clients["harbor-jobservice"]
        test.assertIn(job.request("POST", "/api/v1/jobs", {"job": {"name": "PURGE_AUDIT_LOG"}})[0], (401, 403))
        request = {"job": {"name": "PURGE_AUDIT_LOG", "parameters": {"audit_retention_hour": 24, "dry_run": True, "include_event_types": "pull"}, "metadata": {"kind": "Generic", "unique": False}, "status_hook": ""}}
        auth = "Harbor-Secret " + self.core_secret
        test.assertIn(job.request("POST", "/api/v1/jobs", request, "Harbor-Secret " + secrets.token_urlsafe(36))[0], (401, 403))
        status, raw = job.request("POST", "/api/v1/jobs", request, auth)
        test.assertEqual(status, 202)
        identifier = loads_strict_json(raw)["job"]["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier): raise ValueError("Bound original job ID")
        def complete():
            status, raw = job.request("GET", "/api/v1/jobs/" + identifier, authorization=auth)
            if status != 200: return False
            state = loads_strict_json(raw)["job"]["status"]
            if state in {"Error", "Stopped", "Cancelled", "Failed"}:
                raise ValueError("Original SQL-backed audit dry run terminated unsuccessfully; retain private service diagnostics")
            return state == "Success"
        context["wait"](complete, seconds=45)
        status, raw = job.request("GET", "/api/v1/jobs/" + identifier + "/log", authorization=auth)
        test.assertEqual(status, 200); test.assertIn(b"[DRYRUN]Purged", raw)
        test.assertGreaterEqual(int(self.sql("SELECT count(*) FROM pg_stat_activity a JOIN pg_stat_ssl s ON a.pid=s.pid WHERE a.usename='harbor_fixture' AND s.ssl;")), 2)
        test.assertEqual(self.sql("SELECT count(*) FROM pg_stat_activity a LEFT JOIN pg_stat_ssl s ON a.pid=s.pid WHERE a.usename='harbor_fixture' AND NOT COALESCE(s.ssl, false);"), "0")
        self.completed_jobs.append(identifier)
        return identifier, raw

    def exercise(self, context):
        import test_postgres_runtime_fixture as postgres
        result = {}
        def connected(pg):
            self.pg = pg
            try: result.update(super(ServiceChecks, self).exercise(context))
            finally:
                for child in reversed(self.service_children): context["stop"](child)
                for path in pg["root"].glob("postgres-*.log"):
                    if self.db_password in read_bounded_bytes(path, max_bytes=4 * 1024 ** 2).decode(errors="replace"):
                        raise ValueError("Application credentials appeared in private database logs; details suppressed")
        postgres.exercise(context["test"], hook=connected, planned_promotion=getattr(self, "planned_database_promotion", False))
        expected = getattr(self, "expected_service_jobs", 3 if getattr(self, "planned_database_promotion", False) else 2)
        if type(expected) is not int or expected not in {2, 3, 4, 6}: raise ValueError("Select the exact reviewed workload count")
        if len(self.completed_jobs) != expected or len(set(self.completed_jobs)) != expected: raise ValueError("Require fresh actual jobs in every selected promotion phase")
        result.update(valkey_gateway_harbor_actual_services_startup_verified=True, valkey_gateway_harbor_release_sql_migrations_verified=True,
            valkey_gateway_harbor_core_project_roundtrip_verified=True, valkey_gateway_harbor_actual_dry_run_jobs_verified=True,
            valkey_gateway_harbor_service_tls_authentication_negatives_verified=True, valkey_gateway_harbor_actual_database_tls_verified=True,
            valkey_gateway_harbor_original_services_after_cache_promotion_verified=True, valkey_gateway_harbor_service_binaries_sha256=dict(self.service_binary_hashes),
            valkey_gateway_harbor_full_service_ha_verified=False, valkey_gateway_harbor_database_promotion_verified=False,
            valkey_gateway_harbor_service_cache_outage_recovery_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
