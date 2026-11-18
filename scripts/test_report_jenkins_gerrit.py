import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import report_jenkins_gerrit as subject


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / "private" / "reviews"
        self.directory.mkdir(parents=True, mode=0o700)
        self.plan = {"schema_version": 1, "trigger": {
            "schema_version": 1, "jenkins_origin": "https://jenkins.example.test", "job": ["verify"],
            "job_config_sha256": "a" * 64, "gerrit_origin": "https://gerrit.example.test",
            "project": "demo", "branch": "main", "change": 101, "patchset": 1, "revision": "b" * 40},
            "reviewer_username": "ci-review", "reviewer_account_id": 123,
            "label_definition_sha256": "c" * 64, "trust_jenkins_controller": True}
        self.result = {"status": "matched-success", "build_verified": True, "queue_id": 5, "build": 7,
                       "evidence": {"queue_identity_verified": True, "job_config_reservation_verified": True}}
        self.voted = False
        self.posts = 0
        self.last_payload = None
        self.fail_post = False
        self.patches = [patch.object(subject.trigger, "ROOT", self.root),
                        patch.object(subject.resolver, "resolve", side_effect=lambda *a, **k: self.result),
                        patch.object(subject.reporter, "source_policy"),
                        patch.object(subject.reporter, "review", return_value={}),
                        patch.object(subject.reporter, "published", side_effect=lambda *a: self.voted)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def request(self, path, payload=None):
        if payload is None: return {}
        self.posts += 1
        self.last_payload = payload
        if self.fail_post: raise OSError("unknown outcome")
        self.voted = True

    def run_report(self, apply=False):
        return subject.report(self.plan, "queue.json", self.directory, None, None, self, apply=apply)

    def test_preflight_and_idempotent_vote(self):
        self.assertEqual(self.run_report()["status"], "ready")
        self.assertEqual(self.posts, 0)
        self.assertEqual(self.run_report(True)["status"], "reported")
        self.assertEqual(self.run_report(True)["status"], "already-reported")
        self.assertEqual(self.posts, 1)

    def test_uncertain_write_never_retried(self):
        self.fail_post = True
        with self.assertRaises(OSError): self.run_report(True)
        with self.assertRaisesRegex(ValueError, "uncertain"): self.run_report(True)
        self.assertEqual(self.posts, 1)
        self.voted = True
        self.assertEqual(self.run_report(True)["status"], "already-reported")

    def test_trust_and_bound_evidence_are_mandatory(self):
        self.plan["trust_jenkins_controller"] = False
        with self.assertRaises(ValueError): self.run_report(True)
        self.plan["trust_jenkins_controller"] = True
        self.result["evidence"]["job_config_reservation_verified"] = False
        with self.assertRaises(ValueError): self.run_report(True)
        self.assertEqual(self.posts, 0)

    def test_reviewer_transport_rejects_other_resources_and_votes(self):
        api = subject.ReviewAPI(self.plan, b"fixture-token-123456")
        with self.assertRaisesRegex(ValueError, "outside-policy"):
            api.request("/a/changes/102/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES")
        with self.assertRaisesRegex(ValueError, "outside-policy"):
            api.request("/a/changes/101/revisions/" + "b" * 40 + "/review", {"labels": {"Verified": -1}})

    def test_unreserved_existing_vote_is_not_adopted(self):
        self.voted = True
        with self.assertRaisesRegex(ValueError, "unreserved"):
            self.run_report(True)
        self.assertEqual(self.posts, 0)

    def test_changed_build_evidence_stops_before_reservation(self):
        changed = {**self.result, "build": 8}
        with patch.object(subject.resolver, "resolve", side_effect=[self.result, changed]):
            with self.assertRaisesRegex(ValueError, "evidence-changed"):
                self.run_report(True)
        self.assertEqual(self.posts, 0)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_merged_check_requires_report_receipt_and_never_writes(self):
        item = {"change": 101, "patchset": 1, "revision": "b" * 40,
                "ref": subject.bridge.reference(101, 1)}
        with patch.object(subject.bridge, "current", return_value=item):
            with self.assertRaisesRegex(ValueError, "reservation-required"):
                subject.verify_merged(self.plan, "queue.json", self.directory, None, None, self)
            self.run_report(True)
            result = subject.verify_merged(self.plan, "queue.json", self.directory, None, None, self)
            self.assertTrue(result["review_vote_verified"])
            self.assertFalse(result["deployment_authorized"])
            self.assertEqual((result["revision"], result["queue_id"], result["build"]), ("b" * 40, 5, 7))
            self.assertEqual(result["report_policy_sha256"], subject.reporter.fingerprint(self.plan))
            self.assertEqual(self.posts, 1)
            self.voted = False
            with self.assertRaisesRegex(ValueError, "vote-not-observed"):
                subject.verify_merged(self.plan, "queue.json", self.directory, None, None, self)

    def test_failure_vote_requires_definite_config_bound_failure(self):
        self.result["status"] = "matched-failure"
        with patch.object(subject.resolver, "resolve_failure", return_value=self.result):
            result = subject.report(self.plan, "queue.json", self.directory, None, None, self, apply=True, failure=True)
        self.assertTrue(result["review_vote_verified"])
        self.assertEqual(self.last_payload["labels"], {"Verified": -1})
        self.assertIn("Jenkins failure", self.last_payload["message"])
        self.assertFalse(result["checkout_provenance_verified"])


if __name__ == "__main__": unittest.main()
