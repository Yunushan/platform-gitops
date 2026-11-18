#!/usr/bin/env python3
"""Two real pinned registry daemons; local shared storage is not Ceph/Harbor HA."""

import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import tarfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import yaml

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
from valkey_protocol import Connection, ReplyError


REVISION = "086d3c33b3981cf598d0d82336a907fcf5c4c848"
SOURCE_SHA = "2651addb228cfb4a23c10822c6d8f77b51964bd5f24ea84989d7a32e66e941f3"
GO_SHA = "00859d7bd6defe8bf84d9db9e57b9a4467b2887c18cd93ae7460e713db774bc1"
BINARY_SHA = "76e40d525ad206ab6bc426bc2ee34f265fb8f097147d081cdf62a9bd0e7264a4"
BCRYPT_SHA = "b9e5f29ceac3be6e6127b97d2ba034b01950776fdf3983cfaac08bd10ad0bfa3"
REPOSITORY = "/v2/fixture/ha"
MEDIA = "application/vnd.oci.image.manifest.v1+json"


def digest(raw): return "sha256:" + hashlib.sha256(raw).hexdigest()


def stage_artifact(directory, target):
    manifest = loads_strict_json(read_bounded_bytes(directory / "build-inputs.json", max_bytes=16 * 1024))
    expected = {"exit": {"exit_code": 0}, "sources_unchanged": True, "original_process_and_cgroup_removed": True, "built": True,
        "runtime_verified": False, "publisher_signatures_verified": False, "source_archive_sha256": SOURCE_SHA, "source_revision": REVISION,
        "go_archive_sha256": GO_SHA, "tags": ["include_oss", "include_gcs"], "binary_sha256": BINARY_SHA}
    if (not isinstance(manifest, dict) or set(manifest) != set(expected) | {"seconds", "log_sha256", "version_output"}
            or json.dumps({k: manifest[k] for k in expected}, sort_keys=True) != json.dumps(expected, sort_keys=True)
            or type(manifest["seconds"]) not in {int, float} or not 0 < manifest["seconds"] <= 600
            or not isinstance(manifest["log_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["log_sha256"])
            or not isinstance(manifest["version_output"], str) or len(manifest["version_output"]) > 2048
            or not manifest["version_output"].endswith(" github.com/docker/distribution 2.8.3-harbor.1-rc.1")):
        raise ValueError("Use the original typed registry build receipt")
    source = directory / "registry"
    if source.is_symlink(): raise ValueError("Do not substitute an executable link")
    raw = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
    if hashlib.sha256(raw).hexdigest() != BINARY_SHA: raise ValueError("The reviewed registry executable changed")
    with target.open("xb") as stream: stream.write(raw)
    target.chmod(0o755)
    return target


def upload_location(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value): raise ValueError("Bound upload state")
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc or parsed.fragment or not re.fullmatch(re.escape(REPOSITORY) + r"/blobs/uploads/[0-9a-f-]{36}", parsed.path)):
        raise ValueError("Upload credentials must stay on the reviewed relative repository path")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"_state"} or len(query["_state"]) != 1 or not query["_state"][0] or len(query["_state"][0]) > 4096: raise ValueError("Use exactly one original upload state")
    return parsed.path + "?" + urlencode({"_state": query["_state"][0]})


class Registry:
    def __init__(self, address, context, leaf, password):
        if address not in {"192.0.2.81", "192.0.2.82"}: raise ValueError("Use only the isolated original registry addresses")
        self.address, self.context, self.leaf, self.password = address, context, leaf, password

    def request(self, method, path, body=b"", *, content_type="application/octet-stream", authorized=True, password=None):
        parsed = urlsplit(path)
        if method not in {"GET", "HEAD", "POST", "PATCH", "PUT"} or not path.startswith("/v2/") or parsed.scheme or parsed.netloc or parsed.fragment or len(path) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in path): raise ValueError("Bound original registry requests")
        if not isinstance(body, bytes) or len(body) > 1024 ** 2: raise ValueError("Bound registry payloads")
        owner = self
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                if hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != owner.leaf:
                    self.close(); raise ValueError("The registry TLS invocation changed")
        connection = Pinned(self.address, 5443, context=self.context, timeout=4)
        headers = {"Content-Type": content_type, "Accept": MEDIA}
        if authorized: headers["Authorization"] = "Basic " + base64.b64encode(("registry-fixture:" + (self.password if password is None else password)).encode()).decode()
        try:
            connection.request(method, path, body=body, headers=headers)
            result = connection.getresponse(); raw = result.read(1024 ** 2 + 1)
            if len(raw) > 1024 ** 2: raise ValueError("Bound registry responses")
            return result.status, {k.lower(): v for k, v in result.getheaders()}, raw
        finally: connection.close()


