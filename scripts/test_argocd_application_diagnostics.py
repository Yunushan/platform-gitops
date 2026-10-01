#!/usr/bin/env python3
"""Application diagnoses must remain read-only, distinguish status types and redact input."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import diagnose_argocd_applications as diagnostic


def app(name="example", health="Healthy", sync="Synced", **status):
    return {"metadata": {"name": name}, "status": {"health": {"status": health}, "sync": {"status": sync}, **status}}


class ApplicationDiagnosticTest(unittest.TestCase):
    def test_status_types_are_independent(self):
        report = diagnostic.diagnose({"items": [app()]}, [])
        self.assertEqual(report["applications_needing_attention"], 0)
        self.assertEqual(report["applications"][0]["last_operation"], "NotRecorded")
        self.assertEqual(report["health_counts"], {"Healthy": 1})
        report = diagnostic.diagnose({"items": [app(operationState={"phase": "Unknown"})]}, [])
        self.assertEqual(report["applications"][0]["last_operation"], "Unknown")
        self.assertEqual(report["applications_needing_attention"], 0)
        report = diagnostic.diagnose({"items": [app(health="Progressing", sync="OutOfSync", operationState={"phase": "Running"})]}, [])
        self.assertEqual(report["applications_needing_attention"], 1)
        self.assertEqual(report["applications"][0]["last_operation"], "Running")

    def test_redacted_failure_categories(self):
        private = "https://private.example.test/path?access_token=not-for-output"
        example = app("harbor", "Degraded", "OutOfSync", conditions=[
            {"type": "SyncError", "message": f"ingress is invalid harbor.<PLATFORM_DOMAIN>; {private}; updates to statefulset spec are forbidden"},
            {"type": "OrphanedResourceWarning", "message": "Application has orphaned resources"}],
            resources=[{"kind": "Deployment", "name": "private-resource", "health": {"status": "Degraded", "message": "Deployment exceeded its progress deadline"}},
                       {"kind": "PersistentVolumeClaim", "requiresPruning": True}],
            operationState={"phase": "Failed", "message": private})
        report = diagnostic.diagnose({"items": [example]}, [])
        serialized = json.dumps(report)
        for withheld in (private, "access_token", "private-resource", "<PLATFORM_DOMAIN>"):
            self.assertNotIn(withheld, serialized)
        item = report["applications"][0]
        self.assertIn("unresolved_deployment_placeholder", item["failure_categories"])
        self.assertIn("immutable_resource_change", item["failure_categories"])
        self.assertIn("rollout_deadline_exceeded", item["failure_categories"])
        self.assertEqual(item["resources_requiring_prune"], 1)
        self.assertTrue(item["orphan_warning"])

    def test_comparison_errors_missing_state_and_revision(self):
        example = app(conditions=[{"type": "ComparisonError", "message": "private render failed"}])
        report = diagnostic.diagnose({"items": [example]}, [], "a" * 40)
        self.assertIn("manifest_generation_or_comparison", report["applications"][0]["failure_categories"])
        self.assertFalse(report["applications"][0]["revision_matches_expected"])
        report = diagnostic.diagnose({"items": [{"metadata": {"name": "unknown"}}]}, [])
        self.assertEqual(report["applications"][0]["health"], "Unknown")
        for items, wanted in (([], []), ([app()], ["missing"])):
            with self.assertRaises(ValueError):
                diagnostic.diagnose({"items": items}, wanted)

    def test_only_bounded_get_is_executed_and_errors_are_suppressed(self):
        output = io.StringIO()
        with patch.object(diagnostic, "run_bounded", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"items": [app()]}))) as run:
            with redirect_stdout(output):
                self.assertEqual(diagnostic.main([]), 0)
            command = run.call_args.args[0]
            self.assertIn("get", command)
            self.assertIn("applications.argoproj.io", command)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 30)
            self.assertFalse(any(word in command for word in ("patch", "apply", "delete", "sync", "exec")))
        error = io.StringIO()
        with patch.object(diagnostic, "run_bounded", side_effect=RuntimeError("private-secret-token")), redirect_stderr(error):
            self.assertEqual(diagnostic.main([]), 1)
        self.assertNotIn("private-secret-token", error.getvalue())
        with patch.object(diagnostic, "read_bounded_bytes", return_value=json.dumps({"items": [app(sync="OutOfSync")]}).encode()), patch.object(diagnostic, "run_bounded") as run:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(diagnostic.main(["--input", "private/capture.json"]), 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
