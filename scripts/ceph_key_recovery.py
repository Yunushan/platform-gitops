#!/usr/bin/env python3
"""Capture host-OSD recovery keys into age ciphertext and one OpenBao KV v2 entry."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

from atomic_file import atomic_write_bytes, atomic_write_text
from bootstrap_keycloak_realm import sources
from bounded_file import read_bootstrap_bytes
from bounded_subprocess import run_bounded
from ceph_key_material import Budget, MAX_BYTES, Source, decode, exact, validate_material, validate_source
from deploy_postgres_ha import lock
from http_transport import stream_bounded_response
import openbao_snapshot as bao
import render_ceph_storage as storage
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
FALSE_FLAGS = ("source_fencing_verified", "monitor_database_backup_verified", "provider_restore_verified", "off_platform_verified", "ha_verified", "full_stack_verified")
JOURNAL = "ceph-key-recovery-proof.json"
ARCHIVE = "ceph-recovery.age"


def path_parts(value):
    return (isinstance(value, str) and 1 <= len(value) <= 192
            and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", part) for part in value.split("/")))


def bao_plan(plan):
    return {"schema_version": 1, "source": plan["destination"]["server"],
            "limits": {"request_seconds": plan["limits"]["command_seconds"], "transfer_seconds": 60, "max_bytes": 8 * 1024 * 1024}}


def validate(plan):
    exact(plan, "schema_version operation_id runtime source destination encryption limits")
    if (type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not storage.uid(plan["operation_id"]) or plan["runtime"] not in storage.RUNTIMES):
        raise ValueError("Select a supported original provider recovery scope")
    validate_source(plan["source"])
    exact(plan["limits"], "command_seconds overall_seconds")
    limits = plan["limits"]
    if not storage.integer(limits["command_seconds"], 5, 60) or not storage.integer(limits["overall_seconds"], 30, 7200):
        raise ValueError("Bound provider reads and the whole recovery operation")
    destination = plan["destination"]
    exact(destination, "server mount mount_uuid prefix")
    if (not path_parts(destination["mount"]) or "/" in destination["mount"] or not storage.uid(destination["mount_uuid"])
            or not path_parts(destination["prefix"])):
        raise ValueError("Select an explicit KV mount and namespace without URL or traversal syntax")
    bao.validate(bao_plan(plan))
    encryption = plan["encryption"]
    exact(encryption, "binary binary_sha256 recipient")
    if (not isinstance(encryption["binary"], str) or not Path(encryption["binary"]).is_absolute()
            or not storage.sha(encryption["binary_sha256"]) or not isinstance(encryption["recipient"], str)
            or not re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", encryption["recipient"])):
        raise ValueError("Pin a private age executable and one native X25519 recovery recipient")
    return plan


class Cipher:
    def __init__(self, plan, budget):
        self.config, self.budget = plan["encryption"], budget

    def run(self, arguments, payload):
        binary = Path(self.config["binary"])
        if binary.is_symlink():
            raise ValueError("Encryption executable must not be a symlink")
        original = read_bootstrap_bytes(binary, max_bytes=64 * 1024 * 1024, follow_symlinks=False)
        if hashlib.sha256(original).hexdigest() != self.config["binary_sha256"]:
            raise ValueError("The reviewed encryption executable changed")
        result = run_bounded([str(binary), *arguments], input=payload, timeout=self.budget.seconds(60),
                             check=False, output_max_bytes=MAX_BYTES)
        self.budget.seconds(30)
        if result.returncode or not result.stdout:
            raise ValueError("Encryption operation failed; private subprocess output was suppressed")
        if read_bootstrap_bytes(binary, max_bytes=64 * 1024 * 1024, follow_symlinks=False) != original:
            raise ValueError("The encryption executable changed during use")
        return result.stdout

    def encrypt(self, payload):
        if not 0 < len(payload) <= MAX_BYTES - 65536:
            raise ValueError("Recovery plaintext exceeds the encrypted archive bound")
        result = self.run(["--encrypt", "--recipient", self.config["recipient"]], payload)
        if not result.startswith(b"age-encryption.org/v1\n"):
            raise ValueError("The encryption tool did not produce a native age archive")
        return result

    def decrypt(self, payload, identity):
        private = private_bytes(identity, 65536)
        lines = [line for line in private.decode("ascii").splitlines() if line and not line.startswith("#")]
        if len(lines) != 1 or not re.fullmatch(r"AGE-SECRET-KEY-1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}", lines[0]):
            raise ValueError("Use one separate native X25519 identity; plugin and SSH identities are not executed")
        with tempfile.TemporaryDirectory(prefix="platform-recovery-identity-") as name:
            frozen = Path(name) / "identity"
            atomic_write_bytes(frozen, private, replace=False)
            return self.run(["--decrypt", "--identity", str(frozen)], payload)


class Vault(bao.Client):
    def __init__(self, plan, budget):
        super().__init__(bao_plan(plan))
        self.budget = budget

    def json(self, path, *, method="GET", data=None, allow_missing=False):
        self.seconds = self.budget.seconds(self.seconds)
        request_deadline = min(self.budget.deadline, time.monotonic() + self.seconds)
        connection = self.connect()
        try:
            connection.request(method, "/v1/" + path, body=storage.canonical(data).encode() if data is not None else None,
                               headers={"X-Vault-Token": self.bearer, "Accept": "application/json", "Content-Type": "application/json"})
            response = connection.getresponse()
            if allow_missing and method == "GET" and response.status == 404:
                return None
            if response.status != 200:
                raise ValueError("The explicit recovery escrow request did not succeed")
            raw = b"".join(stream_bounded_response(response, max_bytes=8 * 1024 * 1024, deadline=request_deadline))
            value = loads_strict_json(raw)
            self.budget.seconds(30)
            return value
        finally:
            connection.close()


def endpoints(plan, operation):
    destination = plan["destination"]
    key = destination["prefix"] + "/" + operation
    return destination["mount"] + "/data/" + key, destination["mount"] + "/metadata/" + key


def check_vault(client, plan, operation, *, writing=False):
    expected = plan["destination"]["server"]
    health = client.json("sys/health")
    if (health.get("cluster_id") != expected["cluster_id"] or health.get("version") != expected["version"]
            or health.get("initialized") is not True or health.get("sealed") is not False or health.get("standby") is not False):
        raise ValueError("The original active OpenBao cluster is not healthy")
    data_path, meta_path = endpoints(plan, operation)
    raft_path = "sys/storage/raft/configuration"
    mount_path = "sys/mounts/" + plan["destination"]["mount"]
    denied = [plan["destination"]["mount"] + "/data/" + plan["destination"]["prefix"] + "/unrelated-recovery",
              plan["destination"]["mount"] + "/delete/" + plan["destination"]["prefix"] + "/" + operation,
              "sys/storage/raft/snapshot-force", "auth/token/create"]
    paths = [data_path, meta_path, raft_path, mount_path, *denied]
    response = client.json("sys/capabilities-self", method="POST", data={"paths": paths})
    capabilities = response.get("data", response)
    for path, allowed, required in ((data_path, {"create", "read"}, {"create", "read"} if writing else {"read"}),
                                    (meta_path, {"read"}, {"read"}), (raft_path, {"read"}, {"read"}), (mount_path, {"read"}, {"read"})):
        observed = capabilities.get(path)
        if not isinstance(observed, list) or not required <= set(observed) <= allowed:
            raise ValueError("Use the restricted original recovery credential")
    if any(capabilities.get(path) != ["deny"] for path in denied):
        raise ValueError("Recovery credentials permit unrelated or destructive operations")
    mount = client.json(mount_path).get("data", {})
    if (mount.get("uuid") != plan["destination"]["mount_uuid"] or mount.get("type") != "kv"
            or mount.get("options", {}).get("version") != "2"):
        raise ValueError("The original escrow mount is not the reviewed KV v2 engine")
    rows = client.json(raft_path).get("data", {}).get("config", {}).get("servers", [])
    if (len(rows) != 3 or {row.get("node_id"): row.get("address") for row in rows} != expected["raft"]
            or any(row.get("voter") is not True for row in rows) or sum(row.get("leader") is True for row in rows) != 1):
        raise ValueError("The original OpenBao voting membership changed")


def private_directory(output, private_root, *, create):
    output, private_root = Path(output).absolute(), Path(private_root).absolute()
    if output.resolve() != output or private_root.resolve() != private_root or not output.relative_to(private_root).parts:
        raise ValueError("Use a canonical ignored private recovery directory")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("The reviewed private output parent must already exist")
    if create:
        output.mkdir(mode=0o700)
    info = output.lstat()
    if not stat.S_ISDIR(info.st_mode) or output.is_symlink() or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("Recovery directory ownership or private permissions are invalid")
    return output


def save(directory, record):
    value = {key: val for key, val in record.items() if key != "receipt_sha256"}
    value["receipt_sha256"] = storage.fingerprint(value)
    atomic_write_text(directory / JOURNAL, storage.canonical(value) + "\n")


def private_bytes(path, maximum):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        raise ValueError("Private recovery input is not an original owner-only regular file")
    return read_bootstrap_bytes(path, max_bytes=maximum, follow_symlinks=False)


def load(directory, plan):
    value = loads_strict_json(private_bytes(directory / JOURNAL, 131072))
    checksum = value.pop("receipt_sha256", None)
    exact(value, "schema_version operation phase plan_sha256 toolset_sha256 source_scope_sha256 captured_at repeated_source_reads_match captured escrow_verified ciphertext_sha256 ciphertext_bytes " + " ".join(FALSE_FLAGS))
    if (checksum != storage.fingerprint(value) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("plan_sha256") != storage.fingerprint(plan) or not storage.uid(value.get("operation"))
            or value["operation"] != plan["operation_id"]
            or not storage.sha(value.get("toolset_sha256")) or not storage.sha(value.get("ciphertext_sha256"))
            or not storage.integer(value.get("ciphertext_bytes"), 64, MAX_BYTES)
            or value.get("phase") not in {"encrypted", "escrow-create-requested", "completed"}
            or value.get("source_scope_sha256") != storage.fingerprint(plan["source"])
            or value.get("repeated_source_reads_match") is not True or any(value.get(key) is not False for key in FALSE_FLAGS)):
        raise ValueError("The original recovery journal is incomplete or changed")
    completed = value["phase"] == "completed"
    if value["captured"] is not completed or value["escrow_verified"] is not completed:
        raise ValueError("Recovery journal claims do not match the original phase")
    if not isinstance(value["captured_at"], str) or datetime.fromisoformat(value["captured_at"]).utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Recovery capture timestamp is not explicitly UTC")
    return value


def archive(directory, record):
    value = private_bytes(directory / ARCHIVE, MAX_BYTES)
    if (len(value) != record["ciphertext_bytes"] or hashlib.sha256(value).hexdigest() != record["ciphertext_sha256"]
            or not value.startswith(b"age-encryption.org/v1\n")):
        raise ValueError("The original encrypted recovery archive changed")
    return value


def capsule(plan, record, encrypted):
    return {"schema_version": 1, "operation": record["operation"], "plan_sha256": record["plan_sha256"],
            "source_scope_sha256": storage.fingerprint(plan["source"]), "ciphertext_sha256": record["ciphertext_sha256"],
            "encrypted_archive_base64": base64.b64encode(encrypted).decode()}


def read_escrow(client, plan, record):
    data_path, meta_path = endpoints(plan, record["operation"])
    metadata = client.json(meta_path, allow_missing=True)
    if metadata is None:
        raise ValueError("The original escrow write is unresolved; no second write is allowed")
    current = metadata.get("data", {})
    version = current.get("versions", {}).get("1", {})
    if (type(current.get("current_version")) is not int or current["current_version"] != 1
            or version.get("destroyed") is not False or version.get("deletion_time") != ""):
        raise ValueError("The original escrow version was replaced, deleted or destroyed")
    response = client.json(data_path + "?version=1").get("data", {})
    version = response.get("metadata", {})
    if type(version.get("version")) is not int or version["version"] != 1 or version.get("destroyed") is not False or version.get("deletion_time") != "":
        raise ValueError("The original readable escrow version is invalid")
    item = response.get("data", {})
    exact(item, "schema_version operation plan_sha256 source_scope_sha256 ciphertext_sha256 encrypted_archive_base64")
    if type(item["schema_version"]) is not int or item["schema_version"] != 1:
        raise ValueError("Escrow ciphertext schema is invalid")
    encrypted = decode(item["encrypted_archive_base64"], MAX_BYTES)
    if (item != capsule(plan, record, encrypted) or len(encrypted) != record["ciphertext_bytes"]
            or hashlib.sha256(encrypted).hexdigest() != record["ciphertext_sha256"]):
        raise ValueError("Escrow ciphertext or its original scope changed")
    return encrypted


def write_escrow(client, plan, directory, record, encrypted):
    check_vault(client, plan, record["operation"], writing=True)
    data_path, meta_path = endpoints(plan, record["operation"])
    if client.json(meta_path, allow_missing=True) is not None:
        raise ValueError("The original destination is occupied; never overwrite or choose another key automatically")
    record["phase"] = "escrow-create-requested"
    save(directory, record)
    # Any lost acknowledgement leaves this exact intent durable. Resume reads
    # this original version and never repeats an uncertain POST, even on 404.
    client.json(data_path, method="POST", data={"options": {"cas": 0}, "data": capsule(plan, record, encrypted)})


def complete(client, plan, directory, record):
    check_vault(client, plan, record["operation"])
    if read_escrow(client, plan, record) != archive(directory, record):
        raise ValueError("The local and escrow encrypted archives differ")
    record.update(phase="completed", captured=True, escrow_verified=True)
    save(directory, record)
    return {"captured": True, "escrow_verified": True, "decryption_verified": False, **dict.fromkeys(FALSE_FLAGS, False)}


def validate_plaintext(value, plan, record):
    exact(value, "schema_version operation plan_sha256 source material")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["operation"] != record["operation"]
            or value["plan_sha256"] != storage.fingerprint(plan) or storage.fingerprint(value["source"]) != storage.fingerprint(plan["source"])):
        raise ValueError("Decrypted archive belongs to another original scope")
    validate_material(value["material"], plan["source"])


def operate(plan, *, mode="local", output=None, private_root=None, source=None, vault=None, cipher=None, identity=None):
    validate(plan)
    if mode == "local":
        return {"local_inputs_verified": True, "captured": False, **dict.fromkeys(FALSE_FLAGS, False)}
    if mode not in {"capture", "resume", "recover", "verify-archive"} or output is None:
        raise ValueError("Select an explicit recovery operation and private directory")
    budget = Budget(plan["limits"]["overall_seconds"])
    directory = private_directory(output, private_root or ROOT / "private", create=mode == "capture")
    with lock(directory):
        cipher = cipher if cipher is not None else Cipher(plan, budget)
        if mode == "capture":
            # The operator can issue a token for this exact known path before
            # starting capture. Never silently broaden a token to a UUID prefix.
            operation = plan["operation_id"]
            vault = vault if vault is not None else Vault(plan, budget)
            check_vault(vault, plan, operation, writing=True)
            source = source if source is not None else Source(plan, budget)
            material = source.collect()
            record = {"schema_version": 1, "operation": operation, "phase": "encrypted", "plan_sha256": storage.fingerprint(plan),
                      "toolset_sha256": sources(Path(__file__).stem), "source_scope_sha256": storage.fingerprint(plan["source"]),
                      "captured_at": datetime.now(timezone.utc).isoformat(), "repeated_source_reads_match": True,
                      "captured": False, "escrow_verified": False, **dict.fromkeys(FALSE_FLAGS, False)}
            envelope = {"schema_version": 1, "operation": operation, "plan_sha256": record["plan_sha256"], "source": plan["source"], "material": material}
            validate_plaintext(envelope, plan, record)
            encrypted = cipher.encrypt(storage.canonical(envelope).encode())
            if not isinstance(encrypted, bytes) or not 64 <= len(encrypted) <= MAX_BYTES or not encrypted.startswith(b"age-encryption.org/v1\n"):
                raise ValueError("Encryption did not return a bounded native archive")
            record.update(ciphertext_sha256=hashlib.sha256(encrypted).hexdigest(), ciphertext_bytes=len(encrypted))
            atomic_write_bytes(directory / ARCHIVE, encrypted, replace=False)
            save(directory, record)
            write_escrow(vault, plan, directory, record, encrypted)
            return complete(vault, plan, directory, record)
        record = load(directory, plan)
        if mode == "verify-archive":
            if identity is None:
                raise ValueError("Provide the separate private recovery identity")
            plaintext = cipher.decrypt(archive(directory, record), identity)
            validate_plaintext(loads_strict_json(plaintext), plan, record)
            return {"archive_verified": True, "decryption_verified": True, "creator_source_matches_current": record["toolset_sha256"] == sources(Path(__file__).stem), **dict.fromkeys(FALSE_FLAGS, False)}
        vault = vault if vault is not None else Vault(plan, budget)
        if mode == "recover":
            check_vault(vault, plan, record["operation"])
            encrypted = read_escrow(vault, plan, record)
            atomic_write_bytes(directory / ARCHIVE, encrypted, replace=False)
            return {"archive_recovered": True, "escrow_verified": True, **dict.fromkeys(FALSE_FLAGS, False)}
        if record["toolset_sha256"] != sources(Path(__file__).stem):
            raise ValueError("Resume requires the original implementation; offline verification and recovery remain available")
        encrypted = archive(directory, record)
        if record["phase"] == "encrypted":
            write_escrow(vault, plan, directory, record, encrypted)
        return complete(vault, plan, directory, record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--mode", choices=("local", "capture", "resume", "recover", "verify-archive"), default="local")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--identity-file", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bootstrap_bytes(args.plan, max_bytes=262144, follow_symlinks=False))
        result = operate(plan, mode=args.mode, output=args.output_dir, identity=args.identity_file)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print("Ceph recovery operation stopped; retain the private archive and journal. Details suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
