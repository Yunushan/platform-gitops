#!/usr/bin/env python3
"""Actual Harbor-issued bearer tokens, core OCI APIs and artifact SQL across promotions."""

import base64
import hashlib
import http.client
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_registry_runtime_fixture import MEDIA, REPOSITORY, RegistryChecks, digest, publish, retrieve, upload_location
from test_harbor_service_runtime_fixture import ServiceChecks


ORIGIN = "https://192.0.2.91:8443"


def encoded(raw): return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def token_parts(value):
    if not isinstance(value, str) or len(value) > 32768 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value): raise ValueError("Bound original compact token")
    parts = value.split(".")
    header, claims = [loads_strict_json(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts[:2]]
    if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict): raise ValueError("Use the original Harbor RSA token shape")
    return parts, claims


def original_location(value):
    if not isinstance(value, str): raise ValueError("Require the original upload state")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "192.0.2.91:8443" or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("Do not redirect bearer credentials")
        value = parsed.path + ("?" + parsed.query if parsed.query else "")
    return upload_location(value)


class CoreRegistry:
    def __init__(self, service, authorization=None):
        if service.address not in {"192.0.2.91", "192.0.2.94", "192.0.2.95"} or service.port != 8443: raise ValueError("Use only reviewed core HTTPS endpoints")
        self.service, self.authorization = service, authorization

    def _request(self, method, path, body=b"", content_type="application/octet-stream", authorization=None):
        parsed = urlsplit(path)
        if method not in {"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"} or parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/") or len(path) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in path): raise ValueError("Bound original core HTTP request")
        if not isinstance(body, bytes) or len(body) > 1024 ** 2: raise ValueError("Bound original OCI payload")
        if authorization is not None and (not isinstance(authorization, str) or len(authorization) > 32780 or any(ord(c) < 32 or ord(c) > 126 for c in authorization)): raise ValueError("Bound the original authorization header")
        owner = self
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                if hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != owner.service.leaf:
                    self.close(); raise ValueError("The original core TLS identity changed")
        connection = Pinned(self.service.address, 8443, context=self.service.trust, timeout=6)
        headers = {"Content-Type": content_type, "Accept": MEDIA}
        if authorization: headers["Authorization"] = authorization
        try:
            connection.request(method, path, body=body, headers=headers)
            result = connection.getresponse(); raw = result.read(1024 ** 2 + 1)
            if len(raw) > 1024 ** 2: raise ValueError("Bound original core response")
            return result.status, {k.lower(): v for k, v in result.getheaders()}, raw
        finally: connection.close()

    def request(self, method, path, body=b"", *, content_type="application/octet-stream", authorized=True):
        if not (path == "/v2/" or path.startswith(REPOSITORY + "/")): raise ValueError("Use the original fixture repository")
        return self._request(method, path, body, content_type, self.authorization if authorized else None)

    def token(self, username, password, repository="fixture/ha", *, scoped=True):
        if type(scoped) is not bool: raise ValueError("Select an explicit login or scoped token request")
        if username not in {"admin", "fixture-reader"} or repository not in {"fixture/ha", "other-fixture/ha"}: raise ValueError("Use only reviewed fixture identities and projects")
        query = {"service": "harbor-registry"}
        if scoped: query["scope"] = "repository:" + repository + ":pull,push"
        query = urlencode(query)
        auth = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        return self._request("GET", "/service/token?" + query, authorization=auth)


