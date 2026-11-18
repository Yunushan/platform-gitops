#!/usr/bin/env python3
"""Optional real Jenkins-to-real Gerrit check in a disposable namespace."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import tekton_gerrit_bridge as bridge
import test_gerrit_review_runtime_fixture as gerrit
import test_jenkins_native_runtime_fixture as jenkins


def child():
    root = Path(__file__).resolve().parents[1]
    os.environ["GERRIT_REVIEW_TEST_ARTIFACTS"] = str(root / "private/validation/gerrit/3.13.10")
    archive = root / "private/validation/keycloak/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz"
    os.environ["GERRIT_REVIEW_TEST_JAVA_ARCHIVE"] = str(archive)
    war = root / "private/validation/jenkins/2.580.2/jenkins.war"
    with gerrit.server(active_processors=2, handoff=True) as context:
        prepared = gerrit.prepare(context, fast_forward_only=True, handoff=True)
        api = context["api"]; number = prepared["change_number"]
        detail = api.call("ci-read", "/a/changes/" + str(number) + "?o=CURRENT_REVISION")
        source = {"source": {"project": prepared["project"], "branch": "main"}}
        item = bridge.current(source, detail)
        jenkins.child(war, archive, real_gerrit=(context, prepared, item))


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0 and os.environ.get("JENKINS_REAL_GERRIT_FIXTURE") == "1",
                     "root-owned disposable network namespace fixture not requested")
class RealJenkinsGerrit(unittest.TestCase):
    def test_real_jenkins_git_and_merged_gerrit(self):
        result = subprocess.run(["unshare", "--net", sys.executable, "-B", __file__, "--child"], capture_output=True,
                                text=True, timeout=600, check=True)
        reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        matches = [report for report in reports if report.get("jenkins_version") == "2.580.2"]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]["real_git_checkout_record_matched"])
        self.assertTrue(matches[0]["gerrit_verified"])
        self.assertTrue(matches[0]["open_patchset_queued_once"])
        self.assertTrue(matches[0]["queue_identity_verified"])
        self.assertTrue(matches[0]["job_config_reservation_verified"])
        self.assertTrue(matches[0]["multiple_patchsets_verified"])
        self.assertTrue(matches[0]["generated_job_verified"])
        self.assertTrue(matches[0]["open_change_build_compared"])
        self.assertTrue(matches[0]["queue_to_build_resolved"])
        self.assertTrue(matches[0]["jenkins_real_gerrit_git_handoff_verified"])
        self.assertTrue(matches[0]["jenkins_failed_build_vote_verified"])
        if os.environ.get("JENKINS_ARGOCD_FIXTURE") == "1":
            self.assertTrue(matches[0]["jenkins_argocd_reconciliation_verified"])
        self.assertFalse(matches[0]["full_stack_verified"])


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--child": child()
    else: unittest.main()
