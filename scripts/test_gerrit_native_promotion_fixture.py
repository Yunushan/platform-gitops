#!/usr/bin/env python3
"""Invoke the packaged promotion CLI in an owned isolated non-root service."""

import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded


def service_state(name):
    result = run_bounded(["systemctl", "show", name, "--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup"],
        text=True, timeout=10, output_max_bytes=65536, check=False)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def process_identity(state):
    pid = int(state.get("MainPID", "0"))
    if not pid or not state.get("InvocationID") or not state.get("ControlGroup"): return None
    proc = Path("/proc") / str(pid)
    try:
        ticks = int((proc / "stat").read_text().rsplit(") ", 1)[1].split()[19])
        status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
        executable = (proc / "exe").stat(); expected = Path("/usr/bin/python3").stat()
        if (executable.st_dev, executable.st_ino) != (expected.st_dev, expected.st_ino) or int(status["Uid"].split()[0]) == 0: return None
        return {"pid": pid, "start_ticks": ticks, "python_executable_matches": True, "invocation_id": state["InvocationID"], "control_group": state["ControlGroup"],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "uid": [int(v) for v in status["Uid"].split()],
            "no_new_privileges": status["NoNewPrivs"].strip(), "effective_capabilities": status["CapEff"].strip(),
            "network_namespace_matches": (proc / "ns/net").stat().st_ino == Path("/proc/self/ns/net").stat().st_ino}
    except (FileNotFoundError, ProcessLookupError): return None


