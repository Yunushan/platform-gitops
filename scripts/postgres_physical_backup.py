#!/usr/bin/env python3
"""Explicit TLS physical backup capture and offline integrity checks; never restores a server."""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import sys
import tarfile
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import open_bounded_file, read_bounded_bytes
from bounded_subprocess import run_bounded
import postgres_ha_policy as policy
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("psql", "pg_basebackup", "pg_verifybackup", "pg_waldump")
SHA = re.compile(r"[0-9a-f]{64}")
UNVERIFIED = {key: False for key in ("off_platform_verified", "continuous_wal_archive_verified", "restore_verified", "ha_verified", "full_stack_verified")}


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def integer(value, lower, upper): return type(value) is int and lower <= value <= upper
def digest(value): return isinstance(value, str) and bool(SHA.fullmatch(value))


def toolset():
    root = Path(__file__).resolve().parent; found, pending = {}, [Path(__file__).stem]
    while pending:
        name = pending.pop()
        if name + ".py" in found: continue
        raw = read_bounded_bytes(root / (name + ".py"), max_bytes=2 * 1024 ** 2)
        found[name + ".py"] = hashlib.sha256(raw).hexdigest()
        for item in ast.walk(ast.parse(raw)):
            modules = [v.name for v in item.names] if isinstance(item, ast.Import) else [item.module] if isinstance(item, ast.ImportFrom) and item.module else []
            for module in modules:
                dependency = module.split(".")[0]
                if (root / (dependency + ".py")).is_file() and dependency + ".py" not in found: pending.append(dependency)
    return fingerprint(found)


def system_identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]{0,19}", value)) and int(value) < 2 ** 64


def validate(plan):
    fields = {"schema_version", "runtime", "postgres_version", "system_identifier", "source", "runner", "client", "tls", "limits"}
    if (not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or plan["runtime"] not in policy.RUNTIMES or not isinstance(plan["postgres_version"], str) or not re.fullmatch(r"(?:17|18)[.][1-9][0-9]*", plan["postgres_version"])
            or plan["system_identifier"] is not None and not system_identifier(plan["system_identifier"])):
        raise ValueError("Use a complete reviewed PostgreSQL 17/18 backup plan")
    source, runner, client, tls, limits = (plan[key] for key in ("source", "runner", "client", "tls", "limits"))
    if (not isinstance(source, dict) or set(source) != {"host", "hostaddr", "port", "user"} or not policy.hostname(source["host"])
            or not integer(source["port"], 1024, 65535) or not isinstance(source["user"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", source["user"])
            or source["user"] in {"postgres", "platform_pg_admin"}): raise ValueError("Declare one verified-TLS endpoint and an approved replication identity")
    address = ipaddress.ip_address(source["hostaddr"])
    if str(address) != source["hostaddr"] or address.is_unspecified or address.is_multicast or address.is_link_local: raise ValueError("Declare an exact source transport address")
    if (not isinstance(runner, dict) or set(runner) != {"uid", "gid", "machine_id_sha256", "declared_database_machine_ids_sha256"}
            or any(not integer(runner[k], 1, 65535) for k in ("uid", "gid")) or not digest(runner["machine_id_sha256"])
            or not isinstance(runner["declared_database_machine_ids_sha256"], list) or len(runner["declared_database_machine_ids_sha256"]) != 3
            or any(not digest(v) for v in runner["declared_database_machine_ids_sha256"]) or len(set(runner["declared_database_machine_ids_sha256"])) != 3
            or runner["machine_id_sha256"] in runner["declared_database_machine_ids_sha256"]): raise ValueError("Bind a separate non-root backup runner and reviewed database host declarations")
    if (not isinstance(client, dict) or set(client) != {"bin_dir", "artifacts_sha256", "library_dir", "libraries_sha256"} or not policy.absolute(client["bin_dir"])
            or not isinstance(client["artifacts_sha256"], dict) or set(client["artifacts_sha256"]) != set(TOOLS) or any(not digest(v) for v in client["artifacts_sha256"].values())
            or client["library_dir"] is not None and not policy.absolute(client["library_dir"]) or not isinstance(client["libraries_sha256"], dict)
            or bool(client["libraries_sha256"]) != (client["library_dir"] is not None) or len(client["libraries_sha256"]) > 32
            or any(not re.fullmatch(r"lib[A-Za-z0-9_.+-]+[.]so(?:[.][0-9]+)*", name) or not digest(v) for name, v in client["libraries_sha256"].items())):
        raise ValueError("Pin the complete client command set and any private loader directory")
    if (not isinstance(tls, dict) or set(tls) != {"inputs_dir", "ca_sha256", "client_certificate_sha256"} or not policy.absolute(tls["inputs_dir"])
            or not digest(tls["ca_sha256"]) or not digest(tls["client_certificate_sha256"])): raise ValueError("Use private certificate-authenticated TLS inputs and public certificate pins")
    if (not isinstance(limits, dict) or set(limits) != {"timeout_seconds", "max_backup_bytes", "max_files", "max_rate_kib"}
            or not integer(limits["timeout_seconds"], 30, 86400) or not integer(limits["max_backup_bytes"], 64 * 1024 ** 2, 1024 ** 5)
            or not integer(limits["max_files"], 32, 1000000) or not integer(limits["max_rate_kib"], 32, 1048576)):
        raise ValueError("Declare bounded execution, post-capture size, inventory and transfer limits")
    return plan


def directory(path, *, private=False):
    if not path.is_absolute() or path != path.resolve(): raise ValueError("Use canonical absolute paths")
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022: raise ValueError("Untrusted directory or symlink ancestor")
    if private and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077): raise ValueError("Require an owned private backup directory")


def regular(path, *, private=False):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & (0o077 if private else 0o022)): raise ValueError("Untrusted regular file or private input permissions")
    return info


