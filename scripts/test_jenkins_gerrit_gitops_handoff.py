import copy
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import jenkins_artifact_promotion as artifact
import jenkins_gerrit_gitops_handoff as adapter
import jenkins_static_image as publisher
import test_ci_gitops_handoff as fixtures
from test_gerrit_artifact_promotion import ArtifactCD, Git, deployment
import test_report_jenkins_gerrit as report_fixtures


class JenkinsHandoffTests(unittest.TestCase):
    def test_example_is_valid_and_bound_to_the_report_policy(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "jenkins-gerrit-argocd-handoff.json"
        example = json.loads(path.read_text(encoding="utf-8"))
        adapter.validate(example)
        example["report"]["trigger"]["change"] += 1
        with self.assertRaises(adapter.handoff.HandoffError): adapter.validate(example)

    @unittest.skipUnless(os.name == "posix", "Linux operator entry point")
    def test_cli_uses_private_journal_and_scoped_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "private").mkdir(mode=0o700)
            inputs = root / "inputs"; inputs.mkdir(mode=0o700)
            git_inputs = root / "git-inputs"; git_inputs.mkdir(mode=0o700)
            for name, value in {"jenkins-user": "jenkins-reader", "gerrit-user": "source-reader", "reviewer-user": "ci-review",
                                "jenkins-token": "synthetic-jenkins-token", "gerrit-token": "synthetic-gerrit-token",
                                "reviewer-token": "synthetic-review-token"}.items():
                path = inputs / name; path.write_text(value); path.chmod(0o600)
            for name, value in {"token": "synthetic-writer-token", "ca.crt": "fixture-ca"}.items():
                path = git_inputs / name; path.write_text(value); path.chmod(0o600)
            plan, _, _, _, _, _, _ = fixtures.fixture(root)
            report_case = report_fixtures.ReporterTests(); report_case.setUp()
            try:
                plan["bridge_contract_sha256"] = adapter.reporter.reporter.fingerprint(report_case.plan)
                policy = {"schema_version": 1, "runtime": "rke2", "report": report_case.plan, "promotion": plan}
                policy_path = root / "policy.json"; policy_path.write_text(json.dumps(policy))
                args = ["handoff", "--policy", str(policy_path), "--queue-journal", str(root / "queue"),
                        "--review-journal-dir", str(root / "reviews"), "--input-dir", str(inputs),
                        "--git-input-dir", str(git_inputs), "--output-dir", str(root / "private" / "proof")]
                out = io.StringIO()
                with patch.object(adapter.handoff, "ROOT", root), patch.object(adapter.bridge, "frozen_target", return_value=contextlib.nullcontext()), \
                     patch.object(adapter, "Client", return_value=object()), patch.object(adapter, "execute", return_value={"preflight_verified": True}) as execute, \
                     patch.object(sys, "argv", args), contextlib.redirect_stdout(out):
                    self.assertEqual(adapter.main(), 0)
                self.assertTrue(json.loads(out.getvalue())["preflight_verified"])
                self.assertFalse(execute.call_args.kwargs["apply"])
                self.assertNotIn("synthetic-", out.getvalue())
                proof = json.loads((root / "private" / "proof" / "handoff-proof.json").read_text())
                self.assertEqual(proof["phase"], "local-validated")
                self.assertNotIn("synthetic-", json.dumps(proof))
                args[-1] = str(root / "private" / "failure")
                error_output = io.StringIO()
                with patch.object(adapter.handoff, "ROOT", root), patch.object(adapter.bridge, "frozen_target", return_value=contextlib.nullcontext()), \
                     patch.object(adapter, "Client", return_value=object()), patch.object(adapter, "execute", side_effect=adapter.KubernetesOperationError(1, "forbidden private-token")), \
                     patch.object(sys, "argv", args), contextlib.redirect_stderr(error_output):
                    self.assertEqual(adapter.main(), 1)
                failed = json.loads((root / "private" / "failure" / "handoff-proof.json").read_text())
                self.assertEqual(failed["phase"], "failed")
                self.assertTrue(failed["failure_diagnostic"]["kubernetes"]["signals"]["forbidden"])
                self.assertNotIn("private-token", json.dumps(failed) + error_output.getvalue())
            finally:
                report_case.tearDown()

    def test_cli_rejects_missing_inputs_without_echoing_contents(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["handoff", "--policy", "missing-sensitive-path", "--queue-journal", "missing",
                        "--review-journal-dir", "missing", "--input-dir", "missing", "--git-input-dir", "missing", "--output-dir", "missing"]), contextlib.redirect_stderr(output):
            self.assertEqual(adapter.main(), 1)
        self.assertNotIn("missing-sensitive-path", output.getvalue())

    def test_guarded_promotion_and_changed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, _, _, _, _, cd = fixtures.fixture(Path(directory))
            report_case = report_fixtures.ReporterTests()
            report_case.setUp()
            try:
                report = report_case.plan
                trigger = report["trigger"]
                trigger["revision"] = "a" * 40
                trigger["project"] = "demo"
                clone = trigger["gerrit_origin"] + "/a/demo.git"
                obj = cd.objects["application.argoproj.io/application"]
                obj["spec"]["source"]["repoURL"] = clone
                plan["cd"]["spec_sha256"] = adapter.handoff.sha256(obj["spec"])
                plan["bridge_contract_sha256"] = adapter.reporter.reporter.fingerprint(report)
                policy = {"schema_version": 1, "runtime": "rke2", "report": report, "promotion": plan}
                evidence = {"revision": "a" * 40, "queue_id": 5, "review_vote_verified": True}
                git = fixtures.Git()
                with patch.object(adapter, "verify_target"), patch.object(adapter.reporter, "verify_merged", return_value=evidence), patch.object(adapter.handoff, "GitBranch", return_value=git):
                    result = adapter.execute(policy, None, None, None, None, None, cd, git_inputs=Path(directory))
                    self.assertTrue(result["preflight_verified"])
                    self.assertFalse(result["branch_push_attempted"])
                git = fixtures.Git()
                with patch.object(adapter, "verify_target"), patch.object(adapter.reporter, "verify_merged", return_value=evidence), patch.object(adapter.handoff, "GitBranch", return_value=git):
                    result = adapter.execute(policy, None, None, None, None, None, cd, git_inputs=Path(directory), apply=True)
                    self.assertTrue(result["controller_verified"])
                    self.assertEqual(git.pushes, 1)
                git = fixtures.Git()
                changed = {**evidence, "queue_id": 6}
                with patch.object(adapter, "verify_target"), patch.object(adapter.reporter, "verify_merged", side_effect=[evidence, changed]), patch.object(adapter.handoff, "GitBranch", return_value=git):
                    with self.assertRaises(adapter.handoff.HandoffError):
                        adapter.execute(policy, None, None, None, None, None, cd, git_inputs=Path(directory), apply=True)
                    self.assertEqual(git.pushes, 0)
                invalid = copy.deepcopy(policy); invalid["promotion"]["git_username"] = report["reviewer_username"]
                with self.assertRaises(adapter.handoff.HandoffError):
                    adapter.execute(invalid, None, None, None, None, None, cd, git_inputs=Path(directory))
            finally:
                report_case.tearDown()


