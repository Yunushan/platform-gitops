#!/usr/bin/env python3
"""Disposable real OpenBao Raft test inside an already isolated Linux netns."""

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from unittest import mock

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import render_openbao_ha as renderer
import openbao_snapshot as backup
import verify_openbao_runtime as verifier


ARCHIVE = "openbao_2.7.2_linux_amd64.tar.gz"
ARCHIVE_SHA256 = "582b1f3c2fdc5afd443b2d57a225ef654de75adea609fdb66731fd582a9cbc96"
ARCHIVE_BYTES = 74907645
CLEAN = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": "/nonexistent"}


def sources():
    directory = Path(__file__).resolve().parent
    paths = {Path(m.__file__).resolve() for m in list(sys.modules.values())
             if getattr(m, "__file__", None) and Path(m.__file__).suffix == ".py" and Path(m.__file__).resolve().parent == directory}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def exercise(test):
    if sys.platform != "linux" or os.geteuid() != 0 or os.environ.get("OPENBAO_TEST_RUNTIME") != "1":
        raise ValueError("Select the explicit disposable root Linux fixture")
    if Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino:
        raise ValueError("Run only inside a new isolated network namespace")
    def command(args):
        result = run_bounded(args, text=True, env=CLEAN, timeout=30, output_max_bytes=256 * 1024, check=False)
        if result.returncode: raise ValueError("Fixture setup command failed; private diagnostics suppressed")
        return result.stdout
    interfaces = json.loads(command(["/usr/sbin/ip", "-j", "address", "show"]))
    if len(interfaces) != 1 or interfaces[0]["ifname"] != "lo" or interfaces[0].get("addr_info"):
        raise ValueError("Use a fresh namespace containing only an unconfigured loopback device")
    inputs = Path(os.environ["OPENBAO_TEST_ARTIFACTS"]).resolve(strict=True)
    archive = inputs / ARCHIVE
    if archive.is_symlink() or archive.stat().st_size != ARCHIVE_BYTES: raise ValueError("Use the exact bounded cached release")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ARCHIVE_SHA256: raise ValueError("Release archive fingerprint differs")
    committed_sources = sources()
    root = Path(tempfile.mkdtemp(prefix="platform-openbao-runtime-", dir="/tmp")); root.chmod(0o755)
    identity = (root.stat().st_dev, root.stat().st_ino)
    account = pwd.getpwnam("nobody"); uid, gid = account.pw_uid, account.pw_gid
    processes = {}; logs = {}; expected_stopped = set(); private_values = []; report = {}; deadline = time.monotonic() + 240
    def write(name, content, *, mode=0o644, owner=False):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        for parent in [*reversed(path.parent.parents), path.parent]:
            if parent.is_relative_to(root): parent.chmod(0o755)
        path.write_text(content, encoding="utf-8", newline="\n"); path.chmod(mode)
        if owner: os.chown(path, uid, gid)
        return path
    def wait(check, *, seconds=60):
        until = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            for child in processes.values():
                if child.poll() not in {None, 0} and child.pid not in expected_stopped:
                    raise ValueError("An original OpenBao fixture process exited before readiness")
            try:
                result = check()
                if result: return result
            except ssl.SSLCertVerificationError:
                raise ValueError("Generated fixture PKI failed strict certificate verification") from None
            except (OSError, TimeoutError): pass
            time.sleep(0.3)
        raise TimeoutError("OpenBao fixture observation timed out")
    def start(member):
        name = member["name"]
        if name in processes and processes[name].poll() is None: raise ValueError("Do not replace an active original fixture process")
        log = root / (name + "-" + str(len(logs)) + ".log")
        with log.open("xb") as output:
            child = subprocess.Popen([str(root / "bao"), "server", "-config=" + str(root / (name + ".json"))],
                cwd=root, env=CLEAN, stdout=output, stderr=subprocess.STDOUT, user=uid, group=gid, extra_groups=[], start_new_session=True)
        processes[name] = child; logs[log] = child
        return child
    try:
        command(["/usr/sbin/ip", "link", "set", "lo", "up"])
        plan = json.loads((Path(__file__).resolve().parents[1] / "examples/openbao-ha.json").read_text())
        with tarfile.open(archive) as package:
            members = package.getmembers(); selected = [m for m in members if m.name == "bao"]
            if len(members) != 4 or len(selected) != 1 or not selected[0].isfile() or selected[0].size != 181848661:
                raise ValueError("Unexpected pinned archive layout")
            with package.extractfile(selected[0]) as stream, (root / "bao").open("xb") as output:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
        (root / "bao").chmod(0o755)
        with (root / "bao").open("rb") as stream: binary_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        plan.update(binary=str(root / "bao"), binary_sha256=binary_sha, private_dir=str(root / "inputs"), audit_dir=str(root / "audit"))
        command(["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-subj", "/CN=fixture-openbao-ca", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.crt")])
        (root / "ca.key").chmod(0o600)
        (root / "audit").mkdir(); os.chown(root / "audit", uid, gid); (root / "audit").chmod(0o700)
        for member in plan["members"]:
            name = member["name"]; member["data_dir"] = str(root / ("data-" + name))
            data = Path(member["data_dir"]); data.mkdir(); os.chown(data, uid, gid); data.chmod(0o700)
            command(["/usr/sbin/ip", "address", "add", member["address"] + "/32", "dev", "lo"])
            tls = root / "inputs" / name / "tls"; tls.mkdir(parents=True)
            for parent in [*reversed(tls.parents), tls]:
                if parent.is_relative_to(root): parent.chmod(0o755)
            extension = write(name + "-extensions.cnf", "subjectAltName=IP:" + member["address"] + "\nextendedKeyUsage=serverAuth,clientAuth\n"
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n")
            command(["/usr/bin/openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=" + name,
                "-keyout", str(tls / "tls.key"), "-out", str(root / (name + ".csr"))])
            command(["/usr/bin/openssl", "x509", "-req", "-in", str(root / (name + ".csr")), "-CA", str(root / "ca.crt"), "-CAkey", str(root / "ca.key"),
                "-CAcreateserial", "-days", "1", "-extfile", str(extension), "-out", str(tls / "tls.crt")])
            command(["/usr/bin/openssl", "verify", "-x509_strict", "-verify_ip", member["address"], "-CAfile", str(root / "ca.crt"), str(tls / "tls.crt")])
            shutil.copyfile(root / "ca.crt", tls / "ca.crt")
            for name in ("tls.crt", "ca.crt"): (tls / name).chmod(0o644)
            os.chown(tls / "tls.key", 0, gid); (tls / "tls.key").chmod(0o440)
        files = renderer.render(plan, "raw")
        for name, content in files.items(): write(name, content)
        context = ssl.create_default_context(cafile=str(root / "ca.crt"))
        def call(member, path, *, bearer=None, method="GET", data=None, raw_data=None, binary=False, seconds=15):
            headers = {}
            if bearer: headers["X-Vault-Token"] = bearer
            body = None
            if data is not None: body = json.dumps(data).encode(); headers["Content-Type"] = "application/json"
            if raw_data is not None:
                if data is not None: raise ValueError("Select one fixture request encoding")
                body = raw_data; headers["Content-Type"] = "application/octet-stream"
            remaining = min(seconds, deadline - time.monotonic())
            if remaining <= 0: raise TimeoutError("Fixture operation deadline expired")
            connection = http.client.HTTPSConnection(member["address"], 8200, timeout=remaining, context=context)
            try:
                connection.request(method, "/v1/" + path, body=body, headers=headers)
                response = connection.getresponse(); raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024: raise ValueError("API response exceeds fixture bound")
                return response.status, raw if binary else json.loads(raw) if raw else {}
            finally: connection.close()
        def require(member, path, *, expected=200, **kwargs):
            status, value = call(member, path, **kwargs)
            if status != expected:
                errors = value.get("errors", []) if isinstance(value, dict) else []
                messages = []
                for error in errors[:3]:
                    if not isinstance(error, str): continue
                    for private in private_values: error = error.replace(private, "<redacted>")
                    messages.append(error[:400])
                print(json.dumps({"openbao_fixture_api_error": {"status": status, "path": path, "errors": messages}}), flush=True)
                raise ValueError("Fixture API request failed with HTTP " + str(status))
            return value
        def healthy(member):
            status, value = call(member, "sys/health?standbyok=true")
            return value if status == 200 and value.get("initialized") is True and value.get("sealed") is False else None
        first = plan["members"][0]
        start(first)
        wait(lambda: call(first, "sys/health")[0] == 501)
        untrusted = http.client.HTTPSConnection(first["address"], 8200, timeout=5, context=ssl.create_default_context())
        try:
            try: untrusted.request("GET", "/v1/sys/health")
            except ssl.SSLCertVerificationError: report["untrusted_ca_rejected"] = True
            else: raise ValueError("The fixture CA was unexpectedly trusted by the host's default roots")
        finally: untrusted.close()
        print("Disposable OpenBao fixture: initializing one fresh member and joining two followers", flush=True)
        initialized = require(first, "sys/init", method="POST", seconds=60, data={"secret_shares": 3, "secret_threshold": 2})
        keys = initialized["keys_base64"]; root_token = initialized["root_token"]
        if len(keys) != 3 or not root_token: raise ValueError("Incomplete private fixture initialization")
        private_values.extend([*keys, *initialized.get("keys", []), root_token])
        for key in keys[:2]: require(first, "sys/unseal", method="POST", data={"key": key})
        first_health = wait(lambda: healthy(first)); cluster_id = first_health["cluster_id"]
        for member in plan["members"][1:]:
            start(member); wait(lambda: call(member, "sys/health")[0] in {501, 503})
            # Retry-join is real configuration; explicit local join acknowledges
            # the same challenge before supplying this fixture's Shamir shares.
            joined = require(member, "sys/storage/raft/join", method="POST", data={"leader_api_addr": renderer.origin(plan, first, "raw", 8200),
                "leader_ca_cert": (root / "ca.crt").read_text()})
            if joined.get("joined") is not True: raise ValueError("Follower join was not acknowledged")
            for key in keys[:2]: require(member, "sys/unseal", method="POST", data={"key": key})
            state = wait(lambda: healthy(member))
            if state["cluster_id"] != cluster_id: raise ValueError("Follower has a different cluster identity")
        report.update(tls_three_member_startup_verified=True, shamir_init_unseal_verified=True)
        def voters(member, token):
            rows = require(member, "sys/storage/raft/configuration", bearer=token)["data"]["config"]["servers"]
            return rows if len(rows) == 3 and {r["node_id"] for r in rows} == {m["name"] for m in plan["members"]} and all(r["voter"] is True for r in rows) else None
        wait(lambda: voters(first, root_token)); report["three_voters_verified"] = True
        audits = require(first, "sys/audit", bearer=root_token)["data"]
        if "platform-local/" not in audits: raise ValueError("Configured audit device is not active")
        require(first, "sys/mounts/fixture", bearer=root_token, method="POST", data={"type": "kv", "options": {"version": "2"}}, expected=204)
        value = secrets.token_urlsafe(36); private_values.append(value)
        require(first, "fixture/data/allowed", bearer=root_token, method="POST", data={"data": {"value": value}})
        reader_policy = 'path "fixture/data/allowed" { capabilities = ["read"] }\npath "sys/capabilities-self" { capabilities = ["update"] }'
        operator_policy = '\n'.join('path "' + path + '" { capabilities = [' + rights + '] }' for path, rights in (
            ("sys/storage/raft/configuration", '"read"'), ("sys/storage/raft/snapshot", '"read"'),
            ("fixture/data/*", '"create", "read", "update"'), ("sys/audit", '"read"')))
        backup_policy = '\n'.join('path "' + path + '" { capabilities = ["read"] }' for path in backup.READ_PATHS)
        backup_policy += '\npath "sys/capabilities-self" { capabilities = ["update"] }'
        observer_policy = '\n'.join('path "' + path + '" { capabilities = [' + ', '.join('"' + c + '"' for c in sorted(rights)) + '] }' for path, rights in verifier.OBSERVER_READS.items())
        observer_policy += '\npath "sys/capabilities-self" { capabilities = ["update"] }'
        for name, policy in (("fixture-reader", reader_policy), ("fixture-operator", operator_policy), ("fixture-backup", backup_policy), ("fixture-observer", observer_policy)):
            require(first, "sys/policies/acl/" + name, bearer=root_token, method="PUT", data={"policy": policy}, expected=204)
        def token_for(policy):
            result = require(first, "auth/token/create", bearer=root_token, method="POST", data={"policies": [policy], "no_parent": True,
                "no_default_policy": True, "ttl": "10m", "renewable": False})
            token = result["auth"]["client_token"]; private_values.append(token)
            observed = require(first, "auth/token/lookup", bearer=root_token, method="POST", data={"token": token})["data"]
            if observed.get("orphan") is not True or set(observed.get("policies", [])) != {policy}:
                raise ValueError("The independent restricted fixture token has unexpected ownership or policies")
            return token
        reader = token_for("fixture-reader"); operator = token_for("fixture-operator"); snapshot_reader = token_for("fixture-backup"); observer = token_for("fixture-observer")
        def backend_ready(member):
            status, result = call(member, "fixture/data/allowed", bearer=reader)
            if status == 400 and result.get("errors") == ["Waiting for the primary to upgrade from non-versioned to versioned data. This backend will be unavailable for a brief period and will resume service when the primary is finished."]:
                return False
            if status != 200: raise ValueError("Restricted fixture read failed before backend readiness")
            if result.get("data", {}).get("data", {}).get("value") != value: raise ValueError("The replicated fixture value differs")
            return True
        def permitted(member):
            result = require(member, "fixture/data/allowed", bearer=reader)
            if result["data"]["data"].get("value") != value: raise ValueError("The replicated fixture value differs")
            for path, method, body in (("fixture/data/forbidden", "GET", None), ("fixture/data/allowed", "POST", {"data": {"value": "denied"}}),
                                       ("sys/storage/raft/configuration", "GET", None)):
                require(member, path, bearer=reader, method=method, data=body, expected=403)
            return True
        for member in plan["members"]:
            wait(lambda: backend_ready(member)); permitted(member)
        report["restricted_policy_verified"] = True
        require(first, "auth/token/revoke-self", bearer=root_token, method="POST", expected=204)
        require(first, "fixture/data/allowed", bearer=root_token, expected=403)
        report["initial_root_revocation_verified"] = True
        leader = next(row["node_id"] for row in voters(first, operator) if row.get("leader") is True)
        original_leader = next(m for m in plan["members"] if m["name"] == leader)
        print("Disposable OpenBao fixture: stopping the leader and verifying survivor access", flush=True)
        child = processes[leader]; expected_stopped.add(child.pid); child.kill(); child.wait(timeout=10)
        survivors = [m for m in plan["members"] if m["name"] != leader]
        def new_leader():
            for member in survivors:
                status, state = call(member, "sys/health")
                if status == 200 and state.get("standby") is False and state.get("cluster_id") == cluster_id: return member
            return None
        active = wait(new_leader)
        for member in survivors: wait(lambda: permitted(member))
        require(active, "fixture/data/after-failover", bearer=operator, method="POST", data={"data": {"value": "after-leader-loss"}})
        report["leader_process_loss_verified"] = True
        print("Disposable OpenBao fixture: restarting the original data member and reapplying its unseal shares", flush=True)
        restarted = start(original_leader)
        if restarted.pid == child.pid: raise ValueError("Expected a new original-data process invocation")
        wait(lambda: call(original_leader, "sys/health")[0] == 503)
        for key in keys[:2]: require(original_leader, "sys/unseal", method="POST", data={"key": key})
        state = wait(lambda: healthy(original_leader))
        if state["cluster_id"] != cluster_id: raise ValueError("Restart changed the original cluster identity")
        wait(lambda: voters(active, operator)); wait(lambda: permitted(original_leader))
        observation_plan = {"schema_version": 1, "cluster": {"id": cluster_id, "version": plan["version"],
            "members": [{"name": m["name"], "api_url": renderer.origin(plan, m, "raw", 8200), "raft_address": m["address"] + ":8201"} for m in plan["members"]],
            "ca_file": str(root / "ca.crt"), "ca_sha256": hashlib.sha256((root / "ca.crt").read_bytes()).hexdigest()},
            "observer": {"token_env": "OPENBAO_FIXTURE_OBSERVER"}, "probe": {"token_env": "OPENBAO_FIXTURE_READER", "path": "fixture/data/allowed",
                "value_sha256": hashlib.sha256(value.encode()).hexdigest(), "denied_paths": ["fixture/data/forbidden"]},
            "audit_devices": {"platform-local/": {"type": "file", "file_path": str(root / "audit/audit.json")}}, "request_seconds": 5}
        with mock.patch.dict(os.environ, {"OPENBAO_FIXTURE_OBSERVER": observer, "OPENBAO_FIXTURE_READER": reader}):
            observed = verifier.operate(observation_plan, live=True)
            if observed["live_observations_verified"] is not True or any(observed[k] is not False for k in verifier.FALSE_FLAGS):
                raise ValueError("The production observer made an invalid acceptance claim")
        report.update(read_only_runtime_observer_verified=True, runtime_observer_toolset_sha256=observed["toolset_sha256"])
        report["sealed_restart_recovery_verified"] = True
        print("Disposable OpenBao fixture: capturing through the scoped production backup helper", flush=True)
        backup_plan = {"schema_version": 1, "source": {"url": renderer.origin(plan, active, "raw", 8200), "cluster_id": cluster_id, "version": plan["version"],
            "raft": {m["name"]: m["address"] + ":8201" for m in plan["members"]}, "ca_file": str(root / "ca.crt"),
            "ca_sha256": hashlib.sha256((root / "ca.crt").read_bytes()).hexdigest(), "token_env": "OPENBAO_FIXTURE_SNAPSHOT_TOKEN"},
            "limits": {"request_seconds": 30, "transfer_seconds": 60, "max_bytes": 8 * 1024 * 1024}}
        with mock.patch.dict(os.environ, {"OPENBAO_FIXTURE_SNAPSHOT_TOKEN": snapshot_reader}):
            captured = backup.operate(backup_plan, mode="capture", output=root / "backup")
        test.assertTrue(captured["captured"])
        test.assertTrue(backup.operate(backup_plan, mode="verify", output=root / "backup")["verified"])
        snapshot = read_bounded_bytes(root / "backup/snapshot.tar.gz", max_bytes=8 * 1024 * 1024)
        report.update(snapshot_capture_verified=True, snapshot_sha256=captured["snapshot_sha256"], snapshot_bytes=captured["snapshot_bytes"],
            guarded_snapshot_capture_verified=True, snapshot_internal_checksums_verified=True, snapshot_gzip_crc_verified=True)
        print("Disposable OpenBao fixture: stopping every source process before isolated restore", flush=True)
        for member in plan["members"]:
            child = processes[member["name"]]; expected_stopped.add(child.pid); child.terminate(); child.wait(timeout=10)
        if any(processes[m["name"]].poll() is None for m in plan["members"]): raise ValueError("A source process is still running during restore")
        target = {"name": "bao-restore", "address": first["address"]}
        restored_data = root / "restored-data"; restored_data.mkdir(); os.chown(restored_data, uid, gid); restored_data.chmod(0o700)
        if any(restored_data.iterdir()) or str(restored_data) in {m["data_dir"] for m in plan["members"]}: raise ValueError("Restore needs a new empty data scope")
        restore_config = json.loads(files[first["name"] + ".json"])
        restore_config["storage"]["raft"].update(path=str(restored_data), node_id=target["name"])
        restore_config["storage"]["raft"].pop("retry_join")
        write(target["name"] + ".json", json.dumps(restore_config))
        start(target); wait(lambda: call(target, "sys/health")[0] == 501)
        fresh = require(target, "sys/init", method="POST", seconds=60, data={"secret_shares": 3, "secret_threshold": 2})
        fresh_keys = fresh["keys_base64"]; fresh_root = fresh["root_token"]
        private_values.extend([*fresh_keys, *fresh.get("keys", []), fresh_root])
        for key in fresh_keys[:2]: require(target, "sys/unseal", method="POST", data={"key": key})
        fresh_health = wait(lambda: healthy(target))
        if fresh_health["cluster_id"] == cluster_id: raise ValueError("Restore target is not an independent initialized cluster")
        print("Disposable OpenBao fixture: refusing different-key normal restore, then restoring only its fresh target", flush=True)
        rejected = require(target, "sys/storage/raft/snapshot", bearer=fresh_root, method="POST", raw_data=snapshot, seconds=60, expected=400)
        refusal = " ".join(str(v) for v in rejected.get("errors", [])).casefold()
        if "seal" not in refusal or "hash" not in refusal: raise ValueError("Normal restore did not fail at the expected seal-check boundary")
        if require(target, "sys/health")["cluster_id"] != fresh_health["cluster_id"]: raise ValueError("Rejected restore changed the target cluster")
        require(target, "sys/storage/raft/snapshot-force", bearer=fresh_root, method="POST", raw_data=snapshot, seconds=60, expected=204)
        wait(lambda: call(target, "sys/health")[0] == 503)
        for key in keys[:2]: require(target, "sys/unseal", method="POST", data={"key": key})
        restored_health = wait(lambda: healthy(target))
        if restored_health["cluster_id"] != cluster_id: raise ValueError("Restored cluster identity differs from the snapshot source")
        wait(lambda: backend_ready(target)); permitted(target)
        require(target, "fixture/data/allowed", bearer=fresh_root, expected=403)
        require(target, "fixture/data/allowed", bearer=root_token, expected=403)
        after = require(target, "fixture/data/after-failover", bearer=operator)
        if after.get("data", {}).get("data", {}).get("value") != "after-leader-loss": raise ValueError("The independent snapshot witness was not restored")
        restored_members = require(target, "sys/storage/raft/configuration", bearer=operator)["data"]["config"]["servers"]
        if (len(restored_members) != 1 or restored_members[0].get("node_id") != target["name"] or restored_members[0].get("voter") is not True
                or restored_members[0].get("leader") is not True): raise ValueError("Restore did not preserve the fresh target's independent single-node Raft configuration")
        require(target, "fixture/data/after-restore", bearer=operator, method="POST", data={"data": {"value": "fresh-target-commit"}})
        committed = require(target, "fixture/data/after-restore", bearer=operator)
        if committed.get("data", {}).get("data", {}).get("value") != "fresh-target-commit": raise ValueError("Recovered target could not commit and read a new write")
        test.assertTrue(backup.operate(backup_plan, mode="verify", output=root / "backup")["verified"])
        report.update(source_processes_stopped_during_restore_verified=True, fresh_target_snapshot_restore_verified=True,
            different_key_normal_restore_rejected=True, original_unseal_shares_restore_verified=True, restored_data_and_rbac_verified=True,
            offline_backup_verification_verified=True, restored_writes_committed_verified=True, restored_single_voter_identity_verified=True,
            snapshot_capture_toolset_sha256=backup.toolset())
        audit = read_bounded_bytes(root / "audit/audit.json", max_bytes=16 * 1024 * 1024)
        if b'hmac-sha256:' not in audit or b'fixture/data/allowed' not in audit: raise ValueError("No HMAC request audit was observed")
        for private in private_values:
            if private.encode() in audit: raise ValueError("A private fixture value appeared in the audit file")
        report.update(audit_hmac_verified=True, cluster_id_sha256=hashlib.sha256(cluster_id.encode()).hexdigest())
        if sources() != committed_sources: raise ValueError("Fixture sources changed during execution")
        report.update(version=plan["version"], runtime="three-voter-source-and-single-node-restore-isolated-single-host-netns", source_sha256=committed_sources,
            archive_sha256=ARCHIVE_SHA256, binary_sha256=binary_sha,
            configuration_sha256={m["name"]: hashlib.sha256(files[m["name"] + ".json"].encode()).hexdigest() for m in plan["members"]})
    finally:
        for process in processes.values():
            if process.poll() is None: process.terminate()
        for process in processes.values():
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
        for path in logs:
            data = read_bounded_bytes(path, max_bytes=16 * 1024 * 1024)
            if any(value.encode() in data for value in private_values): raise ValueError("A private fixture value appeared in server logs")
            if any(process.poll() not in {None, 0, -15, -9} for process in processes.values()):
                text = data.decode(errors="replace").casefold()
                print(json.dumps({"openbao_fixture_safe_log_signals": {term: term in text for term in (
                    "permission denied", "failed to parse", "error loading", "invalid", "audit", "certificate", "unknown", "mlock", "address already in use")}}), flush=True)
        if (root.is_symlink() or root.parent != Path("/tmp") or not root.name.startswith("platform-openbao-runtime-")
                or (root.stat().st_dev, root.stat().st_ino) != identity or root.stat().st_uid != 0):
            raise ValueError("Original fixture cleanup scope changed")
        shutil.rmtree(root)
    report.update(temporary_processes_and_data_removed=True, private_values_absent_from_server_logs=True, physical_node_loss_verified=False, off_platform_restore_verified=False,
        ha_verified=False, full_stack_verified=False, publisher_signature_verified=False)
    output = os.environ.get("OPENBAO_TEST_REPORT")
    if output:
        path = Path(output).absolute(); private = Path(__file__).resolve().parents[1] / "private"
        if path.resolve() != path or not path.is_relative_to(private.resolve()) or not path.parent.is_dir():
            raise ValueError("Use a new receipt inside existing private validation storage")
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            json.dump(report, stream, indent=2, sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    return report
