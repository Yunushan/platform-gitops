#!/usr/bin/env python3
"""Real certificate-only backup fixture using the existing disposable PostgreSQL cluster."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

from bounded_subprocess import run_bounded
import postgres_physical_backup as backup
from test_postgres_physical_backup import fixture


def capture(test, state, restore=None):
    base = state["root"] / "backup-workspace"; base.mkdir(mode=0o755)
    private = base / "private"; private.mkdir(mode=0o700); os.chown(private, 65534, 65534)
    bins = base / "bin"; bins.mkdir(mode=0o755)
    inputs = base / "inputs"; inputs.mkdir(mode=0o700); os.chown(inputs, 65534, 65534)
    for name in ("ca.crt", "client.crt", "client.key"):
        target = inputs / name; shutil.copyfile(state["root"] / name, target); target.chmod(0o600); os.chown(target, 65534, 65534)
    plan = fixture(); plan.update(postgres_version=state["version"], system_identifier=state["system_identifier"])
    plan["source"].update(host="127.0.0.1", hostaddr="127.0.0.1", user="streaming_replica")
    plan["runner"]["machine_id_sha256"] = backup.machine_identity()
    plan["client"]["bin_dir"] = str(bins)
    for name in backup.TOOLS:
        target = bins / name; shutil.copyfile(state["binaries"] / name, target); target.chmod(0o755)
        plan["client"]["artifacts_sha256"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
    if state["libraries"]:
        libraries = base / "lib"; libraries.mkdir(mode=0o755)
        plan["client"]["library_dir"] = str(libraries)
        for source in Path(state["libraries"]).iterdir():
            if re.fullmatch(r"lib[A-Za-z0-9_.+-]+[.]so(?:[.][0-9]+)*", source.name) and source.is_file():
                target = libraries / source.name; shutil.copyfile(source, target); target.chmod(0o644)
                plan["client"]["libraries_sha256"][source.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    plan["tls"].update(inputs_dir=str(inputs), ca_sha256=hashlib.sha256((inputs / "ca.crt").read_bytes()).hexdigest(),
                        client_certificate_sha256=hashlib.sha256((inputs / "client.crt").read_bytes()).hexdigest())
    plan_path = private / "plan.json"; plan_path.write_text(json.dumps(plan)); plan_path.chmod(0o600); os.chown(plan_path, 65534, 65534)
    scripts = Path(__file__).resolve().parent
    if restore:
        environment = ["PATH=/usr/bin:/bin", "LC_ALL=C"]
        if state["libraries"]: environment += ["LD_LIBRARY_PATH=" + state["libraries"]]
        statement = "CREATE SCHEMA platform_restore_canary; CREATE TABLE platform_restore_canary.witness(id BIGINT PRIMARY KEY,payload TEXT NOT NULL); INSERT INTO platform_restore_canary.witness VALUES (1,'fixture restore witness one'),(2,'fixture restore witness iki — Türkçe');"
        result = run_bounded(["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/env", "-i", *environment,
            str(state["binaries"] / "psql"), "-XAtq", "--no-password", "--set=ON_ERROR_STOP=1", "-h", str(state["root"] / "socket-1"), "-p", "5432", "-U", "postgres", "-d", "postgres", "-c", statement],
            text=True, timeout=30, check=False, output_max_bytes=16384)
        test.assertEqual(result.returncode, 0, "Fixture backup-time witness creation failed")
    program = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import postgres_physical_backup as backup
backup.ROOT=Path(sys.argv[2])
plan=json.loads(Path(sys.argv[3]).read_text())
result=backup.operate(plan,mode=sys.argv[4],output=backup.ROOT/'private/capture',receipt_sha256=sys.argv[5] if len(sys.argv)>5 else None)
print(json.dumps(result))
"""
    prefix = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C",
              sys.executable, "-E", "-s", "-B", "-c", program, str(scripts), str(base), str(plan_path)]
    def run(mode, pin=None, *, success=True):
        result = run_bounded([*prefix, mode, *([pin] if pin else [])], text=True, check=False, timeout=90, output_max_bytes=16384)
        if success:
            test.assertEqual(result.returncode, 0, "Real backup fixture failed; private diagnostics suppressed")
            return json.loads(result.stdout)
        test.assertNotEqual(result.returncode, 0)
        return None
    result = run("capture"); test.assertTrue(result["backup_integrity_verified"])
    for key in backup.UNVERIFIED: test.assertFalse(result[key])
    pin = result["receipt_sha256"]
    # Complete offline verification must not require the capture credentials.
    shutil.rmtree(inputs)
    test.assertTrue(run("verify", pin)["backup_integrity_verified"])
    if restore: restore({**state, "workspace": base, "plan": plan, "plan_path": plan_path, "receipt_sha256": pin, "bins": bins})
    data = private / "capture/data"
    ignored = data / "postgresql.auto.conf"
    saved = ignored.read_bytes(); ignored.write_bytes(saved + b"\n# fixture ignored-file corruption\n")
    run("verify", pin, success=False); ignored.write_bytes(saved)
    # pg_verifybackup itself must also detect a real relation/control-file change.
    path = data / "global/pg_control"; saved = path.read_bytes()
    altered = bytearray(saved); altered[0] ^= 1; path.write_bytes(altered)
    command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C"]
    if plan["client"]["library_dir"]: command += ["LD_LIBRARY_PATH=" + plan["client"]["library_dir"]]
    result = run_bounded([*command, str(bins / "pg_verifybackup"), str(data)], text=True, check=False, timeout=30, output_max_bytes=16384)
    test.assertNotEqual(result.returncode, 0)
    path.write_bytes(saved)
