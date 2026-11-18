#!/usr/bin/env python3
"""Opt-in loopback-only API admission fixture; no workers, storage devices or existing kubeconfig."""

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from unittest import mock

from bounded_subprocess import run_bounded
from kubernetes_management import Client
import deploy_rook_bootstrap as deploy
import render_rook_bootstrap as renderer
from test_rook_bootstrap import fixture


# Official controller-tools envtest-v1.35.0 linux-amd64 archive; SHA-512:
# 130369c16f076e724d089189afaede960316f5f5dea6cf57be7a4fc6f09c7734289
# 3192509790e4056e116e232dff832ed863f5bd55dcb55d38f3ab834828a11
BINARIES = {
    "kube-apiserver": "999e4874de50139f40929d6a9f4844efc66ad09647dc0e84031daca4711f12b6",
    "etcd": "e3d7d2950b4efbebb7c585a5876c2dc7db4a0c4b8ed95939121be853e0c55a27",
    "kubectl": "a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989",
}


def child():
    expected_parent = int(sys.argv[2])
    if os.getppid() != expected_parent: raise SystemExit(1)
    if os.geteuid() == 0:
        os.setgroups([]); os.setgid(65534); os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0) or os.getppid() != expected_parent: raise SystemExit(1)
    os.execve(sys.argv[3], sys.argv[3:], {"PATH": "/usr/bin:/bin", "HOME": os.getcwd(), "LC_ALL": "C", "GOMAXPROCS": "2"})


