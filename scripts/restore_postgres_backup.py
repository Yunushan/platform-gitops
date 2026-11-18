#!/usr/bin/env python3
"""Restore a pinned physical backup into a fresh private Unix-socket drill and stop it."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import open_bounded_file, read_bounded_bytes
from bounded_subprocess import run_bounded
import postgres_physical_backup as backup
import postgres_restore_child as child
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("postgres", "psql", "pg_controldata")
SCHEMA = "platform_restore_canary"
IDENTITY_SQL = """BEGIN READ ONLY;
SET LOCAL search_path=pg_catalog;
SELECT jsonb_build_object(
 'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
 'version',current_setting('server_version_num')::integer,
 'database',current_database(), 'user',current_user,
 'superuser',(SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
 'in_recovery',pg_is_in_recovery(), 'server_address',inet_server_addr(),
 'data_directory',current_setting('data_directory'),
 'listen_addresses',current_setting('listen_addresses'),
 'read_only',current_setting('transaction_read_only'),
 'encoding',current_setting('server_encoding'),
 'checksums',current_setting('data_checksums'),
 'replay_lsn',pg_last_wal_replay_lsn()::text);
ROLLBACK;
"""
UNVERIFIED = {key: False for key in ("restore_verified", "application_restore_verified", "off_platform_verified", "network_namespace_verified", "ha_verified", "full_stack_verified")}


def implementation():
    return {"verifier_sha256": hashlib.sha256(read_bounded_bytes(Path(__file__), max_bytes=256 * 1024)).hexdigest(),
            "child_sha256": hashlib.sha256(read_bounded_bytes(Path(child.__file__), max_bytes=128 * 1024)).hexdigest(),
            "backup_toolset_sha256": backup.toolset()}


def validate(plan, original):
    fields = {"schema_version", "backup_plan_sha256", "backup_receipt_sha256", "runner_machine_id_sha256", "server", "runtime_parent", "database_user", "port", "limits", "witnesses"}
    if (not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or not backup.system_identifier(original["system_identifier"])
            or plan["backup_plan_sha256"] != backup.fingerprint(original) or not backup.digest(plan["backup_receipt_sha256"])
            or not backup.digest(plan["runner_machine_id_sha256"]) or plan["runner_machine_id_sha256"] in original["runner"]["declared_database_machine_ids_sha256"]
            or not backup.policy.absolute(plan["runtime_parent"]) or not plan["runtime_parent"].startswith("/run/")
            or not isinstance(plan["database_user"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", plan["database_user"])
            or not backup.integer(plan["port"], 1024, 65535)): raise ValueError("Use a complete reviewed restore-drill scope")
    server = plan["server"]
    if (not isinstance(server, dict) or set(server) != {"bin_dir", "artifacts_sha256", "library_dir", "libraries_sha256", "asset_trees_sha256"}
            or not backup.policy.absolute(server["bin_dir"]) or not isinstance(server["artifacts_sha256"], dict) or set(server["artifacts_sha256"]) != set(TOOLS)
            or any(not backup.digest(v) for v in server["artifacts_sha256"].values()) or not isinstance(server["libraries_sha256"], dict)
            or server["library_dir"] is not None and not backup.policy.absolute(server["library_dir"])
            or bool(server["libraries_sha256"]) != (server["library_dir"] is not None) or len(server["libraries_sha256"]) > 32
            or any(not re.fullmatch(r"lib[A-Za-z0-9_.+-]+[.]so(?:[.][0-9]+)*", k) or not backup.digest(v) for k, v in server["libraries_sha256"].items())
            or not isinstance(server["asset_trees_sha256"], dict) or not 1 <= len(server["asset_trees_sha256"]) <= 4
            or any(not backup.policy.absolute(k) or not backup.digest(v) for k, v in server["asset_trees_sha256"].items())):
        raise ValueError("Pin the restore server, SQL client, control-data reader and private loader inputs")
    limits = plan["limits"]
    if (not isinstance(limits, dict) or set(limits) != {"startup_seconds", "query_seconds", "stop_seconds", "max_memory_bytes", "max_log_bytes"}
            or not backup.integer(limits["startup_seconds"], 5, 300) or not backup.integer(limits["query_seconds"], 2, 30)
            or not backup.integer(limits["stop_seconds"], 5, 60) or not backup.integer(limits["max_memory_bytes"], 512 * 1024 ** 2, 16 * 1024 ** 3)
            or not backup.integer(limits["max_log_bytes"], 1024 ** 2, 32 * 1024 ** 2)): raise ValueError("Bound the restore process, observations and log")
    witnesses = plan["witnesses"]
    if not isinstance(witnesses, list) or not 1 <= len(witnesses) <= 8: raise ValueError("Pin at least one independently recorded backup-time data witness")
    for witness in witnesses:
        if (not isinstance(witness, dict) or set(witness) != {"database", "rows", "sha256"} or not isinstance(witness["database"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", witness["database"]) or not backup.integer(witness["rows"], 1, 1000)
                or not backup.digest(witness["sha256"])): raise ValueError("Use bounded reserved-schema witness observations")
    if len({w["database"] for w in witnesses}) != len(witnesses): raise ValueError("Witness databases must be distinct")


def inputs(original, plan, source):
    backup.validate(original); validate(plan, original)
    raw = read_bounded_bytes(source / "postgres-backup-proof.json", max_bytes=128 * 1024)
    if hashlib.sha256(raw).hexdigest() != plan["backup_receipt_sha256"]: raise ValueError("The original backup receipt differs")
    proof = loads_strict_json(raw)
    if (not isinstance(proof, dict) or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1 or proof.get("phase") != "completed" or proof.get("command") != "capture"
            or proof.get("plan_sha256") != plan["backup_plan_sha256"] or proof.get("toolset_sha256") != backup.toolset()
            or proof.get("captured") is not True or proof.get("backup_integrity_verified") is not True
            or any(proof.get(k) is not False for k in backup.UNVERIFIED) or proof.get("source_after", {}).get("system_identifier") != original["system_identifier"]):
        raise ValueError("Use the completed original captured backup")
    return proof


def tool_plan(original, plan):
    return {**original, "client": plan["server"]}


def asset_tree(root):
    backup.directory(root); files = {}; total = 0
    for parent, directories, leaves in os.walk(root, followlinks=False):
        for name in directories: backup.directory(Path(parent) / name)
        for name in leaves:
            path = Path(parent) / name; info = backup.regular(path)
            total += info.st_size
            if len(files) >= 20000 or total > 512 * 1024 ** 2: raise ValueError("Restore runtime asset inventory exceeds its bound")
            files[path.relative_to(root).as_posix()] = hashlib.sha256(read_bounded_bytes(path, max_bytes=64 * 1024 ** 2)).hexdigest()
    if not files: raise ValueError("Restore runtime assets are missing")
    return backup.fingerprint(files)


def runtime_inputs(original, plan):
    server = plan["server"]; binaries = Path(server["bin_dir"]); backup.directory(binaries)
    for path, expected in server["asset_trees_sha256"].items():
        if asset_tree(Path(path)) != expected: raise ValueError("Restore runtime assets differ from their reviewed tree pins")
    if server["library_dir"] is not None:
        libraries = Path(server["library_dir"]); backup.directory(libraries)
        if {p.name for p in libraries.iterdir()} != set(server["libraries_sha256"]): raise ValueError("Unreviewed restore loader input")
        for name, digest in server["libraries_sha256"].items():
            backup.regular(libraries / name)
            if hashlib.sha256(read_bounded_bytes(libraries / name, max_bytes=64 * 1024 ** 2)).hexdigest() != digest: raise ValueError("Restore loader input differs")
    for tool, digest in server["artifacts_sha256"].items():
        path = binaries / tool; info = backup.regular(path)
        if info.st_mode & 0o6000 or not info.st_mode & 0o111 or hashlib.sha256(read_bounded_bytes(path, max_bytes=64 * 1024 ** 2)).hexdigest() != digest: raise ValueError("Restore executable differs")
        output = backup.command(tool_plan(original, plan), tool, ["--version"]).strip()
        if not re.fullmatch(re.escape(tool) + r" \(PostgreSQL\) " + re.escape(original["postgres_version"]) + r"(?: [^\r\n]*)?", output): raise ValueError("Restore executable versions differ")


def clone_data(source, target, seal, limits):
    target.mkdir(mode=0o700)
    for name in sorted(seal["directories"], key=lambda p: (len(Path(p).parts), p)):
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts: raise ValueError("Invalid sealed directory")
        backup.private_parents(target, parts)
    for name, expected in seal["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts: raise ValueError("Invalid sealed file")
        origin, destination = source / relative, target / relative
        info = backup.regular(origin, private=True); backup.private_parents(target, relative.parts[:-1])
        checksum = hashlib.sha256(); written = 0
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with open_bounded_file(origin, max_bytes=expected["size"]) as reader, os.fdopen(descriptor, "wb") as writer:
            while chunk := reader.read(1024 * 1024):
                written += len(chunk)
                if written > expected["size"]: raise ValueError("Backup changed during copying")
                checksum.update(chunk); writer.write(chunk)
        after = backup.regular(origin, private=True)
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or written != expected["size"] or checksum.hexdigest() != expected["sha256"]): raise ValueError("Copied backup content differs from the original seal")
    if backup.inventory(target, limits) != seal: raise ValueError("Copied backup inventory differs")


def configure(original, plan, operation, output, socket):
    import pwd
    data = output / "data"; quarantine = output / "quarantine"; quarantine.mkdir(mode=0o700)
    for name in ("postgresql.conf", "postgresql.auto.conf", "standby.signal", "recovery.signal", "postmaster.pid", "postmaster.opts"):
        if (data / name).exists():
            backup.regular(data / name, private=True); os.rename(data / name, quarantine / name)
    atomic_write_text(data / "postgresql.auto.conf", "# Restore drill: original automatic configuration quarantined.\n")
    account = pwd.getpwuid(os.geteuid()).pw_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", account): raise ValueError("Use a simple reviewed local account identity")
    hba, ident, config = (output / name for name in ("drill-hba.conf", "drill-ident.conf", "drill.conf"))
    atomic_write_text(hba, "local all " + plan["database_user"] + " peer map=restore_drill\nhost all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n")
    atomic_write_text(ident, "restore_drill " + account + " " + plan["database_user"] + "\n")
    # All loaded configuration is fresh. In particular no source auto.conf,
    # includes, shell hooks, replication credentials or preload libraries run.
    values = {"data_directory": str(data), "hba_file": str(hba), "ident_file": str(ident), "listen_addresses": "", "port": plan["port"],
        "unix_socket_directories": str(socket), "unix_socket_permissions": "0700", "ssl": "off", "shared_preload_libraries": "", "session_preload_libraries": "", "local_preload_libraries": "",
        "archive_mode": "off", "archive_command": "", "archive_library": "", "restore_command": "", "archive_cleanup_command": "", "recovery_end_command": "",
        "primary_conninfo": "", "primary_slot_name": "", "autovacuum": "off", "max_logical_replication_workers": 0,
        "max_parallel_workers": 0, "max_parallel_maintenance_workers": 0, "max_sync_workers_per_subscription": 0, "jit": "off",
        "hot_standby": "off", "default_transaction_read_only": "on", "password_encryption": "scram-sha-256", "fsync": "on", "full_page_writes": "on",
        "shared_buffers": "32MB", "work_mem": "4MB", "maintenance_work_mem": "16MB", "temp_file_limit": "64MB", "logging_collector": "off", "log_destination": "stderr",
        "statement_timeout": str(plan["limits"]["query_seconds"] * 1000), "lock_timeout": "1000", "idle_in_transaction_session_timeout": str(plan["limits"]["query_seconds"] * 1000),
        "log_statement": "none", "log_min_messages": "warning", "log_min_error_statement": "panic", "cluster_name": "restore-drill-" + operation}
    def literal(value): return str(value) if type(value) is int else "'" + value.replace("'", "''") + "'"
    atomic_write_text(config, "\n".join(key + " = " + literal(value) for key, value in values.items()) + "\n")
    return ["-D", str(data), "-c", "config_file=" + str(config)]


def process_identity(pid):
    raw = read_bounded_bytes(Path("/proc") / str(pid) / "stat", max_bytes=8192).decode()
    fields = raw.rsplit(") ", 1)[1].split()
    return {"pid": pid, "start_ticks": int(fields[19]), "process_group": int(fields[2]), "session": int(fields[3]),
            "boot_id": read_bounded_bytes(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).strip().decode()}


def group_live(identity, operation=None):
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal(): continue
        try:
            fields = read_bounded_bytes(entry / "stat", max_bytes=8192).decode().rsplit(") ", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError): continue
        except PermissionError:
            if entry.stat().st_uid == os.geteuid(): raise ValueError("Cannot observe the owned restore process group")
            continue
        if fields[0] in {"Z", "X"}: continue
        if int(fields[2]) == identity["process_group"] and int(fields[3]) == identity["session"]: return True
        if operation and entry.stat().st_uid == os.geteuid():
            try: command = read_bounded_bytes(entry / "cmdline", max_bytes=65536)
            except (FileNotFoundError, ProcessLookupError): continue
            if command.startswith(("postgres: restore-drill-" + operation + ":").encode()): return True
    return False


def stop(process, identity, seconds, operation=None):
    if identity["process_group"] != process.pid or identity["session"] != process.pid: raise ValueError("The restore process group differs")
    if process.poll() is None:
        if process_identity(process.pid) != identity: raise ValueError("The original restore postmaster identity changed")
        process.send_signal(signal.SIGINT)
        try: process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
    deadline = time.monotonic() + seconds
    while group_live(identity, operation):
        if time.monotonic() >= deadline: raise TimeoutError("The original restore process group has not drained")
        time.sleep(0.1)


def launch(original, plan, arguments, log):
    server = plan["server"]; executable = Path(server["bin_dir"]) / "postgres"
    argv = [sys.executable, "-E", "-s", "-B", str(Path(child.__file__).resolve()), "--parent", str(os.getpid()), "--uid", str(os.geteuid()),
            "--executable", str(executable), "--sha256", server["artifacts_sha256"]["postgres"], "--memory-bytes", str(plan["limits"]["max_memory_bytes"]), "--", *arguments]
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "ab", buffering=0) as stream:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, env=backup.environment(tool_plan(original, plan)),
                                start_new_session=True, umask=0o077)


def sql(original, plan, socket, database, query):
    env = backup.environment(tool_plan(original, plan))
    env["PGCLIENTENCODING"] = "UTF8"
    env["PGOPTIONS"] = "-c default_transaction_read_only=on -c session_preload_libraries= -c local_preload_libraries= -c search_path=pg_catalog -c enable_indexscan=off -c enable_bitmapscan=off -c enable_indexonlyscan=off -c constraint_exclusion=off -c jit=off -c temp_tablespaces="
    connection = "host=" + str(socket) + " port=" + str(plan["port"]) + " user=" + plan["database_user"] + " dbname=" + database
    result = run_bounded([str(Path(plan["server"]["bin_dir"]) / "psql"), "-XAtq", "--no-password", "--set=ON_ERROR_STOP=1", "--dbname=" + connection,
                          "--command=" + query], env=env, input="", text=True, check=False, timeout=plan["limits"]["query_seconds"], output_max_bytes=128 * 1024)
    if result.returncode: raise ValueError("Restore SQL observation failed; private diagnostics suppressed")
    return loads_strict_json(result.stdout.strip())


def observe(original, plan, socket, output, proof):
    expected_version = int(original["postgres_version"].split(".")[0]) * 10000 + int(original["postgres_version"].split(".")[1])
    observations = []
    for witness in plan["witnesses"]:
        value = sql(original, plan, socket, witness["database"], IDENTITY_SQL)
        expected = {"system_identifier": original["system_identifier"], "version": expected_version, "database": witness["database"], "user": plan["database_user"],
                    "superuser": True, "in_recovery": False, "server_address": None, "data_directory": str(output / "data"), "listen_addresses": "", "read_only": "on", "encoding": "UTF8", "checksums": "on"}
        if not isinstance(value, dict) or any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in expected.items()): raise ValueError("Restored server identity, local connection or consistency checks differ")
        if backup.wal_position(value.get("replay_lsn")) < backup.wal_position(proof["required_wal"]["End-LSN"]): raise ValueError("Restored server has not replayed the required backup WAL")
        metadata = sql(original, plan, socket, witness["database"], """BEGIN READ ONLY;
SET LOCAL search_path=pg_catalog;
SELECT jsonb_build_object('kind',c.relkind,'am',a.amname,'rules',c.relhasrules,'rls',c.relrowsecurity,
 'columns',(SELECT jsonb_agg(jsonb_build_array(x.attname,x.atttypid::integer,x.attgenerated,x.attnotnull) ORDER BY x.attnum)
 FROM pg_attribute x WHERE x.attrelid=c.oid AND x.attnum>0 AND NOT x.attisdropped))
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_am a ON a.oid=c.relam
WHERE n.nspname='platform_restore_canary' AND c.relname='witness';
ROLLBACK;
""")
        if metadata != {"kind": "r", "am": "heap", "rules": False, "rls": False, "columns": [["id", 20, "", True], ["payload", 25, "", True]]}: raise ValueError("The reserved witness must be an ordinary heap with builtin scalar columns")
        observed = sql(original, plan, socket, witness["database"], """BEGIN READ ONLY;
SET LOCAL search_path=pg_catalog;
WITH sample AS MATERIALIZED (SELECT id,payload FROM ONLY platform_restore_canary.witness LIMIT 1001),
metrics AS MATERIALIZED (SELECT count(*) AS rows,coalesce(sum(octet_length(payload)),0) AS bytes FROM sample),
bounded AS (SELECT sample.* FROM sample,metrics WHERE metrics.rows<=1000 AND metrics.bytes<=1048576)
SELECT jsonb_build_object('rows',(SELECT rows FROM metrics),'bytes',(SELECT bytes FROM metrics),
 'sha256',(SELECT encode(sha256(convert_to(coalesce(string_agg(id::text||':'||octet_length(payload)::text||':'||payload,E'\\n' ORDER BY id,payload COLLATE "C"),''),'UTF8')),'hex') FROM bounded));
ROLLBACK;
""")
        if (not isinstance(observed, dict) or not backup.integer(observed.get("rows"), 1, 1000) or observed.get("rows") != witness["rows"] or observed.get("sha256") != witness["sha256"]
                or not backup.integer(observed.get("bytes"), 0, 1048576)): raise ValueError("Restored backup-time witness data differs")
        observations.append({"database_sha256": hashlib.sha256(witness["database"].encode()).hexdigest(), "rows": witness["rows"], "sha256": witness["sha256"], "replay_lsn": value["replay_lsn"]})
    return observations


def check_shutdown(original, plan, output):
    raw = backup.command(tool_plan(original, plan), "pg_controldata", [str(output / "data")])
    fields = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip() for line in raw.splitlines() if ":" in line}
    if (fields.get("Database cluster state") != "shut down" or fields.get("Database system identifier") != original["system_identifier"]
            or fields.get("Data page checksum version") != "1"): raise ValueError("The restored database control state is not clean and consistent")


def allocate_runtime(plan, operation):
    parent = Path(plan["runtime_parent"]); backup.directory(parent, private=True)
    socket = parent / operation
    if len((str(socket) + "/.s.PGSQL." + str(plan["port"])).encode()) > 100: raise ValueError("Choose a shorter private Unix-socket parent")
    socket.mkdir(mode=0o700, exist_ok=False)
    return socket


def operate(original, plan, source, *, run=False, output=None):
    proof = inputs(original, plan, source)
    result = {"local_inputs_verified": True, "database_restore_drill_verified": False, "server_stopped": False, **UNVERIFIED}
    if type(run) is not bool: raise ValueError("Select an explicit restore drill")
    if not run: return result
    if output is None: raise ValueError("Use a fresh private drill directory")
    backup.require_runner(original)
    if backup.machine_identity() != plan["runner_machine_id_sha256"]: raise ValueError("This is not the approved drill runner")
    resolved = output.resolve(); source = source.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink() or resolved.is_relative_to(source) or source.is_relative_to(resolved): raise ValueError("Keep the original backup and fresh drill data separate")
    runtime_parent = Path(plan["runtime_parent"]).resolve()
    if any(a.is_relative_to(b) or b.is_relative_to(a) for a, b in ((runtime_parent, source), (runtime_parent, resolved))): raise ValueError("Keep the runtime socket separate from backup and restored data")
    backup.directory(resolved.parent, private=True)
    backup.operate(original, mode="verify", output=source, receipt_sha256=plan["backup_receipt_sha256"])
    runtime_inputs(original, plan)
    resolved.mkdir(mode=0o700, exist_ok=False)
    operation = uuid4().hex
    receipt = resolved / "postgres-restore-proof.json"
    state = {"schema_version": 1, "command": "database-restore-drill", "operation_id": operation, "restore_plan_sha256": backup.fingerprint(plan),
             "backup_receipt_sha256": plan["backup_receipt_sha256"], **implementation(), **result}
    def record(phase, **values):
        state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
        atomic_write_text(receipt, backup.canonical(state)); backup.sync_directory(resolved); backup.sync_directory(resolved.parent)
    process = identity = None
    record("starting")
    try:
        socket = allocate_runtime(plan, operation)
        seal = loads_strict_json(read_bounded_bytes(source / "sealed-inventory.json", max_bytes=64 * 1024 ** 2))
        clone_data(source / "data", resolved / "data", seal, original["limits"])
        arguments = configure(original, plan, operation, resolved, socket)
        backup.sync_data(resolved / "data")
        record("before-server-start", startup_attempted=True, runtime_directory=str(socket))
        process = launch(original, plan, arguments, resolved / "postgres.log")
        identity = process_identity(process.pid); record("server-handle-recorded", process=identity)
        deadline = time.monotonic() + plan["limits"]["startup_seconds"]
        while True:
            if process.poll() is not None: raise ValueError("The restore server exited before becoming available")
            if (resolved / "postgres.log").stat().st_size > plan["limits"]["max_log_bytes"]: raise ValueError("The restore log exceeded its bound")
            pid = resolved / "data/postmaster.pid"
            if pid.exists():
                lines = read_bounded_bytes(pid, max_bytes=4096).decode().splitlines()
                if len(lines) >= 8 and lines[-1].strip() == "ready":
                    if lines[0] != str(process.pid) or lines[1] != str(resolved / "data") or lines[3] != str(plan["port"]) or lines[4] != str(socket): raise ValueError("The observed socket belongs to another postmaster")
                    break
            if time.monotonic() >= deadline: raise TimeoutError("The original restore server is not ready")
            time.sleep(0.1)
        record("server-ready")
        observations = observe(original, plan, socket, resolved, proof)
        record("witnesses-observed", observations=observations)
        stop(process, identity, plan["limits"]["stop_seconds"], operation)
        if process.returncode != 0: raise ValueError("The restore server did not shut down cleanly")
        check_shutdown(original, plan, resolved)
        record("server-stopped", server_stopped=True)
        backup.operate(original, mode="verify", output=source, receipt_sha256=plan["backup_receipt_sha256"])
        runtime_inputs(original, plan)
        if any(state[key] != value for key, value in implementation().items()): raise ValueError("Restore implementation changed during the drill")
        result.update(database_restore_drill_verified=True, server_stopped=True)
        record("completed", **result)
        socket.rmdir()
        return result
    except BaseException:
        stopped = False
        try:
            if process is not None:
                if identity is None and process.poll() is not None:
                    identity = {"pid": process.pid, "process_group": process.pid, "session": process.pid}
                elif identity is None: identity = process_identity(process.pid)
                stop(process, identity, plan["limits"]["stop_seconds"], operation); stopped = True
        finally:
            record("failed", database_restore_drill_verified=False, server_stopped=stopped, error_code="postgres-restore-drill-unverified", **UNVERIFIED)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("backup-plan", "restore-plan", "backup-dir"): parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--run-drill", action="store_true"); parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        original = loads_strict_json(read_bounded_bytes(args.backup_plan, max_bytes=128 * 1024))
        plan = loads_strict_json(read_bounded_bytes(args.restore_plan, max_bytes=128 * 1024))
        print(json.dumps(operate(original, plan, args.backup_dir, run=args.run_drill, output=args.output_dir)))
        return 0
    except Exception:
        print("PostgreSQL restore drill refused; preserve the private operation directory and original process handle. Private details suppressed.")
        return 1


if __name__ == "__main__": raise SystemExit(main())
