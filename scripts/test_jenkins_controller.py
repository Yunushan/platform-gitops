#!/usr/bin/env python3
"""Check single-active Jenkins controller rendering."""
import copy
import unittest

from render_jenkins_controller import render


class JenkinsControllerTest(unittest.TestCase):
    def setUp(self):
        self.plan = {"schema_version": 1, "name": "jenkins", "namespace": "ci-system",
                     "image": "jenkins/jenkins@sha256:" + "a" * 64, "home_pvc": "jenkins-home",
                     "node_selector": {"kubernetes.io/os": "linux"}}

    def test_single_active_persistent_controller(self):
        account, service, deployment = render(self.plan)
        self.assertFalse(account["automountServiceAccountToken"])
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "jenkins-home")

    def test_invalid_inputs_are_rejected(self):
        for key, value in [("image", "jenkins/jenkins:lts"), ("namespace", "default"),
                           ("node_selector", {"kubernetes.io/os": "windows"}), ("name", "Invalid"),
                           ("schema_version", True)]:
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan); plan[key] = value
                with self.assertRaises(ValueError): render(plan)


if __name__ == "__main__": unittest.main()