def execute(test, context, root, bundle, digest, inputs, contract, plan, ci, cd, run, *, apply=False, expected_code=0, excluded_uids=(), artifact=False, signature_inputs=None):
    if os.geteuid() != 0 or root.is_symlink() or root.parent != Path("/var/tmp") or not root.name.startswith("platform-gerrit-review-"):
        raise ValueError("Require the original private Gerrit worker scope")
    if bundle.parent != root or inputs.parent != root: raise ValueError("Require the prepared native promotion package")
    if type(artifact) is not bool or plan.get("schema_version") not in ((2, 3) if artifact else (1,)):
        raise ValueError("Select the matching explicit native promotion variant")
    private = root / "native-promoter-inputs"; private.mkdir(mode=0o700, exist_ok=True)
    for name, content in (("api-token", (inputs / "api-token").read_bytes()), ("token", (inputs / "token").read_bytes()),
            ("ca.crt", (inputs / "ca.crt").read_bytes()), ("ci-kubeconfig", Path(ci.prefix[2]).read_bytes()), ("cd-kubeconfig", Path(cd.prefix[2]).read_bytes())):
        path = private / name; path.write_bytes(content); path.chmod(0o400)
    signature_bind = ""
    signature_credentials = []
    if plan.get("schema_version") == 3:
        import hashlib
        if not isinstance(signature_inputs, dict) or set(signature_inputs) != set(plan["signature_policy"]):
            raise ValueError("Require reviewed signature input sources for the signed variant")
        for key, path in signature_inputs.items():
            path = Path(path)
            with path.open("rb") as incoming: sha = hashlib.file_digest(incoming, "sha256").hexdigest()
            selected = plan["signature_policy"][key]
            if path.is_symlink() or sha != selected["sha256"]: raise ValueError("Original signature input changed")
            if key == "cosign_binary":
                if selected["path"] != "/usr/local/bin/cosign": raise ValueError("Use the explicit verifier tool mount")
                signature_bind = " " + str(path) + ":/usr/local/bin/cosign"
            else:
                name = "signature-" + key
                if selected["path"] != "/run/platform/gerrit-promoter/" + name: raise ValueError("Use the isolated signature credential mount")
                target = private / name; target.write_bytes(path.read_bytes()); target.chmod(0o400)
                signature_credentials.append(name)
    elif signature_inputs is not None:
        raise ValueError("Legacy promotion cannot request signature credentials")
    outputs = root / "native-promoter-journals"; outputs.mkdir(mode=0o700, exist_ok=True)
    output = outputs / uuid4().hex; output.mkdir(mode=0o700)
    # The only writable host bind is inside the original private 0700 Gerrit
    # scope. The service can create a new 0700 journal there; it cannot reach
    # that host path through its parent or access another service's journal.
    output.chmod(0o1733)
    for path in (bundle, *bundle.iterdir()):
        if path.is_symlink() or path != bundle and not path.is_file(): raise ValueError("Unexpected promotion package entry")
        os.chown(path, 0, 0); path.chmod(0o755 if path.is_dir() else 0o444)
    name = "platform-gerrit-promoter-" + uuid4().hex + ".service"
    user = "p-gp-" + uuid4().hex[:12]
    proof = "proof-" + uuid4().hex
    base = "/opt/platform/gerrit-promoter"
    program = "gerrit_artifact_promotion.py" if artifact else "gerrit_gitops_handoff.py"
    runtime_limit = plan["timeout_seconds"] + 60 if artifact else 180
    arguments = ["/usr/bin/python3", "-B", base + "/" + program, "--bundle-dir", base, "--contract-sha256", digest,
        "--pipeline-run", run, "--output-dir", "/opt/platform/private/" + proof, "--apply" if apply else "--check-server"]
    properties = ["Type=exec", "DynamicUser=yes", "User=" + user, "Group=" + user, "UMask=0077", "NoNewPrivileges=yes",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes", "RestrictSUIDSGID=yes", "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "MemoryMax=512M", "TasksMax=64", "RuntimeMaxSec=" + str(runtime_limit), "KillMode=control-group", "Restart=no",
        "NetworkNamespacePath=/proc/" + str(os.getpid()) + "/ns/net", "TemporaryFileSystem=/opt:ro /run:ro /usr/local/bin:ro",
        "BindReadOnlyPaths=" + str(bundle) + ":" + base + " /run/credentials/" + name + ":/run/platform/gerrit-promoter " + context["client"].prefix[0] + ":/usr/local/bin/kubectl" + signature_bind,
        "BindPaths=" + str(output) + ":/opt/platform/private",
        *["LoadCredential=" + key + ":" + str(private / key) for key in ("api-token", "token", "ca.crt", "ci-kubeconfig", "cd-kubeconfig", *signature_credentials)]]
    environment = [contract["plan"]["management_target"]["kubeconfig_env"] + "=/run/platform/gerrit-promoter/ci-kubeconfig",
        plan["cd"]["management_target"]["kubeconfig_env"] + "=/run/platform/gerrit-promoter/cd-kubeconfig"]
    description = "Owned synthetic Gerrit promotion " + digest
    stop = threading.Event(); observation = {}; observation_errors = []
    def observe():
        while not stop.is_set():
            try:
                current = service_state(name)
                if observation and current.get("InvocationID") and current["InvocationID"] != observation["invocation_id"]:
                    observation_errors.append("original-native-unit-replaced"); return
                if not observation:
                    identity = process_identity(current)
                    if identity is not None: observation.update(identity)
            except (OSError, ValueError, TimeoutError):
                # A transient observation failure is not a terminal unit state.
                pass
            stop.wait(0.1)
    monitor = threading.Thread(target=observe, name="owned-native-promotion-observer") if artifact else None
    if monitor: monitor.start()
    def native_evidence():
        if not artifact: return {}
        test.assertFalse(observation_errors); test.assertTrue(observation)
        test.assertEqual(observation["uid"], [uid] * 4)
        test.assertEqual(observation["no_new_privileges"], "1"); test.assertEqual(int(observation["effective_capabilities"], 16), 0)
        test.assertIs(observation["network_namespace_matches"], True)
        return {"original_process": observation}
    try:
        result = run_bounded(["systemd-run", "--wait", "--pipe", "--collect", "--quiet", "--unit=" + name, "--description=" + description,
            *["--property=" + value for value in properties], *["--setenv=" + value for value in environment], *arguments],
            text=True, timeout=runtime_limit + 30, output_max_bytes=256 * 1024, check=False)
        secrets = [(private / key).read_bytes().strip() for key in ("api-token", "token")]
        if signature_credentials: secrets.append(json.loads((private / "signature-credential").read_bytes())["token"].encode())
        for key in ("ci-kubeconfig", "cd-kubeconfig"):
            secrets.extend(u["user"]["token"].encode() for u in json.loads((private / key).read_bytes())["users"])
        test.assertFalse(any(value.decode() in result.stdout + result.stderr for value in secrets))
        if result.returncode != expected_code:
            prefix = "Gerrit artifact promotion" if artifact else "Gerrit GitOps handoff"
            codes = re.findall(prefix + r" stopped: ([a-z0-9-]{1,100})", result.stderr)
            safe = {}
            path = output / proof / "handoff-proof.json"
            if path.is_file():
                failed = json.loads(read_bounded_bytes(path, max_bytes=256 * 1024))
                safe = {key: value for key in ("phase", "failed_at", "error_code") if isinstance(value := failed.get(key), str) and re.fullmatch(r"[a-z0-9-]{1,100}", value)}
                diagnostic = failed.get("failure_diagnostic")
                if isinstance(diagnostic, dict):
                    safe["failure_diagnostic"] = diagnostic
            failure = {"native_promotion_exit_code": result.returncode, "apply": apply,
                       "safe_failure_codes": codes, "safe_journal_phase": safe}
            print(json.dumps(failure), flush=True)
            if artifact and os.environ.get("K3S_VM_TEST_REPORT"):
                from test_gerrit_artifact_promotion_worker_fixture import retain
                retain("native-promotion-failure.json", failure)
            raise AssertionError("Packaged native promotion returned an unexpected exit code")
        journal_file = output / proof / "handoff-proof.json"
        journal = json.loads(read_bounded_bytes(journal_file, max_bytes=256 * 1024))
        uid = journal_file.stat().st_uid
        test.assertNotEqual(uid, 0); test.assertNotIn(uid, excluded_uids)
        test.assertEqual(journal_file.stat().st_mode & 0o777, 0o600)
        test.assertEqual(journal["handoff_contract_sha256"], digest)
        test.assertIs(journal["full_stack_verified"], False)
        if expected_code:
            test.assertEqual(journal["phase"], "failed")
            return {"unit": name, "uid": uid, "journal": journal, "result": None, **native_evidence()}
        value = json.loads(result.stdout)
        test.assertEqual(journal["phase"], "controller-verified" if apply else "git-preflight-verified")
        if apply:
            test.assertIs(journal["controller_verified"], True)
            if artifact:
                test.assertNotEqual(journal["promoted_revision"], journal["source_revision"])
                test.assertIs(journal["deployment_rollout_verified"], True)
                test.assertEqual(value["artifact_image"], journal["artifact"]["image"])
            else: test.assertEqual(journal["promoted_revision"], journal["source_revision"])
        return {"unit": name, "uid": uid, "journal": journal, "result": value, **native_evidence()}
    finally:
        stop.set()
        if monitor:
            monitor.join(timeout=15)
            test.assertFalse(monitor.is_alive())
        state = run_bounded(["systemctl", "show", "--all", name], text=True, timeout=20, output_max_bytes=256 * 1024, check=False)
        values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if values.get("LoadState") != "not-found":
            if (values.get("Description") != description or values.get("Transient") != "yes"
                    or observation and values.get("InvocationID") not in {"", observation["invocation_id"]}):
                raise ValueError("Refuse to stop a replaced promotion unit")
            run_bounded(["systemctl", "stop", name], timeout=30, output_max_bytes=65536, check=True)
        def stopped():
            observed = run_bounded(["systemctl", "show", name, "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID"], text=True, timeout=20, check=False)
            record = dict(line.split("=", 1) for line in observed.stdout.splitlines() if "=" in line)
            return record.get("LoadState") == "not-found" or record.get("ActiveState") == "inactive" and record.get("MainPID") == "0"
        context["wait"](stopped, seconds=30)
        if observation:
            def removed():
                proc = Path("/proc") / str(observation["pid"])
                try: same = int((proc / "stat").read_text().rsplit(") ", 1)[1].split()[19]) == observation["start_ticks"]
                except (FileNotFoundError, ProcessLookupError): same = False
                return not same and not Path("/sys/fs/cgroup" + observation["control_group"]).exists()
            context["wait"](removed, seconds=30)
            observation["original_process_and_cgroup_removed"] = True

