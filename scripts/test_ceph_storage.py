#!/usr/bin/env python3
"""Exercise finite Ceph resources, capacity refusal, admission and uncertain-create recovery."""

from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import provision_ceph_storage as provision
import render_ceph_storage as policy


def uid(number): return "00000000-0000-0000-0000-" + str(number).zfill(12)
def image(letter): return "registry.example.com/fixture@sha256:" + letter * 64
def owner(kind, identity): return [{"kind": kind, "uid": identity, "controller": True}]
def condition(kind): return [{"type": kind, "status": "True"}]


def fixture():
    plan = {"schema_version": 2, "target": {"kubeconfig_env": "STORAGE_KUBECONFIG", "context": "storage", "cluster_uid": uid(1)},
        "provider": {"namespace": "rook-ceph", "namespace_uid": uid(2), "cluster": "rook-ceph", "cluster_uid": uid(3), "fsid": uid(4), "spec_sha256": "d" * 64,
            "image": image("f"), "ceph_version": "20.2.4", "domain_label": "topology.kubernetes.io/zone",
            "operator": {"namespace": "rook-ceph", "namespace_uid": uid(2), "deployment": "rook-ceph-operator", "uid": uid(5), "image": image("a"), "cluster_crd_uid": uid(70),
                "configmaps": {name: {"uid": uid(71 + i), "data_sha256": "0" * 64} for i, name in enumerate(("rook-ceph-operator-config", "rook-csi-operator-image-set-configmap"))}},
            "toolbox": {"deployment": "rook-ceph-tools", "uid": uid(6), "pod": "rook-ceph-tools-pod-0", "pod_uid": uid(7), "container": "tools", "image": image("f")},
            "crds": {name: uid(8 + i) for i, name in enumerate(policy.CRDS)}},
        "nodes": [{"name": "storage-" + str(i), "uid": uid(10 + i), "failure_domain": "rack-" + str(i), "crush_host": "storage-" + str(i)} for i in range(1, 5)],
        "csi": {kind: {"namespace": "rook-ceph", "namespace_uid": uid(2), "driver": "rook-ceph." + kind + ".csi.ceph.com", "controller": {"name": "csi-" + kind + "-provisioner", "uid": uid(20 + i), "images": [image("b")]},
            "node_plugin": {"name": "csi-" + kind + "-node", "uid": uid(30 + i), "images": [image("c")]}, "driver_uid": uid(40 + i)} for i, kind in enumerate(("rbd", "cephfs"))},
        "capacity": {"rbd_bytes": 10 * 1024 ** 3, "cephfs_data_bytes": 10 * 1024 ** 3, "cephfs_metadata_bytes": 1024 ** 3, "recovery_reserve_percent": 25},
        "mds_resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
        "names": {"block_pool": "platform-rbd", "filesystem": "platform-shared", "rbd_class": "ceph-rbd", "cephfs_class": "cephfs"}, "admitted_sha256": None}
    plan["provider"]["spec_sha256"] = policy.fingerprint({"cephVersion": {"image": plan["provider"]["image"]}})
    plan["access_nodes"] = [{k: n[k] for k in ("name", "uid", "failure_domain")} for n in plan["nodes"]]
    return plan


def operator_configs(plan):
    return {"rook-ceph-operator-config": {"ROOK_ENABLE_DISCOVERY_DAEMON": "false", "ROOK_DISABLE_DEVICE_HOTPLUG": "true", "ROOK_CEPH_ALLOW_LOOP_DEVICES": "false", "ROOK_DELETE_UNUSED_CRUSH_RULES": "false",
            "ROOK_CURRENT_NAMESPACE_ONLY": str(plan["provider"]["operator"]["namespace"] == plan["provider"]["namespace"]).lower()}, "rook-csi-operator-image-set-configmap": {"plugin": image("b")}}


def split_topology_fixture():
    plan = fixture(); plan["provider"]["operator"].update(namespace="rook-system", namespace_uid=uid(990))
    plan["access_nodes"] += [{"name": "client-" + str(i), "uid": uid(500 + i), "failure_domain": "rack-" + str(i)} for i in (1, 2)]
    for kind, config in plan["csi"].items(): config.update(namespace="rook-system", namespace_uid=uid(990), driver="rook-system." + kind + ".csi.ceph.com")
    return plan


