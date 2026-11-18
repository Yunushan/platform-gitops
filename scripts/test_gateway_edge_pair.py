#!/usr/bin/env python3
"""Test pair ordering, durable jobs, root reservations and pinned SSH without a real VIP."""

import base64
from contextlib import nullcontext
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import struct
import shlex
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import gateway_edge_pair_agent as agent
import manage_gateway_edge_pair as pair
import manage_gateway_edge_host as host
from test_gateway_edge_host import package, FakeHost


def fixture(root):
    bundle, digest, gateway, ca, artifacts = package(root, "docker")
    tools = pair.manifest(pair.tool_files())
    plan = {"schema_version": 1, "name": "platform-entry", "edge_contract_sha256": digest, "toolset_sha256": agent.fingerprint(tools), "ssh_binary_sha256": "f" * 64,
        "peers": [{"host": "edge-" + label, "address": "192.0.2." + str(10 + index), "port": 22, "machine_sha256": char * 64,
            "identity_env": "ENTRY_" + label.upper() + "_SSH_KEY", "known_hosts_env": "ENTRY_" + label.upper() + "_HOST_KEYS", "remote_root": "/opt/platform/edge-tools",
            "paths": {"bundle": "/opt/platform/entry", "gateway": "/opt/platform/gateway", "ca": "/opt/platform/ca.crt", "artifacts": None}, "expected_current": "absent"}
            for index, (label, char) in enumerate((("a", "c"), ("b", "d")))]}
    return plan, bundle, gateway, ca, artifacts, tools


class Remote:
    def __init__(self, plan):
        self.plan, self.calls, self.jobs, self.leases = plan, [], {}, {}
        self.nodes = {p["host"]: {"machine_sha256": p["machine_sha256"], "stable": None, "pending": None, "healthy": False} for p in plan["peers"]}
        self.lost_start, self.running_polls, self.fail_job, self.unknown, self.finishing = False, 0, False, False, False
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def call(self, peer, request):
        name, verb = peer["host"], request["verb"]
        self.calls.append((name, verb, request["job_id"]))
        if verb == "reserve":
            if name in self.leases and self.leases[name] != request["operation_id"]: raise ValueError("Other coordinator owns peer")
            self.leases[name] = request["operation_id"]; return {"reserved": True}
        if verb == "inspect": return copy.deepcopy(self.nodes[name])
        if verb == "start":
            if request["job_id"] not in self.jobs: self.jobs[request["job_id"]] = {"host": name, "done": False}
            if self.lost_start:
                self.lost_start = False; raise TimeoutError("SSH acknowledgment lost after job accepted")
            return {"state": "accepted"}
        if verb == "status":
            job = self.jobs.get(request["job_id"])
            if job is None: return {"state": "not-found"}
            if self.running_polls:
                self.running_polls -= 1; return {"state": "unverified" if self.unknown else "finishing" if self.finishing else "running", "process": {"pid": 123, "start_ticks": 456}}
            if self.fail_job: return {"state": "failed", "result": {"error_code": "fixture-failure"}}
            self.nodes[name].update(stable=request["target"], healthy=True)
            job["done"] = True
            return {"state": "completed", "result": {"host_ready": True, "ha_verified": False}}
        if verb in {"release", "cancel"}:
            self.leases.pop(name, None); return {"released": True}
        raise AssertionError("Unexpected RPC verb")


class PairTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GATEWAY_EDGE_PAIR_VM_TERMINATED_RUNTIME") == "1", "Explicit terminated worker recovery in disposable guests")
    def test_real_native_pair_vm_terminated_recovery(self):
        from functools import partial
        from test_gateway_edge import fixture as edge_fixture
        from test_gateway_edge_host_vm_fixture import exercise
        from test_gateway_edge_pair_vm_fixture import workload
        result = exercise(self, edge_fixture, pair_workload=partial(workload, recovery=True, terminate_worker=True))
        self.assertTrue(result["terminated_job_recovery_verified"])

    @unittest.skipUnless(os.environ.get("GATEWAY_EDGE_PAIR_VM_RECOVERY_RUNTIME") == "1", "Explicit actual coordinator interruption recovery in disposable guests")
    def test_real_native_pair_vm_recovery(self):
        from functools import partial
        from test_gateway_edge import fixture as edge_fixture
        from test_gateway_edge_host_vm_fixture import exercise
        from test_gateway_edge_pair_vm_fixture import workload
        result = exercise(self, edge_fixture, pair_workload=partial(workload, recovery=True))
        self.assertTrue(result["coordinator_loss_recovery_verified"])

    def test_isolated_tool_package_can_validate_native_and_docker_bundles(self):
        for runtime in ("raw", "docker"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundle, digest, gateway, *_ = package(root, runtime)
                isolated = root / "isolated/scripts"; isolated.mkdir(parents=True)
                for name, source in pair.tool_files().items():
                    (isolated / name).write_text(source, encoding="utf-8", newline="\n")
                dependency = root / "dependencies/yaml"; dependency.mkdir(parents=True)
                for path in Path(agent.yaml.__file__).parent.glob("*.py"):
                    (dependency / path.name).write_bytes(path.read_bytes())
                command = "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[5]); sys.path.insert(0,sys.argv[1]); import manage_gateway_edge_host as h; h.capture(Path(sys.argv[2]),sys.argv[3],Path(sys.argv[4]))"
                result = pair.run_bounded([sys.executable, "-I", "-B", "-c", command, str(isolated), str(bundle), digest, str(gateway), str(dependency.parent)],
                    text=True, timeout=20, output_max_bytes=65536, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get("GATEWAY_EDGE_PAIR_VM_RUNTIME") == "1", "Explicit real two-guest native pair runtime")
    def test_real_native_pair_vm_lifecycle(self):
        from test_gateway_edge import fixture as edge_fixture
        from test_gateway_edge_host_vm_fixture import exercise
        from test_gateway_edge_pair_vm_fixture import workload
        result = exercise(self, edge_fixture, pair_workload=workload)
        self.assertTrue(result["pair_coordinator_verified"])

    def test_optional_real_ssh_configuration_without_connecting(self):
        binary = os.environ.get("GATEWAY_PAIR_TEST_SSH")
        if not binary: self.skipTest("Set the reviewed local OpenSSH binary for -G parsing; no connection is made")
        key_type = b"ssh-ed25519"
        key = base64.b64encode(struct.pack(">I", len(key_type)) + key_type + struct.pack(">I", 32) + b"x" * 32).decode()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, *_ = fixture(root)
            plan["ssh_binary_sha256"] = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
            identity = root / "identity"; identity.write_text("unused-by-configuration-parser"); identity.chmod(0o600)
            variables = {}
            for peer in plan["peers"]:
                known = root / (peer["host"] + ".hosts"); known.write_text(peer["address"] + " ssh-ed25519 " + key + "\n")
                variables.update({peer["identity_env"]: str(identity), peer["known_hosts_env"]: str(known)})
            with mock.patch.dict(os.environ, variables), pair.SSH(plan, {}, Path(binary)) as transport:
                for prefix in transport.peers.values():
                    result = pair.run_bounded([prefix[0], "-G", *prefix[1:]], input="", timeout=15, text=True, output_max_bytes=256 * 1024, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("batchmode yes", result.stdout)
                    self.assertIn("stricthostkeychecking true", result.stdout)
                    self.assertIn("forwardagent no", result.stdout)

    def test_optional_pinned_bootstrap_identity_only_without_host_state_changes(self):
        if os.name != "posix" or os.geteuid() != 0 or os.environ.get("GATEWAY_PAIR_TEST_BOOTSTRAP") != "1":
            self.skipTest("Opt in to the root Linux bootstrap fixture in a trusted volatile /run directory")
        with tempfile.TemporaryDirectory(prefix="platform-entry-rpc-", dir="/run") as temporary:
            root = Path(temporary); (root / "scripts").mkdir(mode=0o755)
            files = pair.tool_files(); tools = pair.manifest(files)
            for name, text in files.items():
                path = root / "scripts" / name; path.write_text(text, encoding="utf-8", newline="\n"); path.chmod(0o644)
            request = {"verb": "identity", "operation_id": "1" * 32, "plan_sha256": "2" * 64, "target": "3" * 64, "host": "edge-a",
                "machine_sha256": hashlib.sha256(Path("/etc/machine-id").read_bytes().strip()).hexdigest(), "paths": {}, "expected_current": "absent", "rollback": False, "tools": tools, "job_id": None}
            command = ["/bin/sh", "-c", shlex.join(["/usr/bin/python3", "-I", "-B", "-c", pair.BOOTSTRAP, str(root), plan_hash := agent.fingerprint(tools)])]
            result = pair.run_bounded(command, input=agent.canonical(request), text=True, timeout=20, output_max_bytes=256 * 1024, check=False)
            self.assertEqual(result.returncode, 0, "Pinned identity bootstrap failed; raw private diagnostics suppressed")
            self.assertEqual(json.loads(result.stdout), {"machine_sha256": request["machine_sha256"], "toolset_sha256": plan_hash})
            (root / "scripts/gateway_edge_pair_agent.py").write_text("raise RuntimeError('modified source')")
            result = pair.run_bounded(command, input=agent.canonical(request), text=True, timeout=20, output_max_bytes=256 * 1024, check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_default_mode_cannot_read_ssh_credentials_or_construct_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, bundle, gateway, *_ = fixture(Path(directory))
            with mock.patch.object(pair, "SSH") as ssh, mock.patch.object(pair, "probe") as probe:
                self.assertFalse(pair.operate(plan, bundle, gateway)["pair_services_ready"])
            ssh.assert_not_called(); probe.assert_not_called()

    def test_tool_inventory_is_closed_and_detects_source_drift(self):
        self.assertIn("PyYAML==" + agent.YAML_VERSION, (pair.ROOT / "requirements/ci-yaml.txt").read_text())
        files = pair.tool_files()
        self.assertIn("gateway_edge_pair_agent.py", files)
        self.assertIn("manage_gateway_edge_host.py", files)
        self.assertIn("bounded_subprocess.py", files)
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"):
            root = Path(temporary); (root / "scripts").mkdir()
            for name, content in files.items(): (root / "scripts" / name).write_text(content, encoding="utf-8", newline="\n")
            with mock.patch.object(agent, "ROOT", root):
                agent.tools_verified(pair.manifest(files))
                (root / "scripts" / "unreviewed.py").write_text("raise RuntimeError()")
                with self.assertRaises(ValueError): agent.tools_verified(pair.manifest(files))

    def test_unhealthy_existing_a_blocks_all_host_jobs_and_can_cancel_unused_reservations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan)
            plan["peers"][0]["expected_current"] = "a" * 64
            remote.nodes["edge-a"].update(stable="a" * 64, healthy=False)
            with self.assertRaises(ValueError): self.run_pair(root, plan, bundle, gateway, remote)
            self.assertFalse(any(verb == "start" for _, verb, _ in remote.calls))
            self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote, mode="cancel")["cancelled"])
            self.assertFalse(remote.leases)

    def test_exact_known_host_entries_and_key_types_are_required(self):
        key_type = b"ssh-ed25519"
        key = base64.b64encode(struct.pack(">I", len(key_type)) + key_type + struct.pack(">I", 32) + b"x" * 32).decode()
        peer = {"address": "192.0.2.10", "port": 22}
        text = "192.0.2.10 ssh-ed25519 " + key
        self.assertEqual(pair.known_host_snapshot(text, peer), text + "\n")
        for bad in (text.replace("192.0.2.10", "*"), text.replace("192.0.2.10", "other.example.com"), "@cert-authority " + text,
                    text.replace("ssh-ed25519", "ssh-rsa"), text + "\n192.0.2.11 ssh-ed25519 " + key):
            with self.assertRaises(ValueError): pair.known_host_snapshot(bad, peer)

    def test_duplicate_machine_or_remote_path_scope_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, bundle, gateway, *_ = fixture(Path(temporary))
            for change in (lambda p: p["peers"][1].update(machine_sha256=p["peers"][0]["machine_sha256"]),
                           lambda p: p["peers"][0].update(remote_root="/tmp/../unreviewed"), lambda p: p["peers"][0].update(address="192.0.2.99")):
                bad = copy.deepcopy(plan); change(bad)
                with self.assertRaises(ValueError): pair.operate(bad, bundle, gateway)

    def run_pair(self, root, plan, bundle, gateway, remote, *, mode="apply", observer=None, recover=False):
        files = pair.tool_files()
        with mock.patch.object(pair, "ROOT", root), mock.patch.object(pair, "tool_files", return_value=files), mock.patch.object(pair.time, "sleep"):
            return pair.operate(plan, bundle, gateway, mode=mode, output=root / "private" / "operation", transport=remote,
                observer=observer or (lambda: {"vip_reachable": True}), recover_terminated_jobs=recover, wait_seconds=5)

    def test_both_reservations_precede_b_then_a_and_final_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan)
            self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote)["pair_services_ready"])
            starts = [(name, verb) for name, verb, _ in remote.calls if verb == "start"]
            self.assertEqual(starts, [("edge-b", "start"), ("edge-a", "start")])
            self.assertEqual(remote.calls[:2], [("edge-b", "reserve", None), ("edge-a", "reserve", None)])
            self.assertFalse(remote.leases)
            proof = json.loads((root / "private/operation/pair-proof.json").read_text())
            self.assertFalse(proof["ha_verified"]); self.assertFalse(proof["vip_failover_verified"])

    def test_lost_ack_resumes_the_same_remote_job_without_a_second_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan); remote.lost_start = True
            with self.assertRaises(TimeoutError): self.run_pair(root, plan, bundle, gateway, remote)
            receipt = root / "private/operation/pair-proof.json"
            failed = json.loads(receipt.read_text())
            failure = {key: failed[key] for key in ("failed_at", "error_code")}
            before = [row for row in remote.calls if row[1] == "start"]
            self.assertEqual(len(before), 1); self.assertTrue(remote.leases)
            self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote, mode="resume")["pair_services_ready"])
            self.assertEqual([row for row in remote.calls if row[1] == "start" and row[0] == "edge-b"], before)
            completed = json.loads(receipt.read_text())
            self.assertEqual(completed["phase"], "completed")
            self.assertNotIn("error_code", completed); self.assertNotIn("failed_at", completed)
            self.assertEqual(completed["last_failure"], failure)
            self.assertEqual(completed["job_state"], "completed")

    def test_expired_observation_does_not_restart_running_or_unknown_jobs(self):
        for job_state in ("running", "unverified", "finishing"):
            with self.subTest(job_state=job_state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan)
                remote.running_polls, remote.unknown, remote.finishing = 100, job_state == "unverified", job_state == "finishing"
                with mock.patch.object(pair.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError):
                    self.run_pair(root, plan, bundle, gateway, remote)
                self.assertEqual([row[0] for row in remote.calls if row[1] == "start"], ["edge-b"])
                remote.running_polls = 0
                self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote, mode="resume")["pair_services_ready"])
                self.assertEqual(len([row for row in remote.calls if row[1] == "start"]), 2)

    def test_vip_probe_failure_preserves_a_and_resumes_without_restarting_b(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan)
            with self.assertRaises(ValueError): self.run_pair(root, plan, bundle, gateway, remote, observer=lambda: {"vip_reachable": False})
            self.assertEqual([row[0] for row in remote.calls if row[1] == "start"], ["edge-b"])
            self.run_pair(root, plan, bundle, gateway, remote, mode="resume")
            self.assertEqual([row[0] for row in remote.calls if row[1] == "start"], ["edge-b", "edge-a"])

    def test_rolling_enablement_checks_ssh_after_both_peers_have_the_new_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, bundle, gateway, *_ = fixture(root)
            remote = Remote(plan)
            files = pair.tool_files()
            with mock.patch.object(pair, "ROOT", root), mock.patch.object(pair.time, "sleep"), \
                    mock.patch.object(pair, "tool_files", return_value=files), \
                    mock.patch.object(pair, "observer_context") as trust, \
                    mock.patch.object(pair, "probe", return_value={"vip_reachable": True}) as probe:
                pair.operate(plan, bundle, gateway, mode="apply", output=root / "private" / "operation",
                             transport=remote, wait_seconds=5)
            self.assertEqual([call.kwargs for call in probe.call_args_list], [
                {"context": trust.return_value, "check_ssh": False},
                {"context": trust.return_value, "check_ssh": True},
            ])

    def test_failed_jobs_are_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan); remote.fail_job = True
            with self.assertRaises(ValueError): self.run_pair(root, plan, bundle, gateway, remote)
            self.assertEqual(len([row for row in remote.calls if row[1] == "start"]), 1)
            remote.fail_job = False
            self.run_pair(root, plan, bundle, gateway, remote, mode="resume")
            self.assertEqual([row[0] for row in remote.calls if row[1] == "start"], ["edge-b", "edge-a"])

    def test_coordinator_lock_rejects_concurrent_writers_and_reopens_after_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with pair.coordinator_lock(root):
                with self.assertRaises(OSError):
                    with pair.coordinator_lock(root): self.fail("Concurrent lock acquired")
            with pair.coordinator_lock(root): pass

    def test_only_explicit_resume_can_recover_a_verified_terminated_job(self):
        class TerminatedRemote(Remote):
            def __init__(self, plan): super().__init__(plan); self.old_job, self.recovered = None, False
            def call(self, peer, request):
                if request["verb"] == "start" and self.old_job is None: self.old_job = request["job_id"]
                if request["job_id"] == self.old_job and request["verb"] == "status":
                    self.calls.append((peer["host"], "status", request["job_id"]))
                    return {"state": "failed", "result": {"termination_verified": True}} if self.recovered else {"state": "unverified"}
                if request["verb"] == "recover":
                    self.calls.append((peer["host"], "recover", request["job_id"]))
                    self.recovered = True
                    return {"state": "failed", "result": {"termination_verified": True}}
                return super().call(peer, request)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = TerminatedRemote(plan)
            with mock.patch.object(pair.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError):
                self.run_pair(root, plan, bundle, gateway, remote)
            self.assertFalse(remote.recovered)
            self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote, mode="resume", recover=True)["pair_services_ready"])
            self.assertEqual(len([row for row in remote.calls if row[1] == "recover"]), 1)
            self.assertEqual([row[0] for row in remote.calls if row[1] == "start"], ["edge-b", "edge-b", "edge-a"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = Remote(plan)
            with self.assertRaises(ValueError): self.run_pair(root, plan, bundle, gateway, remote, recover=True)
            self.assertFalse(remote.calls)

    def test_inconclusive_recovery_does_not_allocate_another_job(self):
        class UncertainRemote(Remote):
            def call(self, peer, request):
                if request["verb"] == "recover":
                    self.calls.append((peer["host"], "recover", request["job_id"]))
                    return {"state": "unverified", "termination_verified": False}
                return super().call(peer, request)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = UncertainRemote(plan)
            remote.running_polls, remote.unknown = 100, True
            with mock.patch.object(pair.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError):
                self.run_pair(root, plan, bundle, gateway, remote)
            with mock.patch.object(pair.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError):
                self.run_pair(root, plan, bundle, gateway, remote, mode="resume", recover=True)
            self.assertEqual(len([row for row in remote.calls if row[1] == "start"]), 1)
            self.assertEqual(len(remote.jobs), 1)


@unittest.skipUnless(os.name == "posix", "POSIX agent receipts and host reservation fixtures")
class AgentTests(unittest.TestCase):
    def context(self, root):
        bundle, digest, gateway, ca, artifacts = package(root)
        machine = FakeHost(root)
        contract, _ = host.capture(bundle, digest, gateway)
        owner = {"name": contract["plan"]["name"], "runtime": "raw", "host": "edge-a"}
        request = {"verb": "reserve", "operation_id": "1" * 32, "plan_sha256": "2" * 64, "target": digest, "host": "edge-a", "machine_sha256": "3" * 64,
            "paths": {"bundle": str(bundle), "gateway": str(gateway), "ca": str(ca), "artifacts": str(artifacts)}, "expected_current": "absent", "rollback": False, "tools": {}, "job_id": None}
        lease = {"schema_version": 1, "operation_id": request["operation_id"], "plan_sha256": request["plan_sha256"], "target": digest, "owner": owner}
        instance = agent.Agent(machine)
        return instance, request, contract, owner, lease

    def test_reservation_excludes_direct_host_mutation_and_other_coordinators(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                self.assertTrue(instance.rpc(request)["reserved"])
                changed = {**lease, "operation_id": "9" * 32}
                with mock.patch.object(instance, "context", return_value=(contract, owner, changed)), self.assertRaises(ValueError): instance.rpc(request)
            paths = request["paths"]
            with self.assertRaises(ValueError): host.operate(Path(paths["bundle"]), request["target"], Path(paths["gateway"]), "edge-a", mode="apply",
                ca_file=Path(paths["ca"]), artifacts=Path(paths["artifacts"]), machine=instance.machine)

    def test_existing_reservation_and_busy_inspection_do_not_require_a_new_host_lock(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                instance.rpc(request)
                with mock.patch.object(host, "host_lock", side_effect=BlockingIOError("Running host job owns lock")):
                    self.assertTrue(instance.rpc(request)["reserved"])
                    self.assertTrue(instance.rpc({**request, "verb": "inspect"})["busy"])

    def test_transient_job_is_detached_bounded_and_has_private_logging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); instance, *_ = self.context(root)
            instance.jobs().mkdir(parents=True)
            with mock.patch.object(instance.machine, "run", side_effect=["", "999\n"]) as run:
                self.assertEqual(instance.spawn("7" * 32), 999)
            arguments = run.call_args_list[0].args[0]
            self.assertEqual(arguments[0], "/usr/bin/systemd-run")
            for flag in ("--slice=system.slice", "--service-type=exec", "--collect", "--property=KillMode=control-group", "--property=RuntimeMaxSec=900", "--property=MemoryMax=512M", "--property=Delegate=no", "--property=SendSIGKILL=yes"):
                self.assertIn(flag, arguments)
            self.assertEqual((instance.jobs() / ("7" * 32 + ".log")).stat().st_mode & 0o777, 0o600)

    def test_cancel_is_idempotent_only_before_any_job(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                instance.rpc(request)
                self.assertTrue(instance.rpc({**request, "verb": "cancel"})["released"])
                self.assertTrue(instance.rpc({**request, "verb": "cancel"})["released"])
                self.assertFalse((instance.machine.state / "state.json").exists())

    def test_job_launch_is_idempotent_and_worker_binds_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), \
             mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()), mock.patch.object(host.health, "healthy", return_value=True):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                instance.rpc(request)
                request.update(verb="start", job_id="4" * 32)
                identity = lambda pid: {"pid": pid, "start_ticks": 7, "boot_id": "11111111-2222-3333-4444-555555555555"}
                with mock.patch.object(agent, "process_identity", side_effect=identity), mock.patch.object(instance, "spawn", return_value=999) as spawn:
                    self.assertEqual(instance.rpc(request)["state"], "accepted")
                    self.assertEqual(instance.rpc(request)["state"], "running")
                    self.assertEqual(spawn.call_count, 1)
                    instance.execute(request["job_id"])
                with mock.patch.object(agent, "process_identity", return_value=None), mock.patch.object(agent.systemd_job, "termination", return_value={"termination_verified": True, "cgroup_empty": True}):
                    status = instance.rpc({**request, "verb": "status"})
                    self.assertEqual(status["state"], "completed"); self.assertTrue(status["result"]["host_ready"])
                    self.assertTrue(instance.rpc({**request, "verb": "release", "job_id": None})["released"])

    def test_dead_worker_is_unverified_and_blocks_a_second_job(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                instance.rpc(request); request.update(verb="start", job_id="5" * 32)
                with mock.patch.object(agent, "process_identity", return_value=None), mock.patch.object(instance, "spawn", return_value=999):
                    instance.rpc(request)
                    self.assertEqual(instance.rpc({**request, "verb": "status"})["state"], "unverified")
                    with self.assertRaises(ValueError): instance.rpc({**request, "job_id": "6" * 32})

    def test_terminated_job_recovery_requires_unit_evidence_and_preserves_host_state(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            signature = {"pid": 999, "start_ticks": 7, "boot_id": "11111111-2222-3333-4444-555555555555"}
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                instance.rpc(request); request.update(verb="start", job_id="5" * 32)
                with mock.patch.object(agent, "process_identity", return_value=signature), mock.patch.object(instance, "spawn", return_value=999): instance.rpc(request)
                before = (instance.machine.state / "state.json").read_bytes()
                with mock.patch.object(agent, "process_identity", return_value=None):
                    with mock.patch.object(agent.systemd_job, "termination", return_value=None):
                        self.assertEqual(instance.rpc({**request, "verb": "recover"})["state"], "unverified")
                    with self.assertRaises(ValueError), mock.patch.object(instance, "spawn", return_value=1000): instance.rpc({**request, "job_id": "6" * 32})
                    proof = {"termination_verified": True, "unit_absent": True, "cgroup_empty": True, "boot_id": signature["boot_id"]}
                    with mock.patch.object(agent.systemd_job, "termination", return_value=proof), mock.patch.object(host, "operate") as mutate:
                        recovered = instance.rpc({**request, "verb": "recover"})
                        self.assertEqual(recovered["state"], "failed"); self.assertTrue(recovered["result"]["termination_verified"])
                        with self.assertRaises(ValueError): instance.execute(request["job_id"])
                        mutate.assert_not_called()
                self.assertEqual((instance.machine.state / "state.json").read_bytes(), before)
                self.assertEqual(host.pair_reservation(instance.machine.state, owner), lease)

    def test_live_worker_and_busy_job_lock_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            signature = {"pid": 999, "start_ticks": 7, "boot_id": "11111111-2222-3333-4444-555555555555"}
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)), mock.patch.object(agent, "process_identity", return_value=signature):
                instance.rpc(request); request.update(verb="start", job_id="5" * 32)
                with mock.patch.object(instance, "spawn", return_value=999): instance.rpc(request)
                with mock.patch.object(agent.systemd_job, "termination") as query:
                    self.assertEqual(instance.rpc({**request, "verb": "recover"})["state"], "running")
                    query.assert_not_called()
                    with mock.patch.object(host, "host_lock", side_effect=BlockingIOError("Live worker owns job lock")):
                        self.assertEqual(instance.rpc({**request, "verb": "recover"})["state"], "unverified")
                    query.assert_not_called()

    def test_terminal_receipt_cannot_advance_until_systemd_descendants_have_drained(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
            root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
            signature = {"pid": 999, "start_ticks": 7, "boot_id": "11111111-2222-3333-4444-555555555555"}
            with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                instance.rpc(request); request.update(verb="start", job_id="5" * 32)
                with mock.patch.object(agent, "process_identity", return_value=signature), mock.patch.object(instance, "spawn", return_value=999): instance.rpc(request)
                job = instance.job_read(request["job_id"]); job.update(state="failed", result={"error_code": "fixture"})
                host.atomic_write_text(instance.job_path(request["job_id"]), agent.canonical(job))
                with mock.patch.object(agent, "process_identity", return_value=None), mock.patch.object(agent.systemd_job, "termination", return_value=None):
                    self.assertEqual(instance.rpc({**request, "verb": "status"})["state"], "finishing")
                    with self.assertRaises(ValueError): instance.rpc({**request, "verb": "release", "job_id": None})
                    with self.assertRaises(ValueError), mock.patch.object(instance, "spawn") as spawn: instance.rpc({**request, "job_id": "6" * 32})
                    spawn.assert_not_called()
                    self.assertEqual(host.pair_reservation(instance.machine.state, owner), lease)
                original_receipt = instance.job_path(request["job_id"]).read_bytes()
                with mock.patch.object(agent, "process_identity", return_value=None), mock.patch.object(agent.systemd_job, "termination", return_value={"termination_verified": True, "cgroup_empty": True}):
                    recovered = instance.rpc({**request, "verb": "recover"})
                    self.assertEqual(recovered["result"]["error_code"], "fixture")
                    self.assertTrue(recovered["result"]["termination_verified"])
                self.assertEqual(instance.job_path(request["job_id"]).read_bytes(), original_receipt)

    def test_missing_or_mismatched_host_marker_is_never_recreated_from_a_lease(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary, mock.patch.object(host.starter, "trusted_paths"), mock.patch.object(host, "host_lock", side_effect=lambda *a, **kw: nullcontext()):
                root = Path(temporary); instance, request, contract, owner, lease = self.context(root)
                signature = {"pid": 999, "start_ticks": 7, "boot_id": "11111111-2222-3333-4444-555555555555"}
                with mock.patch.object(instance, "context", return_value=(contract, owner, lease)):
                    instance.rpc(request)
                    request.update(verb="start", job_id="5" * 32)
                    with mock.patch.object(agent, "process_identity", return_value=signature), mock.patch.object(instance, "spawn", return_value=999): instance.rpc(request)
                    receipt = instance.machine.state / "state.json"
                    if missing: receipt.unlink()
                    else:
                        state = instance.read_state(owner); state.pop("coordination")
                        host.atomic_write_text(receipt, agent.canonical(state))
                    with self.assertRaises(ValueError): instance.rpc({**request, "verb": "reserve", "job_id": None})
                    with mock.patch.object(agent, "process_identity", return_value=None), mock.patch.object(agent.systemd_job, "termination") as query:
                        with self.assertRaises(ValueError): instance.rpc({**request, "verb": "recover"})
                        query.assert_not_called()
                    self.assertEqual(host.pair_reservation(instance.machine.state, owner), lease)


if __name__ == "__main__": unittest.main()
