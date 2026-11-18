#!/usr/bin/env python3
"""Run the exact host helper with staged real release, Java, PKI and schema."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import tarfile
from uuid import uuid4

from bounded_subprocess import run_bounded
import activate_keycloak_host as activate
import render_keycloak_ha as renderer
from test_keycloak_ha import fixture
from test_keycloak_host_application_fixture import exercise as application_acceptance


def preflight(test):
    from test_keycloak_runtime_fixture import ARTIFACTS
    if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use the explicit root Linux preflight fixture")
    artifacts = Path(os.environ["KEYCLOAK_TEST_ARTIFACTS"]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="platform-keycloak-preflight-", dir="/run") as temporary:
        root = Path(temporary)
        for name, digest in ARTIFACTS.items():
            path = artifacts / name
            if path.is_symlink() or path.stat().st_size > 256 * 1024 ** 2: raise ValueError("Use the reviewed bounded runtime archives")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest: raise ValueError("Runtime archive fingerprint differs")
            with tarfile.open(path) as archive: archive.extractall(root, filter="data")
        (root / "tls").mkdir()
        activate.Host().run(["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(root / "tls/tls.key"), "-out", str(root / "tls/tls.crt")])
        shutil.copyfile(root / "tls/tls.crt", root / "ca.crt")
        import secrets
        context = {"root": root, "native_home": root / "keycloak-26.4.7", "java_home": root / "jdk-21.0.13+9-jre", "database_port": 5432, "database_password": secrets.token_urlsafe(36)}
        return exercise(test, context, preflight_only=True)


def exercise(test, context, *, preflight_only=False, application_check=None):
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").is_dir(): raise ValueError("Use a disposable root Linux systemd runner")
    import pwd
    import grp
    backend = activate.Host(); account = pwd.getpwnam("nobody"); group = grp.getgrgid(account.pw_gid)
    entries = json.loads(backend.run(["/usr/sbin/ip", "-j", "-4", "address", "show"]))
    candidates = [a["local"] for row in entries for a in row.get("addr_info", []) if a.get("scope") == "global" and not a["local"].startswith("127.")]
    if not candidates: raise ValueError("The native fixture needs a local reviewed peer IPv4 address")
    address = sorted(set(candidates))[0]; member = "kcnative-fixture-" + uuid4().hex[:12]
    root = Path(tempfile.mkdtemp(prefix="platform-keycloak-native-", dir="/run")); root.chmod(0o755)
    unit = Path("/etc/systemd/system") / ("platform-" + member + ".service")
    data = Path("/var/lib") / ("platform-keycloak-" + member)
    reservations = []; original_inode = None; original_text = None; state_inode = None; contract = plan = journal = None
    report = {key: False for key in ("keycloak_host_prepared_activation_verified", "keycloak_host_original_resume_verified", "keycloak_host_kernel_listener_verified", "keycloak_host_oidc_discovery_verified", "keycloak_host_private_values_absent_from_journal", "keycloak_host_temporary_service_and_files_removed", "keycloak_host_physical_node_ha_verified")}
    def write(name, text, mode=0o644):
        path = root / name; path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        # mkdir's mode is filtered by the calling shell's umask. Native service
        # inputs need searchable host ancestors even when that umask is 077;
        # the credential files keep their separately selected private modes.
        for parent in [*reversed(path.parent.parents), path.parent]:
            if parent.is_relative_to(root): parent.chmod(0o755)
        path.write_text(text, encoding="utf-8", newline="\n"); path.chmod(mode); return path
    def inventory(home, *, data=False):
        rows = []
        for path in sorted(home.rglob("*")):
            if data and path.relative_to(home).parts[0] == "data": continue
            if path.is_symlink(): raise ValueError("Stage a materialized immutable release")
            if path.is_file(): rows.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(home).as_posix() + "\n")
        return "".join(rows)
    def worker(mode):
        result = run_bounded(["/usr/bin/python3", "-B", str(root / "scripts/native_fixture_worker.py"), str(root / "host-plan.json"), str(root / "bundle"), mode, str(root / "private/operation")],
            env=activate.CLEAN, text=True, timeout=240, output_max_bytes=256 * 1024, check=False)
        if result.returncode:
            try: diagnostic = json.loads(result.stdout)
            except ValueError: diagnostic = {"unclassified_worker_failure": True}
            print(json.dumps({"native_fixture_safe_failure": diagnostic}, sort_keys=True), flush=True)
            raise ValueError("The actual prepared host activation failed; private values suppressed")
        return json.loads(result.stdout)
    try:
        if unit.exists() or unit.is_symlink() or data.exists() or data.is_symlink(): raise ValueError("The native fixture scope already exists")
        for host, port in (() if preflight_only else (("127.0.0.1", 8443), ("127.0.0.1", 9000), (address, 7800), (address, 57800))):
            handle = socket.socket(); handle.bind((host, port)); reservations.append(handle)
        home, java = root / "release", root / "java"
        shutil.copytree(context["native_home"], home, symlinks=False, ignore=lambda parent, names: {"data"} if Path(parent) == context["native_home"] else set())
        (home / "data").mkdir(mode=0o755)
        shutil.copytree(context["java_home"], java, symlinks=False)
        for tree in (home, java):
            for path in [tree, *tree.rglob("*")]:
                info = path.stat(); os.chown(path, 0, 0); path.chmod(0o755 if path.is_dir() or info.st_mode & 0o111 else 0o644)
        # The root-owned staged source is byte-for-byte identical to this
        # checkout. The worker uses the real Host and no mocked prerequisites.
        for path in Path(__file__).parent.glob("*.py"):
            target = root / "scripts" / path.name; target.parent.mkdir(exist_ok=True); shutil.copyfile(path, target); target.chmod(0o644)
        write("scripts/native_fixture_worker.py", '''import json,sys,traceback
from pathlib import Path
import activate_keycloak_host as activation
try:
 plan=json.loads(Path(sys.argv[1]).read_text())
 result=activation.operate(Path(sys.argv[2]),plan,mode=sys.argv[3],output=Path(sys.argv[4]),wait_seconds=120)
 print(json.dumps(result))
except Exception as error:
 frames=[{"file":Path(f.filename).name,"line":f.lineno,"function":f.name} for f in traceback.extract_tb(error.__traceback__)]
 print(json.dumps({"exception_type":type(error).__name__,"locations":frames[-8:]}));raise SystemExit(1)
''')
        selected = fixture(); selected.update(hostname="localhost", public_port=8443, realm="platform-native-fixture", private_dir=str(root / "inputs"), native_home=str(home))
        selected["database"].update(host="127.0.0.1", port=context["database_port"], name="keycloak_fixture", username="keycloak_fixture")
        selected["members"][0].update(name=member, address=address)
        for source, destination in ((context["root"] / "tls/tls.crt", "inputs/tls/tls.crt"), (context["root"] / "tls/tls.key", "inputs/tls/tls.key"),
                                    (context["root"] / "ca.crt", "inputs/db-ca/ca.crt"), (context["root"] / "ca.crt", "inputs/health-ca.crt")):
            write(destination, source.read_text())
        key = root / "inputs/tls/tls.key"; key.chmod(0o640); os.chown(key, 0, account.pw_gid)
        write("inputs/database.env", "KC_DB_PASSWORD=" + context["database_password"] + "\n", 0o600)
        native_manifest = write("inputs/native-files.sha256", inventory(home, data=True))
        java_manifest = write("inputs/java-files.sha256", inventory(java))
        selected["native_manifest_sha256"] = hashlib.sha256(native_manifest.read_bytes()).hexdigest()
        files = renderer.render(selected, "raw")
        for name, content in files.items(): write("bundle/" + name, content)
        write("inputs/native-manifest.sha256", files["native-manifest.sha256"])
        contract = json.loads(files["keycloak-contract.json"])
        plan = {"schema_version": 1, "contract_sha256": hashlib.sha256((root / "bundle/keycloak-contract.json").read_bytes()).hexdigest(), "member": member,
            "machine_id_sha256": hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "service_uid": account.pw_uid, "service_gid": account.pw_gid, "bind_addresses": {"https": "127.0.0.1", "management": "127.0.0.1"},
            "files_sha256": {name: hashlib.sha256((root / "inputs" / name).read_bytes()).hexdigest() for name in activate.FILES}, "docker": None,
            "native": {"user": account.pw_name, "group": group.gr_name, "java_home": str(java), "java_manifest_sha256": hashlib.sha256(java_manifest.read_bytes()).hexdigest(), "systemctl_sha256": activate.file_hash(Path("/usr/bin/systemctl")), "max_runtime_seconds": 240}}
        write("host-plan.json", json.dumps(plan), 0o600)
        test.assertTrue(worker("check-host")["host_checked"])
        if preflight_only: return {"keycloak_host_preflight_verified": True}
        for handle in reservations: handle.close()
        reservations.clear()
        print("Disposable Keycloak native fixture: activating exact prepared distribution", flush=True)
        result = worker("activate"); test.assertTrue(result["original_member_active"]); test.assertTrue(result["tls_readiness_observed"])
        report["keycloak_host_prepared_activation_verified"] = True
        journal = json.loads((root / "private/operation/keycloak-host-proof.json").read_text())
        test.assertEqual(journal["toolset_sha256"], activate.tools())
        original = backend.inspect(contract, plan, journal, {})
        test.assertTrue(activate.owns_listener(original, "127.0.0.1", 9000)); test.assertTrue(activate.owns_listener(original, "127.0.0.1", 8443))
        report["keycloak_host_kernel_listener_verified"] = True
        before = journal["invocation"]; test.assertTrue(worker("resume")["read_only"])
        after = json.loads((root / "private/operation/keycloak-host-proof.json").read_text()); test.assertEqual(after["invocation"], before); test.assertEqual(after["handle"], journal["handle"])
        report["keycloak_host_original_resume_verified"] = True
        report["keycloak_host_artifacts_sha256"] = {"native_manifest": selected["native_manifest_sha256"], "java_manifest": plan["native"]["java_manifest_sha256"],
            "activation_plan": activate.fingerprint(plan), "activation_journal": hashlib.sha256((root / "private/operation/keycloak-host-proof.json").read_bytes()).hexdigest(), "host_toolset": after["toolset_sha256"]}
        connection = activate.DirectHTTPS("127.0.0.1", "localhost", 8443, ssl.create_default_context(cafile=str(root / "inputs/health-ca.crt")))
        try:
            connection.request("GET", "/realms/master/.well-known/openid-configuration"); response = connection.getresponse(); body = response.read(128 * 1024 + 1)
            test.assertEqual(response.status, 200); test.assertLessEqual(len(body), 128 * 1024); test.assertEqual(json.loads(body)["issuer"], "https://localhost:8443/realms/master")
        finally: connection.close()
        report["keycloak_host_oidc_discovery_verified"] = True
        if application_check is not None:
            report.update(application_acceptance(test, context, bundle=root / "bundle", selected=selected,
                contract_sha256=plan["contract_sha256"], application_check=application_check, name="native"))
            final = backend.inspect(contract, plan, after, {})
            test.assertTrue(backend.healthy(contract, plan, final))
            test.assertEqual(final["invocation"], before)
            report["keycloak_host_application_original_invocation_verified"] = True
    finally:
        for handle in reservations: handle.close()
        if unit.exists() or unit.is_symlink():
            info = unit.lstat()
            expected = activate.unit_text(contract, plan, json.loads((root / "private/operation/keycloak-host-proof.json").read_text())) if contract and plan else None
            if unit.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or unit.read_text() != expected: raise ValueError("Original native fixture unit changed; preserve handles")
            original_inode = (info.st_dev, info.st_ino); original_text = expected
            if data.exists():
                if data.is_symlink() or data.parent != Path("/var/lib") or data.name != "platform-keycloak-" + member: raise ValueError("Unexpected native fixture state")
                state_inode = (data.stat().st_dev, data.stat().st_ino)
            backend.run(["/usr/bin/systemctl", "stop", unit.name])
            backend.run(["/usr/bin/systemctl", "reset-failed", unit.name])
            logs = backend.run(["/usr/bin/journalctl", "--no-pager", "-o", "cat", "-u", unit.name, "--lines=2000"])
            if context["database_password"] in logs or "prvKey" in logs or ("-----BEGIN " + "PRIVATE KEY-----") in logs: raise ValueError("A generated private value entered the native fixture journal")
            report["keycloak_host_private_values_absent_from_journal"] = True
            if (unit.stat().st_dev, unit.stat().st_ino) != original_inode or unit.read_text() != original_text: raise ValueError("Native fixture unit changed before cleanup")
            unit.unlink(); backend.reload()
        if data.exists():
            if data.is_symlink() or state_inode != (data.stat().st_dev, data.stat().st_ino): raise ValueError("Native fixture state identity changed")
            shutil.rmtree(data)
        if root.resolve().parent != Path("/run") or not root.name.startswith("platform-keycloak-native-"): raise ValueError("Unexpected native fixture cleanup path")
        shutil.rmtree(root)
    report["keycloak_host_temporary_service_and_files_removed"] = True
    return report
