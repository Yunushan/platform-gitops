#!/usr/bin/env python3
"""Exercise GitLab pipeline evidence through both GitOps handoff paths."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ci_gitops_handoff as handoff
import gerrit_artifact_promotion as promotion
import gitlab_artifact_promotion as artifact_promotion
import gitlab_gitops_handoff as gitlab_handoff
import gitlab_static_image as publisher
from test_ci_gitops_handoff import Git, fixture
from test_gerrit_artifact_promotion import DEPLOYMENT_UID, OLD_IMAGE, Git as ArtifactGit, deployment
import test_gitlab_pipeline_verification as pipeline_fixture
import verify_image_signature as signatures


def publication_case(fixed, directory, *, job_id=31, ca_sha256="2" * 64):
    plan = copy.deepcopy(fixed)
    pipeline = plan["pipeline"]
    contract = {"schema_version": 1, **{key: pipeline[key] for key in ("api_url", "project_id", "project_path", "branch")},
        "job": "build", "source": {"read_username": "cd-writer", "ca_sha256": ca_sha256}, "static_directory": "public",
        "registry": {"origin": "https://registry.example.invalid", "repository": "fixture/ha", "base_digest": "sha256:" + "9" * 64,
                     "ca_sha256": "1" * 64, "leaf_sha256": "2" * 64, "credential_sha256": "3" * 64,
                     "project_id": 61, "robot_id": 12, "files": ["index.html"]}}
    plan.update(publisher_contract_sha256=publisher.fingerprint(contract), artifact_policy={
        "manifest": "deploy/staging/workload.json", "namespace": "application", "deployment": "application",
        "deployment_uid": DEPLOYMENT_UID, "container": "app", "expected_image": OLD_IMAGE,
        "expected_deployment_revision": "b" * 40, "commit_name": "Artifact Writer", "commit_email": "writer@example.invalid",
        "destination_server": "https://kubernetes.default.svc"}, signature_policy={
        name: {"path": str(directory / name), "sha256": digest} for name, digest in
        (("cosign_binary", signatures.COSIGN_SHA256), ("registry_ca", "1" * 64),
         ("public_key", "2" * 64), ("trusted_root", "3" * 64), ("credential", "4" * 64))})
    manifest = "sha256:" + "1" * 64
    receipt = {"schema_version": 1, "producer": "platform-gitlab-static-image", "publisher_contract_sha256": publisher.fingerprint(contract),
        "job": {"project_id": pipeline["project_id"], "pipeline_id": pipeline["pipeline_id"], "revision": pipeline["revision"], "job_id": job_id},
        "artifact": {"schema_version": 1, "manifest_digest": manifest, "image": publisher.image_reference(contract, manifest),
                     "base_digest": contract["registry"]["base_digest"], "source_revision": pipeline["revision"],
                     "config_digest": "sha256:" + "2" * 64, "output_layer_digest": "sha256:" + "3" * 64, "output_tree_sha256": "4" * 64,
                     "artifact_publication_verified": True, "publisher_signature_verified": False, "deployment_verified": False,
                     "ha_verified": False, "full_stack_verified": False}}
    return plan, contract, receipt


class GitLabHandoffTest(unittest.TestCase):
    def setup_case(self, root, kind):
        policy, contract, _, _, _, _, cd = fixture(root, kind=kind)
        source = pipeline_fixture.PipelineVerificationTest()
        source.setUp()
        policy["bridge_contract_sha256"] = handoff.sha256(source.plan)
        clone = "https://gitlab.example/group/project.git"
        if kind == "argocd":
            obj = cd.objects["application.argoproj.io/application"]
            obj["spec"]["source"]["repoURL"] = clone
        else:
            obj = cd.objects["kustomization.kustomize.toolkit.fluxcd.io/application"]
            repository = cd.objects["gitrepository.source.toolkit.fluxcd.io/application"]
            repository["spec"]["url"] = clone
            policy["cd"]["source_spec_sha256"] = handoff.sha256(repository["spec"])
        policy["cd"]["spec_sha256"] = handoff.sha256(obj["spec"])
        return {"schema_version": 1, "runtime": "rke2", "pipeline": source.plan, "handoff": policy}, source, cd

    def test_preflight_and_apply_for_both_controllers(self):
        for kind in ("argocd", "flux"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                plan, source, cd = self.setup_case(Path(directory), kind)
                git = Git()
                with mock.patch.object(handoff, "GitBranch", return_value=git):
                    result = gitlab_handoff.execute(plan, cd, source.get)
                    self.assertTrue(result["preflight_verified"])
                    self.assertFalse(result["branch_push_attempted"])
                    result = gitlab_handoff.execute(plan, cd, source.get, apply=True)
                self.assertTrue(result["controller_verified"])
                self.assertTrue(result["branch_push_attempted"])
                self.assertEqual(git.pushes, 1)

    def test_bad_job_or_modified_pipeline_contract_never_pushes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, source, cd = self.setup_case(Path(directory), "argocd")
            git = Git()
            source.jobs[0]["status"] = "failed"
            with mock.patch.object(handoff, "GitBranch", return_value=git):
                with self.assertRaises(ValueError): gitlab_handoff.execute(plan, cd, source.get, apply=True)
            self.assertEqual(git.pushes, 0)
            plan["pipeline"]["branch"] = "another"
            with self.assertRaises(ValueError): gitlab_handoff.validate(plan)

    def test_ci_change_before_push_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, source, cd = self.setup_case(Path(directory), "argocd")
            git = Git()
            original = git.prepare
            def change(*args):
                result = original(*args)
                source.branch["commit"]["id"] = "b" * 40
                return result
            git.prepare = change
            with mock.patch.object(handoff, "GitBranch", return_value=git):
                with self.assertRaises(ValueError): gitlab_handoff.execute(plan, cd, source.get, apply=True)
            self.assertEqual(git.pushes, 0)

    def test_local_validation_accepts_each_runtime_without_contacting_services(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            plan, _, _ = self.setup_case(Path(directory), "argocd")
            path = Path(directory) / "plan.json"
            for runtime in ("rke2", "k3s", "k8s", "docker", "raw"):
                with self.subTest(runtime=runtime):
                    plan["runtime"] = runtime
                    plan["handoff"]["runtime"] = runtime
                    path.write_text(json.dumps(plan), encoding="utf-8")
                    result = gitlab_handoff.operate(path)
                    self.assertTrue(result["local_inputs_verified"])
                    self.assertFalse(result["controller_verified"])
                    self.assertFalse(result["full_stack_verified"])

    def test_reusable_policy_binds_each_run_and_rejects_policy_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixed, source, cd = self.setup_case(Path(directory), "argocd")
            pipeline_policy = {k: v for k, v in fixed["pipeline"].items() if k not in {"pipeline_id", "revision"}}
            policy = dict(fixed["handoff"], bridge_contract_sha256=handoff.sha256(pipeline_policy))
            reusable = {"schema_version": 2, "runtime": "rke2", "pipeline_policy": pipeline_policy, "handoff": policy}
            materialized = gitlab_handoff.materialize(reusable, 23, "a" * 40)
            self.assertEqual(materialized, fixed)
            git = Git()
            with mock.patch.object(handoff, "GitBranch", return_value=git):
                self.assertTrue(gitlab_handoff.execute(materialized, cd, source.get, apply=True)["controller_verified"])
            for invalid_id, invalid_revision in [(None, "a" * 40), (23, None), (True, "a" * 40), (23, "invalid")]:
                with self.subTest(pipeline_id=invalid_id, revision=invalid_revision), self.assertRaises(ValueError):
                    gitlab_handoff.materialize(reusable, invalid_id, invalid_revision)
            pipeline_policy["required_jobs"] = ["build"]
            with self.assertRaises(ValueError): gitlab_handoff.materialize(reusable, 23, "a" * 40)

    def test_reusable_policy_validates_locally_without_run_identity(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            fixed, _, _ = self.setup_case(Path(directory), "argocd")
            pipeline_policy = {k: v for k, v in fixed["pipeline"].items() if k not in {"pipeline_id", "revision"}}
            reusable = {"schema_version": 2, "runtime": "rke2", "pipeline_policy": pipeline_policy,
                        "handoff": dict(fixed["handoff"], bridge_contract_sha256=handoff.sha256(pipeline_policy))}
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(reusable), encoding="utf-8")
            self.assertTrue(gitlab_handoff.operate(path)["local_inputs_verified"])
            with self.assertRaises(ValueError): gitlab_handoff.operate(path, check_server=True)

    def test_wait_for_exact_pipeline_fails_closed_on_errors_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, source, _ = self.setup_case(Path(directory), "argocd")
            pipeline = plan["pipeline"]
            now = [0]
            def clock(): return now[0]
            def sleep(seconds): now[0] += seconds
            states = iter(["pending", "running", "success"])
            def get(path):
                item, headers = source.get(path)
                item["status"] = next(states)
                return item, headers
            gitlab_handoff.wait_pipeline(pipeline, get, 30, clock=clock, sleep=sleep)
            self.assertEqual(now[0], 10)
            for status in ("failed", "canceled", "manual", "unknown"):
                def failed(path):
                    item, headers = source.get(path)
                    item["status"] = status
                    return item, headers
                with self.subTest(status=status), self.assertRaises(ValueError):
                    gitlab_handoff.wait_pipeline(pipeline, failed, 30, clock=clock, sleep=sleep)
            def pending(path):
                item, headers = source.get(path)
                item["status"] = "pending"
                return item, headers
            with self.assertRaisesRegex(ValueError, "timeout"):
                gitlab_handoff.wait_pipeline(pipeline, pending, 10, clock=clock, sleep=sleep)
            def wrong_commit(path):
                item, headers = source.get(path)
                item["sha"] = "b" * 40
                return item, headers
            with self.assertRaisesRegex(ValueError, "identity"):
                gitlab_handoff.wait_pipeline(pipeline, wrong_commit, 10, clock=clock, sleep=sleep)

    def test_wait_is_bounded_and_cannot_run_in_local_mode(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            plan, _, _ = self.setup_case(Path(directory), "argocd")
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            for seconds in (-1, 3601, True, 1):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    gitlab_handoff.operate(path, wait_seconds=seconds)

    def test_discovery_validates_latest_and_rechecks_before_push(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, source, cd = self.setup_case(Path(directory), "argocd")
            policy = {k: v for k, v in plan["pipeline"].items() if k not in {"pipeline_id", "revision"}}
            latest = dict(source.pipeline)
            def get(path):
                if path == "/projects/7/pipelines/latest?ref=main": return dict(latest), {}
                return source.get(path)
            self.assertEqual(gitlab_handoff.discover_latest(policy, get), (23, "a" * 40))
            git = Git()
            original = git.prepare
            def newer(*args):
                result = original(*args)
                latest["id"] = 24
                return result
            git.prepare = newer
            with mock.patch.object(handoff, "GitBranch", return_value=git):
                with self.assertRaisesRegex(ValueError, "latest-pipeline-changed"):
                    gitlab_handoff.execute(plan, cd, get, apply=True, require_latest=True)
            self.assertEqual(git.pushes, 0)
            latest["source"] = "schedule"
            with self.assertRaises(ValueError): gitlab_handoff.discover_latest(policy, get)
            source.branch["protected"] = False
            with self.assertRaises(ValueError): gitlab_handoff.discover_latest(policy, get)


class GitLabArtifactTests(unittest.TestCase):
    def case(self, root):
        fixed, api, cd = GitLabHandoffTest().setup_case(root, "flux")
        plan, contract, receipt = publication_case(fixed, root)
        live = deployment()
        live["metadata"].update(uid=DEPLOYMENT_UID, generation=1)
        cd.objects["deployment.apps/application"] = live
        return plan, contract, receipt, api, cd

    def test_release_request_binds_exact_job_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, receipt, api, _ = self.case(Path(directory))
            read = mock.Mock(return_value=(receipt, {}))
            request = artifact_promotion.release_request(plan, contract, api.get, read)
            read.assert_called_once_with("/projects/7/jobs/31/artifacts/" + publisher.ARTIFACT)
            self.assertEqual(request["image"], receipt["artifact"]["image"])
            self.assertEqual(request["annotations"]["platform.gitops/gitlab-pipeline-id"], "23")
            self.assertEqual(request["annotations"]["platform.gitops/gitlab-job-id"], "31")
            self.assertNotIn(directory, str(request))
            plan["signature_policy"]["credential"]["sha256"] = "6" * 64
            self.assertEqual(artifact_promotion.release_request(plan, contract, api.get, read), request)
            plan["artifact_policy"]["expected_deployment_revision"] = "c" * 40
            self.assertNotEqual(artifact_promotion.release_request(plan, contract, api.get, read), request)

    def test_wrong_receipt_and_retry_block_git(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, receipt, api, cd = self.case(Path(directory))
            for name, change in (("job", lambda r: r["job"].update(job_id=32)),
                                 ("contract", lambda r: r.update(publisher_contract_sha256="8" * 64)),
                                 ("image", lambda r: r["artifact"].update(image="registry.example.invalid/other/app@" + r["artifact"]["manifest_digest"])),
                                 ("tag", lambda r: r["artifact"].update(image="registry.example.invalid/fixture/ha:latest"))):
                wrong = copy.deepcopy(receipt)
                change(wrong)
                with self.subTest(name=name), mock.patch.object(promotion, "ArtifactGit") as git, self.assertRaises(ValueError):
                    artifact_promotion.execute(plan, contract, cd, api.get, lambda _: (wrong, {}), apply=True)
                git.assert_not_called()

            def new_job(_):
                api.jobs[0]["id"] = 33
                return receipt, {}

            with mock.patch.object(promotion, "ArtifactGit") as git, self.assertRaisesRegex(ValueError, "jobs-changed"):
                artifact_promotion.execute(plan, contract, cd, api.get, new_job, apply=True)
            git.assert_not_called()

    def test_missing_signature_and_changed_receipt_block_push(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, receipt, api, cd = self.case(Path(directory))
            with (mock.patch.object(signatures, "operate", side_effect=ValueError("no approved signature")),
                  mock.patch.object(promotion, "ArtifactGit") as git, self.assertRaisesRegex(ValueError, "signature-not-verified")):
                artifact_promotion.execute(plan, contract, cd, api.get, lambda _: (receipt, {}), apply=True)
            git.assert_not_called()
            git = ArtifactGit(cd)
            other = copy.deepcopy(receipt)
            other["artifact"]["output_tree_sha256"] = "b" * 64
            read = mock.Mock(side_effect=[(receipt, {}), (other, {})])
            with (mock.patch.object(promotion, "verify_release_signature", return_value={"signed_annotations_sha256": "a" * 64}) as verifier,
                  mock.patch.object(promotion, "verify_destination"), mock.patch.object(promotion, "ArtifactGit", return_value=git),
                  self.assertRaisesRegex(ValueError, "evidence-changed-before-push")):
                artifact_promotion.execute(plan, contract, cd, api.get, read, apply=True)
            verifier.assert_called_once()
            self.assertEqual(git.pushes, 0)

    def test_runner_job_identity_is_pinned_to_protected_push(self):
        with tempfile.TemporaryDirectory() as directory:
            _, contract, receipt, _, _ = self.case(Path(directory))
            env = {"CI_SERVER_URL": contract["api_url"].removesuffix("/api/v4"), "CI_PROJECT_ID": "7", "CI_PROJECT_PATH": "group/project",
                   "CI_COMMIT_BRANCH": "main", "CI_JOB_NAME": "build", "CI_COMMIT_REF_PROTECTED": "true", "CI_PIPELINE_SOURCE": "push",
                   "CI_COMMIT_SHA": "a" * 40, "CI_PIPELINE_ID": "23", "CI_JOB_ID": "31"}
            self.assertEqual(publisher.job_identity(contract, env), receipt["job"])
            for name, value in (("CI_COMMIT_REF_PROTECTED", "false"), ("CI_PIPELINE_SOURCE", "merge_request_event"),
                                ("CI_PIPELINE_ID", "1\n"), ("CI_JOB_NAME", "other-job"), ("CI_COMMIT_SHA", "0" * 40)):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    publisher.job_identity(contract, {**env, name: value})


if __name__ == "__main__":
    unittest.main()