class SignedJenkinsHandoffTests(unittest.TestCase):
    def setUp(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        self.contract = json.loads((examples / "jenkins-static-image.json").read_bytes())
        self.plan = json.loads((examples / "jenkins-argocd-artifact-promotion.json").read_bytes())
        for key in ("ca_sha256", "leaf_sha256", "credential_sha256"):
            self.contract["registry"][key] = "a" * 64
        self.contract["jenkins_ca_sha256"] = self.contract["source"]["ca_sha256"] = "a" * 64
        self.contract["source"]["checkout_program_sha256"] = publisher.checkout.checkout_program_sha256()
        self.plan["publisher_contract_sha256"] = publisher.fingerprint(self.contract)
        for key, ref in self.plan["signature_policy"].items():
            if key != "cosign_binary":
                ref["sha256"] = "b" * 64
            if os.name != "posix":
                ref["path"] = str(Path.cwd() / (key + ".fixture"))
        trigger = self.plan["handoff"]["report"]["trigger"]
        self.identity = {"build": 7, **{key: trigger[key] for key in ("change", "patchset", "revision")}}
        self.review = {**self.identity, "queue_id": 18, "review_vote_verified": True}
        source = publisher.source_plan(self.contract, self.identity)
        digest = "sha256:" + "d" * 64
        self.receipt = {"schema_version": 1, "producer": "platform-jenkins-static-image", "build": self.identity.copy(),
                        "publisher_contract_sha256": self.plan["publisher_contract_sha256"], "artifact": {
                            "schema_version": 1, "image": publisher.image_reference(self.contract, digest),
                            "manifest_digest": digest, "config_digest": digest, "output_layer_digest": digest,
                            "output_tree_sha256": "c" * 64, "base_digest": self.contract["registry"]["base_digest"],
                            "source_revision": source["revision"], "artifact_publication_verified": True,
                            "publisher_signature_verified": False, "deployment_verified": False, "ha_verified": False,
                            "full_stack_verified": False, "source_checkout_witness_bound": True,
                            "source_plan_sha256": publisher.fingerprint(source),
                            "checkout_witness_sha256": hashlib.sha256(publisher.checkout.witness_bytes(source)).hexdigest()}}
        self.raw = publisher.harbor.canonical(self.receipt)
        self.reader = Mock(return_value=self.raw)
        self.readers = (self.reader, None, None)
        self.args = (self.plan, self.contract, None, None)
        self.verification = adapter.reporter

    def test_review_scope_and_exact_build_artifact_bound_to_signature(self):
        with patch.object(self.verification, "verify_merged", return_value=self.review) as reviews:
            release = artifact.release_request(*self.args, *self.readers)
        self.assertEqual(reviews.call_count, 2)
        self.reader.assert_called_once_with("/job/team/job/review-build/7/artifact/" + publisher.ARTIFACT, False,
                                            accept="application/json", max_bytes=publisher.LIMIT)
        self.assertEqual(release["image"], self.receipt["artifact"]["image"])
        self.assertEqual(release["annotations"]["platform.gitops/jenkins-build"], "7")
        self.assertEqual(release["annotations"]["platform.gitops/jenkins-queue-id"], "18")
        self.assertEqual(len(release["annotations"]), 8)
        for runtime in ("rke2", "k3s", "k8s", "docker", "raw"):
            self.plan["handoff"]["runtime"] = self.plan["handoff"]["promotion"]["runtime"] = runtime
            with self.subTest(runtime=runtime):
                self.assertTrue(artifact.operate(self.plan, self.contract)["local_inputs_verified"])

    def test_worker_ref_and_archived_receipt_identity_are_checked(self):
        env = {"JOB_NAME": "team/review-build", "BUILD_NUMBER": "7", "GERRIT_CHANGE_NUMBER": "123",
               "GERRIT_PATCHSET_NUMBER": "1", "GERRIT_PATCHSET_REVISION": self.identity["revision"],
               "GIT_COMMIT": self.identity["revision"], "GERRIT_REFSPEC": "refs/changes/23/123/1"}
        self.assertEqual(publisher.build_identity(self.contract, env), self.identity)
        for key, value in (("BUILD_NUMBER", "07"), ("JOB_NAME", "wrong"), ("GERRIT_REFSPEC", "refs/heads/main"),
                           ("GIT_COMMIT", "a" * 40)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                publisher.build_identity(self.contract, {**env, key: value})
        for key, value in (("build", 8), ("patchset", True), ("revision", "c" * 40)):
            receipt = {**self.receipt, "build": {**self.identity, key: value}}
            with self.subTest(key=key), self.assertRaises(ValueError):
                publisher.verify_receipt(self.contract, self.identity, receipt)

    def test_changed_merge_and_missing_signature_stop_before_git(self):
        with (patch.object(self.verification, "verify_merged", side_effect=[self.review, {**self.review, "queue_id": 19}]),
              self.assertRaisesRegex(ValueError, "changed-during-artifact-read")):
            artifact.release_request(*self.args, *self.readers)
        with (patch.object(self.verification, "verify_merged", return_value=self.review),
              patch.object(artifact.promotion, "verify_release_signature", side_effect=ValueError("no authorized signature")),
              patch.object(artifact.promotion, "ArtifactGit") as git, self.assertRaisesRegex(ValueError, "no authorized signature")):
            artifact.execute(*self.args, self.readers, None, git_inputs=Path.cwd(), apply=True)
        git.assert_not_called()

    def test_changed_archive_bytes_stop_apply_even_with_unchanged_image(self):
        cd = ArtifactCD()
        policy = self.plan["artifact_policy"]
        handoff = self.plan["handoff"]["promotion"]
        live = deployment(policy["expected_image"])
        live["metadata"].update(uid=policy["deployment_uid"], generation=1)
        live["spec"]["template"]["spec"]["containers"][0]["name"] = policy["container"]
        cd.objects["deployment.apps/" + policy["deployment"]] = live
        git = Git()
        git.source = self.identity["revision"]
        git.tip = policy["expected_deployment_revision"]
        self.reader.side_effect = [self.raw, self.raw + b"\n"]
        with (patch.object(self.verification, "verify_merged", return_value=self.review),
              patch.object(artifact.promotion, "verify_release_signature", return_value={"signed_annotations_sha256": "a" * 64}),
              patch.object(artifact.promotion, "verify_destination"),
              patch.object(artifact.promotion, "artifact_cd_object", return_value={"metadata": {"uid": handoff["cd"]["uid"]}}),
              patch.object(artifact.promotion, "ArtifactGit", return_value=git),
              self.assertRaisesRegex(ValueError, "evidence-changed-before-push")):
            artifact.execute(*self.args, self.readers, cd, git_inputs=Path.cwd(), apply=True)
        self.assertEqual(git.pushes, 0)


if __name__ == "__main__": unittest.main()
