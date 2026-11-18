#!/usr/bin/env python3
"""Behavior checks for optional fresh CNPG bootstrap; no database or HA qualification."""

import base64
from contextlib import nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from bounded_file import read_bounded_bytes
import cnpg_release
import deploy_cnpg_operator as deploy
import render_cnpg_operator as renderer
from test_postgres_installation import PUBLIC_CA, conditions, owner, uid


OFFICIAL = copy.deepcopy(cnpg_release.RELEASES)


def fixture():
    return {"schema_version": 1, "operation_id": uid(700), "version": "1.30.1",
        "target": {"kubeconfig_env": "CNPG_BOOTSTRAP_KUBECONFIG", "context": "operator", "cluster_uid": uid(701)},
        "namespace_uid": uid(702), "nodes": [{"name": "worker-" + str(i), "uid": uid(710 + i), "domain": "zone-" + str(i)} for i in (1, 2)],
        "domain_label": "topology.kubernetes.io/zone", "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "admitted_sha256": None}


def release_fixture():
    objects = [{"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition", "metadata": {"name": name},
        "spec": {"group": "postgresql.cnpg.io", "versions": [{"name": "v1", "served": True, "storage": True,
            "schema": {"openAPIV3Schema": {"type": "object", "properties": {"spec": {"type": "object"}}}}}]}}
        for name in ["clusters.postgresql.cnpg.io", *["fixture-" + str(i) + ".postgresql.cnpg.io" for i in range(10)]]]
    objects += [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": renderer.NAMESPACE}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "cnpg-manager", "namespace": renderer.NAMESPACE}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "cnpg-manager"}, "rules": []},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "fixture-editor"}, "rules": []},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": "cnpg-manager-rolebinding"},
         "roleRef": {"kind": "ClusterRole", "name": "cnpg-manager", "apiGroup": "rbac.authorization.k8s.io"},
         "subjects": [{"kind": "ServiceAccount", "name": "cnpg-manager", "namespace": renderer.NAMESPACE}]},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cnpg-default-monitoring", "namespace": renderer.NAMESPACE}, "data": {"queries": "fixture"}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "cnpg-webhook-service", "namespace": renderer.NAMESPACE},
         "spec": {"ports": [{"port": 443, "targetPort": 9443}], "selector": {"app.kubernetes.io/name": "cloudnative-pg"}}}]
    container = {"name": "manager", "image": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1", "command": ["/manager"],
        "args": ["controller", "--leader-elect"], "env": [{"name": "OPERATOR_IMAGE_NAME", "value": "fixture"}],
        "securityContext": {"allowPrivilegeEscalation": False, "runAsUser": 10001, "capabilities": {"drop": ["ALL"]}}}
    objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": renderer.DEPLOYMENT, "namespace": renderer.NAMESPACE},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "cloudnative-pg"}},
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": "cloudnative-pg"}},
                         "spec": {"containers": [container], "serviceAccountName": "cnpg-manager"}}}})
    for kind, name in (("MutatingWebhookConfiguration", "cnpg-mutating-webhook-configuration"), ("ValidatingWebhookConfiguration", "cnpg-validating-webhook-configuration")):
        objects.append({"apiVersion": "admissionregistration.k8s.io/v1", "kind": kind, "metadata": {"name": name},
            "webhooks": [{"name": "fixture.cnpg.io", "admissionReviewVersions": ["v1"], "sideEffects": "None", "failurePolicy": "Fail",
                "clientConfig": {"service": {"name": "cnpg-webhook-service", "namespace": renderer.NAMESPACE, "path": "/fixture"}}}]})
    return yaml.safe_dump_all(objects).encode()


