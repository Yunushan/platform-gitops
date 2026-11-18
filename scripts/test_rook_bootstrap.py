#!/usr/bin/env python3
"""Exercise pinned Rook APIs, phased admission and original-handle recovery without a cluster."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_rook_bootstrap as deploy
import render_rook_bootstrap as renderer
from test_ceph_storage import FakeClient as Workloads, split_topology_fixture, uid, image, condition, owner


def fixture():
    return {"schema_version": 1, "namespace": "rook-system", "namespace_uid": uid(1),
        "target": {"kubeconfig_env": "STORAGE_KUBECONFIG", "context": "storage", "cluster_uid": uid(2)},
        "nodes": [{"name": "storage-" + str(i), "uid": uid(10 + i), "failure_domain": "rack-" + str(i)} for i in range(1, 5)],
        "controller_nodes": ["storage-1", "storage-2"], "domain_label": "topology.kubernetes.io/zone",
        "images": {name: image(format(i, "x")) for i, name in enumerate(sorted(renderer.IMAGES), 1)},
        "kubelet_dir": "/var/lib/kubelet", "watch_current_namespace_only": True,
        "resources": {name: {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}} for name in renderer.BUDGETS},
        "admitted_core_sha256": None}


class FakeClient(Workloads):
    def __init__(self, plan):
        self.selected = plan; self.plan = {"provider": {"namespace": plan["namespace"]}}
        self.data = {}; self.counter = 100; self.calls = []; self.attempts = []; self.created = []
        self.lost_ack = None; self.reject = None; self.inject = None; self.delay = False
        self.data[(None, "namespace/kube-system")] = {"metadata": {"uid": plan["target"]["cluster_uid"]}}
        self.data[(None, "namespace/" + plan["namespace"])] = {"metadata": {"uid": plan["namespace_uid"], "labels": {"pod-security.kubernetes.io/enforce": "privileged"}}}
        for node in plan["nodes"]:
            self.data[(None, "node/" + node["name"])] = {"metadata": {"uid": node["uid"], "labels": {"kubernetes.io/hostname": node["name"], plan["domain_label"]: node["failure_domain"]}},
                "status": {"conditions": condition("Ready"), "nodeInfo": {"operatingSystem": "linux"}}}

    def get(self, resource, namespace=None, *, optional=False):
        if "/" in resource:
            kind, name = resource.split("/", 1); resource = kind.split(".", 1)[0] + "/" + name
        return super().get(resource, namespace, optional=optional)

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append(tuple(args))
        if args[0] == "version": return json.dumps({"serverVersion": {"major": "1", "minor": "35"}})
        if args[0] == "config": return json.dumps({"current-context": "storage", "contexts": [{"name": "storage", "context": {"cluster": "storage"}}], "clusters": [{"name": "storage", "cluster": {"server": "https://api.example.com:6443"}}]})
        if args[0] != "create": raise AssertionError("Unexpected bootstrap action")
        obj = json.loads(payload); kind = obj["kind"]; meta = obj["metadata"]; namespace = meta.get("namespace"); name = meta["name"]
        if self.inject: self.inject(obj)
        if "--dry-run=server" in args:
            if obj["apiVersion"].startswith("csi.ceph.io/"):
                self.assert_api(kind)
            return json.dumps(obj)
        self.attempts.append(deploy.shared.key(obj))
        if self.reject == kind: raise ValueError("Fixture create rejected")
        meta.update(uid=self.next_uid(), generation=1)
        if kind == "CustomResourceDefinition": obj["status"] = {"conditions": condition("Established") + condition("NamesAccepted")}
        if kind == "Deployment":
            self.workload("deployment", name, meta["uid"], obj["spec"]["template"]["spec"]["containers"][0]["image"], self.selected["controller_nodes"][:obj["spec"]["replicas"]])
            generated = self.data[(namespace, "deployment/" + name)]
            meta["annotations"]["deployment.kubernetes.io/revision"] = "1"; obj["status"] = generated["status"]
            if self.delay: obj["status"]["readyReplicas"] = 0
        resource = kind.lower() + "/" + name
        self.data[(namespace, resource)] = copy.deepcopy(obj); self.created.append(deploy.shared.key(obj))
        if kind == "Driver":
            for workload, suffix, selected in (("deployment", "ctrlplugin", self.selected["controller_nodes"]), ("daemonset", "nodeplugin", [n["name"] for n in self.selected["nodes"]])):
                self.workload(workload, name + "-" + suffix, self.next_uid(), self.selected["images"]["plugin"], selected, controller=("Driver", meta["uid"]))
            self.data[(None, "csidriver/" + name)] = {"metadata": {"uid": self.next_uid(), "annotations": {"csi.ceph.io/ownerref": json.dumps({"Namespace": namespace, "Name": name})}},
                "spec": {"attachRequired": obj["spec"]["attachRequired"], "volumeLifecycleModes": ["Persistent"], "fsGroupPolicy": "File", "podInfoOnMount": True, "seLinuxMount": True}}
        if self.lost_ack == kind:
            self.lost_ack = None; raise ValueError("Fixture acknowledgement lost")
        return json.dumps(obj)

    def assert_api(self, kind):
        name = "drivers" if kind == "Driver" else "operatorconfigs"
        if self.get("customresourcedefinition/" + name + ".csi.ceph.io") is None: raise AssertionError("Custom-resource admission attempted before APIs exist")


class RenderTests(unittest.TestCase):
    def test_optional_real_kubernetes_admission(self):
        if os.environ.get("ROOK_BOOTSTRAP_TEST_API") != "1": self.skipTest("Opt in to an isolated local Kubernetes API fixture")
        from test_rook_api_fixture import exercise
        exercise(self)

    def test_operator_defaults_and_resource_quantities_are_concrete_before_admission(self):
        plan = fixture(); plan["resources"]["rook_operator"] = {"requests": {"cpu": "1000m", "memory": "1024Mi"}, "limits": {"cpu": "2000m", "memory": "2048Mi"}}
        obj = next(o for o in renderer.objects(plan, "rke2")["core"] if o["kind"] == "Deployment" and o["metadata"]["name"] == "rook-ceph-operator")
        pod = obj["spec"]["template"]["spec"]; container = pod["containers"][0]
        self.assertEqual(container["resources"]["requests"], {"cpu": "1", "memory": "1Gi"})
        self.assertEqual(pod["restartPolicy"], "Always")
        self.assertTrue(all(e["valueFrom"]["fieldRef"]["apiVersion"] == "v1" for e in container["env"] if "valueFrom" in e))
        actual = copy.deepcopy(obj); actual["spec"]["template"]["spec"]["serviceAccount"] = pod["serviceAccountName"]
        deploy.admitted(obj, actual)
        actual["spec"]["template"]["spec"]["serviceAccount"] = "foreign"
        with self.assertRaises(ValueError): deploy.admitted(obj, actual)

    def test_csi_watch_scope_uses_the_server_canonical_environment_form(self):
        for restricted in (True, False):
            plan = fixture(); plan["watch_current_namespace_only"] = restricted
            obj = next(o for o in renderer.objects(plan, "rke2")["core"] if o["kind"] == "Deployment" and o["metadata"]["name"] == "ceph-csi-controller-manager")
            env = obj["spec"]["template"]["spec"]["containers"][0]["env"]
            variable = next(e for e in env if e["name"] == "WATCH_NAMESPACE")
            self.assertEqual(variable, {"name": "WATCH_NAMESPACE", "value": plan["namespace"]} if restricted else {"name": "WATCH_NAMESPACE"})
            actual = copy.deepcopy(obj)
            for e in actual["spec"]["template"]["spec"]["containers"][0]["env"]:
                if e.get("value") == "": e.pop("value")
            deploy.admitted(obj, actual)

    def test_five_runtimes_pin_every_enabled_image_and_runtime_access(self):
        plan = fixture()
        for runtime in renderer.storage.RUNTIMES:
            phases = renderer.objects(plan, runtime); files = renderer.render(plan, runtime)
            flags = json.loads(files["render-status.json"])
            self.assertTrue(all(flags[k] is False for k in renderer.UNVERIFIED))
            roles = [o for o in phases["core"] if o["kind"] in {"Role", "ClusterRole"}]
            self.assertFalse(any("aggregationRule" in o or any(k.startswith("rbac.authorization.k8s.io/aggregate-to-") for k in o["metadata"].get("labels", {})) for o in roles))
            deployments = {o["metadata"]["name"]: o for o in phases["core"] if o["kind"] == "Deployment"}
            self.assertEqual(deployments["rook-ceph-operator"]["spec"]["replicas"], 1)
            self.assertEqual(deployments["ceph-csi-controller-manager"]["spec"]["replicas"], 2)
            images = next(o for o in phases["core"] if o["metadata"]["name"] == "rook-csi-operator-image-set-configmap")
            self.assertTrue(images["immutable"]); self.assertEqual(set(images["data"].values()), {v for k, v in plan["images"].items() if k not in {"rook", "csi_operator"}})
            self.assertEqual(len([o for o in phases["drivers"] if o["kind"] == "Driver"]), 2)

    def test_incomplete_images_budgets_or_redundancy_are_rejected(self):
        for change in (lambda p: p["images"].pop("snapshot_metadata"), lambda p: p["images"].update(plugin="ceph:latest"),
                lambda p: p.update(controller_nodes=["storage-1"]), lambda p: p["resources"]["rook_operator"]["limits"].update(memory="32Mi"),
                lambda p: p["nodes"][0].update(uid=p["nodes"][1]["uid"]), lambda p: p.update(password="dummy")):
            plan = fixture(); change(plan)
            with self.assertRaises(ValueError): renderer.objects(plan, "rke2")

    def test_optional_actual_driver_schemas_and_unknown_field_pruning(self):
        if os.environ.get("CEPH_STORAGE_TEST_SCHEMA") != "1": self.skipTest("Opt in to pinned upstream CRD schemas")
        import jsonschema
        schemas = renderer.schema_map()
        def fields(value, schema):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"apiVersion", "kind", "metadata"} and "properties" not in schema: continue
                    if key not in schema.get("properties", {}):
                        self.assertTrue(schema.get("x-kubernetes-preserve-unknown-fields") or isinstance(schema.get("additionalProperties"), dict), key)
                    fields(item, schema.get("properties", {}).get(key, schema.get("additionalProperties", {})))
            elif isinstance(value, list):
                for item in value: fields(item, schema.get("items", {}))
        for obj in renderer.objects(fixture(), "rke2")["drivers"]:
            schema = schemas[obj["apiVersion"] + "/" + obj["kind"]]
            jsonschema.Draft4Validator(schema).validate(obj); fields(obj["spec"], schema["properties"]["spec"])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture(); self.client = FakeClient(self.plan)
        phases = renderer.objects(self.plan, "rke2")
        self.plan["admitted_core_sha256"] = renderer.storage.fingerprint({deploy.shared.key(o): renderer.storage.fingerprint(deploy.shared.material(o)) for o in phases["apis"] + phases["core"]})
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / "private").mkdir()
        patch = mock.patch.object(deploy, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop)
        self.output = self.root / "private/bootstrap"

    def run_bootstrap(self, mode="create"):
        return deploy.operate(self.plan, "rke2", mode=mode, output=self.output, client=self.client)

    def test_check_server_is_read_only_and_defers_custom_api_admission(self):
        result = self.run_bootstrap("check-server")
        self.assertTrue(result["server_checked"]); self.assertTrue(result["custom_api_dry_run_pending"])
        self.assertEqual(result["admitted_core_sha256"], self.plan["admitted_core_sha256"]); self.assertFalse(self.client.attempts)

    def test_create_then_resume_observes_original_resources_and_provider_handoff(self):
        result = self.run_bootstrap(); self.assertTrue(result["bootstrap_resources_ready"])
        self.assertTrue(all(result[k] is False for k in renderer.UNVERIFIED))
        count = len(self.client.attempts); self.assertTrue(self.run_bootstrap("resume")["bootstrap_resources_ready"])
        self.assertEqual(len(self.client.attempts), count)
        proof = json.loads((self.output / "rook-bootstrap-proof.json").read_text())
        self.assertEqual(len(proof["generated"]), 6); self.assertEqual(len(proof["provider_operator"]["configmaps"]), 2)
        self.assertEqual(proof["storage_access_nodes"], self.plan["nodes"])
        self.assertEqual(set(proof["storage_csi"]), {"rbd", "cephfs"})
        for kind, config in proof["storage_csi"].items():
            self.assertEqual(config["namespace_uid"], self.plan["namespace_uid"])
            self.assertEqual(config["driver"], self.plan["namespace"] + "." + kind + ".csi.ceph.com")
            self.assertEqual(config["controller"]["images"], [self.plan["images"]["plugin"]])

    def test_lost_ack_api_or_controller_or_driver_is_observed_once(self):
        for kind in ("CustomResourceDefinition", "Deployment", "Driver"):
            with self.subTest(kind=kind):
                self.output = self.root / "private" / kind; self.client = FakeClient(self.plan); self.client.lost_ack = kind
                with self.assertRaises(ValueError): self.run_bootstrap()
                self.assertTrue(self.run_bootstrap("resume")["bootstrap_resources_ready"])
                self.assertEqual(len(self.client.attempts), len(set(self.client.attempts)))

    def test_original_bootstrap_handoff_is_consumed_by_split_storage_prerequisites(self):
        storage_plan = split_topology_fixture()
        self.plan["nodes"] = copy.deepcopy(storage_plan["access_nodes"])
        self.plan["controller_nodes"] = ["client-1", "client-2"]
        self.plan["watch_current_namespace_only"] = False
        self.client = FakeClient(self.plan)
        phases = renderer.objects(self.plan, "rke2")
        self.plan["admitted_core_sha256"] = renderer.storage.fingerprint({deploy.shared.key(o): renderer.storage.fingerprint(deploy.shared.material(o)) for o in phases["apis"] + phases["core"]})
        self.assertTrue(self.run_bootstrap()["bootstrap_resources_ready"])
        proof = json.loads((self.output / "rook-bootstrap-proof.json").read_text())
        storage_plan["target"] = copy.deepcopy(self.plan["target"])
        storage_plan["access_nodes"] = copy.deepcopy(proof["storage_access_nodes"])
        storage_plan["provider"]["operator"] = copy.deepcopy(proof["provider_operator"])
        storage_plan["csi"] = copy.deepcopy(proof["storage_csi"])
        renderer.storage.validate(storage_plan, "rke2")
        # Only the Ceph provider/OSDs are simulated here. Preserve the bootstrap's
        # original API, configurations and generated CSI workloads verbatim.
        client = Workloads(storage_plan)
        for key, value in self.client.data.items():
            if key[0] == self.plan["namespace"] or key[1].startswith("csidriver/") or key == (None, "customresourcedefinition/cephclusters.ceph.rook.io"):
                client.data[key] = copy.deepcopy(value)
        storage_plan["provider"]["operator"] = copy.deepcopy(proof["provider_operator"])
        self.assertEqual(len(deploy.shared.prerequisites(client, storage_plan)), 4)
        self.assertEqual(deploy.shared.observe(client, storage_plan)["capacity"]["raw_bytes"], 4 * 200 * 1024 ** 3)
        self.assertFalse(client.attempts)
        driver = storage_plan["csi"]["rbd"]["driver"]
        client.data[(None, "csidriver/" + driver)]["metadata"]["uid"] = uid(9999)
        with self.assertRaises(ValueError): deploy.shared.prerequisites(client, storage_plan)
        self.assertFalse(client.attempts)

    def test_uncertain_missing_create_is_never_repeated(self):
        self.client.reject = "CustomResourceDefinition"
        with self.assertRaises(ValueError): self.run_bootstrap()
        self.client.reject = None
        with self.assertRaises(ValueError): self.run_bootstrap("resume")
        self.assertEqual(len(self.client.attempts), 1)

    def test_foreign_api_cannot_be_adopted(self):
        obj = renderer.objects(self.plan, "rke2")["apis"][0]
        self.client.data[(None, "customresourcedefinition/" + obj["metadata"]["name"])] = obj
        with self.assertRaises(ValueError): self.run_bootstrap()
        self.assertFalse(self.client.attempts)

    def test_preexisting_csi_registration_cannot_be_overwritten(self):
        name = self.plan["namespace"] + ".rbd.csi.ceph.com"
        self.client.data[(None, "csidriver/" + name)] = {"metadata": {"uid": uid(999)}}
        with self.assertRaises(ValueError): self.run_bootstrap()
        self.assertFalse(self.client.attempts)

    def test_source_change_stops_before_the_first_create(self):
        initial = deploy.toolset()
        with mock.patch.object(deploy, "toolset", side_effect=[initial, "0" * 64]):
            with self.assertRaises(ValueError): self.run_bootstrap()
        self.assertFalse(self.client.attempts)

    def test_admission_cannot_broaden_rbac_or_operator_host_access(self):
        def rbac(obj):
            if obj["kind"] == "ClusterRoleBinding": obj["subjects"].append({"kind": "Group", "name": "system:authenticated", "apiGroup": "rbac.authorization.k8s.io"})
        def host(obj):
            if obj["kind"] == "Deployment": obj["spec"]["template"]["spec"]["hostPID"] = True
        def aggregation(obj):
            if obj["kind"] == "ClusterRole": obj["metadata"]["labels"]["rbac.authorization.k8s.io/aggregate-to-admin"] = "true"
        for i, inject in enumerate((rbac, host, aggregation)):
            self.output = self.root / "private" / str(i); self.client = FakeClient(self.plan); self.client.inject = inject
            with self.assertRaises(ValueError): self.run_bootstrap()
            self.assertFalse(self.client.attempts)

    def test_generated_driver_replacement_is_not_accepted_on_resume(self):
        self.run_bootstrap()
        key = next(k for k in self.client.data if k[1].startswith("csidriver/"))
        self.client.data[key]["metadata"]["uid"] = uid(9999)
        with self.assertRaises(ValueError): self.run_bootstrap("resume")

    def test_operator_readiness_can_progress_without_recreating(self):
        self.client.delay = True
        def advance(_):
            for (namespace, key), obj in self.client.data.items():
                if key.startswith("deployment/") and "readyReplicas" in obj.get("status", {}): obj["status"]["readyReplicas"] = obj["spec"]["replicas"]
        with mock.patch.object(deploy.time, "sleep", side_effect=advance):
            self.assertTrue(self.run_bootstrap()["bootstrap_resources_ready"])
        self.assertEqual(len(self.client.attempts), len(set(self.client.attempts)))


if __name__ == "__main__": unittest.main()
