#!/usr/bin/env python3
"""Trusted publisher scope, versioned source witnesses and credential custody."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

import yaml

import render_zuul_static_image as renderer
from test_harbor_static_image import plan_fixture
from test_zuul_canary import plans as canary_plans
import zuul_static_image as worker


def fixture(root):
    registry, credential = plan_fixture(root)
    _, ci, _ = canary_plans()
    plan = {"schema_version": 1, "tenant": "platform", "config_project": "platform/ci-config",
        "project": "applications/example", "branch": "main", "pipeline": "platform-static-post", "job": "platform-static-publish",
        "secret": "private-static-publisher", "source": {"clone_url": "https://review.example.com/a/applications/example.git",
            "read_username": "source-read", "ca_sha256": hashlib.sha256(b"synthetic-source-ca").hexdigest(),
            "checkout_program_sha256": worker.source_binding.checkout_program_sha256()},
        "static_directory": "site", "registry": {name: registry[name] for name in worker.REGISTRY_FIELDS.split()}}
    event = {"build_uuid": "a" * 32, "project": plan["project"], "pipeline": plan["pipeline"], "branch": "main", "revision": "e" * 40}
    return plan, ci, event, credential


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan, self.ci, self.event, self.credential = fixture(self.root)

    def test_all_hosting_runtimes_prepare_one_final_trusted_post_review_job(self):
        for runtime in renderer.RUNTIMES:
            files = renderer.render(self.plan, self.ci, runtime)
            objects = yaml.safe_load(files["zuul.yaml"])
            pipeline, job, project = objects[0]["pipeline"], objects[1]["job"], objects[2]["project"]
            self.assertIs(pipeline["post-review"], True)
            self.assertIs(job["post-review"], True)
            self.assertIs(job["final"], True)
            self.assertIs(job["protected"], True)
            self.assertIsNone(job["parent"])
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(job["nodeset"], {"nodes": []})
            self.assertEqual(job["allowed-projects"], ["review.example.com/applications/example"])
            self.assertEqual(job["secrets"], [{"name": "platform_publication", "secret": self.plan["secret"], "pass-to-parent": False}])
            self.assertEqual(project[self.plan["pipeline"]]["jobs"], [self.plan["job"]])
            self.assertNotIn("success", pipeline)
            self.assertNotIn("submit", json.dumps(objects))
            status = json.loads(files["render-status.json"])
            for key in ("published", "artifact_publication_verified", "application_ci_verified", "deployment_verified", "ha_verified", "full_stack_verified"):
                self.assertIs(status[key], False)
            self.assertNotIn(self.credential["secret"], "\n".join(files.values()))

    def test_secrets_use_stdin_without_remote_tasks_or_caller_selected_cleanup_paths(self):
        files = renderer.render(self.plan, self.ci, "raw")
        play = yaml.safe_load(files[renderer.PLAYBOOK])[0]
        self.assertEqual(play["hosts"], "localhost")
        self.assertEqual(play["connection"], "local")
        self.assertFalse(play["become"])
        self.assertFalse(play["gather_facts"])
        self.assertEqual(len(play["tasks"]), 5)
        inspection = play["tasks"][0]["ansible.builtin.command"]
        self.assertEqual(inspection["argv"][-1], "--check-contract")
        self.assertNotIn("platform_publication", json.dumps(inspection))
        task = play["tasks"][1]
        self.assertTrue(task["no_log"])
        self.assertIs(task["failed_when"], False)
        command = task["ansible.builtin.command"]
        self.assertIn("--input-stdin", command["argv"])
        self.assertNotIn("--input-dir", command["argv"])
        self.assertNotIn("platform_publication.source_token", " ".join(command["argv"]))
        self.assertIn("platform_publication.source_token", command["stdin"])
        self.assertFalse(command["stdin_add_newline"])
        self.assertNotIn("ansible.builtin.file", json.dumps(play))
        diagnostic = play["tasks"][2]
        self.assertEqual(diagnostic["when"], "platform_publication_result.rc | default(1) != 0")
        self.assertIn("platform_publication_result.rc", diagnostic["ansible.builtin.fail"]["msg"])
        self.assertNotIn("stdout", diagnostic["ansible.builtin.fail"]["msg"])
        self.assertNotIn("stderr", diagnostic["ansible.builtin.fail"]["msg"])
        reader = play["tasks"][3]["ansible.builtin.command"]
        self.assertEqual(reader["argv"][-1], "--read-receipt")
        self.assertNotIn("stdin", reader)
        self.assertTrue(play["tasks"][4]["no_log"])

    def test_contract_only_cli_requires_no_private_inputs_and_prints_only_fixed_diagnostics(self):
        output, errors = StringIO(), StringIO()
        with mock.patch.object(worker, "load_contract", return_value={"plan": self.plan}), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(worker.main(["--contract", "contract.json", "--contract-sha256", "f" * 64, "--check-contract"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"contract_verified": True, "artifact_publication_verified": False, "full_stack_verified": False})
        with mock.patch.object(worker, "load_contract", side_effect=ValueError("private-secret-that-must-not-escape")), redirect_stderr(errors):
            self.assertEqual(worker.main(["--contract", "contract.json", "--contract-sha256", "f" * 64, "--check-contract"]), worker.EXIT_STAGE["contract"])
        self.assertNotIn("private-secret", errors.getvalue())
        self.assertIn("stage=contract;", errors.getvalue())

    def test_unrelated_tenant_config_project_source_and_executor_are_rejected(self):
        for change in (lambda p, c: p.update(project="other/project"),
                       lambda p, c: p["source"].update(checkout_program_sha256="0" * 64),
                       lambda p, c: p.update(config_project=p["project"]),
                       lambda p, c: p["source"].update(clone_url="https://other.example.com/a/applications/example.git"),
                       lambda p, c: c["executor"].update(enabled=False),
                       lambda p, c: c["tenants"].append({**copy.deepcopy(c["tenants"][0]), "name": "other"})):
            plan, ci = copy.deepcopy(self.plan), copy.deepcopy(self.ci)
            change(plan, ci)
            with self.assertRaises(ValueError):
                renderer.render(plan, ci, "raw")

    def test_contract_pins_every_program_and_runs_only_its_original_entrypoint(self):
        files = renderer.render(self.plan, self.ci, "raw")
        package = self.root / "package"
        for name, value in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8", newline="\n")
        contract_path = package / renderer.PACKAGE / "contract.json"
        digest = json.loads(files["render-status.json"])["contract_sha256"]
        with self.assertRaises(ValueError):
            worker.load_contract(contract_path, digest)
        with mock.patch.object(worker, "__file__", str(contract_path.parent / "programs/zuul_static_image.py")):
            self.assertEqual(worker.load_contract(contract_path, digest)["plan"], self.plan)
            (contract_path.parent / "programs/tekton_checkout.py").write_text("changed")
            with self.assertRaises(ValueError):
                worker.load_contract(contract_path, digest)

    def test_wrong_zuul_event_is_rejected_without_inputs_or_publication(self):
        for key, value in (("build_uuid", "../../elsewhere"), ("project", "other/app"), ("branch", "topic"),
                           ("pipeline", "check"), ("revision", "0" * 40), ("revision", "main")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.validate_event(self.plan, {**self.event, key: value})
        with self.assertRaises(ValueError):
            worker.validate_event(self.plan, {**self.event, "extra": True})

    def test_artifact_metadata_binds_the_original_build_contract_source_and_registry(self):
        files = renderer.render(self.plan, self.ci, "raw")
        contract = json.loads(files[renderer.PACKAGE + "/contract.json"])
        source = worker.source_plan(self.plan, self.event["revision"])
        receipt = {"schema_version": 1, "image": "registry.example.invalid/fixture/ha@sha256:" + "1" * 64,
            "manifest_digest": "sha256:" + "1" * 64, "config_digest": "sha256:" + "2" * 64, "output_layer_digest": "sha256:" + "3" * 64,
            "output_tree_sha256": "4" * 64, "base_digest": self.plan["registry"]["base_digest"], "source_revision": self.event["revision"],
            "artifact_publication_verified": True, "publisher_signature_verified": False, "deployment_verified": False, "ha_verified": False,
            "full_stack_verified": False, "source_checkout_witness_bound": True,
            "checkout_witness_sha256": hashlib.sha256(worker.source_binding.witness_bytes(source)).hexdigest(), "source_plan_sha256": worker.fingerprint(source)}
        artifact = {"name": worker.ARTIFACT, "url": "https://registry.example.invalid/v2/fixture/ha/manifests/" + receipt["manifest_digest"],
            "metadata": {"schema_version": 1, "producer": "platform-zuul-static-image", "build_uuid": self.event["build_uuid"],
                "contract_sha256": worker.fingerprint(contract), "receipt": receipt}}
        self.assertEqual(worker.verify_artifact(contract, self.event, [artifact]), receipt)
        changes = (lambda a: a["metadata"].update(build_uuid="b" * 32),
                   lambda a: a["metadata"].update(contract_sha256="0" * 64),
                   lambda a: a.update(url="https://other.example.invalid/image"),
                   lambda a: a["metadata"]["receipt"].update(source_revision="a" * 40),
                   lambda a: a["metadata"]["receipt"].update(checkout_witness_sha256="0" * 64),
                   lambda a: a["metadata"]["receipt"].update(base_digest="sha256:" + "0" * 64),
                   lambda a: a["metadata"]["receipt"].update(image="registry.example.invalid/other/image@sha256:" + "1" * 64),
                   lambda a: a["metadata"]["receipt"].update(publisher_signature_verified=True),
                   lambda a: a["metadata"]["receipt"].update(artifact_publication_verified=1),
                   lambda a: a["metadata"].update(extra=True))
        for change in changes:
            current = copy.deepcopy(artifact); change(current)
            with self.subTest(change=change.__code__.co_firstlineno), self.assertRaises(ValueError):
                worker.verify_artifact(contract, self.event, [current])
        with self.assertRaises(ValueError):
            worker.verify_artifact(contract, self.event, [artifact, copy.deepcopy(artifact)])

    @unittest.skipUnless(os.environ.get("ZUUL_STATIC_GIT_TEST") == "1", "Disposable TLS Git transport not selected")
    def test_actual_tls_branch_checkout(self):
        from test_zuul_static_image_runtime_fixture import exercise
        self.assertTrue(exercise(self)["git_tls_branch_checkout_verified"])

    @unittest.skipUnless(os.environ.get("ZUUL_STATIC_HARBOR_TEST_RUNTIME") == "1", "Disposable native Harbor publication not selected")
    def test_native_harbor_publication_cli(self):
        from test_zuul_static_image_harbor_fixture import exercise
        self.assertTrue(exercise(self)["valkey_gateway_zuul_static_nonroot_cli_publication_verified"])

    @unittest.skipUnless(os.environ.get("ZUUL_STATIC_JOB_TEST_RUNTIME") == "1", "Explicit native encrypted Zuul/Harbor job")
    def test_native_encrypted_zuul_harbor_job(self):
        from test_zuul_static_image_job_fixture import exercise
        self.assertTrue(exercise(self)["valkey_gateway_zuul_static_actual_encrypted_job_executed_verified"])

    def test_native_job_hook_requires_explicit_separate_profile(self):
        import test_zuul_web_fixture as web
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                web.exercise(self, integrated=True, executor_startup=True, real_job=True, static_publication=lambda *args: None)
        with mock.patch.dict(os.environ, {"ZUUL_STATIC_JOB_TEST_RUNTIME": "1"}):
            with self.assertRaises(ValueError):
                web.exercise(self, static_publication=lambda *args: None)
            with self.assertRaises(ValueError):
                web.exercise(self, integrated=True, executor_startup=True, real_job=True,
                             post_controllers=("flux",), static_publication=lambda *args: None)

    @unittest.skipUnless(os.name == "posix", "Original Linux input ownership")
    def test_private_payload_is_ephemeral_and_credentials_are_excluded_from_output(self):
        payload = {"source_ca": "synthetic-source-ca", "source_token": "synthetic-read-credential-1234567890",
                   "registry_ca": "synthetic-public-ca", "registry_credential": worker.publisher.canonical(self.credential).decode()}
        observed = []
        def run(contract, event, inputs):
            worker.private_inputs(inputs)
            observed.append(inputs)
            self.assertEqual((inputs / "source/token").read_text(), payload["source_token"])
            self.assertEqual((inputs / "registry/credential.json").read_text(), payload["registry_credential"])
            return {"artifact_publication_verified": True}
        if os.geteuid() == 0:
            self.skipTest("Select the non-root Linux test invocation")
        with mock.patch.object(worker, "operate", side_effect=run):
            self.assertTrue(worker.operate_private_payload({"plan": self.plan}, self.event, payload)["artifact_publication_verified"])
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())

    @unittest.skipUnless(os.name == "posix", "Original Linux receipt ownership")
    def test_receipt_handoff_refuses_cross_build_symlinks_and_overwrite(self):
        if os.geteuid() == 0:
            self.skipTest("Select the non-root Linux test invocation")
        root = self.root / self.event["build_uuid"] / "work"
        root.mkdir(parents=True, mode=0o700)
        safe = worker.receipt_path(root, self.event)
        with self.assertRaises(ValueError):
            worker.receipt_path(root, {**self.event, "build_uuid": "b" * 32})
        target = self.root / "unrelated"
        target.write_text("unrelated")
        safe.symlink_to(target)
        with self.assertRaises(ValueError):
            worker.read_receipt(safe, {}, self.event)
        argv = ["--contract", "contract.json", "--contract-sha256", "f" * 64, "--event-json", json.dumps(self.event),
                "--receipt-work-root", str(root), "--input-stdin"]
        with mock.patch.object(worker, "load_contract", return_value={"plan": self.plan}), mock.patch.object(worker, "operate_private_payload") as publish, redirect_stderr(StringIO()):
            self.assertEqual(worker.main(argv), worker.EXIT_STAGE["receipt"])
            publish.assert_not_called()
        self.assertEqual(target.read_text(), "unrelated")


if __name__ == "__main__":
    unittest.main()
