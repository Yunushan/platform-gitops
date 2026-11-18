#!/usr/bin/env python3
"""Optional isolated Kubernetes API admission and authorization test for GitLab Runner."""

import os
from pathlib import Path
import sys
import unittest

import yaml

import test_rook_api_fixture as api_fixture


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("ROOK_BOOTSTRAP_TEST_BIN")
                     and os.environ.get("GITLAB_RUNNER_KUSTOMIZE"), "isolated API fixture not requested")
class RunnerApiFixtureTest(unittest.TestCase):
    def test_admission_and_namespace_scoped_authorization(self):
        source = Path(os.environ["GITLAB_RUNNER_KUSTOMIZE"])
        docs = [obj for obj in yaml.safe_load_all(source.read_text(encoding="utf-8")) if isinstance(obj, dict)]
        # The profile's private placeholder is replaced by a synthetic reference;
        # no Secret or credential is needed for API admission.
        for obj in docs:
            if obj.get("kind") == "Deployment":
                for volume in obj["spec"]["template"]["spec"].get("volumes", []):
                    for item in volume.get("projected", {}).get("sources", []):
                        secret = item.get("secret", {})
                        if secret.get("name") == "<GITLAB_RUNNER_TOKEN_SECRET_NAME>":
                            secret["name"] = "fixture-runner-token"

        def run_fixture(client, root, kubectl):
            namespaces = [obj for obj in docs if obj["kind"] == "Namespace"]
            self.assertEqual({obj["metadata"]["name"] for obj in namespaces}, {"gitlab-runner", "gitlab-jobs"})
            for obj in namespaces + [obj for obj in docs if obj["kind"] != "Namespace"]:
                admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], obj)
                self.assertEqual(admitted["kind"], obj["kind"])
                created = kubectl(["create", "-f", "-", "-o", "json"], obj)
                self.assertTrue(created["metadata"]["uid"])

            def allowed(namespace, account, verb, resource, target_namespace, subresource=None):
                account_user = f"system:serviceaccount:{namespace}:{account}"
                attributes = {"namespace": target_namespace, "verb": verb, "resource": resource}
                if subresource:
                    attributes["subresource"] = subresource
                request = {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview",
                           "spec": {"user": account_user, "groups": ["system:serviceaccounts",
                                    f"system:serviceaccounts:{namespace}", "system:authenticated"],
                                    "resourceAttributes": attributes}}
                return kubectl(["create", "-f", "-", "-o", "json"], request)["status"].get("allowed", False)

            self.assertTrue(allowed("gitlab-runner", "gitlab-runner", "create", "pods", "gitlab-jobs"))
            self.assertTrue(allowed("gitlab-runner", "gitlab-runner", "get", "secrets", "gitlab-jobs"))
            self.assertTrue(allowed("gitlab-runner", "gitlab-runner", "create", "pods", "gitlab-jobs", "attach"))
            self.assertFalse(allowed("gitlab-runner", "gitlab-runner", "get", "secrets", "gitlab-runner"))
            self.assertFalse(allowed("gitlab-jobs", "gitlab-job", "get", "secrets", "gitlab-runner"))
            self.assertFalse(allowed("gitlab-jobs", "gitlab-job", "create", "pods", "gitlab-jobs"))

            pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "fixture-job", "namespace": "gitlab-jobs"},
                   "spec": {"serviceAccountName": "gitlab-job", "automountServiceAccountToken": False,
                            "containers": [{"name": "job", "image": "example.invalid/fixture:local"}]}}
            admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], pod)
            self.assertFalse(admitted["spec"]["automountServiceAccountToken"])
            self.assertFalse(any(volume.get("projected") for volume in admitted["spec"].get("volumes", [])))

        api_fixture.exercise(self, management_action=run_fixture)


if __name__ == "__main__":
    unittest.main()
