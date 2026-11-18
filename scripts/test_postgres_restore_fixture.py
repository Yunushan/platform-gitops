#!/usr/bin/env python3
"""Run a real non-root offline restore drill using the captured fixture backup."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from bounded_subprocess import run_bounded
import restore_postgres_backup as restore


def exercise(test, state):
    base = state["workspace"]; original = state["plan"]
    with tempfile.TemporaryDirectory(prefix="pgr-", dir="/run") as temporary:
        parent = Path(temporary); parent.chmod(0o700); os.chown(parent, 65534, 65534)
        # Preserve the distro's relative bin/share layout. A copied standalone
        # executable is insufficient for PostgreSQL's timezone/catalog assets.
        installation = base / "server/usr"
        bins = installation / "lib/postgresql/18/bin"; bins.mkdir(parents=True, mode=0o755)
        share = installation / "share/postgresql/18"
        shutil.copytree(state["binaries"].parents[3] / "share/postgresql/18", share)
        library = installation / "lib/postgresql/18/lib"
        shutil.copytree(state["binaries"].parent / "lib", library)
        # The source archive is on the Windows-mounted workspace; normalize
        # its copied mode bits before using the files as trusted Linux assets.
        for path in [base / "server", *(base / "server").rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        tree = {path.relative_to(share).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in share.rglob("*") if path.is_file()}
        server = {"bin_dir": str(bins), "artifacts_sha256": {}, "library_dir": original["client"]["library_dir"], "libraries_sha256": original["client"]["libraries_sha256"],
                  "asset_trees_sha256": {str(share): restore.backup.fingerprint(tree), str(library): restore.backup.fingerprint({
                      path.relative_to(library).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in library.rglob("*") if path.is_file()})}}
        for name in restore.TOOLS:
            target = bins / name
            if not target.exists(): shutil.copyfile(state["binaries"] / name, target); target.chmod(0o755)
            server["artifacts_sha256"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
        rows = [(1, "fixture restore witness one"), (2, "fixture restore witness iki — Türkçe")]
        witness = hashlib.sha256("\n".join(str(i) + ":" + str(len(v.encode())) + ":" + v for i, v in rows).encode()).hexdigest()
        plan = {"schema_version": 1, "backup_plan_sha256": restore.backup.fingerprint(original), "backup_receipt_sha256": state["receipt_sha256"],
            "runner_machine_id_sha256": restore.backup.machine_identity(), "runtime_parent": str(parent), "server": server, "database_user": "postgres", "port": 16543,
            "limits": {"startup_seconds": 20, "query_seconds": 10, "stop_seconds": 10, "max_memory_bytes": 1024 ** 3, "max_log_bytes": 4 * 1024 ** 2},
            "witnesses": [{"database": "postgres", "rows": 2, "sha256": witness}]}
        plan_path = base / "private/restore-plan.json"; plan_path.write_text(json.dumps(plan)); plan_path.chmod(0o600); os.chown(plan_path, 65534, 65534)
        scripts = Path(__file__).resolve().parent
        program = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import postgres_physical_backup as backup
import restore_postgres_backup as restore
backup.ROOT=restore.ROOT=Path(sys.argv[2])
original=json.loads(Path(sys.argv[3]).read_text())
plan=json.loads(Path(sys.argv[4]).read_text())
print(json.dumps(restore.operate(original,plan,backup.ROOT/'private/capture',run=True,output=backup.ROOT/'private'/sys.argv[5])))
"""
        command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C",
                   sys.executable, "-E", "-s", "-B", "-c", program, str(scripts), str(base), str(state["plan_path"]), str(plan_path)]
        result = run_bounded([*command, "restore"], text=True, timeout=90, check=False, output_max_bytes=16384)
        test.assertEqual(result.returncode, 0, "Real restore drill failed; private fixture diagnostics suppressed")
        flags = json.loads(result.stdout); test.assertTrue(flags["database_restore_drill_verified"]); test.assertTrue(flags["server_stopped"])
        for key in restore.UNVERIFIED: test.assertFalse(flags[key])
        proof = json.loads((base / "private/restore/postgres-restore-proof.json").read_text())
        test.assertEqual(proof["phase"], "completed"); test.assertEqual(proof["observations"][0]["rows"], 2)
        test.assertFalse(list(parent.iterdir()))
        # An accepted backup can still fail its independently expected data
        # witness; the new instance must be stopped and success remain false.
        plan["witnesses"][0]["sha256"] = "f" * 64; plan_path.write_text(json.dumps(plan))
        result = run_bounded([*command, "wrong-witness"], text=True, timeout=90, check=False, output_max_bytes=16384)
        test.assertNotEqual(result.returncode, 0)
        failed = json.loads((base / "private/wrong-witness/postgres-restore-proof.json").read_text())
        test.assertFalse(failed["database_restore_drill_verified"]); test.assertTrue(failed["server_stopped"])
        # Failed private runtime directories are retained only if nonempty.
        for directory in parent.iterdir(): directory.rmdir()


