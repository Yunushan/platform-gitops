#!/usr/bin/env python3
"""Actual API checks for the identity handoff; no workers or Argo CD process."""

import copy
from contextlib import contextmanager
import json
from pathlib import Path
import time
from unittest import mock

import yaml

import apply_argocd_identity as apply


class RolloutClient:
    """Only this fixture models a controller; production code never patches status."""
    def __init__(self, client, kubectl, hook=None): self.client, self.kubectl, self.hook = client, kubectl, hook

    @contextmanager
    def frozen(self):
        with self.client.frozen(): yield self

    def get(self, *args, **kwargs): return self.client.get(*args, **kwargs)

    def call(self, args, **kwargs):
        raw = self.client.call(args, **kwargs)
        if args[:2] == ["patch", "deployment/argo-cd-argocd-server"] and "--dry-run=server" not in args:
            obj = json.loads(raw)
            if self.hook: self.hook(obj)
            replicas = obj["spec"].get("replicas", 1)
            self.kubectl(["patch", "deployment/argo-cd-argocd-server", "-n", "argocd", "--subresource=status", "--type=merge", "-p", apply.canonical({"status": {
                "observedGeneration": obj["metadata"]["generation"], "replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas}}), "-o", "json"])
        return raw


def await_crd_conditions(client, created):
    name=created["metadata"]["name"]; identity=created["metadata"].get("uid")
    if not identity: raise ValueError("The original fixture CRD has no UID")
    deadline=time.monotonic()+30
    while True:
        current=client.get("crd/"+name)
        if current.get("metadata",{}).get("uid")!=identity: raise ValueError("The original fixture CRD was replaced")
        conditions=current.get("status",{}).get("conditions")
        if isinstance(conditions,list) and conditions: return
        if time.monotonic()>=deadline: raise TimeoutError("The original fixture CRD has no observed conditions")
        time.sleep(0.2)


def prepare(client, kubectl, *, image="registry.example.com/argocd@sha256:" + "a" * 64, crds=None):
    namespace = kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "argocd"}})
    crd = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition", "metadata": {"name": "appprojects.argoproj.io"}, "spec": {"group": "argoproj.io", "scope": "Namespaced",
        "names": {"plural": "appprojects", "singular": "appproject", "kind": "AppProject", "listKind": "AppProjectList"}, "versions": [{"name": "v1alpha1", "served": True, "storage": True,
            "schema": {"openAPIV3Schema": {"type": "object", "properties": {"spec": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}}}}}]}}
    for definition in crds or [crd]:
        created=kubectl(["create", "-f", "-", "-o", "json"], definition)
        # kubectl 1.35 condition access can fail immediately while the CRD
        # controller has not yet populated a status.conditions array.
        await_crd_conditions(client,created)
        # Server-side discovery is observed, not inferred from the create ACK.
        client.call(["wait", "--for=condition=Established", "crd/" + definition["metadata"]["name"], "--timeout=30s"])
    deployment = kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "argo-cd-argocd-server", "namespace": "argocd"},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "argocd-server"}}, "template": {"metadata": {"labels": {"app.kubernetes.io/name": "argocd-server"}},
            "spec": {"containers": [{"name": "server", "image": image}]}}}})
    # This API-only fixture explicitly simulates readiness. It runs no Pod and
    # cannot certify deployment, networking, login, authorization or HA.
    deployment = kubectl(["patch", "deployment/argo-cd-argocd-server", "--namespace", "argocd", "--subresource=status", "--type=merge", "-p", apply.canonical({"status": {
        "observedGeneration": deployment["metadata"]["generation"], "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}), "-o", "json"])
    configmaps = {}
    for name, data in (("argocd-cm", {"admin.enabled": "true", "timeout.reconciliation": "180s"}), ("argocd-rbac-cm", {"policy.default": "role:readonly", "policy.csv": "", "unrelated-setting": "preserved"})):
        configmaps[name] = kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "argocd", "labels": {"app.kubernetes.io/part-of": "argocd"}}, "data": data})
    return namespace, deployment, configmaps


class Settings:
    def __init__(self, client): self.client = client

    def get(self, path):
        if path == "/api/version": return {"Version": "v3.5.0+api-fixture"}
        data = self.client.get("configmap/argocd-cm", "argocd").get("data", {})
        if "oidc.config" not in data: return {}
        oidc = yaml.safe_load(data["oidc.config"])
        return {"url": data["url"], "oidcConfig": {"name": oidc["name"], "issuer": oidc["issuer"], "clientID": oidc["clientID"], "enablePKCEAuthentication": True, "scopes": oidc["requestedScopes"], "idTokenClaims": oidc["requestedIDTokenClaims"]}}


def exercise(test, client, root, kubectl):
    namespace, deployment, configmaps = prepare(client, kubectl)
    plan = copy.deepcopy(test.plan); plan["target"] = {"kubeconfig_env": "DISPOSABLE_MANAGEMENT_CONFIG", "context": "storage", "cluster_uid": kubectl(["get", "ns/kube-system", "-o", "json"])["metadata"]["uid"]}
    plan["namespace_uid"] = namespace["metadata"]["uid"]
    plan["server"].update(uid=deployment["metadata"]["uid"], pod_template_sha256=apply.fingerprint(deployment["spec"]["template"]), spec_sha256=apply.fingerprint(deployment["spec"]))
    plan["configmaps"] = {name: {"uid": obj["metadata"]["uid"], "sha256": apply.fingerprint(apply.material(obj))} for name, obj in configmaps.items()}
    plan["projects_sha256"] = apply.projects(client, "argocd"); plan["admitted_sha256"] = None
    (root / "private").mkdir(mode=0o700)
    managed = RolloutClient(client, kubectl)
    def run(mode, directory):
        return apply.operate(test.bundle, plan, mode=mode, output=root / "private" / directory, wait_seconds=5, client=managed, provider_client=test.source.client, server=Settings(client))
    with mock.patch.object(apply, "ROOT", root):
        review = run("check-server", "review"); test.assertTrue(review["server_checked"]); plan["admitted_sha256"] = review["admitted_sha256"]
        result = run("apply", "operation"); test.assertTrue(result["argocd_configuration_applied"])
        test.assertTrue(run("resume", "operation")["server_settings_observed"])
        for name, original in configmaps.items(): test.assertEqual(client.get("configmap/" + name, "argocd")["metadata"]["uid"], original["metadata"]["uid"])
        with test.assertRaises(ValueError): run("apply", "refuse-existing")
        secret = client.get("secret/platform-sso-argocd", "argocd"); original_secret_uid = secret["metadata"]["uid"]
        test.assertTrue(secret["immutable"])
        client.call(["delete", "secret/platform-sso-argocd", "--namespace", "argocd"])
        with test.assertRaises(ValueError): run("resume", "operation")
        test.assertIsNone(client.get("secret/platform-sso-argocd", "argocd", optional=True))
        test.assertTrue(apply.uid(original_secret_uid))
    return {"real_api_admission_and_cas_verified": True, "original_uids_preserved": True, "deleted_secret_not_recreated": True, "argocd_runtime_verified": False, "ha_verified": False}