def exercise(test, *, global_only=False, client_check_only=False, resource_objects=None, admission_checker=None, management_action=None, privileged_admission=False, disk_workspace=False):
    if type(privileged_admission) is not bool: raise ValueError("Select privileged admission explicitly for the disposable API")
    if resource_objects is not None and (global_only or client_check_only): raise ValueError("Select one disposable admission fixture mode")
    if management_action is not None and (global_only or client_check_only or resource_objects is not None or not callable(management_action)): raise ValueError("Select one disposable explicit-target management action")
    if sys.platform != "linux" or os.uname().machine != "x86_64" or not shutil.which("openssl"):
        raise ValueError("This explicit fixture requires Linux amd64 and OpenSSL")
    binaries = Path(os.environ["ROOK_BOOTSTRAP_TEST_BIN"]).resolve(strict=True)
    for name, digest in BINARIES.items():
        path = binaries / name
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest: raise ValueError("Use the exact reviewed envtest binary set")
    # Reserve fresh loopback ports; never connect to a borrowed API/etcd process.
    reservations = [socket.socket() for _ in range(3)]
    try:
        for item in reservations: item.bind(("127.0.0.1", 0))
        ports = [item.getsockname()[1] for item in reservations]
    finally:
        for item in reservations: item.close()
    base = "/var/tmp" if disk_workspace else "/tmp"
    if os.statvfs(base).f_flag & os.ST_NOEXEC: raise ValueError("The disposable fixture needs an executable Linux temporary filesystem")
    with tempfile.TemporaryDirectory(prefix="rook-admission-fixture-", dir=base) as temporary:
        root = Path(temporary); root.chmod(0o700)
        if os.geteuid() == 0: os.chown(root, 65534, 65534)
        local_binaries = root / "bin"; local_binaries.mkdir(mode=0o755); local_binaries.chmod(0o755)
        for name in BINARIES:
            shutil.copyfile(binaries / name, local_binaries / name); (local_binaries / name).chmod(0o755)
        binaries = local_binaries
        processes = []; deadline = time.monotonic() + 900
        def run(args, payload=None, limit=20):
            seconds = min(limit, deadline - time.monotonic())
            if seconds <= 0: raise TimeoutError("Disposable API admission fixture deadline")
            result = run_bounded(list(map(str, args)), input=payload, text=True, timeout=seconds, check=False, output_max_bytes=4 * 1024 ** 2)
            if result.returncode: raise ValueError("Disposable API fixture command failed; inspect only its private local diagnostic")
            return result.stdout
        def write(name, value):
            path = root / name; path.write_text(value, encoding="utf-8"); path.chmod(0o600)
            if os.geteuid() == 0: os.chown(path, 65534, 65534)
            return path
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture-only-kubernetes-root",
            "-addext", "basicConstraints=critical,CA:TRUE", "-keyout", root / "ca.key", "-out", root / "ca.crt"])
        for name, subject, usage in (("server", "/CN=fixture-only-api", "serverAuth"), ("admin", "/CN=fixture-admin/O=system:masters", "clientAuth")):
            run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", subject, "-keyout", root / (name + ".key"), "-out", root / (name + ".csr")])
            ext = write(name + ".ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=" + usage + ("\nsubjectAltName=IP:127.0.0.1\n" if name == "server" else "\n"))
            run(["openssl", "x509", "-req", "-days", "1", "-in", root / (name + ".csr"), "-CA", root / "ca.crt", "-CAkey", root / "ca.key", "-CAcreateserial", "-extfile", ext, "-out", root / (name + ".crt")])
        run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", root / "service-account.key"])
        for path in root.iterdir():
            if not path.is_file(): continue
            path.chmod(0o600)
            if os.geteuid() == 0: os.chown(path, 65534, 65534)
        def start(binary, arguments):
            log = (root / (binary + ".log")).open("w+b")
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(os.getpid()), str(binaries / binary), *arguments],
                cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            processes.append((process, log)); return process
        def wait(callback):
            while True:
                for process, log in processes:
                    if process.poll() is not None:
                        raise ValueError("A disposable API fixture child stopped; raw logs are not printed")
                try:
                    if callback(): return
                except (ValueError, OSError): pass
                if time.monotonic() >= deadline: raise TimeoutError("Disposable fixture readiness deadline")
                time.sleep(0.25)
        try:
            client_url, peer_url = ("http://127.0.0.1:" + str(p) for p in ports[:2])
            start("etcd", ["--name=fixture", "--data-dir=" + str(root / "etcd-data"), "--listen-client-urls=" + client_url, "--advertise-client-urls=" + client_url,
                "--listen-peer-urls=" + peer_url, "--initial-advertise-peer-urls=" + peer_url, "--initial-cluster=fixture=" + peer_url, "--log-level=error"])
            def etcd_listens():
                with socket.create_connection(("127.0.0.1", ports[0]), timeout=0.5): return True
            wait(etcd_listens)
            start("kube-apiserver", ["--etcd-servers=" + client_url, "--bind-address=127.0.0.1", "--advertise-address=192.0.2.2", "--secure-port=" + str(ports[2]),
                "--service-cluster-ip-range=192.0.2.0/24", "--authorization-mode=Node,RBAC", "--anonymous-auth=false", "--client-ca-file=" + str(root / "ca.crt"),
                "--tls-cert-file=" + str(root / "server.crt"), "--tls-private-key-file=" + str(root / "server.key"), "--cert-dir=" + str(root / "certs"),
                "--service-account-key-file=" + str(root / "service-account.key"), "--service-account-signing-key-file=" + str(root / "service-account.key"),
                "--service-account-issuer=https://fixture-only-api.invalid", "--v=0", *(["--allow-privileged=true"] if privileged_admission else [])])
            encoded = lambda name: base64.b64encode((root / name).read_bytes()).decode()
            config = {"apiVersion": "v1", "kind": "Config", "current-context": "storage", "contexts": [{"name": "storage", "context": {"cluster": "fixture", "user": "fixture"}}],
                "clusters": [{"name": "fixture", "cluster": {"server": "https://127.0.0.1:" + str(ports[2]), "certificate-authority-data": encoded("ca.crt")}}],
                "users": [{"name": "fixture", "user": {"client-certificate-data": encoded("admin.crt"), "client-key-data": encoded("admin.key")}}]}
            kubeconfig = write("kubeconfig.json", json.dumps(config)); prefix = [binaries / "kubectl", "--kubeconfig", kubeconfig, "--request-timeout=10s"]
            def kubectl(arguments, obj=None): return json.loads(run([*prefix, *arguments], json.dumps(obj) if obj is not None else None))
            wait(lambda: "ok" in run([*prefix, "get", "--raw=/readyz"], limit=12))
            for namespace in ("kube-system", "rook-system"):
                rows = kubectl(["get", "namespaces", "-o", "json"])["items"]
                if namespace not in {o["metadata"]["name"] for o in rows}:
                    kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "privileged"}}})
            plan = fixture(); plan["namespace_uid"] = kubectl(["get", "ns/rook-system", "-o", "json"])["metadata"]["uid"]
            plan["target"]["cluster_uid"] = kubectl(["get", "ns/kube-system", "-o", "json"])["metadata"]["uid"]
            if management_action is not None:
                target = {"kubeconfig_env": "DISPOSABLE_MANAGEMENT_CONFIG", "context": "storage", "cluster_uid": plan["target"]["cluster_uid"]}
                with mock.patch.dict(os.environ, {"DISPOSABLE_MANAGEMENT_CONFIG": str(kubeconfig), "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", "")}):
                    return management_action(Client(target), root, kubectl)
            if resource_objects is not None:
                if not isinstance(resource_objects, list) or not 1 <= len(resource_objects) <= 32: raise ValueError("Use a finite prepared resource set")
                namespaces = {obj["metadata"]["namespace"] for obj in resource_objects}
                for namespace in namespaces:
                    kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
                def preserved(expected, actual):
                    if isinstance(expected, dict):
                        test.assertIsInstance(actual, dict)
                        for key, value in expected.items(): test.assertIn(key, actual); preserved(value, actual[key])
                    elif isinstance(expected, list):
                        test.assertIsInstance(actual, list); test.assertEqual(len(expected), len(actual))
                        for a, b in zip(expected, actual): preserved(a, b)
                    else: test.assertIs(type(actual), type(expected)); test.assertEqual(actual, expected)
                for obj in resource_objects:
                    dry = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], obj); preserved(obj, dry)
                    pinned = admission_checker(obj, dry) if admission_checker else None
                    actual = kubectl(["create", "-f", "-", "-o", "json"], obj); preserved(obj, actual)
                    test.assertTrue(actual["metadata"]["uid"])
                    observed = kubectl(["get", obj["kind"], obj["metadata"]["name"], "-n", obj["metadata"]["namespace"], "-o", "json"])
                    test.assertEqual(actual["metadata"]["uid"], observed["metadata"]["uid"]); preserved(obj, observed)
                    if admission_checker:
                        test.assertEqual(admission_checker(obj, actual), pinned)
                        test.assertEqual(admission_checker(obj, observed), pinned)
                return  # Admission/read-back only; no operators or workers.
            if client_check_only:
                import render_ceph_client_check as clients
                import verify_ceph_clients as checks
                import verify_ceph_recovery_reads as reads
                from test_ceph_clients import fixture as client_fixture
                from test_ceph_storage import fixture as storage_fixture
                storage_plan = storage_fixture(); client_plan = client_fixture(storage_plan)
                namespace = client_plan["namespace"]
                kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
                kubectl(["create", "serviceaccount", "default", "-n", namespace, "-o", "json"])
                canary_objects = clients.objects(client_plan, storage_plan, "rke2")
                reader_source = Path(reads.__file__).with_name("ceph_recovery_read.py").read_text(encoding="utf-8")
                config, jobs = reads.resources(canary_objects, reader_source, "rebuilt", "admission")
                for obj in [*canary_objects, config, *jobs.values()]:
                    expected = checks.admitted(obj, kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], obj))
                    actual = kubectl(["create", "-f", "-", "-o", "json"], obj)
                    observed = kubectl(["get", obj["kind"], obj["metadata"]["name"], "-n", namespace, "-o", "json"])
                    test.assertEqual(checks.admitted(obj, actual), expected)
                    test.assertEqual(checks.admitted(obj, observed), expected)
                    test.assertTrue(clients.storage.uid(actual["metadata"]["uid"]))
                    test.assertEqual(observed["metadata"]["uid"], actual["metadata"]["uid"])
                    if obj["kind"] == "PersistentVolumeClaim": test.assertNotEqual(actual.get("status", {}).get("phase"), "Bound")
                for job in jobs.values():
                    name = job["metadata"]["name"]
                    actual = kubectl(["get", "job", name, "-n", namespace, "-o", "json"])
                    template = actual["spec"]["template"]
                    pod = {
                        "apiVersion": "v1", "kind": "Pod",
                        "metadata": {
                            "name": name + "-pod", "namespace": namespace, "labels": template["metadata"]["labels"],
                            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": name,
                                                 "uid": actual["metadata"]["uid"], "controller": True}],
                        },
                        "spec": template["spec"],
                    }
                    admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], pod)
                    expected_spec = job["spec"]["template"]["spec"]
                    test.assertEqual(checks.provider.pod_policy(admitted["spec"]), checks.provider.pod_policy(expected_spec))
                    test.assertTrue(admitted["spec"]["volumes"][1]["persistentVolumeClaim"]["readOnly"])
                    test.assertTrue(admitted["spec"]["containers"][0]["volumeMounts"][1]["readOnly"])
                return  # Admission/read-back only: no workers or storage backend.
            global_plan = fixture(); global_plan["watch_current_namespace_only"] = False
            global_deployment = next(o for o in renderer.objects(global_plan, "rke2")["core"] if o["kind"] == "Deployment" and o["metadata"]["name"] == "ceph-csi-controller-manager")
            global_deployment["metadata"]["name"] = "ceph-csi-global-watch-dry-run"
            global_admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], global_deployment)
            deploy.admitted(global_deployment, global_admitted)
            if global_only: return
            for node in plan["nodes"]:
                obj = kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Node", "metadata": {"name": node["name"], "labels": {"kubernetes.io/hostname": node["name"], plan["domain_label"]: node["failure_domain"]}}})
                node["uid"] = obj["metadata"]["uid"]
                kubectl(["patch", "node/" + node["name"], "--subresource=status", "--type=merge", "-p", json.dumps({"status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"operatingSystem": "linux"}}}), "-o", "json"])
                kubectl(["patch", "node/" + node["name"], "--type=merge", "-p", '{"spec":{"taints":[]}}', "-o", "json"])
            (root / "private").mkdir(mode=0o700)
            class FixtureClient(Client):
                def call(self, args, *, payload=None, seconds=30):
                    # Only this isolated fixture's synthetic API objects occur
                    # here. No production kubeconfig, API token or Secret is used.
                    remaining = min(seconds + 10, 40, deadline - time.monotonic())
                    if remaining <= 0: raise TimeoutError("Disposable API fixture deadline")
                    result = run_bounded([*self.prefix, "--request-timeout=10s", *args], input=payload.decode() if payload else "", text=True,
                        timeout=remaining, check=False, output_max_bytes=4 * 1024 ** 2)
                    if result.returncode: raise ValueError("Isolated API rejected fixture resource: " + result.stderr[-3000:])
                    return result.stdout
            original_admitted = deploy.admitted
            def checked_admission(expected, actual):
                try: return original_admitted(expected, actual)
                except ValueError as error:
                    def differences(a, b, path=""):
                        if isinstance(a, dict):
                            if not isinstance(b, dict): return [path + ":type"]
                            return [x for k, v in a.items() for x in ([path + "/" + k + ":missing"] if k not in b else differences(v, b[k], path + "/" + k))]
                        if isinstance(a, list):
                            if not isinstance(b, list) or len(a) != len(b): return [path + ":length"]
                            return [x for i, (v, w) in enumerate(zip(a, b)) for x in differences(v, w, path + "/" + str(i))]
                        return [] if type(a) is type(b) and a == b else [path + ":value-or-type"]
                    raise ValueError("Isolated admission mismatch: " + expected["kind"] + "; " + ",".join(differences(expected, actual)[:10])) from error
            with mock.patch.dict(os.environ, {"STORAGE_KUBECONFIG": str(kubeconfig), "HOME": str(root), "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", "")}), mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy, "Client", FixtureClient), mock.patch.object(deploy, "admitted", checked_admission):
                first = deploy.operate(plan, "rke2", mode="check-server", output=root / "private/check")
                test.assertTrue(first["server_checked"]); plan["admitted_core_sha256"] = first["admitted_core_sha256"]
                # No operators/workers are executed. The bounded wait must stop
                # after all original objects were admitted and created.
                with test.assertRaises(TimeoutError):
                    deploy.operate(plan, "rke2", mode="create", output=root / "private/create", wait_seconds=5)
                proof = json.loads((root / "private/create/rook-bootstrap-proof.json").read_text())
                phases = renderer.objects(plan, "rke2")
                test.assertEqual(len(proof["objects"]), sum(len(v) for v in phases.values()))
                test.assertTrue(all(e["uid"] is not None for e in proof["objects"].values()))
                for obj in sum(phases.values(), []):
                    current = deploy.shared.get(Client(plan["target"]), obj)
                    obj["metadata"]["annotations"] = {**obj["metadata"].get("annotations", {}), deploy.OPERATION: proof["operation_id"], deploy.CONTRACT: proof["plan_sha256"]}
                    deploy.owned(current, obj, proof, proof["objects"][deploy.shared.key(obj)])
                test.assertFalse(proof["bootstrap_resources_ready"])
                test.assertTrue(all(proof[k] is False for k in renderer.UNVERIFIED))
        finally:
            for process, log in reversed(processes):
                if process.poll() is None:
                    if os.getpgid(process.pid) != process.pid: raise ValueError("A fixture process escaped its original group")
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
                log.close()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--child": child()
