#!/usr/bin/env python3
"""Real coordinator loss and same-job resume in the owned disposable guests."""

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import gateway_edge_pair_agent as agent
import manage_gateway_edge_pair as pair
from bounded_file import read_bounded_bytes


REMOTE = r'''import json,os,pathlib,re,subprocess,sys,time
action,operation,job_id,target=sys.argv[1:]
assert action in {"stop","continue","inspect","terminate"}
assert re.fullmatch("[a-f0-9]{32}",operation) and re.fullmatch("[a-f0-9]{32}",job_id) and re.fullmatch("[a-f0-9]{64}",target)
root=pathlib.Path("/var/lib/platform-gateway-edge")
path=root/"pair-jobs"/(job_id+".json")
if not path.exists():
 print(json.dumps({"ready":False}));sys.exit(0)
job=json.loads(path.read_text())
assert job["request"]["operation_id"]==operation and job["request"]["job_id"]==job_id and job["request"]["target"]==target and job["request"]["host"]=="edge-b"
unit="platform-entry-job-"+job_id+".service"
value=subprocess.run(["systemctl","show",unit,"--property=LoadState,ActiveState,MainPID,ControlGroup,InvocationID"],capture_output=True,text=True,timeout=5)
props=dict(row.split("=",1) for row in value.stdout.splitlines() if "=" in row)
signature=job["process"]
def identity(pid):
 try:
  parts=(pathlib.Path("/proc")/str(pid)/"stat").read_text().rsplit(") ",1)[1].split()
  if parts[0]=="Z":return None
  return {"pid":pid,"start_ticks":int(parts[19]),"boot_id":pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
 except FileNotFoundError:return None
ready=(isinstance(signature,dict) and identity(signature["pid"])==signature and job["state"]=="running" and props.get("ActiveState")=="active" and props.get("MainPID")==str(signature["pid"]) and props.get("ControlGroup")=="/system.slice/"+unit)
if not ready:
 print(json.dumps({"ready":False,"job_state":job["state"]}));sys.exit(0)
group=pathlib.Path("/sys/fs/cgroup")/props["ControlGroup"].lstrip("/")
def members():
 result=[]
 for pid in (group/"cgroup.procs").read_text().split():
  try:
   fields=(pathlib.Path("/proc")/pid/"stat").read_text().rsplit(") ",1)[1].split()
   if fields[0]!="Z":result.append({"identity":identity(int(pid)),"state":fields[0]})
  except FileNotFoundError:pass
 return result
if action in {"stop","continue","terminate"}:
 subprocess.run(["systemctl","kill","--kill-who=all","--signal="+{"stop":"SIGSTOP","continue":"SIGCONT","terminate":"SIGKILL"}[action],unit],check=True,capture_output=True,timeout=5)
 if action=="terminate":
  print(json.dumps({"termination_requested":True,"operation_id":operation,"job_id":job_id,"target":target,"process":signature,"unit":unit,"properties_before_signal":props}));sys.exit(0)
 if action=="stop":
  deadline=time.monotonic()+3
  while True:
   rows=members()
   if rows and all(row["state"] in {"T","t"} for row in rows):break
   assert time.monotonic()<deadline
   time.sleep(0.02)
rows=members()
print(json.dumps({"ready":True,"action":action,"operation_id":operation,"job_id":job_id,"target":target,"process":signature,"unit":unit,"properties":props,"members":rows}))
'''


SNAPSHOT = r'''import json,pathlib,subprocess
root=pathlib.Path("/var/lib/platform-gateway-edge")
state=json.loads((root/"state.json").read_text())
lease=root/"pair-reservation.json"
jobs=sorted(p.stem for p in (root/"pair-jobs").glob("*.json"))
result={"stable":state["stable"],"pending":state["pending"],"job_ids":jobs,"reservation":json.loads(lease.read_text()) if lease.exists() else None,"services":{}}
for role in ["haproxy","keepalived"]:
 unit="edge-"+state["owner"]["host"]+"-"+role+".service"
 value=subprocess.run(["systemctl","show",unit,"--property=InvocationID,MainPID,ActiveState"],capture_output=True,text=True,check=True,timeout=5)
 result["services"][role]=dict(row.split("=",1) for row in value.stdout.splitlines() if "=" in row)
print(json.dumps(result))
'''


def snapshot(context):
    return {node["name"]: json.loads(context["ssh"](node, "sudo /usr/bin/python3 -I -B -c " + shlex.quote(SNAPSHOT)).stdout) for node in context["nodes"]}


