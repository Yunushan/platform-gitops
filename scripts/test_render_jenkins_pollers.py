import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import render_jenkins_pollers as subject


def fixture():
    root = "/opt/platform-gitops"
    return {"schema_version": 1, "root": root, "user": "ci_poller", "group": "ci_poller",
            "queue_dir": root + "/private/queue", "review_dir": root + "/private/reviews",
            "trigger_policy": root + "/private/trigger.json", "result_policy": root + "/private/results.json",
            "trigger_inputs": root + "/private/trigger-inputs", "result_inputs": root + "/private/result-inputs",
            "trigger_sha256": "a" * 64, "result_sha256": "b" * 64, "interval_seconds": 60}


class PollerUnits(unittest.TestCase):
    def test_example_matches_validator(self):
        path = Path(__file__).resolve().parents[1] / "examples/jenkins-gerrit-pollers.json"
        self.assertEqual(len(subject.render(json.loads(path.read_text()))), 2)

    @unittest.skipUnless(os.name == "posix" and shutil.which("systemd-analyze"), "systemd validation unavailable")
    def test_units_are_accepted_by_systemd_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, content in subject.render(fixture()).items():
                path = Path(directory) / name; path.write_text(content); paths.append(str(path))
            result = subprocess.run(["systemd-analyze", "verify", *paths], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_units_bind_policy_and_use_no_automatic_restart(self):
        files = subject.render(fixture())
        self.assertEqual(len(files), 2)
        for unit in files.values():
            self.assertIn("Restart=no", unit)
            self.assertIn("--watch --interval-seconds 60 --apply", unit)
            self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("scan_jenkins_gerrit_results.py", files["jenkins-gerrit-results.service"])
        self.assertIn("scan_jenkins_gerrit.py", files["jenkins-gerrit-trigger.service"])

    def test_rejects_injection_and_misplaced_private_paths(self):
        for key, value in (("user", "root"), ("root", "/home/user"), ("queue_dir", "/tmp/queue"),
                           ("result_sha256", "bad"), ("interval_seconds", 1), ("group", "ci\nExecStart=/tmp/x")):
            plan = copy.deepcopy(fixture()); plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): subject.render(plan)
        plan = fixture(); plan["review_dir"] = plan["queue_dir"]
        with self.assertRaises(ValueError): subject.render(plan)

    def test_writable_state_and_credential_directories_do_not_overlap(self):
        for key, value in (("review_dir", "/opt/platform-gitops/private/queue/reviews"),
                           ("trigger_policy", "/opt/platform-gitops/private/queue/policy.json"),
                           ("result_inputs", "/opt/platform-gitops/private/reviews/credentials"),
                           ("result_inputs", "/opt/platform-gitops/private/trigger-inputs/nested")):
            plan = fixture(); plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): subject.render(plan)


if __name__ == "__main__": unittest.main()
