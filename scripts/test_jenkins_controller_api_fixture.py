#!/usr/bin/env python3
"""Optional disposable API check for Jenkins controller pod admission."""
import copy
import os
import sys
import unittest

import test_jenkins_controller as controller_fixture
import test_rook_api_fixture as api_fixture
from render_jenkins_controller import render


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("ROOK_BOOTSTRAP_TEST_BIN")
                     and os.environ.get("JENKINS_CONTROLLER_API_FIXTURE") == "1", "disposable API fixture not requested")
class JenkinsAdmission(unittest.TestCase):
    def test_restricted_pod_and_workload_admitted(self):
        fixture = controller_fixture.JenkinsControllerTest(); fixture.setUp()
        objects = render(fixture.plan)
        def check(_client, _root, kubectl):
            namespace = fixture.plan["namespace"]
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
            for obj in objects:
                kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], obj)
                kubectl(["create", "-f", "-", "-o", "json"], obj)
            template = objects[2]["spec"]["template"]
            pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "jenkins-probe", "namespace": namespace},
                   "spec": copy.deepcopy(template["spec"])}
            admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], pod)
            self.assertFalse(admitted["spec"]["automountServiceAccountToken"])
        api_fixture.exercise(self, management_action=check)


if __name__ == "__main__": unittest.main()
