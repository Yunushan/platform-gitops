#!/usr/bin/env python3
"""Test Flux artifact availability, target binding and ordered deployment guards."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from bounded_file import read_bounded_bytes
import deploy_flux as deploy
import kubernetes_management as management
import render_flux as renderer


def fixture():
    objects = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "flux-system"}}]
    for kind, group in (("gitrepositories", "source"), ("kustomizations", "kustomize")):
        objects.append({"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": kind + "." + group + ".toolkit.fluxcd.io"}, "spec": {"group": group + ".toolkit.fluxcd.io"}})
    for name in sorted(renderer.CORE):
        manager = {"name": "manager", "image": "ghcr.io/fluxcd/" + name + ":v1.0.0", "args": ["--enable-leader-election"],
                   "ports": [{"name": "healthz", "containerPort": 9440}], "volumeMounts": []}
        pod = {"serviceAccountName": name, "containers": [manager], "volumes": []}
        if name == "source-controller":
            manager["args"] += ["--storage-path=/data", "--storage-adv-addr=source-controller.$(RUNTIME_NAMESPACE).svc.cluster.local."]
            manager["ports"].append({"name": "http", "containerPort": 9090})
            manager["volumeMounts"].append({"name": "data", "mountPath": "/data"})
            pod["volumes"].append({"name": "data", "emptyDir": {}})
        objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": "flux-system"},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {"metadata": {"labels": {"app": name}}, "spec": pod}}})
    objects.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "source-controller", "namespace": "flux-system"},
                    "spec": {"selector": {"app": "source-controller"}, "ports": [{"port": 80, "targetPort": "http"}]}})
    payload = yaml.safe_dump_all(objects, sort_keys=False).encode()
    plan = {"schema_version": 1, "flux_version": "2.6.4", "namespace": "flux-system",
        "bundle_sha256": hashlib.sha256(payload).hexdigest(),
        "images": {name: "registry.example.com/flux/" + name + "@sha256:" + "a" * 64 for name in renderer.CORE},
        "artifact_server_image": "registry.example.com/platform/nginx@sha256:" + "b" * 64,
        "artifact_claim": "flux-artifacts", "failure_domain_key": "kubernetes.io/hostname",
        "node_selector": {"platform.gitops/control-service": "true"},
        "management_target": {"kubeconfig_env": "PLATFORM_MANAGEMENT_KUBECONFIG", "context": "management",
                              "cluster_uid": "11111111-1111-4111-8111-111111111111"},
        "sync": {"name": "platform", "namespace": "platform-apps", "url": "ssh://git@review.example.com:29418/platform/deploy",
                 "branch": "main", "path": "./clusters/platform", "read_secret": "flux-read", "service_account": "platform-reconciler", "prune": False}}
    return plan, payload


def package(directory: Path):
    plan, payload = fixture()
    files = renderer.render(plan, payload, "rke2")
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8", newline="\n")
    return plan, json.loads(files["render-status.json"])["contract_sha256"]


class FakeClient:
    def __init__(self, plan):
        self.plan, self.calls = plan, []
        self.owner = None
        self.cluster_uid = plan["management_target"]["cluster_uid"]
        self.access_modes = ["ReadWriteMany"]
        self.stale = False
        self.unverified_api = False
        self.mutation = None
        self.context = plan["management_target"]["context"]
        self.revision_mismatch = False

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append((args, payload, seconds))
        if args[:2] == ["config", "view"]:
            return json.dumps({"current-context": self.context, "contexts": [{"name": self.context, "context": {"cluster": "selected"}}],
                               "clusters": [{"name": "selected", "cluster": {"server": "https://api.example.com:6443", "insecure-skip-tls-verify": self.unverified_api}}]})
        if self.mutation and args and args[0] == "apply" and "--dry-run=server" in args:
            self.mutation()
        return ""

    def get(self, resource, namespace=None, *, optional=False):
        self.calls.append((["get", resource, namespace], None, 30))
        if resource == "namespace/kube-system":
            return {"metadata": {"uid": self.cluster_uid}}
        if resource.startswith("persistentvolumeclaim/"):
            return {"spec": {"accessModes": self.access_modes}, "status": {"phase": "Bound"}}
        if resource.startswith("serviceaccount/"):
            return {"metadata": {"name": "platform-reconciler"}}
        if resource.startswith(("gitrepository.", "kustomization.")) and not optional:
            generation = 2
            observed = 1 if self.stale else generation
            status = {"observedGeneration": observed, "conditions": [{"type": "Ready", "status": "True", "observedGeneration": observed}]}
            if resource.startswith("gitrepository."):
                status["artifact"] = {"revision": "main@sha1:" + "a" * 40}
            else:
                status["lastAppliedRevision"] = "main@sha1:" + ("b" if self.revision_mismatch else "a") * 40
            return {"metadata": {"generation": generation}, "status": status}
        if optional:
            return {"metadata": {"labels": {renderer.OWNER_LABEL: self.owner}}} if self.owner is not None else None
        raise AssertionError("Unexpected test resource query")


class FluxTests(unittest.TestCase):
    def test_artifact_service_targets_independent_read_only_server_in_every_replica(self):
        plan, payload = fixture()
        objects = list(yaml.safe_load_all(renderer.render(plan, payload, "rke2")["flux-components.yaml"]))
        source = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "source-controller")
        pod = source["spec"]["template"]["spec"]
        manager, server = pod["containers"]
        self.assertIn("--storage-addr=127.0.0.1:9091", manager["args"])
        self.assertIn("--enable-leader-election", manager["args"])
        self.assertNotIn("http", {port["name"] for port in manager["ports"]})
        self.assertEqual(manager["readinessProbe"]["httpGet"], {"path": "/readyz", "port": "healthz"})
        self.assertEqual(server["ports"], [{"name": "http", "containerPort": 9090}])
        self.assertTrue(server["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertIn("platform-api", {mount["name"] for mount in manager["volumeMounts"]})
        self.assertNotIn("platform-api", {mount["name"] for mount in server["volumeMounts"]})
        data = next(mount for mount in server["volumeMounts"] if mount["name"] == "data")
        self.assertTrue(data["readOnly"])
        volume = next(volume for volume in pod["volumes"] if volume["name"] == "data")
        self.assertEqual(volume["persistentVolumeClaim"]["claimName"], plan["artifact_claim"])
        self.assertNotIn("emptyDir", volume)
        service = next(item for item in objects if item["kind"] == "Service")
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], server["ports"][0]["name"])
        self.assertIn("autoindex off", renderer.NGINX)
        self.assertIn("limit_except GET", renderer.NGINX)

    def test_every_controller_has_leader_election_separate_hosts_and_disruption_budget(self):
        plan, payload = fixture()
        objects = list(yaml.safe_load_all(renderer.render(plan, payload, "k8s")["flux-components.yaml"]))
        budgets = {obj["metadata"]["name"]: obj["spec"] for obj in objects if obj["kind"] == "PodDisruptionBudget"}
        for deployment in (obj for obj in objects if obj["kind"] == "Deployment"):
            self.assertEqual(deployment["spec"]["replicas"], 2)
            self.assertEqual(deployment["spec"]["strategy"]["rollingUpdate"], {"maxSurge": 0, "maxUnavailable": 1})
            pod = deployment["spec"]["template"]["spec"]
            self.assertIn("--enable-leader-election", pod["containers"][0]["args"])
            self.assertEqual(pod["nodeSelector"], plan["node_selector"])
            anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
            self.assertEqual(anti["topologyKey"], plan["failure_domain_key"])
            self.assertEqual(budgets[deployment["metadata"]["name"]]["minAvailable"], 1)

    def test_sync_is_scoped_and_secrets_are_references(self):
        plan, payload = fixture()
        files = renderer.render(plan, payload, "docker")
        source, sync = list(yaml.safe_load_all(files["flux-sync.yaml"]))
        self.assertEqual(source["spec"]["secretRef"], {"name": "flux-read"})
        self.assertEqual(source["metadata"]["namespace"], "platform-apps")
        self.assertEqual(sync["spec"]["serviceAccountName"], "platform-reconciler")
        self.assertFalse(sync["spec"]["force"])
        self.assertFalse(sync["spec"]["prune"])
        for runtime in renderer.RUNTIMES:
            status = json.loads(renderer.render(plan, payload, runtime)["render-status.json"])
            self.assertFalse(status["deployed"])
            self.assertFalse(status["ha_verified"])
        for value in ("https://user:password@example.com/repo", "http://review.example.com/repo", "ssh://git@review.example.com/repo?token=bad"):
            bad = copy.deepcopy(plan)
            bad["sync"]["url"] = value
            with self.assertRaises(ValueError):
                renderer.validate(bad)

    def test_malformed_changed_or_incomplete_exports_are_not_silently_patched(self):
        plan, payload = fixture()
        with self.assertRaises(ValueError):
            renderer.render(plan, payload + b"\n", "raw")
        for mutation in ("foreign-controller", "secret", "no-election", "no-persistent-layout"):
            objects = list(yaml.safe_load_all(payload))
            if mutation == "secret":
                objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "unexpected", "namespace": "flux-system"}})
            else:
                source = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "source-controller")
                if mutation == "foreign-controller": source["metadata"]["name"] = "unknown-controller"
                if mutation == "no-election": source["spec"]["template"]["spec"]["containers"][0]["args"].remove("--enable-leader-election")
                if mutation == "no-persistent-layout": source["spec"]["template"]["spec"]["volumes"] = []
            changed = yaml.safe_dump_all(objects).encode()
            bad = {**plan, "bundle_sha256": hashlib.sha256(changed).hexdigest()}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                renderer.render(bad, changed, "rke2")

    def test_default_deployment_check_reads_only_locked_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, digest = package(root)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with mock.patch.object(deploy, "Client") as client:
                report = deploy.operate(root, digest)
                client.assert_not_called()
            self.assertTrue(report["read_only"])
            self.assertFalse(report["deployed"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
            (root / "flux-sync.yaml").write_bytes(b"changed")
            with mock.patch.object(deploy, "Client") as client, self.assertRaises(ValueError):
                deploy.operate(root, digest, apply=True)
            client.assert_not_called()

    def test_wrong_cluster_insecure_api_rwo_storage_and_foreign_ownership_prevent_writes(self):
        for failure in ("cluster", "context", "unverified", "storage", "ownership"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, digest = package(root)
                client = FakeClient(plan)
                if failure == "cluster": client.cluster_uid = "22222222-2222-4222-8222-222222222222"
                if failure == "context": client.context = "wrong-context"
                if failure == "unverified": client.unverified_api = True
                if failure == "storage": client.access_modes = ["ReadWriteOnce"]
                if failure == "ownership": client.owner = "another-manager"
                with mock.patch.object(deploy, "Client", return_value=client) as factory, self.assertRaises(ValueError):
                    deploy.operate(root, digest, apply=True)
                factory.assert_called_once()
                self.assertFalse(any(args[0] == "apply" for args, _, _ in client.calls))

    def test_apply_orders_crd_controller_and_sync_readiness_without_forcing_or_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            # Disk changes after local verification must not change the applied bytes.
            original = (root / "flux-components.yaml").read_bytes()
            client.mutation = lambda: (root / "flux-components.yaml").write_bytes(b"unreviewed-after-check")
            with mock.patch.object(deploy, "Client", return_value=client):
                report = deploy.operate(root, digest, apply=True)
            self.assertTrue(report["deployed"])
            self.assertTrue(report["bootstrap_ready"])
            self.assertFalse(report["reconciliation_verified"])
            self.assertFalse(report["ha_verified"])
            calls = [args for args, _, _ in client.calls]
            applies = [(index, data) for index, (args, data, _) in enumerate(client.calls) if args[0] == "apply" and "--dry-run=server" not in args]
            self.assertEqual(len(applies), 2)
            self.assertEqual(applies[0][1], original)
            controller_end = max(index for index, args in enumerate(calls) if args[0] == "rollout")
            self.assertLess(controller_end, applies[1][0])
            self.assertTrue(all("--server-side" in args for args in calls if args[0] == "apply"))
            self.assertFalse(any(any(value in {"--force-conflicts", "delete", "--prune"} for value in args) for args in calls))

    def test_stale_ready_conditions_cannot_be_reported_as_current_bootstrap_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            client.stale = True
            with mock.patch.object(deploy, "Client", return_value=client), self.assertRaises(ValueError):
                deploy.operate(root, digest, apply=True)
            self.assertEqual(len([args for args, _, _ in client.calls if args[0] == "apply" and "--dry-run=server" not in args]), 2)

    def test_server_check_only_dry_runs_components_and_never_submits_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            with mock.patch.object(deploy, "Client", return_value=client):
                report = deploy.operate(root, digest, check_server=True)
            self.assertTrue(report["read_only"])
            self.assertTrue(report["server_checked"])
            self.assertFalse(report["deployed"])
            mutations = [(args, data) for args, data, _ in client.calls if args[0] == "apply"]
            self.assertEqual(len(mutations), 1)
            self.assertIn("--dry-run=server", mutations[0][0])
            self.assertNotIn(b"kind: GitRepository", mutations[0][1])

    def test_ready_on_an_older_revision_cannot_be_reported_as_bootstrap_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            client.revision_mismatch = True
            with mock.patch.object(deploy, "Client", return_value=client), self.assertRaisesRegex(ValueError, "captured source revision"):
                deploy.operate(root, digest, apply=True)
            self.assertTrue(any(args[0] == "wait" and any("lastAppliedRevision" in arg for arg in args) for args, _, _ in client.calls))

    def test_actual_client_binds_context_and_limits_output_without_changing_ambient_context(self):
        plan, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "private-config"
            config.write_text("non-secret-test-fixture", encoding="utf-8")
            with mock.patch.dict(os.environ, {plan["management_target"]["kubeconfig_env"]: str(config)}), \
                    mock.patch.object(management.shutil, "which", return_value="/reviewed/kubectl"), \
                    mock.patch.object(management, "run_bounded", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as runner:
                client = deploy.Client(plan["management_target"])
                client.get("namespace/kube-system")
                args = runner.call_args.args[0]
                self.assertEqual(args[:5], ["/reviewed/kubectl", "--kubeconfig", str(config), "--context", "management"])
                self.assertIn("--request-timeout=30s", args)
                self.assertEqual(runner.call_args.kwargs["output_max_bytes"], 4 * 1024 ** 2)
                self.assertGreater(runner.call_args.kwargs["timeout"], 30)
                self.assertNotIn("use-context", args)

    def test_source_changes_during_revision_check_require_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, digest = package(root)
            client = FakeClient(plan)
            original_get = client.get
            source_reads = 0
            def get(resource, namespace=None, *, optional=False):
                nonlocal source_reads
                result = original_get(resource, namespace, optional=optional)
                if resource.startswith("gitrepository.") and not optional:
                    source_reads += 1
                    if source_reads > 1:
                        result["status"]["artifact"]["revision"] = "main@sha1:" + "c" * 40
                return result
            with mock.patch.object(deploy, "Client", return_value=client), mock.patch.object(client, "get", side_effect=get), \
                    self.assertRaisesRegex(ValueError, "source changed"):
                deploy.operate(root, digest, apply=True)
            self.assertEqual(source_reads, 2)

    def test_cli_writes_new_private_bundle_and_suppresses_target_details(self):
        plan, payload = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, export_path = root / "plan.json", root / "export.yaml"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            export_path.write_bytes(payload)
            output = root / "private" / "flux"
            args = ["--plan", str(plan_path), "--bundle", str(export_path), "--runtime", "raw", "--output-dir", str(output)]
            with mock.patch.object(renderer, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(renderer.main(args), 0)
            self.assertNotIn("example.com", stdout.getvalue())
            with mock.patch.object(renderer, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(renderer.main(args), 1)

    def test_optional_real_upstream_export_is_compatible(self):
        path = os.environ.get("FLUX_TEST_EXPORT", "")
        if not path:
            self.skipTest("Set FLUX_TEST_EXPORT to a reviewed upstream install export for release compatibility testing")
        payload = read_bounded_bytes(Path(path), max_bytes=16 * 1024 ** 2)
        plan, _ = fixture()
        objects = list(yaml.safe_load_all(payload))
        plan["bundle_sha256"] = hashlib.sha256(payload).hexdigest()
        plan["images"] = {obj["metadata"]["name"]: "registry.example.com/flux/" + obj["metadata"]["name"] + "@sha256:" + "a" * 64
                          for obj in objects if obj["kind"] == "Deployment"}
        for runtime in renderer.RUNTIMES:
            rendered = renderer.render(plan, payload, runtime)
            self.assertEqual(len(list(yaml.safe_load_all(rendered["flux-components.yaml"]))), len(objects) + len(plan["images"]) + 1)


if __name__ == "__main__":
    unittest.main()