class FakeClient:
    def __init__(self, plan):
        self.plan = plan; self.data = {}; self.created = []; self.attempts = []; self.calls = []; self.mutation = None; self.lost_ack = False; self.reject = False; self.counter = 100
        p = plan["provider"]; namespace = p["namespace"]
        self.data[(None, "namespace/kube-system")] = {"metadata": {"uid": plan["target"]["cluster_uid"]}}
        self.data[(None, "namespace/" + namespace)] = {"metadata": {"uid": p["namespace_uid"]}}
        self.data[(namespace, "cephcluster.ceph.rook.io/" + p["cluster"])] = {"metadata": {"uid": p["cluster_uid"], "generation": 1}, "spec": {"cephVersion": {"image": p["image"]}}, "status": {"observedGeneration": 1, "phase": "Ready", "ceph": {"fsid": p["fsid"]}}}
        for name, schema in policy.schemas().items():
            self.data[(None, "customresourcedefinition/" + name)] = {"metadata": {"uid": p["crds"][name]}, "spec": {"versions": [{"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": schema}}]}, "status": {"conditions": condition("Established")}}
        for n in plan["access_nodes"]:
            self.data[(None, "node/" + n["name"])] = {"metadata": {"uid": n["uid"], "labels": {"kubernetes.io/hostname": n["name"], p["domain_label"]: n["failure_domain"]}}, "status": {"conditions": condition("Ready"), "nodeInfo": {"operatingSystem": "linux", "kernelVersion": "6.12.1"}}}
        operator = p["operator"]
        self.data[(None, "namespace/" + operator["namespace"])] = {"metadata": {"uid": operator["namespace_uid"]}}
        self.data[(None, "customresourcedefinition/cephclusters.ceph.rook.io")] = {"metadata": {"uid": operator["cluster_crd_uid"]}, "spec": {"versions": [{"name": "v1", "served": True, "storage": True,
            "schema": {"openAPIV3Schema": provision.cluster_policy.schema()}}]}, "status": {"conditions": condition("Established")}}
        for name, data in operator_configs(plan).items():
            operator["configmaps"][name]["data_sha256"] = policy.fingerprint(data)
            self.data[(operator["namespace"], "configmap/" + name)] = {"metadata": {"name": name, "uid": operator["configmaps"][name]["uid"]}, "immutable": True, "data": data}
        self.workload("deployment", operator["deployment"], operator["uid"], operator["image"], ["storage-1"], namespace=operator["namespace"])
        self.data[(operator["namespace"], "deployment/" + operator["deployment"])]["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "ROOK_CURRENT_NAMESPACE_ONLY", "value": str(operator["namespace"] == namespace).lower()}]
        self.workload("deployment", p["toolbox"]["deployment"], p["toolbox"]["uid"], p["image"], ["storage-1"], pod_uid=p["toolbox"]["pod_uid"], container="tools")
        for role, count in (("mon", 3), ("mgr", 2)):
            for i in range(count): self.workload("deployment", "rook-ceph-" + role + "-" + str(i), uid(50 + i + (10 if role == "mgr" else 0)), p["image"], ["storage-" + str(i + 1)], controller=("CephCluster", p["cluster_uid"]), labels={"app": "rook-ceph-" + role})
        for kind, config in plan["csi"].items():
            self.data[(None, "namespace/" + config["namespace"])] = {"metadata": {"uid": config["namespace_uid"]}}
            workers = [n["name"] for n in plan["access_nodes"] if n["name"] not in {s["name"] for s in plan["nodes"]}]
            controllers = workers[:2] if len(workers) >= 2 else [n["name"] for n in plan["access_nodes"][:2]]
            self.workload("deployment", config["controller"]["name"], config["controller"]["uid"], image("b"), controllers, namespace=config["namespace"])
            self.workload("daemonset", config["node_plugin"]["name"], config["node_plugin"]["uid"], image("c"), [n["name"] for n in plan["access_nodes"]], namespace=config["namespace"])
            self.data[(None, "csidriver/" + config["driver"])] = {"metadata": {"uid": config["driver_uid"]}, "spec": {"volumeLifecycleModes": ["Persistent"], "attachRequired": kind == "rbd"}}
        self.views = {("status",): {"fsid": p["fsid"], "health": {"status": "HEALTH_OK"}, "monmap": {"epoch": 10}, "quorum_names": ["a", "b", "c"], "mgrmap": {"available": True, "active_name": "mgr-a", "standbys": [{"name": "mgr-b"}]}},
            ("mon", "dump"): {"fsid": p["fsid"], "epoch": 10, "mons": [{"name": n} for n in ("a", "b", "c")]},
            ("versions",): {role: {"ceph version 20.2.4 (fixture) tentacle (stable)": 4 if role == "osd" else 3 if role == "mon" else 2} for role in ("mon", "mgr", "osd")},
            ("osd", "dump"): {"osds": [{"osd": i, "up": 1, "in": 1} for i in range(4)], "pools": []},
            ("osd", "df", "tree"): {"nodes": [{"id": i, "type": "osd", "kb": 200 * 1024 ** 2, "kb_used": 5 * 1024 ** 2, "kb_avail": 195 * 1024 ** 2} for i in range(4)]},
            ("osd", "crush", "dump"): {"buckets": [{"id": -1, "name": "default", "type_name": "root", "items": [{"id": -10 - i} for i in range(4)]},
                *[{"id": -10 - i, "name": n["crush_host"], "type_name": "host", "items": [{"id": i}]} for i, n in enumerate(plan["nodes"])]], "rules": [{"rule_id": 1, "steps": [{"op": "take", "item": -1, "item_name": "default"}, {"op": "chooseleaf_firstn", "num": 0, "type": "host"}, {"op": "emit"}]}]},
            ("fs", "dump"): {"filesystems": [], "standbys": []}}

    def next_uid(self): self.counter += 1; return uid(self.counter)

    def workload(self, kind, name, identity, pin, nodes, *, controller=None, labels=None, pod_uid=None, container="service", namespace=None):
        namespace = namespace or self.plan["provider"]["namespace"]; labels = labels or {}; desired = len(nodes)
        meta = {"name": name, "uid": identity, "namespace": namespace, "generation": 1, "labels": labels, "annotations": {"deployment.kubernetes.io/revision": "1"}}
        if controller: meta["ownerReferences"] = owner(*controller)
        workload = {"metadata": meta, "spec": {"replicas": desired, "template": {"spec": {"containers": [{"name": container, "image": pin}]}}}, "status": {"observedGeneration": 1}}
        if kind == "deployment":
            workload["status"].update({k: desired for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")})
            rs = self.next_uid(); self.data[(namespace, "replicaset/" + name)] = {"metadata": {"uid": rs, "ownerReferences": owner("Deployment", identity), "annotations": {"deployment.kubernetes.io/revision": "1"}}}
            parent = ("ReplicaSet", rs)
        else:
            workload["status"].update({k: desired for k in ("desiredNumberScheduled", "currentNumberScheduled", "updatedNumberScheduled", "numberReady", "numberAvailable")}); parent = ("DaemonSet", identity)
        self.data[(namespace, kind + "/" + name)] = workload
        for i, node in enumerate(nodes):
            pod_name = name + "-pod-" + str(i)
            self.data[(namespace, "pod/" + pod_name)] = {"metadata": {"name": pod_name, "uid": pod_uid or self.next_uid(), "labels": labels, "ownerReferences": owner(*parent)},
                "spec": {"nodeName": node, "containers": [{"name": container, "image": pin}]}, "status": {"phase": "Running", "conditions": condition("Ready"), "containerStatuses": [{"name": container, "ready": True, "state": {"running": {}}, "imageID": pin, "containerID": "containerd://fixture-" + pod_name, "restartCount": 0}]}}

    def frozen(self): return nullcontext()

    def get(self, resource, namespace=None, *, optional=False):
        self.calls.append(("get", resource, namespace))
        plural = {"pods": "pod/", "replicasets": "replicaset/", "deployments": "deployment/"}
        if resource in plural: return {"items": [copy.deepcopy(value) for (ns, key), value in self.data.items() if ns == namespace and key.startswith(plural[resource])]}
        return copy.deepcopy(self.data.get((namespace, resource)))

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append(tuple(args))
        if args[0] == "config": return json.dumps({"current-context": "storage", "contexts": [{"name": "storage", "context": {"cluster": "storage"}}], "clusters": [{"name": "storage", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] == "exec":
            start = args.index("ceph") + 2
            return json.dumps(self.views[tuple(args[start:-1])])
        if args[0] != "create": raise AssertionError("Unexpected provider mutation")
        obj = json.loads(payload)
        if self.mutation: self.mutation(obj)
        if "--dry-run=server" in args: return json.dumps(obj)
        self.attempts.append(provision.key(obj))
        if self.reject: raise ValueError("fixture rejected before acknowledgement")
        obj["metadata"]["uid"] = self.next_uid(); obj["status"] = {"phase": "Ready"}
        namespace = obj["metadata"].get("namespace")
        resource = provision.resource_name({**obj["metadata"], "apiVersion": obj["apiVersion"], "kind": obj["kind"]})
        self.data[(namespace, resource)] = copy.deepcopy(obj); self.created.append(provision.key(obj))
        names = self.plan["names"]
        def pool(name, spec, identity): self.views[("osd", "dump")]["pools"].append({"pool_name": name, "pool": identity, "type": 1, "size": 3, "min_size": 2, "crush_rule": 1, "quota_max_bytes": spec["quotas"]["maxBytes"]})
        if obj["kind"] == "CephBlockPool": pool(names["block_pool"], obj["spec"], 1)
        if obj["kind"] == "CephFilesystem":
            pool(names["filesystem"] + "-metadata", obj["spec"]["metadataPool"], 2); pool(names["filesystem"] + "-data", obj["spec"]["dataPools"][0], 3)
            info = {"1": {"name": names["filesystem"] + "-a", "state": "up:active"}, "2": {"name": names["filesystem"] + "-b", "state": "up:standby-replay"}}
            self.views[("fs", "dump")]["filesystems"] = [{"mdsmap": {"fs_name": names["filesystem"], "metadata_pool": 2, "data_pools": [3], "max_mds": 1, "info": info}}]
            for i, letter in enumerate(("a", "b")):
                self.workload("deployment", "rook-ceph-mds-" + names["filesystem"] + "-" + letter, self.next_uid(), self.plan["provider"]["image"], ["storage-" + str(i + 1)],
                    controller=("CephFilesystem", obj["metadata"]["uid"]), labels={"app": "rook-ceph-mds", "ceph_daemon_id": names["filesystem"] + "-" + letter})
        if self.lost_ack: self.lost_ack = False; raise ValueError("fixture acknowledgement lost")
        return json.dumps(obj)


class StorageTests(unittest.TestCase):
    def test_all_runtimes_render_scoped_replication_and_retention_without_deployment(self):
        for runtime in policy.RUNTIMES:
            plan = fixture(); files = policy.render(plan, runtime); self.assertEqual(len(policy.objects(plan, runtime)), 4)
            flags = provision.operate(plan, runtime)
            self.assertTrue(flags["local_inputs_verified"]); self.assertFalse(flags["storage_resources_provisioned"])
            for key in policy.UNVERIFIED: self.assertFalse(flags[key])
            self.assertIn("WaitForFirstConsumer", files["ceph-storage.yaml"])

    def test_csi_classes_request_encrypted_transport_and_the_reviewed_fsid(self):
        plan = fixture(); classes = policy.objects(plan, "rke2")[2:]
        self.assertEqual(classes[0]["parameters"]["mapOptions"], "krbd:ms_mode=secure,fsid=" + plan["provider"]["fsid"])
        self.assertEqual(classes[0]["parameters"]["tryOtherMounters"], "false")
        self.assertEqual(classes[1]["parameters"]["kernelMountOptions"], "ms_mode=secure,fsid=" + plan["provider"]["fsid"])

    def test_unsafe_scopes_and_unbounded_capacity_refuse(self):
        changes = [lambda p: p["nodes"].pop(), lambda p: p["nodes"][1].update(uid=p["nodes"][0]["uid"]), lambda p: p["nodes"][1].update(failure_domain=p["nodes"][0]["failure_domain"]),
            lambda p: p["capacity"].update(recovery_reserve_percent=0), lambda p: p["capacity"].update(rbd_bytes=True), lambda p: p["provider"].update(image="ceph:latest"),
            lambda p: p["provider"].update(ceph_version="19.2.3"), lambda p: p["csi"]["rbd"]["controller"].update(images=["latest"]),
            lambda p: p["mds_resources"]["limits"].update(memory="512Mi"), lambda p: p["mds_resources"]["requests"].update(cpu="0"), lambda p: p.update(password="dummy")]
        for change in changes:
            plan = fixture(); change(plan)
            with self.assertRaises(ValueError): policy.objects(plan, "rke2")

    def test_access_inventory_requires_matching_storage_identities_and_explicit_operator(self):
        changes = [lambda p: p.update(schema_version=1), lambda p: p["access_nodes"].pop(), lambda p: p["access_nodes"][0].update(uid=uid(888)),
            lambda p: p["access_nodes"].append(dict(p["access_nodes"][0])), lambda p: p["provider"]["operator"].pop("namespace_uid"),
            lambda p: p["provider"]["operator"].update(namespace_uid=uid(888)), lambda p: p["provider"]["operator"]["configmaps"].pop("rook-ceph-operator-config")]
        for change in changes:
            plan = fixture(); change(plan)
            with self.assertRaises(ValueError): policy.validate(plan, "rke2")

    def test_optional_real_upstream_schema_validation(self):
        if os.environ.get("CEPH_STORAGE_TEST_SCHEMA") != "1": self.skipTest("Opt in to jsonschema against pinned upstream Rook schemas")
        import jsonschema
        plan = fixture(); schemas = policy.schemas()
        for runtime in policy.RUNTIMES:
            for obj in policy.objects(plan, runtime)[:2]:
                schema = schemas["cephblockpools.ceph.rook.io" if obj["kind"] == "CephBlockPool" else "cephfilesystems.ceph.rook.io"]
                jsonschema.Draft4Validator(schema).validate(obj)


class ProvisionTests(unittest.TestCase):
    def test_old_or_unreported_client_kernel_refuses_before_allocation(self):
        for index, value in enumerate(("5.10.99", "")):
            self.output = self.root / "private" / ("kernel-" + str(index))
            self.client.data[(None, "node/storage-1")]["status"]["nodeInfo"]["kernelVersion"] = value
            with self.assertRaises(ValueError): self.run_create()
            self.assertFalse(self.client.attempts)

    def setUp(self):
        self.plan = fixture(); self.client = FakeClient(self.plan)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / "private").mkdir()
        self.patch = mock.patch.object(provision, "ROOT", self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        self.output = self.root / "private/create"
        self.plan["admitted_sha256"] = policy.fingerprint({provision.key(o): policy.fingerprint(provision.material(o)) for o in policy.objects(self.plan, "rke2")})

    def run_create(self, mode="create", runtime="rke2"): return provision.operate(self.plan, runtime, mode=mode, output=self.output, client=self.client)

    def test_server_check_is_read_only_and_returns_concrete_admission_pin(self):
        result = self.run_create("check-server")
        self.assertEqual(result["admitted_sha256"], self.plan["admitted_sha256"]); self.assertFalse(self.client.created)
        self.assertTrue(result["server_checked"])

    def test_create_provisions_four_resources_and_verifies_redundant_services(self):
        result = self.run_create(); self.assertTrue(result["storage_resources_provisioned"])
        self.assertEqual(len(self.client.created), 4)
        for key in policy.UNVERIFIED: self.assertFalse(result[key])
        proof = json.loads((self.output / "ceph-storage-proof.json").read_text()); self.assertEqual(proof["phase"], "completed")
        self.assertTrue(self.run_create("resume")["storage_resources_provisioned"]); self.assertEqual(len(self.client.attempts), 4)

    def test_lost_acknowledgement_observes_original_without_repeating_create(self):
        self.client.lost_ack = True
        with self.assertRaises(ValueError): self.run_create()
        self.assertEqual(len(self.client.created), 1)
        self.assertTrue(self.run_create("resume")["storage_resources_provisioned"])
        self.assertEqual(len(set(self.client.attempts)), 4); self.assertEqual(len(self.client.attempts), 4)

    def test_missing_uncertain_write_cannot_be_retried_or_adopted(self):
        self.client.reject = True
        with self.assertRaises(ValueError): self.run_create()
        self.client.reject = False
        with self.assertRaises(ValueError): self.run_create("resume")
        self.assertEqual(len(self.client.attempts), 1)

    def test_data_or_names_already_present_refuse_before_create(self):
        self.client.views[("osd", "dump")]["pools"] = [{"pool_name": self.plan["names"]["block_pool"]}]
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)

    def test_provider_and_quorum_identity_drift_refuse_before_create(self):
        for index, mutation in enumerate((lambda c: c.views[("status",)].update(fsid=uid(999)), lambda c: c.views[("status",)]["mgrmap"].update(standbys=[]),
            lambda c: c.views[("status",)].update(quorum_names=["a", "b"]), lambda c: c.views[("osd", "dump")]["osds"][0].update(up=0))):
            client = FakeClient(self.plan); mutation(client)
            with self.assertRaises(ValueError): provision.operate(self.plan, "rke2", mode="create", output=self.root / "private" / str(index), client=client)
            self.assertFalse(client.attempts)

    def test_imbalanced_or_insufficient_recovery_capacity_refuses(self):
        for mutation in (lambda c: c.views[("osd", "df", "tree")]["nodes"][0].update(kb_used=190 * 1024 ** 2),
            lambda c: c.views[("osd", "df", "tree")]["nodes"][0].update(kb=10 * 1024 ** 2, kb_used=5 * 1024 ** 2, kb_avail=5 * 1024 ** 2)):
            client = FakeClient(self.plan); mutation(client)
            with self.assertRaises(ValueError): provision.observe(client, self.plan)

    def test_redundant_controller_placement_and_pinned_images_are_required(self):
        pod = self.client.data[("rook-ceph", "pod/csi-rbd-provisioner-pod-1")]; pod["spec"]["nodeName"] = "storage-1"
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)

    def test_weakened_admission_is_rejected(self):
        self.client.mutation = lambda obj: obj["spec"]["replicated"].update(size=1) if obj["kind"] == "CephBlockPool" else None
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)

    def test_replaced_original_object_never_recreates_storage(self):
        self.run_create(); identity = next(key for key in self.client.data if key[1].startswith("cephblockpool.ceph.rook.io/"))
        self.client.data[identity]["metadata"]["uid"] = uid(999)
        with self.assertRaises(ValueError): self.run_create("resume")
        self.assertEqual(len(self.client.attempts), 4)

    def test_pending_backend_data_cannot_be_adopted_on_resume(self):
        self.client.lost_ack = True
        with self.assertRaises(ValueError): self.run_create()
        self.client.views[("osd", "dump")]["pools"].append({"pool_name": self.plan["names"]["filesystem"] + "-metadata"})
        with self.assertRaises(ValueError): self.run_create("resume")
        self.assertEqual(len(self.client.attempts), 1)

    def test_wrong_runtime_replication_rule_or_standby_cannot_pass(self):
        self.run_create()
        self.client.views[("osd", "crush", "dump")]["rules"][0]["steps"][0]["item_name"] = "foreign"
        with self.assertRaises(ValueError): self.run_create("resume")
        self.client.views[("osd", "crush", "dump")]["rules"][0]["steps"][0]["item_name"] = "default"
        self.client.views[("fs", "dump")]["filesystems"][0]["mdsmap"]["info"]["2"]["state"] = "up:active"
        with self.assertRaises(ValueError): self.run_create("resume")

    def test_failed_proof_publication_preserves_handles_for_observation(self):
        original = provision.atomic_write_text
        def publish(path, value):
            if path.name == "ceph-storage-proof.json" and json.loads(value).get("phase") == "completed": raise OSError("fixture publication failure")
            return original(path, value)
        with mock.patch.object(provision, "atomic_write_text", side_effect=publish), self.assertRaises(OSError): self.run_create()
        proof = json.loads((self.output / "ceph-storage-proof.json").read_text()); self.assertEqual(proof["phase"], "failed"); self.assertFalse(proof["storage_resources_provisioned"])
        self.assertTrue(self.run_create("resume")["storage_resources_provisioned"]); self.assertEqual(len(self.client.attempts), 4)

    def test_ceph_observer_never_accepts_mutating_commands(self):
        with self.assertRaises(ValueError): provision.ceph(self.client, self.plan, ["osd", "pool", "delete"])
        self.assertFalse(self.client.calls)

    def test_csi_namespace_and_driver_prefix_can_differ_from_provider_namespace(self):
        for kind, config in self.plan["csi"].items():
            config.update(namespace="rook-system", namespace_uid=uid(990), driver="rook-system." + kind + ".csi.ceph.com")
        self.client = FakeClient(self.plan)
        self.plan["admitted_sha256"] = policy.fingerprint({provision.key(o): policy.fingerprint(provision.material(o)) for o in policy.objects(self.plan, "rke2")})
        objects = policy.objects(self.plan, "rke2")
        self.assertEqual(objects[2]["provisioner"], "rook-system.rbd.csi.ceph.com")
        self.assertEqual(objects[2]["parameters"]["clusterID"], "rook-ceph")
        self.assertTrue(self.run_create()["storage_resources_provisioned"])

    def use_split_topology(self):
        self.plan = split_topology_fixture(); self.client = FakeClient(self.plan)
        self.plan["admitted_sha256"] = policy.fingerprint({provision.key(o): policy.fingerprint(provision.material(o)) for o in policy.objects(self.plan, "rke2")})

    def test_separate_operator_namespace_and_access_workers_do_not_add_fake_osd_capacity(self):
        self.use_split_topology()
        self.assertTrue(self.run_create()["storage_resources_provisioned"])
        self.assertEqual(provision.observe(self.client, self.plan)["capacity"]["raw_bytes"], 4 * 200 * 1024 ** 3)
        self.assertTrue(self.run_create("resume")["storage_resources_provisioned"])
        self.assertEqual(len(self.client.attempts), 4)
        self.assertEqual(len(self.plan["access_nodes"]), 6); self.assertEqual(len(self.plan["nodes"]), 4)

    def test_replaced_operator_namespace_or_configuration_refuses_before_creates(self):
        for index, target in enumerate(("namespace", "configmap")):
            self.use_split_topology(); self.output = self.root / "private" / str(index)
            op = self.plan["provider"]["operator"]
            key = (None, "namespace/" + op["namespace"]) if target == "namespace" else (op["namespace"], "configmap/rook-ceph-operator-config")
            self.client.data[key]["metadata"]["uid"] = uid(9999)
            with self.assertRaises(ValueError): self.run_create()
            self.assertFalse(self.client.attempts)

    def test_namespace_watch_cannot_be_disabled_even_with_a_new_config_digest(self):
        self.use_split_topology(); op = self.plan["provider"]["operator"]
        config = self.client.data[(op["namespace"], "configmap/rook-ceph-operator-config")]
        config["data"]["ROOK_CURRENT_NAMESPACE_ONLY"] = "true"
        op["configmaps"]["rook-ceph-operator-config"]["data_sha256"] = policy.fingerprint(config["data"])
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)

    def test_missing_access_plugin_and_unreviewed_controller_node_refuse(self):
        self.use_split_topology(); config = self.plan["csi"]["rbd"]; ns = config["namespace"]
        self.client.workload("daemonset", config["node_plugin"]["name"], config["node_plugin"]["uid"], image("c"), [n["name"] for n in self.plan["nodes"]], namespace=ns)
        # Remove old rollout pods so only a genuine missing-worker service remains.
        for key in list(self.client.data):
            if key[0] == ns and key[1].startswith("pod/" + config["node_plugin"]["name"] + "-pod-") and int(key[1].rsplit("-", 1)[1]) >= 4: del self.client.data[key]
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)
        self.use_split_topology(); config = self.plan["csi"]["rbd"]
        self.client.workload("deployment", config["controller"]["name"], config["controller"]["uid"], image("b"), ["client-1", "client-2", "unreviewed"], namespace=config["namespace"])
        self.output = self.root / "private/unreviewed"
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)

    def test_added_access_workers_do_not_hide_a_missing_crush_host(self):
        self.use_split_topology(); self.client.views[("osd", "crush", "dump")]["buckets"].pop()
        with self.assertRaises(ValueError): self.run_create()
        self.assertFalse(self.client.attempts)


if __name__ == "__main__": unittest.main()
