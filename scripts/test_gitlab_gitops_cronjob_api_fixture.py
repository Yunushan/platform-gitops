#!/usr/bin/env python3
"""Optional disposable Kubernetes API and restricted-pod admission fixture."""
import copy
import os
import sys
import unittest

import test_gitlab_gitops_cronjob as scheduler_fixture
import test_rook_api_fixture as api_fixture
from render_gitlab_gitops_cronjob import render


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("ROOK_BOOTSTRAP_TEST_BIN")
                     and os.environ.get("GITLAB_CRONJOB_API_FIXTURE") == "1", "disposable Kubernetes API fixture not requested")
class CronJobAdmission(unittest.TestCase):
    def test_cronjob_and_restricted_pod_admitted(self):
        fixture = scheduler_fixture.CronJobTest()
        fixture.setUp()
        objects = render(fixture.plan)
        def check(_client, _root, kubectl):
            namespace = fixture.plan["namespace"]
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
            for obj in objects:
                result = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], obj)
                self.assertEqual(result["kind"], obj["kind"])
                kubectl(["create", "-f", "-", "-o", "json"], obj)
            template = objects[1]["spec"]["jobTemplate"]["spec"]["template"]
            pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "handoff-probe", "namespace": namespace},
                   "spec": copy.deepcopy(template["spec"])}
            admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], pod)
            self.assertFalse(admitted["spec"]["automountServiceAccountToken"])
            self.assertTrue(admitted["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        api_fixture.exercise(self, management_action=check)


if __name__ == "__main__": unittest.main()
