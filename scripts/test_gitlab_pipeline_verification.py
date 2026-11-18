#!/usr/bin/env python3
"""Fail-closed tests for the read-only GitLab pipeline verifier."""
import copy
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from verify_gitlab_pipeline import requester, validate, verify


class PipelineVerificationTest(unittest.TestCase):
    def test_read_only_request_retries_temporary_server_errors(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.read.side_effect = [b'{"id":7}', b""]
        response.headers = {"X-Next-Page": ""}
        response.__enter__.return_value = response
        failures = [HTTPError("https://gitlab.example/api/v4/projects/7", code, "unavailable", {}, io.BytesIO())
                    for code in (500, 503)]
        with patch("verify_gitlab_pipeline.open_http_request", side_effect=[*failures, response]) as read, \
             patch("verify_gitlab_pipeline.time.sleep"):
            data, headers = requester(self.plan, "fixture-token")("/projects/7")
        self.assertEqual(data, {"id": 7})
        self.assertEqual(headers, {"x-next-page": ""})
        self.assertEqual(read.call_count, 3)
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in read.call_args_list))

    def test_request_retries_have_a_fixed_limit_and_preserve_authentication_errors(self):
        for code, count in ((500, 3), (401, 1), (403, 1), (404, 1), (422, 1)):
            with self.subTest(code=code):
                failures = [HTTPError("https://gitlab.example/api/v4/projects/7", code, "failure", {}, io.BytesIO())
                            for _ in range(count)]
                with patch("verify_gitlab_pipeline.open_http_request", side_effect=failures) as read, \
                     patch("verify_gitlab_pipeline.time.sleep"), self.assertRaises(HTTPError):
                    requester(self.plan, "fixture-token")("/projects/7")
                self.assertEqual(read.call_count, count)

    def setUp(self):
        self.sha = "a" * 40
        self.plan = {"schema_version": 1, "api_url": "https://gitlab.example/api/v4",
                     "project_id": 7, "project_path": "group/project", "branch": "main",
                     "pipeline_id": 23, "revision": self.sha, "required_jobs": ["build", "test"]}
        self.project = {"id": 7, "path_with_namespace": "group/project"}
        self.branch = {"name": "main", "protected": True, "commit": {"id": self.sha}}
        self.pipeline = {"id": 23, "project_id": 7, "sha": self.sha, "ref": "main",
                         "status": "success", "source": "push", "updated_at": "2026-01-01T00:00:00Z"}
        self.jobs = [{"id": 31, "name": "build", "status": "success", "allow_failure": False},
                     {"id": 32, "name": "test", "status": "success", "allow_failure": False}]
        self.details = {j["id"]: dict(j, pipeline={"id": 23}, commit={"id": self.sha}) for j in self.jobs}
        self.calls = {}

    def get(self, path):
        self.calls[path] = self.calls.get(path, 0) + 1
        if path == "/projects/7": return copy.deepcopy(self.project), {}
        if path == "/projects/7/repository/branches/main": return copy.deepcopy(self.branch), {}
        if path == "/projects/7/pipelines/23": return copy.deepcopy(self.pipeline), {}
        if path == "/projects/7/pipelines/23/jobs?per_page=100&page=1&include_retried=false":
            return copy.deepcopy(self.jobs), {"x-next-page": ""}
        if path.startswith("/projects/7/jobs/"): return copy.deepcopy(self.details[int(path.rsplit("/", 1)[1])]), {}
        raise AssertionError(path)

    def test_valid_pipeline_is_read_only_and_does_not_authorize_deployment(self):
        result = verify(self.plan, self.get)
        self.assertTrue(result["verified"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["deployment_authorized"])
        self.assertEqual(result["required_job_ids"], {"build": 31, "test": 32})

    def test_invalid_plan_is_rejected(self):
        for key, value in [("required_jobs", [[]]), ("api_url", "https://x:bad/api/v4"),
                           ("branch", "main/../other"), ("project_id", True),
                           ("revision", "b" * 39)]:
            with self.subTest(key=key):
                plan = dict(self.plan, **{key: value})
                with self.assertRaises(ValueError): validate(plan)

    def test_identity_and_state_mismatch_fail_closed(self):
        for target, key, value in [("project", "id", True), ("branch", "protected", False),
                                   ("pipeline", "source", "schedule"), ("pipeline", "status", "running"),
                                   ("pipeline", "sha", "b" * 40)]:
            with self.subTest(target=target, key=key):
                original = getattr(self, target)[key]
                getattr(self, target)[key] = value
                with self.assertRaises(ValueError): verify(self.plan, self.get)
                getattr(self, target)[key] = original

    def test_missing_failing_optional_and_duplicate_jobs_rejected(self):
        for jobs in [self.jobs[:1], [dict(self.jobs[0], status="failed"), self.jobs[1]],
                     [dict(self.jobs[0], allow_failure=True), self.jobs[1]], self.jobs + self.jobs[:1]]:
            with self.subTest(jobs=jobs):
                original, self.jobs = self.jobs, jobs
                with self.assertRaises(ValueError): verify(self.plan, self.get)
                self.jobs = original

    def test_job_detail_mismatch_rejected(self):
        self.details[31]["commit"] = {"id": "b" * 40}
        with self.assertRaises(ValueError): verify(self.plan, self.get)

    def test_incomplete_pagination_rejected(self):
        def get(path):
            data, headers = self.get(path)
            return (data, {}) if "/jobs?" in path else (data, headers)
        with self.assertRaises(ValueError): verify(self.plan, get)

    def test_job_retry_and_branch_changes_during_check_rejected(self):
        for kind in ("jobs", "branch"):
            self.calls.clear()
            def get(path):
                data, headers = self.get(path)
                if self.calls[path] == 2 and kind == "jobs" and "/jobs?" in path:
                    data[0]["id"] = 999
                if self.calls[path] == 2 and kind == "branch" and "/branches/" in path:
                    data["commit"]["id"] = "b" * 40
                return data, headers
            with self.subTest(kind=kind), self.assertRaises(ValueError): verify(self.plan, get)

    def test_requester_uses_private_header_and_parses_bounded_json(self):
        class Response(io.BytesIO):
            headers = {"X-Next-Page": ""}
        def open_request(request, **kwargs):
            self.assertEqual(request.get_header("Private-token"), "fixture-token")
            self.assertEqual(request.full_url, "https://gitlab.example/api/v4/projects/7")
            self.assertFalse(kwargs["use_environment_proxy"])
            return Response(b'{"id":7}')
        with patch("verify_gitlab_pipeline.open_http_request", side_effect=open_request):
            data, headers = requester(self.plan, "fixture-token")("/projects/7")
        self.assertEqual(data, {"id": 7})
        self.assertEqual(headers["x-next-page"], "")


if __name__ == "__main__":
    unittest.main()