def signed_negative(context, original, changes, label):
    if label not in {"audience", "expired"}: raise ValueError("Use a reviewed negative token case")
    parts, claims = token_parts(original); claims.update(changes)
    signing_input = parts[0] + "." + encoded(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    source = context["write"]("jwt-inputs/" + label + ".txt", signing_input, owner=True, mode=0o600)
    target = context["root"] / "jwt-inputs" / (label + ".sig")
    context["run"](["/usr/bin/openssl", "dgst", "-sha256", "-sign", context["root"] / "service-signing.key", "-out", target, source])
    return signing_input + "." + encoded(read_bounded_bytes(target, max_bytes=4096))


class JWTChecks:
    def service_workload(self, context, phase):
        super().service_workload(context, phase)
        test = context["test"]; core = self.service_clients["harbor-core"]
        admin_auth = "Basic " + base64.b64encode(("admin:" + self.admin_password).encode()).decode()
        if phase == "initial":
            for project in ("fixture", "other-fixture"):
                test.assertEqual(core.request("POST", "/api/v2.0/projects", {"project_name": project, "public": False}, admin_auth)[0], 201)
            self.reader_password = "Aa9!" + secrets.token_urlsafe(32); context["private_values"].append(self.reader_password)
            test.assertEqual(core.request("POST", "/api/v2.0/users", {"username": "fixture-reader", "email": "fixture-reader@example.invalid", "realname": "Synthetic read-only fixture", "password": self.reader_password}, admin_auth)[0], 201)
            test.assertEqual(core.request("POST", "/api/v2.0/projects/fixture/members", {"role_id": 3, "member_user": {"username": "fixture-reader"}}, admin_auth)[0], 201)
            self.jwt_phases = set(); self.jwt_witness = None
            if hasattr(self, "jwt_identities_ready"): self.jwt_identities_ready(context)
        anonymous = CoreRegistry(core)
        status, headers, _ = anonymous.request("GET", "/v2/")
        test.assertEqual(status, 401)
        test.assertEqual(headers.get("www-authenticate"), 'Bearer realm="' + ORIGIN + '/service/token",service="harbor-registry"')
        tokens = {}
        for username, password in (("admin", self.admin_password), ("fixture-reader", self.reader_password)):
            status, _, raw = anonymous.token(username, password); test.assertEqual(status, 200)
            response = loads_strict_json(raw); value = response.get("token"); _, claims = token_parts(value)
            context["private_values"].append(value)
            test.assertEqual(claims.get("iss"), "harbor-token-issuer"); test.assertEqual(claims.get("sub"), username)
            test.assertIn(claims.get("aud"), ("harbor-registry", ["harbor-registry"]))
            access = claims.get("access")
            if not isinstance(access, list) or len(access) != 1 or access[0].get("type") != "repository" or access[0].get("name") != "fixture/ha": raise ValueError("Original token access claims changed")
            actions = access[0].get("actions", []); test.assertIn("pull", actions)
            if username == "fixture-reader": test.assertNotIn("push", actions); test.assertNotIn("delete", actions)
            else: test.assertIn("push", actions)
            tokens[username] = value
        wrong = secrets.token_urlsafe(36); context["private_values"].append(wrong)
        test.assertEqual(anonymous.token("admin", wrong, scoped=False)[0], 401)
        status, _, raw = anonymous.token("admin", wrong); test.assertEqual(status, 200)
        denied_token = loads_strict_json(raw)["token"]; context["private_values"].append(denied_token)
        _, denied_claims = token_parts(denied_token)
        test.assertIn(denied_claims.get("sub"), (None, ""))
        test.assertEqual(denied_claims.get("access"), [{"type": "repository", "name": "fixture/ha", "actions": []}])
        writer = CoreRegistry(core, "Bearer " + tokens["admin"])
        reader = CoreRegistry(core, "Bearer " + tokens["fixture-reader"])
        # Core may return its original absolute upload URL; normalize only the
        # exact known origin before the unchanged signed-state validator.
        class UploadClient:
            def request(self, *args, **kwargs):
                status, headers, raw = writer.request(*args, **kwargs)
                if args[0] in {"POST", "PATCH"} and "location" in headers: headers["location"] = original_location(headers["location"])
                return status, headers, raw
        tag = "jwt-" + phase
        parts = publish(UploadClient(), UploadClient(), tag)
        retrieve(writer, tag, parts); retrieve(reader, tag, parts)
        test.assertEqual(reader.request("POST", REPOSITORY + "/blobs/uploads/")[0], 401)
        test.assertEqual(reader.request("PUT", REPOSITORY + "/manifests/" + tag, parts[2], content_type=MEDIA)[0], 401)
        test.assertEqual(anonymous.request("GET", REPOSITORY + "/manifests/" + tag)[0], 401)
        test.assertEqual(anonymous.request("POST", REPOSITORY + "/blobs/uploads/")[0], 401)
        status, _, raw = anonymous.token("admin", self.admin_password, repository="other-fixture/ha"); test.assertEqual(status, 200)
        other = loads_strict_json(raw)["token"]; context["private_values"].append(other)
        test.assertEqual(CoreRegistry(core, "Bearer " + other).request("GET", REPOSITORY + "/manifests/" + tag)[0], 401)
        original = tokens["admin"]; split, _ = token_parts(original)
        signature = split[2]; tampered = ".".join([*split[:2], ("A" if signature[0] != "A" else "B") + signature[1:]])
        invalid = [denied_token, tampered, signed_negative(context, original, {"aud": "unreviewed-registry"}, "audience"), signed_negative(context, original, {"exp": int(time.time()) - 3600}, "expired")]
        for value in invalid:
            context["private_values"].append(value)
            test.assertEqual(CoreRegistry(core, "Bearer " + value).request("GET", REPOSITORY + "/manifests/" + tag)[0], 401)
        test.assertEqual(CoreRegistry(core, "Basic " + base64.b64encode(("registry-fixture:" + self.password).encode()).decode()).request("GET", REPOSITORY + "/manifests/" + tag)[0], 401)
        status, raw = core.request("GET", "/api/v2.0/projects/fixture/repositories/ha/artifacts/" + tag, authorization=admin_auth)
        test.assertEqual(status, 200); metadata = loads_strict_json(raw)
        test.assertEqual(metadata.get("digest"), digest(parts[2])); test.assertEqual(metadata.get("repository_name"), "fixture/ha")
        if not any(item.get("name") == tag for item in metadata.get("tags", [])): raise ValueError("Harbor did not persist the OCI tag")
        test.assertEqual(self.sql("SELECT count(*) FROM artifact a JOIN repository r ON r.repository_id=a.repository_id WHERE r.name='fixture/ha' AND a.digest='" + digest(parts[2]) + "';", "registry"), "1")
        if self.jwt_witness:
            previous_token, previous_tag, previous_parts = self.jwt_witness
            retrieve(CoreRegistry(core, "Bearer " + previous_token), previous_tag, previous_parts)
        else: self.jwt_witness = (tokens["fixture-reader"], tag, parts)
        self.jwt_phases.add(phase)
        if hasattr(self, "jwt_workload_completed"): self.jwt_workload_completed(context, phase)

    def exercise(self, context):
        result = super().exercise(context)
        if self.jwt_phases != getattr(self, "expected_jwt_phases", {"initial", "promoted", "database-promoted"}): raise ValueError("Require real OCI/token work in every selected failure/promotion phase")
        result.update(valkey_gateway_harbor_core_jwt_oci_roundtrip_verified=True, valkey_gateway_harbor_artifact_metadata_sql_verified=True,
            valkey_gateway_harbor_jwt_guest_pull_and_write_refusal_verified=True, valkey_gateway_harbor_jwt_cross_project_refusal_verified=True,
            valkey_gateway_harbor_jwt_signature_audience_expiry_refusal_verified=True, valkey_gateway_harbor_original_jwt_after_promotions_verified=True,
            valkey_gateway_harbor_full_ha_verified=False, valkey_gateway_harbor_signed_image_admission_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