def image(phase):
    data = ("synthetic registry witness: " + phase + "\n").encode(); output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo("witness.txt"); entry.size = len(data); entry.mode = 0o644; archive.addfile(entry, io.BytesIO(data))
    layer = output.getvalue()
    config = json.dumps({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}, "config": {}}, sort_keys=True, separators=(",", ":")).encode()
    manifest = json.dumps({"schemaVersion": 2, "mediaType": MEDIA,
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest(config), "size": len(config)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": digest(layer), "size": len(layer)}]}, sort_keys=True, separators=(",", ":")).encode()
    return layer, config, manifest


def publish(writer, finisher, phase):
    parts = image(phase)
    for blob in parts[:2]:
        status, headers, _ = writer.request("POST", REPOSITORY + "/blobs/uploads/")
        if status != 202: raise ValueError("An authenticated upload did not start: HTTP " + str(status))
        location = upload_location(headers.get("location"))
        status, headers, _ = writer.request("PATCH", location, blob)
        if status != 202: raise ValueError("An original upload chunk was refused: HTTP " + str(status))
        location = upload_location(headers.get("location"))
        status, headers, _ = finisher.request("PUT", location + "&" + urlencode({"digest": digest(blob)}))
        if status != 201 or headers.get("docker-content-digest") != digest(blob): raise ValueError("Cross-replica blob completion differs: HTTP " + str(status))
    status, headers, _ = finisher.request("PUT", REPOSITORY + "/manifests/" + phase, parts[2], content_type=MEDIA)
    if status != 201 or headers.get("docker-content-digest") != digest(parts[2]): raise ValueError("The original OCI manifest differs: HTTP " + str(status))
    return parts


def retrieve(client, phase, parts):
    status, headers, raw = client.request("GET", REPOSITORY + "/manifests/" + phase)
    if status != 200 or raw != parts[2] or headers.get("docker-content-digest") != digest(parts[2]): raise ValueError("The served OCI manifest differs")
    for blob in parts[:2]:
        status, headers, raw = client.request("GET", REPOSITORY + "/blobs/" + digest(blob))
        if status != 200 or raw != blob or headers.get("docker-content-digest") != digest(blob): raise ValueError("The served OCI blob differs")


class RegistryChecks:
    def __init__(self):
        self.password, self.cache_password, self.http_secret = (secrets.token_urlsafe(36) for _ in range(3))
        self.phases = set()

    def auth_files(self, *args, **kwargs):
        files = super().auth_files(*args, **kwargs)
        files["server.acl"] += "user harbor_registry reset on #" + hashlib.sha256(self.cache_password.encode()).hexdigest() + (
            " ~blobs::* ~repository::fixture/ha::* resetchannels -@all +ping +select +hmget +hmset +hget +hset +hsetnx +hdel +sadd +sismember\n")
        return files

    def client_check(self, context, phase, gateway_address):
        import bcrypt
        from bcrypt import _bcrypt
        from test_valkey_runtime_fixture import CLEAN
        root, test, run, write = context["root"], context["test"], context["run"], context["write"]
        if phase == "initial":
            if bcrypt.__version__ != "5.0.0" or hashlib.sha256(Path(_bcrypt.__file__).read_bytes()).hexdigest() != BCRYPT_SHA: raise ValueError("Use the reviewed credential-generation runtime")
            context["private_values"].extend((self.password, self.cache_password, self.http_secret))
            binary = stage_artifact(Path(os.environ["HARBOR_REGISTRY_TEST_ARTIFACTS"]), root / "registry")
            password_hash = bcrypt.hashpw(self.password.encode(), bcrypt.gensalt(rounds=10)).decode(); context["private_values"].append(password_hash)
            htpasswd = write("registry-inputs/htpasswd", "registry-fixture:" + password_hash + "\n", owner=True, mode=0o600)
            extension = write("registry.ext", "subjectAltName=DNS:registry-fixture.example.invalid,IP:192.0.2.81,IP:192.0.2.82\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n")
            run(["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=registry-fixture.example.invalid", "-keyout", root / "registry.key", "-out", root / "registry.csr"])
            run(["/usr/bin/openssl", "x509", "-req", "-in", root / "registry.csr", "-CA", root / "ca.crt", "-CAkey", root / "ca.key", "-CAcreateserial", "-days", "1", "-extfile", extension, "-out", root / "registry.crt"])
            (root / "registry.key").chmod(0o600)
            key = write("registry-inputs/tls.key", (root / "registry.key").read_text(), owner=True, mode=0o600)
            certificate = write("registry-inputs/tls.crt", (root / "registry.crt").read_text())
            store = root / "registry-store"; store.mkdir(mode=0o700); os.chown(store, 65534, 65534)
            self.children, self.identities, self.clients = [], [], []
            trust = ssl.create_default_context(cafile=str(root / "ca.crt")); trust.minimum_version = ssl.TLSVersion.TLSv1_2
            leaf = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()
            for index, address in enumerate(("192.0.2.81", "192.0.2.82")):
                run(["/usr/sbin/ip", "address", "add", address + "/32", "dev", "lo"])
                configuration = {"version": 0.1, "log": {"level": "error"}, "storage": {"filesystem": {"rootdirectory": str(store)}, "cache": {"layerinfo": "redis"}, "maintenance": {"uploadpurging": {"enabled": False}}},
                    "http": {"addr": address + ":5443", "relativeurls": True, "secret": self.http_secret, "tls": {"certificate": str(certificate), "key": str(key), "minimumtls": "tls1.2"}},
                    "auth": {"htpasswd": {"realm": "isolated-registry", "path": str(htpasswd)}},
                    "redis": {"addr": gateway_address + ":6381", "username": "harbor_registry", "password": self.cache_password, "db": 2, "enableTLS": True,
                        "dialtimeout": "1s", "readtimeout": "1s", "writetimeout": "1s", "pool": {"maxidle": 4, "maxactive": 16, "idletimeout": "30s"}}}
                path = write("registry-inputs/" + str(index) + ".yaml", yaml.safe_dump(configuration), owner=True, mode=0o600)
                child = context["spawn"]("harbor-registry-" + str(index), [binary, "serve", path], {**CLEAN, "SSL_CERT_FILE": str(root / "ca.crt"), "SSL_CERT_DIR": "/nonexistent"})
                self.children.append(child); self.identities.append(Path("/proc", str(child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19])
                self.clients.append(Registry(address, trust, leaf, self.password))
            for client in self.clients:
                context["wait"](lambda: client.request("GET", "/v2/")[0] == 200, seconds=15)
                test.assertEqual(client.request("GET", "/v2/", authorized=False)[0], 401)
                wrong = secrets.token_urlsafe(36); test.assertNotEqual(wrong, self.password); context["private_values"].append(wrong)
                test.assertEqual(client.request("GET", "/v2/", password=wrong)[0], 401)
                with test.assertRaisesRegex(ValueError, "TLS invocation"):
                    Registry(client.address, trust, "0" * 64, self.password).request("GET", "/v2/")
                with socket.create_connection((client.address, 5443), timeout=3) as clear:
                    clear.settimeout(3); clear.sendall(b"GET /v2/ HTTP/1.1\r\nHost: registry-fixture.example.invalid\r\nConnection: close\r\n\r\n")
                    try: response = clear.recv(4096)
                    except OSError: response = b""
                    test.assertTrue(not response or b"Client sent an HTTP request to an HTTPS server." in response)
            run(["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1", "-subj", "/CN=fixture-unrelated-registry-ca",
                "-keyout", root / "registry-other.key", "-out", root / "registry-other.crt"])
            (root / "registry-other.key").chmod(0o600)
            unrelated = ssl.create_default_context(cafile=str(root / "registry-other.crt"))
            for client in self.clients:
                with test.assertRaises(ssl.SSLCertVerificationError): Registry(client.address, unrelated, leaf, self.password).request("GET", "/v2/")
            self.initial = publish(self.clients[0], self.clients[1], phase)
            for client in self.clients: retrieve(client, phase, self.initial)
            context["stop"](self.children[0])
            retrieve(self.clients[1], phase, self.initial)
        else:
            parts = publish(self.clients[1], self.clients[1], phase)
            retrieve(self.clients[1], phase, parts)
            retrieve(self.clients[1], "initial", self.initial)
        child = self.children[1]
        test.assertEqual(self.clients[1].request("GET", REPOSITORY + "/manifests/" + phase, authorized=False)[0], 401)
        test.assertEqual(self.clients[1].request("POST", REPOSITORY + "/blobs/uploads/", authorized=False)[0], 401)
        test.assertIsNone(child.poll()); test.assertEqual(Path("/proc", str(child.pid), "stat").read_text().rsplit(") ", 1)[1].split()[19], self.identities[1])
        status = dict(line.split(":", 1) for line in Path("/proc", str(child.pid), "status").read_text().splitlines() if ":" in line)
        test.assertEqual(status["Uid"].split()[0], "65534"); test.assertEqual(int(status["CapEff"].strip(), 16), 0)
        trust = ssl.create_default_context(cafile=str(root / "ca.crt")); trust.minimum_version = ssl.TLSVersion.TLSv1_2
        leaf = hashlib.sha256(ssl.PEM_cert_to_DER_cert((root / "gateway.crt").read_text())).hexdigest()
        def cache_connection():
            value = self.cache_password
            return Connection(gateway_address, "cache-gateway.example.invalid", 6381, trust, peer_sha256=leaf, seconds=2, deadline=time.monotonic() + 5,
                max_bytes=65536, username="harbor_registry", password=value)
        if phase != "checker-stopped":
            blob = self.initial[0] if phase == "initial" else parts[0]
            with cache_connection() as client:
                test.assertEqual(client.command("SELECT", 2), "OK")
                test.assertEqual(client.command("HMGET", "blobs::" + digest(blob), "digest", "size"), [digest(blob), str(len(blob))])
                with test.assertRaises(ReplyError) as refused: client.command("HMGET", "unreviewed:key", "digest")
                test.assertEqual(refused.exception.code, "NOPERM")
                with test.assertRaises(ReplyError) as refused: client.command("CONFIG", "GET", "*")
                test.assertEqual(refused.exception.code, "NOPERM")
        else:
            with test.assertRaises((OSError, ReplyError, ValueError)):
                with cache_connection() as client: client.command("PING")
        self.phases.add(phase)

    def exercise(self, context):
        result = super().exercise(context)
        if self.phases != {"initial", "promoted", "checker-stopped"}: raise ValueError("All original registry phases are required")
        result.update(valkey_gateway_registry_authenticated_https_verified=True, valkey_gateway_registry_cross_replica_upload_verified=True,
            valkey_gateway_registry_unrelated_ca_and_changed_leaf_refused=True, valkey_gateway_registry_plaintext_refused=True,
            valkey_gateway_registry_anonymous_reads_and_writes_refused=True,
            valkey_gateway_registry_peer_serving_after_process_loss_verified=True, valkey_gateway_registry_oci_digest_roundtrip_verified=True,
            valkey_gateway_registry_original_process_after_cache_promotion_verified=True, valkey_gateway_registry_scoped_cache_populated_verified=True,
            valkey_gateway_registry_cache_outage_fallback_verified=True, valkey_gateway_registry_binaries_sha256={"registry": BINARY_SHA},
            valkey_gateway_registry_independent_shared_storage_verified=False, valkey_gateway_registry_signature_policy_verified=False,
            valkey_gateway_registry_full_harbor_ha_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
