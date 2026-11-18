#!/usr/bin/env python3
"""Actual terminated-worker observation and explicit single retry in owned VMs."""

import json
import shlex
import time

import gateway_edge_pair_agent as agent
from test_gateway_edge_pair_recovery_fixture import arguments, finish, launch, result_from_log, snapshot, stop_controller


OBSERVE = r'''import json,pathlib,re,sys
sys.path.insert(0,"/opt/platform/edge-tools/scripts")
import gateway_edge_pair_agent as agent
operation,job_id,target=sys.argv[1:]
assert re.fullmatch("[a-f0-9]{32}",operation) and re.fullmatch("[a-f0-9]{32}",job_id) and re.fullmatch("[a-f0-9]{64}",target)
instance=agent.Agent();job=instance.job_read(job_id)
assert job["request"]["operation_id"]==operation and job["request"]["target"]==target and job["request"]["host"]=="edge-b"
request={**job["request"],"verb":"status"}
instance.context(request)
live={key:agent.signature_live(job[key]) for key in ["launcher","process"]}
try:
 props=agent.systemd_job.properties(job_id,instance.machine.run)
 proof=agent.systemd_job.termination(job_id,instance.machine.run)
except (OSError,ValueError):
 print(json.dumps({"termination_observed":False,"recorded_processes_live":live}));sys.exit(0)
print(json.dumps({"termination_observed":not any(live.values()) and proof is not None,"recorded_processes_live":live,
 "unit_properties":props,"termination_proof":proof,"job":job,"status":instance.status(request)}))
'''


def recover_terminated(test, context, selected, plan_path, directory, binary, original_proof, remote, evidence):
    peer = next(node for node in context["nodes"] if node["name"] == "edge-b")
    ssh = context["ssh"]; stopped = evidence["snapshots"]["stopped"]; before = evidence["snapshots"]["before"]
    original_job_id = evidence["original_job_id"]
    signal_request = remote("terminate", original_proof)
    test.assertTrue(signal_request["termination_requested"])
    test.assertEqual(signal_request["process"], evidence["paused_job"]["process"])
    def observe():
        command = shlex.join(["sudo", "/usr/bin/python3", "-I", "-B", "-c", OBSERVE, original_proof["operation_id"], original_job_id, selected["edge_contract_sha256"]])
        return json.loads(ssh(peer, command, seconds=15).stdout)
    deadline = time.monotonic() + 20
    while True:
        dead = observe()
        if dead["termination_observed"]: break
        if time.monotonic() >= deadline: raise TimeoutError("Original unit and cgroup did not verify termination")
        time.sleep(0.2)
    test.assertEqual(dead["recorded_processes_live"], {"launcher": False, "process": False})
    test.assertEqual(dead["status"]["state"], "unverified")
    test.assertEqual(dead["job"]["state"], "running")
    test.assertTrue(dead["termination_proof"]["termination_verified"])
    test.assertTrue(dead["termination_proof"]["cgroup_empty"])
    context["write"](directory.with_name(directory.name + "-original-unverified-job.json"), agent.canonical(dead["job"]), 0o600)
    ordinary_log = directory.with_name(directory.name + "-ordinary-dead-resume.log")
    ordinary, ordinary_stream = launch(arguments(plan_path, context["public"], "updated", directory, binary, "resume", wait_seconds=5), ordinary_log)
    recovery = None; recovered_stream = None
    try:
        ordinary_code = finish(ordinary, ordinary_stream, seconds=60)
        test.assertNotEqual(ordinary_code, 0)
        unchanged = json.loads((directory / "pair-proof.json").read_text())
        test.assertEqual(unchanged["phase"], "failed"); test.assertEqual(unchanged["failed_at"], "waiting-for-job")
        test.assertEqual(unchanged["peers"], original_proof["peers"])
        test.assertEqual(unchanged["operation_id"], original_proof["operation_id"])
        still_dead = observe()
        test.assertTrue(still_dead["termination_observed"]); test.assertEqual(still_dead["status"]["state"], "unverified")
        test.assertEqual(still_dead["job"], dead["job"])
        after_ordinary = snapshot(context)
        for host in before:
            test.assertEqual(after_ordinary[host]["job_ids"], stopped[host]["job_ids"])
            test.assertEqual(after_ordinary[host]["reservation"], stopped[host]["reservation"])
        test.assertEqual(after_ordinary["edge-a"]["services"], before["edge-a"]["services"])
        recovered_log = directory.with_name(directory.name + "-explicit-terminated-recovery.log")
        command = arguments(plan_path, context["public"], "updated", directory, binary, "resume", recover_terminated_jobs=True)
        recovery, recovered_stream = launch(command, recovered_log)
        test.assertEqual(finish(recovery, recovered_stream), 0)
        completed = json.loads((directory / "pair-proof.json").read_text())
        test.assertEqual(completed["phase"], "completed")
        test.assertEqual(completed["operation_id"], original_proof["operation_id"])
        replacement_id = completed["peers"]["edge-b"]["job_id"]
        test.assertNotEqual(replacement_id, original_job_id)
        test.assertRegex(replacement_id, r"^[a-f0-9]{32}$")
        read_job = lambda job_id: json.loads(ssh(peer, "sudo cat /var/lib/platform-gateway-edge/pair-jobs/" + job_id + ".json").stdout)
        original = read_job(original_job_id); replacement = read_job(replacement_id)
        test.assertEqual(original["state"], "failed")
        test.assertEqual(original["result"]["error_code"], "job-termination-verified")
        test.assertTrue(original["result"]["termination_verified"]); test.assertTrue(original["result"]["cgroup_empty"])
        test.assertEqual(original["process"], evidence["paused_job"]["process"])
        test.assertEqual(original["request"], dead["job"]["request"])
        test.assertEqual(replacement["state"], "completed"); test.assertTrue(replacement["result"]["host_ready"])
        test.assertNotEqual(replacement["process"], original["process"])
        test.assertEqual(replacement["request"]["operation_id"], original_proof["operation_id"])
        context["write"](directory.with_name(directory.name + "-edge-b-terminated-job.json"), agent.canonical(original), 0o600)
        after = snapshot(context)
        for host in before:
            test.assertEqual(len(after[host]["job_ids"]), len(before[host]["job_ids"]) + (2 if host == "edge-b" else 1))
            test.assertIsNone(after[host]["reservation"])
        test.assertIn(original_job_id, after["edge-b"]["job_ids"]); test.assertIn(replacement_id, after["edge-b"]["job_ids"])
        evidence.update(termination_signal_request=signal_request, original_termination_observed=dead,
            ordinary_dead_resume_return_code=ordinary_code, original_job_after_ordinary_dead_resume=still_dead,
            original_worker_terminated=original, replacement_worker_completed=replacement,
            replacement_job_id=replacement_id, explicit_terminated_recovery_verified=True,
            no_new_job_during_ordinary_dead_resume=True, explicit_replacement_job_count=1)
        evidence["snapshots"].update(after_ordinary_dead_resume=after_ordinary, after=after)
        return result_from_log(recovered_log), evidence
    finally:
        for process, stream in [(ordinary, ordinary_stream), (recovery, recovered_stream)]:
            if process is not None and process.poll() is None:
                signature = agent.process_identity(process.pid)
                if signature: stop_controller(test, process, signature)
            if stream is not None: stream.close()
