#!/usr/bin/env python3
"""Modeled Gerrit pair orchestration and durable remote-job contracts, not HA acceptance."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import gerrit_pair_agent as agent
import manage_gerrit_pair as pair
from test_gerrit_host import plan as host_plan
from test_gerrit_activation import uid


FILES = {"gerrit_pair_agent.py": "# modeled tool inventory\n"}
TOOLS = pair.manifest(FILES)


def fixture(runtime="raw"):
    first, second = host_plan(runtime), host_plan(runtime)
    second.update(member="gerrit-b", machine_id_sha256="b" * 64, boot_id=uid(21))
    second["health"]["address"] = "192.0.2.32"
    return {"schema_version": 1, "name": "review-pair", "site_plan": first["site_plan"], "runtime": runtime,
        "toolset_sha256": pair.activation.values.fingerprint(TOOLS), "ssh_binary_sha256": "a" * 64,
        "peers": [{"host": p["member"], "address": p["health"]["address"], "port": 22, "identity_env": "GERRIT_SSH_KEY", "known_hosts_env": "GERRIT_KNOWN_HOSTS",
            "remote_root": "/opt/platform-gerrit", "bundle": "/etc/gerrit" if runtime == "raw" else "/etc/platform-gerrit", "failure_domain": domain, "host_plan": p}
            for p, domain in ((first, "zone-a"), (second, "zone-b"))]}


def proof():
    return {"host_checked": True, "original_member_active": True, "tls_health_observed": True, "ssh_host_key_observed": True, **pair.activation.UNVERIFIED}


class Transport:
    def __init__(self): self.calls = []; self.jobs = {}; self.lost = None; self.status_override = None; self.check_fail = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def call(self, peer, request):
        verb, name = request["verb"], peer["host"]; self.calls.append((name, verb, request.get("action"), request.get("job_id")))
        if verb == "check":
            if self.check_fail == name: raise ValueError("Modeled peer prerequisite failure")
            p = peer["host_plan"]
            return {"checked": True, "machine_sha256": p["machine_id_sha256"], "boot_id": p["boot_id"], "host_plan_sha256": pair.activation.values.fingerprint(p),
                "package_sha256": pair.activation.values.fingerprint(pair.activation.sites.render(p["site_plan"], p["runtime"]))}
        if verb == "reserve": return {"reserved": True}
        if verb == "release": return {"released": True}
        if verb == "start":
            self.jobs[request["job_id"]] = (name, request["action"])
            if self.lost == (name, request["action"]): self.lost = None; raise TimeoutError("Lost accepted job acknowledgement")
            return {"accepted": True}
        if verb == "status":
            if request["job_id"] not in self.jobs: return {"state": "not-found"}
            return self.status_override or {"state": "completed", "termination_verified": True, "result": proof()}
        raise AssertionError("Unexpected finite RPC verb")


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name); (self.root / "private").mkdir()
        for obj, name, value in ((pair, "ROOT", self.root), (pair, "tool_files", lambda: FILES)):
            patch = mock.patch.object(obj, name, value); patch.start(); self.addCleanup(patch.stop)
        self.plan = fixture(); self.transport = Transport(); self.output = self.root / "private/operation"

    def operate(self, mode="activate", **kwargs):
        return pair.operate(self.plan, mode=mode, output=self.output, transport=self.transport, **kwargs)

    def test_local_exports_closed_tools_and_does_not_construct_transport(self):
        with mock.patch.object(pair, "SSH", side_effect=AssertionError("network")):
            self.assertFalse(pair.operate(self.plan)["pair_members_observed_ready"])
        self.assertFalse(self.output.exists())
        exported = pair.export_tools(self.root / "private/tools")
        self.assertEqual(exported["toolset_sha256"], self.plan["toolset_sha256"])
        self.assertEqual({p.name for p in (self.root / "private/tools/scripts").iterdir()}, set(TOOLS))

    def test_both_hosts_check_before_activation_then_both_originals_observed_again(self):
        for runtime in ("raw", "docker"):
            self.plan = fixture(runtime); self.transport = Transport(); self.output = self.root / ("private/operation-" + runtime)
            result = self.operate()
            self.assertTrue(result["pair_members_observed_ready"])
            for key in pair.UNVERIFIED: self.assertFalse(result[key])
            self.assertEqual([c[:3] for c in self.transport.calls[:2]], [("gerrit-b", "check", None), ("gerrit-a", "check", None)])
            self.assertEqual([(n, action) for n, verb, action, _ in self.transport.calls if verb == "start"],
                [("gerrit-b", "activate"), ("gerrit-a", "activate"), ("gerrit-b", "resume"), ("gerrit-a", "resume")])

    def test_completed_resume_reobserves_original_members_without_reactivating(self):
        self.operate(); before = len([c for c in self.transport.calls if c[1] == "start"])
        self.operate("resume")
        starts = [c for c in self.transport.calls if c[1] == "start"]
        self.assertEqual(len(starts), before + 2); self.assertEqual([c[2] for c in starts[-2:]], ["resume", "resume"])

    def test_lost_final_ack_requires_new_observations_instead_of_reusing_old_readiness(self):
        call = self.transport.call
        def lost_release(peer, request):
            if request["verb"] == "release": raise TimeoutError("Lost final acknowledgement")
            return call(peer, request)
        with mock.patch.object(self.transport, "call", side_effect=lost_release), self.assertRaises(TimeoutError): self.operate()
        self.assertEqual(sum(c[1] == "start" for c in self.transport.calls), 4)
        self.operate("resume")
        self.assertEqual(sum(c[1] == "start" for c in self.transport.calls), 6)

    def test_lost_job_ack_does_not_repeat_the_original_remote_job(self):
        self.transport.lost = ("gerrit-b", "activate")
        with self.assertRaises(TimeoutError): self.operate()
        original_id = next(c[3] for c in self.transport.calls if c[1] == "start")
        self.operate("resume")
        self.assertEqual(sum(c[1] == "start" and c[3] == original_id for c in self.transport.calls), 1)

    def test_missing_or_failed_uncertain_job_is_not_relaunched(self):
        self.transport.lost = ("gerrit-b", "activate")
        with self.assertRaises(TimeoutError): self.operate()
        self.transport.jobs.clear()
        with self.assertRaises(ValueError): self.operate("resume")
        self.assertEqual(sum(c[1] == "start" for c in self.transport.calls), 1)

    def test_live_finishing_job_waits_and_never_advances_or_clears_original_handle(self):
        self.transport.status_override = {"state": "finishing", "termination_verified": False}
        with mock.patch.object(pair.time, "monotonic", side_effect=[0, 10]), self.assertRaises(TimeoutError): self.operate(wait_seconds=5)
        self.assertEqual(sum(c[1] == "start" for c in self.transport.calls), 1)
        self.assertFalse(any(c[1] == "release" for c in self.transport.calls))

    def test_preflight_or_unverified_result_prevents_starting_the_second_member(self):
        self.transport.check_fail = "gerrit-a"
        with self.assertRaises(ValueError): self.operate()
        self.assertFalse(any(c[1] == "start" for c in self.transport.calls))
        self.transport = Transport(); self.output = self.root / "private/bad-result"
        self.transport.status_override = {"state": "completed", "termination_verified": False, "result": proof()}
        with self.assertRaises(ValueError): self.operate()
        self.assertEqual(sum(c[1] == "start" for c in self.transport.calls), 1)

    def test_same_machine_different_siteplan_and_unreviewed_toolset_refuse_locally(self):
        mutations = [lambda p: p["peers"][1]["host_plan"].update(machine_id_sha256=p["peers"][0]["host_plan"]["machine_id_sha256"]),
            lambda p: p["peers"][1]["host_plan"]["site_plan"].update(shared_claim="other-shared"),
            lambda p: p.update(toolset_sha256="0" * 64), lambda p: p["peers"][1].update(failure_domain="zone-a")]
        for mutate in mutations:
            bad = copy.deepcopy(self.plan); mutate(bad)
            with self.assertRaises(ValueError): pair.operate(bad)
        self.assertEqual(self.transport.calls, [])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.member = self.root / "private/gerrit-pair-agent/gerrit-a"; self.member.mkdir(parents=True)
        self.operation = "1" * 32; self.base = self.member / self.operation
        self.plan = fixture()["peers"][0]["host_plan"]
        self.request = {"schema_version": 1, "verb": "reserve", "operation_id": self.operation, "plan_sha256": "c" * 64,
            "tools": TOOLS, "host_plan": self.plan, "bundle": "/etc/gerrit", "job_id": None, "action": None}
        self.lease = {"schema_version": 1, "operation_id": self.operation, "plan_sha256": "c" * 64, "host_plan_sha256": "d" * 64, "tools_sha256": "e" * 64, "bundle": "/etc/gerrit", "package_sha256": "f" * 64}
        self.machine = mock.Mock(); self.machine.existing.return_value = None
        self.backend = agent.Agent(self.machine)
        def directory(path):
            path.mkdir(mode=0o700, exist_ok=True); return path
        patches = [mock.patch.object(agent, "ROOT", self.root), mock.patch.object(self.backend, "context", return_value=(self.machine, self.member, self.lease)),
            mock.patch.object(agent.activation.host_tools, "trusted"), mock.patch.object(agent.identities, "signature_live", return_value=False),
            mock.patch.object(agent.jobs, "termination", return_value={"termination_verified": True}), mock.patch.object(agent, "private_directory", side_effect=directory)]
        for patch in patches: patch.start(); self.addCleanup(patch.stop)
        self.backend.rpc(self.request)

    def req(self, verb, job="2" * 32, action="activate"):
        return {**self.request, "verb": verb, "job_id": job if verb in {"start", "status"} else None, "action": action if verb in {"start", "status"} else None}

    def finish_launch(self, job_id, log, command, run):
        path = self.base / (job_id + ".json"); record = json.loads(path.read_text())
        record.update(state="completed", worker={"pid": 99, "start_ticks": 10, "boot_id": uid(1)}, result=proof()); path.write_text(json.dumps(record))
        return 99

    def test_fast_worker_result_cannot_be_overwritten_by_late_launcher(self):
        with mock.patch.object(agent.jobs, "launch", side_effect=self.finish_launch) as launched:
            self.backend.rpc(self.req("start")); self.backend.rpc(self.req("start"))
            self.assertEqual(launched.call_count, 1)
        result = self.backend.rpc(self.req("status")); self.assertEqual(result["state"], "completed")
        self.assertTrue(result["termination_verified"])

    def test_live_cgroup_prevents_terminal_or_release_claim(self):
        with mock.patch.object(agent.jobs, "launch", side_effect=self.finish_launch):
            self.backend.rpc(self.req("start")); self.backend.rpc(self.req("start", "3" * 32, "resume"))
        with mock.patch.object(agent.jobs, "termination", return_value=None):
            self.assertEqual(self.backend.rpc(self.req("status", "3" * 32, "resume"))["state"], "finishing")
            with self.assertRaises(ValueError): self.backend.rpc(self.req("release"))
        self.assertTrue(self.backend.rpc(self.req("release"))["released"])
        self.assertTrue((self.member / "reservation.json").exists())

    def test_failed_launch_is_not_repeated_and_terminal_unknown_job_stops(self):
        with mock.patch.object(agent.jobs, "launch", side_effect=TimeoutError("lost launch response")) as launched:
            with self.assertRaises(TimeoutError): self.backend.rpc(self.req("start"))
            self.backend.rpc(self.req("start")); self.assertEqual(launched.call_count, 1)
        self.assertEqual(self.backend.rpc(self.req("status"))["state"], "unverified")
        with self.assertRaises(ValueError): self.backend.rpc(self.req("start", "3" * 32, "resume"))

    def test_worker_executes_original_host_activation_once_and_resume_is_separate(self):
        with mock.patch.object(agent.jobs, "launch", return_value=99): self.backend.rpc(self.req("start"))
        with mock.patch.object(agent.activation, "operate", return_value=proof()) as operate:
            self.backend.execute("gerrit-a", self.operation, "2" * 32)
            with self.assertRaises(ValueError): self.backend.execute("gerrit-a", self.operation, "2" * 32)
            self.assertEqual(operate.call_count, 1); self.assertEqual(operate.call_args.kwargs["mode"], "activate")


class SystemdJobTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GERRIT_PAIR_TEST_SYSTEMD") == "1", "Explicit disposable PID-1 job fixture")
    def test_original_remote_job_survives_launcher_and_waits_for_cgroup_termination(self):
        from test_gerrit_pair_job_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["pid1_job_survives_launcher_exit"])
        self.assertTrue(result["original_job_termination_verified"])
        self.assertFalse(result["gerrit_runtime_verified"]); self.assertFalse(result["ceph_runtime_verified"])

    @unittest.skipUnless(os.environ.get("GERRIT_PAIR_TEST_SYSTEMD") == "1", "Explicit protected Linux agent-source fixture")
    def test_closed_exported_tools_verify_and_regenerate_packages_in_isolated_python(self):
        from test_gerrit_pair_job_fixture import exercise_tools
        result = exercise_tools(self)
        self.assertTrue(result["closed_toolset_verified"]); self.assertTrue(result["runtime_package_regenerated"])
        self.assertFalse(result["gerrit_runtime_verified"])


if __name__ == "__main__": unittest.main()
