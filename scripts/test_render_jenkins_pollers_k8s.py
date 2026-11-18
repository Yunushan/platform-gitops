import copy
import json
import os
from pathlib import Path
import sys
import unittest

import render_jenkins_pollers_k8s as subject


def fixture():
    return {"schema_version": 1, "name": "jenkins-pollers", "namespace": "ci-integration",
            "image": "example.invalid/platform/pollers@sha256:" + "a" * 64, "state_pvc": "poller-state",
            "trigger_secret": "trigger-credentials", "result_secret": "result-credentials",
            "trigger_configmap": "trigger-policy", "result_configmap": "result-policy",
            "trigger_sha256": "b" * 64, "result_sha256": "c" * 64, "interval_seconds": 60,
            "node_selector": {"kubernetes.io/os": "linux", "kubernetes.io/hostname": "worker-a"}}


class KubernetesPollerTest(unittest.TestCase):
    def test_example_validates(self):
        path = Path(__file__).resolve().parents[1] / "examples/jenkins-gerrit-pollers-k8s.json"
        self.assertEqual(subject.render(json.loads(path.read_text()))["kind"], "Pod")

    def test_single_pod_has_separate_inputs_and_persistent_shared_state(self):
        pod = subject.render(fixture()); spec = pod["spec"]
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual(len(spec["containers"]), 2)
        self.assertEqual(spec["initContainers"][0]["name"], "state-check")
        for container in spec["containers"]:
            self.assertIn("--watch", container["command"])
            self.assertIn("--apply", container["command"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            names = {m["name"] for m in container["volumeMounts"]}
            self.assertIn("state", names)
            self.assertIn(container["name"] + "-credentials", names)
            self.assertNotIn(("result" if container["name"] == "trigger" else "trigger") + "-credentials", names)
        self.assertEqual(next(v for v in spec["volumes"] if v["name"] == "state")["persistentVolumeClaim"]["claimName"], "poller-state")

    def test_rejects_unpinned_image_and_shared_credentials(self):
        for key, value in (("image", "example.invalid/poller:latest"), ("namespace", "default"),
                           ("result_secret", "trigger-credentials"), ("interval_seconds", 1)):
            plan = copy.deepcopy(fixture()); plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): subject.render(plan)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("JENKINS_POLLER_API_FIXTURE") == "1", "isolated API fixture not requested")
    def test_restricted_namespace_server_admission(self):
        import test_rook_api_fixture as api_fixture
        pod = subject.render(fixture())
        def check(_client, _root, kubectl):
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": pod["metadata"]["namespace"], "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
                "name": "default", "namespace": pod["metadata"]["namespace"]}})
            admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], pod)
            self.assertEqual(admitted["spec"]["restartPolicy"], "Never")
            self.assertFalse(admitted["spec"]["automountServiceAccountToken"])
            self.assertEqual(len(admitted["spec"]["containers"]), 2)
        api_fixture.exercise(self, management_action=check)


if __name__ == "__main__": unittest.main()
