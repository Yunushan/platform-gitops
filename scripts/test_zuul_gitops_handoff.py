"""Exercise pinned Zuul evidence with both reviewed controller contracts."""

import copy
import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from strict_json import loads_strict_json
from test_ci_gitops_handoff import Git, fixture
from test_gerrit_artifact_promotion import DEPLOYMENT_UID, OLD_IMAGE, Git as ImageGit, deployment
from test_zuul_static_image import fixture as publisher_fixture
import gerrit_artifact_promotion as promotion
import render_zuul_static_image as renderer
import zuul_artifact_promotion as artifact
import zuul_static_image as publisher
import zuul_gitops_handoff as zuul
import verify_image_signature as signatures


class ZuulHandoffTests(unittest.TestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[1] / "examples/zuul-argocd-handoff.json"
        self.plan = loads_strict_json(example.read_bytes())
        self.ci = self.plan["buildset"]
        self.ci.update(zuul_ca_sha256="e" * 64, revision="a" * 40)
        self.plan["handoff"]["bridge_contract_sha256"] = zuul.handoff.sha256(self.ci)
        self.plan["handoff"]["cd"]["spec_sha256"] = "f" * 64
        ref = {"project": self.ci["project"], "branch": "main", "ref": "refs/heads/main",
               "newrev": "a" * 40, "change": None, "patchset": None, "oldrev": "b" * 40}
        self.row = {
            "uuid": self.ci["uuid"], "pipeline": "post", "result": "SUCCESS", "refs": [ref], "user_id": None,
            "first_build_start_time": "2026-01-15T12:00:00", "last_build_end_time": "2026-01-15T12:01:00",
            "builds": [{
                "uuid": "c" * 32, "job_name": "verify-manifests", "start_time": "2026-01-15T12:00:05",
                "end_time": "2026-01-15T12:00:35", "result": "SUCCESS", "voting": True, "final": True,
                "ref": copy.deepcopy(ref),
            }],
        }

    def verify(self, row):
        get = mock.Mock(return_value=row)
        proof = zuul.verify_buildset(self.ci, get)
        get.assert_called_once_with("/api/tenant/platform/buildset/" + self.ci["uuid"])
        return proof

    def test_verifies_pinned_uuid_revision_and_final_voting_job(self):
        original = self.row["builds"][0]
        self.row["builds"].insert(0, {**original, "uuid": "d" * 32, "final": False, "result": "RETRY"})
        proof = self.verify(self.row)
        self.assertEqual(proof["revision"], self.ci["revision"])
        self.assertEqual(proof["jobs"]["verify-manifests"]["uuid"], original["uuid"])

    def test_trust_pin_uses_exact_file_bytes_with_terminal_newline(self):
        trust = b"fixture CA data\n"
        self.ci["zuul_ca_sha256"] = sha256(trust).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            inputs = Path(directory)
            for name, contents in (("zuul-ca.crt", trust), ("zuul-token", b"fixture-read-token\n")):
                path = inputs / name
                path.write_bytes(contents)
                path.chmod(0o600)
            with mock.patch.object(zuul.ssl, "create_default_context") as context:
                zuul.requester(self.ci, inputs)
            context.assert_called_once_with(cadata=trust.decode("ascii"))
            (inputs / "zuul-ca.crt").write_bytes(trust.rstrip(b"\n"))
            with self.assertRaisesRegex(zuul.handoff.HandoffError, "zuul-trust-anchor-changed"):
                zuul.requester(self.ci, inputs)

    def test_rejects_unrelated_speculative_pending_and_incomplete_results(self):
        changes = (
            lambda b: b.update(result="FAILURE"),
            lambda b: b.update(pipeline="check"),
            lambda b: b.update(user_id="operator"),
            lambda b: b["refs"][0].update(newrev="0" * 40),
            lambda b: b["refs"][0].update(change=27, patchset="1"),
            lambda b: b["refs"].append(dict(b["refs"][0])),
            lambda b: b["builds"][0].update(final=False),
            lambda b: b["builds"][0].update(voting=False),
            lambda b: b["builds"][0].update(end_time=None),
            lambda b: b["builds"][0].update(result="SKIPPED"),
            lambda b: b["builds"][0]["ref"].update(project="different/project"),
            lambda b: b["builds"].append(dict(b["builds"][0])),
        )
        for change in changes:
            candidate = copy.deepcopy(self.row)
            change(candidate)
            with self.subTest(change=change.__code__.co_firstlineno), self.assertRaises(zuul.handoff.HandoffError):
                self.verify(candidate)

    def controller(self, directory, kind):
        policy, _, _, _, _, _, cd = fixture(Path(directory), kind=kind)
        policy["bridge_contract_sha256"] = zuul.handoff.sha256(self.ci)
        self.plan["handoff"] = policy
        clone = zuul.validate(self.plan)["plan"]["source"]["clone_url"]
        if kind == "argocd":
            app = cd.objects["application.argoproj.io/application"]
            app["spec"]["source"]["repoURL"] = clone
        else:
            app = cd.objects["kustomization.kustomize.toolkit.fluxcd.io/application"]
            source = cd.objects["gitrepository.source.toolkit.fluxcd.io/application"]
            source["spec"]["url"] = clone
            policy["cd"]["source_spec_sha256"] = zuul.handoff.sha256(source["spec"])
        policy["cd"]["spec_sha256"] = zuul.handoff.sha256(app["spec"])
        return cd

    def test_preflight_exact_promotion_and_replay_for_both_controllers(self):
        for kind in ("argocd", "flux"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                cd = self.controller(directory, kind)
                git = Git()
                get = mock.Mock(return_value=self.row)
                with mock.patch.object(zuul.bridge, "verify_target") as target, mock.patch.object(zuul.handoff, "GitBranch", return_value=git):
                    self.assertTrue(zuul.execute(self.plan, cd, get)["read_only"])
                    self.assertEqual(git.pushes, 0)
                    self.assertTrue(zuul.execute(self.plan, cd, get, apply=True)["controller_verified"])
                    replay = zuul.execute(self.plan, cd, get, apply=True)
                    self.assertTrue(replay["controller_verified"])
                    self.assertFalse(replay["branch_push_attempted"])
                    self.assertEqual(git.pushes, 1)
                self.assertEqual(target.call_count, 3)
                self.assertEqual(get.call_count, 5)

    def test_changed_build_evidence_stops_before_push(self):
        changed = copy.deepcopy(self.row)
        changed["builds"][0]["uuid"] = "d" * 32
        with tempfile.TemporaryDirectory() as directory:
            cd = self.controller(directory, "flux")
            git = Git()
            get = mock.Mock(side_effect=[self.row, changed])
            with mock.patch.object(zuul.bridge, "verify_target"), mock.patch.object(zuul.handoff, "GitBranch", return_value=git):
                with self.assertRaisesRegex(zuul.handoff.HandoffError, "evidence-changed-before-push"):
                    zuul.execute(self.plan, cd, get, apply=True)
            self.assertEqual(git.pushes, 0)


def promotion_fixture(root, kind):
    existing = ZuulHandoffTests()
    existing.setUp()
    publication, ci, event, _ = publisher_fixture(root)
    buildset = existing.ci
    buildset.update(gerrit_origin="https://review.example.com", pipeline=publication["pipeline"],
                    project=publication["project"], required_jobs=[publication["job"]])
    cd = existing.controller(root, kind)
    handoff = existing.plan
    files = renderer.render(publication, ci, handoff["runtime"])
    contract = json.loads(files[renderer.PACKAGE + "/contract.json"])
    policy = {"manifest": "deploy/staging/workload.json", "namespace": "application", "deployment": "application",
              "deployment_uid": DEPLOYMENT_UID, "container": "app", "expected_image": OLD_IMAGE,
              "expected_deployment_revision": "b" * 40, "commit_name": "Artifact writer",
              "commit_email": "cd-writer@example.invalid", "destination_server": "https://kubernetes.default.svc"}
    plan = {**handoff, "publisher_contract_sha256": publisher.fingerprint(contract), "artifact_policy": policy}
    row = existing.row
    row["pipeline"] = buildset["pipeline"]
    job = row["builds"][0]
    job["job_name"] = publication["job"]
    for ref in (row["refs"][0], job["ref"]):
        ref["project"] = buildset["project"]
    event.update(build_uuid=job["uuid"], revision=buildset["revision"])
    source = publisher.source_plan(publication, event["revision"])
    registry = publication["registry"]
    manifest = "sha256:" + "1" * 64
    receipt = {"schema_version": 1, "image": registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + manifest,
               "manifest_digest": manifest, "config_digest": "sha256:" + "2" * 64, "output_layer_digest": "sha256:" + "3" * 64,
               "output_tree_sha256": "4" * 64, "base_digest": registry["base_digest"], "source_revision": event["revision"],
               "artifact_publication_verified": True, "publisher_signature_verified": False, "deployment_verified": False,
               "ha_verified": False, "full_stack_verified": False, "source_checkout_witness_bound": True,
               "checkout_witness_sha256": sha256(publisher.source_binding.witness_bytes(source)).hexdigest(),
               "source_plan_sha256": publisher.fingerprint(source)}
    job["artifacts"] = [{"name": publisher.ARTIFACT, "url": registry["origin"] + "/v2/" + registry["repository"] + "/manifests/" + manifest,
                         "metadata": {"schema_version": 1, "producer": "platform-zuul-static-image",
                                      "build_uuid": job["uuid"], "contract_sha256": publisher.fingerprint(contract), "receipt": receipt}}]
    live = deployment()
    live["metadata"].update(uid=DEPLOYMENT_UID, generation=1)
    live["status"] = {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
    cd.objects["deployment.apps/application"] = live
    return plan, contract, row, cd, receipt


class ArtifactPromotionTests(unittest.TestCase):
    def test_requires_artifact_from_exact_final_build_and_reviewed_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, row, _, receipt = promotion_fixture(Path(directory), "argocd")
            artifact.validate(plan, contract)
            policy_path = Path(directory) / "policy.json"
            contract_path = Path(directory) / "publisher.json"
            policy_path.write_text(json.dumps(plan), encoding="utf-8")
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", artifact.__file__, "--plan", str(policy_path),
                                     "--publisher-contract", str(contract_path)], check=True,
                                    text=True, capture_output=True, timeout=20)
            self.assertEqual(json.loads(result.stdout), {"local_inputs_verified": True, "read_only": True,
                                                         "controller_verified": False})
            get = mock.Mock(return_value=row)
            data, proof = artifact.verify(plan, contract, get)
            self.assertEqual(data["zuul_build_uuid"], row["builds"][0]["uuid"])
            self.assertEqual(proof["artifact"], receipt)
            get.assert_called_once()
            with self.assertRaisesRegex(ValueError, "contract"):
                artifact.validate({**plan, "publisher_contract_sha256": "0" * 64}, contract)
            for change in (
                lambda buildset: buildset["builds"].append({**buildset["builds"][0], "final": False}),
                lambda buildset: buildset["builds"][0]["artifacts"][0]["metadata"].update(build_uuid="d" * 32),
                lambda buildset: buildset["builds"][0].update(voting=False),
                lambda buildset: buildset["builds"][0]["artifacts"].append(buildset["builds"][0]["artifacts"][0]),
            ):
                wrong = copy.deepcopy(row)
                change(wrong)
                with self.assertRaises(ValueError):
                    artifact.verify(plan, contract, lambda _: wrong)

    def test_push_and_replay_require_selected_revision_and_original_deployment(self):
        for kind in ("argocd", "flux"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                plan, contract, row, cd, receipt = promotion_fixture(Path(directory), kind)
                git = ImageGit(cd)
                git.image = receipt["image"]
                cd_key = ("application.argoproj.io/application" if kind == "argocd"
                          else "kustomization.kustomize.toolkit.fluxcd.io/application")
                app = cd.objects[cd_key]
                if kind == "argocd":
                    app["status"]["sync"]["revision"] = git.target
                    app["status"]["operationState"]["syncResult"]["revision"] = git.target
                else:
                    revision = "deploy/staging@sha1:" + git.target
                    app["status"]["lastAppliedRevision"] = revision
                    cd.objects["gitrepository.source.toolkit.fluxcd.io/application"]["status"]["artifact"]["revision"] = revision
                get = mock.Mock(return_value=row)
                with mock.patch.object(promotion, "verify_destination"), mock.patch.object(promotion, "ArtifactGit", return_value=git):
                    preview = artifact.execute(plan, contract, cd, get)
                    self.assertEqual(preview["artifact_image"], receipt["image"])
                    self.assertEqual(git.pushes, 0)
                    changed = copy.deepcopy(row)
                    changed["builds"][0]["artifacts"][0]["metadata"]["receipt"]["output_tree_sha256"] = "5" * 64
                    with self.assertRaisesRegex(ValueError, "evidence-changed-before-push"):
                        artifact.execute(plan, contract, cd, mock.Mock(side_effect=[row, changed]), apply=True)
                    self.assertEqual(git.pushes, 0)
                    self.assertTrue(artifact.execute(plan, contract, cd, get, apply=True)["deployment_rollout_verified"])
                    self.assertFalse(artifact.execute(plan, contract, cd, get, apply=True)["branch_push_attempted"])
                    self.assertEqual(git.pushes, 1)
                    cd.objects["deployment.apps/application"]["metadata"]["uid"] = "replacement"
                    with self.assertRaisesRegex(ValueError, "original-artifact-deployment-changed"):
                        artifact.execute(plan, contract, cd, get, apply=True)

    def signed_plan(self, directory, kind):
        plan, contract, row, cd, receipt = promotion_fixture(directory, kind)
        plan["schema_version"] = 2
        plan["signature_policy"] = {
            name: {"path": str(directory / name), "sha256": digest}
            for name, digest in (("cosign_binary", signatures.COSIGN_SHA256),
                                 ("registry_ca", "1" * 64), ("public_key", "2" * 64),
                                 ("trusted_root", "3" * 64), ("credential", "4" * 64))
        }
        return plan, contract, row, cd, receipt

    def test_release_request_binds_final_build_and_target_without_private_file_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, row, _, receipt = self.signed_plan(Path(directory), "argocd")
            get = mock.Mock(return_value=row)
            request = artifact.release_request(plan, contract, get=get)
            get.assert_called_once_with(f"/api/tenant/{plan['buildset']['tenant']}/buildset/{plan['buildset']['uuid']}")
            self.assertEqual(set(request), {"schema_version", "image", "annotations"})
            self.assertEqual(request["image"], receipt["image"])
            annotations = request["annotations"]
            self.assertEqual(annotations["platform.gitops/zuul-build-uuid"], row["builds"][0]["uuid"])
            self.assertEqual(annotations["platform.gitops/zuul-buildset-uuid"], plan["buildset"]["uuid"])
            self.assertEqual(annotations["platform.gitops/publisher-contract-sha256"], publisher.fingerprint(contract))
            self.assertNotIn(directory, json.dumps(request))
            changed = copy.deepcopy(plan)
            changed["signature_policy"]["credential"]["sha256"] = "5" * 64
            self.assertEqual(artifact.release_request(changed, contract, get=get), request)
            changed["artifact_policy"]["expected_deployment_revision"] = "c" * 40
            self.assertNotEqual(artifact.release_request(changed, contract, get=get)["annotations"], annotations)
            for bad in ({**plan, "schema_version": 1}, {k: v for k, v in plan.items() if k != "signature_policy"}):
                with self.assertRaisesRegex(ValueError, "invalid-zuul-artifact-promotion-plan"):
                    artifact.validate(bad, contract)
            unsigned = {k: v for k, v in plan.items() if k != "signature_policy"}
            unsigned["schema_version"] = 1
            with self.assertRaisesRegex(ValueError, "requires-signed"):
                artifact.release_request(unsigned, contract, get=get)

    def test_signed_gate_refuses_unsigned_or_changed_binding_before_git_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, row, cd, _ = self.signed_plan(Path(directory), "flux")
            get = mock.Mock(return_value=row)
            with (mock.patch.object(signatures, "operate", side_effect=ValueError("private output")),
                  mock.patch.object(promotion, "ArtifactGit") as git,
                  self.assertRaisesRegex(ValueError, "merged-ci-image-signature-not-verified")):
                artifact.execute(plan, contract, cd, get, apply=True)
            git.assert_not_called()

            git = ImageGit(cd)
            proof = {"signed_annotations_sha256": "a" * 64}
            outcomes = [proof, {"signed_annotations_sha256": "b" * 64}]
            with (mock.patch.object(promotion, "verify_release_signature", side_effect=outcomes) as verify,
                  mock.patch.object(promotion, "verify_destination"),
                  mock.patch.object(promotion, "ArtifactGit", return_value=git),
                  self.assertRaisesRegex(ValueError, "signed-ci-evidence-changed-before-push")):
                artifact.execute(plan, contract, cd, get, apply=True)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args_list[0], verify.call_args_list[1])
            self.assertEqual(git.pushes, 0)

    def test_reviewed_flux_transform_or_remote_target_cannot_override_image_or_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, contract, _, cd, _ = promotion_fixture(Path(directory), "flux")
            app = cd.objects["kustomization.kustomize.toolkit.fluxcd.io/application"]
            for field, value in (("images", [{"name": "web", "newTag": "other"}]),
                                 ("kubeConfig", {"secretRef": {"name": "another-cluster"}}),
                                 ("targetNamespace", "another-namespace"), ("deletionPolicy", "Delete")):
                with self.subTest(field=field):
                    original = copy.deepcopy(app["spec"])
                    app["spec"][field] = value
                    plan["handoff"]["cd"]["spec_sha256"] = zuul.handoff.sha256(app["spec"])
                    selected, source = artifact.validate(plan, contract)
                    with self.assertRaisesRegex(ValueError, "plain-directory"):
                        artifact.inspect_cd(cd, selected, source)
                    app["spec"] = original


if __name__ == "__main__":
    unittest.main()
