#!/usr/bin/env python3
"""Modeled API failure/recovery contracts; no real Gerrit, Ceph or HA qualification."""

from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from uuid import UUID

import deploy_gerrit_sites as deploy
from test_gerrit_sites import fixture as site_fixture
from test_gerrit_storage import scope


def uid(number): return str(UUID(int=number))


def fixture():
    site_plan = site_fixture(); site_plan.update(schema_version=2, shared_storage=scope())
    for site in site_plan["sites"]: site["config_hashes"]["etc/healthcheck.config"] = "e" * 64
    return {"schema_version": 1, "target": {"kubeconfig_env": "GERRIT_TEST_KUBECONFIG", "context": "disposable", "cluster_uid": uid(1)},
        "namespace_uid": uid(2), "site_plan": site_plan, "domain_label": "topology.kubernetes.io/zone",
        "nodes": {name: {"name": "worker-" + str(index), "uid": uid(10 + index), "failure_domain": "zone-" + str(index)}
            for index, name in enumerate(("gerrit-a", "gerrit-b"), 1)},
        "claims": {name: {"uid": uid(20 + index), "pv": "pv-" + str(index), "pv_uid": uid(30 + index),
            "csi_sha256": deploy.values.fingerprint(csi(index))}
            for index, name in enumerate(("gerrit-shared", "gerrit-a-site", "gerrit-b-site"))}, "admitted_sha256": None}


def csi(index):
    return {"driver": "rook-ceph.cephfs.csi.ceph.com" if index == 0 else "rook-ceph.rbd.csi.ceph.com", "volumeHandle": "modeled-volume-" + str(index)}


def true(kind): return [{"type": kind, "status": "True"}]


