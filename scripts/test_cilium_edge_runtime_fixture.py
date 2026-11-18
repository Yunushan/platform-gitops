#!/usr/bin/env python3
"""Actual native edge VMs in front of the original Cilium/CI/CD application."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import ssl
import time
from http.client import HTTPException

import yaml

import gateway_edge_pair_agent as agent
import manage_gateway_edge_pair as pair
import render_gateway_edge as renderer
from test_argocd_worker_workload import BODY
from test_cilium_worker_runtime_fixture import DirectTLS, stage
from test_gateway_edge_host_vm_fixture import HAPROXY_SHA, KEEPALIVED_SHA, OPTIONS_SHA, sha, stage_native_assets
from test_k3s_vm_fixture import qmp


NODES = [{"name": "edge-a", "address": "192.0.2.10", "memory": 512}, {"name": "edge-b", "address": "192.0.2.11", "memory": 512}]
VIP = "192.0.2.50"
BOOTSTRAP = '''import pathlib,shutil,site
paths=[pathlib.Path(p) for p in site.getsitepackages() if p.startswith("/usr/local/lib/") and p.endswith("/dist-packages")]
assert len(paths)==1
paths[0].mkdir(parents=True,exist_ok=True)
shutil.copytree("/mnt/fixtures/native-edge/yaml",paths[0]/"yaml")
import yaml
assert yaml.__version__=="6.0.3"
'''
GUEST_OBSERVATION = r'''import hashlib,json,pathlib,subprocess,sys
host=sys.argv[1];roles={}
for role in ["haproxy","keepalived"]:
 value=subprocess.run(["systemctl","show","edge-"+host+"-"+role+".service","--property=ActiveState,MainPID,InvocationID,NRestarts"],check=True,capture_output=True,text=True,timeout=5)
 props=dict(line.split("=",1) for line in value.stdout.splitlines() if "=" in line)
 pid=int(props["MainPID"]);assert props["ActiveState"]=="active" and pid>1
 proc=pathlib.Path("/proc")/str(pid)
 status=dict(line.split(":",1) for line in (proc/"status").read_text().splitlines() if ":" in line)
 with (proc/"exe").open("rb") as stream:digest=hashlib.file_digest(stream,"sha256").hexdigest()
 roles[role]={"properties":props,"exe_sha256":digest,"uid":int(status["Uid"].split()[0]),"cap_eff":status["CapEff"].strip(),"cap_bnd":status["CapBnd"].strip(),"no_new_privileges":status["NoNewPrivs"].strip()}
events=dict((k,int(v)) for k,v in (line.split() for line in pathlib.Path("/sys/fs/cgroup/system.slice/memory.events").read_text().splitlines()))
memory={line.split(":")[0]:int(line.split(":")[1].split()[0]) for line in pathlib.Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemTotal:","MemAvailable:","SwapTotal:"))}
kernel=subprocess.run(["journalctl","-k","-b","--no-pager","-o","cat"],capture_output=True,text=True,check=True,timeout=5).stdout.casefold()
k3s=subprocess.run(["systemctl","show","fixture-k3s.service","--property=LoadState","--value"],capture_output=True,text=True,timeout=5).stdout.strip()
print(json.dumps({"roles":roles,"memory_events":events,"memory_kib":memory,"kernel_oom_signal":any(s in kernel for s in ["out of memory: killed process","invoked oom-killer","memory cgroup out of memory"]),"k3s_unit_load_state":k3s,"boot_id":pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}))
'''


def write(path, payload, mode=0o444):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload if isinstance(payload, bytes) else payload.encode())
    path.chmod(mode)


def stage_assets(root, public, run):
    available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    if available < 13 * 1024 ** 2: raise ValueError("The seven-guest profile requires at least 13 GiB available before starting guests")
    destination = public / "native-edge"; destination.mkdir(mode=0o755)
    hp = Path(os.environ["GATEWAY_EDGE_HOST_VM_HAPROXY"]).resolve(strict=True)
    ka = Path(os.environ["GATEWAY_EDGE_HOST_VM_KEEPALIVED"]).resolve(strict=True)
    archives = stage_native_assets(destination, root / "native-edge-archives", hp, ka, run, write)
    if yaml.__version__ != "6.0.3": raise ValueError("Use the pinned renderer dependency")
    yaml_pins = {}
    for path in Path(yaml.__file__).parent.glob("*.py"):
        write(destination / "yaml" / path.name, path.read_bytes()); yaml_pins[path.name] = sha(path)
    write(destination / "bootstrap.py", BOOTSTRAP)
    public.chmod(0o755)
    for path in destination.rglob("*"):
        if path.is_dir() and not path.is_symlink(): path.chmod(0o755)
    destination.chmod(0o755)
    pins = {p.relative_to(public).as_posix(): sha(p) for p in destination.rglob("*") if p.is_file() and not p.is_symlink()}
    return {"nodes": [dict(node) for node in NODES], "public_files_sha256": pins, "package_sha256": archives,
        "yaml_sources_sha256": yaml_pins, "available_memory_before_guests_kib": available}


def configure_guest(config, client_public_key):
    config["disable_root"] = False
    config["users"].append({"name": "root", "lock_passwd": True, "ssh_authorized_keys": [client_public_key]})
    config["runcmd"][-1] = ["sh", "-ec", "cd /mnt/fixtures; sha256sum -c /etc/fixture/artifact-sums"]
    config["runcmd"].append(["sh", "-ec", "printf '/mnt/fixtures/native-edge/libraries/usr/lib/x86_64-linux-gnu\\n' > /etc/ld.so.conf.d/fixture-edge.conf; ldconfig; /usr/bin/python3 -I -B /mnt/fixtures/native-edge/bootstrap.py; touch /run/fixture-artifacts-ready"])


def verify(test, context, report, gateway_bundle, gateway_digest, pki, hostname, unchanged, denied, *,
           gateway_runtime="k3s", serving_nodes=None, expected_body=BODY, simulate_host_loss=False):
    for key in ("real_cilium_gerrit_tekton_argocd_flow_verified",):
        # The outer flow records this aggregate after application_gateway returns.
        if key in report and report[key] is not True: raise ValueError("The original application flow must remain verified")
    client, ssh = context["client"], context["ssh"]
    chosen = context["native_edge_hosts"]; nodes = chosen["nodes"]; public = chosen["public"]
    test.assertEqual(len(nodes), 2); test.assertTrue(unchanged()); test.assertTrue(denied())
    gateway_bytes = (gateway_bundle / "deployment-contract.json").read_bytes()
    original_gateway = json.loads(gateway_bytes)
    test.assertEqual(original_gateway["runtime"], gateway_runtime)
    test.assertEqual(hashlib.sha256(gateway_bytes).hexdigest(), gateway_digest)
    backend_nodes = [client.get("node/" + node["name"]) for node in
                     (context["nodes"][1:] if serving_nodes is None else serving_nodes)]
    plan = {"schema_version": 1, "name": "fixture-entry", "namespace": "fixture-entry", "gateway_contract_sha256": gateway_digest,
        "vip": VIP + "/24", "vrid": 78, "https_port": 443, "health_port": 8404, "minimum_ready_routes": 1,
        "client_cidrs": ["192.0.2.1/32"],
        "entry_hosts": [{"name": node["name"], "address": node["address"], "interface": "eth0", "failure_domain": "entry-zone-" + str(index), "node_name": None, "node_uid": None} for index, node in enumerate(nodes)],
        "backends": [{"node_name": node["metadata"]["name"], "node_uid": node["metadata"]["uid"], "address": next(a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP")} for node in backend_nodes],
        "routes": [{"hostname": hostname, "health_path": "/", "health_status": 200}], "images": None,
        "binaries": {"haproxy": {"path": renderer.BASE + "/bin/haproxy", "version": "3.2.26", "sha256": HAPROXY_SHA},
            "keepalived": {"path": renderer.BASE + "/bin/keepalived", "version": "2.3.4", "sha256": KEEPALIVED_SHA, "config_opts_sha256": OPTIONS_SHA}},
        "ca_sha256": sha(pki / "ca.crt"), "ca_secret": "native-entry-ca"}
    files = renderer.render(plan, original_gateway, "raw")
    bundle = context["root"] / "native-application-entry"
    for name, value in files.items(): write(bundle / name, value)
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    tool_files = pair.tool_files(); tools = pair.manifest(tool_files); binary = Path("/usr/bin/ssh")
    peers = []
    evidence = Path(os.environ["K3S_VM_TEST_REPORT"]).with_suffix("") / "native-application-entry"
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    stage("native-external-entry-staging")
    for index, node in enumerate(nodes):
        test.assertEqual(qmp(node["directory"] / "qmp.sock", "query-memory-size-summary")["base-memory"], 512 * 1024 ** 2)
        test.assertEqual(ssh(node, "ip -4 route show default").stdout.strip(), "")
        test.assertEqual(ssh(node, "sudo /usr/bin/python3 -I -B -c 'import yaml; print(yaml.__version__)'").stdout.strip(), "6.0.3")
        machine = ssh(node, "sudo /usr/bin/python3 -I -B -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path(\"/etc/machine-id\").read_bytes().strip()).hexdigest())'").stdout.strip()
        test.assertRegex(machine, r"^[a-f0-9]{64}$")
        known = context["root"] / (node["name"] + ".known-hosts")
        line = next(line for line in (context["root"] / "known-hosts").read_text().splitlines() if line.startswith(node["address"] + " "))
        write(known, line + "\n", 0o600)
        key_env, known_env = "CLUSTER_EDGE_" + str(index) + "_KEY", "CLUSTER_EDGE_" + str(index) + "_HOSTS"
        if key_env in os.environ or known_env in os.environ: raise ValueError("Do not replace existing identity references")
        os.environ[key_env], os.environ[known_env] = str(context["root"] / "client-key"), str(known)
        peers.append({"host": node["name"], "address": node["address"], "port": 22, "machine_sha256": machine,
            "identity_env": key_env, "known_hosts_env": known_env, "remote_root": "/opt/platform/entry-tools",
            "paths": {"bundle": "/opt/platform/entry-bundle", "gateway": "/opt/platform/gateway-bundle", "ca": "/opt/platform/entry-ca.crt", "artifacts": "/opt/platform/entry-artifacts"}, "expected_current": "absent"})
    pair_plan = {"schema_version": 1, "name": plan["name"], "edge_contract_sha256": digest, "toolset_sha256": agent.fingerprint(tools),
        "ssh_binary_sha256": sha(binary), "peers": peers}
    write(evidence / "pair-plan.json", agent.canonical(pair_plan), 0o600)
    observations = []
    tls = ssl.create_default_context(cafile=str(pki / "ca.crt")); tls.verify_flags |= ssl.VERIFY_X509_STRICT
    def serving():
        connection = DirectTLS(hostname, 443, context=tls, timeout=2); connection.address = VIP
        try:
            connection.request("GET", "/"); response = connection.getresponse()
            return response.status == 200 and response.read(1024) == expected_body
        finally: connection.close()
    def owners(excluded=None):
        return [node["name"] for node in nodes if node is not excluded and any(
            a.get("local") == VIP for row in json.loads(ssh(node, "ip -j address show dev eth0").stdout)
            for a in row.get("addr_info", []))]
    def observe_guest(node):
        command = shlex.join(["sudo", "/usr/bin/python3", "-I", "-B", "-c", GUEST_OBSERVATION, node["name"]])
        value = json.loads(ssh(node, command, seconds=20).stdout)
        test.assertFalse(value["kernel_oom_signal"])
        test.assertEqual(value["memory_events"].get("oom", 0), 0); test.assertEqual(value["memory_events"].get("oom_kill", 0), 0)
        test.assertEqual(value["k3s_unit_load_state"], "not-found")
        for role, row in value["roles"].items():
            expected = (1 << 10) if role == "haproxy" else (1 << 6) | (1 << 12) | (1 << 13)
            test.assertEqual(row["exe_sha256"], plan["binaries"][role]["sha256"])
            test.assertEqual(row["properties"]["NRestarts"], "0")
            test.assertEqual(row["uid"] == 0, role == "keepalived")
            test.assertEqual(int(row["cap_eff"], 16), expected); test.assertEqual(int(row["cap_bnd"], 16), expected)
            test.assertEqual(row["no_new_privileges"], "1")
        return value
    try:
        result = pair.operate(pair_plan, bundle, gateway_bundle, mode="apply", output=evidence / "operation", ssh_binary=binary,
            ca_file=pki / "ca.crt", artifacts=public / "artifacts", distribute_inputs=True, wait_seconds=90)
        test.assertTrue(result["pair_services_ready"]); test.assertTrue(result["vip_reachable"])
        test.assertTrue(serving()); test.assertTrue(unchanged()); test.assertTrue(denied())
        current = owners(); test.assertEqual(len(current), 1)
        original_owner = next(node for node in nodes if node["name"] == current[0])
        peer_name = next(node["name"] for node in nodes if node != original_owner)
        connection = DirectTLS(hostname, 443, context=ssl.create_default_context(), timeout=3); connection.address = VIP
        try:
            with test.assertRaises(ssl.SSLCertVerificationError): connection.request("GET", "/")
        finally: connection.close()
        before = {node["name"]: observe_guest(node) for node in nodes}
        stage("actual-application-vip-health-failover")
        unit = "edge-" + original_owner["name"] + "-haproxy.service"
        started = time.monotonic()
        monitor = original_owner["directory"] / "qmp.sock"
        if simulate_host_loss:
            qmp(monitor, "stop")
        else:
            ssh(original_owner, "sudo systemctl stop " + unit)
        try:
            deadline = started + 30
            while True:
                try: healthy = serving()
                except (OSError, HTTPException): healthy = False
                live = owners(excluded=original_owner if simulate_host_loss else None)
                test.assertLessEqual(len(live), 1)
                observations.append({"elapsed_seconds": round(time.monotonic() - started, 3), "http_payload_verified": healthy, "owners": live})
                if healthy and live == [peer_name]:
                    recovery_seconds = observations[-1]["elapsed_seconds"]
                    break
                if time.monotonic() >= deadline: raise TimeoutError("Actual application did not serve through the peer entry")
                time.sleep(0.25)
            if simulate_host_loss:
                test.assertEqual(qmp(monitor, "query-status")["status"], "paused")
        finally:
            if simulate_host_loss:
                qmp(monitor, "cont")
            else:
                ssh(original_owner, "sudo systemctl start " + unit)
        health_command = shlex.join(["sudo", "/usr/bin/python3", "-I", "-B", renderer.BASE + "/gateway_edge_health.py", "--port", "8404", "--identity", renderer.edge_identity(plan)])
        context["wait"](lambda: ssh(original_owner, health_command, check=False, seconds=8).returncode == 0, seconds=30)
        resumed = pair.operate(pair_plan, bundle, gateway_bundle, mode="resume", output=evidence / "operation", ssh_binary=binary,
            ca_file=pki / "ca.crt", artifacts=public / "artifacts", distribute_inputs=True, wait_seconds=90)
        test.assertTrue(resumed["pair_services_ready"])
        context["wait"](lambda: len(owners()) == 1 and serving(), seconds=30)
        if not simulate_host_loss:
            test.assertEqual(owners(), [peer_name])
        test.assertTrue(unchanged()); test.assertTrue(denied())
        after = {node["name"]: observe_guest(node) for node in nodes}
        for node in nodes:
            name = node["name"]
            test.assertEqual(before[name]["boot_id"], after[name]["boot_id"])
            for role in ("haproxy", "keepalived"):
                initial = before[name]["roles"][role]["properties"]["InvocationID"]
                final = after[name]["roles"][role]["properties"]["InvocationID"]
                test.assertRegex(initial, r"^[a-f0-9]{32}$"); test.assertRegex(final, r"^[a-f0-9]{32}$")
                test.assertEqual(initial != final, not simulate_host_loss and name == original_owner["name"] and role == "haproxy")
        test.assertEqual((gateway_bundle / "deployment-contract.json").read_bytes(), gateway_bytes)
        proof = json.loads((evidence / "operation/pair-proof.json").read_text())
        test.assertEqual(proof["phase"], "completed"); test.assertTrue(all(p["released"] for p in proof["peers"].values()))
        jobs = []
        for node in nodes:
            job_id = proof["peers"][node["name"]]["job_id"]; test.assertRegex(job_id, r"^[a-f0-9]{32}$")
            job = json.loads(ssh(node, "sudo cat /var/lib/platform-gateway-edge/pair-jobs/" + job_id + ".json").stdout)
            test.assertEqual(job["state"], "completed"); test.assertTrue(job["result"]["host_ready"])
            test.assertEqual(job["request"]["operation_id"], proof["operation_id"])
            test.assertEqual(job["request"]["target"], digest)
            properties = ssh(node, "sudo systemctl show platform-entry-job-" + job_id + ".service --property=LoadState,ActiveState,MainPID,ControlPID,ControlGroup").stdout
            cleanup = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
            test.assertEqual(cleanup, {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "ControlGroup": ""})
            test.assertEqual(ssh(node, "sudo test ! -e /var/lib/platform-gateway-edge/pair-reservation.json", check=False).returncode, 0)
            write(evidence / (node["name"] + "-job.json"), agent.canonical(job), 0o600)
            jobs.append({"host": node["name"], "job_id": job_id, "record_sha256": agent.fingerprint(job), "unit_cleanup": cleanup})
        write(evidence / "failover-observations.json", json.dumps(observations, sort_keys=True), 0o600)
        return {"actual_application_external_vip_verified": True, "actual_cicd_application_external_vip_verified": gateway_runtime == "k3s",
            "actual_native_entry_health_failover_verified": True, "actual_native_entry_vm_pause_failover_verified": simulate_host_loss,
            "actual_native_entry_rejects_untrusted_ca_verified": True, "actual_native_entry_runtime": "raw", "actual_gateway_runtime": gateway_runtime,
            "native_edge_guest_count": 2, "native_edge_guest_memory_mib": 512, "native_edge_guest_before": before, "native_edge_guest_after": after,
            "native_edge_inputs": chosen["inputs"], "native_edge_gateway_contract_sha256": gateway_digest, "native_edge_contract_sha256": digest,
            "native_edge_backend_identities": plan["backends"], "native_edge_peer_machine_fingerprints": {p["host"]: p["machine_sha256"] for p in peers},
            "native_edge_pair_evidence": str(evidence), "native_edge_pair_proof_sha256": sha(evidence / "operation/pair-proof.json"),
            "native_edge_jobs": jobs, "native_edge_tool_files_sha256": tools, "native_edge_failover_observations": observations,
            "native_edge_fault_to_serving_peer_seconds": recovery_seconds, "native_edge_fault_original_owner": original_owner["name"],
            "native_edge_fault_serving_peer": peer_name, "native_edge_physical_ha_verified": False, "full_stack_verified": False}
    finally:
        for peer in peers: os.environ.pop(peer["identity_env"], None); os.environ.pop(peer["known_hosts_env"], None)
