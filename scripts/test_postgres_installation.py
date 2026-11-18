#!/usr/bin/env python3
"""Exercise fresh CNPG admission, identity, data non-adoption and same-handle recovery."""

import base64
from contextlib import nullcontext
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bounded_subprocess import run_bounded
import deploy_postgres_ha as deploy
import render_postgres_ha as renderer
from test_postgres_ha import fixture, write_package


def uid(number): return "00000000-0000-0000-0000-" + str(number).zfill(12)
def conditions(*names): return [{"type": name, "status": "True"} for name in names]
def owner(kind, identity): return [{"kind": kind, "uid": identity, "controller": True}]


# Public test-only certificate. Its ephemeral generation key was discarded.
PUBLIC_CA = b"""-----BEGIN CERTIFICATE-----
MIIBqjCCAVGgAwIBAgIUDYrUTc7rezaZZfjJsJsWt8/QncUwCgYIKoZIzj0EAwIw
IzEhMB8GA1UEAwwYZml4dHVyZS1vbmx5LXBvc3RncmVzLWNhMB4XDTI2MTEwNjIy
MjA1N1oXDTM2MTEwMzIyMjA1N1owIzEhMB8GA1UEAwwYZml4dHVyZS1vbmx5LXBv
c3RncmVzLWNhMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEGh6/Rw36xWeh1a9d
I0GTWb9Qt4Rtmicp2KWgGOXl4scSUiblIDax0n4sf2IUK+qzl0ppTesEscmrIgTn
pN63MKNjMGEwHQYDVR0OBBYEFBabFQODLLzZ0K71XmbSDFUzOS3DMB8GA1UdIwQY
MBaAFBabFQODLLzZ0K71XmbSDFUzOS3DMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0P
AQH/BAQDAgEGMAoGCCqGSM49BAMCA0cAMEQCIGJ+DciSrEMWHX4bKD9cIaYqaEQV
nNLP5y7pF3iJAJ/tAiBHmOUSYyn2cHyO9JUY9G8w3e3WQ89RtJ+1dKkrNe2d1Q==
-----END CERTIFICATE-----
"""


def target_plan(digest, selected):
    return {"schema_version": 1, "contract_sha256": digest, "target": {"kubeconfig_env": "DATABASE_KUBECONFIG", "context": "database", "cluster_uid": uid(10)},
        "namespace_uid": uid(11), "operator": {"namespace": "cnpg-system", "namespace_uid": uid(12), "deployment": "cnpg-controller-manager", "uid": uid(13),
            "image": "registry.example.com/cnpg:1.30.0@sha256:" + "f" * 64, "crd_uid": uid(14)},
        "storage_class_uid": uid(15), "storage_parameters_sha256": deploy.fingerprint({"clusterID": "rook-ceph"}),
        "secrets": {selected["tls"]["server_ca_secret"]: {"uid": uid(16), "type": "Opaque"}, selected["tls"]["server_secret"]: {"uid": uid(17), "type": "kubernetes.io/tls"},
                    selected["bootstrap"]["secret"]: {"uid": uid(18), "type": "kubernetes.io/basic-auth"}},
        "admitted_spec_sha256": deploy.fingerprint(deploy.policy.cnpg(selected)["spec"])}


