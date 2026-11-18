#!/usr/bin/env python3
"""Actual staged SSH pair coordination in the owned disposable edge guests."""

import copy
import json
import os
from pathlib import Path
import shlex

import gateway_edge_pair_agent as agent
import manage_gateway_edge_pair as pair
from test_gateway_edge_host_vm_fixture import sha


PROVISION_YAML = '''import pathlib,shutil,site
paths=[pathlib.Path(p) for p in site.getsitepackages() if p.startswith("/usr/local/lib/") and p.endswith("/dist-packages")]
assert len(paths)==1
paths[0].mkdir(parents=True,exist_ok=True)
shutil.copytree("/mnt/fixtures/scripts/yaml",paths[0]/"yaml")
'''


def workload(test, context, *, recovery=False, terminate_worker=False):
    if type(recovery) is not bool or type(terminate_worker) is not bool or terminate_worker and not recovery:
        raise ValueError("Select an explicit interruption-recovery laboratory profile")
    root, public, nodes = (context[k] for k in ("root", "public", "nodes"))
    ssh, write = (context[k] for k in ("ssh", "write"))
    if not root.name.startswith("platform-k3s-vm-") or root.parent != Path("/var/tmp"):
        raise ValueError("Use only the original disposable edge guests")
    sources = pair.tool_files(); tools = pair.manifest(sources)
    test.assertLess(len(tools), 100)
    binary = Path("/usr/bin/ssh"); binary_sha = sha(binary)
    peers = []
    for index, node in enumerate(nodes):
        # This dependency belongs to the disposable guest, outside the exact
        # remote tool inventory. Isolated Python must find the same serializer.
        ssh(node, "sudo /usr/bin/python3 -I -B -c " + shlex.quote(PROVISION_YAML))
        test.assertEqual(ssh(node, "sudo /usr/bin/python3 -I -B -c 'import yaml; print(yaml.__version__)'").stdout.strip(), "6.0.3")
        machine = ssh(node, "sudo /usr/bin/python3 -I -B -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path(\"/etc/machine-id\").read_bytes().strip()).hexdigest())'").stdout.strip()
        test.assertRegex(machine, r"^[0-9a-f]{64}$")
        known = root / (node["name"] + ".known-hosts")
        line = next(line for line in (root / "known-hosts").read_text().splitlines() if line.startswith(node["address"] + " "))
        write(known, line + "\n", 0o600)
        key_env, known_env = "FIXTURE_EDGE_" + str(index) + "_KEY", "FIXTURE_EDGE_" + str(index) + "_HOSTS"
        if key_env in os.environ or known_env in os.environ: raise ValueError("Do not replace existing credential references")
        os.environ[key_env], os.environ[known_env] = str(root / "client-key"), str(known)
        peers.append({"host": node["name"], "address": node["address"], "port": 22, "machine_sha256": machine,
            "identity_env": key_env, "known_hosts_env": known_env, "remote_root": "/opt/platform/edge-tools", "paths": {}, "expected_current": "absent"})
    test.assertEqual(len({peer["machine_sha256"] for peer in peers}), 2)
    plan = {"schema_version": 1, "name": "platform-entry", "edge_contract_sha256": context["releases"][0],
        "toolset_sha256": agent.fingerprint(tools), "ssh_binary_sha256": binary_sha, "peers": peers}
    evidence_root = Path(os.environ["GATEWAY_EDGE_HOST_VM_REPORT"]).with_suffix("")
    evidence_root.mkdir(mode=0o700, exist_ok=False)
    phases = []; resumes = []; fault = None
    try:
        for label, mode, expected in (("initial", "apply", "absent"), ("updated", "apply", context["releases"][0]), ("initial", "rollback", context["releases"][1])):
            selected = copy.deepcopy(plan)
            selected["edge_contract_sha256"] = context["releases"][0 if label == "initial" else 1]
            for peer in selected["peers"]:
                peer["expected_current"] = expected
                peer["paths"] = {"bundle": "/opt/platform/" + label + "-entry", "gateway": "/opt/platform/" + label + "-gateway",
                    "ca": "/opt/platform/" + label + "-ca.crt", "artifacts": "/opt/platform/" + label + "-artifacts"}
            directory = evidence_root / (str(len(phases) + 1) + "-" + mode)
            plan_path = evidence_root / (directory.name + "-plan.json")
            write(plan_path, agent.canonical(selected), 0o600)
            print(json.dumps({"real_pair_phase": mode, "label": label, "stage": "started"}), flush=True)
            if recovery and label == "updated":
                from test_gateway_edge_pair_recovery_fixture import interrupted_update
                result, fault = interrupted_update(test, context, selected, plan_path, directory, binary, terminate_worker=terminate_worker)
            else:
                result = pair.operate(selected, public / label, public / "gateway", mode=mode, output=directory,
                    ssh_binary=binary, ca_file=public / "ca.crt", artifacts=public / "artifacts", distribute_inputs=True, wait_seconds=90)
            test.assertTrue(result["pair_services_ready"]); test.assertTrue(result["vip_reachable"])
            if recovery:
                from test_gateway_edge_pair_recovery_fixture import completed_resume
                resumes.append(completed_resume(test, context, plan_path, label, directory, binary))
            proof = json.loads((directory / "pair-proof.json").read_text())
            test.assertEqual(proof["phase"], "completed")
            test.assertNotIn("error_code", proof); test.assertNotIn("failed_at", proof)
            test.assertEqual(proof["job_state"], "completed")
            if recovery and label == "updated": test.assertEqual(proof["last_failure"], fault["uncertain_resume_failure"])
            test.assertTrue(all(peer["released"] for peer in proof["peers"].values()))
            jobs = []
            for node in nodes:
                job_id = proof["peers"][node["name"]]["job_id"]
                test.assertRegex(job_id, r"^[a-f0-9]{32}$")
                job = json.loads(ssh(node, "sudo cat /var/lib/platform-gateway-edge/pair-jobs/" + job_id + ".json").stdout)
                test.assertEqual(job["state"], "completed"); test.assertTrue(job["result"]["host_ready"])
                test.assertEqual(job["request"]["operation_id"], proof["operation_id"])
                test.assertEqual(job["request"]["target"], selected["edge_contract_sha256"])
                write(evidence_root / (directory.name + "-" + node["name"] + "-job.json"), agent.canonical(job), 0o600)
                unit = "platform-entry-job-" + job_id + ".service"
                properties = ssh(node, "sudo systemctl show " + unit + " --property=LoadState,ActiveState,MainPID,ControlPID,ControlGroup").stdout
                props = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
                test.assertEqual(props, {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "ControlGroup": ""})
                test.assertEqual(ssh(node, "sudo test ! -e /var/lib/platform-gateway-edge/pair-reservation.json", check=False).returncode, 0)
                jobs.append({"host": node["name"], "job_id": job_id, "request_sha256": agent.fingerprint(job["request"]),
                    "record_sha256": agent.fingerprint(job), "launcher": job["launcher"], "process": job["process"], "unit_cleanup": props})
            phases.append({"mode": mode, "label": label, "target": selected["edge_contract_sha256"], "operation_id": proof["operation_id"],
                "proof_path": str(directory / "pair-proof.json"), "proof_sha256": sha(directory / "pair-proof.json"), "jobs": jobs})
            print(json.dumps({"real_pair_phase": mode, "label": label, "stage": "completed"}), flush=True)
        records = []
        for node in nodes:
            state = json.loads(ssh(node, "sudo cat /var/lib/platform-gateway-edge/state.json").stdout)
            test.assertEqual(state["stable"], context["releases"][0]); test.assertEqual(state["previous"], context["releases"][1])
            test.assertIsNone(state["pending"]); test.assertEqual(len(state["releases"]), 2)
            records.append({"host": node["name"], "initial_contract_sha256": context["releases"][0], "retained_release_count": 2, "rollback_verified": True})
        # Observe all real routes with the production verifier, after both hosts rolled back.
        contract = json.loads((public / "initial/edge-contract.json").read_text())
        test.assertTrue(pair.probe(contract, public / "ca.crt")["vip_reachable"])
        test.assertEqual(len({job["job_id"] for phase in phases for job in phase["jobs"]}), 6)
        return records, {"pair_coordinator_verified": True, "real_pair_staging_verified": True, "real_pair_systemd_jobs_verified": True,
            "pair_operation_phases": phases, "pair_tool_files_sha256": tools, "pair_ssh_binary_sha256": binary_sha,
            "pair_machine_fingerprints": {peer["host"]: peer["machine_sha256"] for peer in peers}, "pair_evidence_root": str(evidence_root),
            "completed_pair_resume_verified": recovery, "coordinator_loss_recovery_verified": recovery,
            "uncertain_resume_preserves_original_job_verified": recovery, "terminated_job_recovery_verified": terminate_worker,
            "pair_completed_resumes": resumes, "pair_interruption_recovery": fault}
    except BaseException:
        for node in nodes:
            logs = ssh(node, "sudo find /var/lib/platform-gateway-edge/pair-jobs -maxdepth 1 -name '*.log' -exec tail -n 12 {} +", check=False, seconds=10)
            print(json.dumps({"synthetic_guest_pair_job_logs": logs.stdout[-5000:]}), flush=True)
        raise
    finally:
        for peer in peers:
            os.environ.pop(peer["identity_env"], None); os.environ.pop(peer["known_hosts_env"], None)