class FakeClient:
    def __init__(self, plan):
        self.plan, self.objects, self.calls, self.created = plan, {}, [], []
        self.cluster_uid = plan["target"]["cluster_uid"]
        self.mutate_admission = None
        self.lost_ack_at = self.reject_at = None
        self.certificates = set()
        self.live = True
        self.pods, self.sets = [], []

    def frozen(self):
        return nullcontext(self)

    def get(self, resource, namespace=None, optional=False):
        self.calls.append(("get", resource, namespace))
        if resource == "namespace/kube-system":
            return {"metadata": {"uid": self.cluster_uid}}
        if resource == "namespace/" + renderer.NAMESPACE:
            return {"metadata": {"uid": self.plan["namespace_uid"], "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
        for node in self.plan["nodes"]:
            if resource == "node/" + node["name"]:
                return {"metadata": {"uid": node["uid"], "labels": {"kubernetes.io/hostname": node["name"], self.plan["domain_label"]: node["domain"]}},
                        "status": {"conditions": conditions("Ready"), "nodeInfo": {"operatingSystem": "linux"}}}
        kind, name = resource.split("/", 1)
        return copy.deepcopy(self.objects.get((kind.split(".")[0], namespace, name)))

    def call(self, arguments, payload=None, seconds=30):
        self.calls.append(tuple(arguments))
        if arguments[:2] == ["config", "view"]:
            return json.dumps({"current-context": "operator", "contexts": [{"name": "operator", "context": {"cluster": "one"}}],
                "clusters": [{"name": "one", "cluster": {"server": "https://api.example.com:6443"}}]})
        if arguments[0] == "version":
            return json.dumps({"serverVersion": {"major": "1", "minor": "35"}})
        if arguments[0] == "get":
            if arguments[1].startswith("secret/"):
                self.assert_metadata_only(arguments)
                return arguments[1] if arguments[1].split("/", 1)[1] in self.certificates else ""
            if arguments[1] == "replicasets":
                return json.dumps({"items": self.sets})
            if arguments[1] == "pods":
                return json.dumps({"items": self.pods})
            raise AssertionError("Unexpected collection read")
        if arguments[0] != "create":
            raise AssertionError("Only create/dry-run and read operations are permitted")
        value = json.loads(payload)
        if "--dry-run=server" in arguments:
            if self.mutate_admission:
                self.mutate_admission(value)
            return json.dumps(value)
        if len(self.created) + 1 == self.reject_at:
            raise TimeoutError("Unknown creation result; no object is visible")
        identity = (value["kind"].lower(), value["metadata"].get("namespace"), value["metadata"]["name"])
        if identity in self.objects:
            raise AssertionError("An original object was created again")
        value["metadata"].update(uid=uid(800 + len(self.created)), generation=1)
        self.created.append(identity)
        self.objects[identity] = value
        if value["kind"] == "CustomResourceDefinition":
            value["status"] = {"conditions": conditions("Established", "NamesAccepted")}
        if value["kind"] == "Deployment":
            self.make_ready()
        if len(self.created) == self.lost_ack_at:
            self.lost_ack_at = None
            raise TimeoutError("Acknowledgement lost after committed creation")
        return json.dumps(value)

    @staticmethod
    def assert_metadata_only(arguments):
        if arguments[-2:] != ["-o", "name"]:
            raise AssertionError("Private certificate data must not be fetched")

    def make_ready(self):
        deployment = self.objects[("deployment", renderer.NAMESPACE, renderer.DEPLOYMENT)]
        deployment["metadata"].setdefault("annotations", {})["deployment.kubernetes.io/revision"] = "1"
        deployment["status"] = {"observedGeneration": 1, **{name: 2 if self.live else 0 for name in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")}}
        self.sets = [{"metadata": {"uid": uid(950), "ownerReferences": owner("Deployment", deployment["metadata"]["uid"]),
                                  "annotations": {"deployment.kubernetes.io/revision": "1"}}}]
        self.pods = []
        for index, node in enumerate(self.plan["nodes"][:2]):
            spec = copy.deepcopy(deployment["spec"]["template"]["spec"])
            spec["nodeName"] = node["name"]
            self.pods.append({"metadata": {"name": "cnpg-fixture-" + str(index), "uid": uid(960 + index), "ownerReferences": owner("ReplicaSet", uid(950))},
                "spec": spec, "status": {"phase": "Running", "conditions": conditions("Ready"),
                    "containerStatuses": [{"name": "manager", "ready": True, "state": {"running": {}}}]}})
        for value in self.objects.values():
            if value["kind"].endswith("WebhookConfiguration"):
                for webhook in value["webhooks"]:
                    webhook["clientConfig"]["caBundle"] = base64.b64encode(PUBLIC_CA).decode()
        self.certificates = {"cnpg-ca-secret", "cnpg-webhook-cert"}


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.release = fixture(), release_fixture()
        release = {**OFFICIAL["1.30.1"], "manifest_sha256": hashlib.sha256(self.release).hexdigest()}
        patch = mock.patch.dict(cnpg_release.RELEASES, {"1.30.1": release})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(deploy, "sources", return_value="a" * 64)
        patch.start()
        self.addCleanup(patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "private").mkdir(mode=0o700)
        patch = mock.patch.object(deploy, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.client = FakeClient(self.plan)
        self.output = self.root / "private/create"

    def operate(self, mode="create", runtime="rke2", output=None):
        return deploy.operate(self.plan, runtime, self.release, mode=mode, output=output or self.output, client=self.client, wait_seconds=5)

    def review(self):
        result = self.operate("check-server", output=self.root / "private/check")
        self.plan["admitted_sha256"] = result["admitted_sha256"]
        self.assertFalse(self.client.created)

    def test_local_mode_constructs_no_client_and_writes_no_directory(self):
        with mock.patch.object(deploy, "Client", side_effect=AssertionError("Unexpected network client")):
            result = deploy.operate(self.plan, "rke2", self.release)
        self.assertTrue(result["local_inputs_verified"])
        self.assertFalse(result["operator_resources_ready"])
        self.assertFalse(self.output.exists())

    def test_three_kubernetes_runtimes_create_once_and_resume_originals(self):
        self.review()
        for runtime in renderer.RUNTIMES:
            self.client = FakeClient(self.plan)
            self.output = self.root / ("private/create-" + runtime)
            result = self.operate(runtime=runtime)
            self.assertTrue(result["operator_resources_ready"])
            self.assertTrue(result["webhook_admission_checked"])
            self.assertEqual(len(self.client.created), 20)
            for name in renderer.UNVERIFIED:
                self.assertIs(result[name], False)
            self.assertTrue(self.operate("resume", runtime)["operator_resources_ready"])
            self.assertEqual(len(self.client.created), 20)

    def test_create_requires_review_before_network_or_directory_creation(self):
        with self.assertRaises(ValueError):
            self.operate()
        self.assertFalse(self.client.calls)
        self.assertFalse(self.output.exists())

    def test_lost_acknowledgement_observes_original_resource_without_recreation(self):
        self.review()
        self.client.lost_ack_at = 2
        with self.assertRaises(TimeoutError):
            self.operate()
        self.assertTrue(self.operate("resume")["operator_resources_ready"])
        self.assertEqual(len(self.client.created), len(set(self.client.created)))
        self.assertEqual(len(self.client.created), 20)

    def test_missing_uncertain_object_is_not_created_again(self):
        self.review()
        self.client.reject_at = 2
        with self.assertRaises(TimeoutError):
            self.operate()
        self.client.reject_at = None
        with self.assertRaises(ValueError):
            self.operate("resume")
        self.assertEqual(len(self.client.created), 1)

    def test_existing_api_or_certificate_cannot_be_adopted(self):
        self.client.certificates.add("cnpg-webhook-cert")
        with self.assertRaises(ValueError):
            self.operate("check-server")
        self.assertFalse(self.client.created)

    def test_foreign_cluster_uid_is_rejected_before_dry_run(self):
        self.client.cluster_uid = uid(999)
        with self.assertRaises(ValueError):
            self.operate("check-server")
        self.assertFalse(any(call[0] == "create" for call in self.client.calls))

    def test_reviewed_hash_and_finite_webhook_policy_cannot_be_weakened(self):
        def change(value):
            if value["kind"].endswith("WebhookConfiguration"):
                value["webhooks"][0]["failurePolicy"] = "Ignore"
        self.client.mutate_admission = change
        with self.assertRaises(ValueError):
            self.operate("check-server")
        self.assertFalse(self.client.created)

    def test_admission_cannot_aggregate_operator_permissions_into_other_roles(self):
        def change(value):
            if value["kind"] == "ClusterRole":
                value["metadata"].setdefault("labels", {})["rbac.authorization.k8s.io/aggregate-to-view"] = "true"
        self.client.mutate_admission = change
        with self.assertRaises(ValueError):
            self.operate("check-server")
        self.assertFalse(self.client.created)

    def test_only_the_standard_webhook_rule_scope_default_is_accepted(self):
        expected = next(value for value in renderer.objects(self.plan, "rke2", self.release)["webhooks"]
                        if value["kind"] == "MutatingWebhookConfiguration")
        expected["webhooks"][0]["rules"] = [{"apiGroups": ["postgresql.cnpg.io"], "apiVersions": ["v1"], "operations": ["CREATE"], "resources": ["clusters"]}]
        current = copy.deepcopy(expected)
        current["webhooks"][0]["rules"][0]["scope"] = "*"
        self.assertEqual(deploy.admitted(expected, current), renderer.fingerprint(deploy.body(expected)))
        current["webhooks"][0]["rules"][0]["scope"] = "Namespaced"
        with self.assertRaises(ValueError):
            deploy.admitted(expected, current)

    def test_existing_operator_api_is_not_adopted(self):
        value = renderer.objects(self.plan, "rke2", self.release)["apis"][0]
        self.client.objects[(value["kind"].lower(), None, value["metadata"]["name"])] = value
        with self.assertRaises(ValueError):
            self.operate("check-server")
        self.assertFalse(self.client.created)

    def test_unreviewed_container_environment_is_refused(self):
        def change(value):
            if value["kind"] == "Deployment":
                value["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "WATCH_NAMESPACE", "value": "unreviewed"})
        self.client.mutate_admission = change
        with self.assertRaises(ValueError):
            self.operate("check-server")
        self.assertFalse(self.client.created)

    def test_changed_source_stops_before_the_first_write(self):
        self.review()
        with mock.patch.object(deploy, "sources", side_effect=["a" * 64, "b" * 64]):
            with self.assertRaises(ValueError):
                self.operate()
        self.assertFalse(self.client.created)

    def test_changed_original_uid_or_webhook_ca_refuses_resume(self):
        self.review()
        self.operate()
        first = self.client.objects[self.client.created[0]]
        original = first["metadata"]["uid"]
        first["metadata"]["uid"] = uid(999)
        with self.assertRaises(ValueError):
            self.operate("resume")
        first["metadata"]["uid"] = original
        for value in self.client.objects.values():
            if value["kind"].endswith("WebhookConfiguration"):
                for webhook in value["webhooks"]:
                    webhook["clientConfig"]["caBundle"] = base64.b64encode(PUBLIC_CA + b"\n").decode()
        with self.assertRaises(ValueError):
            self.operate("resume")
        self.assertEqual(len(self.client.created), 20)

    def test_not_ready_retains_original_intents_for_later_observation(self):
        self.review()
        self.client.live = False
        with mock.patch.object(deploy.time, "monotonic", side_effect=[0, 999]), mock.patch.object(deploy.time, "sleep"):
            with self.assertRaises(TimeoutError):
                self.operate()
        self.client.live = True
        self.client.make_ready()
        self.assertTrue(self.operate("resume")["operator_resources_ready"])
        self.assertEqual(len(self.client.created), 20)


class OfficialReleaseTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CNPG_TEST_VM_RUNTIME") == "1", "Disposable real worker fixture not selected")
    def test_real_worker_operator(self):
        from test_cnpg_operator_runtime_fixture import exercise
        self.assertTrue(exercise(self)["cnpg_operator_runtime_verified"])

    @unittest.skipUnless(os.environ.get("CNPG_TEST_RELEASE"), "Reviewed upstream operator release fixture not selected")
    def test_official_release_preserves_runtime_access_and_pins_two_candidates(self):
        payload = read_bounded_bytes(Path(os.environ["CNPG_TEST_RELEASE"]), max_bytes=2 * 1024 ** 2)
        plan = fixture()
        for runtime in renderer.RUNTIMES:
            phases = renderer.objects(plan, runtime, payload)
            self.assertEqual(len(sum(phases.values(), [])), 20)
            deployment = phases["workload"][0]
            self.assertEqual(deployment["spec"]["replicas"], 2)
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(container["image"], OFFICIAL["1.30.1"]["image"])
            self.assertEqual(next(value["value"] for value in container["env"] if value["name"] == "OPERATOR_IMAGE_NAME"), container["image"])
            self.assertEqual([value["metadata"]["name"] for value in phases["support"] if value["kind"] == "ClusterRole"], ["cnpg-manager"])
            self.assertTrue(all(webhook["failurePolicy"] == "Fail" for value in phases["webhooks"] for webhook in value["webhooks"]))


if __name__ == "__main__":
    unittest.main()
