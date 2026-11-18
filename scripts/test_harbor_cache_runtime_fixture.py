#!/usr/bin/env python3
"""Pinned Harbor cache/pool constructors through a disposable native TLS gateway.

This does not start Harbor core, jobservice or the distribution registry.
"""

import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json


REVISION = "335c05f2b9507b4c7116fe69b84e31ef9f4b91dd"
BINARY_SHA = "aa6ba76c88925d15f7f116e0da49a89511c8f60bc989927bf3b7940d86144a6a"
GO_SHA = "00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1"
SOURCE_SHA = "eb2b81020144653c50adb58894d831268dd934b0a38e376281e12548b276dfd0"
SOURCES = {
    "src/go.mod": "2452db301573926edd007e67acdafb0178dcdbe2af7dc030e5d60d6addedf7c4",
    "src/go.sum": "11a39a7e13d325dbb236653c7758b0846a76d9e13085d7e19229524fb18cd7bd",
    "src/lib/cache/redis/redis.go": "b1781b60f111022064b9e8aecf32e2a0e79977e49352959e6fcb627e7579fe8e",
    "src/lib/cache/redis/util.go": "5cb26e89440fb9f02e98da3deaa2d6bc34c403d5e00d052a7b7cbc24baa03053",
    "src/lib/redis/pool.go": "bf7088f1ca73b3107698cb68c453cc772bd58b400a4fa81f5bb93183339974c0",
}
FIELDS = {"core_cache_verified", "legacy_pool_verified", "logical_databases_separate", "scoped_acl_verified"}


def artifact(directory):
    binary = directory / "harbor-cache-client"
    if binary.is_symlink() or not binary.is_file() or not 0 < binary.stat().st_size < 64 * 1024 ** 2:
        raise ValueError("Select the bounded original Harbor client fixture")
    with binary.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != BINARY_SHA: raise ValueError("The client artifact differs")
    manifest = loads_strict_json(read_bounded_bytes(directory / "build-inputs.json", max_bytes=16 * 1024))
    source = Path(__file__).parent / "fixtures/harbor-cache-client/main.go"
    expected = {"harbor_version": "2.15.1", "harbor_revision": REVISION, "go_version": "go1.25.9", "go_archive_sha256": GO_SHA,
        "harbor_archive_sha256": SOURCE_SHA, "fixture_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "harbor_sources_sha256": SOURCES,
        "binary_sha256": BINARY_SHA, "build_exit": {"exit_code": 0}, "original_build_process_and_cgroup_removed": True,
        "application_compatibility_verified": False, "publisher_signatures_verified": False}
    if json.dumps(manifest, sort_keys=True) != json.dumps(expected, sort_keys=True): raise ValueError("The exact typed source/toolchain build receipt is required")
    return binary


def stage_artifact(directory, target):
    source = artifact(directory)
    material = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
    if hashlib.sha256(material).hexdigest() != BINARY_SHA: raise ValueError("The original client changed before staging")
    with target.open("xb") as stream: stream.write(material)
    target.chmod(0o755)
    return target


class ClientChecks:
    def __init__(self):
        self.password = secrets.token_urlsafe(36)
        self.child = None
        self.phases = set()

    def auth_files(self, *args, **kwargs):
        files = super().auth_files(*args, **kwargs)
        # Only these constructor operations are under test. This is not a
        # production ACL for Harbor jobs, notifications or registry caching.
        files["server.acl"] += "user harbor_component reset on #" + hashlib.sha256(self.password.encode()).hexdigest() + (
            " ~harbor-component:* resetchannels -@all +ping +hello +client|setname +client|setinfo +select +get +set +del +exists\n")
        return files

    def client_check(self, context, phase, address):
        from test_valkey_runtime_fixture import CLEAN
        test, root = context["test"], context["root"]
        opener = build_opener(ProxyHandler({}))
        def response():
            try: handle = opener.open("http://127.0.0.1:19120/roundtrip", timeout=6)
            except HTTPError as error: handle = error
            with handle:
                raw = handle.read(4097)
                if len(raw) > 4096: raise ValueError("Bound the original client observation")
                result = loads_strict_json(raw)
                if not isinstance(result, dict) or set(result) != FIELDS or any(type(v) is not bool for v in result.values()): raise ValueError("Reject unknown client evidence")
                return result
        if phase == "initial":
            binary = stage_artifact(Path(os.environ["HARBOR_CACHE_TEST_ARTIFACTS"]), root / "harbor-cache-client")
            context["private_values"].append(self.password)
            context["run"](["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
                "-subj", "/CN=fixture-unrelated-cache-root", "-keyout", root / "other-ca.key", "-out", root / "other-ca.crt"])
            (root / "other-ca.key").chmod(0o600)
            def start(label, password, ca):
                return context["spawn"](label, [binary], {**CLEAN, "HARBOR_FIXTURE_PASSWORD": password,
                    "HARBOR_FIXTURE_ADDRESS": address + ":6381", "SSL_CERT_FILE": str(ca), "SSL_CERT_DIR": "/nonexistent"})
            for label, password, ca in (("wrong-password", secrets.token_urlsafe(36), root / "ca.crt"), ("untrusted-ca", self.password, root / "other-ca.crt")):
                context["private_values"].append(password)
                child = start("harbor-client-" + label, password, ca)
                context["wait"](lambda: not any(response().values()), seconds=15)
                context["stop"](child)
            self.child = start("harbor-client-original", self.password, root / "ca.crt")
            self.identity = Path("/proc", str(self.child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19]
        if self.child is None or self.child.poll() is not None: raise ValueError("Retain the original Harbor client process")
        test.assertEqual(Path("/proc", str(self.child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19], self.identity)
        status = dict(line.split(":", 1) for line in Path("/proc", str(self.child.pid), "status").read_text().splitlines() if ":" in line)
        test.assertEqual(status["Uid"].split()[0], "65534"); test.assertEqual(int(status["CapEff"].strip(), 16), 0)
        context["wait"](lambda: all(response().values()) if phase != "checker-stopped" else not any(response().values()), seconds=20)
        self.phases.add(phase)

    def exercise(self, context):
        result = super().exercise(context)
        if self.phases != {"initial", "promoted", "checker-stopped"}: raise ValueError("All original-client phases are required")
        result.update(valkey_gateway_harbor_core_cache_client_verified=True, valkey_gateway_harbor_legacy_pool_verified=True,
            valkey_gateway_harbor_client_logical_databases_separate=True, valkey_gateway_harbor_client_scope_enforced=True,
            valkey_gateway_harbor_clients_wrong_password_refused=True, valkey_gateway_harbor_clients_untrusted_ca_refused=True,
            valkey_gateway_harbor_clients_original_process_reconnect_verified=True, valkey_gateway_harbor_clients_checker_loss_refused=True,
            valkey_gateway_harbor_client_binaries_sha256={"client": BINARY_SHA}, valkey_gateway_harbor_jobservice_verified=False,
            valkey_gateway_harbor_registry_verified=False, valkey_gateway_harbor_core_application_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(ClientChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