def machine_identity():
    value = read_bounded_bytes(Path("/etc/machine-id"), max_bytes=128).strip()
    if not re.fullmatch(b"[0-9a-f]{32}", value): raise ValueError("The backup host machine identity is unavailable")
    return hashlib.sha256(value).hexdigest()


def require_runner(plan):
    runner = plan["runner"]
    if sys.platform != "linux" or os.geteuid() != runner["uid"] or os.getegid() != runner["gid"]:
        raise ValueError("Run as the reviewed non-root Linux backup identity")


def environment(plan, *, source=False):
    env = {"PATH": plan["client"]["bin_dir"] + ":/usr/bin:/bin", "LC_ALL": "C", "PGPASSFILE": "/dev/null", "PGCONNECT_TIMEOUT": "5"}
    if plan["client"]["library_dir"] is not None: env["LD_LIBRARY_PATH"] = plan["client"]["library_dir"]
    if source:
        inputs = Path(plan["tls"]["inputs_dir"])
        env.update(PGSSLMODE="verify-full", PGSSLMINPROTOCOLVERSION="TLSv1.2", PGSSLROOTCERT=str(inputs / "ca.crt"), PGSSLCERT=str(inputs / "client.crt"), PGSSLKEY=str(inputs / "client.key"))
    return env


def command(plan, tool, args, *, source=False, seconds=20):
    result = run_bounded([str(Path(plan["client"]["bin_dir"]) / tool), *args], env=environment(plan, source=source), input="", text=True,
                         timeout=seconds, check=False, output_max_bytes=256 * 1024)
    if result.returncode: raise ValueError("PostgreSQL backup client command failed; private diagnostics suppressed")
    return result.stdout


