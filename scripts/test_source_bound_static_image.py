#!/usr/bin/env python3
"""Source witness and three-way credential isolation regression contracts."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import publish_harbor_static_image as publisher
import source_bound_static_image as bound
from render_gerrit_harbor_static_task import task_spec
from test_harbor_static_image import plan_fixture


def source_fixture():
    return {"schema_version": 1, "clone_url": "https://review.example.invalid/a/team/application.git", "read_username": "ci-read", "revision": "e" * 40,
        "patchset_ref": "refs/changes/23/123/2", "ca_sha256": hashlib.sha256(b"synthetic-source-ca").hexdigest(), "checkout_program_sha256": bound.checkout_program_sha256()}


class SourceBoundTests(unittest.TestCase):
    def test_reviewed_branch_binding_preserves_the_explicit_source_kind(self):
        source = source_fixture()
        source.update(schema_version=2, branch_ref="refs/heads/main")
        source.pop("patchset_ref")
        self.assertEqual(bound.validate_source(source), source)
        self.assertEqual(bound.witness_document(source)["branch_ref"], "refs/heads/main")
        self.assertNotIn("patchset_ref", bound.witness_document(source))
        for change in ({"branch_ref": "refs/changes/23/123/2"}, {"branch_ref": "refs/heads/a/../b"},
                       {"branch_ref": "refs/heads/main.lock"}, {"patchset_ref": "refs/changes/23/123/2"}, {"schema_version": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bound.validate_source({**source, **change})

    def test_branch_checkout_verifies_the_fetched_head_before_writing_a_witness(self):
        source = source_fixture()
        source.update(schema_version=2, branch_ref="refs/heads/main")
        source.pop("patchset_ref")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = root / "inputs"; credentials.mkdir()
            (credentials / "ca.crt").write_bytes(b"synthetic-source-ca")
            proof = root / "proof"; proof.mkdir()
            calls = []
            def runner(args, **kwargs):
                calls.append((args, kwargs))
            with mock.patch.object(bound.tekton_checkout.subprocess, "run", side_effect=runner):
                bound.checkout_with_witness(source, root / "checkout", proof / "checkout.json", credentials)
            fetch = next(args for args, _ in calls if "fetch" in args)
            self.assertEqual(fetch[-1], "refs/heads/main:refs/platform/source")
            transactions = [kwargs["input"] for args, kwargs in calls if "update-ref" in args]
            self.assertEqual(transactions, [("verify refs/platform/source " + source["revision"] + "\n").encode(),
                                            ("verify HEAD " + source["revision"] + "\n").encode()])
            self.assertEqual((proof / "checkout.json").read_bytes(), bound.witness_bytes(source))

    @unittest.skipUnless(os.name == "posix", "Linux generated build program")
    def test_generated_build_program_runs_with_its_own_imports(self):
        from test_gerrit_harbor_worker_fixture import BUILD
        from test_harbor_worker_fixture import BODY
        writes = {}
        def path(value):
            result = mock.Mock(); result.exists.return_value = False
            result.write_text.side_effect = OSError("read-only source")
            result.write_bytes.side_effect = lambda content: writes.update({value: content})
            return result
        with mock.patch("pathlib.Path", side_effect=path), mock.patch("os.getuid", return_value=1000), mock.patch("builtins.print"):
            exec(compile(BUILD, "build.py", "exec"), {})
        self.assertEqual(writes, {"/workspace/output/index.html": BODY})

    def test_rejects_noncanonical_source_urls_refs_and_commits(self):
        source = source_fixture(); bound.validate_source(source)
        for key, value in (("clone_url", "http://review.example.invalid/a/team/application.git"), ("clone_url", "https://u:p@review.example.invalid/a/team/application.git"),
                ("clone_url", "https://review.example.invalid/a/team/../application.git"), ("clone_url", "https://review.example.invalid/a/team/application.git?token=private"),
                ("clone_url", "HTTPS://review.example.invalid/a/team/application.git"), ("patchset_ref", "refs/heads/main"), ("patchset_ref", "refs/changes/24/123/2"),
                ("revision", "0" * 40), ("revision", "main"), ("checkout_program_sha256", "latest"), ("schema_version", True)):
            with self.subTest(key=key), self.assertRaises(ValueError): bound.validate_source({**source, key: value})

    def test_witness_binds_source_origin_revision_ref_ca_and_program(self):
        source = source_fixture(); raw = bound.witness_bytes(source)
        self.assertEqual(bound.verify_witness(source, raw), hashlib.sha256(raw).hexdigest())
        for key, value in (("clone_url", "https://other.example.invalid/a/team/application.git"), ("revision", "a" * 40), ("patchset_ref", "refs/changes/23/123/3"),
                ("ca_sha256", "b" * 64), ("checkout_program_sha256", "c" * 64)):
            with self.subTest(key=key), self.assertRaises(ValueError): bound.verify_witness({**source, key: value}, raw)
        for changed in (raw + b" ", raw.replace(b"schema_version", b"unknown_field")):
            with self.assertRaises(ValueError): bound.verify_witness(source, changed)

    def test_checkout_witness_is_written_only_after_verified_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = source_fixture(); credentials = root / "inputs"; credentials.mkdir(); (credentials / "ca.crt").write_bytes(b"synthetic-source-ca")
            proof = root / "proof"; proof.mkdir(); workspace = root / "checkout"; witness = proof / "checkout.json"
            with mock.patch.object(bound.tekton_checkout, "checkout", side_effect=ValueError("wrong commit")):
                with self.assertRaises(ValueError): bound.checkout_with_witness(source, workspace, witness, credentials)
            self.assertFalse(witness.exists())
            with mock.patch.object(bound.tekton_checkout, "checkout") as checkout:
                bound.checkout_with_witness(source, workspace, witness, credentials)
                self.assertEqual(checkout.call_args.args[:2], (source["clone_url"], source["revision"]))
                self.assertEqual(checkout.call_args.kwargs["patchset_ref"], source["patchset_ref"])
            self.assertEqual(witness.read_bytes(), bound.witness_bytes(source))
            with mock.patch.object(bound.tekton_checkout, "checkout") as checkout:
                with self.assertRaises(ValueError): bound.checkout_with_witness(source, workspace, witness, credentials)
                checkout.assert_not_called()

    def test_changed_ca_refuses_checkout_before_git_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "ca.crt").write_bytes(b"wrong-ca"); proof = root / "proof"; proof.mkdir()
            with mock.patch.object(bound.tekton_checkout, "checkout") as checkout:
                with self.assertRaises(ValueError): bound.checkout_with_witness(source_fixture(), root / "checkout", proof / "checkout.json", root)
                checkout.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux no-follow witness input")
    def test_witness_substitution_and_revision_conflict_prevent_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, _ = plan_fixture(root); source = source_fixture(); output = root / "output"; output.mkdir(); witness = root / "checkout.json"
            witness.write_bytes(bound.witness_bytes(source))
            with mock.patch.object(publisher, "publish", return_value={"source_revision": source["revision"]}) as publish:
                result = bound.publish_bound(plan, source, witness, output)
                self.assertTrue(result["source_checkout_witness_bound"]); publish.assert_called_once()
            for raw, revision in ((b"tampered", source["revision"]), (bound.witness_bytes(source), "a" * 40)):
                witness.write_bytes(raw)
                with mock.patch.object(publisher, "publish") as publish:
                    with self.assertRaises(ValueError): bound.publish_bound({**plan, "revision": revision}, source, witness, output)
                    publish.assert_not_called()
            witness.unlink(); target = root / "target"; target.write_bytes(bound.witness_bytes(source)); witness.symlink_to(target)
            with mock.patch.object(publisher, "publish") as publish:
                with self.assertRaises(OSError): bound.publish_bound(plan, source, witness, output)
                publish.assert_not_called()

    def test_task_keeps_source_robot_and_witness_mounts_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, _ = plan_fixture(Path(temporary)); plan.update(ca_file="/publisher-input/ca.crt", credential_file="/run/robot/credential.json")
            image = "registry.example.invalid/python@sha256:" + "f" * 64
            arguments = {"checkout_image": image, "build_image": image, "publisher_image": image, "build_argv": ["python3", "-I", "-B", "/workspace/source/build.py"],
                "program_map": "program", "input_map": "input", "robot_secret": "robot", "source_map": "source-plan", "source_secret": "git-reader"}
            task = task_spec(plan, source_fixture(), **arguments)
            self.assertEqual([s["name"] for s in task["steps"]], ["checkout", "build", "publish"])
            steps = {s["name"]: {m["name"]: m for m in s["volumeMounts"]} for s in task["steps"]}
            self.assertEqual(set(steps["build"]), {"output", "build-tmp", "source-tree"}); self.assertTrue(steps["build"]["source-tree"]["readOnly"])
            self.assertNotIn("robot", steps["checkout"]); self.assertNotIn("source-credential", steps["publish"])
            self.assertTrue(steps["publish"]["witness"]["readOnly"]); self.assertFalse(steps["checkout"]["witness"].get("readOnly", False))
            with self.assertRaises(ValueError): task_spec(plan, source_fixture(), **{**arguments, "source_secret": "robot"})
            with self.assertRaises(ValueError): task_spec(plan, {**source_fixture(), "revision": "a" * 40}, **arguments)
            self.assertEqual(plan["revision"], "e" * 40)


if __name__ == "__main__": unittest.main()
