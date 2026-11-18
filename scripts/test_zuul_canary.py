#!/usr/bin/env python3
"""Exercise canary scope, worker execution and non-submitting review reports."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import render_zuul_canary as canary
from test_nodepool_static import plans as worker_plans


def plans():
    workers, ci = worker_plans()
    ci["executor"] = {"enabled": True, "image": ci["image"], "replicas": 2,
        "kubernetes_seccomp": "runtime-default", "kubernetes_apparmor": "runtime-default",
        "docker_seccomp": "runtime-default", "docker_apparmor": "runtime-default"}
    ci["tenants"][0]["untrusted_projects"].append("platform/ci-canary")
    selection = {"schema_version": 1, "tenant": "platform", "config_project": "platform/ci-config",
        "canary_project": "platform/ci-canary", "canary_branch": "main", "worker_label": "platform-linux"}
    return selection, ci, workers


class CanaryTests(unittest.TestCase):
    def test_canary_reports_messages_without_casting_votes_or_submitting(self):
        selection, ci, workers = plans()
        objects = yaml.safe_load(canary.job_configuration(selection, ci, workers))
        pipelines = [item["pipeline"] for item in objects if "pipeline" in item]
        self.assertEqual(len(pipelines), 1)
        pipeline = pipelines[0]
        self.assertEqual(pipeline["manager"], "independent")
        self.assertFalse(pipeline["post-review"])
        self.assertNotIn(pipeline["name"], {"check", "gate", "post", "deploy"})
        self.assertEqual(pipeline["require"], {"gerrit": {"open": True, "current-patchset": True}})
        for action in ("start", "success", "failure"):
            self.assertEqual(pipeline[action], {"gerrit": {"comment": True, "notify": "NONE"}})
            self.assertIn("application code is not tested", pipeline[action + "-message"])
        # No other action may sneak in a submit, a review label or notifications.
        self.assertEqual(set(pipeline), {"name", "manager", "precedence", "post-review", "description",
            "require", "trigger", "start-message", "success-message", "failure-message", "start", "success", "failure"})

    def test_exact_project_scope_and_worker_allocation_cannot_be_confused_with_executor_only_job(self):
        selection, ci, workers = plans()
        objects = yaml.safe_load(canary.job_configuration(selection, ci, workers))
        job = next(item["job"] for item in objects if "job" in item)
        project = next(item["project"] for item in objects if "project" in item)
        self.assertEqual(project["name"], "review.example.com/platform/ci-canary")
        self.assertEqual(job["allowed-projects"], [project["name"]])
        self.assertIsNone(job["parent"])
        self.assertTrue(job["final"])
        self.assertTrue(job["protected"])
        self.assertEqual(job["nodeset"], {"nodes": [{"name": "canary", "label": "platform-linux"}]})
        self.assertEqual(project[canary.PIPELINE]["jobs"], [job["name"]])
        self.assertLessEqual(job["timeout"], 300)
        semaphore = next(item["semaphore"] for item in objects if "semaphore" in item)
        self.assertEqual(semaphore["max"], 1)
        self.assertEqual(job["semaphores"], [semaphore["name"]])

    def test_branch_and_comment_filters_handle_gerrit_prefixes_without_triggering_on_reports(self):
        selection, ci, workers = plans()
        selection["canary_branch"] = "canary/release.1"
        pipeline = yaml.safe_load(canary.job_configuration(selection, ci, workers))[0]["pipeline"]
        for trigger in pipeline["trigger"]["gerrit"]:
            self.assertTrue(re.fullmatch(trigger["branch"], "canary/release.1"))
            for other in ("main", "canary/releaseX1", "prefixcanary/release.1", "canary/release.1-more"):
                self.assertFalse(re.fullmatch(trigger["branch"], other))
        regex = next(item["comment"] for item in pipeline["trigger"]["gerrit"] if item["event"] == "comment-added")
        for comment in ("platform-canary recheck", "Patch Set 7:\n\nplatform-canary recheck\n", "platform-canary recheck \t"):
            self.assertTrue(re.search(regex, comment))
        for comment in (pipeline["success-message"], "recheck", "please platform-canary recheck", "platform-canary recheck later"):
            self.assertFalse(re.search(regex, comment))

    def test_cross_tenant_and_trust_category_confusion_is_rejected(self):
        for field, wrong in (("tenant", "missing"), ("config_project", "platform/ci-canary"),
                             ("canary_project", "platform/ci-config"), ("canary_project", ".*"),
                             ("worker_label", "another-tenant-linux")):
            selection, ci, workers = plans()
            selection[field] = wrong
            with self.subTest(field=field, wrong=wrong), self.assertRaises(ValueError):
                canary.validate(selection, ci, workers)
        selection, ci, workers = plans()
        second = copy.deepcopy(ci["tenants"][0])
        second.update(name="another-tenant", allowed_labels=["another-linux"])
        ci["tenants"].append(second)
        another = copy.deepcopy(workers["pools"][0])
        another.update(name="another-pool", tenant="another-tenant", label="another-linux")
        for index, node in enumerate(another["nodes"]):
            node["host"] = f"another-worker-{index}.example.com"
        workers["pools"].append(another)
        with self.assertRaises(ValueError):
            canary.validate(selection, ci, workers)
        second["config_projects"] = ["another/config"]
        self.assertIs(canary.validate(selection, ci, workers), selection)
        selection["worker_label"] = "another-linux"
        with self.assertRaises(ValueError):
            canary.validate(selection, ci, workers)

    def test_unknown_fields_invalid_branches_and_implicit_executors_fail(self):
        for field, value in (("schema_version", True), ("canary_branch", "main\n.*"), ("canary_branch", "release.lock"),
                             ("canary_branch", "../main"), ("worker_label", []), ("extra", {"submit": True})):
            selection, ci, workers = plans()
            selection[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                canary.validate(selection, ci, workers)
        for executor in (None, {"enabled": False}):
            selection, ci, workers = plans()
            if executor is None:
                del ci["executor"]
            else:
                ci["executor"] = executor
            with self.assertRaises(ValueError):
                canary.validate(selection, ci, workers)

    def test_generated_playbook_targets_allocated_host_and_only_runs_fixed_probe(self):
        play = yaml.safe_load(canary.playbook())[0]
        self.assertEqual(play["hosts"], "canary")
        self.assertFalse(play["gather_facts"])
        self.assertFalse(play["become"])
        commands = [task for task in play["tasks"] if "ansible.builtin.command" in task]
        self.assertEqual(len(commands), 1)
        argv = commands[0]["ansible.builtin.command"]["argv"]
        self.assertEqual(argv, ["/usr/bin/python3", "-c", canary.PROBE, "{{ zuul.build }}"])
        self.assertFalse(commands[0]["changed_when"])
        for task in play["tasks"]:
            self.assertFalse(set(task) & {"delegate_to", "local_action", "shell", "ansible.builtin.shell", "become"})
        # Execute the actual probe with controlled worker identities; no worker,
        # SSH session, service or shell is started by this local unit test.
        build = "0123456789abcdef" * 2
        with mock.patch.object(os, "geteuid", return_value=1000, create=True), \
                mock.patch.object(sys, "argv", ["-c", build]), contextlib.redirect_stdout(io.StringIO()) as stdout:
            exec(compile(argv[2], "<canary-probe>", "exec"), {})
        self.assertEqual(json.loads(stdout.getvalue()), {"schema_version": 1, "component": "platform-zuul-canary",
                                                       "build": build, "unprivileged": True})
        for user, argument in ((0, build), (1000, "not-a-build-uuid"), (1000, build.upper())):
            with mock.patch.object(os, "geteuid", return_value=user, create=True), \
                    mock.patch.object(sys, "argv", ["-c", argument]), self.assertRaises((SystemExit, ValueError)):
                exec(compile(argv[2], "<canary-probe>", "exec"), {})

    def test_all_five_runtimes_share_job_semantics_without_claiming_acceptance(self):
        selection, ci, workers = plans()
        reference = None
        for runtime in canary.RUNTIMES:
            files = canary.render(selection, ci, workers, runtime)
            if reference is None:
                reference = files["zuul.yaml"]
            self.assertEqual(files["zuul.yaml"], reference)
            status = json.loads(files["render-status.json"])
            for field in ("published", "triggered", "live_verified", "application_ci_verified", "ha_verified",
                          "automatic_submission", "review_votes"):
                self.assertIs(status[field], False)
            for name, digest in status["files_sha256"].items():
                self.assertEqual(hashlib.sha256(files[name].encode()).hexdigest(), digest)
            self.assertEqual(status["plan_digests"]["zuul"], canary.digest(ci))
        with self.assertRaises(ValueError):
            canary.render(selection, ci, workers, "unknown")

    def test_cli_keeps_private_values_local_and_refuses_overwrite(self):
        selection, ci, workers = plans()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("canary.json", "ci.json", "workers.json")]
            for path, content in zip(paths, (selection, ci, workers)):
                path.write_text(json.dumps(content), encoding="utf-8")
            output = root / "private" / "canary"
            args = ["--plan", str(paths[0]), "--zuul-plan", str(paths[1]), "--nodepool-plan", str(paths[2]),
                    "--runtime", "docker", "--output-dir", str(output)]
            with mock.patch.object(canary, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(canary.main(args), 0)
            self.assertNotIn("example.com", stdout.getvalue())
            self.assertNotIn(selection["canary_project"], stdout.getvalue())
            self.assertFalse(json.loads(stdout.getvalue())["triggered"])
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            with mock.patch.object(canary, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(canary.main(args), 1)
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)
            for kind, destination in (("outside", root / "public"), ("existing", root / "private")):
                with self.subTest(kind=kind), mock.patch.object(canary, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(canary.main(args[:-1] + [str(destination)]), 1)

    def test_duplicate_json_fields_do_not_select_a_different_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "duplicate.json"
            path.write_text('{"tenant":"one","tenant":"two"}', encoding="utf-8")
            args = ["--plan", str(path), "--zuul-plan", str(path), "--nodepool-plan", str(path),
                    "--runtime", "raw", "--output-dir", str(root / "private" / "new")]
            with mock.patch.object(canary, "ROOT", root), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(canary.main(args), 1)
            self.assertNotIn("two", stderr.getvalue())
            self.assertFalse((root / "private" / "new").exists())


if __name__ == "__main__":
    unittest.main()
