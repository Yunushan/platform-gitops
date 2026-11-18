#!/usr/bin/env python3
"""Exercise fresh provider admission, device-proof consumption and original-handle recovery."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_ceph_provider as deploy
import render_ceph_provider as renderer
from test_ceph_provider import fixture
from test_ceph_storage import FakeClient as Workloads, uid, condition, owner


class FakeClient(Workloads):
    def __init__(self, plan):
        self.selected = plan; self.plan = {"provider": {"namespace": plan["operator"]["namespace"]}}
        self.data = {}; self.logs = {}; self.counter = 100; self.calls = []; self.attempts = []; self.created = []
        self.lost_ack = None; self.reject = None; self.bad_probe = None; self.expired = False; self.inject = None
        self.data[(None, "namespace/kube-system")] = {"metadata": {"uid": plan["target"]["cluster_uid"]}}
        for name, identity in ((plan["namespace"], plan["namespace_uid"]), (plan["operator"]["namespace"], plan["operator"]["namespace_uid"])):
            self.data[(None, "namespace/" + name)] = {"metadata": {"uid": identity, "labels": {"pod-security.kubernetes.io/enforce": "privileged"}}}
        self.data[(None, "customresourcedefinition/cephclusters.ceph.rook.io")] = {"metadata": {"uid": plan["operator"]["cluster_crd_uid"]}, "spec": {"versions": [{"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": renderer.schema()}}]}, "status": {"conditions": condition("Established")}}
        for index, node in enumerate(plan["nodes"]):
            machine = str(index + 1) * 32; node["machine_id_sha256"] = hashlib.sha256(machine.encode()).hexdigest()
            self.data[(None, "node/" + node["name"])] = {"metadata": {"uid": node["uid"], "labels": {"kubernetes.io/hostname": node["name"], plan["domain_label"]: node["failure_domain"]}}, "status": {"conditions": condition("Ready"), "nodeInfo": {"machineID": machine, "bootID": node["boot_id"], "operatingSystem": "linux", "kernelVersion": "6.8.0-fixture"}}}
        operator = plan["operator"]; self.workload("deployment", operator["deployment"], operator["uid"], operator["image"], [plan["nodes"][0]["name"]])
        dep = self.data[(operator["namespace"], "deployment/" + operator["deployment"])]
        dep["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "ROOK_CURRENT_NAMESPACE_ONLY", "value": "false"}]
        configs = {"rook-ceph-operator-config": {"ROOK_ENABLE_DISCOVERY_DAEMON": "false", "ROOK_DISABLE_DEVICE_HOTPLUG": "true", "ROOK_CEPH_ALLOW_LOOP_DEVICES": "false", "ROOK_DELETE_UNUSED_CRUSH_RULES": "false", "ROOK_CURRENT_NAMESPACE_ONLY": str(operator["namespace"] == plan["namespace"]).lower()},
            "rook-csi-operator-image-set-configmap": {"plugin": plan["images"]["ceph"]}}
        for name, data in configs.items():
            plan["operator"]["configmaps"][name]["data_sha256"] = renderer.storage.fingerprint(data)
            self.data[(operator["namespace"], "configmap/" + name)] = {"metadata": {"name": name, "uid": operator["configmaps"][name]["uid"]}, "immutable": True, "data": data}

    def get(self, resource, namespace=None, *, optional=False):
        if resource == "cephclusters.ceph.rook.io":
            return {"items": [copy.deepcopy(v) for (ns, key), v in self.data.items() if ns == namespace and key.startswith("cephcluster.ceph.rook.io/")]}
        return super().get(resource, namespace, optional=optional)

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append(tuple(args))
        if args[0] == "config": return json.dumps({"current-context": "storage", "contexts": [{"name": "storage", "context": {"cluster": "storage"}}], "clusters": [{"name": "storage", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] == "get":
            prefix = "secret/" if args[1] == "secrets" else "configmap/"
            namespace = args[args.index("--namespace") + 1]
            return "\n".join(v["metadata"]["name"] for (ns, key), v in self.data.items() if ns == namespace and key.startswith(prefix))
        if args[0] == "logs": return self.logs[args[1].split("/", 1)[1]]
        if args[0] != "create": raise AssertionError("Unexpected provider operation")
        obj = json.loads(payload)
        if self.inject: self.inject(obj)
        if "--dry-run=server" in args: return json.dumps(obj)
        identity = deploy.shared.key(obj); self.attempts.append(identity)
        if self.reject == obj["kind"]: raise ValueError("Fixture request rejected before write")
        obj["metadata"].update(uid=self.next_uid(), generation=1)
        resource = deploy.shared.resource_name({**obj["metadata"], "apiVersion": obj["apiVersion"], "kind": obj["kind"]})
        self.data[(self.selected["namespace"], resource)] = copy.deepcopy(obj); self.created.append(identity)
        if obj["kind"] == "Job": self.complete_probe(obj)
        if obj["kind"] == "CephCluster": self.complete_cluster(obj, resource)
        if self.lost_ack == obj["kind"]:
            self.lost_ack = None; raise ValueError("Fixture create acknowledgement lost")
        return json.dumps(self.data[(self.selected["namespace"], resource)])

    def complete_probe(self, job):
        spec = job["spec"]["template"]["spec"]; node = next(n for n in self.selected["nodes"] if n["name"] == spec["nodeName"])
        scope = renderer.scope(self.selected, node)
        config_name = next(v["configMap"]["name"] for v in spec["volumes"] if v["name"] == "program")
        program = self.data[(self.selected["namespace"], "configmap/" + config_name)]["data"]["probe.py"]
        value = {"schema_version": 1, "scope_sha256": renderer.storage.fingerprint(scope), "probe_sha256": hashlib.sha256(program.encode("utf-8")).hexdigest(),
            "observed_at": (datetime.now(timezone.utc) - timedelta(hours=1) if self.expired else datetime.now(timezone.utc)).isoformat(), "fresh_devices_observed": True,
            "devices": [{**{k: v for k, v in d.items() if k != "path"}, "zero_sha256": "e" * 64} for d in scope["devices"]],
            "fencing_verified": False, "provider_installed": False, "ha_verified": False, "full_stack_verified": False}
        if self.bad_probe: self.bad_probe(value)
        pod_name = job["metadata"]["name"] + "-pod"
        self.logs[pod_name] = json.dumps(value)
        state = {"phase": "Succeeded", "containerStatuses": [{"name": "probe", "restartCount": 0, "imageID": self.selected["images"]["probe"], "state": {"terminated": {"exitCode": 0}}}]}
        self.data[(self.selected["namespace"], "pod/" + pod_name)] = {"metadata": {"name": pod_name, "uid": self.next_uid(), "ownerReferences": owner("Job", job["metadata"]["uid"])}, "spec": copy.deepcopy(spec), "status": state}
        resource = "job.batch/" + job["metadata"]["name"]
        self.data[(self.selected["namespace"], resource)]["status"] = {"succeeded": 1, "conditions": condition("Complete")}

    def complete_cluster(self, cluster, resource):
        self.plan["provider"]["namespace"] = self.selected["namespace"]
        status = {"observedGeneration": 1, "phase": "Ready", "ceph": {"health": "HEALTH_OK", "fsid": uid(999), "versions": {}}}
        for role, count in (("mon", 3), ("mgr", 2), ("osd", 4)):
            status["ceph"]["versions"][role] = {"ceph version 20.2.4 (fixture) tentacle (stable)": count}
            for i in range(count):
                self.workload("deployment", "rook-ceph-" + role + "-" + str(i), self.next_uid(), self.selected["images"]["ceph"], [self.selected["nodes"][i]["name"]],
                    controller=("CephCluster", cluster["metadata"]["uid"]), labels={"app": "rook-ceph-" + role})
        self.data[(self.selected["namespace"], resource)]["status"] = status


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture(); self.client = FakeClient(self.plan)
        operation = self.plan["cluster_id"].replace("-", "")
        objects = [*renderer.probe_objects(self.plan, operation), renderer.cluster(self.plan)]
        self.plan["admitted_sha256"] = renderer.storage.fingerprint({deploy.shared.key(o): renderer.storage.fingerprint(deploy.shared.material(o)) for o in objects})
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / "private").mkdir()
        self.patcher = mock.patch.object(deploy, "ROOT", self.root); self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.output = self.root / "private/provider"

    def run_provider(self, mode="create"): return deploy.operate(self.plan, "rke2", mode=mode, output=self.output, client=self.client)

    def test_local_and_admission_modes_never_initialize_devices(self):
        self.assertFalse(deploy.operate(self.plan, "raw")["provider_resource_ready"])
        result = self.run_provider("check-server"); self.assertEqual(result["admitted_sha256"], self.plan["admitted_sha256"]); self.assertFalse(self.client.created)

    def test_cluster_creation_follows_all_complete_original_probes(self):
        result = self.run_provider(); self.assertTrue(result["provider_resource_ready"]); self.assertTrue(result["devices_observed"])
        self.assertEqual(self.client.created[-1], "CephCluster/" + self.plan["name"])
        for key in ("provider_installed", "ha_verified", "full_stack_verified", "node_loss_verified"): self.assertFalse(result[key])
        self.assertTrue(self.run_provider("resume")["provider_resource_ready"]); self.assertEqual(len(self.client.attempts), 6)

    def test_lost_cluster_acknowledgement_only_observes_original_initialization(self):
        self.client.lost_ack = "CephCluster"
        with self.assertRaises(ValueError): self.run_provider()
        self.assertTrue(self.run_provider("resume")["provider_resource_ready"])
        self.assertEqual(self.client.attempts.count("CephCluster/" + self.plan["name"]), 1)

    def test_absent_uncertain_cluster_request_cannot_be_repeated(self):
        self.client.reject = "CephCluster"
        with self.assertRaises(ValueError): self.run_provider()
        self.client.reject = None
        with self.assertRaises(ValueError): self.run_provider("resume")
        self.assertEqual(self.client.attempts.count("CephCluster/" + self.plan["name"]), 1)

    def test_invalid_or_expired_probes_prevent_cluster_creation(self):
        for index, invalid in enumerate(("scope", "expired", "unpackaged-program")):
            plan = fixture(); client = FakeClient(plan); plan["admitted_sha256"] = self.plan["admitted_sha256"]
            if invalid == "scope": client.bad_probe = lambda value: value.update(scope_sha256="0" * 64)
            elif invalid == "unpackaged-program":
                source_digest = hashlib.sha256(Path(renderer.probe.__file__).read_bytes()).hexdigest()
                client.bad_probe = lambda value: value.update(probe_sha256=source_digest)
            else: client.expired = True
            with self.assertRaises(ValueError): deploy.operate(plan, "rke2", mode="create", output=self.root / "private" / str(index), client=client)
            self.assertFalse(any(k.startswith("CephCluster/") for k in client.attempts))

    def test_admission_cannot_add_an_unreviewed_host_mount(self):
        def inject(obj):
            if obj["kind"] == "Job": obj["spec"]["template"]["spec"]["volumes"].append({"name": "foreign", "hostPath": {"path": "/", "type": "Directory"}})
        self.client.inject = inject
        with self.assertRaises(ValueError): self.run_provider()
        self.assertFalse(self.client.attempts)

    def test_admission_cannot_add_unselected_osd_devices(self):
        self.client.inject = lambda obj: obj["spec"]["storage"].update(devices=[{"name": "/dev/foreign"}]) if obj["kind"] == "CephCluster" else None
        with self.assertRaises(ValueError): self.run_provider()
        self.assertFalse(self.client.attempts)

    def test_original_node_boot_identity_is_required(self):
        self.client.data[(None, "node/" + self.plan["nodes"][0]["name"])]["status"]["nodeInfo"]["bootID"] = uid(777)
        with self.assertRaises(ValueError): self.run_provider()
        self.assertFalse(self.client.attempts)

    def test_bootstrap_configuration_identity_and_discovery_policy_are_required(self):
        key = (self.plan["operator"]["namespace"], "configmap/rook-ceph-operator-config")
        self.client.data[key]["data"]["ROOK_ENABLE_DISCOVERY_DAEMON"] = "true"
        # Even an updated digest cannot approve unbounded discovery in this adapter.
        self.plan["operator"]["configmaps"]["rook-ceph-operator-config"]["data_sha256"] = renderer.storage.fingerprint(self.client.data[key]["data"])
        with self.assertRaises(ValueError): self.run_provider()
        self.assertFalse(self.client.attempts)

    def test_same_namespace_bootstrap_configs_are_allowed_but_foreign_state_is_not(self):
        plan = fixture(); plan["operator"].update(namespace=plan["namespace"], namespace_uid=plan["namespace_uid"])
        client = FakeClient(plan)
        operation = plan["cluster_id"].replace("-", "")
        objects = [*renderer.probe_objects(plan, operation), renderer.cluster(plan)]
        plan["admitted_sha256"] = renderer.storage.fingerprint({deploy.shared.key(o): renderer.storage.fingerprint(deploy.shared.material(o)) for o in objects})
        key = (plan["namespace"], "configmap/rook-ceph-csi-config")
        client.data[key] = {"metadata": {"name": "rook-ceph-csi-config", "uid": uid(887),
                                        "ownerReferences": owner("Deployment", plan["operator"]["uid"])},
                            "data": {"csi-cluster-config-json": "[]"}}
        self.assertTrue(deploy.operate(plan, "rke2", mode="check-server", output=self.root / "private/same-namespace", client=client)["server_checked"])
        client.data[key]["data"]["csi-cluster-config-json"] = '[{"clusterID":"existing"}]'
        with self.assertRaises(ValueError):
            deploy.operate(plan, "rke2", mode="check-server", output=self.root / "private/old-csi-clusters", client=client)
        client.data[key]["data"]["csi-cluster-config-json"] = "[]"
        client.data[key]["metadata"]["ownerReferences"] = owner("Deployment", uid(886))
        with self.assertRaises(ValueError):
            deploy.operate(plan, "rke2", mode="check-server", output=self.root / "private/foreign-csi-owner", client=client)
        client.data[key]["metadata"]["ownerReferences"] = owner("Deployment", plan["operator"]["uid"])
        client.data[(plan["namespace"], "configmap/rook-ceph-old-state")] = {"metadata": {"name": "rook-ceph-old-state", "uid": uid(888)}}
        with self.assertRaises(ValueError): deploy.operate(plan, "rke2", mode="check-server", output=self.root / "private/foreign-state", client=client)
        self.assertFalse(client.attempts)


if __name__ == "__main__": unittest.main()
