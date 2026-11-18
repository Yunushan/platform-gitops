#!/usr/bin/env python3
"""Five real KVM guests: embedded-etcd quorum and one fixed TCP API endpoint."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import threading
from urllib.request import ProxyHandler, Request, build_opener

from test_k3s_vm_fixture import fingerprint, qmp


HAPROXY_SHA = "3137dc28d1fa8095ce2610e5736d67cbbdec3a3b15af49bb3ba8631ebe5904ed"
API = "https://192.0.2.1:6443"


def server_command(node, *, first=False, external_cni=False, runtime_seconds=1100):
    if type(runtime_seconds) is not int or runtime_seconds not in {1100, 1700} or runtime_seconds == 1700 and not external_cni:
        raise ValueError("Select an explicit finite guest runtime profile")
    if node["name"] not in {"fixture-control", "fixture-control-2", "fixture-control-3"} or node["address"] not in {"192.0.2.2", "192.0.2.5", "192.0.2.6"}:
        raise ValueError("Use only an original quorum guest")
    return ("sudo systemd-run --quiet --unit=fixture-k3s --property=Type=exec --property=Restart=on-failure --property=RestartSec=2s "
        "--property=StartLimitIntervalSec=120 --property=StartLimitBurst=3 --property=RuntimeMaxSec=" + str(runtime_seconds) + " /mnt/fixtures/k3s server "
        "--data-dir=/var/lib/fixture-k3s --node-name=" + node["name"] + " --node-ip=" + node["address"] + " --bind-address=" + node["address"] + " --advertise-address=" + node["address"] +
        " --write-kubeconfig=/etc/fixture/admin.yaml --write-kubeconfig-mode=600 --token-file=/etc/fixture/" + ("server-token --cluster-init" if first else "join-token --server=" + API) +
        " --tls-san=192.0.2.1 --node-taint=node-role.kubernetes.io/control-plane=true:NoSchedule --flannel-iface=eth0 --flannel-backend=" + ("none --disable-kube-proxy " if external_cni else "host-gw ") +
        "--node-label=topology.kubernetes.io/zone=zone-" + node["name"] + " " +
        "--cluster-cidr=198.18.0.0/16 --service-cidr=198.19.0.0/16 --cluster-dns=198.19.0.10 --disable=traefik,local-storage,servicelb "
        "--disable-helm-controller --disable-network-policy --secrets-encryption --etcd-arg=enable-grpc-gateway=true")


def start_proxy(root, controls, processes, write, run, environment):
    source = Path(os.environ["K3S_QUORUM_TEST_HAPROXY"]).resolve(strict=True)
    fingerprint(source, HAPROXY_SHA)
    binary = root / "bin/haproxy"; shutil.copyfile(source, binary); binary.chmod(0o755)
    # The enclosing service uses UMask=0077. This code-only directory needs
    # traversal by the dropped UID; the original private scope remains 0700.
    binary.parent.chmod(0o755)
    configuration = ("global\n  maxconn 128\n  nbthread 1\ndefaults\n  mode tcp\n  timeout connect 2s\n  timeout client 15s\n  timeout server 15s\n  timeout check 1s\n"
        "frontend fixture-api\n  bind 192.0.2.1:6443\n  default_backend fixture-control\nbackend fixture-control\n  balance roundrobin\n  option tcp-check\n"
        + "".join("  server " + node["name"] + " " + node["address"] + ":6443 check inter 1s fall 2 rise 1\n" for node in controls))
    config = write("quorum-haproxy.cfg", configuration)
    run([binary, "-c", "-f", config])
    log = (root / "api-proxy.log").open("w+b")
    from test_k3s_vm_fixture import __file__ as child_program
    process = subprocess.Popen([sys.executable, "-B", child_program, "--qemu-child", str(os.getpid()), str(binary), "-db", "-f", str(config)],
        env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    processes.append((process, log))
    return {"pid": process.pid, "sha256": HAPROXY_SHA, "configuration_sha256": hashlib.sha256(configuration.encode()).hexdigest()}


def prepare_control(ssh, node):
    ssh(node, "sudo mkdir -p /var/lib/fixture-k3s/agent/images && sudo cp /mnt/fixtures/k3s-airgap-images-amd64.tar.zst /var/lib/fixture-k3s/agent/images/", seconds=60)


def join_controls(client, controls, ssh, wait, token, *, external_cni=False, runtime_seconds=1100):
    for node in controls[1:]:
        prepare_control(ssh, node)
        ssh(node, "sudo install -m 600 /dev/null /etc/fixture/join-token && sudo tee /etc/fixture/join-token >/dev/null", payload=token + "\n")
        ssh(node, server_command(node, external_cni=external_cni, runtime_seconds=runtime_seconds))
        def ready(node=node):
            if ssh(node, "sudo systemctl is-failed --quiet fixture-k3s", check=False).returncode == 0:
                raise AssertionError("An original quorum server failed")
            # Reusing the guest's k3s wrapper races its initial binary-extraction
            # lock. Probe with the already pinned parent kubectl and original CA.
            try:
                return client.call(["get", "--server=https://" + node["address"] + ":6443", "--raw=/readyz"], seconds=5).strip() == "ok"
            except subprocess.TimeoutExpired:
                return False
        wait(ready, seconds=180)


def etcd(ssh, node, path):
    if path not in {"/v3/cluster/member/list", "/v3/maintenance/status"}: raise ValueError("Read only fixed embedded-etcd resources")
    # Read the fixture's original mTLS identity inside its own guest; no key is
    # exported to the parent or placed in an argument, log or receipt.
    program = '''import json,ssl,sys
from urllib.request import Request,ProxyHandler,HTTPSHandler,build_opener
try:
    base='/var/lib/fixture-k3s/server/tls/etcd/'
    context=ssl.create_default_context(cafile=base+'server-ca.crt')
    context.load_cert_chain(base+'client.crt',base+'client.key')
    opener=build_opener(ProxyHandler({}),HTTPSHandler(context=context))
    # K3s reserves 2379 for gRPC; its separately configured HTTPS JSON listener
    # is 2382, with the same original CA and dedicated client identity.
    with opener.open(Request('https://127.0.0.1:2382'+sys.argv[1],data=b'{}',headers={'Content-Type':'application/json'}),timeout=5) as response:
        raw=response.read(65537)
    if len(raw)>65536: raise ValueError()
    value=json.loads(raw)
    if not isinstance(value,dict) or 'error' in value: raise ValueError()
    print(json.dumps(value))
except Exception as error:
    kinds={'FileNotFoundError','SSLError','SSLCertVerificationError','HTTPError','URLError','TimeoutError','ValueError','JSONDecodeError','RemoteDisconnected'}
    print(json.dumps({'exception':type(error).__name__ if type(error).__name__ in kinds else 'unclassified','http_status':getattr(error,'code',None),'certificate_failure':isinstance(getattr(error,'reason',error),ssl.SSLError)}))
    sys.exit(1)
'''
    result = ssh(node, "sudo python3 -c " + shlex.quote(program) + " " + shlex.quote(path), check=False, seconds=12)
    if result.returncode:
        try: diagnostic = json.loads(result.stdout)
        except ValueError: diagnostic = {"exception": "guest-client-failed"}
        print(json.dumps({"etcd_read_failure": diagnostic}), flush=True)
        raise ValueError("Original guest embedded-etcd read failed")
    return json.loads(result.stdout)


def membership(document, controls):
    rows = document.get("members") if isinstance(document, dict) else None
    if not isinstance(rows, list) or len(rows) != 3: raise ValueError("Require exactly three original etcd voters")
    result = {}
    for node in controls:
        matches = [row for row in rows if row.get("peerURLs") == ["https://" + node["address"] + ":2380"]]
        if len(matches) != 1: raise ValueError("Embedded-etcd peer inventory changed")
        row = matches[0]; member = str(row.get("ID", ""))
        if (not member.isdecimal() or int(member) <= 0 or row.get("isLearner", False) is not False
                or row.get("clientURLs") != ["https://" + node["address"] + ":2379"]):
            raise ValueError("Embedded-etcd member is not an original voting peer")
        result[node["name"]] = member
    if len(set(result.values())) != 3: raise ValueError("Duplicate embedded-etcd member identity")
    return result


def control_services_ready(context, controls):
    """Require original guest units and each direct API, not cached Node health."""
    result = {}
    for node in controls:
        observed = context["ssh"](node, "sudo systemctl show fixture-k3s -p ActiveState -p SubState -p MainPID -p NRestarts -p InvocationID -p Restart", check=False)
        if observed.returncode: return False
        state = dict(line.split("=", 1) for line in observed.stdout.splitlines() if "=" in line)
        if (state.get("ActiveState") != "active" or state.get("SubState") != "running"
                or not state.get("MainPID", "").isdecimal() or int(state["MainPID"]) <= 0
                or not state.get("NRestarts", "").isdecimal() or int(state["NRestarts"]) > 3
                or state.get("Restart") != "on-failure" or not re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))): return False
        if context["client"].call(["get", "--server=https://" + node["address"] + ":6443", "--raw=/readyz"], seconds=8).strip() != "ok": return False
        result[node["name"]] = {"pid": int(state["MainPID"]), "restarts": int(state["NRestarts"]), "invocation_id": state["InvocationID"]}
    return result


def stable_controls(context, controls):
    previous, since = None, None
    def stable():
        nonlocal previous, since
        try: current = control_services_ready(context, controls)
        except (OSError, ValueError, subprocess.TimeoutExpired): current = False
        if not current:
            previous, since = None, None
            return False
        if current != previous:
            previous, since = current, time.monotonic()
        return current if time.monotonic() - since >= 15 else False
    return context["wait"](stable, seconds=120)


def signal_original_control(context, node, before):
    """Inject once through a pidfd pinned to the reviewed guest service process."""
    pid, invocation = before.get("pid"), before.get("invocation_id")
    if type(pid) is not int or pid <= 1 or not re.fullmatch(r"[0-9a-f]{32}", str(invocation)):
        raise ValueError("Require an original guest process and service invocation")
    root = context["root"]
    if (root.parent != Path("/var/tmp") or not root.name.startswith("platform-k3s-vm-") or root.is_symlink()
            or root.stat().st_uid != 65534 or node not in context["control_nodes"]
            or qmp(node["directory"] / "qmp.sock", "query-status")["status"] != "running"):
        raise ValueError("Require the original running disposable control guest")
    program = '''import json,os,signal,subprocess,sys
pid=int(sys.argv[1]); invocation=sys.argv[2]
descriptor=os.pidfd_open(pid)
try:
    output=subprocess.run(['systemctl','show','fixture-k3s','-p','MainPID','-p','InvocationID','-p','ActiveState'],capture_output=True,text=True,check=True,timeout=5).stdout
    state=dict(line.split('=',1) for line in output.splitlines() if '=' in line)
    if state.get('MainPID')!=str(pid) or state.get('InvocationID')!=invocation or state.get('ActiveState')!='active':
        raise ValueError('Original fixture process changed before failure injection')
    signal.pidfd_send_signal(descriptor,signal.SIGKILL)
    print(json.dumps({'original_process_signaled':True}))
finally:
    os.close(descriptor)
'''
    # No retry: an interrupted SSH response may follow a delivered signal.
    observed = context["ssh"](node, "sudo python3 -c " + shlex.quote(program) + " " + str(pid) + " " + shlex.quote(invocation), seconds=15)
    if json.loads(observed.stdout) != {"original_process_signaled": True}: raise ValueError("Guest failure injection was not acknowledged")


def process_recovery(test, context, controls, node, traffic):
    before = stable_controls(context, controls)
    original = before[node["name"]]
    stop, samples = threading.Event(), []
    def observe():
        while not stop.is_set():
            try: samples.append(traffic() is True)
            except Exception: samples.append(False)
            if len(samples) >= 600: return
            stop.wait(0.25)
    thread = threading.Thread(target=observe, daemon=True); thread.start()
    started = time.monotonic()
    try:
        print(json.dumps({"k3s_quorum_stage": "original-control-process-failure"}), flush=True)
        signal_original_control(context, node, original)
        def restarted():
            current = control_services_ready(context, controls)
            return current and current[node["name"]]["invocation_id"] != original["invocation_id"] and current[node["name"]]["restarts"] == original["restarts"] + 1
        context["wait"](restarted, seconds=120)
        after = stable_controls(context, controls)
        test.assertNotEqual(after[node["name"]]["invocation_id"], original["invocation_id"])
        test.assertEqual(after[node["name"]]["restarts"], original["restarts"] + 1)
        for other in controls:
            if other is not node: test.assertEqual(after[other["name"]], before[other["name"]])
    finally:
        stop.set(); thread.join(timeout=8)
    test.assertFalse(thread.is_alive())
    test.assertLess(len(samples), 600, "Traffic observation ended at its safety cap before recovery completed")
    test.assertGreaterEqual(len(samples), 2); test.assertTrue(all(samples))
    print(json.dumps({"k3s_quorum_stage": "original-control-process-restarted"}), flush=True)
    return {"forced_control_process_failure_verified": True, "supervised_control_process_restart_verified": True,
        "worker_http_during_control_process_restart_verified": True,
        "control_process_restart": {"node": node["name"], "before": original, "after": after[node["name"]],
            "recovery_seconds": round(time.monotonic() - started, 3), "http_samples": len(samples), "failed_http_samples": samples.count(False)}}


def serving(test, context):
    from test_k3s_vm_fixture import PROBE_IMAGE
    client, wait = context["client"], context["wait"]
    create = lambda obj: json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(obj).encode()))
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "quorum-probe", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
    pods = []
    for node in context["nodes"][1:]:
        obj = create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": node["name"], "namespace": "quorum-probe", "labels": {"app": "quorum-probe"}},
            "spec": {"automountServiceAccountToken": False, "nodeSelector": {"kubernetes.io/hostname": node["name"]},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "http", "image": PROBE_IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["httpd", "-f", "-p", "8080", "-h", "/www"],
                    "volumeMounts": [{"name": "content", "mountPath": "/www", "readOnly": True}],
                    "readinessProbe": {"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 1},
                    "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": "64Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}],
                "volumes": [{"name": "content", "configMap": {"name": "web-content"}}]}})
        pods.append((node, obj["metadata"]["uid"]))
    create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-content", "namespace": "quorum-probe"}, "data": {"index.html": "original worker data plane\n"}})
    service = create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "quorum-probe"},
        "spec": {"type": "NodePort", "selector": {"app": "quorum-probe"}, "ports": [{"port": 80, "targetPort": 8080}]}})
    def ready():
        for node, uid in pods:
            actual = client.get("pod/" + node["name"], "quorum-probe")
            test.assertEqual(actual["metadata"]["uid"], uid)
            if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in actual.get("status", {}).get("conditions", [])): return False
            test.assertEqual(actual["spec"]["nodeName"], node["name"])
        return True
    wait(ready, seconds=120)
    opener = build_opener(ProxyHandler({}))
    port = service["spec"]["ports"][0]["nodePort"]
    def traffic():
        try:
            for node, _ in pods:
                with opener.open(Request("http://" + node["address"] + ":" + str(port) + "/"), timeout=3) as response:
                    if response.status != 200 or response.read(1024) != b"original worker data plane\n": return False
            return True
        except OSError: return False
    wait(traffic, seconds=30)
    return traffic, ready, [uid for _, uid in pods]


def exercise(test, context, controls, proxy):
    client, ssh, wait = context["client"], context["ssh"], context["wait"]
    expected_names = {node["name"] for node in [*controls, *context["nodes"][1:]]}
    original_nodes = {row["metadata"]["name"]: row["metadata"]["uid"] for row in json.loads(client.call(["get", "nodes", "-o", "json"]))["items"]}
    test.assertEqual(set(original_nodes), expected_names)
    def read_members():
        document = etcd(ssh, controls[0], "/v3/cluster/member/list")
        try: return membership(document, controls)
        except ValueError:
            rows = document.get("members", [])
            print(json.dumps({"etcd_membership_shape": {"count": len(rows), "ids_present": all("ID" in row for row in rows),
                "peer_url_counts": [len(row.get("peerURLs", [])) for row in rows], "client_url_counts": [len(row.get("clientURLs", [])) for row in rows],
                "learners": sum(row.get("isLearner", False) is True for row in rows)}}), flush=True)
            raise
    members = wait(read_members, seconds=60)
    def states(nodes): return {node["name"]: etcd(ssh, node, "/v3/maintenance/status") for node in nodes}
    def leader(nodes, excluded=None):
        rows = states(nodes); values = {str(value.get("leader", "0")) for value in rows.values()}
        if len(values) != 1 or "0" in values or excluded in values: return None
        selected = values.pop()
        if selected not in members.values(): raise AssertionError("Unrecognized etcd leader")
        for name, row in rows.items():
            test.assertEqual(str(row["header"]["member_id"]), members[name])
            test.assertFalse(row.get("errors")); test.assertFalse(row.get("isLearner", False))
        return selected
    old_leader = wait(lambda: leader(controls), seconds=60)
    failed = next(node for node in controls if members[node["name"]] == old_leader)
    survivors = [node for node in controls if node is not failed]
    traffic, pods_ready, pod_uids = serving(test, context)
    def create(name, value):
        return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps({"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "quorum-probe"}, "data": {"probe": value}}).encode(), seconds=5))
    baseline = create("before-outage", "committed-before-outage")
    paused = []
    try:
        print(json.dumps({"k3s_quorum_stage": "original-leader-vm-paused"}), flush=True)
        qmp(failed["directory"] / "qmp.sock", "stop"); paused.append(failed)
        test.assertEqual(qmp(failed["directory"] / "qmp.sock", "query-status")["status"], "paused")
        new_leader = wait(lambda: leader(survivors, excluded=old_leader), seconds=60)
        wait(lambda: client.call(["get", "--raw=/readyz"]).strip() == "ok", seconds=60)
        acknowledged = create("one-server-down", "committed-with-two-voters")
        test.assertTrue(acknowledged["metadata"]["uid"])
        test.assertEqual(client.get("configmap/before-outage", "quorum-probe")["metadata"]["uid"], baseline["metadata"]["uid"])
        wait(traffic, seconds=30)
        second = next(node for node in survivors if members[node["name"]] == new_leader)
        qmp(second["directory"] / "qmp.sock", "stop"); paused.append(second)
        test.assertEqual(qmp(second["directory"] / "qmp.sock", "query-status")["status"], "paused")
        last = next(node for node in survivors if node is not second)
        wait(lambda: str(etcd(ssh, last, "/v3/maintenance/status").get("leader", "0")) == "0", seconds=60)
        print(json.dumps({"k3s_quorum_stage": "two-voters-paused-no-write-acknowledgment"}), flush=True)
        with test.assertRaises(ValueError): create("unacknowledged-during-quorum-loss", "observe-outcome-after-recovery")
        # An HTTP timeout cannot prove that an uncertain write never commits.
        # Read its actual outcome after recovery; never retry that write here.
        wait(traffic, seconds=30)
    finally:
        for node in reversed(paused):
            qmp(node["directory"] / "qmp.sock", "cont")
    context["lab_time"].synchronize([*controls, *context["nodes"][1:]], ssh, wait)
    service_states = stable_controls(context, controls)
    wait(lambda: leader(controls), seconds=90)
    for node in controls:
        test.assertEqual(wait(lambda node=node: membership(etcd(ssh, node, "/v3/cluster/member/list"), controls), seconds=60), members)
    wait(lambda: client.call(["get", "--raw=/readyz"]).strip() == "ok", seconds=90)
    recovered = create("after-recovery", "committed-after-original-voters-resumed")
    test.assertTrue(recovered["metadata"]["uid"])
    uncertain = client.get("configmap/unacknowledged-during-quorum-loss", "quorum-probe", optional=True)
    if uncertain: test.assertEqual(uncertain["data"], {"probe": "observe-outcome-after-recovery"})
    test.assertEqual(client.get("configmap/one-server-down", "quorum-probe")["metadata"]["uid"], acknowledged["metadata"]["uid"])
    wait(pods_ready, seconds=90); wait(traffic, seconds=30)
    process_failure_leader = wait(lambda: leader(controls), seconds=60)
    selected = next(node for node in controls if members[node["name"]] == process_failure_leader)
    restart_report = process_recovery(test, context, controls, selected, traffic)
    wait(lambda: leader(controls), seconds=90)
    for node in controls:
        test.assertEqual(wait(lambda node=node: membership(etcd(ssh, node, "/v3/cluster/member/list"), controls), seconds=60), members)
    restarted_write = create("after-process-recovery", "committed-after-supervised-restart")
    test.assertEqual(client.get("configmap/after-process-recovery", "quorum-probe")["metadata"]["uid"], restarted_write["metadata"]["uid"])
    test.assertEqual(client.get("configmap/after-recovery", "quorum-probe")["metadata"]["uid"], recovered["metadata"]["uid"])
    wait(pods_ready, seconds=90); wait(traffic, seconds=30)
    final_nodes = {row["metadata"]["name"]: row["metadata"]["uid"] for row in json.loads(client.call(["get", "nodes", "-o", "json"]))["items"]}
    test.assertEqual(final_nodes, original_nodes)
    config = json.loads(client.call(["config", "view", "--minify", "-o", "json"]))
    test.assertEqual(config["clusters"][0]["cluster"]["server"], API)
    print(json.dumps({"k3s_quorum_stage": "original-five-node-cluster-recovered"}), flush=True)
    return {**restart_report, "virtual_three_control_plane_quorum_verified": True, "virtual_control_plane_count": 3,
        "fixed_api_endpoint_verified": True, "original_etcd_leader_vm_pause_verified": True,
        "one_voter_loss_write_acknowledged": True, "two_voter_loss_write_not_acknowledged": True,
        "unacknowledged_write_observed_after_recovery": uncertain is not None, "original_voter_recovery_verified": True,
        "worker_http_during_control_plane_outages_verified": True, "quorum_worker_pod_uids": pod_uids,
        "original_node_uids": original_nodes, "original_etcd_member_ids": members, "api_proxy": proxy,
        "original_control_services_ready_verified": True, "recovered_control_services": service_states,
        "api_entry_redundancy_verified": False, "physical_failure_domains_verified": False, "full_stack_verified": False}
