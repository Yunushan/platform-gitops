import tempfile
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import scan_jenkins_gerrit_results as subject
import test_report_jenkins_gerrit as reporter_fixture


class ResultScanTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX file locking")
    def test_only_one_result_poller_can_hold_the_queue_lock(self):
        fixture = reporter_fixture.ReporterTests(); fixture.setUp()
        try:
            with subject.cycle_lock(fixture.directory):
                with self.assertRaises(BlockingIOError):
                    with subject.cycle_lock(fixture.directory): pass
            with subject.cycle_lock(fixture.directory): pass
        finally:
            fixture.tearDown()

    def test_synthetic_policy_validates(self):
        path = Path(__file__).resolve().parents[1] / "examples/jenkins-gerrit-results-scan.json"
        subject.validate(json.loads(path.read_text(encoding="utf-8")))

    def test_failure_reporting_is_opt_in_and_excludes_aborted_builds(self):
        fixture = reporter_fixture.ReporterTests(); fixture.setUp()
        try:
            trigger = fixture.plan["trigger"]
            scan = {k: v for k, v in trigger.items() if k not in {"change", "patchset", "revision"}}
            scan.update(max_changes=3, max_submissions=2)
            policy = {k: fixture.plan[k] for k in ("schema_version", "reviewer_username", "reviewer_account_id", "label_definition_sha256", "trust_jenkins_controller")}
            policy.update(scan=scan, max_reports=1, report_failures=True)
            item = {"change": trigger["change"], "patchset": trigger["patchset"], "revision": trigger["revision"]}
            result = {"status": "terminal-non-success", "jenkins_result": "FAILURE"}
            with patch.object(subject.bridge, "discover", return_value=[item]), patch.object(subject.scanner, "journal_state", return_value="queued"), \
                 patch.object(subject.resolver, "resolve", return_value=result), patch.object(subject.reporter, "report", return_value={"status": "ready"}) as report:
                subject.cycle(policy, fixture.directory, fixture.directory, None, None, lambda _: object())
                self.assertTrue(report.call_args.kwargs["failure"])
                result["jenkins_result"] = "ABORTED"
                subject.cycle(policy, fixture.directory, fixture.directory, None, None, lambda _: object())
                self.assertEqual(report.call_count, 1)
        finally: fixture.tearDown()

    def test_bounded_preflight_and_apply(self):
        fixture = reporter_fixture.ReporterTests(); fixture.setUp()
        try:
            trigger = fixture.plan["trigger"]
            scan = {k: v for k, v in trigger.items() if k not in {"change", "patchset", "revision"}}
            scan.update(max_changes=3, max_submissions=2)
            policy = {k: fixture.plan[k] for k in ("schema_version", "reviewer_username", "reviewer_account_id", "label_definition_sha256", "trust_jenkins_controller")}
            policy.update(scan=scan, max_reports=1)
            item = {"change": trigger["change"], "patchset": trigger["patchset"], "revision": trigger["revision"]}
            calls = []
            def report(*args, **kwargs):
                calls.append(kwargs.get("apply", False))
                return {"status": "reported" if kwargs.get("apply") else "ready"}
            with patch.object(subject.bridge, "discover", return_value=[item]), patch.object(subject.scanner, "journal_state", return_value="queued"), \
                 patch.object(subject.resolver, "resolve", return_value={"status": "matched-success"}), patch.object(subject.reporter, "report", side_effect=report):
                result = subject.cycle(policy, fixture.directory, fixture.directory, None, None, lambda _: object(), apply=True)
            self.assertEqual(result["reported"], 1)
            self.assertEqual(calls, [False, True])
            self.assertFalse(result["deployment_authorized"])
            with patch.object(subject.bridge, "discover", return_value=[item, {**item, "change": 102}]), \
                 patch.object(subject.scanner, "journal_state", return_value="queued"), \
                 patch.object(subject.resolver, "resolve", return_value={"status": "matched-success"}), \
                 patch.object(subject.reporter, "report", side_effect=report):
                with self.assertRaisesRegex(ValueError, "bound-exceeded"):
                    subject.cycle(policy, fixture.directory, fixture.directory, None, None, lambda _: object(), apply=True)
            self.assertEqual(calls, [False, True, False, False])
        finally:
            fixture.tearDown()


if __name__ == "__main__": unittest.main()
