#!/usr/bin/env python3
"""Exercise client data exchange, exact CSI binding and original-handle recovery."""

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ceph_client_io as program
import ceph_recovery_read as read_program
import render_ceph_client_check as renderer
import verify_ceph_clients as verify
import verify_ceph_host_recovery as recovery
from test_ceph_storage import FakeClient as StorageClient, fixture as storage_fixture, split_topology_fixture, uid, image, owner, condition


def fixture(storage_plan, proof_hash="d" * 64, client_nodes=None):
    return {"schema_version": 1, "operation_id": uid(9999), "storage_plan_sha256": renderer.storage.fingerprint(storage_plan), "storage_proof_sha256": proof_hash,
        "namespace": "storage-canary", "namespace_uid": uid(9998), "nodes": [{k: n[k] for k in ("name", "uid", "failure_domain")} for n in (client_nodes or storage_plan["nodes"][:2])],
        "image": image("e"), "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "payload_bytes": 65536, "timeout_seconds": 30, "admitted_sha256": None}


class DataTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Client file operations require POSIX descriptors")
    def test_recovery_read_compares_each_original_file_without_modifying_it(self):
        scope = renderer.scope(fixture(storage_fixture()))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operation = root / (".platform-canary-" + scope["operation_id"].replace("-", ""))
            operation.mkdir(mode=0o700)
            fixtures = {name: program.payload(scope, name) for names in read_program.FILES.values() for name in names}
            for name, data in fixtures.items():
                (operation / (name + ".bin")).write_bytes(data)
            for kind, names in read_program.FILES.items():
                self.assertEqual(read_program.read_payloads(vars(program), scope, kind, root),
                                 {name: program.expected(scope, name) for name in names})
            self.assertEqual({path.stem: path.read_bytes() for path in operation.iterdir()}, fixtures)
            (operation / "cephfs-right.bin").write_bytes(b"z" * scope["payload_bytes"])
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                read_program.read_payloads(vars(program), scope, "cephfs", root)

    @unittest.skipUnless(os.name == "posix", "Client file operations require Linux/POSIX descriptors")
    def test_rbd_persists_across_independent_opens_and_refuses_retry_or_corruption(self):
        scope = renderer.scope(fixture(storage_fixture()))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            written = program.exchange(scope, "rbd-write", root)
            self.assertEqual(program.exchange(scope, "rbd-read", root), written)
            with self.assertRaises(ValueError): program.exchange(scope, "rbd-write", root)
            path = next(root.glob("*/rbd.bin")); path.write_bytes(b"x" * scope["payload_bytes"])
            with self.assertRaises(ValueError): program.exchange(scope, "rbd-read", root)

    @unittest.skipUnless(os.name == "posix", "Client file operations require Linux/POSIX descriptors")
    def test_cephfs_peers_exchange_fsynced_bytes_before_either_exits(self):
        scope = renderer.scope(fixture(storage_fixture()))
        with tempfile.TemporaryDirectory() as temporary, ThreadPoolExecutor(max_workers=2) as executor:
            jobs = {role: executor.submit(program.exchange, scope, role, Path(temporary)) for role in ("cephfs-left", "cephfs-right")}
            values = {role: result.result(timeout=10) for role, result in jobs.items()}
            self.assertEqual(values["cephfs-left"]["payload_sha256"], values["cephfs-right"]["peer_sha256"])
            self.assertTrue(all(v["peer_barrier_verified"] for v in values.values()))

    @unittest.skipUnless(os.name == "posix", "Client file operations require Linux/POSIX descriptors")
    def test_cephfs_peer_waits_for_payload_publication_and_rejects_published_truncation(self):
        scope = renderer.scope(fixture(storage_fixture()))
        peer = "cephfs-right"
        data = program.payload(scope, peer)
        for truncated in (False, True):
            with self.subTest(truncated=truncated), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                operation = root / (".platform-canary-" + scope["operation_id"].replace("-", ""))
                operation.mkdir(mode=0o700)
                payload = operation / (peer + ".bin")
                payload.write_bytes(data[:128])
                descriptor = os.open(operation, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    def publish_peer():
                        if not truncated:
                            with payload.open("ab") as output:
                                output.write(data[128:])
                                output.flush()
                                os.fsync(output.fileno())
                        program.file_write(descriptor, peer + ".ready", b"")
                        program.file_write(descriptor, peer + ".seen", b"")

                    with mock.patch.object(program.time, "sleep", side_effect=lambda _: publish_peer()) as wait:
                        if truncated:
                            with self.assertRaisesRegex(ValueError, "identity or length differs"):
                                program.exchange(scope, "cephfs-left", root)
                        else:
                            result = program.exchange(scope, "cephfs-left", root)
                            self.assertEqual(result["peer_sha256"], program.expected(scope, peer))
                            self.assertTrue(result["peer_barrier_verified"])
                        wait.assert_called_once()
                finally:
                    os.close(descriptor)

    @unittest.skipUnless(os.name == "posix", "Client file operations require Linux/POSIX descriptors")
    def test_missing_peer_deadline_and_symlinks_are_not_accepted(self):
        scope = renderer.scope(fixture(storage_fixture()))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TimeoutError): program.exchange(scope, "cephfs-left", Path(temporary), deadline=0)
        if os.name != "posix": return
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); program.exchange(scope, "rbd-write", root)
            path = next(root.glob("*/rbd.bin")); path.unlink(); path.symlink_to(root / "absent")
            with self.assertRaises(OSError): program.exchange(scope, "rbd-read", root)

    def test_mount_identity_and_readonly_policy_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mountinfo"
            path.write_text("20 1 8:1 / /volume rw - ext4 /dev/fixture rw\n")
            self.assertEqual(program.mounted("rbd", False, inventory=path), "ext4")
            with self.assertRaises(ValueError): program.mounted("cephfs", False, inventory=path)
            with self.assertRaises(ValueError): program.mounted("rbd", True, inventory=path)
            path.write_text("20 1 8:1 / /volume ro - ext4 /dev/fixture ro\n")
            self.assertEqual(program.mounted("rbd", True, inventory=path), "ext4")

    def test_local_validation_opens_no_mounts_and_writes_no_volume(self):
        plan = fixture(storage_fixture())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scope.json"; path.write_text(json.dumps(renderer.scope(plan)))
            with mock.patch.object(program, "execute", side_effect=AssertionError("live canary")), mock.patch("builtins.print"):
                self.assertEqual(program.main(["--scope", str(path), "--role", "rbd-write"]), 0)
            self.assertEqual(len(list(Path(temporary).iterdir())), 1)