def client_inputs(plan):
    binaries = Path(plan["client"]["bin_dir"]); directory(binaries)
    for name, expected in plan["client"]["artifacts_sha256"].items():
        path = binaries / name; info = regular(path)
        if not info.st_mode & 0o111 or hashlib.sha256(read_bounded_bytes(path, max_bytes=32 * 1024 ** 2)).hexdigest() != expected: raise ValueError("A client executable differs from its reviewed pin")
    if plan["client"]["library_dir"] is not None:
        libraries = Path(plan["client"]["library_dir"]); directory(libraries)
        if {p.name for p in libraries.iterdir()} != set(plan["client"]["libraries_sha256"]): raise ValueError("Unexpected loader-directory contents")
        for name, expected in plan["client"]["libraries_sha256"].items():
            regular(libraries / name)
            if hashlib.sha256(read_bounded_bytes(libraries / name, max_bytes=64 * 1024 ** 2)).hexdigest() != expected: raise ValueError("A loader input differs from its reviewed pin")
    for tool in TOOLS:
        output = command(plan, tool, ["--version"]).strip()
        if not re.fullmatch(re.escape(tool) + r" \(PostgreSQL\) " + re.escape(plan["postgres_version"]) + r"(?: [^\r\n]*)?", output): raise ValueError("The pinned client versions differ from the selected PostgreSQL patch")


def tls_inputs(plan):
    inputs = Path(plan["tls"]["inputs_dir"]); directory(inputs, private=True)
    ca, cert, key = (inputs / name for name in ("ca.crt", "client.crt", "client.key"))
    regular(ca); regular(cert); regular(key, private=True)
    ca_bytes = read_bounded_bytes(ca, max_bytes=256 * 1024); cert_bytes = read_bounded_bytes(cert, max_bytes=256 * 1024)
    key_bytes = read_bounded_bytes(
        key,
        max_bytes=64 * 1024,
    )
    if (hashlib.sha256(ca_bytes).hexdigest() != plan["tls"]["ca_sha256"] or hashlib.sha256(cert_bytes).hexdigest() != plan["tls"]["client_certificate_sha256"]
            or b"PRIVATE KEY" in ca_bytes + cert_bytes or b"ENCRYPTED" in key_bytes
            or not key_bytes.startswith(tuple(b"-----BEGIN " + name + b"-----" for name in (b"PRIVATE KEY", b"RSA PRIVATE KEY", b"EC PRIVATE KEY")))): raise ValueError("Trust or client certificate material changed")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=ca_bytes.decode("ascii")); context.load_cert_chain(cert, key)


def connection(plan, *, identify=False):
    source = plan["source"]
    value = " ".join(key + "=" + str(source[key]) for key in ("host", "hostaddr", "port", "user"))
    return value + (" replication=true" if identify else "")


def wal_position(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}", value): raise ValueError("Missing WAL position")
    high, low = value.split("/"); result = (int(high, 16) << 32) + int(low, 16)
    if not result: raise ValueError("Missing WAL position")
    return result


def identify(plan):
    prefix = ["-XAtq", "--no-password", "--set=ON_ERROR_STOP=1", "--dbname=" + connection(plan, identify=True)]
    raw = command(plan, "psql", [*prefix, "--command=IDENTIFY_SYSTEM;"], source=True).strip()
    parts = raw.split("|")
    if len(parts) != 4 or not system_identifier(parts[0]) or not re.fullmatch(r"[1-9][0-9]{0,9}", parts[1]) or int(parts[1]) >= 2 ** 32 or parts[3]: raise ValueError("Invalid physical replication identity response")
    wal_position(parts[2])
    for setting, expected in (("server_version_num", str(int(plan["postgres_version"].split(".")[0]) * 10000 + int(plan["postgres_version"].split(".")[1]))), ("ssl", "on"), ("full_page_writes", "on"), ("data_checksums", "on")):
        if command(plan, "psql", [*prefix, "--command=SHOW " + setting + ";"], source=True).strip() != expected: raise ValueError("Source version or physical backup policy differs")
    if plan["system_identifier"] is not None and parts[0] != plan["system_identifier"]: raise ValueError("The source database identity differs from the reviewed pin")
    return {"system_identifier": parts[0], "timeline": int(parts[1]), "wal_position": parts[2]}


