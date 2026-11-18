#!/usr/bin/env python3
"""Exercise pinned Tekton HA candidates, admission guards and canary correlation."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from bounded_file import read_bounded_bytes
import deploy_tekton as deploy
import kubernetes_management as management
import render_tekton as renderer
from test_flux import FakeClient as TargetClient


def fixture():
    objects = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
               for name in (renderer.NAMESPACE, renderer.RESOLVERS_NAMESPACE)]
    for name in ("tasks", "taskruns", "pipelines", "pipelineruns"):
        objects.append({"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": name + ".tekton.dev"}, "spec": {"group": "tekton.dev"}})
    maps = [(renderer.NAMESPACE, name) for name in ("feature-flags", "config-defaults", "config-leader-election-controller",
             "config-leader-election-events", "config-leader-election-webhook", "pipelines-info")]
    maps += [(renderer.RESOLVERS_NAMESPACE, name) for name in ("resolvers-feature-flags", "config-leader-election-resolvers")]
    for namespace, name in maps:
        objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace},
                        "data": {"version": "v1.4.0"} if name == "pipelines-info" else {}})
    image = "registry.example.com/tekton/controller@sha256:" + "a" * 64
    for name in sorted(renderer.CONTROLLERS):
        namespace = renderer.RESOLVERS_NAMESPACE if name.endswith("remote-resolvers") else renderer.NAMESPACE
        objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": {"metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": "manager", "image": image, "args": ["-entrypoint-image", image]}]}}}})
    objects.append({"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": {"name": "tekton-pipelines-webhook", "namespace": renderer.NAMESPACE},
        "spec": {"minReplicas": 1, "maxReplicas": 5, "scaleTargetRef": {"name": "tekton-pipelines-webhook"}}})
    objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "webhook-certs", "namespace": renderer.NAMESPACE}})
    for kind, name in (("ValidatingWebhookConfiguration", "validation.webhook.pipeline.tekton.dev"),
                       ("ValidatingWebhookConfiguration", "config.webhook.pipeline.tekton.dev"),
                       ("MutatingWebhookConfiguration", "webhook.pipeline.tekton.dev")):
        objects.append({"apiVersion": "admissionregistration.k8s.io/v1", "kind": kind, "metadata": {"name": name}, "webhooks": [{
            "name": name, "failurePolicy": "Fail", "clientConfig": {"service": {"name": "tekton-pipelines-webhook", "namespace": renderer.NAMESPACE}}}]})
    payload = yaml.safe_dump_all(objects, sort_keys=False).encode()
    plan = {"schema_version": 1, "pipelines_version": "1.4.0", "bundle_sha256": hashlib.sha256(payload).hexdigest(),
        "management_target": {"kubeconfig_env": "PLATFORM_MANAGEMENT_KUBECONFIG", "context": "management", "cluster_uid": "11111111-1111-4111-8111-111111111111"},
        "replicas": 2, "leader_buckets": 2, "webhook_max_replicas": 3,
        "node_selector": {"platform.gitops/control-service": "true", "kubernetes.io/os": "linux"}, "failure_domain_key": "kubernetes.io/hostname",
        "image_overrides": {}, "api_fields": "stable", "resolvers": {name: False for name in renderer.RESOLVERS},
        "canary": {"name": "platform-canary", "namespace": "platform-ci-canary", "image": "registry.example.com/python@sha256:" + "b" * 64,
                   "node_selector": {"platform.gitops/ci-worker": "true", "kubernetes.io/os": "linux"}}}
    return plan, payload


def package(root):
    plan, payload = fixture()
    files = renderer.render(plan, payload, "rke2")
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8", newline="\n")
    return plan, json.loads(files["render-status.json"])["contract_sha256"]


class FakeClient(TargetClient):
    def __init__(self, plan):
        super().__init__(plan)
        self.restricted = True
        self.webhook_ready = True
        self.bad_uid = False
        self.credential_volume = False
        self.control_plane = False
        self.extra_child = False
        self.wrong_pipeline = False
        self.run_uid = "22222222-2222-4222-8222-222222222222"
        self.task_uid = "33333333-3333-4333-8333-333333333333"

    def call(self, args, *, payload=None, seconds=30):
        if args[0] == "create":
            self.calls.append((args, payload, seconds))
            return json.dumps({"metadata": {"name": "platform-canary-run", "uid": self.run_uid}})
        if args[0] == "get" and any("go-template=" in arg for arg in args):
            self.calls.append((args, payload, seconds))
            return "webhook-certs\n" + self.owner if self.owner is not None else ""
        return super().call(args, payload=payload, seconds=seconds)

    def get(self, resource, namespace=None, *, optional=False):
        if resource == "namespace/kube-system":
            return super().get(resource, namespace, optional=optional)
        self.calls.append((["get", resource, namespace], None, 30))
        if optional:
            return {"metadata": {"labels": {renderer.OWNER_LABEL: self.owner}}} if self.owner is not None else None
        if resource.startswith("namespace/"):
            return {"metadata": {"labels": {"pod-security.kubernetes.io/enforce": "restricted" if self.restricted else "baseline"}}}
        if "webhookconfiguration" in resource:
            return {"webhooks": [{"rules": [{}] if self.webhook_ready else [], "failurePolicy": "Fail", "clientConfig": {
                "caBundle": "nonempty-test-fixture", "service": {"namespace": renderer.NAMESPACE, "name": "tekton-pipelines-webhook"}}}]}
        if resource.startswith("pipelinerun."):
            return {"metadata": {"uid": self.run_uid},
                "spec": {"pipelineRef": {"name": "another-pipeline" if self.wrong_pipeline else self.plan["canary"]["name"]},
                         "taskRunTemplate": {"serviceAccountName": self.plan["canary"]["name"]}},
                "status": {"conditions": [{"type": "Succeeded", "status": "True"}],
                    "childReferences": [{"kind": "TaskRun", "pipelineTaskName": "probe", "name": "canary-task"}]
                    + ([{"kind": "TaskRun", "pipelineTaskName": "unexpected", "name": "unexpected-task"}] if self.extra_child else [])}}
        if resource.startswith("taskrun."):
            marker = {"schema_version": 1, "component": "platform-tekton-canary", "uid": "wrong" if self.bad_uid else self.task_uid, "unprivileged": True}
            return {"metadata": {"uid": self.task_uid, "ownerReferences": [{"kind": "PipelineRun", "uid": self.run_uid, "controller": True}]},
                "spec": {"taskRef": {"name": self.plan["canary"]["name"]}, "serviceAccountName": self.plan["canary"]["name"]},
                "status": {"conditions": [{"type": "Succeeded", "status": "True"}], "podName": "canary-pod", "results": [{"name": "marker", "value": json.dumps(marker)}]}}
        if resource.startswith("pod/"):
            return {"metadata": {"uid": "44444444-4444-4444-8444-444444444444", "ownerReferences": [{"kind": "TaskRun", "uid": self.task_uid, "controller": True}]},
                "status": {"phase": "Succeeded"}, "spec": {"nodeName": "worker-1", "serviceAccountName": self.plan["canary"]["name"],
                    "containers": [{"name": "step-probe", "image": self.plan["canary"]["image"]}],
                    "volumes": [{"projected": {"sources": [{"serviceAccountToken": {}}]}}] if self.credential_volume else []}}
        if resource.startswith("node/"):
            return {"metadata": {"labels": {**self.plan["canary"]["node_selector"], **({"node-role.kubernetes.io/control-plane": ""} if self.control_plane else {})}}}
        raise AssertionError("Unexpected canary resource query")


def declared_fields(data, schema):
    """Check field names against the release CRD, not full API validation."""
    if schema.get("x-kubernetes-preserve-unknown-fields"):
        return
    if isinstance(data, dict):
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                declared_fields(value, properties[key])
            elif isinstance(schema.get("additionalProperties"), dict):
                declared_fields(value, schema["additionalProperties"])
            elif properties and key not in {"apiVersion", "kind", "metadata"}:
                raise AssertionError("Generated field is not declared by the selected CRD: " + key)
    elif isinstance(data, list):
        for item in data:
            declared_fields(item, schema.get("items", {}))


class TektonTests(unittest.TestCase):
    def test_remote_resolvers_require_explicit_beta_feature_approval(self):
        plan, payload = fixture()
        plan["resolvers"]["http"] = True
        with self.assertRaisesRegex(ValueError, "beta"):
            renderer.render(plan, payload, "k8s")
        plan["api_fields"] = "beta"
        objects = list(yaml.safe_load_all(renderer.render(plan, payload, "k8s")["tekton-components.yaml"]))
        maps = {obj["metadata"]["name"]: obj.get("data", {}) for obj in objects if obj["kind"] == "ConfigMap"}
        self.assertEqual(maps["feature-flags"]["enable-api-fields"], "beta")
        self.assertEqual(maps["resolvers-feature-flags"]["enable-http-resolver"], "true")

    def test_election_placement_and_webhook_autoscaler_cannot_collapse_to_one(self):
        plan, payload = fixture()
        objects = list(yaml.safe_load_all(renderer.render(plan, payload, "rke2")["tekton-components.yaml"]))
        budgets = [obj for obj in objects if obj["kind"] == "PodDisruptionBudget"]
        self.assertEqual(len(budgets), 4)
        for deployment in (obj for obj in objects if obj["kind"] == "Deployment"):
            self.assertEqual(deployment["spec"]["replicas"], 2)
            pod = deployment["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeSelector"]["kubernetes.io/os"], "linux")
            self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
        hpa = next(obj for obj in objects if obj["kind"] == "HorizontalPodAutoscaler")
        self.assertEqual((hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]), (2, 3))
        maps = {obj["metadata"]["name"]: obj.get("data", {}) for obj in objects if obj["kind"] == "ConfigMap"}
        self.assertEqual(maps["config-leader-election-controller"]["buckets"], "2")
        self.assertEqual(maps["config-leader-election-webhook"]["buckets"], "1")
        self.assertEqual(maps["feature-flags"]["disable-creds-init"], "true")
        self.assertEqual(maps["feature-flags"]["set-security-context"], "true")
        self.assertTrue(all(value == "false" for value in maps["resolvers-feature-flags"].values()))

    def test_every_runtime_keeps_management_target_and_acceptance_flags_explicit(self):
        plan, payload = fixture()
        for runtime in renderer.RUNTIMES:
            files = renderer.render(plan, payload, runtime)
            contract = json.loads(files["deployment-contract.json"])
            self.assertEqual(contract["plan"]["management_target"], plan["management_target"])
            status = json.loads(files["render-status.json"])
            for field in ("deployed", "canary_completed", "event_integration_verified", "application_ci_verified", "ha_verified"):
                self.assertFalse(status[field])
            self.assertTrue(status["release_images"])

    def test_injected_helper_images_and_overrides_are_immutable_and_fully_accounted_for(self):
        plan, payload = fixture()
        plan["image_overrides"] = {"registry.example.com/tekton/controller@sha256:" + "a" * 64:
                                   "mirror.example.com/tekton/controller@sha256:" + "c" * 64}
        status = json.loads(renderer.render(plan, payload, "docker")["render-status.json"])
        self.assertEqual(status["resolved_images"], list(plan["image_overrides"].values()))
        plan["image_overrides"]["unused.example.com/image@sha256:" + "d" * 64] = plan["canary"]["image"]
        with self.assertRaises(ValueError):
            renderer.render(plan, payload, "docker")
        plan, payload = fixture()
        objects = list(yaml.safe_load_all(payload))
        controller = next(obj for obj in objects if obj["kind"] == "Deployment")
        controller["spec"]["template"]["spec"]["containers"][0]["args"][-1] = "registry.example.com/entrypoint:latest"
        changed = yaml.safe_dump_all(objects).encode()
        plan["bundle_sha256"] = hashlib.sha256(changed).hexdigest()
        with self.assertRaises(ValueError):
            renderer.render(plan, changed, "rke2")

    def test_release_version_secret_contents_and_fail_open_webhooks_are_rejected(self):
        for mutation in ("secret", "version", "fail-open", "changed-export"):
            plan, payload = fixture()
            objects = list(yaml.safe_load_all(payload))
            if mutation == "secret":
                next(obj for obj in objects if obj["kind"] == "Secret")["data"] = {"unexpected": "not-accepted"}
            if mutation == "version": plan["pipelines_version"] = "1.5.0"
            if mutation == "fail-open":
                next(obj for obj in objects if obj["kind"] == "ValidatingWebhookConfiguration")["webhooks"][0]["failurePolicy"] = "Ignore"
            changed = yaml.safe_dump_all(objects, sort_keys=False).encode()
            plan["bundle_sha256"] = hashlib.sha256(changed).hexdigest()
            if mutation == "changed-export": changed += b"\n"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                renderer.render(plan, changed, "raw")

    def test_local_mode_has_no_client_and_changed_bytes_cannot_be_deployed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, digest = package(root)
            with mock.patch.object(deploy, "Client") as client:
                self.assertTrue(deploy.operate(root, digest)["read_only"])
                client.assert_not_called()
            (root / "tekton-canary-run.yaml").write_bytes(b"changed")
            with mock.patch.object(deploy, "Client") as client, self.assertRaises(ValueError):
                deploy.operate(root, digest, apply=True)
            client.assert_not_called()

    def test_target_namespace_and_foreign_ownership_block_before_writes(self):
        for failure in ("target", "namespace", "ownership"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, digest = package(root)
                client = FakeClient(plan)
                if failure == "target": client.cluster_uid = "wrong"
                if failure == "namespace": client.restricted = False
                if failure == "ownership": client.owner = "another-manager"
                with mock.patch.object(deploy, "Client", return_value=client) as factory, self.assertRaises(ValueError):
                    deploy.operate(root, digest, apply=True)
                factory.assert_called_once()
                self.assertFalse(any(args[0] in {"apply", "create"} for args, _, _ in client.calls))

    def test_apply_waits_for_admission_and_correlates_real_canary_resource_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            with mock.patch.object(deploy, "Client", return_value=client):
                report = deploy.operate(root, digest, apply=True, run_canary=True)
            self.assertTrue(report["deployed"])
            self.assertTrue(report["canary_completed"])
            self.assertFalse(report["application_ci_verified"])
            self.assertFalse(report["ha_verified"])
            calls = [args for args, _, _ in client.calls]
            created = next(index for index, args in enumerate(calls) if args[0] == "create")
            self.assertLess(max(index for index, args in enumerate(calls) if args[0] == "rollout"), created)
            self.assertLess(max(index for index, args in enumerate(calls) if any(isinstance(arg, str) and "caBundle" in arg for arg in args)), created)
            self.assertFalse(any(args[0] == "delete" or "--force-conflicts" in args for args in calls))
            self.assertTrue(all("--server-side" in args for args in calls if args[0] == "apply"))

    def test_fresh_namespace_preview_is_read_only_and_apply_bootstraps_before_full_dry_run(self):
        class FreshClient(FakeClient):
            def get(self, resource, namespace=None, *, optional=False):
                if resource in {"namespace/tekton-pipelines", "namespace/tekton-pipelines-resolvers"}: return None
                return super().get(resource, namespace, optional=optional)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); plan, digest = package(root)
            client = FreshClient(plan)
            with mock.patch.object(deploy, "Client", return_value=client):
                result = deploy.operate(root, digest, check_server=True)
            self.assertFalse(result["server_checked"])
            self.assertTrue(result["namespace_bootstrap_checked"])
            self.assertEqual(result["namespace_bootstrap_required"], ["tekton-pipelines", "tekton-pipelines-resolvers"])
            operations = [(args, data) for args, data, _ in client.calls if args[0] in {"apply", "create"}]
            self.assertEqual(len(operations), 1)
            self.assertIn("--dry-run=server", operations[0][0])
            self.assertTrue(all(not obj["metadata"].get("namespace") for obj in yaml.safe_load_all(operations[0][1])))
            client.calls.clear()
            with mock.patch.object(deploy, "Client", return_value=client):
                self.assertTrue(deploy.operate(root, digest, apply=True)["deployed"])
            operations = [(args, data) for args, data, _ in client.calls if args[0] == "apply"]
            self.assertIn("--dry-run=server", operations[0][0])
            self.assertNotIn("--dry-run=server", operations[1][0])
            self.assertEqual({obj["kind"] for obj in yaml.safe_load_all(operations[1][1])}, {"Namespace"})
            self.assertIn("--dry-run=server", operations[2][0])
            self.assertTrue(any(obj["metadata"].get("namespace") for obj in yaml.safe_load_all(operations[2][1])))

    def test_green_pipeline_cannot_mask_wrong_uid_credentials_or_control_plane_execution(self):
        for failure in ("bad_uid", "credential_volume", "control_plane", "extra_child", "wrong_pipeline"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, digest = package(root)
                client = FakeClient(plan)
                setattr(client, failure, True)
                with mock.patch.object(deploy, "Client", return_value=client), self.assertRaises(ValueError):
                    deploy.operate(root, digest, apply=True, run_canary=True)
                self.assertTrue(any(args[0] == "create" for args, _, _ in client.calls))

    def test_worker_finalization_wait_binds_original_pod_and_task_and_still_checks_security(self):
        class FinalizingClient(FakeClient):
            def __init__(self, plan, mutation): super().__init__(plan); self.waited = False; self.mutation = mutation
            def call(self, args, **kwargs):
                if args[:2] == ["wait", "--for=jsonpath={.status.phase}=Succeeded"]: self.waited = True
                return super().call(args, **kwargs)
            def get(self, resource, namespace=None, *, optional=False):
                value = super().get(resource, namespace, optional=optional)
                if resource.startswith("pod/"):
                    value["status"]["phase"] = "Succeeded" if self.waited else "Running"
                    if self.waited and self.mutation == "pod": value["metadata"]["uid"] = "55555555-5555-4555-8555-555555555555"
                    if self.waited and self.mutation == "credentials": value["spec"]["volumes"] = [{"secret": {"secretName": "unexpected"}}]
                if resource.startswith("taskrun.") and self.waited and self.mutation == "task": value["metadata"]["uid"] = "66666666-6666-4666-8666-666666666666"
                return value
        plan, _ = fixture()
        for mutation in (None, "pod", "task", "credentials"):
            with self.subTest(mutation=mutation):
                client = FinalizingClient(plan, mutation)
                run = client.get("pipelinerun.tekton.dev/canary")
                if mutation is None: deploy.verify_canary(client, plan, run, wait_worker=True)
                else:
                    with self.assertRaises(ValueError): deploy.verify_canary(client, plan, run, wait_worker=True)
                self.assertTrue(client.waited)

    def test_pending_admission_prevents_any_canary_apply_or_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            client.webhook_ready = False
            with mock.patch.object(deploy, "Client", return_value=client), self.assertRaisesRegex(ValueError, "Admission"):
                deploy.operate(root, digest, apply=True, run_canary=True)
            self.assertFalse(any(args[0] == "create" or (data and b"kind: Task\n" in data) for args, data, _ in client.calls))

    def test_probe_code_runs_without_shell_and_binds_its_marker_to_taskrun_uid(self):
        plan, _ = fixture()
        objects, run = renderer.canary_objects(plan)
        account, task, _ = objects
        self.assertFalse(account["automountServiceAccountToken"])
        step = task["spec"]["steps"][0]
        self.assertEqual(step["command"][:4], ["python3", "-I", "-B", "-c"])
        self.assertTrue(step["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(run["spec"]["taskRunTemplate"]["serviceAccountName"], account["metadata"]["name"])
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "marker"
            uid = "33333333-3333-4333-8333-333333333333"
            with mock.patch.object(os, "geteuid", return_value=1000, create=True), mock.patch.object(sys, "argv", ["-c", uid, str(result)]):
                exec(compile(step["command"][4], "<tekton-probe>", "exec"), {})
            self.assertEqual(json.loads(result.read_text())["uid"], uid)
            with mock.patch.object(os, "geteuid", return_value=0, create=True), mock.patch.object(sys, "argv", ["-c", uid, str(result)]), self.assertRaises(SystemExit):
                exec(compile(step["command"][4], "<tekton-probe>", "exec"), {})

    def test_secret_ownership_captures_metadata_only(self):
        plan, _ = fixture()
        client = FakeClient(plan)
        client.owner = renderer.NAMESPACE
        item = {"apiVersion": "v1", "kind": "Secret", "name": "webhook-certs", "namespace": renderer.NAMESPACE}
        management.verify_owned(client, item, renderer.OWNER_LABEL, renderer.NAMESPACE)
        query = client.calls[-1][0]
        self.assertTrue(any(arg.startswith("go-template=") for arg in query))
        self.assertFalse(any(".data" in arg for arg in query))
        client.owner = "foreign"
        with self.assertRaises(ValueError):
            management.verify_owned(client, item, renderer.OWNER_LABEL, renderer.NAMESPACE)

    def test_cli_keeps_private_values_local_and_refuses_overwrite(self):
        plan, payload = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, source = root / "plan.json", root / "release.yaml"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            source.write_bytes(payload)
            args = ["--plan", str(plan_path), "--bundle", str(source), "--runtime", "raw", "--output-dir", str(root / "private" / "new")]
            with mock.patch.object(renderer, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(renderer.main(args), 0)
            self.assertNotIn("registry.example.com", stdout.getvalue())
            with mock.patch.object(renderer, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(renderer.main(args), 1)

    def test_optional_real_release_and_canary_field_names(self):
        source = os.environ.get("TEKTON_TEST_EXPORT", "")
        if not source:
            self.skipTest("Set TEKTON_TEST_EXPORT to a reviewed release for compatibility checks")
        payload = read_bounded_bytes(Path(source), max_bytes=16 * 1024 ** 2)
        plan, _ = fixture()
        plan["bundle_sha256"] = hashlib.sha256(payload).hexdigest()
        objects = [obj for obj in yaml.safe_load_all(payload) if obj is not None]
        schemas = {obj["spec"]["names"]["kind"]: next(version for version in obj["spec"]["versions"] if version["name"] == "v1")["schema"]["openAPIV3Schema"]
                   for obj in objects if obj["kind"] == "CustomResourceDefinition" and obj["metadata"]["name"] in {"tasks.tekton.dev", "pipelines.tekton.dev", "pipelineruns.tekton.dev"}}
        for runtime in renderer.RUNTIMES:
            files = renderer.render(plan, payload, runtime)
            result = list(yaml.safe_load_all(files["tekton-components.yaml"]))
            self.assertEqual(len(result), len(objects) + 4)
            for obj in list(yaml.safe_load_all(files["tekton-canary.yaml"])) + [yaml.safe_load(files["tekton-canary-run.yaml"])]:
                if obj["kind"] in schemas:
                    declared_fields(obj, schemas[obj["kind"]])


if __name__ == "__main__":
    unittest.main()