class RenderTests(unittest.TestCase):
    def test_optional_real_client_api_admission(self):
        if os.environ.get("ROOK_CLIENT_TEST_API") != "1": self.skipTest("Opt in to the isolated Kubernetes client-resource admission fixture")
        from test_rook_api_fixture import exercise
        exercise(self, client_check_only=True)

    def test_all_consumers_get_scoped_nonroot_jobs_and_fresh_claims(self):
        storage_plan = storage_fixture(); plan = fixture(storage_plan)
        for runtime in renderer.storage.RUNTIMES:
            objects = renderer.objects(plan, storage_plan, runtime)
            self.assertEqual(len(objects), 7)
            self.assertEqual([o["spec"]["accessModes"] for o in objects[1:3]], [["ReadWriteOnce"], ["ReadWriteMany"]])
            for job in objects[3:]:
                pod = job["spec"]["template"]["spec"]
                self.assertNotIn("nodeName", pod); self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["securityContext"]["runAsUser"], 65532); self.assertEqual(job["spec"]["backoffLimit"], 0)
                self.assertFalse(any("hostPath" in v for v in pod["volumes"]))
            flags = json.loads(renderer.render(plan, storage_plan, runtime)["render-status.json"])
            self.assertTrue(all(flags[k] is False for k in renderer.UNVERIFIED))

    def test_same_nodes_domains_mutable_images_and_unknown_scope_are_refused(self):
        storage_plan = storage_fixture()
        for change in (lambda p: p["nodes"][1].update(name=p["nodes"][0]["name"]), lambda p: p["nodes"][1].update(failure_domain=p["nodes"][0]["failure_domain"]),
                lambda p: p.update(image="python:latest"), lambda p: p.update(payload_bytes=True), lambda p: p.update(password="dummy")):
            plan = fixture(storage_plan); change(plan)
            with self.assertRaises(ValueError): renderer.objects(plan, storage_plan, "rke2")

    def test_client_identity_must_belong_to_the_storage_access_inventory(self):
        storage_plan = split_topology_fixture()
        for field, value in (("name", "unreviewed-client"), ("uid", uid(8877)), ("failure_domain", "unreviewed-domain")):
            plan = fixture(storage_plan, client_nodes=storage_plan["access_nodes"][-2:]); plan["nodes"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): renderer.objects(plan, storage_plan, "rke2")