def inventory(root, limits):
    directory(root, private=True)
    files, directories, total = {}, [], 0
    for parent, names, leaves in os.walk(root, followlinks=False):
        base = Path(parent)
        for name in names:
            path = base / name; info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077: raise ValueError("Untrusted backup directory")
            directories.append(path.relative_to(root).as_posix())
            if len(files) + len(directories) > limits["max_files"]: raise ValueError("Backup directory inventory limit exceeded")
        for name in leaves:
            path = base / name; info = regular(path, private=True)
            if info.st_uid != os.geteuid(): raise ValueError("Backup files must belong to the capture identity")
            total += info.st_size
            if total > limits["max_backup_bytes"] or len(files) + len(directories) >= limits["max_files"]: raise ValueError("Post-capture backup size or inventory limit exceeded")
            checksum = hashlib.sha256()
            with open_bounded_file(path, max_bytes=info.st_size) as stream:
                while chunk := stream.read(1024 * 1024): checksum.update(chunk)
            after = regular(path, private=True)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns): raise ValueError("Backup file changed while sealing")
            files[path.relative_to(root).as_posix()] = {"size": info.st_size, "sha256": checksum.hexdigest()}
    return {"files": files, "directories": sorted(directories), "bytes": total}


def private_parents(root, parts):
    current = root
    for part in parts:
        current = current / part
        try: current.mkdir(mode=0o700)
        except FileExistsError: pass
        directory(current, private=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def sync_data(data):
    for parent, directories, files in os.walk(data, topdown=False, followlinks=False):
        for name in files:
            descriptor = os.open(Path(parent) / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        sync_directory(Path(parent))


def unpack(archive, destination, limits, budget):
    # Never use extractall or follow paths supplied by the server. Tar format
    # prevents pg_basebackup from writing remote tablespace paths on this host.
    with open_bounded_file(archive, max_bytes=limits["max_backup_bytes"]) as archive_input, tarfile.open(fileobj=archive_input, mode="r|") as stream:
        for item in stream:
            name = item.name.removeprefix("./"); parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or ".." in parts or len(parts) > 16 or len(name) > 512
                    or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts) or not (item.isdir() or item.isfile())):
                raise ValueError("Unsafe backup archive member or unsupported external tablespace")
            budget["entries"] += 1
            if budget["entries"] > limits["max_files"]: raise ValueError("Backup archive inventory limit exceeded")
            path = destination.joinpath(*parts)
            if item.isdir():
                private_parents(destination, parts)
                continue
            if not integer(item.size, 0, limits["max_backup_bytes"]): raise ValueError("Invalid backup archive size")
            budget["bytes"] += item.size
            if budget["bytes"] > limits["max_backup_bytes"]: raise ValueError("Backup extraction size limit exceeded")
            private_parents(destination, parts[:-1])
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            source = stream.extractfile(item)
            with os.fdopen(descriptor, "wb") as output:
                remaining = item.size
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk: raise ValueError("Truncated backup archive member")
                    output.write(chunk); remaining -= len(chunk)


def manifest(data, identity, plan):
    if read_bounded_bytes(data / "PG_VERSION", max_bytes=32).strip() != plan["postgres_version"].split(".")[0].encode():
        raise ValueError("The captured PostgreSQL major version differs")
    raw = read_bounded_bytes(data / "backup_manifest", max_bytes=32 * 1024 ** 2)
    obj = loads_strict_json(raw)
    if (not isinstance(obj, dict) or type(obj.get("PostgreSQL-Backup-Manifest-Version")) is not int or obj["PostgreSQL-Backup-Manifest-Version"] != 2
            or type(obj.get("System-Identifier")) is not int or str(obj["System-Identifier"]) != identity["system_identifier"]
            or not isinstance(obj.get("Files"), list) or not 1 <= len(obj["Files"]) <= plan["limits"]["max_files"]
            or any(not isinstance(row, dict) or row.get("Checksum-Algorithm") != "SHA256" or not digest(row.get("Checksum")) for row in obj["Files"])
            or not isinstance(obj.get("WAL-Ranges"), list) or len(obj["WAL-Ranges"]) != 1): raise ValueError("Manifest identity, SHA-256 file checksums or WAL scope differs")
    wal = obj["WAL-Ranges"][0]
    if set(wal) != {"Timeline", "Start-LSN", "End-LSN"} or type(wal["Timeline"]) is not int or wal["Timeline"] != identity["timeline"] or wal_position(wal["Start-LSN"]) >= wal_position(wal["End-LSN"]): raise ValueError("Manifest spans an unexpected or incomplete database timeline")
    return {"manifest_sha256": hashlib.sha256(raw).hexdigest(), "required_wal": wal}


