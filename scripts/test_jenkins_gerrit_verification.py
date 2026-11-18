#!/usr/bin/env python3
"""Test exact Jenkins build and Gerrit branch comparison."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import verify_jenkins_gerrit as check


class JenkinsGerritTest(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.config = b"reviewed-job-config"
        self.plan = {"schema_version": 1, "jenkins_origin": "https://ci.example", "job": ["team", "build"], "build": 10,
                     "job_config_sha256": hashlib.sha256(self.config).hexdigest(), "gerrit_origin": "https://review.example",
                     "project": "team/project", "branch": "main", "change": 123, "patchset": 2, "revision": self.sha}
        self.build = {"number": 10, "queueId": 17, "building": False, "result": "SUCCESS", "actions": [
            {"parameters": [{"name": "GERRIT_CHANGE_NUMBER", "value": "123"}, {"name": "GERRIT_PATCHSET_NUMBER", "value": "2"},
                            {"name": "GERRIT_PATCHSET_REVISION", "value": self.sha}]},
            {"lastBuiltRevision": {"SHA1": self.sha}, "remoteUrls": ["https://review.example/a/team/project.git"]}]}
        self.change = {"project": "team/project", "branch": "main", "status": "MERGED", "_number": 123,
                       "current_revision": self.sha, "revisions": {self.sha: {"_number": 2, "ref": "refs/changes/23/123/2"}}}
        self.branch = {"ref": "refs/heads/main", "revision": self.sha}

    def jenkins(self, path, json_output=True):
        return copy.deepcopy(self.build) if json_output else self.config

    def gerrit(self, path):
        return copy.deepcopy(self.change if "/changes/" in path else self.branch)

    def test_match_is_read_only_and_does_not_authorize_deployment(self):
        result = check.verify(self.plan, self.jenkins, self.gerrit)
        self.assertTrue(result["jenkins_build_and_gerrit_match"])
        self.assertFalse(result["checkout_provenance_verified"])
        self.assertFalse(result["deployment_authorized"])
        self.assertFalse(result["queue_identity_verified"])
        self.assertTrue(check.verify(self.plan, self.jenkins, self.gerrit, queue_id=17)["queue_identity_verified"])
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit, queue_id=18)
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit, queue_id=True)

    def test_mismatches_fail_closed(self):
        for target, key, value in [("build", "result", "FAILURE"), ("build", "building", True),
                                   ("change", "status", "NEW"), ("branch", "revision", "b" * 40)]:
            with self.subTest(target=target, key=key):
                obj = getattr(self, target); original = obj[key]; obj[key] = value
                with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit)
                obj[key] = original
        self.build["actions"][1]["lastBuiltRevision"]["SHA1"] = "b" * 40
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit)

    def test_duplicate_parameters_and_changed_job_config_rejected(self):
        self.build["actions"][0]["parameters"].append({"name": "GERRIT_CHANGE_NUMBER", "value": "123"})
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit)
        self.build["actions"][0]["parameters"].pop()
        self.config = b"different-config"
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit)

    def test_source_changes_during_check_rejected(self):
        calls = [0]
        def gerrit(path):
            item = self.gerrit(path)
            if "/branches/" in path:
                calls[0] += 1
                if calls[0] == 2: item["revision"] = "b" * 40
            return item
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, gerrit)

    def test_open_patchset_requires_queue_and_stable_target_branch(self):
        self.change["status"] = "NEW"
        self.branch["revision"] = "b" * 40
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, self.gerrit, change_state="NEW")
        result = check.verify(self.plan, self.jenkins, self.gerrit, queue_id=17, change_state="NEW")
        self.assertTrue(result["jenkins_build_and_gerrit_match"])
        self.assertFalse(result["review_vote_authorized"])
        calls = [0]
        def changing(path):
            value = self.gerrit(path)
            if "/branches/" in path:
                calls[0] += 1
                if calls[0] == 2: value["revision"] = "c" * 40
            return value
        with self.assertRaises(ValueError): check.verify(self.plan, self.jenkins, changing, queue_id=17, change_state="NEW")

    def test_queue_reservation_must_match_review_identity_and_acknowledgement(self):
        identity = {k: self.plan[k] for k in ("jenkins_origin", "job", "gerrit_origin", "project", "branch", "change", "patchset", "revision")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = {"schema_version": 1, "state": "queued", "identity_sha256": digest, "queue_id": 17}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(check.queue_id_from_journal(self.plan, path), 17)
            self.assertFalse(check.queue_record_from_journal(self.plan, path)["job_config_bound"])
            version2 = dict(record, schema_version=2, job_config_sha256=self.plan["job_config_sha256"])
            path.write_text(json.dumps(version2), encoding="utf-8")
            self.assertTrue(check.verify(self.plan, self.jenkins, self.gerrit, journal=path)["job_config_reservation_verified"])
            path.write_text(json.dumps(dict(version2, job_config_sha256="c" * 64)), encoding="utf-8")
            with self.assertRaises(ValueError): check.queue_id_from_journal(self.plan, path)
            for changed in (dict(record, state="reserved"), dict(record, identity_sha256="b" * 64), dict(record, queue_id=True)):
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ValueError): check.queue_id_from_journal(self.plan, path)


if __name__ == "__main__": unittest.main()
