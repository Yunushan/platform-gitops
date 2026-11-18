#!/usr/bin/env python3
"""Capture and verify a bounded private Raft snapshot; never restore or unseal."""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tarfile
import time
from urllib.parse import urlsplit

from atomic_file import atomic_write_text, _fsync_directory
from bootstrap_keycloak_realm import sources
from bounded_file import BoundedFileReader, open_bounded_file, read_bootstrap_bytes, read_bounded_bytes
from http_transport import read_bounded_response, stream_bounded_response
import render_openbao_ha as renderer
from strict_json import loads_strict_json


READ_PATHS = ("sys/storage/raft/configuration", "sys/storage/raft/snapshot")
DENY_PATHS = ("sys/storage/raft/snapshot-force", "auth/token/create", "sys/policies/acl/backup-scope-test")
FALSE_FLAGS = ("seal_authenticity_verified", "restore_verified", "off_platform_verified", "ha_verified", "full_stack_verified")


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def digest(value): return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
def integer(value, low, high): return type(value) is int and low <= value <= high
def toolset(): return sources(Path(__file__).stem)


def validate(plan):
    renderer.exact(plan, "schema_version source limits")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported backup plan")
    source = plan["source"]; renderer.exact(source, "url cluster_id version raft ca_file ca_sha256 token_env")
    if not isinstance(source["url"], str): raise ValueError("Select one explicit HTTPS API origin")
    address = urlsplit(source["url"])
    if (address.scheme != "https" or not address.hostname or address.username is not None or address.password is not None or address.query or address.fragment
            or address.path or not renderer.dns_name(address.hostname) or not integer(address.port if address.port is not None else 443, 1, 65535)
            or source["url"] != "https://" + address.hostname + (":" + str(address.port) if address.port is not None else "")):
        raise ValueError("Select one credential-free HTTPS origin without a path")
    if (not isinstance(source["cluster_id"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", source["cluster_id"])
            or source["version"] != "2.7.2" or not digest(source["ca_sha256"]) or not isinstance(source["ca_file"], str) or not Path(source["ca_file"]).is_absolute()
            or not isinstance(source["token_env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", source["token_env"])):
        raise ValueError("Pin the reviewed cluster, release, CA and credential reference")
    if not isinstance(source["raft"], dict) or len(source["raft"]) != 3: raise ValueError("Declare exactly three voting members")
    for name, address in source["raft"].items():
        if (not renderer.label(name) or not isinstance(address, str) or not address.endswith(":8201")
                or not renderer.dns_name(address[:-5])): raise ValueError("Pin each member ID and advertised Raft address")
    if len(set(source["raft"].values())) != 3: raise ValueError("Members must advertise different endpoints")
    renderer.exact(plan["limits"], "request_seconds transfer_seconds max_bytes")
    limits = plan["limits"]
    if not integer(limits["request_seconds"], 2, 60) or not integer(limits["transfer_seconds"], 5, 3600) or not integer(limits["max_bytes"], 1024, 8 * 1024 ** 3):
        raise ValueError("Bound each API request, transfer lifetime and compressed/uncompressed size")
    return plan


class Client:
    def __init__(self, plan):
        source = plan["source"]; self.origin = urlsplit(source["url"]); self.seconds = plan["limits"]["request_seconds"]
        ca = read_bootstrap_bytes(Path(source["ca_file"]), max_bytes=256 * 1024, follow_symlinks=False)
        if hashlib.sha256(ca).hexdigest() != source["ca_sha256"]: raise ValueError("The reviewed CA changed")
        self.context = ssl.create_default_context(cadata=ca.decode("ascii"))
        self.bearer = os.environ.get(source["token_env"], "")
        if not 8 <= len(self.bearer) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self.bearer): raise ValueError("Supply only the private scoped credential through its declared environment variable")
    def connect(self):
        return http.client.HTTPSConnection(self.origin.hostname, self.origin.port or 443, context=self.context, timeout=self.seconds)
    def json(self, path, *, method="GET", data=None):
        connection = self.connect()
        try:
            body = canonical(data).encode() if data is not None else None
            connection.request(method, "/v1/" + path, body=body, headers={"X-Vault-Token": self.bearer, "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200: raise ValueError("The explicit backup API request did not succeed")
            return loads_strict_json(read_bounded_response(response, max_bytes=256 * 1024))
        finally: connection.close()
    def snapshot(self, output, limits):
        connection = self.connect(); deadline = time.monotonic() + limits["transfer_seconds"]; count = 0; checksum = hashlib.sha256()
        try:
            connection.request("GET", "/v1/sys/storage/raft/snapshot", headers={"X-Vault-Token": self.bearer, "Accept": "application/octet-stream"})
            response = connection.getresponse()
            if response.status != 200: raise ValueError("Snapshot download failed; redirects are never followed")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or not 0 < int(length) <= limits["max_bytes"]): raise ValueError("Snapshot length exceeds the transfer bound")
            for block in stream_bounded_response(response, max_bytes=limits["max_bytes"], deadline=deadline):
                count += len(block)
                output.write(block); checksum.update(block)
            if not count or length is not None and count != int(length): raise ValueError("Snapshot response was incomplete")
            return count, checksum.hexdigest()
        finally: connection.close()


def verify_source(client, plan):
    source = plan["source"]; health = client.json("sys/health")
    if (health.get("cluster_id") != source["cluster_id"] or health.get("version") != source["version"] or health.get("initialized") is not True
            or health.get("sealed") is not False or health.get("standby") is not False): raise ValueError("The reviewed active unsealed cluster changed")
    rows = client.json(READ_PATHS[0]).get("data", {}).get("config", {}).get("servers", [])
    if (len(rows) != 3 or {r.get("node_id"): r.get("address") for r in rows} != source["raft"] or any(r.get("voter") is not True for r in rows)
            or sum(r.get("leader") is True for r in rows) != 1): raise ValueError("The original voting membership changed")
    caps = client.json("sys/capabilities-self", method="POST", data={"paths": [*READ_PATHS, *DENY_PATHS]})
    if any(caps.get(path) != ["read"] for path in READ_PATHS) or any(caps.get(path) != ["deny"] for path in DENY_PATHS):
        raise ValueError("Use a snapshot-read credential without restore, token-creation or policy-write access")
    return {"raft_sha256": fingerprint(source["raft"]), "capabilities_sha256": fingerprint({p: caps[p] for p in (*READ_PATHS, *DENY_PATHS)})}


def file_identity(path, maximum):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum
            or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise ValueError("Use an original owner-private bounded regular snapshot file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def verify_archive(path, plan):
    maximum = plan["limits"]["max_bytes"]; original = file_identity(path, maximum)
    deadline = time.monotonic() + plan["limits"]["transfer_seconds"]
    # Drain gzip independently: a tar reader can stop at its end marker before
    # encountering a truncated gzip trailer or CRC error.
    total = 0
    expanded_limit = maximum + 1024 * 1024
    with open_bounded_file(path, max_bytes=maximum) as source, gzip.GzipFile(fileobj=source, mode="rb") as stream:
        while block := stream.read(1024 * 1024):
            total += len(block)
            if time.monotonic() >= deadline: raise TimeoutError("Snapshot verification deadline expired")
            if total > expanded_limit: raise ValueError("Decompressed snapshot exceeds its bound")
    entries = {}; sizes = {}; small = {}
    allowed = {"meta.json", "state.bin", "SHA256SUMS", "SHA256SUMS.sealed"}
    with (
        open_bounded_file(path, max_bytes=maximum) as source,
        gzip.GzipFile(fileobj=source, mode="rb") as expanded,
        tarfile.open(fileobj=BoundedFileReader(expanded, path, expanded_limit), mode="r|") as archive,
    ):
        for member in archive:
            if member.name not in allowed or member.name in entries or not member.isfile() or not 0 < member.size <= (maximum if member.name == "state.bin" else 64 * 1024):
                raise ValueError("Unexpected snapshot archive member")
            checksum = hashlib.sha256(); captured = bytearray(); count = 0
            with archive.extractfile(member) as stream:
                while block := stream.read(1024 * 1024):
                    if time.monotonic() >= deadline: raise TimeoutError("Snapshot verification deadline expired")
                    count += len(block); checksum.update(block)
                    if count > member.size: raise ValueError("Snapshot member grew beyond its declaration")
                    if member.name != "state.bin": captured.extend(block)
            if count != member.size: raise ValueError("Snapshot archive member is incomplete")
            entries[member.name] = checksum.hexdigest(); sizes[member.name] = count
            if member.name != "state.bin": small[member.name] = bytes(captured)
    if set(entries) != allowed: raise ValueError("A sealed snapshot archive is incomplete")
    sums = {}
    for line in small["SHA256SUMS"].decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (meta[.]json|state[.]bin)", line)
        if not match or match[2] in sums: raise ValueError("Invalid or duplicated snapshot checksum entry")
        sums[match[2]] = match[1]
    if sums != {k: entries[k] for k in ("meta.json", "state.bin")}: raise ValueError("Snapshot internal checksums do not match")
    meta = loads_strict_json(small["meta.json"])
    if (not integer(meta.get("Version"), 1, 1) or not integer(meta.get("Index"), 1, 2 ** 64 - 1) or not integer(meta.get("Term"), 1, 2 ** 64 - 1)
            or type(meta.get("Size")) is not int or meta["Size"] != sizes["state.bin"]): raise ValueError("Snapshot Raft metadata is invalid")
    servers = meta.get("Configuration", {}).get("Servers", [])
    if (len(servers) != 3 or {r.get("ID"): r.get("Address") for r in servers} != plan["source"]["raft"]
            or any(type(r.get("Suffrage")) is not int or r["Suffrage"] != 0 for r in servers)): raise ValueError("Snapshot membership differs from the reviewed voters")
    snapshot_hash = hashlib.sha256()
    with open_bounded_file(path, max_bytes=maximum) as stream:
        while block := stream.read(1024 * 1024):
            if time.monotonic() >= deadline: raise TimeoutError("Snapshot verification deadline expired")
            snapshot_hash.update(block)
    checksum = snapshot_hash.hexdigest()
    if file_identity(path, maximum) != original: raise ValueError("Snapshot file changed during verification")
    return {"snapshot_sha256": checksum, "snapshot_bytes": original[2], "raft_index": meta["Index"], "raft_term": meta["Term"], "members_sha256": fingerprint(plan["source"]["raft"]),
        "member_sha256": entries, "internal_checksums_verified": True, "gzip_crc_verified": True, "seal_authenticity_verified": False}


def operate(plan, *, mode="local", output=None, client=None):
    validate(plan)
    if mode == "local": return {"local_inputs_verified": True, "captured": False, "read_only": True, **{k: False for k in FALSE_FLAGS}}
    if mode == "verify":
        if output is None: raise ValueError("Select the original private backup directory")
        proof = loads_strict_json(read_bootstrap_bytes(output / "openbao-snapshot-proof.json", max_bytes=128 * 1024, follow_symlinks=False))
        checksum = proof.pop("receipt_sha256", None)
        if (not digest(checksum) or checksum != fingerprint(proof) or proof.get("phase") != "completed" or proof.get("captured") is not True
                or proof.get("plan_sha256") != fingerprint(plan) or not digest(proof.get("toolset_sha256")) or any(proof.get(k) is not False for k in FALSE_FLAGS)):
            raise ValueError("The original backup receipt is invalid or incomplete")
        observed = verify_archive(output / "snapshot.tar.gz", plan)
        if any(proof.get(k) != v for k, v in observed.items()): raise ValueError("The original backup artifact changed")
        return {**observed, "verified": True, "creator_source_matches_current": proof["toolset_sha256"] == toolset(), "read_only": True, **{k: False for k in FALSE_FLAGS}}
    if mode != "capture" or output is None: raise ValueError("Select capture or offline verification")
    directory = output.absolute()
    if directory.resolve() != directory or not directory.parent.is_dir(): raise ValueError("Use a canonical new private backup directory with an existing parent")
    directory.mkdir(mode=0o700, exist_ok=False)
    if os.name == "posix": directory.chmod(0o700)
    source = toolset(); proof = {"schema_version": 1, "plan_sha256": fingerprint(plan), "toolset_sha256": source, "phase": "prepared", "captured": False, **{k: False for k in FALSE_FLAGS}}
    receipt = directory / "openbao-snapshot-proof.json"
    def record(phase, **values):
        proof.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
        atomic_write_text(receipt, canonical({**proof, "receipt_sha256": fingerprint(proof)}))
    record("prepared")
    try:
        network = client or Client(plan); before = verify_source(network, plan); record("source-checked", source_identity_verified=True, token_scope_verified=True)
        partial = directory / "snapshot.part"
        with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            count, checksum = network.snapshot(stream, plan["limits"]); stream.flush(); os.fsync(stream.fileno())
        record("downloaded")
        observed = verify_archive(partial, plan)
        if observed["snapshot_bytes"] != count or observed["snapshot_sha256"] != checksum or verify_source(network, plan) != before or toolset() != source:
            raise ValueError("Source, artifact or executing code changed during capture")
        final = directory / "snapshot.tar.gz"
        if os.name == "nt": os.rename(partial, final)  # Windows rename refuses an existing destination.
        else:
            os.link(partial, final); partial.unlink()  # POSIX publication must not replace a prior file.
        _fsync_directory(directory)
        record("completed", captured=True, **observed)
        return {**proof, "read_only": True}
    except BaseException:
        record("failed", captured=False, error_code="openbao-snapshot-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path)
    modes = parser.add_mutually_exclusive_group(); modes.add_argument("--capture", action="store_true"); modes.add_argument("--verify", action="store_true"); args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        print(canonical(operate(plan, mode="capture" if args.capture else "verify" if args.verify else "local", output=args.output_dir))); return 0
    except Exception:
        print("OpenBao snapshot operation stopped; preserve the private directory. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
