#!/usr/bin/env python3
"""Verify the GitLab handoff Kubernetes reader's exact scope."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

from ci_gitops_handoff import sha256
from render_gitlab_gitops_access import render
import test_gitlab_gitops_handoff as pipeline_fixture
import test_rook_api_fixture as api_fixture


def reviewed_plan(kind, *, separate_flux_source=False):
    with tempfile.TemporaryDirectory() as directory:
        fixed, _, _ = pipeline_fixture.GitLabHandoffTest().setup_case(Path(directory), kind)
    pipeline = {key: value for key, value in fixed["pipeline"].items() if key not in {"pipeline_id", "revision"}}
    handoff = dict(fixed["handoff"], bridge_contract_sha256=sha256(pipeline))
    if separate_flux_source:
        handoff["cd"]["source_namespace"] = "flux-source"
    return {"schema_version": 2, "runtime": "rke2", "pipeline_policy": pipeline, "handoff": handoff}


class GitLabAccessTest(unittest.TestCase):
    def test_only_exact_objects_and_cluster_identity_are_readable(self):
        for kind in ("argocd", "flux"):
            with self.subTest(kind=kind):
                plan = reviewed_plan(kind, separate_flux_source=kind == "flux")
                cd = plan["handoff"]["cd"]
                objects = render(plan, sha256(plan), namespace="automation", account="observer")
                self.assertEqual(objects[0]["kind"], "ServiceAccount")
                self.assertFalse(objects[0]["automountServiceAccountToken"])
                cluster_roles = [obj for obj in objects if obj["kind"] == "ClusterRole"]
                self.assertEqual([obj["rules"] for obj in cluster_roles],
                                 [[{"apiGroups": [""], "resources": ["namespaces"],
                                    "resourceNames": ["kube-system"], "verbs": ["get"]}]])
                actual = {(obj["metadata"]["namespace"], tuple(rule["apiGroups"]), tuple(rule["resources"]),
                           tuple(rule["resourceNames"]), tuple(rule["verbs"]))
                          for obj in objects if obj["kind"] == "Role" for rule in obj["rules"]}
                if kind == "argocd":
                    self.assertEqual(actual, {(cd["namespace"], ("argoproj.io",), ("applications",), (cd["name"],), ("get",))})
                else:
                    self.assertEqual(actual, {
                        (cd["namespace"], ("kustomize.toolkit.fluxcd.io",), ("kustomizations",), (cd["name"],), ("get",)),
                        ("flux-source", ("source.toolkit.fluxcd.io",), ("gitrepositories",), (cd["source_name"],), ("get",)),
                    })
                for obj in objects:
                    if obj["kind"].endswith("Binding"):
                        self.assertEqual(obj["subjects"], [{"kind": "ServiceAccount", "name": "observer", "namespace": "automation"}])

    def test_non_reviewed_inputs_are_rejected(self):
        plan = reviewed_plan("argocd")
        for digest, namespace, account in (("0" * 64, "automation", "observer"),
                                            (sha256(plan), "kube-system", "observer"),
                                            (sha256(plan), "automation", "observer/system"),
                                            (sha256(plan), "automation", "default")):
            with self.subTest(namespace=namespace, account=account), self.assertRaises(ValueError):
                render(plan, digest, namespace=namespace, account=account)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("ROOK_BOOTSTRAP_TEST_BIN")
                         and os.environ.get("GITLAB_CRONJOB_API_FIXTURE") == "1", "disposable API fixture not requested")
    def test_api_authorizes_distinct_flux_namespaces_and_denies_other_access(self):
        plan = reviewed_plan("flux", separate_flux_source=True)
        cd = plan["handoff"]["cd"]
        objects = render(plan, sha256(plan), namespace="automation", account="observer")

        def check(_, _root, kubectl):
            for namespace in {"automation", cd["namespace"], cd["source_namespace"]}:
                kubectl(["create", "-f", "-", "-o", "json"],
                        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
            for obj in objects:
                kubectl(["create", "-f", "-", "-o", "json"], obj)

            def allowed(verb, resource, *, namespace=None, name=None):
                spec = {"user": "system:serviceaccount:automation:observer",
                        "resourceAttributes": {"verb": verb, **resource}}
                if namespace is not None:
                    spec["resourceAttributes"]["namespace"] = namespace
                if name is not None:
                    spec["resourceAttributes"]["name"] = name
                result = kubectl(["create", "-f", "-", "-o", "json"], {
                    "apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview", "spec": spec,
                })
                return result["status"]["allowed"]

            namespace = {"resource": "namespaces"}
            source = {"group": "source.toolkit.fluxcd.io", "resource": "gitrepositories"}
            deployment = {"group": "kustomize.toolkit.fluxcd.io", "resource": "kustomizations"}
            self.assertTrue(allowed("get", namespace, name="kube-system"))
            self.assertFalse(allowed("get", namespace, name="default"))
            self.assertTrue(allowed("get", source, namespace="flux-source", name=cd["source_name"]))
            self.assertFalse(allowed("get", source, namespace=cd["namespace"], name=cd["source_name"]))
            self.assertFalse(allowed("get", source, namespace="flux-source", name="unreviewed"))
            self.assertFalse(allowed("list", source, namespace="flux-source"))
            self.assertTrue(allowed("get", deployment, namespace=cd["namespace"], name=cd["name"]))
            self.assertFalse(allowed("patch", deployment, namespace=cd["namespace"], name=cd["name"]))
            self.assertFalse(allowed("get", {"resource": "secrets"}, namespace="automation", name="gitlab-handoff-inputs"))

        api_fixture.exercise(self, management_action=check)


if __name__ == "__main__":
    unittest.main()