def parent_death(test):
    """Exercise the exec guard's real Linux parent-death behavior with one idle process."""
    import signal
    import time
    if os.name != "posix" or os.geteuid() != 0: raise ValueError("The opt-in parent-death fixture requires a disposable root Linux host")
    with tempfile.TemporaryDirectory(prefix="pdp-", dir="/run") as temporary:
        root = Path(temporary); root.chmod(0o700); os.chown(root, 65534, 65534)
        # A distro may ship a multicall coreutils binary: renaming sleep to
        # postgres can make it exit immediately based on argv[0]. Compile an
        # idle single-process fixture so only the kernel guard can end it.
        executable = root / "postgres"
        compiled = run_bounded(["/usr/bin/cc", "-x", "c", "-o", str(executable), "-"],
            input="#include <unistd.h>\nint main(void) { for (;;) pause(); }\n", text=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False, timeout=30, output_max_bytes=8192)
        test.assertEqual(compiled.returncode, 0, "The disposable kernel-guard fixture requires a working C compiler")
        executable.chmod(0o755)
        expected = hashlib.sha256(executable.read_bytes()).hexdigest()
        scripts = Path(__file__).resolve().parent; marker = root / "child.json"
        program = """import json,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import postgres_restore_child as child
import restore_postgres_backup as restore
process=subprocess.Popen([sys.executable,'-E','-s','-B',child.__file__,'--parent',str(os.getpid()),'--uid','65534','--executable',sys.argv[2],'--sha256',sys.argv[3],'--memory-bytes',str(512*1024**2),'--','30'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
deadline=time.monotonic()+5
while time.monotonic()<deadline:
 if process.poll() is not None: raise RuntimeError('The synthetic guard child exited early')
 status=Path('/proc',str(process.pid),'status').read_text()
 if 'NoNewPrivs:\\t1' in status and os.readlink('/proc/'+str(process.pid)+'/exe')==sys.argv[2]:
  Path(sys.argv[4]).write_text(json.dumps(restore.process_identity(process.pid)))
  os.kill(os.getpid(),signal.SIGKILL)
 time.sleep(.02)
process.kill();process.wait();raise RuntimeError('The guard child did not arm')
"""
        command = ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C",
                   sys.executable, "-E", "-s", "-B", "-c", program, str(scripts), str(executable), expected, str(marker)]
        result = run_bounded(command, text=True, check=False, timeout=10, output_max_bytes=8192)
        test.assertEqual(result.returncode, -signal.SIGKILL, "Synthetic parent did not exit through the intended kernel test")
        identity = json.loads(marker.read_text()); pid = identity["pid"]
        def alive():
            try:
                fields = Path("/proc", str(pid), "stat").read_text().rsplit(") ", 1)[1].split()
                return int(fields[19]) == identity["start_ticks"] and fields[0] not in {"Z", "X"}
            except FileNotFoundError: return False
        try:
            deadline = time.monotonic() + 5
            while alive() and time.monotonic() < deadline: time.sleep(.05)
            test.assertFalse(alive(), "The original child survived parent death")
        finally:
            if alive() and os.readlink("/proc/" + str(pid) + "/exe") == str(executable): os.kill(pid, signal.SIGKILL)
