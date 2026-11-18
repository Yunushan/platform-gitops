import os
from pathlib import Path
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "gitops/clusters/rke2-main/premium-3node/apps/gitlab-runner/values.yaml"


class GitLabRunnerProfileTest(unittest.TestCase):
    def test_values_require_external_secret_and_namespace_scoped_permissions(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["runners"]["secret"], "<GITLAB_RUNNER_TOKEN_SECRET_NAME>")
        self.assertNotIn("runnerToken", values)
        self.assertFalse(values["rbac"]["clusterWideAccess"])
        self.assertEqual(values["runners"]["jobNamespace"], "gitlab-jobs")
        job = tomllib.loads(values["runners"]["config"])["runners"][0]["kubernetes"]
        self.assertEqual(job["namespace"], "gitlab-jobs")
        self.assertEqual(job["service_account"], "gitlab-job")
        self.assertFalse(job["automount_service_account_token"])
        self.assertTrue(values["rbac"]["rules"])
        for rule in values["rbac"]["rules"]:
            self.assertNotIn("*", rule["resources"])
            self.assertNotIn("*", rule["verbs"])

    @unittest.skipUnless(os.environ.get("GITLAB_RUNNER_RENDER"), "no rendered runner chart supplied")
    def test_vendored_chart_uses_secret_reference_and_scoped_role(self):
        docs = [x for x in yaml.safe_load_all(Path(os.environ["GITLAB_RUNNER_RENDER"]).read_text(encoding="utf-8")) if isinstance(x, dict)]
        self.assertFalse(any(x["kind"] in {"ClusterRole", "ClusterRoleBinding", "Secret"} for x in docs))
        role = next(x for x in docs if x["kind"] == "Role")
        for rule in role["rules"]:
            self.assertNotIn("*", rule["resources"])
            self.assertNotIn("*", rule["verbs"])
        pod = next(x for x in docs if x["kind"] == "Deployment")["spec"]["template"]["spec"]
        projected = next(v["projected"] for v in pod["volumes"] if v["name"] == "projected-secrets")
        token_source = projected["sources"][0]["secret"]
        self.assertEqual(token_source["name"], "<GITLAB_RUNNER_TOKEN_SECRET_NAME>")
        self.assertEqual({item["key"] for item in token_source["items"]}, {"runner-token", "runner-registration-token"})

    @unittest.skipUnless(os.environ.get("GITLAB_RUNNER_KUSTOMIZE"), "no Kustomize render supplied")
    def test_kustomize_keeps_runner_authority_out_of_manager_namespace(self):
        docs = [x for x in yaml.safe_load_all(Path(os.environ["GITLAB_RUNNER_KUSTOMIZE"]).read_text(encoding="utf-8")) if isinstance(x, dict)]
        objects = {(x["kind"], x["metadata"]["name"]): x for x in docs}
        self.assertIn(("Namespace", "gitlab-runner"), objects)
        self.assertIn(("Namespace", "gitlab-jobs"), objects)
        self.assertEqual(objects[("Role", "gitlab-runner")]["metadata"]["namespace"], "gitlab-jobs")
        binding = objects[("RoleBinding", "gitlab-runner")]
        self.assertEqual(binding["metadata"]["namespace"], "gitlab-jobs")
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "gitlab-runner", "namespace": "gitlab-runner"}])
        job_sa = objects[("ServiceAccount", "gitlab-job")]
        self.assertEqual(job_sa["metadata"]["namespace"], "gitlab-jobs")
        self.assertFalse(job_sa["automountServiceAccountToken"])
        policy = objects[("NetworkPolicy", "gitlab-jobs-network")]
        self.assertEqual(policy["metadata"]["namespace"], "gitlab-jobs")
        self.assertEqual(set(policy["spec"]["policyTypes"]), {"Ingress", "Egress"})
        self.assertNotIn("ingress", policy["spec"])
        manager_policies = [x for x in docs if x["kind"] == "NetworkPolicy" and x["metadata"]["name"].startswith("platform-")]
        self.assertTrue(manager_policies)
        self.assertTrue(all(x["metadata"].get("namespace") == "gitlab-runner" for x in manager_policies))


if __name__ == "__main__":
    unittest.main()