class FakeClient:
    def __init__(self, plan):
        self.plan = plan; self.data = {}; self.calls = []; self.created = []; self.counter = 100
        self.lose = None; self.failed_check = False; self.pod_injection = False; self.delay = False
        self.data[(None, "namespace/kube-system")] = {"metadata": {"uid": plan["target"]["cluster_uid"]}}
        namespace = plan["site_plan"]["namespace"]
        self.data[(None, "namespace/" + namespace)] = {"metadata": {"uid": plan["namespace_uid"]}}
        for node in plan["nodes"].values():
            self.data[(None, "node/" + node["name"])] = {"metadata": {"uid": node["uid"], "labels": {
                "kubernetes.io/hostname": node["name"], plan["domain_label"]: node["failure_domain"]}},
                "status": {"conditions": true("Ready"), "nodeInfo": {"operatingSystem": "linux"}}}
        for index, (name, expected) in enumerate(plan["claims"].items()):
            modes = ["ReadWriteMany"] if index == 0 else ["ReadWriteOnce"]
            self.data[(namespace, "persistentvolumeclaim/" + name)] = {"metadata": {"uid": expected["uid"]},
                "spec": {"volumeName": expected["pv"], "accessModes": modes}, "status": {"phase": "Bound"}}
            self.data[(None, "persistentvolume/" + expected["pv"])] = {"metadata": {"uid": expected["pv_uid"]},
                "spec": {"accessModes": modes, "claimRef": {"name": name, "namespace": namespace, "uid": expected["uid"]}, "csi": csi(index)}, "status": {"phase": "Bound"}}

    def frozen(self): return nullcontext(self)

    def next_uid(self): self.counter += 1; return uid(self.counter)

    def get(self, resource, namespace=None, *, optional=False):
        self.calls.append(("get", resource))
        if resource in {"pods", "replicasets"}:
            kind = {"pods": "pod", "replicasets": "replicaset"}[resource]
            return {"items": [copy.deepcopy(obj) for (ns, key), obj in self.data.items() if ns == namespace and key.startswith(kind + "/")]}
        kind, name = resource.split("/", 1); key = kind.split(".", 1)[0] + "/" + name
        return copy.deepcopy(self.data.get((namespace, key)))

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append(tuple(args))
        if args[0] == "config":
            return json.dumps({"current-context": "disposable", "contexts": [{"name": "disposable", "context": {"cluster": "disposable"}}],
                "clusters": [{"name": "disposable", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] != "create": raise AssertionError("Unexpected mutation")
        obj = json.loads(payload); meta = obj["metadata"]; name, namespace = meta["name"], meta["namespace"]
        meta.update(uid=self.next_uid(), generation=1)
        if obj["kind"] == "Job":
            generated = {"batch.kubernetes.io/controller-uid": meta["uid"], "batch.kubernetes.io/job-name": name}
            obj["spec"]["selector"] = {"matchLabels": {"batch.kubernetes.io/controller-uid": meta["uid"]}}
            obj["spec"]["template"]["metadata"]["labels"].update(generated)
        if "--dry-run=server" in args: return json.dumps(obj)
        key = deploy.shared.key(obj); self.created.append(key)
        self.data[(namespace, obj["kind"].lower() + "/" + name)] = obj
        if obj["kind"] in {"Job", "Deployment"}: self.start(obj)
        if self.lose == obj["kind"]: self.lose = None; raise TimeoutError("MODELED_PRIVATE_DIAGNOSTIC")
        return json.dumps(obj)

    def start(self, obj):
        meta = obj["metadata"]; name, namespace = meta["name"], meta["namespace"]
        kind = obj["kind"]; owner_kind, owner_uid = kind, meta["uid"]
        if kind == "Deployment":
            meta.setdefault("annotations", {})["deployment.kubernetes.io/revision"] = "1"
            owner_uid = self.next_uid(); owner_kind = "ReplicaSet"
            self.data[(namespace, "replicaset/" + name + "-rs")] = {"metadata": {"uid": owner_uid,
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [{"kind": "Deployment", "uid": meta["uid"], "controller": True}]}}
            obj["status"] = {"observedGeneration": 1, **{k: 1 for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")}}
        else:
            obj["status"] = {"conditions": true("Failed" if self.failed_check else "Complete"), "failed" if self.failed_check else "succeeded": 1}
        pod = copy.deepcopy(obj["spec"]["template"])
        pod["metadata"].update(name=name + "-pod", uid=self.next_uid(), ownerReferences=[{"kind": owner_kind, "uid": owner_uid, "controller": True}])
        pod["spec"]["nodeName"] = pod["spec"]["nodeSelector"]["kubernetes.io/hostname"]
        if self.pod_injection: pod["spec"]["hostPID"] = True
        pod["status"] = {"phase": "Succeeded" if kind == "Job" else "Running", "conditions": true("Ready"), "containerStatuses": [{
            "name": "gerrit", "ready": True, "restartCount": 0, "containerID": "containerd://" + "c" * 64,
            "imageID": "registry.example.com/java@sha256:" + "b" * 64,
            "state": {"terminated": {"exitCode": 0}} if kind == "Job" else {"running": {}}}]}
        self.data[(namespace, "pod/" + name + "-pod")] = pod


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.private = self.root / "private"; self.private.mkdir()
        self.patch = mock.patch.object(deploy, "ROOT", self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        self.plan = fixture(); self.client = FakeClient(self.plan)
        self.source = mock.patch.object(deploy, "toolset", return_value="f" * 64); self.source.start(); self.addCleanup(self.source.stop)

    def pin(self):
        result = deploy.operate(self.plan, "k3s", mode="check-server", output=self.private / "preview", client=self.client)
        self.plan["admitted_sha256"] = result["admitted_sha256"]

    def run_create(self):
        return deploy.operate(self.plan, "k3s", mode="create", output=self.private / "operation", client=self.client, wait_seconds=5)

    def resume(self):
        return deploy.operate(self.plan, "k3s", mode="resume", output=self.private / "operation", client=self.client, wait_seconds=5)

    def test_local_does_not_construct_client_or_write_and_requires_version2(self):
        with mock.patch.object(deploy, "Client", side_effect=AssertionError("network")):
            result = deploy.operate(self.plan, "rke2")
        self.assertFalse(result["daemons_ready"]); self.assertEqual(list(self.private.iterdir()), [])
        self.plan["site_plan"]["schema_version"] = 1
        with self.assertRaises(ValueError): deploy.operate(self.plan, "k8s")

    def test_activation_gates_daemons_on_both_site_checks_and_keeps_ha_false(self):
        self.pin(); self.assertEqual(self.client.created, [])
        result = self.run_create()
        self.assertTrue(result["site_checks_completed"]); self.assertTrue(result["daemons_ready"])
        for name in deploy.UNVERIFIED: self.assertFalse(result[name])
        self.assertEqual(self.client.created, ["ConfigMap/" + o["metadata"]["name"] if o["kind"] == "ConfigMap" else deploy.shared.key(o)
            for o in sum(deploy.objects(self.plan, "k3s").values(), [])])
        before = self.client.created[:]; self.resume(); self.assertEqual(before, self.client.created)
        self.assertTrue(all(c[0] in {"get", "config", "create"} for c in self.client.calls))

    def test_failed_or_injected_check_never_creates_a_daemon_and_never_repeats_job(self):
        for injection in (False, True):
            with self.subTest(injection=injection):
                plan = fixture(); client = FakeClient(plan)
                preview = deploy.operate(plan, "k3s", mode="check-server", output=self.private / ("preview-" + str(injection)), client=client)
                plan["admitted_sha256"] = preview["admitted_sha256"]
                client.failed_check = not injection; client.pod_injection = injection
                directory = self.private / ("failed-" + str(injection))
                for mode in ("create", "resume"):
                    with self.assertRaises(ValueError): deploy.operate(plan, "k3s", mode=mode, output=directory, client=client)
                self.assertEqual(sum(k.startswith("Job/") for k in client.created), 2)
                self.assertFalse(any(k.startswith(("Deployment/", "Service/")) for k in client.created))

    def test_lost_ack_resumes_only_existing_original_job_or_deployment(self):
        self.pin(); self.client.lose = "Job"
        with self.assertRaises(TimeoutError): self.run_create()
        self.client.lose = "Deployment"
        with self.assertRaises(TimeoutError): self.resume()
        self.resume()
        self.assertEqual(sum(k.startswith("Job/") for k in self.client.created), 2)
        self.assertEqual(sum(k.startswith("Deployment/") for k in self.client.created), 2)

    def test_uncertain_missing_create_never_reissues_and_replaced_handle_refuses(self):
        self.pin(); self.client.lose = "Job"
        with self.assertRaises(TimeoutError): self.run_create()
        original = self.client.data.pop(("gerrit", "job/gerrit-a-verify")); before = self.client.created[:]
        with self.assertRaises(ValueError): self.resume()
        self.assertEqual(before, self.client.created)
        original["metadata"]["uid"] = uid(999)
        self.client.data[("gerrit", "job/gerrit-a-verify")] = original
        # Even unknown UID after lost ACK cannot authorize forged Job selector/controller labels.
        with self.assertRaises(ValueError): self.resume()

    def test_changed_node_claim_volume_or_unrelated_consumer_prevents_mutations(self):
        mutations = [lambda c: c.data[(None, "node/worker-1")]["metadata"].update(uid=uid(999)),
            lambda c: c.data[(None, "persistentvolume/pv-0")]["spec"]["csi"].update(volumeHandle="another-volume"),
            lambda c: c.data[("gerrit", "persistentvolumeclaim/gerrit-shared")]["metadata"].update(uid=uid(999)),
            lambda c: c.data.update({("gerrit", "pod/unrelated"): {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "gerrit-shared"}}]}}})]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                client = FakeClient(self.plan); mutate(client)
                with self.assertRaises(ValueError): deploy.operate(self.plan, "k8s", mode="check-server", output=self.private / ("bad-" + str(index)), client=client)
                self.assertEqual(client.created, [])

    def test_modified_original_process_and_unreviewed_admission_refuse(self):
        self.pin(); self.run_create()
        pod = self.client.data[("gerrit", "pod/gerrit-a-pod")]
        pod["status"]["containerStatuses"][0]["restartCount"] = 1
        with self.assertRaises(ValueError): self.resume()
        journal = json.loads((self.private / "operation/gerrit-activation.json").read_text())
        self.assertFalse(journal["daemons_ready"])
        obj = deploy.objects(self.plan, "k8s")["guards"][1]; injected = copy.deepcopy(obj)
        injected["spec"]["template"]["spec"]["hostNetwork"] = True
        with self.assertRaises(ValueError): deploy.admitted(obj, injected)

    def test_pending_check_waits_and_replacement_is_not_a_fresh_success(self):
        self.pin(); self.client.lose = "Job"
        with self.assertRaises(TimeoutError): self.run_create()
        journal = json.loads((self.private / "operation/gerrit-activation.json").read_text())
        phases = deploy.objects(self.plan, "k3s")
        for obj in phases["guards"]:
            obj["metadata"].setdefault("annotations", {}).update({deploy.OPERATION: journal["operation_id"], deploy.CONTRACT: journal["plan_sha256"]})
        job = phases["guards"][1]; job_uid = self.client.data[("gerrit", "job/gerrit-a-verify")]["metadata"]["uid"]
        journal["objects"][deploy.shared.key(job)]["uid"] = job_uid
        pod = self.client.data[("gerrit", "pod/gerrit-a-verify-pod")]
        pod["spec"].pop("nodeName"); pod["status"]["phase"] = "Pending"
        self.assertFalse(deploy.checks_complete(self.client, self.plan, {"guards": phases["guards"][:2]}, journal))
        pod["metadata"]["uid"] = uid(888)
        with self.assertRaises(ValueError): deploy.checks_complete(self.client, self.plan, {"guards": phases["guards"][:2]}, journal)

    def test_distinct_claims_cannot_alias_the_same_backend(self):
        claim = self.plan["claims"]["gerrit-b-site"]
        actual = self.client.data[(None, "persistentvolume/" + claim["pv"])]
        actual["spec"]["csi"] = csi(1)
        claim["csi_sha256"] = deploy.values.fingerprint(actual["spec"]["csi"])
        with self.assertRaises(ValueError): deploy.operate(self.plan, "k8s", mode="check-server", output=self.private / "alias", client=self.client)
        self.assertEqual(self.client.created, [])

    def test_guard_does_not_execute_java_init_or_write_and_mount_is_not_claimed_readonly(self):
        for runtime in ("rke2", "k3s", "k8s"):
            phases = deploy.objects(self.plan, runtime)
            for config, job in zip(phases["guards"][::2], phases["guards"][1::2]):
                self.assertNotIn("exec java", config["data"]["verify.sh"])
                commands = "\n".join(line for line in config["data"]["verify.sh"].splitlines() if not line.startswith("#"))
                self.assertNotIn("--init", commands)
                self.assertIn("--require-healthcheck", config["data"]["verify.sh"])
                mounts = {v["name"]: v for v in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
                self.assertTrue(mounts["site"]["readOnly"]); self.assertNotIn("readOnly", mounts["shared"])
                self.assertNotIn("fsGroup", job["spec"]["template"]["spec"]["securityContext"])
                self.assertEqual(job["spec"]["backoffLimit"], 0)


class ActualAdmissionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GERRIT_ACTIVATION_TEST_API") == "1", "Explicit disposable API fixture only")
    def test_real_api_admits_packages_and_dryrun_matches_created_material(self):
        from test_rook_api_fixture import exercise
        resources = sum(deploy.objects(fixture(), "k8s").values(), [])
        exercise(self, resource_objects=resources, admission_checker=deploy.admitted)


if __name__ == "__main__": unittest.main()
