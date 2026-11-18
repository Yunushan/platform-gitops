#!/usr/bin/env python3
"""Test queue-to-build resolution without review or deployment writes."""
import json
from pathlib import Path
import tempfile
import unittest

import resolve_jenkins_gerrit_build as resolver
import test_jenkins_gerrit_verification as verifier_fixture
import trigger_jenkins_gerrit as trigger


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.fixture = verifier_fixture.JenkinsGerritTest(); self.fixture.setUp()
        self.fixture.change["status"] = "NEW"
        self.plan = {k: v for k, v in self.fixture.plan.items() if k != "build"}
        self.rows = [{"number": 10, "queueId": 17, "building": False, "result": "SUCCESS"}]
    def jenkins(self, path, json_output=True):
        if "/api/json?tree=builds[" in path: return {"builds": self.rows}
        return self.fixture.jenkins(path, json_output)
    def journal(self, directory):
        path = Path(directory) / "receipt.json"
        record = {"schema_version": 2, "state": "queued", "identity_sha256": trigger.reservation_key(self.plan),
                  "job_config_sha256": self.plan["job_config_sha256"], "queue_id": 17}
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_matching_success_remains_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            result = resolver.resolve(self.plan, self.journal(directory), self.jenkins, self.fixture.gerrit)
            self.assertEqual(result["status"], "matched-success")
            self.assertTrue(result["evidence"]["job_config_reservation_verified"])
            self.assertFalse(result["review_vote_authorized"])
            self.assertFalse(result["deployment_authorized"])

    def test_pending_absent_failed_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.journal(directory)
            for rows, expected in [([], "not-found-in-bounded-history"),
                                   ([{"number": 10, "queueId": 17, "building": True, "result": None}], "running"),
                                   ([{"number": 10, "queueId": 17, "building": False, "result": "FAILURE"}], "terminal-non-success")]:
                self.rows = rows
                result = resolver.resolve(self.plan, path, self.jenkins, self.fixture.gerrit)
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["build_verified"])
            self.rows = [{"number": 10, "queueId": 17}, {"number": 11, "queueId": 17}]
            with self.assertRaises(ValueError): resolver.resolve(self.plan, path, self.jenkins, self.fixture.gerrit)

    def test_failed_build_requires_bound_queue_current_patch_and_matching_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.journal(directory)
            self.rows = [{"number": 10, "queueId": 17, "building": False, "result": "FAILURE"}]
            self.fixture.build["result"] = "FAILURE"
            self.fixture.build["actions"].pop()  # A checkout failure may have no SCM record.
            result = resolver.resolve_failure(self.plan, path, self.jenkins, self.fixture.gerrit)
            self.assertEqual(result["status"], "matched-failure")
            self.assertFalse(result["evidence"]["checkout_provenance_verified"])
            self.assertFalse(result["deployment_authorized"])
            self.fixture.build["actions"][0]["parameters"][0]["value"] = "999"
            with self.assertRaises(ValueError): resolver.resolve_failure(self.plan, path, self.jenkins, self.fixture.gerrit)
            self.fixture.build["actions"][0]["parameters"][0]["value"] = "123"
            self.fixture.change["status"] = "MERGED"
            with self.assertRaises(ValueError): resolver.resolve_failure(self.plan, path, self.jenkins, self.fixture.gerrit)


if __name__ == "__main__": unittest.main()