class FakeClient:
    def __init__(self, plan, selected):
        self.plan, self.selected, self.calls, self.objects, self.collections = plan, selected, [], {}, {}
        self.cluster = None; self.live = True; self.lost_ack = False; self.reject_before_write = False; self.mutate_admission = None
        self.created = 0; self.secret_mutable = False; self.generated_secret = False; self.real_admission = None
        self.namespace, self.name = selected["namespace"], selected["name"]
        operator = plan["operator"]
        self.objects["namespace/kube-system"] = {"metadata": {"uid": plan["target"]["cluster_uid"]}}
        for name, identity in ((self.namespace, plan["namespace_uid"]), (operator["namespace"], operator["namespace_uid"])): self.objects["namespace/" + name] = {"metadata": {"uid": identity, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
        self.objects["customresourcedefinition/clusters.postgresql.cnpg.io"] = {"metadata": {"uid": operator["crd_uid"]}, "spec": {"versions": [{"name": "v1", "served": True, "storage": True,
            "schema": {"openAPIV3Schema": json.loads(renderer.cnpg_schema_text())}}]}, "status": {"conditions": conditions("Established")}}
        for member in selected["members"]:
            self.objects["node/" + member["node_name"]] = {"metadata": {"uid": member["node_uid"], "labels": {"kubernetes.io/hostname": member["node_name"], selected["failure_domain_key"]: member["failure_domain"]}},
                "status": {"conditions": conditions("Ready"), "addresses": [{"type": "InternalIP", "address": member["address"]}]}}
        container = {"name": "manager", "image": operator["image"], "command": ["/manager"], "args": ["controller", "--leader-elect"], "env": [{"name": "OPERATOR_IMAGE_NAME", "value": operator["image"]}]}
        self.objects["deployment/" + operator["deployment"]] = {"metadata": {"uid": operator["uid"], "generation": 2, "labels": {"app.kubernetes.io/version": selected["cnpg_version"]},
            "annotations": {"deployment.kubernetes.io/revision": "1"}}, "spec": {"replicas": 2, "template": {"spec": {"containers": [container]}}},
            "status": {"observedGeneration": 2, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}}
        self.collections["replicasets", operator["namespace"]] = [{"metadata": {"uid": uid(20), "ownerReferences": owner("Deployment", operator["uid"]), "annotations": {"deployment.kubernetes.io/revision": "1"}}}]
        self.collections["pods", operator["namespace"]] = [self.pod("operator-" + str(i), uid(21 + i), m["node_name"], operator["image"], "ReplicaSet", uid(20)) for i, m in enumerate(selected["members"][:2])]
        self.objects["storageclass/" + selected["storage"]["class"]] = {"metadata": {"uid": plan["storage_class_uid"]}, "provisioner": "rook-ceph.rbd.csi.ceph.com", "reclaimPolicy": "Retain", "allowVolumeExpansion": True, "parameters": {"clusterID": "rook-ceph"}}
        self.collections["persistentvolumes", None] = []
        for kind in ("pods", "persistentvolumeclaims", "services", "endpointslices.discovery.k8s.io"): self.collections[kind, self.namespace] = []

    @staticmethod
    def pod(name, identity, node, image, kind, controller):
        return {"metadata": {"name": name, "uid": identity, "ownerReferences": owner(kind, controller)}, "spec": {"nodeName": node, "containers": [{"name": "main", "image": image}]},
                "status": {"phase": "Running", "conditions": conditions("Ready"), "containerStatuses": [{"name": "main", "ready": True, "state": {"running": {}}}]}}

    def frozen(self): return nullcontext(self)
    def get(self, resource, namespace=None, optional=False):
        self.calls.append(("get", resource))
        if resource == deploy.CLUSTER + self.name: return copy.deepcopy(self.cluster)
        return copy.deepcopy(self.objects.get(resource))

    def call(self, args, payload=None, seconds=30):
        self.calls.append(tuple(args))
        if args[:2] == ["config", "view"]:
            return json.dumps({"current-context": "database", "contexts": [{"name": "database", "context": {"cluster": "selected"}}],
                "clusters": [{"name": "selected", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] == "get" and args[1].startswith("secret/"):
            name = args[1].split("/", 1)[1]
            if "--ignore-not-found=true" in args: return uid(90) if self.generated_secret else ""
            pin = self.plan["secrets"][name]
            if ".immutable" in args[-1]:
                keys = {"ca.crt"} if name == self.selected["tls"]["server_ca_secret"] else {"tls.crt", "tls.key"} if name == self.selected["tls"]["server_secret"] else {"username", "password"}
                return "\n".join([pin["uid"], pin["type"], "false" if self.secret_mutable else "true", *("true" if key in keys else "false" for key in ("ca.crt", "tls.crt", "tls.key", "username", "password"))]) + "\n"
            if '"ca.crt"' in args[-1]: return base64.b64encode(PUBLIC_CA).decode()
            if '"username"' in args[-1]: return base64.b64encode(self.selected["bootstrap"]["owner"].encode()).decode()
            raise AssertionError("Private Secret values must never be requested")
        if args[0] == "get":
            namespace = args[args.index("--namespace") + 1] if "--namespace" in args else None
            return json.dumps({"items": copy.deepcopy(self.collections.get((args[1], namespace), []))})
        if args[0] == "create":
            obj = json.loads(payload)
            if "--dry-run=server" in args:
                if self.mutate_admission: self.mutate_admission(obj)
                return json.dumps(obj)
            if self.reject_before_write: raise TimeoutError("Creation outcome unknown and no resource observed")
            if self.cluster is not None: raise ValueError("Already exists")
            if self.real_admission: self.real_admission(obj)
            self.created += 1; self.cluster = obj; self.cluster["metadata"].update(uid=uid(30), generation=1)
            if self.live: self.make_ready()
            if self.lost_ack:
                self.lost_ack = False; raise TimeoutError("Acknowledgment lost after successful create")
            return json.dumps(self.cluster)
        raise AssertionError("Unexpected Kubernetes action")

    def make_ready(self):
        name, namespace = self.name, self.namespace
        pods = []
        for index, member in enumerate(self.selected["members"], start=1):
            pod = self.pod(name + "-" + str(index), uid(30 + index), member["node_name"], self.selected["images"]["postgres"], "Cluster", uid(30))
            claim_name = name + "-" + str(index); volume_name = "volume-" + str(index)
            pod["spec"]["volumes"] = [{"name": "pgdata", "persistentVolumeClaim": {"claimName": claim_name}}]
            pod["status"]["podIP"] = "198.51.100." + str(index)
            claim = {"metadata": {"name": claim_name, "uid": uid(40 + index), "ownerReferences": owner("Cluster", uid(30))},
                "spec": {"storageClassName": self.selected["storage"]["class"], "volumeName": volume_name, "accessModes": ["ReadWriteOnce"]}, "status": {"phase": "Bound"}}
            volume = {"metadata": {"name": volume_name, "uid": uid(50 + index)}, "status": {"phase": "Bound"}, "spec": {"persistentVolumeReclaimPolicy": "Retain", "storageClassName": self.selected["storage"]["class"],
                "claimRef": {"name": claim_name, "namespace": namespace, "uid": claim["metadata"]["uid"]}, "csi": {"driver": "rook-ceph.rbd.csi.ceph.com", "volumeHandle": volume_name}}}
            self.objects["persistentvolumeclaim/" + claim_name] = claim; self.objects["persistentvolume/" + volume_name] = volume
            pods.append(pod)
        self.collections["pods", namespace] = pods
        self.collections["persistentvolumeclaims", namespace] = [self.objects["persistentvolumeclaim/" + p["metadata"]["name"]] for p in pods]
        self.cluster["status"] = {"conditions": conditions("Ready"), "instances": 3, "readyInstances": 3, "currentPrimary": pods[0]["metadata"]["name"], "targetPrimary": pods[0]["metadata"]["name"],
            "instancesReportedState": {p["metadata"]["name"]: {"isPrimary": i == 0, "timeLineID": 1} for i, p in enumerate(pods)}}
        self.objects["service/" + name + "-rw"] = {"metadata": {"name": name + "-rw", "uid": uid(60), "ownerReferences": owner("Cluster", uid(30))}, "spec": {"type": "ClusterIP", "ports": [{"port": 5432}]}}
        self.collections["endpointslices.discovery.k8s.io", namespace] = [{"metadata": {"ownerReferences": owner("Service", uid(60))}, "ports": [{"port": 5432, "protocol": "TCP"}],
            "endpoints": [{"conditions": {"ready": True}, "addresses": [pods[0]["status"]["podIP"]], "targetRef": {"kind": "Pod", "namespace": namespace, "name": pods[0]["metadata"]["name"], "uid": pods[0]["metadata"]["uid"]}}]}]


class InstallationTests(unittest.TestCase):
    def test_optional_real_kubectl_secret_projection_is_metadata_and_presence_only(self):
        binary = os.environ.get("POSTGRES_INSTALL_TEST_KUBECTL")
        if not binary: self.skipTest("Set the local kubectl binary for client-only Go-template verification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client)
            template = next(c[-1] for c in client.calls if c[0] == "get" and len(c) > 1 and c[1].startswith("secret/") and ".immutable" in c[-1])
            config = root / "offline-kubeconfig"
            # A synthetic static credential avoids kubectl's interactive
            # missing-auth prompt; dry-run=client makes no server request.
            config.write_text(json.dumps({"apiVersion": "v1", "kind": "Config", "current-context": "offline", "contexts": [{"name": "offline", "context": {"cluster": "offline", "user": "offline"}}],
                "clusters": [{"name": "offline", "cluster": {"server": "https://127.0.0.1:1"}}], "users": [{"name": "offline", "user": {"token": "fixture-only-unused-token"}}]}))
            marker = "fixture-only-do-not-print-value"
            command = [binary, "--kubeconfig", str(config), "--context", "offline", "--namespace", "fixture", "create", "secret", "generic", "fixture", "--dry-run=client", "-o", template]
            command += ["--from-literal=" + key + "=" + marker for key in ("ca.crt", "tls.crt", "tls.key", "username", "password")]
            result = run_bounded(command, input="", text=True, timeout=20, output_max_bytes=65536, check=False)
            self.assertEqual(result.returncode, 0, "Client-only template parsing failed; diagnostics suppressed")
            self.assertEqual(result.stdout.splitlines()[3:], ["true"] * 5)
            self.assertNotIn(marker, result.stdout)

    def prepare(self, root, runtime="rke2"):
        selected = fixture(); selected["tls"]["ca_sha256"]["postgres"] = hashlib.sha256(PUBLIC_CA).hexdigest()
        bundle, digest = write_package(root, selected, runtime)
        plan = target_plan(digest, selected)
        return bundle, plan, FakeClient(plan, selected)

    def run_install(self, root, bundle, plan, client, mode="apply"):
        with mock.patch.object(deploy, "ROOT", root), mock.patch.object(deploy.time, "sleep"):
            return deploy.operate(bundle, plan, mode=mode, output=root / "private/operation", client=client, wait_seconds=5)

    def test_default_constructs_no_client_and_reads_no_kubeconfig(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle, plan, _ = self.prepare(Path(temporary))
            with mock.patch.object(deploy, "Client", side_effect=AssertionError("Client constructed")):
                self.assertTrue(deploy.operate(bundle, plan)["local_inputs_verified"])

    def test_all_three_kubernetes_runtimes_create_once_and_keep_database_acceptance_false(self):
        for runtime in ("rke2", "k3s", "k8s"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root, runtime)
                result = self.run_install(root, bundle, plan, client)
                self.assertTrue(result["resources_ready"]); self.assertEqual(client.created, 1)
                for key in ("synchronous_replication_verified", "promotion_verified", "restore_verified", "ha_verified", "full_stack_verified"): self.assertFalse(result[key])
                self.assertTrue(self.run_install(root, bundle, plan, client, "resume")["read_only"])
                self.assertEqual(client.created, 1)

    def test_check_server_writes_concrete_snapshot_without_creating_a_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); plan["admitted_spec_sha256"] = None
            result = self.run_install(root, bundle, plan, client, "check-server")
            snapshot = json.loads((root / "private/operation/admitted-spec.json").read_text())
            self.assertEqual(result["admitted_spec_sha256"], deploy.fingerprint(snapshot)); self.assertEqual(client.created, 0)
            self.assertTrue(result["read_only"])

    def test_lost_creation_ack_resumes_same_uid_without_another_create(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); client.lost_ack = True
            with self.assertRaises(TimeoutError): self.run_install(root, bundle, plan, client)
            self.assertEqual(client.created, 1)
            self.assertTrue(self.run_install(root, bundle, plan, client, "resume")["resources_ready"])
            self.assertEqual(client.created, 1)

    def test_uncertain_absent_creation_is_only_observed_not_repeated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); client.reject_before_write = True
            with self.assertRaises(TimeoutError): self.run_install(root, bundle, plan, client)
            before = len([c for c in client.calls if c[0] == "create" and "--dry-run=server" not in c])
            with mock.patch.object(deploy.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError): self.run_install(root, bundle, plan, client, "resume")
            self.assertEqual(len([c for c in client.calls if c[0] == "create" and "--dry-run=server" not in c]), before)

    def test_unpinned_mutable_or_changed_prerequisites_refuse_every_cluster_write(self):
        changes = [lambda c: c.objects["namespace/kube-system"]["metadata"].update(uid=uid(99)),
            lambda c: c.objects["node/db-1"]["metadata"]["labels"].update({"topology.kubernetes.io/zone": "wrong-zone"}),
            lambda c: c.objects["storageclass/ceph-rbd"].update(reclaimPolicy="Delete"), lambda c: setattr(c, "secret_mutable", True),
            lambda c: c.objects["deployment/cnpg-controller-manager"]["status"].update(availableReplicas=1),
            lambda c: c.objects["deployment/cnpg-controller-manager"]["spec"]["template"]["spec"]["containers"][0]["env"][0].update(value="operator:latest")]
        for change in changes:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root); change(client)
                with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client)
                self.assertEqual(client.created, 0)

    def test_old_children_persistent_volumes_or_generated_secrets_are_never_adopted(self):
        changes = [lambda c: c.collections["persistentvolumeclaims", c.namespace].append({"metadata": {"name": c.name + "-1", "uid": uid(99)}}),
            lambda c: c.collections["persistentvolumes", None].append({"spec": {"storageClassName": "ceph-rbd"}, "status": {"phase": "Available"}}),
            lambda c: setattr(c, "generated_secret", True), lambda c: setattr(c, "cluster", {"metadata": {"uid": uid(99)}})]
        for change in changes:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root); change(client)
                with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client)
                self.assertEqual(client.created, 0)

    def test_weakened_admission_and_unreviewed_defaults_are_refused(self):
        for mutate in (lambda obj: obj["spec"]["postgresql"]["synchronous"].update(dataDurability="preferred"), lambda obj: obj["spec"].update(logLevel="debug")):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root); client.mutate_admission = mutate
                with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client)
                self.assertEqual(client.created, 0)

    def test_new_resource_uid_or_policy_drift_cannot_be_resumed(self):
        for mutate in (lambda c: c.cluster["metadata"].update(uid=uid(99)), lambda c: c.cluster["spec"]["postgresql"]["synchronous"].update(number=2)):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client); mutate(client)
                with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client, "resume")
                self.assertEqual(client.created, 1)

    def test_ready_status_cannot_hide_wrong_endpoint_shared_disk_or_foreign_claim(self):
        changes = [lambda c: c.collections["endpointslices.discovery.k8s.io", c.namespace][0]["endpoints"][0]["targetRef"].update(uid=uid(99)),
            lambda c: c.objects["persistentvolume/volume-2"]["spec"]["csi"].update(volumeHandle="volume-1"),
            lambda c: c.objects["persistentvolumeclaim/platform-postgres-1"]["metadata"].update(ownerReferences=owner("Cluster", uid(99)))]
        for change in changes:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client); change(client)
                with mock.patch.object(deploy.time, "monotonic", side_effect=itertools.count()), self.assertRaises(TimeoutError): self.run_install(root, bundle, plan, client, "resume")
                self.assertEqual(client.created, 1)

    def test_no_private_secret_json_or_password_key_is_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client)
            secret_calls = [c for c in client.calls if len(c) > 1 and c[0] == "get" and c[1].startswith("secret/")]
            self.assertTrue(secret_calls)
            self.assertTrue(all("json" not in c for c in secret_calls))
            self.assertFalse(any(c[-1] in {'go-template={{index .data "password"}}', 'go-template={{index .data "tls.key"}}'} for c in secret_calls))

    def test_public_trust_validation_does_not_open_an_ambient_tls_keylog_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root)
            with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": str(root / "not-written.log")}): self.run_install(root, bundle, plan, client)
            self.assertFalse((root / "not-written.log").exists())

    def test_prerequisite_race_after_dry_run_cannot_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root)
            client.mutate_admission = lambda _: client.objects["node/db-1"]["metadata"].update(uid=uid(99))
            with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client)
            self.assertEqual(client.created, 0)

    def test_create_response_uid_is_retained_even_when_real_admission_has_drifted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root)
            client.real_admission = lambda obj: obj["spec"]["postgresql"]["synchronous"].update(dataDurability="preferred")
            with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client)
            receipt = json.loads((root / "private/operation/postgres-install-proof.json").read_text())
            self.assertEqual(receipt["cluster_uid"], uid(30)); self.assertTrue(receipt["creation_attempted"])
            self.assertFalse(receipt["resources_ready"])
            with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client, "resume")
            self.assertEqual(client.created, 1)

    def test_missing_original_cluster_is_never_recreated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client)
            client.cluster = None
            with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client, "resume")
            self.assertEqual(client.created, 1)

    def test_changed_deployment_plan_or_mutated_admission_has_no_implicit_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client)
            changed = copy.deepcopy(plan); changed["target"]["context"] = "another-cluster"
            with self.assertRaises(ValueError): self.run_install(root, bundle, changed, client, "resume")
            self.assertEqual(client.created, 1)

    def test_private_journal_lock_rejects_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with deploy.lock(root):
                with self.assertRaises(OSError):
                    with deploy.lock(root): self.fail("Concurrent writer obtained lock")

    def test_changed_installer_toolset_cannot_resume_an_old_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); self.run_install(root, bundle, plan, client)
            count = len(client.calls)
            with mock.patch.object(deploy, "toolset", return_value="0" * 64), self.assertRaises(ValueError): self.run_install(root, bundle, plan, client, "resume")
            self.assertEqual(len(client.calls), count)

    def test_apply_without_reviewed_admission_does_not_construct_a_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root); plan["admitted_spec_sha256"] = None
            with mock.patch.object(deploy, "Client", side_effect=AssertionError("Client constructed")), self.assertRaises(ValueError):
                self.run_install(root, bundle, plan, None)
            self.assertFalse((root / "private/operation").exists())

    def test_reviewed_hash_cannot_enable_data_imports_or_sql_hooks_in_fresh_mode(self):
        changes = [lambda spec: spec["storage"].update(pvcTemplate={"dataSource": {"name": "old-data", "kind": "VolumeSnapshot"}}),
            lambda spec: spec["bootstrap"]["initdb"].update(postInitSQL=["SELECT 1"]),
            lambda spec: spec.update(imageCatalogRef={"name": "another-catalog", "kind": "ClusterImageCatalog"})]
        for change in changes:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); bundle, plan, client = self.prepare(root)
                reviewed = deploy.policy.cnpg(client.selected)["spec"]; change(reviewed); plan["admitted_spec_sha256"] = deploy.fingerprint(reviewed)
                client.mutate_admission = lambda obj: change(obj["spec"])
                with self.assertRaises(ValueError): self.run_install(root, bundle, plan, client)
                self.assertEqual(client.created, 0)

    def test_current_operator_pods_may_use_tolerated_control_plane_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, plan, client = self.prepare(root)
            for index, pod in enumerate(client.collections["pods", plan["operator"]["namespace"]], start=1):
                name = "control-plane-" + str(index); pod["spec"]["nodeName"] = name
                client.objects["node/" + name] = {"metadata": {"uid": uid(100 + index), "labels": {"topology.kubernetes.io/zone": "rack-" + str(index)}},
                    "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]}, "status": {"conditions": conditions("Ready")}}
            self.assertTrue(self.run_install(root, bundle, plan, client)["resources_ready"])


if __name__ == "__main__": unittest.main()