def operate(plan, *, mode="local", output=None, receipt_sha256=None):
    validate(plan)
    result = {"local_plan_verified": True, "server_contacted": False, "captured": False, "backup_integrity_verified": False, **UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-source", "capture", "verify"} or output is None: raise ValueError("Choose an explicit backup operation and private directory")
    if mode == "capture" and plan["system_identifier"] is None: raise ValueError("Review and pin the database identity before capture")
    if mode == "verify" and not digest(receipt_sha256): raise ValueError("Pin the exact completed capture receipt for offline verification")
    runner = plan["runner"]; require_runner(plan)
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private backup/evidence directory")
    directory(resolved.parent, private=True)
    if mode != "verify": resolved.mkdir(mode=0o700, exist_ok=False)
    directory(resolved, private=True)
    receipt = resolved / "postgres-backup-proof.json"
    if mode == "verify":
        regular(receipt, private=True); raw_receipt = read_bounded_bytes(receipt, max_bytes=128 * 1024)
        if hashlib.sha256(raw_receipt).hexdigest() != receipt_sha256: raise ValueError("The original capture receipt changed")
        proof = loads_strict_json(raw_receipt)
        if (not isinstance(proof, dict) or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
                or proof.get("plan_sha256") != fingerprint(plan) or proof.get("phase") != "completed" or proof.get("toolset_sha256") != toolset()
                or proof.get("command") != "capture" or proof.get("captured") is not True or proof.get("backup_integrity_verified") is not True
                or proof.get("source_after", {}).get("system_identifier") != plan["system_identifier"]
                or any(proof.get(key) is not False for key in UNVERIFIED)): raise ValueError("Use the completed original backup evidence")
    else:
        proof = {"schema_version": 1, "command": mode, "operation_id": uuid4().hex, "plan_sha256": fingerprint(plan), "toolset_sha256": toolset(), "phase": "starting", **result}
    def record(phase, **values):
        proof.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
        atomic_write_text(receipt, canonical(proof))
        sync_directory(resolved); sync_directory(resolved.parent)
    try:
        if mode != "verify" and machine_identity() != runner["machine_id_sha256"]: raise ValueError("This is not the reviewed backup runner")
        client_inputs(plan)
        if mode == "verify":
            regular(resolved / "sealed-inventory.json", private=True)
            raw = read_bounded_bytes(resolved / "sealed-inventory.json", max_bytes=64 * 1024 ** 2)
            if hashlib.sha256(raw).hexdigest() != proof.get("inventory_sha256"): raise ValueError("The original backup seal changed")
            if inventory(resolved / "data", plan["limits"]) != loads_strict_json(raw): raise ValueError("Backup files differ from the original seal")
            manifest(resolved / "data", proof["source_after"], plan)
            command(plan, "pg_verifybackup", ["--exit-on-error", str(resolved / "data")], seconds=plan["limits"]["timeout_seconds"])
            if inventory(resolved / "data", plan["limits"]) != loads_strict_json(raw): raise ValueError("Backup changed during offline verification")
            return {**result, "captured": True, "backup_integrity_verified": True, "receipt_sha256": receipt_sha256}
        record("starting")
        tls_inputs(plan)
        before = identify(plan); record("source-checked", server_contacted=True, source_before=before, runner_machine_id_sha256=runner["machine_id_sha256"])
        if mode == "check-source":
            record("source-check-completed")
            return {**result, "server_contacted": True}
        if shutil.disk_usage(resolved).free < 2 * plan["limits"]["max_backup_bytes"] + 16 * 1024 ** 2: raise ValueError("Reserve space for transport plus extracted backup and enforce a separate filesystem quota")
        transport = resolved / "transport"; transport.mkdir(mode=0o700)
        record("before-capture", capture_attempted=True)
        command(plan, "pg_basebackup", ["--no-password", "--dbname=" + connection(plan), "--pgdata=" + str(transport), "--format=tar", "--wal-method=stream",
            "--checkpoint=fast", "--manifest-checksums=SHA256", "--manifest-force-encode", "--max-rate=" + str(plan["limits"]["max_rate_kib"])], source=True, seconds=plan["limits"]["timeout_seconds"])
        record("capture-client-finished")
        if {p.name for p in transport.iterdir()} != {"base.tar", "pg_wal.tar", "backup_manifest"}: raise ValueError("Unsupported external tablespaces or transport inventory")
        inventory(transport, plan["limits"])
        data = resolved / "data"; data.mkdir(mode=0o700)
        budget = {"entries": 0, "bytes": 0}
        unpack(transport / "base.tar", data, plan["limits"], budget)
        wal = data / "pg_wal"; wal.mkdir(mode=0o700, exist_ok=True); directory(wal, private=True)
        unpack(transport / "pg_wal.tar", wal, plan["limits"], budget)
        descriptor = os.open(data / "backup_manifest", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        raw = read_bounded_bytes(transport / "backup_manifest", max_bytes=32 * 1024 ** 2)
        with os.fdopen(descriptor, "wb") as stream: stream.write(raw)
        details = manifest(data, before, plan)
        checked_inventory = inventory(data, plan["limits"])
        command(plan, "pg_verifybackup", ["--exit-on-error", str(data)], seconds=plan["limits"]["timeout_seconds"])
        after = identify(plan)
        if (after["system_identifier"] != before["system_identifier"] or after["timeline"] != before["timeline"]
                or wal_position(after["wal_position"]) < max(wal_position(before["wal_position"]), wal_position(details["required_wal"]["End-LSN"]))): raise ValueError("Source history changed during capture")
        client_inputs(plan); tls_inputs(plan)
        sync_data(data)
        if inventory(data, plan["limits"]) != checked_inventory: raise ValueError("Backup changed during capture verification")
        seal = canonical(checked_inventory)
        if len(seal.encode()) > 64 * 1024 ** 2: raise ValueError("The backup inventory exceeds its supported evidence bound")
        atomic_write_text(resolved / "sealed-inventory.json", seal)
        result.update(server_contacted=True, captured=True, backup_integrity_verified=True)
        record("completed", **result, source_after=after, **details, inventory_sha256=hashlib.sha256(seal.encode()).hexdigest())
        return {**result, "receipt_sha256": hashlib.sha256(read_bounded_bytes(receipt, max_bytes=128 * 1024)).hexdigest()}
    except BaseException:
        if mode != "verify": record("failed", captured=False, backup_integrity_verified=False, failed_at=proof["phase"], error_code="postgres-backup-unverified", **UNVERIFIED)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--receipt-sha256", help="Reviewed completed capture receipt required for offline verification")
    modes = parser.add_mutually_exclusive_group()
    for name in ("check-source", "capture", "verify"): modes.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        mode = next((name.replace("_", "-") for name in ("check_source", "capture", "verify") if getattr(args, name)), "local")
        print(json.dumps(operate(plan, mode=mode, output=args.output_dir, receipt_sha256=args.receipt_sha256)))
        return 0
    except Exception:
        print("PostgreSQL backup operation refused; private details were suppressed. Preserve partial captures for investigation.")
        return 1


if __name__ == "__main__": raise SystemExit(main())
