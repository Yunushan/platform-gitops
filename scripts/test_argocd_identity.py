#!/usr/bin/env python3
"""Check guarded credential handoff, CAS updates and uncertain-write resume."""

import copy
from contextlib import contextmanager
import hashlib
import itertools
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import yaml

import apply_argocd_identity as apply
import configure_keycloak_argocd as identity
import test_keycloak_argocd as provider_tests
from test_keycloak_realm import uid


class KubeClient:
    def __init__(self):
        self.calls = []; self.writes = []; self.lost_ack = None; self.unknown = None; self.admission = None; self.project_items = []
        self.resources = {
            "namespace/kube-system": {"metadata": {"uid": uid(10)}}, "namespace/argocd": {"metadata": {"uid": uid(11)}},
            "deployment/argo-cd-argocd-server": {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "argo-cd-argocd-server", "namespace": "argocd", "resourceVersion": "1", "uid": uid(12), "generation": 1}, "spec": {"replicas": 2, "template": {"metadata": {"labels": {"app.kubernetes.io/name": "argocd-server"}}, "spec": {"containers": [{"name": "server", "image": "registry.example.com/argocd@sha256:" + "a" * 64}]}}},
                "status": {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}}}
        for index, (name, data) in enumerate((("argocd-cm", {"admin.enabled": "true", "timeout.reconciliation": "180s"}), ("argocd-rbac-cm", {"policy.csv": "g, previous-group, role:readonly", "policy.default": "role:readonly", "unrelated-setting": "preserved"}))):
            self.resources["configmap/" + name] = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"uid": uid(20 + index), "name": name, "namespace": "argocd", "resourceVersion": "1", "labels": {"app.kubernetes.io/part-of": "argocd"}}, "data": data}

    @contextmanager
    def frozen(self): yield self

    def get(self, name, namespace=None, *, optional=False):
        self.calls.append(["get-json", name]); return copy.deepcopy(self.resources.get(name))

    def call(self, args, *, payload=None, seconds=30):
        self.calls.append(list(args))
        if args[0] == "config": return json.dumps({"current-context": "application", "contexts": [{"name": "application", "context": {"cluster": "fixture"}}], "clusters": [{"name": "fixture", "cluster": {"server": "https://api.example.com", "certificate-authority-data": "fixture-ca"}}]})
        if args[:2] == ["get", "appprojects.argoproj.io"]: return json.dumps({"items": self.project_items})
        if args[0] == "get" and args[1].startswith("secret/"):
            obj = self.resources.get(args[1]); meta = (obj or {}).get("metadata", {}); annotations = meta.get("annotations", {})
            return "\n".join([meta["uid"], annotations.get(apply.OPERATION, "<no value>"), annotations.get(apply.CONTRACT, "<no value>")]) if obj else ""
        dry = "--dry-run=server" in args
        if args[0] == "create":
            obj = json.loads(payload); name = "secret/" + obj["metadata"]["name"]; obj["metadata"].update(uid=uid(30), resourceVersion="1")
        elif args[0] == "patch":
            name = args[1]; obj = copy.deepcopy(self.resources[name]); patch = json.loads(Path(args[args.index("--patch-file") + 1]).read_text())
            if obj["metadata"]["uid"] != patch[0]["value"] or obj["metadata"]["resourceVersion"] != patch[1]["value"]: raise ValueError("CAS conflict")
            if obj["kind"] == "Deployment":
                obj["spec"]["template"]["metadata"]["annotations"] = patch[2]["value"]; obj["metadata"]["generation"] += 1; obj["status"]["observedGeneration"] = obj["metadata"]["generation"]
            else: obj["data"] = patch[2]["value"]
            obj["metadata"]["annotations"] = patch[3]["value"]; obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        else: raise AssertionError("Unexpected Kubernetes operation")
        if self.admission: self.admission(obj)
        if not dry:
            self.writes.append(name)
            if self.unknown == name: raise TimeoutError("Unknown write outcome")
            self.resources[name] = copy.deepcopy(obj)
            if self.lost_ack == name: self.lost_ack = None; raise TimeoutError("Lost acknowledgement")
        return json.dumps(obj)