def arguments(plan_path, public, label, directory, binary, mode, *, wait_seconds=90, recover_terminated_jobs=False):
    return [sys.executable, "-B", str(Path(pair.__file__).resolve()), "--plan", str(plan_path), "--bundle-dir", str(public / label),
        "--gateway-bundle-dir", str(public / "gateway"), "--output-dir", str(directory), "--ssh-binary", str(binary),
        "--ca-file", str(public / "ca.crt"), "--artifacts-dir", str(public / "artifacts"), "--distribute-inputs", "--wait-seconds", str(wait_seconds), "--" + mode,
        *(["--recover-terminated-jobs"] if recover_terminated_jobs else [])]


def launch(command, path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    log = os.fdopen(descriptor, "wb")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return process, log


def finish(process, log, *, seconds=120):
    try: code = process.wait(timeout=seconds)
    finally: log.close()
    return code


def stop_controller(test, process, signature):
    if process.poll() is not None: return
    test.assertEqual(agent.process_identity(process.pid), signature)
    os.kill(process.pid, signal.SIGSTOP)
    test.assertEqual(agent.process_identity(process.pid), signature)
    os.kill(process.pid, signal.SIGKILL)
    test.assertEqual(process.wait(timeout=10), -signal.SIGKILL)


def result_from_log(path):
    data = read_bounded_bytes(path, max_bytes=1024 ** 2).decode()
    return json.loads(data.splitlines()[-1])


def interrupted_update(test, context, selected, plan_path, directory, binary, *, terminate_worker=False):
    before = snapshot(context)
    command = arguments(plan_path, context["public"], "updated", directory, binary, "apply")
    controller, log = launch(command, directory.with_name(directory.name + "-interrupted.log"))
    original = agent.process_identity(controller.pid)
    test.assertIsNotNone(original)
    peer = next(node for node in context["nodes"] if node["name"] == "edge-b")
    paused = None; uncertain = None; recovery = None; extra = []; children = []
    def remote(action, proof):
        job_id = proof["peers"]["edge-b"]["job_id"]
        test.assertRegex(job_id, r"^[a-f0-9]{32}$")
        command = shlex.join(["sudo", "/usr/bin/python3", "-I", "-B", "-c", REMOTE, action, proof["operation_id"], job_id, selected["edge_contract_sha256"]])
        return json.loads(context["ssh"](peer, command, seconds=15).stdout)
    try:
        deadline = time.monotonic() + 75
        while True:
            test.assertIsNone(controller.poll(), "The original coordinator ended before the controlled interruption")
            try: proof = json.loads((directory / "pair-proof.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError): proof = {}
            if proof.get("peers", {}).get("edge-b", {}).get("job_id"):
                paused = remote("stop", proof)
                if paused["ready"]: break
            if time.monotonic() >= deadline: raise TimeoutError("No original running B job was observed for the controlled fault")
            time.sleep(0.05)
        test.assertIsNone(proof["peers"]["edge-a"]["job_id"])
        test.assertTrue(paused["members"])
        test.assertTrue(all(row["state"] in {"T", "t"} for row in paused["members"]))
        for value in (Path("/proc") / str(controller.pid) / "task" / str(controller.pid) / "children").read_text().split():
            signature = agent.process_identity(int(value))
            if signature: children.append(signature)
        stop_controller(test, controller, original); log.close()
        deadline = time.monotonic() + 25
        while any(agent.signature_live(signature) for signature in children):
            if time.monotonic() >= deadline: raise TimeoutError("An original coordinator SSH child did not drain")
            time.sleep(0.1)
        test.assertIsNone(agent.process_identity(original["pid"]))
        stopped = snapshot(context)
        for host, value in stopped.items():
            test.assertEqual(value["reservation"]["operation_id"], proof["operation_id"])
            test.assertEqual(value["reservation"]["target"], selected["edge_contract_sha256"])
        test.assertEqual(stopped["edge-a"]["services"], before["edge-a"]["services"])
        test.assertEqual(stopped["edge-a"]["job_ids"], before["edge-a"]["job_ids"])
        uncertain_log = directory.with_name(directory.name + "-uncertain-resume.log")
        uncertain, uncertain_stream = launch(arguments(plan_path, context["public"], "updated", directory, binary, "resume", wait_seconds=5), uncertain_log)
        extra.append((uncertain, uncertain_stream)); code = finish(uncertain, uncertain_stream, seconds=60)
        test.assertNotEqual(code, 0)
        same = json.loads((directory / "pair-proof.json").read_text())
        test.assertEqual(same["phase"], "failed")
        test.assertEqual(same["failed_at"], "waiting-for-job")
        test.assertEqual(same["error_code"], "pair-operation-incomplete")
        test.assertEqual(same["operation_id"], proof["operation_id"])
        test.assertEqual(same["peers"], proof["peers"])
        still = remote("inspect", proof)
        test.assertTrue(still["ready"]); test.assertEqual(still["process"], paused["process"])
        test.assertTrue(all(row["state"] in {"T", "t"} for row in still["members"]))
        unchanged = snapshot(context)
        for host in before:
            test.assertEqual(unchanged[host]["job_ids"], stopped[host]["job_ids"])
            test.assertEqual(unchanged[host]["reservation"], stopped[host]["reservation"])
        test.assertEqual(unchanged["edge-a"]["services"], before["edge-a"]["services"])
        if terminate_worker:
            from test_gateway_edge_pair_terminated_fixture import recover_terminated
            evidence = {"original_controller": original, "original_controller_killed": True,
                "original_ssh_children_drained": children, "paused_job": paused, "uncertain_resume_return_code": code,
                "uncertain_resume_wait_seconds": 5, "uncertain_resume_failure": {key: same[key] for key in ("failed_at", "error_code")},
                "original_job_after_uncertain_resume": still, "other_host_unchanged_while_uncertain": True,
                "no_new_job_during_uncertain_resume": True, "operation_id": proof["operation_id"], "original_job_id": paused["job_id"],
                "snapshots": {"before": before, "stopped": stopped, "uncertain": unchanged}}
            return recover_terminated(test, context, selected, plan_path, directory, binary, proof, remote, evidence)
        continued = remote("continue", proof)
        test.assertTrue(continued["ready"]); test.assertEqual(continued["process"], paused["process"])
        resumed_log = directory.with_name(directory.name + "-recovered.log")
        recovery, resumed_stream = launch(arguments(plan_path, context["public"], "updated", directory, binary, "resume"), resumed_log)
        extra.append((recovery, resumed_stream)); test.assertEqual(finish(recovery, resumed_stream), 0)
        completed = json.loads((directory / "pair-proof.json").read_text())
        test.assertEqual(completed["phase"], "completed")
        test.assertEqual(completed["operation_id"], proof["operation_id"])
        test.assertEqual(completed["peers"]["edge-b"]["job_id"], paused["job_id"])
        job = json.loads(context["ssh"](peer, "sudo cat /var/lib/platform-gateway-edge/pair-jobs/" + paused["job_id"] + ".json").stdout)
        test.assertEqual(job["state"], "completed"); test.assertEqual(job["process"], paused["process"])
        after = snapshot(context)
        for host in before:
            test.assertEqual(len(after[host]["job_ids"]), len(before[host]["job_ids"]) + 1)
            test.assertIsNone(after[host]["reservation"])
        return result_from_log(resumed_log), {"original_controller": original, "original_controller_killed": True,
            "original_ssh_children_drained": children, "paused_job": paused, "uncertain_resume_return_code": code,
            "uncertain_resume_wait_seconds": 5,
            "uncertain_resume_failure": {key: same[key] for key in ("failed_at", "error_code")},
            "original_job_after_uncertain_resume": still, "continued_job": continued,
            "original_worker_completed": job, "other_host_unchanged_while_uncertain": True,
            "no_new_job_during_uncertain_resume": True, "operation_id": proof["operation_id"],
            "original_job_id": paused["job_id"], "snapshots": {"before": before, "stopped": stopped, "uncertain": unchanged, "after": after}}
    finally:
        for process, stream in [(controller, log), *extra]:
            if process.poll() is None:
                signature = agent.process_identity(process.pid)
                if signature: stop_controller(test, process, signature)
            stream.close()
        if not terminate_worker and paused and paused.get("ready") and (recovery is None or recovery.poll() is None):
            # Restore only the exact original stopped laboratory job; this is
            # not a new start and never treats a stopped process as terminal.
            try: remote("continue", proof)
            except (OSError, ValueError, AssertionError): pass


def completed_resume(test, context, plan_path, label, directory, binary):
    before = snapshot(context)
    original = json.loads((directory / "pair-proof.json").read_text())
    log_path = directory.with_name(directory.name + "-completed-resume.log")
    process, log = launch(arguments(plan_path, context["public"], label, directory, binary, "resume"), log_path)
    try: test.assertEqual(finish(process, log), 0)
    finally:
        if process.poll() is None:
            signature = agent.process_identity(process.pid)
            if signature: stop_controller(test, process, signature)
        log.close()
    after = snapshot(context)
    resumed = json.loads((directory / "pair-proof.json").read_text())
    test.assertEqual(resumed["operation_id"], original["operation_id"])
    test.assertEqual(resumed["peers"], original["peers"])
    test.assertEqual(before, after)
    return {"operation_id": original["operation_id"], "same_original_job_ids": True, "no_service_restart": True,
        "snapshots": {"before": before, "after": after}, "result": result_from_log(log_path)}
