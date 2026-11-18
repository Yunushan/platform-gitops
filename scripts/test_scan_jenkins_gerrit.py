#!/usr/bin/env python3
"""Check bounded discovery, duplicate handling and uncertain reservation stops."""
import copy
import os
import contextlib
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import scan_jenkins_gerrit as scanner
import trigger_jenkins_gerrit as trigger
import test_trigger_jenkins_gerrit as trigger_fixture


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.fixture = trigger_fixture.TriggerTest(); self.fixture.setUp()
        self.policy = {k: v for k, v in self.fixture.plan.items() if k not in {"change", "patchset", "revision"}}
        self.policy.update(max_changes=10, max_submissions=5)
        self.rows = [self.fixture.change]
    def gerrit(self, path):
        if path.startswith("/a/changes/?"): return copy.deepcopy(self.rows)
        return self.fixture.gerrit(path)

    @unittest.skipUnless(os.name == "posix", "POSIX file locking")
    def test_pollers_share_cycle_lock_without_excluding_each_other_lifetimes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            with mock.patch.object(trigger, "ROOT", root), scanner.cycle_lock(private, ".jenkins-trigger-poller.lock"):
                with scanner.cycle_lock(private, ".jenkins-result-poller.lock"):
                    with scanner.cycle_lock(private, ".jenkins-queue-cycle.lock"):
                        with self.assertRaises(BlockingIOError):
                            with scanner.cycle_lock(private, ".jenkins-queue-cycle.lock"): pass
                    with scanner.cycle_lock(private, ".jenkins-queue-cycle.lock"): pass

    @unittest.skipUnless(os.name == "posix", "POSIX file locking")
    def test_watch_stops_after_cycle_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            policy = root / "policy.json"; policy.write_text(json.dumps(self.policy))
            values = {"jenkins-user": b"jenkins", "gerrit-user": b"reader", "jenkins-token": b"a" * 20,
                      "gerrit-token": b"b" * 20, "ca.crt": b"fixture"}
            argv = ["scan", "--policy", str(policy), "--policy-sha256", scanner.canonical_digest(self.policy),
                    "--input-dir", str(root), "--journal-dir", str(private), "--watch"]
            output = io.StringIO(); errors = io.StringIO()
            with mock.patch.object(trigger, "ROOT", root), mock.patch.object(scanner, "private_input", side_effect=lambda n, **_: values[n]), \
                 mock.patch.object(scanner, "cycle", side_effect=[{"queued": 0}, ValueError("private-error")]) as cycle, \
                 mock.patch.object(scanner.time, "sleep") as sleep, mock.patch.object(sys, "argv", argv), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(scanner.main(), 1)
            self.assertEqual(cycle.call_count, 2); sleep.assert_called_once_with(60)
            self.assertEqual(json.loads(output.getvalue()), {"queued": 0})
            self.assertNotIn("private-error", errors.getvalue())

    def test_first_cycle_and_confirmed_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            with mock.patch.object(trigger, "ROOT", root):
                preflight = scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, self.fixture.post, private)
                self.assertTrue(preflight["read_only"]); self.assertFalse(self.fixture.calls)
                result = scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, self.fixture.post, private, apply=True)
                self.assertEqual(result["queued"], 1)
                again = scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, self.fixture.post, private, apply=True)
                self.assertEqual(again["already_queued"], 1)
                self.assertEqual(len(self.fixture.calls), 1)
                changed = dict(self.policy, job_config_sha256="f" * 64)
                with self.assertRaisesRegex(ValueError, "reserved-job-config-changed"):
                    scanner.cycle(changed, self.fixture.jenkins, self.gerrit, self.fixture.post, private, apply=True)

    def test_uncertain_reservation_stops_future_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            def uncertain(*_): raise TimeoutError("synthetic")
            with mock.patch.object(trigger, "ROOT", root):
                with self.assertRaises(TimeoutError): scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, uncertain, private, apply=True)
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, self.fixture.post, private, apply=True)
                self.assertFalse(self.fixture.calls)

    def test_inventory_bound_and_bad_job_stop_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            self.policy["max_changes"] = 1; self.policy["max_submissions"] = 1
            other = copy.deepcopy(self.rows[0]); other["_number"] = 124
            other["revisions"][self.fixture.sha]["ref"] = "refs/changes/24/124/2"
            self.rows.append(other)
            with mock.patch.object(trigger, "ROOT", root):
                with self.assertRaises(ValueError): scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, self.fixture.post, private, apply=True)
                self.assertFalse(list(private.iterdir())); self.assertFalse(self.fixture.calls)
                self.rows.pop()
                self.fixture.config = b"changed-job"
                with self.assertRaises(ValueError): scanner.cycle(self.policy, self.fixture.jenkins, self.gerrit, self.fixture.post, private, apply=True)
                self.assertFalse(list(private.iterdir())); self.assertFalse(self.fixture.calls)


if __name__ == "__main__": unittest.main()