class Server:
    def __init__(self, client): self.client = client; self.version = "v3.5.0+fixture"; self.stale = False

    def get(self, path):
        if path == "/api/version": return {"Version": self.version}
        if self.stale: return {}
        data = self.client.resources["configmap/argocd-cm"]["data"]
        if "oidc.config" not in data: return {}
        oidc = yaml.safe_load(data["oidc.config"])
        return {"url": data["url"], "oidcConfig": {"name": oidc["name"], "issuer": oidc["issuer"], "clientID": oidc["clientID"], "enablePKCEAuthentication": oidc["enablePKCEAuthentication"], "scopes": oidc["requestedScopes"], "idTokenClaims": oidc["requestedIDTokenClaims"]}}


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.source = provider_tests.ArgoCDIdentityTests(methodName="runTest"); self.addCleanup(self.source.doCleanups); self.source.setUp()
        self.root, self.bundle = self.source.root, self.source.bundle
        self.patch = mock.patch.object(apply, "ROOT", self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        operation = self.root / "private/provider-client"; self.source.action("apply", operation)
        identity_file = self.root / "private/identity-plan.json"; identity_file.write_text(json.dumps(self.source.plan))
        proof = operation / "keycloak-argocd-proof.json"
        self.client = KubeClient(); self.public = Server(self.client); self.counter = 0
        deployment = self.client.resources["deployment/argo-cd-argocd-server"]
        self.plan = {"schema_version": 1, "identity_plan": {"path": str(identity_file.absolute()), "sha256": hashlib.sha256(identity_file.read_bytes()).hexdigest()},
            "identity_proof": {"path": str(proof.absolute()), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}, "target": {"kubeconfig_env": "ARGO_MANAGEMENT_CONFIG", "context": "application", "cluster_uid": uid(10)}, "namespace_uid": uid(11),
            "server": {"deployment": "argo-cd-argocd-server", "uid": uid(12), "pod_template_sha256": apply.fingerprint(deployment["spec"]["template"]), "spec_sha256": apply.fingerprint(deployment["spec"]), "image": deployment["spec"]["template"]["spec"]["containers"][0]["image"]}, "rollout_id": uid(50),
            "configmaps": {name: {"uid": self.client.resources["configmap/" + name]["metadata"]["uid"], "sha256": apply.fingerprint(apply.material(self.client.resources["configmap/" + name]))} for name in apply.NAMES},
            "projects_sha256": apply.fingerprint([]), "argo_ca_file": str(self.source.ca.absolute()), "argo_ca_sha256": hashlib.sha256(self.source.ca.read_bytes()).hexdigest(), "admitted_sha256": None}

    def action(self, mode, output=None):
        self.counter += 1
        return apply.operate(self.bundle, self.plan, mode=mode, output=output or self.root / "private" / ("application-" + str(self.counter)), wait_seconds=5, client=self.client, provider_client=self.source.client, server=self.public)

    def approve(self): self.plan["admitted_sha256"] = self.action("check-server")["admitted_sha256"]

    def test_local_validation_never_constructs_clients_or_reads_credential_inputs(self):
        with mock.patch.object(apply, "Client", side_effect=AssertionError("No API")), mock.patch.dict(apply.os.environ, {}, clear=True):
            self.assertTrue(apply.operate(self.bundle, self.plan)["local_inputs_verified"])
        self.assertFalse(self.client.calls)

    def test_server_check_preserves_live_state_and_produces_reviewable_handoff(self):
        output = self.root / "private/check"; before = copy.deepcopy(self.client.resources)
        result = self.action("check-server", output)
        self.assertTrue(result["server_checked"]); self.assertTrue(apply.sha(result["admitted_sha256"])); self.assertEqual(self.client.resources, before); self.assertFalse(self.client.writes)
        self.assertTrue((output / "argocd-values.yaml").is_file()); self.assertTrue((output / "secret-reference.json").is_file())

    def test_apply_orders_secret_rbac_oidc_and_preserves_unrelated_fields_and_uids(self):
        self.approve(); result = self.action("apply")
        self.assertEqual(self.client.writes, ["secret/platform-sso-argocd", "configmap/argocd-rbac-cm", "configmap/argocd-cm", "deployment/argo-cd-argocd-server"])
        self.assertTrue(result["argocd_configuration_applied"]); self.assertTrue(result["server_settings_observed"])
        self.assertTrue(result["original_rollout_observed"])
        for key in apply.UNVERIFIED: self.assertIs(result[key], False)
        self.assertEqual(self.client.resources["configmap/argocd-cm"]["data"]["admin.enabled"], "true")
        self.assertEqual(self.client.resources["configmap/argocd-rbac-cm"]["data"]["unrelated-setting"], "preserved")
        for name in apply.NAMES: self.assertEqual(self.client.resources["configmap/" + name]["metadata"]["uid"], self.plan["configmaps"][name]["uid"])

    def test_apply_requires_reviewed_admission(self):
        with self.assertRaises(ValueError): self.action("apply")
        self.assertFalse(self.client.writes)

    def test_unsafe_restart_strategies_refuse_before_any_write(self):
        for strategy in ({"type": "Recreate"}, {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "100%", "maxSurge": 0}},
                         {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 0}}):
            self.client = KubeClient(); self.public = Server(self.client)
            deployment = self.client.resources["deployment/argo-cd-argocd-server"]
            deployment["spec"]["strategy"] = strategy
            self.plan["server"]["spec_sha256"] = apply.fingerprint(deployment["spec"])
            with self.assertRaises(ValueError): self.action("check-server")
            self.assertFalse(self.client.writes)

    def test_lost_restart_response_waits_for_new_generation_without_repeating_patch(self):
        self.approve(); output = self.root / "private/restart-progress"
        name = "deployment/argo-cd-argocd-server"; self.client.lost_ack = name
        with self.assertRaises(TimeoutError): self.action("apply", output)
        deployment = self.client.resources[name]; ready = copy.deepcopy(deployment["status"])
        deployment["status"].update(observedGeneration=1, readyReplicas=1, availableReplicas=1, updatedReplicas=1)
        with mock.patch.object(apply.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(apply.time, "sleep"):
            with self.assertRaises(TimeoutError): self.action("resume", output)
        self.assertEqual(len(self.client.writes), 4)
        deployment["status"] = ready
        self.assertTrue(self.action("resume", output)["original_rollout_observed"])
        self.assertEqual(len(self.client.writes), 4)

    def test_preexisting_secret_is_never_adopted_or_read_as_json(self):
        self.client.resources["secret/platform-sso-argocd"] = {"metadata": {"uid": uid(99)}}
        with self.assertRaises(ValueError): self.action("check-server")
        self.assertNotIn(["get-json", "secret/platform-sso-argocd"], self.client.calls); self.assertFalse(self.client.writes)

    def test_original_target_namespace_server_and_project_roles_are_pinned(self):
        for mutation in (lambda: self.client.resources["namespace/kube-system"]["metadata"].update(uid=uid(90)), lambda: self.client.resources["namespace/argocd"]["metadata"].update(uid=uid(90)),
                         lambda: self.client.resources["deployment/argo-cd-argocd-server"]["status"].update(readyReplicas=0), lambda: self.client.project_items.append({"metadata": {"name": "other", "uid": uid(91)}, "spec": {"roles": []}})):
            self.client = KubeClient(); self.public = Server(self.client); mutation()
            with self.assertRaises(ValueError): self.action("check-server")
            self.assertFalse(self.client.writes)

    def test_changed_configmap_and_unreviewed_rbac_fragments_refuse(self):
        self.client.resources["configmap/argocd-cm"]["data"]["admin.enabled"] = "false"
        with self.assertRaises(ValueError): self.action("check-server")
        self.client = KubeClient(); self.public = Server(self.client)
        cm = self.client.resources["configmap/argocd-rbac-cm"]; cm["data"]["policy.other.csv"] = "g, other, role:admin"
        self.plan["configmaps"]["argocd-rbac-cm"]["sha256"] = apply.fingerprint(apply.material(cm))
        with self.assertRaises(ValueError): self.action("check-server")
        self.assertFalse(self.client.writes)

    def test_wrong_application_version_or_changed_provider_credential_refuses(self):
        self.public.version = "v3.5.1"
        with self.assertRaises(ValueError): self.action("check-server")
        self.public.version = "v3.5.0"; self.source.client.application_client["secret"] = "x" * 64
        with self.assertRaises(ValueError): self.action("check-server")
        self.assertFalse(self.client.writes)

    def test_lost_response_observes_each_original_write_without_repeating_it(self):
        for name in ("secret/platform-sso-argocd", "configmap/argocd-rbac-cm", "configmap/argocd-cm", "deployment/argo-cd-argocd-server"):
            self.client = KubeClient(); self.public = Server(self.client); self.approve(); output = self.root / "private" / ("lost-" + name.split("/")[-1]); self.client.lost_ack = name
            with self.assertRaises(TimeoutError): self.action("apply", output)
            self.assertTrue(self.action("resume", output)["argocd_configuration_applied"])
            self.assertEqual(len(self.client.writes), 4); self.assertEqual(len(set(self.client.writes)), 4)

    def test_absent_or_unchanged_unknown_write_is_never_retried(self):
        for name in ("secret/platform-sso-argocd", "configmap/argocd-rbac-cm", "configmap/argocd-cm", "deployment/argo-cd-argocd-server"):
            self.client = KubeClient(); self.public = Server(self.client); self.approve(); self.client.unknown = name; output = self.root / "private" / ("unknown-" + name.split("/")[-1])
            with self.assertRaises(TimeoutError): self.action("apply", output)
            before = len(self.client.writes)
            with mock.patch.object(apply.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(apply.time, "sleep"):
                with self.assertRaises(TimeoutError): self.action("resume", output)
            self.assertEqual(len(self.client.writes), before)

    def test_deleted_and_replaced_original_objects_are_not_recreated(self):
        self.approve(); output = self.root / "private/original"; self.action("apply", output)
        original = self.client.resources.pop("secret/platform-sso-argocd")
        with self.assertRaises(ValueError): self.action("resume", output)
        self.client.resources["secret/platform-sso-argocd"] = original; self.client.resources["configmap/argocd-cm"]["metadata"]["uid"] = uid(92)
        with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(len(self.client.writes), 4)

    def test_unloaded_application_settings_can_be_observed_later_without_new_writes(self):
        self.approve(); output = self.root / "private/reload"; self.public.stale = True
        with mock.patch.object(apply.time, "monotonic", side_effect=itertools.count(0, 10)), mock.patch.object(apply.time, "sleep"):
            with self.assertRaises(TimeoutError): self.action("apply", output)
        self.public.stale = False; self.assertTrue(self.action("resume", output)["server_settings_observed"]); self.assertEqual(len(self.client.writes), 4)

    def test_foreign_admission_changes_fail_before_any_live_write(self):
        self.client.admission = lambda o: o["metadata"]["labels"].update({"unreviewed": "value"})
        with self.assertRaises(ValueError): self.action("check-server")
        self.assertFalse(self.client.writes)

    def test_no_plaintext_client_credential_is_saved_in_operation_artifacts(self):
        self.approve(); output = self.root / "private/privacy"; self.action("apply", output)
        for path in output.iterdir():
            if path.is_file() and self.source.credential.encode() in path.read_bytes(): self.fail("A generated client credential entered an operation artifact")

    def test_resume_source_changes_refuse_before_server_observation(self):
        self.approve(); output = self.root / "private/source"; self.action("apply", output); before = len(self.client.calls)
        with mock.patch.object(apply, "tools", return_value="f" * 64):
            with self.assertRaises(ValueError): self.action("resume", output)
        self.assertEqual(len(self.client.calls), before)

    @unittest.skipUnless(os.environ.get("ARGOCD_IDENTITY_TEST_API") == "1", "Explicit opt-in to disposable real API/etcd processes")
    def test_actual_api_admission_cas_and_original_handle_resume(self):
        from test_argocd_identity_api_fixture import exercise as check
        from test_rook_api_fixture import exercise
        result = exercise(self, management_action=lambda client, root, kubectl: check(self, client, root, kubectl))
        self.assertTrue(result["real_api_admission_and_cas_verified"]); self.assertFalse(result["argocd_runtime_verified"])


if __name__ == "__main__": unittest.main()
