#!/usr/bin/env python3
"""Test termination evidence and opt-in local PID-1 job/session-loss behavior."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4

import gateway_edge_pair_agent as agent
import gateway_edge_systemd_job as jobs
import manage_gateway_edge_host as host
import manage_gateway_edge_pair as pair


BOOT = "11111111-2222-3333-4444-555555555555"


def property_text(**updates):
    values = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "ControlPID": "0",
              "ControlGroup": "", "Transient": "no", "Restart": "no", **updates}
    return "\n".join(key + "=" + value for key, value in values.items()) + "\n"


class TerminationTests(unittest.TestCase):
    def test_missing_unit_needs_all_terminal_properties_and_records_boot_identity(self):
        with mock.patch.object(jobs, "read_bounded_text", return_value=BOOT):
            proof = jobs.termination("1" * 32, lambda _: property_text())
        self.assertTrue(proof["termination_verified"]); self.assertTrue(proof["unit_absent"])
        self.assertEqual(proof["boot_id"], BOOT)

    def test_live_transitioning_restartable_and_foreign_units_remain_unverified(self):
        changes = [{"ActiveState": state} for state in ("active", "activating", "deactivating", "reloading", "maintenance")]
        changes += [{"MainPID": "123"}, {"ControlPID": "123"}, {"Restart": "always"}, {"LoadState": "error"},
                    {"LoadState": "loaded", "Transient": "no"}, {"SubState": "start"}, {"ControlGroup": "/system.slice/foreign.service"}]
        for change in changes:
            with self.subTest(change=change), mock.patch.object(jobs, "read_bounded_text") as read:
                self.assertIsNone(jobs.termination("1" * 32, lambda _: property_text(**change)))
                read.assert_not_called()

    def test_cgroup_descendants_must_be_empty_even_when_main_pid_is_zero(self):
        group = "/system.slice/" + jobs.unit_name("1" * 32)
        run = lambda _: property_text(LoadState="loaded", Transient="yes", ControlGroup=group)
        for events in ("populated 1\nfrozen 0\n", "populated 0\npopulated 1\n", "invalid\n"):
            with mock.patch.object(jobs, "read_bounded_text", return_value=events):
                self.assertIsNone(jobs.termination("1" * 32, run))
        with mock.patch.object(jobs, "read_bounded_text", side_effect=["populated 0\nfrozen 0\n", BOOT]) as read:
            self.assertTrue(jobs.termination("1" * 32, run)["cgroup_empty"])
            self.assertTrue(str(read.call_args_list[0].args[0]).endswith("cgroup.events"))

    def test_property_and_cgroup_observation_failure_is_not_termination(self):
        for output in ("", property_text() + "MainPID=0\n", property_text().replace("MainPID=0\n", ""), "private error"):
            with self.assertRaises(ValueError): jobs.termination("1" * 32, lambda _: output)
        with self.assertRaises(TimeoutError): jobs.termination("1" * 32, mock.Mock(side_effect=TimeoutError("Observation expired")))
        with mock.patch.object(jobs, "read_bounded_text", side_effect=FileNotFoundError()), self.assertRaises(FileNotFoundError):
            jobs.termination("1" * 32, lambda _: property_text(LoadState="loaded", Transient="yes", ControlGroup="/system.slice/" + jobs.unit_name("1" * 32)))

    def test_unit_identity_is_an_exact_recorded_job_handle(self):
        for value in ("../foreign", "-" * 32, "1" * 31, None, "a" * 32 + ".service"):
            with self.assertRaises(ValueError): jobs.unit_name(value)


WORKER = '''import json,os,pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[1])
child=subprocess.Popen([sys.executable,"-I","-B","-c","import time; time.sleep(45)"])
(root/"worker.json").write_text(json.dumps({"pid":os.getpid(),"child":child.pid}))
deadline=time.monotonic()+30
while not (root/"release").exists() and time.monotonic()<deadline: time.sleep(.05)
'''

LAUNCHER = '''import json,pathlib,sys,time
root=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(root/"tools/scripts"))
from gateway_edge_systemd_job import launch
from manage_gateway_edge_host import Host
pid=launch(sys.argv[2],root/"job.log",["/usr/bin/python3","-I","-B",str(root/"worker.py"),str(root)],Host().run)
(root/"launcher.json").write_text(json.dumps({"pid":pid}))
time.sleep(30)
'''


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0
                     and os.environ.get("GATEWAY_PAIR_TEST_SYSTEMD_LIVE") == "1", "Opt in to a temporary local systemd worker fixture")
class LiveSystemdTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value: return value
            time.sleep(.05)
        self.fail("Local systemd fixture observation timed out")

    def run_fixture(self, *, kill_worker=False):
        machine = host.Host()
        self.assertEqual(machine.run(["/usr/bin/systemctl", "is-active", "init.scope"]).strip(), "active")
        job_id = uuid4().hex
        with tempfile.TemporaryDirectory(prefix="platform-entry-systemd-", dir="/run") as temporary:
            root = Path(temporary); (root / "tools/scripts").mkdir(parents=True)
            for name, source in pair.tool_files().items():
                (root / "tools/scripts" / name).write_text(source, encoding="utf-8", newline="\n")
            (root / "worker.py").write_text(WORKER); (root / "launcher.py").write_text(LAUNCHER)
            launcher = subprocess.Popen(["/usr/bin/python3", "-I", "-B", str(root / "launcher.py"), str(root), job_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                self.wait_for(lambda: (root / "launcher.json").exists() and (root / "worker.json").exists())
                launched = json.loads((root / "launcher.json").read_text()); worker = json.loads((root / "worker.json").read_text())
                self.assertEqual(launched["pid"], worker["pid"])
                worker_identity = agent.process_identity(worker["pid"]); child_identity = agent.process_identity(worker["child"])
                self.assertIsNotNone(worker_identity); self.assertIsNotNone(child_identity)
                observed = jobs.properties(job_id, machine.run)
                self.assertEqual(observed["ControlGroup"], "/system.slice/" + jobs.unit_name(job_id))
                self.assertIn(observed["ControlGroup"], Path("/proc/" + str(worker["child"]) + "/cgroup").read_text())
                self.assertIn("NoNewPrivs:\t1", Path("/proc/" + str(worker["pid"]) + "/status").read_text())
                self.assertIsNone(jobs.termination(job_id, machine.run))
                # A fixture-only host receipt exercises real locks and atomic
                # recovery writes without installing any entry/application unit.
                instance = agent.Agent(host.Host(state=root / "state", active=root / "active", units=root / "units"))
                instance.jobs().mkdir(parents=True)
                owner = {"name": "fixture-entry", "runtime": "raw", "host": "edge-a"}
                lease = {"schema_version": 1, "operation_id": "1" * 32, "plan_sha256": "2" * 64, "target": "3" * 64, "owner": owner}
                state = {"schema_version": 1, "owner": owner, "stable": None, "previous": None, "pending": None, "releases": {},
                    "coordination": {key: lease[key] for key in ("operation_id", "plan_sha256", "target")}}
                host.atomic_write_text(instance.machine.state / "state.json", agent.canonical(state))
                host.atomic_write_text(instance.machine.state / "pair-reservation.json", agent.canonical(lease))
                request = {"verb": "recover", "operation_id": lease["operation_id"], "plan_sha256": lease["plan_sha256"], "target": lease["target"],
                           "host": owner["host"], "job_id": job_id}
                host.atomic_write_text(instance.job_path(job_id), agent.canonical({"schema_version": 1, "request": {**request, "verb": "start"},
                    "state": "running", "launcher": agent.process_identity(launcher.pid), "process": worker_identity, "result": None}))
                self.assertEqual(instance.recover(request, lease)["state"], "running")
                original_state = (instance.machine.state / "state.json").read_bytes()
                # Kill the complete launcher session, as loss of an administrative
                # connection can do. PID 1's separately owned job must survive it.
                os.killpg(launcher.pid, signal.SIGKILL); launcher.wait(timeout=5)
                self.assertEqual(agent.process_identity(worker["pid"]), worker_identity)
                self.assertEqual(agent.process_identity(worker["child"]), child_identity)
                self.assertEqual(jobs.properties(job_id, machine.run)["ActiveState"], "active")
                if kill_worker:
                    self.assertEqual(agent.process_identity(worker["pid"]), worker_identity)
                    os.kill(worker["pid"], signal.SIGKILL)
                else: (root / "release").touch()
                proof = self.wait_for(lambda: jobs.termination(job_id, machine.run))
                self.assertTrue(proof["termination_verified"]); self.assertTrue(proof["cgroup_empty"])
                self.assertNotEqual(agent.process_identity(worker["pid"]), worker_identity)
                self.assertNotEqual(agent.process_identity(worker["child"]), child_identity)
                self.assertEqual((root / "job.log").stat().st_mode & 0o777, 0o600)
                recovered = instance.recover(request, lease)
                self.assertEqual(recovered["state"], "failed")
                self.assertTrue(recovered["result"]["termination_verified"])
                self.assertFalse(recovered["result"]["ha_verified"])
                self.assertEqual((instance.machine.state / "state.json").read_bytes(), original_state)
                self.assertEqual(host.pair_reservation(instance.machine.state, owner), lease)
            finally:
                if launcher.poll() is None:
                    os.killpg(launcher.pid, signal.SIGKILL); launcher.wait(timeout=5)
                # This name is a random fixture-owned transient unit, never an
                # application/cluster service. Its files are removed afterwards.
                pair.run_bounded(["/usr/bin/systemctl", "stop", jobs.unit_name(job_id)], input="", text=True, timeout=15,
                    output_max_bytes=65536, check=False)
                self.wait_for(lambda: jobs.properties(job_id, machine.run)["LoadState"] == "not-found")

    def test_job_survives_launcher_session_death_and_cleans_its_descendants(self):
        self.run_fixture()

    def test_killed_worker_needs_verified_cgroup_termination_before_recovery(self):
        self.run_fixture(kill_worker=True)


if __name__ == "__main__": unittest.main()
