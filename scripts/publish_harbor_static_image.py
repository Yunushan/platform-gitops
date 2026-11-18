#!/usr/bin/env python3
"""Publish reviewed static web output as a digest-addressed OCI image using a Harbor robot.

This is a deliberately narrow, daemonless publisher, not a Dockerfile interpreter.
Run it as a trusted CI step with the robot handoff mounted only into that step.
"""

import argparse
import base64
import gzip
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import stat
import sys
import tarfile
import time
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

from bounded_file import read_bounded_bytes
from http_transport import read_bounded_response
from strict_json import loads_strict_json


MEDIA = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
LIMIT = 16 * 1024 ** 2


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
def digest(raw): return "sha256:" + hashlib.sha256(raw).hexdigest()


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()): raise ValueError("Unexpected publication fields")


def hex64(value): return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def origin(value):
    if not isinstance(value, str) or len(value) > 512: raise ValueError("Bound the HTTPS registry origin")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path or parsed.netloc != parsed.netloc.lower()
            or any(ord(c) <= 32 or ord(c) > 126 for c in value) or "%" in value or "\\" in value
            or parsed.port is not None and not 1 <= parsed.port <= 65535): raise ValueError("Use a canonical credential-free HTTPS origin")
    return parsed


def validate(plan):
    exact(plan, "schema_version origin ca_file ca_sha256 leaf_sha256 credential_file credential_sha256 project_id robot_id repository base_digest revision files")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported publication plan")
    origin(plan["origin"])
    for key in ("ca_sha256", "leaf_sha256", "credential_sha256"):
        if not hex64(plan[key]): raise ValueError("Pin the original credential and TLS files")
    for key in ("project_id", "robot_id"):
        if type(plan[key]) is not int or plan[key] <= 0: raise ValueError("Pin the original robot and project IDs")
    for key in ("ca_file", "credential_file"):
        if not isinstance(plan[key], str) or not (Path(plan[key]).is_absolute() or PurePosixPath(plan[key]).is_absolute()): raise ValueError("Use absolute private input paths")
    if not isinstance(plan["repository"], str) or not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._/-][a-z0-9]+)*", plan["repository"]) or len(plan["repository"]) > 200: raise ValueError("Select one project repository")
    if not isinstance(plan["base_digest"], str) or not DIGEST.fullmatch(plan["base_digest"]): raise ValueError("Pin the base manifest by digest")
    if not isinstance(plan["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["revision"]): raise ValueError("Pin the selected Git revision")
    names = plan["files"]
    if not isinstance(names, list) or not 1 <= len(names) <= 128 or any(not isinstance(n, str) for n in names): raise ValueError("Select bounded static files")
    if names != sorted(set(names)) or "index.html" not in names: raise ValueError("Use unique sorted files including index.html")
    for name in names:
        if len(name) > 200 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in name.split("/")): raise ValueError("Use explicit relative public file names")
    return plan


def private_inputs(plan):
    ca = read_bounded_bytes(Path(plan["ca_file"]), max_bytes=65536)
    raw = read_bounded_bytes(Path(plan["credential_file"]), max_bytes=16384)
    if hashlib.sha256(ca).hexdigest() != plan["ca_sha256"] or hashlib.sha256(raw).hexdigest() != plan["credential_sha256"]: raise ValueError("Original TLS or credential input changed")
    credential = loads_strict_json(raw)
    exact(credential, "schema_version operation_id plan_sha256 origin project_id robot_id username secret expires_at")
    username = credential["username"]; project = plan["repository"].split("/", 1)[0]
    if (type(credential["schema_version"]) is not int or credential["schema_version"] != 1
            or not isinstance(username, str) or not re.fullmatch(re.escape("robot$" + project + "+") + r"[a-z0-9]+(?:[-_][a-z0-9]+)*", username)
            or any(credential[k] != plan[k] or type(credential[k]) is not int for k in ("project_id", "robot_id"))
            or credential["origin"] != plan["origin"] or not isinstance(credential["operation_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", credential["operation_id"])
            or not hex64(credential["plan_sha256"]) or type(credential["expires_at"]) is not int or credential["expires_at"] <= time.time() + 180
            or not isinstance(credential["secret"], str) or not 20 <= len(credential["secret"]) <= 512
            or any(ord(c) <= 32 or ord(c) > 126 for c in credential["secret"])): raise ValueError("Require the original unexpired project robot handoff")
    return ca, credential


def output_layer(directory, names, secret):
    """Snapshot only selected regular files, with no symlink or parent traversal."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"): raise ValueError("Static publication requires Linux no-follow file descriptors")
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    records = []; contents = []; total = 0
    try:
        for name in names:
            parent = os.dup(root)
            try:
                parts = name.split("/")
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent); parent = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4 * 1024 ** 2: raise ValueError("Select bounded unlinked regular public output")
                    with os.fdopen(fd, "rb", closefd=False) as stream: raw = stream.read(4 * 1024 ** 2 + 1)
                    after = os.fstat(fd)
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) != before.st_size: raise ValueError("Build output changed during snapshot")
                finally: os.close(fd)
            finally: os.close(parent)
            total += len(raw)
            if total > 8 * 1024 ** 2 or secret.encode() in raw: raise ValueError("Output exceeds its bound or contains the robot secret")
            records.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
            contents.append((name, raw))
    finally: os.close(root)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        directories = {"www"}
        for name in names:
            parts = name.split("/")[:-1]
            directories.update("www/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1))
        for name in sorted(directories):
            item = tarfile.TarInfo(name); item.type = tarfile.DIRTYPE; item.mode = 0o555; item.uid = item.gid = 1000
            archive.addfile(item)
        for name, raw in contents:
            item = tarfile.TarInfo("www/" + name); item.size = len(raw); item.mode = 0o444; item.uid = item.gid = 1000
            archive.addfile(item, io.BytesIO(raw))
    return output.getvalue(), hashlib.sha256(canonical(records)).hexdigest()


class Registry:
    def __init__(self, plan, ca, credential):
        self.plan = plan; self.remote = origin(plan["origin"]); self.credential = credential
        self.trust = ssl.create_default_context(cadata=ca.decode("ascii")); self.trust.minimum_version = ssl.TLSVersion.TLSv1_2
        self.prefix = "/v2/" + plan["repository"]; self.authorization = None; self.deadline = time.monotonic() + 180

    def request(self, method, path, body=b"", content_type="application/octet-stream", *, basic=False):
        parsed = urlsplit(path)
        token_path = "/service/token?" + urlencode({"service": "harbor-registry", "scope": "repository:" + self.plan["repository"] + ":pull,push"})
        if (time.monotonic() >= self.deadline or method not in {"GET", "HEAD", "POST", "PUT"} or not isinstance(body, bytes) or len(body) > LIMIT
                or not isinstance(path, str) or len(path) > 8192 or parsed.scheme or parsed.netloc or parsed.fragment
                or any(ord(c) < 33 or ord(c) > 126 for c in path)
                or basic and (method != "GET" or path != token_path)
                or not basic and not path.startswith(self.prefix + "/")): raise ValueError("Refuse out-of-scope registry operation")
        owner = self
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                if hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != owner.plan["leaf_sha256"]:
                    self.close(); raise ValueError("Registry leaf certificate changed")
        connection = Pinned(self.remote.hostname, self.remote.port or 443, context=self.trust, timeout=min(10, self.deadline - time.monotonic()))
        headers = {"Content-Type": content_type, "Accept": MEDIA + ", application/vnd.docker.distribution.manifest.v2+json", "Connection": "close"}
        if basic: headers["Authorization"] = "Basic " + base64.b64encode((self.credential["username"] + ":" + self.credential["secret"]).encode()).decode()
        elif self.authorization: headers["Authorization"] = self.authorization
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            if response.status not in {200, 201, 202, 404}: raise ValueError("Registry refused publication: HTTP " + str(response.status))
            raw = read_bounded_response(response, max_bytes=LIMIT)
            return response.status, {k.lower(): v for k, v in response.getheaders()}, raw
        finally: connection.close()

    def authenticate(self):
        path = "/service/token?" + urlencode({"service": "harbor-registry", "scope": "repository:" + self.plan["repository"] + ":pull,push"})
        status, _, raw = self.request("GET", path, basic=True)
        result = loads_strict_json(raw); token = result.get("token") if isinstance(result, dict) else None
        if status != 200 or not isinstance(token, str) or len(token) > 32768 or not re.fullmatch(r"[A-Za-z0-9_-]+[.][A-Za-z0-9_-]+[.][A-Za-z0-9_-]+", token): raise ValueError("Invalid robot bearer response")
        header, claims = [loads_strict_json(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))) for p in token.split(".")[:2]]
        expected = [{"type": "repository", "name": self.plan["repository"], "actions": ["pull", "push"]}]
        access = claims.get("access") if isinstance(claims, dict) else None
        if isinstance(access, list):
            access = [{**r, "actions": sorted(r.get("actions", []))} if isinstance(r, dict) else r for r in access]
        if (not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict)
                or claims.get("sub") != self.credential["username"] or claims.get("aud") not in ("harbor-registry", ["harbor-registry"])
                or type(claims.get("exp")) is not int or claims["exp"] <= time.time() or access != expected): raise ValueError("Robot repository authorization changed")
        self.authorization = "Bearer " + token

    def fetch(self, kind, identifier):
        if kind not in {"manifests", "blobs"} or not isinstance(identifier, str) or not DIGEST.fullmatch(identifier): raise ValueError("Fetch only pinned content")
        status, _, raw = self.request("GET", self.prefix + "/" + kind + "/" + identifier)
        if status != 200 or digest(raw) != identifier: raise ValueError("Registry content digest changed")
        return raw

    def location(self, value):
        if not isinstance(value, str) or len(value) > 8192: raise ValueError("Bound the original upload state")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme + "://" + parsed.netloc != self.plan["origin"]: raise ValueError("Refuse off-origin upload credentials")
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (parsed.fragment or not re.fullmatch(re.escape(self.prefix) + r"/blobs/uploads/[0-9a-f-]{36}", parsed.path)
                or set(query) != {"_state"} or len(query["_state"]) != 1 or not 1 <= len(query["_state"][0]) <= 4096): raise ValueError("Original repository upload state changed")
        return parsed.path + "?" + urlencode({"_state": query["_state"][0]})

    def upload(self, raw):
        identifier = digest(raw)
        status, _, _ = self.request("HEAD", self.prefix + "/blobs/" + identifier)
        if status == 200:
            if self.fetch("blobs", identifier) != raw: raise ValueError("Existing blob changed")
            return identifier
        if status != 404: raise ValueError("Blob existence could not be established")
        status, headers, _ = self.request("POST", self.prefix + "/blobs/uploads/")
        if status != 202: raise ValueError("Upload did not start")
        location = self.location(headers.get("location"))
        status, headers, _ = self.request("PUT", location + "&" + urlencode({"digest": identifier}), raw)
        if status != 201 or headers.get("docker-content-digest") != identifier or self.fetch("blobs", identifier) != raw: raise ValueError("Original blob upload was not verified")
        return identifier


def descriptor(raw, media): return {"mediaType": media, "digest": digest(raw), "size": len(raw)}


def assemble(plan, base_raw, config_raw, base_layers, layer, tree):
    base = loads_strict_json(base_raw); config = loads_strict_json(config_raw)
    if (not isinstance(base, dict) or base.get("schemaVersion") != 2 or base.get("mediaType") not in {MEDIA, "application/vnd.docker.distribution.manifest.v2+json"}
            or set(base) - {"schemaVersion", "mediaType", "config", "layers", "annotations"}
            or not isinstance(config, dict) or config.get("os") != "linux" or config.get("architecture") != "amd64"
            or len(base_layers) > 8 or len(base_layers) != len(base.get("layers", []))): raise ValueError("Require a pinned Linux amd64 base image")
    cfg = base.get("config", {})
    if not isinstance(cfg, dict) or cfg.get("digest") != digest(config_raw) or cfg.get("size") != len(config_raw) or set(cfg) != {"mediaType", "digest", "size"} or cfg["mediaType"] not in {CONFIG, "application/vnd.docker.container.image.v1+json"}: raise ValueError("Base configuration changed")
    diff_ids = []
    for item, raw in zip(base["layers"], base_layers):
        if (not isinstance(item, dict) or set(item) != {"mediaType", "digest", "size"} or item.get("digest") != digest(raw) or item.get("size") != len(raw)
                or item.get("mediaType") not in {LAYER, LAYER + "+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip"}): raise ValueError("Base layer descriptor changed")
        if item["mediaType"] != LAYER:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream: expanded = stream.read(64 * 1024 ** 2 + 1)
            if len(expanded) > 64 * 1024 ** 2: raise ValueError("Base layer expansion exceeds its bound")
        else: expanded = raw
        diff_ids.append(digest(expanded))
    if config.get("rootfs") != {"type": "layers", "diff_ids": diff_ids}: raise ValueError("Base root filesystem changed")
    new_config = {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": diff_ids + [digest(layer)]},
        "config": {"User": "1000:1000", "Entrypoint": ["/bin/httpd"], "Cmd": ["-f", "-p", "8080", "-h", "/www"],
            "WorkingDir": "/www", "ExposedPorts": {"8080/tcp": {}}, "Labels": {"org.opencontainers.image.revision": plan["revision"], "platform.gitops/output-sha256": tree}}}
    config_raw = canonical(new_config)
    manifest = {"schemaVersion": 2, "mediaType": MEDIA, "config": descriptor(config_raw, CONFIG), "layers": base["layers"] + [descriptor(layer, LAYER)]}
    return config_raw, canonical(manifest)


def publish(plan, directory):
    validate(plan); ca, credential = private_inputs(plan)
    directory = Path(directory).absolute()
    for key in ("ca_file", "credential_file"):
        if Path(plan[key]).resolve().is_relative_to(directory.resolve()): raise ValueError("Keep credentials and trust outside build output")
    layer, tree = output_layer(directory, plan["files"], credential["secret"])
    registry = Registry(plan, ca, credential); registry.authenticate()
    base_raw = registry.fetch("manifests", plan["base_digest"]); base = loads_strict_json(base_raw)
    if not isinstance(base, dict) or not isinstance(base.get("config"), dict) or not isinstance(base.get("layers"), list) or len(base["layers"]) > 8: raise ValueError("Invalid base manifest")
    config_raw = registry.fetch("blobs", base["config"].get("digest")); layers = []; total = 0
    for item in base["layers"]:
        if not isinstance(item, dict): raise ValueError("Invalid base layer")
        raw = registry.fetch("blobs", item.get("digest")); total += len(raw)
        if total > 64 * 1024 ** 2: raise ValueError("Base image exceeds its bound")
        layers.append(raw)
    config_raw, manifest_raw = assemble(plan, base_raw, config_raw, layers, layer, tree)
    registry.upload(layer); registry.upload(config_raw)
    identifier = digest(manifest_raw)
    status, headers, _ = registry.request("PUT", registry.prefix + "/manifests/" + identifier, manifest_raw, MEDIA)
    if status != 201 or headers.get("docker-content-digest") != identifier or registry.fetch("manifests", identifier) != manifest_raw: raise ValueError("Manifest publication was not verified")
    return {"schema_version": 1, "image": plan["origin"].removeprefix("https://") + "/" + plan["repository"] + "@" + identifier,
        "manifest_digest": identifier, "config_digest": digest(config_raw), "output_layer_digest": digest(layer), "output_tree_sha256": tree,
        "base_digest": plan["base_digest"], "source_revision": plan["revision"], "artifact_publication_verified": True,
        "publisher_signature_verified": False, "deployment_verified": False, "ha_verified": False, "full_stack_verified": False}


def verify_static_receipt(registry, revision, receipt):
    """Validate the original static publisher receipt against its reviewed scope."""
    exact(receipt, "schema_version image manifest_digest config_digest output_layer_digest output_tree_sha256 base_digest source_revision artifact_publication_verified publisher_signature_verified deployment_verified ha_verified full_stack_verified")
    if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or any(not isinstance(receipt[key], str) or not DIGEST.fullmatch(receipt[key])
                   for key in ("manifest_digest", "config_digest", "output_layer_digest"))
            or not hex64(receipt["output_tree_sha256"])):
        raise ValueError("invalid-static-publication-receipt")
    image = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + receipt["manifest_digest"]
    if (receipt["image"] != image or receipt["source_revision"] != revision or receipt["base_digest"] != registry["base_digest"]
            or receipt["artifact_publication_verified"] is not True
            or any(receipt[key] is not False for key in ("publisher_signature_verified", "deployment_verified", "ha_verified", "full_stack_verified"))):
        raise ValueError("static-publication-receipt-scope-mismatch")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=65536)); result = publish(plan, args.output)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception as error:
        names = {"publish_harbor_static_image.py", "bounded_file.py", "strict_json.py"}
        locations = [Path(frame.filename).name + ":" + str(frame.lineno) for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).name in names][-6:]
        kind = type(error).__name__ if type(error).__name__ in {"ValueError", "OSError", "TimeoutError", "SSLError", "HTTPException", "FileInputTooLarge", "FileInputNotRegular"} else "PublicationFailure"
        print(json.dumps({"publisher_failure": {"exception_type": kind, "code_locations": locations}}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
