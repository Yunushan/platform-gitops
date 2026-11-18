#!/usr/bin/env python3
"""Exercise exact-review Jenkins queue reservation and uncertain writes."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import trigger_jenkins_gerrit as trigger


class TriggerTest(unittest.TestCase):
    def setUp(self):
        self.config = b"reviewed-job"
        self.sha = "a" * 40
        self.plan = {"schema_version": 1, "jenkins_origin": "https://ci.example", "job": ["team", "build"],
                     "job_config_sha256": hashlib.sha256(self.config).hexdigest(), "gerrit_origin": "https://review.example",
                     "project": "team/project", "branch": "main", "change": 123, "patchset": 2, "revision": self.sha}
        self.change = {"project": "team/project", "branch": "main", "status": "NEW", "_number": 123,
                       "current_revision": self.sha, "revisions": {self.sha: {"_number": 2, "ref": "refs/changes/23/123/2"}}}
        self.calls = []

    def jenkins(self, path, json_output=True): return self.config
    def gerrit(self, path): return copy.deepcopy(self.change)
    def post(self, path, params):
        self.calls.append((path, params))
        return 201, "https://ci.example/queue/item/17/"

    def test_preflight_and_one_queue_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            with mock.patch.object(trigger, "ROOT", root):
                result = trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private)
                self.assertTrue(result["preflight_verified"]); self.assertFalse(self.calls)
                result = trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private, apply=True)
                self.assertEqual(result["queue_id"], 17)
                self.assertFalse(result["deployment_authorized"])
                self.assertEqual(self.calls[0][1]["GERRIT_REFSPEC"], "refs/changes/23/123/2")
                journal = next(private.glob("*.json"))
                self.assertEqual(json.loads(journal.read_text())["state"], "queued")
                self.assertEqual(json.loads(journal.read_text())["job_config_sha256"], self.plan["job_config_sha256"])
                with self.assertRaises(FileExistsError): trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private, apply=True)
                self.assertEqual(len(self.calls), 1)

    def test_uncertain_post_remains_reserved_and_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            def uncertain(*_): raise TimeoutError("synthetic")
            with mock.patch.object(trigger, "ROOT", root):
                with self.assertRaises(TimeoutError): trigger.submit(self.plan, self.jenkins, self.gerrit, uncertain, private, apply=True)
                self.assertEqual(json.loads(next(private.glob("*.json")).read_text())["state"], "reserved")
                with self.assertRaises(FileExistsError): trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private, apply=True)
                self.assertFalse(self.calls)

    def test_acknowledged_post_with_failed_journal_update_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            with mock.patch.object(trigger, "ROOT", root):
                with mock.patch.object(trigger, "atomic_write_text", side_effect=OSError("disk error")):
                    with self.assertRaises(OSError):
                        trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private, apply=True)
                self.assertEqual(json.loads(next(private.glob("*.json")).read_text())["state"], "reserved")
                with self.assertRaises(FileExistsError):
                    trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private, apply=True)
                self.assertEqual(len(self.calls), 1)

    def test_concurrent_submissions_make_at_most_one_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            gate = threading.Barrier(2)
            results = []
            def jenkins(path, json_output=True):
                if not getattr(threading.current_thread(), "checked", False):
                    threading.current_thread().checked = True
                    gate.wait(timeout=5)
                return self.config
            def submit():
                try:
                    trigger.submit(self.plan, jenkins, self.gerrit, self.post, private, apply=True)
                    results.append("queued")
                except FileExistsError:
                    results.append("reserved")
            with mock.patch.object(trigger, "ROOT", root):
                threads = [threading.Thread(target=submit) for _ in range(2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertCountEqual(results, ["queued", "reserved"])
            self.assertEqual(len(self.calls), 1)

    def test_stale_change_and_untrusted_queue_location_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            with mock.patch.object(trigger, "ROOT", root):
                self.change["status"] = "MERGED"
                with self.assertRaises(ValueError): trigger.submit(self.plan, self.jenkins, self.gerrit, self.post, private, apply=True)
                self.assertFalse(list(private.iterdir()))
                self.change["status"] = "NEW"
                with self.assertRaises(ValueError):
                    trigger.submit(self.plan, self.jenkins, self.gerrit, lambda *_: (201, "https://other.example/queue/item/17/"), private, apply=True)
                self.assertEqual(json.loads(next(private.glob("*.json")).read_text())["state"], "reserved")

    def test_change_after_reservation_does_not_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); private = root / "private"; private.mkdir(mode=0o700)
            seen = [0]
            def changing(path):
                seen[0] += 1
                value = self.gerrit(path)
                if seen[0] == 2: value["status"] = "MERGED"
                return value
            with mock.patch.object(trigger, "ROOT", root):
                with self.assertRaises(ValueError): trigger.submit(self.plan, self.jenkins, changing, self.post, private, apply=True)
                self.assertFalse(self.calls)
                self.assertEqual(json.loads(next(private.glob("*.json")).read_text())["state"], "reserved")


if __name__ == "__main__": unittest.main()
