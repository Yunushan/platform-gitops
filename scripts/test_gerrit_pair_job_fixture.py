#!/usr/bin/env python3
"""Real PID-1 Gerrit pair job persistence; host/application actions are modeled."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest import mock
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_subprocess import run_bounded
import activate_gerrit_host as activation
import gerrit_pair_agent as agent
import manage_gerrit_pair as pair
from test_gerrit_pair import fixture, proof, TOOLS


def backend(root):
    payload = json.loads((root / "input.json").read_bytes())
    member = root / "private/gerrit-pair-agent/gerrit-a"
    machine = activation.host_tools.Host()
    machine.prerequisites = lambda *args: {"modeled_gerrit_prerequisites": True}
    machine.existing = lambda *args: None
    class FixtureAgent(agent.Agent):
        def context(self, request):
            if (request["operation_id"] != payload["request"]["operation_id"] or request["host_plan"] != payload["request"]["host_plan"]
                    or request["plan_sha256"] != payload["request"]["plan_sha256"]): raise ValueError("Fixture request changed")
            return machine, member, payload["lease"]
    return FixtureAgent(machine), payload


def child(role, root):
    root = Path(root); agent.ROOT = root
    current, payload = backend(root)
    request = payload["request"]
    if role == "launch":
        launch = agent.jobs.launch
        def actual_launch(job_id, log, command, run):
            return launch(job_id, log, [sys.executable, "-B", str(Path(__file__).resolve()), "--child", "worker", str(root)], run)
        with mock.patch.object(agent.jobs, "launch", side_effect=actual_launch):
            result = current.rpc({**request, "verb": "start"})
        print(json.dumps(result)); return
    if role != "worker": raise ValueError("Unknown fixture child")
    with mock.patch.object(agent.activation, "operate", return_value=proof()):
        current.execute("gerrit-a", request["operation_id"], request["job_id"])
    atomic_write_text(root / "worker-completed", "completed\n")
    deadline = time.monotonic() + 15
    while not (root / "worker-may-exit").exists():
        if time.monotonic() >= deadline: raise TimeoutError("Fixture observation deadline")
        time.sleep(0.05)


def exercise(test):
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").is_dir(): raise ValueError("Use an explicit disposable root Linux systemd runner")
    root = Path(tempfile.mkdtemp(prefix="platform-gerrit-pair-job-", dir="/run")); root.chmod(0o700)
    identity = (root.stat().st_dev, root.stat().st_ino)
    for relative in ("private", "private/gerrit-pair-agent", "private/gerrit-pair-agent/gerrit-a"):
        (root / relative).mkdir(mode=0o700)
    operation, job_id = uuid4().hex, uuid4().hex
    request = {"schema_version": 1, "verb": "start", "operation_id": operation, "plan_sha256": "c" * 64, "tools": TOOLS,
        "host_plan": fixture()["peers"][0]["host_plan"], "bundle": "/etc/gerrit", "job_id": job_id, "action": "activate"}
    lease = {"schema_version": 1, "operation_id": operation, "plan_sha256": "c" * 64, "host_plan_sha256": "d" * 64,
        "tools_sha256": "e" * 64, "bundle": "/etc/gerrit", "package_sha256": "f" * 64}
    atomic_write_text(root / "input.json", json.dumps({"request": request, "lease": lease}))
    current, _ = backend(root); machine = activation.host_tools.Host(); worker = None
    def status(): return current.rpc({**request, "verb": "status"})
    try:
        test.assertEqual(agent.jobs.properties(job_id, machine.run)["LoadState"], "not-found")
        current.rpc({**request, "verb": "reserve", "job_id": None, "action": None})
        launch = run_bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--child", "launch", str(root)],
            text=True, timeout=30, output_max_bytes=65536, check=False)
        test.assertEqual(launch.returncode, 0, launch.stderr)
        test.assertTrue(json.loads(launch.stdout)["accepted"])
        deadline = time.monotonic() + 15
        while not (root / "worker-completed").exists():
            if time.monotonic() >= deadline: raise TimeoutError("Original PID-1 worker did not finish its modeled action")
            time.sleep(0.05)
        base = root / "private/gerrit-pair-agent/gerrit-a" / operation
        record = json.loads((base / (job_id + ".json")).read_bytes()); worker = record["worker"]
        test.assertEqual(record["state"], "completed")
        test.assertTrue(agent.identities.signature_live(worker))
        test.assertFalse(agent.identities.signature_live(record["launcher"]))
        test.assertEqual(status()["state"], "finishing")
        test.assertIsNone(agent.jobs.termination(job_id, machine.run))
        atomic_write_text(root / "worker-may-exit", "exit\n")
        while True:
            terminal = status()
            if terminal["state"] == "completed": break
            if time.monotonic() >= deadline: raise TimeoutError("Original job cgroup did not terminate")
            time.sleep(0.05)
        test.assertTrue(terminal["termination_verified"])
        test.assertFalse(agent.identities.signature_live(worker))
        test.assertIsNotNone(agent.jobs.termination(job_id, machine.run))
        test.assertTrue(current.rpc(request)["accepted"])
        test.assertEqual(json.loads((base / (job_id + ".json")).read_bytes())["worker"], worker)
        return {"pid1_job_survives_launcher_exit": True, "live_cgroup_blocks_terminal_claim": True,
            "original_job_not_reissued": True, "original_job_termination_verified": True,
            "gerrit_runtime_verified": False, "ceph_runtime_verified": False, "ssh_transport_tested": False, "ha_verified": False}
    finally:
        atomic_write_text(root / "worker-may-exit", "exit\n")
        deadline = time.monotonic() + 20
        while agent.jobs.termination(job_id, machine.run) is None:
            if time.monotonic() >= deadline: raise ValueError("Preserve the still-live original fixture job and state")
            time.sleep(0.1)
        if worker is not None and agent.identities.signature_live(worker): raise ValueError("Preserve the remaining original worker")
        if root.is_symlink() or root.parent != Path("/run") or not root.name.startswith("platform-gerrit-pair-job-") or (root.stat().st_dev, root.stat().st_ino) != identity:
            raise ValueError("Fixture cleanup identity changed")
        shutil.rmtree(root)


def exercise_tools(test):
    if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use the explicit protected Linux source-staging test")
    files = pair.tool_files(); tools = pair.manifest(files)
    root = Path(tempfile.mkdtemp(prefix="platform-gerrit-tool-import-", dir="/run")); root.chmod(0o700)
    identity = (root.stat().st_dev, root.stat().st_ino); (root / "private").mkdir(mode=0o700)
    try:
        export = root / "private/export"
        with mock.patch.object(pair, "ROOT", root), mock.patch.object(pair, "tool_files", return_value=files):
            summary = pair.export_tools(export)
        test.assertEqual(summary["toolset_sha256"], activation.values.fingerprint(tools))
        site_plan = fixture()["site_plan"]
        expected = activation.values.fingerprint(activation.sites.render(site_plan, "raw"))
        program = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import gerrit_pair_agent as agent
inputs=json.load(sys.stdin)
agent.tool_check(inputs["tools"])
rendered=agent.activation.sites.render(inputs["plan"],"raw")
assert agent.activation.values.fingerprint(rendered)==inputs["expected"]
assert Path(agent.__file__).parent==Path(sys.argv[1])
print(json.dumps({"closed_toolset_verified":True,"runtime_package_regenerated":True,"gerrit_runtime_verified":False,"ha_verified":False}))
'''
        result = run_bounded([sys.executable, "-I", "-B", "-c", program, str(export / "scripts")],
            input=json.dumps({"tools": tools, "plan": site_plan, "expected": expected}), text=True, timeout=30, output_max_bytes=65536, check=False)
        test.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        test.assertTrue(evidence["closed_toolset_verified"])
        test.assertTrue(evidence["runtime_package_regenerated"])
        return {**evidence, "exported_source_files": len(tools)}
    finally:
        if root.is_symlink() or root.parent != Path("/run") or not root.name.startswith("platform-gerrit-tool-import-") or (root.stat().st_dev, root.stat().st_ino) != identity:
            raise ValueError("Protected source fixture cleanup identity changed")
        shutil.rmtree(root)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--child": child(sys.argv[2], sys.argv[3])