class FakeClient(StorageClient):
    def configure(self, plan):
        self.canary = plan; self.logs = {}; self.job_names = []; self.inject = None; self.reject_kind = None; self.lost_kind = None; self.bad_log = None; self.hold_writer = False
        self.data[(None, "namespace/" + plan["namespace"])] = {"metadata": {"uid": plan["namespace_uid"], "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
        for n in self.plan["nodes"]: self.data[(None, "node/" + n["name"])]["status"]["nodeInfo"].update(operatingSystem="linux")

    def get(self, resource, namespace=None, *, optional=False):
        if resource == "persistentvolumes": return {"items": [copy.deepcopy(v) for (ns, key), v in self.data.items() if key.startswith("persistentvolume/")]}
        if resource == "volumeattachments.storage.k8s.io": return {"items": [copy.deepcopy(v) for (ns, key), v in self.data.items() if key.startswith("volumeattachment/")]}
        return super().get(resource, namespace, optional=optional)

    def call(self, args, *, payload=None, seconds=30):
        if args[0] == "logs": return self.logs[args[1].split("/", 1)[1]]
        if args[0] != "create" or json.loads(payload)["kind"] not in {"ConfigMap", "PersistentVolumeClaim", "Job"}: return super().call(args, payload=payload, seconds=seconds)
        obj = json.loads(payload)
        if self.inject: self.inject(obj)
        if "--dry-run=server" in args: return json.dumps(obj)
        identity = verify.shared.key(obj); self.attempts.append(identity)
        if self.reject_kind == obj["kind"]: raise ValueError("Fixture create rejected")
        meta = obj["metadata"]; meta.update(uid=self.next_uid(), generation=1); namespace = meta["namespace"]
        resource = verify.shared.resource_name({**meta, "kind": obj["kind"], "apiVersion": obj["apiVersion"]})
        self.data[(namespace, resource)] = copy.deepcopy(obj); self.created.append(identity)
        if obj["kind"] == "PersistentVolumeClaim": self.data[(namespace, resource)]["status"] = {"phase": "Pending"}
        if obj["kind"] == "Job": self.complete_job(obj, resource)
        if self.lost_kind == obj["kind"]:
            self.lost_kind = None; raise ValueError("Fixture acknowledgement lost")
        return json.dumps(self.data[(namespace, resource)])

    def complete_job(self, obj, resource):
        plan = self.canary; namespace = plan["namespace"]; pod_spec = copy.deepcopy(obj["spec"]["template"]["spec"])
        if pod_spec["containers"][0]["command"][-1] == "/program/read.py":
            self.complete_recovery_read(obj, resource, pod_spec)
            return
        args = pod_spec["containers"][0]["args"]; role = args[args.index("--role") + 1]; self.job_names.append(role)
        if role == "rbd-read" and self.hold_writer: raise AssertionError("Reader created before original RBD writer detached")
        kind = "rbd" if role.startswith("rbd-") else "cephfs"; claim_name = renderer.prefix(plan) + "-" + kind
        claim = self.data[(namespace, "persistentvolumeclaim/" + claim_name)]
        if not claim["spec"].get("volumeName"):
            pv_name = "pvc-" + self.next_uid(); claim["spec"]["volumeName"] = pv_name; claim["status"] = {"phase": "Bound", "capacity": {"storage": "1Gi"}}
            p = self.plan["provider"]; csi = {"driver": self.plan["csi"][kind]["driver"], "volumeHandle": "fixture-" + pv_name,
                "volumeAttributes": {"clusterID": p["namespace"], "storage.kubernetes.io/csiProvisionerIdentity": "fixture-provisioner", "pool" if kind == "rbd" else "fsName": self.plan["names"]["block_pool" if kind == "rbd" else "filesystem"]},
                "nodeStageSecretRef": {"name": "rook-csi-" + kind + "-node", "namespace": p["namespace"]}, **({"fsType": "ext4"} if kind == "rbd" else {})}
            self.data[(None, "persistentvolume/" + pv_name)] = {"metadata": {"name": pv_name, "uid": self.next_uid(), "creationTimestamp": datetime.now(timezone.utc).isoformat()}, "status": {"phase": "Bound"}, "spec": {
                "storageClassName": claim["spec"]["storageClassName"], "accessModes": claim["spec"]["accessModes"], "volumeMode": "Filesystem", "capacity": {"storage": "1Gi"},
                "persistentVolumeReclaimPolicy": "Retain", "claimRef": {"kind": "PersistentVolumeClaim", "namespace": namespace, "name": claim_name, "uid": claim["metadata"]["uid"]}, "csi": csi}}
        scope = renderer.scope(plan); node = scope["nodes"][role]; pod_spec["nodeName"] = node; name = obj["metadata"]["name"] + "-pod"
        self.data[(namespace, "pod/" + name)] = {"metadata": {"name": name, "uid": self.next_uid(), "ownerReferences": owner("Job", obj["metadata"]["uid"])}, "spec": pod_spec,
            "status": {"phase": "Succeeded", "containerStatuses": [{"name": "canary", "restartCount": 0, "imageID": plan["image"], "state": {"terminated": {"exitCode": 0}}}]}}
        self.data[(namespace, resource)]["status"] = {"succeeded": 1, "conditions": condition("Complete")}
        identity = "rbd" if role.startswith("rbd-") else role; peer = "cephfs-right" if role == "cephfs-left" else "cephfs-left"
        result = {"schema_version": 1, "operation_id": plan["operation_id"], "scope_sha256": program.digest(scope), "program_sha256": hashlib.sha256(Path(program.__file__).read_bytes()).hexdigest(),
            "role": role, "node": node, "payload_bytes": plan["payload_bytes"], "filesystem": "ext4" if kind == "rbd" else "ceph", "volume_verified": True,
            "payload_sha256": program.expected(scope, identity), "peer_sha256": program.expected(scope, peer) if kind == "cephfs" else None, "peer_barrier_verified": kind == "cephfs",
            "observed_at": datetime.now(timezone.utc).isoformat(), **program.UNVERIFIED}
        if self.bad_log: self.bad_log(result)
        self.logs[name] = json.dumps(result)
        if kind == "rbd" and self.hold_writer:
            self.data[(None, "volumeattachment/fixture-writer")] = {"metadata": {"uid": self.next_uid()}, "spec": {"source": {"persistentVolumeName": claim["spec"]["volumeName"]}, "attacher": self.plan["csi"]["rbd"]["driver"], "nodeName": node}, "status": {"attached": True}}

    def complete_recovery_read(self, job, resource, pod):
        canary = self.canary
        container = pod["containers"][0]
        kind, phase = container["args"]
        scope = renderer.scope(canary)
        pod["nodeName"] = scope["nodes"]["rbd-read"]
        name = job["metadata"]["name"] + "-pod"
        namespace = canary["namespace"]
        self.data[(namespace, "pod/" + name)] = {
            "metadata": {"name": name, "uid": self.next_uid(), "ownerReferences": owner("Job", job["metadata"]["uid"])},
            "spec": pod, "status": {"phase": "Succeeded", "containerStatuses": [{
                "name": "canary", "restartCount": 0, "imageID": canary["image"], "state": {"terminated": {"exitCode": 0}},
            }]},
        }
        self.data[(namespace, resource)]["status"] = {"succeeded": 1, "conditions": condition("Complete")}
        result = {"phase": phase, "node": pod["nodeName"], "scope_sha256": program.digest(scope),
                  "filesystem": "ext4" if kind == "rbd" else "ceph",
                  "payloads_sha256": {identity: program.expected(scope, identity) for identity in read_program.FILES[kind]}}
        if self.bad_log:
            self.bad_log(result)
        self.logs[name] = json.dumps(result)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name); (self.root / "private").mkdir()
        patches = [mock.patch.object(verify, "ROOT", self.root)]
        for patch in patches: patch.start(); self.addCleanup(patch.stop)
        self.configure_storage(storage_fixture())

    def configure_storage(self, storage_plan, client_nodes=None):
        self.storage = storage_plan; self.client = FakeClient(self.storage)
        self.storage["admitted_sha256"] = renderer.storage.fingerprint({verify.shared.key(o): renderer.storage.fingerprint(verify.shared.material(o)) for o in renderer.storage.objects(self.storage, "rke2")})
        original = {"schema_version": 1, "action": "create", "phase": "completed", "runtime": "rke2", "operation_id": "a" * 32, "plan_sha256": renderer.storage.fingerprint(self.storage),
            "toolset_sha256": verify.shared.toolset(), "storage_resources_provisioned": True, "server_checked": True, "local_inputs_verified": True,
            "observed_admitted_sha256": self.storage["admitted_sha256"], "objects": {}, **renderer.storage.UNVERIFIED}
        for obj in renderer.storage.objects(self.storage, "rke2"):
            obj["metadata"]["annotations"] = {verify.shared.OPERATION: original["operation_id"], verify.shared.CONTRACT: original["plan_sha256"]}
            response = json.loads(self.client.call(["create", "-f", "-", "-o", "json"], payload=renderer.storage.canonical(obj).encode()))
            original["objects"][verify.shared.key(obj)] = {"uid": response["metadata"]["uid"], "admitted_sha256": renderer.storage.fingerprint(verify.shared.material(response))}
        self.proof = self.root / "private/storage-proof.json"; self.proof.write_text(json.dumps(original)); self.proof.chmod(0o600)
        self.plan = fixture(self.storage, hashlib.sha256(self.proof.read_bytes()).hexdigest(), client_nodes=client_nodes); self.client.configure(self.plan)
        self.plan["admitted_sha256"] = renderer.storage.fingerprint({verify.shared.key(o): renderer.storage.fingerprint(verify.material(o)) for o in renderer.objects(self.plan, self.storage, "rke2")})
        self.output = self.root / "private/canary"; self.client.attempts.clear(); self.client.created.clear()

    def run_canary(self, mode="create"):
        return verify.operate(self.plan, self.storage, "rke2", self.proof, mode=mode, output=self.output, client=self.client)

    def test_recovery_reads_keep_original_pvs_and_reject_bad_evidence(self):
        self.run_canary()
        receipt = self.output / "ceph-client-proof.json"
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        path = self.root / "private" / "drill"
        self.client.attempts.clear()
        samples = [{"pool_count": 3, "pg_count": 3, "osd_epoch": epoch} for epoch in (10, 11, 12)]

        def observe(phase, *, client_inputs=(self.plan, receipt, digest), samples=None):
            with mock.patch.object(recovery.storage, "ROOT", self.root), mock.patch.object(recovery, "collect"), \
                    mock.patch.object(recovery, "assess", side_effect=samples or [current_sample.copy(), current_sample.copy()]):
                return recovery.operate(self.storage, "rke2", self.proof, self.plan["storage_proof_sha256"],
                                        "storage-1", phase, path, client=self.client, client_inputs=client_inputs)

        current_sample = samples[0]
        self.assertTrue(observe("baseline")["canary_reads_verified"])
        evidence = path / "ceph-host-recovery.json"
        initial = evidence.read_bytes()
        current_sample = samples[1]
        with self.assertRaisesRegex(ValueError, "same immutable recovery scope"):
            observe("rebuilt", client_inputs=None)
        self.client.bad_log = lambda result: result.update(payloads_sha256={})
        with self.assertRaisesRegex(ValueError, "original bytes"):
            observe("rebuilt")
        self.assertEqual(evidence.read_bytes(), initial)
        self.client.bad_log = None
        with self.assertRaisesRegex(ValueError, "state changed"):
            observe("rebuilt", samples=[current_sample.copy(), {**current_sample, "osd_epoch": 99}])
        self.assertEqual(evidence.read_bytes(), initial)
        self.assertTrue(observe("rebuilt")["canary_reads_verified"])
        current_sample = samples[2]
        pv = next(obj for (_, name), obj in self.client.data.items() if name.startswith("persistentvolume/"))
        original_uid = pv["metadata"]["uid"]
        pv["metadata"]["uid"] = uid(8888)
        attempts = self.client.attempts.copy()
        with self.assertRaisesRegex(ValueError, "replaced or rebound"):
            observe("returned")
        self.assertEqual(self.client.attempts, attempts)
        pv["metadata"]["uid"] = original_uid
        self.assertTrue(observe("returned")["canary_reads_verified"])
        proof = json.loads(evidence.read_bytes())
        self.assertFalse(proof["application_io_verified"])
        self.assertEqual(len({result["job_uid"] for sample in proof["observations"].values()
                              for result in sample["canary_reads"].values()}), 6)
        self.assertTrue(all(name.startswith(("ConfigMap/", "Job/")) for name in self.client.attempts))
        for (namespace, resource), obj in self.client.data.items():
            if resource.startswith("job.batch/") and obj["spec"]["template"]["spec"]["containers"][0]["command"][-1] == "/program/read.py":
                pod = obj["spec"]["template"]["spec"]
                self.assertTrue(pod["volumes"][1]["persistentVolumeClaim"]["readOnly"])
                self.assertTrue(pod["containers"][0]["volumeMounts"][1]["readOnly"])

    def test_local_and_server_check_do_not_create_or_write_volumes(self):
        self.assertFalse(verify.operate(self.plan, self.storage, "rke2", self.proof)["kubernetes_data_roundtrip_verified"])
        self.assertEqual(self.run_canary("check-server")["admitted_sha256"], self.plan["admitted_sha256"])
        self.assertFalse(self.client.attempts)

    def test_handoff_exchange_and_resume_retain_original_claims_jobs_and_pvs(self):
        result = self.run_canary(); self.assertTrue(result["kubernetes_data_roundtrip_verified"])
        self.assertTrue(all(result[k] is False for k in program.UNVERIFIED)); self.assertEqual(len(self.client.attempts), 7)
        self.assertEqual(self.client.job_names[-1], "rbd-read")
        self.assertTrue(self.run_canary("resume")["kubernetes_client_mounts_verified"]); self.assertEqual(len(self.client.attempts), 7)
        proof = json.loads((self.output / "ceph-client-proof.json").read_text())
        self.assertEqual(len(proof["volumes"]), 2); self.assertEqual(len(proof["receipts"]), 4)

    def test_workers_outside_osd_hosts_use_separate_operator_namespace(self):
        storage_plan = split_topology_fixture()
        self.configure_storage(storage_plan, client_nodes=storage_plan["access_nodes"][-2:])
        result = self.run_canary()
        self.assertTrue(result["kubernetes_data_roundtrip_verified"])
        self.assertTrue(all(result[k] is False for k in program.UNVERIFIED))
        proof = json.loads((self.output / "ceph-client-proof.json").read_text())
        self.assertEqual({r["node"] for r in proof["receipts"].values()}, {"client-1", "client-2"})
        volumes = self.client.get("persistentvolumes")["items"]
        self.assertEqual(len(volumes), 2)
        for volume in volumes:
            csi = volume["spec"]["csi"]
            self.assertEqual(csi["volumeAttributes"]["clusterID"], storage_plan["provider"]["namespace"])
            self.assertEqual(csi["nodeStageSecretRef"]["namespace"], storage_plan["provider"]["namespace"])
            self.assertTrue(csi["driver"].startswith("rook-system."))
        self.assertTrue(self.run_canary("resume")["kubernetes_client_mounts_verified"])
        self.assertEqual(len(self.client.attempts), 7)

    def test_reader_waits_for_writer_attachment_release(self):
        self.client.hold_writer = True
        def release(_):
            self.assertNotIn("rbd-read", self.client.job_names)
            self.client.hold_writer = False; self.client.data[(None, "volumeattachment/fixture-writer")]["status"]["attached"] = False
        with mock.patch.object(verify.time, "sleep", side_effect=release): self.assertTrue(self.run_canary()["kubernetes_data_roundtrip_verified"])

    def test_unknown_attachment_state_is_not_treated_as_released(self):
        self.client.hold_writer = True; observations = []
        def release(_):
            self.assertNotIn("rbd-read", self.client.job_names); observations.append(True)
            status = self.client.data[(None, "volumeattachment/fixture-writer")]["status"]
            if len(observations) == 1: status.pop("attached")
            else: self.client.hold_writer = False; status["attached"] = False
        with mock.patch.object(verify.time, "sleep", side_effect=release): self.assertTrue(self.run_canary()["kubernetes_data_roundtrip_verified"])
        self.assertEqual(len(observations), 2)

    def test_lost_create_ack_is_observed_and_never_repeated(self):
        self.client.lost_kind = "PersistentVolumeClaim"
        with self.assertRaises(ValueError): self.run_canary()
        self.assertTrue(self.run_canary("resume")["kubernetes_data_roundtrip_verified"])
        self.assertEqual(len(self.client.attempts), len(set(self.client.attempts)))

    def test_lost_writer_job_ack_is_observed_without_restarting_writes(self):
        self.client.lost_kind = "Job"
        with self.assertRaises(ValueError): self.run_canary()
        self.assertTrue(self.run_canary("resume")["kubernetes_data_roundtrip_verified"])
        self.assertEqual(self.client.job_names.count("rbd-write"), 1)
        self.assertEqual(len(self.client.attempts), len(set(self.client.attempts)))

    def test_uncertain_absent_create_cannot_be_retried(self):
        self.client.reject_kind = "Job"
        with self.assertRaises(ValueError): self.run_canary()
        self.client.reject_kind = None
        with self.assertRaises(ValueError): self.run_canary("resume")
        self.assertEqual(len(self.client.attempts), len(set(self.client.attempts)))

    def test_wrong_bytes_or_stale_receipts_cannot_report_success(self):
        for i, mutation in enumerate((lambda r: r.update(payload_sha256="0" * 64), lambda r: r.update(observed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()))):
            self.plan["operation_id"] = uid(7000 + i); self.client.configure(self.plan)
            self.plan["admitted_sha256"] = renderer.storage.fingerprint({verify.shared.key(o): renderer.storage.fingerprint(verify.material(o)) for o in renderer.objects(self.plan, self.storage, "rke2")})
            self.output = self.root / "private" / str(i); self.client.bad_log = mutation
            with self.assertRaises(ValueError): self.run_canary()
            proof = json.loads((self.output / "ceph-client-proof.json").read_text()); self.assertFalse(proof["kubernetes_data_roundtrip_verified"])

    def test_replaced_pv_or_client_pod_invalidates_original_evidence(self):
        self.run_canary()
        pv = next(k for k in self.client.data if k[1].startswith("persistentvolume/")); self.client.data[pv]["metadata"]["uid"] = uid(8888)
        with self.assertRaises(ValueError): self.run_canary("resume")

    def test_admission_cannot_prebind_data_or_add_host_access(self):
        self.client.inject = lambda o: o["spec"].update(volumeName="foreign-data") if o["kind"] == "PersistentVolumeClaim" else None
        with self.assertRaises(ValueError): self.run_canary()
        self.assertFalse(self.client.attempts)

    def test_admission_cannot_enable_retry_policies_or_host_mounts(self):
        def policy(obj):
            if obj["kind"] == "Job": obj["spec"]["podFailurePolicy"] = {"rules": []}
        self.client.inject = policy
        with self.assertRaises(ValueError): self.run_canary()
        self.assertFalse(self.client.attempts)

    def test_available_old_pv_refuses_before_claim_or_job_creation(self):
        self.client.data[(None, "persistentvolume/old-data")] = {"metadata": {"name": "old-data", "uid": uid(4444)}, "status": {"phase": "Available"}, "spec": {"storageClassName": self.storage["names"]["rbd_class"]}}
        with self.assertRaises(ValueError): self.run_canary()
        self.assertFalse(self.client.attempts)

    def test_orphan_pod_cannot_occupy_a_future_canary_claim(self):
        self.client.data[(self.plan["namespace"], "pod/foreign")] = {"metadata": {"name": "foreign", "uid": uid(4444)},
            "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": renderer.prefix(self.plan) + "-rbd"}}]}}
        with self.assertRaises(ValueError): self.run_canary()
        self.assertFalse(self.client.attempts)

    def test_original_pod_replacement_is_not_accepted(self):
        self.run_canary()
        key = next(k for k in self.client.data if k[0] == self.plan["namespace"] and k[1].startswith("pod/"))
        self.client.data[key]["metadata"]["uid"] = uid(8888)
        with self.assertRaises(ValueError): self.run_canary("resume")


if __name__ == "__main__": unittest.main()
