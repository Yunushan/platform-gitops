#!/usr/bin/env python3
"""Opt-in isolated real PostgreSQL fixture; never uses Kubernetes or company endpoints."""

import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time

from bounded_subprocess import run_bounded
from strict_json import loads_strict_json
from test_postgres_ha import fixture
import verify_postgres_runtime as verify
from render_gerrit_router import dns_name


def exercise(test, hook=None, *, planned_promotion=False, application_tls_name=None,
             max_locks_per_transaction=64, disk_backed=False):
    if type(planned_promotion) is not bool: raise ValueError("Select an explicit disposable promotion case")
    if application_tls_name is not None and (not planned_promotion or not dns_name(application_tls_name)):
        raise ValueError("An application certificate name requires an isolated promotion case and a DNS name")
    if type(max_locks_per_transaction) is not int or not 64 <= max_locks_per_transaction <= 512:
        raise ValueError("Select a bounded PostgreSQL lock table size for the fixture")
    if type(disk_backed) is not bool:
        raise ValueError("Select the fixture storage explicitly")
    if os.name != "posix" or os.geteuid() != 0: raise ValueError("The opt-in fixture requires a disposable root Linux host and setpriv")
    if planned_promotion and (os.environ.get("POSTGRES_PROMOTION_TEST_RUNTIME") != "1" or Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino):
        raise ValueError("Promotion requires explicit opt-in and an owned isolated network namespace")
    if planned_promotion:
        links = run_bounded(["/usr/sbin/ip", "-j", "link", "show"], text=True, timeout=10, check=True, output_max_bytes=16384)
        if [row["ifname"] for row in loads_strict_json(links.stdout)] != ["lo"]: raise ValueError("Use only the owned loopback test namespace")
        run_bounded(["/usr/sbin/ip", "link", "set", "lo", "up"], text=True, timeout=10, check=True, output_max_bytes=16384)
    binaries = Path(os.environ["POSTGRES_RUNTIME_TEST_BIN"]).resolve(strict=True)
    libraries = os.environ.get("POSTGRES_RUNTIME_TEST_LIBS", "")
    for name in ("initdb", "postgres", "pg_ctl", "pg_basebackup", "psql", *(["pg_controldata"] if planned_promotion else [])):
        if not (binaries / name).is_file(): raise ValueError("The fixture binary set is incomplete")
    if not shutil.which("setpriv") or not shutil.which("openssl"): raise ValueError("The fixture requires setpriv and OpenSSL")
    # Never borrow a running local instance. An occupied primary port causes
    # the opt-in fixture to refuse before creating data or stopping anything.
    reservations = [socket.socket() for _ in range(3)]
    hosts = ["127.0.0." + str(i) for i in (11, 12, 13)] if planned_promotion else ["127.0.0.1"] * 3
    try:
        reservations[0].bind((hosts[0], 5432))
        for host, item in zip(hosts[1:], reservations[1:]): item.bind((host, 5432 if planned_promotion else 0))
        ports = [item.getsockname()[1] for item in reservations]
    finally:
        for item in reservations: item.close()
    parent = "/run"
    if disk_backed:
        from test_k3s_vm_fixture import disk_workspace
        parent, _ = disk_workspace()
    with tempfile.TemporaryDirectory(prefix="platform-postgres-query-fixture-", dir=parent) as temporary:
        root = Path(temporary); os.chown(root, 65534, 65534); root.chmod(0o700)
        prefix = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/env", "-i",
                  "PATH=" + str(binaries) + ":/usr/bin:/bin", "HOME=" + str(root), "LC_ALL=C"]
        if libraries: prefix += ["LD_LIBRARY_PATH=" + libraries]
        current = {"index": 1, "host": hosts[0], "port": ports[0]}
        def run(args, timeout=30, expected=0):
            result = run_bounded([*prefix, *map(str, args)], text=True, check=False, timeout=timeout, output_max_bytes=256 * 1024)
            if result.returncode != expected:
                text = (result.stdout + result.stderr).casefold()
                signals = [label for label, marker in (("hba_refusal", "no pg_hba.conf entry"), ("tls_verification", "certificate verify failed"),
                    ("address_in_use", "address already in use"), ("connection_refused", "connection refused"),
                    ("network_unreachable", "network is unreachable"), ("source_stopped", "server closed the connection"),
                    ("shutdown_timeout", "server does not shut down"), ("permission_denied", "permission denied")) if marker in text]
                raise ValueError("Disposable PostgreSQL fixture command failed; safe categories=" + ",".join(signals or ["unclassified"]) + "; private details suppressed")
            return result.stdout
        def write(name, value):
            path = root / name; path.write_text(value, encoding="utf-8"); path.chmod(0o600); os.chown(path, 65534, 65534)
            return path
        def mkdir(name):
            path = root / name; path.mkdir(mode=0o700); os.chown(path, 65534, 65534); return path
        def sql(statement, index=None, *, timeout=30):
            index = current["index"] if index is None else index
            return run([binaries / "psql", "-XAtq", "--no-password", "-v", "ON_ERROR_STOP=1", "-h", root / ("socket-" + str(index)), "-p", ports[index - 1], "-U", "postgres", "-d", "postgres", "-c", statement],
                       timeout=timeout)
        def observe(index):
            args = verify.psql_arguments(18, socket=str(root / ("socket-" + str(index))), port=ports[index - 1])
            location = args.index("/usr/lib/postgresql/18/bin/psql"); args[location] = str(binaries / "psql")
            if libraries: args.insert(2, "LD_LIBRARY_PATH=" + libraries)
            return loads_strict_json(run(args).strip())
        # Only ephemeral fixture keys, scoped to this temporary directory.
        run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
             "-subj", "/CN=fixture-only-postgres-root", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-keyout", root / "ca.key", "-out", root / "ca.crt"])
        for role, subject, purposes in (("server", "fixture-only-postgres", "serverAuth"), ("client", "streaming_replica", "clientAuth")):
            run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=" + subject,
                 "-keyout", root / (role + ".key"), "-out", root / (role + ".csr")])
            names = ["IP:" + host for host in sorted(set(["127.0.0.1", *hosts]))]
            if application_tls_name is not None:
                names.append("DNS:" + application_tls_name)
            write(role + ".ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=" + purposes +
                  ("\nsubjectAltName=" + ",".join(names) + "\n" if role == "server" else "\n"))
            run(["openssl", "x509", "-req", "-days", "1", "-in", root / (role + ".csr"), "-CA", root / "ca.crt", "-CAkey", root / "ca.key",
                 "-CAcreateserial", "-extfile", root / (role + ".ext"), "-out", root / (role + ".crt")])
        for path in root.glob("*.key"): path.chmod(0o600)
        for i in (1, 2, 3): mkdir("socket-" + str(i))
        data = [root / ("data-" + str(i)) for i in (1, 2, 3)]
        try:
            share = binaries.parents[3] / "share/postgresql/18"
            command = [binaries / "initdb", "-D", data[0], "-U", "postgres", "--auth-local=peer", "--auth-host=reject", "--data-checksums", "--no-locale", "--encoding=UTF8"]
            if share.is_dir(): command += ["-L", share]
            run(command)
            hba = "local all postgres peer map=fixture\nhostssl replication streaming_replica 127.0.0.1/32 cert\n"
            ident = "fixture nobody postgres\n"
            for basename, text in (("pg_hba.conf", hba), ("pg_ident.conf", ident)): write("data-1/" + basename, text)
            def configuration(index):
                return ("listen_addresses = '" + hosts[index - 1] + "'\nport = " + str(ports[index - 1]) + "\nunix_socket_directories = '" + str(root / ("socket-" + str(index))) + "'\n"
                    "ssl = on\nssl_min_protocol_version = 'TLSv1.2'\nssl_cert_file = '" + str(root / "server.crt") + "'\nssl_key_file = '" + str(root / "server.key") + "'\n"
                    "ssl_ca_file = '" + str(root / "ca.crt") + "'\nwal_level = replica\nmax_wal_senders = 8\npassword_encryption = 'scram-sha-256'\n"
                    f"max_locks_per_transaction = {max_locks_per_transaction}\n"
                    "synchronous_commit = remote_apply\nsynchronous_standby_names = ''\nfull_page_writes = on\nfsync = on\n")
            write("data-1/postgresql.conf", configuration(1))
            def start(index):
                run([binaries / "pg_ctl", "-D", data[index - 1], "-l", root / ("postgres-" + str(index) + ".log"), "start", "-w", "-t", "15"])
            start(1)
            sql("CREATE ROLE streaming_replica REPLICATION LOGIN;")
            for index in (2, 3):
                connection = ("host=" + hosts[0] + " port=5432 user=streaming_replica sslmode=verify-full sslrootcert=" + str(root / "ca.crt")
                    + " sslcert=" + str(root / "client.crt") + " sslkey=" + str(root / "client.key") + " application_name=platform-postgres-" + str(index))
                run([binaries / "pg_basebackup", "--no-password", "--checkpoint=fast", "--wal-method=stream", "--write-recovery-conf", "--dbname", connection, "-D", data[index - 1]], timeout=60)
                write("data-" + str(index) + "/postgresql.conf", configuration(index)); start(index)
            # Configure quorum only after bootstrap writes and base copies.
            write("data-1/postgresql.conf", configuration(1) + 'synchronous_standby_names = \'ANY 1 ("platform-postgres-2","platform-postgres-3")\'\n')
            run([binaries / "pg_ctl", "-D", data[0], "reload"])
            plan = fixture(); version = observe(1)["server_version_num"]
            plan["postgres_version"] = str(version // 10000) + "." + str(version % 10000)
            # Shared namespace connections have a loopback source address;
            # this is not evidence of separate physical member identities.
            view = {"primary": "platform-postgres-1", "pods": {"platform-postgres-" + str(i): {"addresses": sorted(set(["127.0.0.1", hosts[i - 1]]))} for i in (1, 2, 3)}}
            def strict_sample():
                deadline = time.monotonic() + 30
                while True:
                    samples = {"platform-postgres-" + str(i): observe(i) for i in (1, 2, 3)}; samples["primary_recheck"] = observe(current["index"])
                    try: return verify.compare(samples, plan, view), samples
                    except ValueError:
                        if time.monotonic() >= deadline: raise
                        time.sleep(0.2)
            evidence, samples = strict_sample()
            test.assertEqual(evidence["instances_observed"], 3)
            test.assertTrue(all(row["tls"] and row["client_certificate_present"] for row in samples["platform-postgres-1"]["senders"]))
            promotion_attempted = False
            def promote():
                nonlocal promotion_attempted
                if not planned_promotion or promotion_attempted: raise ValueError("Never repeat an attempted fixture promotion")
                promotion_attempted = True
                before, original = strict_sample()
                source_pid = int((data[0] / "postmaster.pid").read_text().splitlines()[0])
                target_pid = int((data[1] / "postmaster.pid").read_text().splitlines()[0])
                source_ticks = Path("/proc", str(source_pid), "stat").read_text().rsplit(") ", 1)[1].split()[19]
                target_ticks = Path("/proc", str(target_pid), "stat").read_text().rsplit(") ", 1)[1].split()[19]
                # Fence the exact old primary before allowing another writer.
                shutdown_seconds = 60 if disk_backed else 20
                run([binaries / "pg_ctl", "-D", data[0], "stop", "-m", "fast", "-w", "-t", str(shutdown_seconds)],
                    timeout=shutdown_seconds + 8)
                if (data[0] / "postmaster.pid").exists(): raise ValueError("The original primary did not stop")
                if Path("/proc", str(source_pid), "stat").exists() and Path("/proc", str(source_pid), "stat").read_text().rsplit(") ", 1)[1].split()[19] == source_ticks:
                    raise ValueError("Original primary process still exists")
                control = run([binaries / "pg_controldata", "-D", data[0]])
                if not re.search(r"^Database cluster state:\s+shut down$", control, re.M): raise ValueError("Require a clean source shutdown")
                match = re.search(r"^Latest checkpoint location:\s+([0-9A-F]+/[0-9A-F]+)$", control, re.M)
                if not match: raise ValueError("Require the final source checkpoint")
                shutdown_lsn = match[1]; deadline = time.monotonic() + 20
                while verify.lsn(sql("SELECT pg_last_wal_replay_lsn();", 2).strip()) < verify.lsn(shutdown_lsn):
                    if time.monotonic() >= deadline: raise TimeoutError("The candidate did not replay the final shutdown checkpoint")
                    time.sleep(0.1)
                write("data-2/postgresql.conf", configuration(2) + 'synchronous_standby_names = \'ANY 1 ("platform-postgres-1","platform-postgres-3")\'\n')
                run([binaries / "pg_ctl", "-D", data[1], "reload"])
                run([binaries / "pg_ctl", "-D", data[1], "promote", "-w", "-t", "15"])
                current.update(index=2, host=hosts[1], port=ports[1]); view["primary"] = "platform-postgres-2"
                for index in (3, 1):
                    path = data[index - 1] / "postgresql.auto.conf"; text = path.read_text()
                    conninfo = ("host=" + hosts[1] + " port=5432 user=streaming_replica sslmode=verify-full sslrootcert=" + str(root / "ca.crt")
                        + " sslcert=" + str(root / "client.crt") + " sslkey=" + str(root / "client.key") + " application_name=platform-postgres-" + str(index))
                    lines = text.splitlines(); found = [i for i, line in enumerate(lines) if line.startswith("primary_conninfo =")]
                    if len(found) != (0 if index == 1 else 1): raise ValueError("Unexpected original replication configuration")
                    if found: lines[found[0]] = "primary_conninfo = '" + conninfo + "'"
                    else: lines.append("primary_conninfo = '" + conninfo + "'")
                    write("data-" + str(index) + "/postgresql.auto.conf", "\n".join(lines) + "\n")
                    if index == 1:
                        write("data-1/standby.signal", ""); start(1)
                    else: run([binaries / "pg_ctl", "-D", data[index - 1], "reload"])
                # Compare the actual control-file timeline after an explicit
                # checkpoint/restartpoint, rather than accepting stale metadata.
                checkpoint_seconds = 90 if disk_backed else 30
                sql("CHECKPOINT;", 2, timeout=checkpoint_seconds)
                checkpoint = sql("SELECT pg_current_wal_flush_lsn();", 2).strip()
                deadline = time.monotonic() + (60 if disk_backed else 20)
                for index in (1, 3):
                    while verify.lsn(sql("SELECT coalesce(pg_last_wal_replay_lsn(),'0/0'::pg_lsn);", index).strip()) < verify.lsn(checkpoint):
                        if time.monotonic() >= deadline: raise TimeoutError("The replica did not replay the promoted-primary checkpoint")
                        time.sleep(0.1)
                    sql("CHECKPOINT;", index, timeout=checkpoint_seconds)
                after, observed = strict_sample()
                test.assertEqual(after["system_identifier"], before["system_identifier"])
                test.assertGreater(after["timeline_id"], before["timeline_id"])
                test.assertEqual(after["postmaster_started_at"]["platform-postgres-2"], before["postmaster_started_at"]["platform-postgres-2"])
                test.assertEqual(Path("/proc", str(target_pid), "stat").read_text().rsplit(") ", 1)[1].split()[19], target_ticks)
                test.assertTrue(observed["platform-postgres-1"]["in_recovery"])
                return {"source_fenced": True, "shutdown_checkpoint_replayed": True, "target_original_process_preserved": True,
                    "strict_three_member_quorum_restored": True, "timeline_advanced": True, "host": hosts[1], "port": ports[1]}
            state = {"root": root, "binaries": binaries, "libraries": libraries, "version": plan["postgres_version"], "system_identifier": evidence["system_identifier"], "primary": current}
            if planned_promotion: state["promote"] = promote
            if hook: hook(state)
            if planned_promotion and not promotion_attempted: raise ValueError("The selected promotion was never exercised")
            # Confirm the actual server enforces read-only transactions.
            result = run_bounded([*prefix, str(binaries / "psql"), "-XAtq", "--no-password", "-v", "ON_ERROR_STOP=1", "-h", str(root / ("socket-" + str(current["index"]))), "-p", str(current["port"]), "-U", "postgres", "-d", "postgres",
                "-c", "BEGIN READ ONLY; CREATE TABLE fixture_forbidden_write (value int); ROLLBACK;"], text=True, check=False, timeout=15, output_max_bytes=16384)
            test.assertNotEqual(result.returncode, 0); test.assertIn("read-only", result.stderr)
            test.assertEqual(sql("SELECT to_regclass('public.fixture_forbidden_write') IS NULL;").strip(), "t")
            index = current["index"]
            write("data-" + str(index) + "/postgresql.conf", configuration(index)); run([binaries / "pg_ctl", "-D", data[index - 1], "reload"])
            deadline = time.monotonic() + 5
            while observe(index)["settings"]["synchronous_standby_names"]["value"]:
                if time.monotonic() >= deadline: raise TimeoutError("Fixture configuration reload was not observed")
                time.sleep(0.1)
            with test.assertRaises(ValueError): verify.primary(observe(index), plan, view)
        finally:
            # Stop only fixture data directories, including a start whose
            # acknowledgment was lost. Never kill a host's unrelated server.
            for directory in reversed(data):
                if (directory / "postmaster.pid").is_file():
                    run([binaries / "pg_ctl", "-D", directory, "stop", "-m", "immediate", "-w", "-t", "15"])
