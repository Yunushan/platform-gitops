#!/usr/bin/env python3
"""Artifact promotion policy, scoped evidence, and actual offline Git transactions."""

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gerrit_artifact_promotion as promotion
import gerrit_gitops_handoff as legacy
import render_gerrit_artifact_promotion as renderer
import render_gerrit_promotion_access as access
import tekton_gerrit_reporter as reporter
import test_gerrit_artifact_contract as artifact_tests
from test_ci_gitops_handoff import CDClient
from test_gerrit_gitops_handoff import Reader

OLD_IMAGE = "registry.example.invalid/old@sha256:" + "1" * 64
DEPLOYMENT_UID = "99999999-9999-4999-8999-999999999999"


def deployment(image=OLD_IMAGE):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "application", "namespace": "application"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "application"}}, "template": {"metadata": {"labels": {"app": "application"}},
            "spec": {"containers": [{"name": "app", "image": image, "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}}}]}}}}


class ArtifactCD(CDClient):
    server = "https://kubernetes.default.svc"
    def call(self, arguments):
        if arguments != ["config", "view", "--minify", "-o", "json"]: raise AssertionError("Unexpected local CD command")
        return json.dumps({"current-context": "cd", "contexts": [{"name": "cd", "context": {"cluster": "selected"}}],
            "clusters": [{"name": "selected", "cluster": {"server": self.server}}]})


def fixture(root, runtime="rke2"):
    contract, template, digest, settings, report_digest, ci, run, source, _ = artifact_tests.ArtifactContractTests().evidence_fixture(root, runtime)
    reporter.report_run(ci, source, contract, template, digest, settings, report_digest, run)
    source.row["status"] = "MERGED"
    report, _, _ = reporter.load_bundle(root / "evidence/reporter", report_digest)
    api = Reader(source)
    obj = {"metadata": {"name": "application", "namespace": "argocd", "uid": "88888888-8888-4888-8888-888888888888"},
        "spec": {"source": {"repoURL": contract["plan"]["source"]["clone_url"], "targetRevision": "deploy/staging", "path": "deploy/staging"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "application"}, "syncPolicy": {"automated": {"prune": False}}},
        "status": {"operationState": {"phase": "Succeeded"}}}
    cd = ArtifactCD(); cd.objects["application.argoproj.io/application"] = obj
    cd.objects["namespace/kube-system"] = {"metadata": {"uid": "77777777-7777-4777-8777-777777777777"}}
    live = deployment(); live["metadata"].update(uid=DEPLOYMENT_UID, generation=2)
    live["status"] = {"observedGeneration": 2, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
    cd.objects["deployment.apps/application"] = live
    plan = {"schema_version": 2, "runtime": contract["runtime"], "bridge_contract_sha256": digest, "reporter_contract_sha256": report_digest,
        "source_read_username": "ci-read", "source_read_account_id": 7, "deployment_branch": "deploy/staging", "allow_create_branch": False,
        "allow_prune": False, "git_username": "cd-writer", "timeout_seconds": 30,
        "cd": {"kind": "argocd", "management_target": {"kubeconfig_env": "PLATFORM_CD_KUBECONFIG", "context": "cd", "cluster_uid": "77777777-7777-4777-8777-777777777777"},
            "namespace": "argocd", "name": "application", "uid": obj["metadata"]["uid"], "spec_sha256": promotion.common.sha256(obj["spec"]), "path": "deploy/staging"},
        "artifact_policy": {"manifest": "deploy/staging/workload.json", "namespace": "application", "deployment": "application", "deployment_uid": DEPLOYMENT_UID,
            "container": "app", "expected_image": OLD_IMAGE, "expected_deployment_revision": "b" * 40,
            "commit_name": "Artifact writer", "commit_email": "cd-writer@example.invalid", "destination_server": "https://kubernetes.default.svc"}}
    return plan, report, contract, template, ci, cd, api, run


class Git:
    def __init__(self, cd=None): self.tip = "b" * 40; self.source = "a" * 40; self.target = "c" * 40; self.pushes = 0; self.cd = cd
    def ref(self, branch): return self.source if branch == "main" else self.tip
    def prepare_artifact(self, plan, data, proof):
        if self.tip not in {plan["artifact_policy"]["expected_deployment_revision"], self.target}: raise ValueError("drift")
        return self.tip, self.target
    def promote_artifact(self, branch, target, before):
        attempted = self.tip != target; self.tip = target; self.pushes += int(attempted)
        if self.cd:
            obj = self.cd.objects["deployment.apps/application"]
            obj["spec"]["template"]["spec"]["containers"][0]["image"] = self.image
        return attempted


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.plan, self.report, self.contract, self.template, self.ci, self.cd, self.api, self.run = fixture(self.root)
        self.args = (self.plan, self.report, self.contract, self.template, self.ci, self.cd, self.api, self.run["metadata"]["name"])
        _, _, proof = reporter.evidence(self.ci, self.contract, self.template, self.plan["bridge_contract_sha256"], self.run, self.report["plan"])
        self.image = proof["artifact"]["image"]

    def test_promotion_worker_input_is_single_manifest_and_tests_reviewed_deployment(self):
        from test_gerrit_artifact_promotion_worker_fixture import PromotionChecks, MANIFEST, NAMESPACE
        chosen = PromotionChecks()
        inputs = chosen.source_files({"origin": "https://registry.example.invalid", "repository": "fixture/ha", "base_digest": "sha256:" + "a" * 64})
        document = json.loads(inputs[MANIFEST]); container = promotion.manifest_image(document,
            {"namespace": NAMESPACE, "deployment": "artifact-web", "container": "web"})
        self.assertTrue(container["image"].endswith("@sha256:" + "a" * 64))
        self.assertEqual(set(inputs), {MANIFEST, "test_promotion_input.py"})
        compile(inputs["test_promotion_input.py"], "test_promotion_input.py", "exec")
        self.assertFalse(document["spec"]["template"]["spec"].get("volumes"))
        self.assertEqual(document["spec"]["strategy"]["rollingUpdate"], {"maxSurge": 0, "maxUnavailable": 1})

    def test_explicit_version_and_policy_gates_preserve_legacy_refusal(self):
        promotion.validate(self.plan, self.report, self.contract)
        with self.assertRaisesRegex(ValueError, "artifact-digest-promotion"):
            legacy.validate(self.plan, self.report, self.contract)
        for key, value in (("manifest", "../workload.json"), ("manifest", "deploy/staging/nested/workload.json"), ("expected_image", "registry.example.invalid/old:latest"),
                ("expected_deployment_revision", True), ("expected_deployment_revision", None), ("deployment_uid", "replacement"), ("container", "app/other"), ("commit_name", "Name\nInjection"), ("commit_email", "bad@example.invalid>")):
            changed = copy.deepcopy(self.plan); changed["artifact_policy"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): promotion.validate(changed, self.report, self.contract)
        old = copy.deepcopy(self.contract); old["plan"]["schema_version"] = 1
        with self.assertRaises(ValueError): promotion.validate(self.plan, self.report, old)

    def test_packages_each_control_runtime_and_local_inspection_needs_no_credentials(self):
        for runtime in promotion.bridge.RUNTIMES:
            folder = self.root / runtime; folder.mkdir()
            plan, *_ = fixture(folder, runtime)
            rendered = renderer.render(folder / "evidence/reporter", plan)
            self.assertEqual(json.loads(rendered[promotion.CONTRACT])["plan"]["runtime"], runtime)
        files = renderer.render(self.root / "evidence/reporter", self.plan); bundle = self.root / "promotion"; bundle.mkdir()
        for name, text in files.items(): (bundle / name).write_text(text, encoding="utf-8", newline="\n")
        digest = json.loads(files["artifact-promotion-status.json"])["contract_sha256"]
        promotion.load_bundle(bundle, digest)
        with mock.patch.object(promotion, "Client") as client, mock.patch.object(legacy, "ReadAPI") as api:
            self.assertTrue(promotion.operate(bundle, digest, self.run["metadata"]["name"])["read_only"])
            client.assert_not_called(); api.assert_not_called()
        (bundle / "gerrit_artifact_contract.py").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError): promotion.load_bundle(bundle, digest)

    def signed_plan(self):
        import verify_image_signature as signatures
        self.plan["schema_version"] = 3
        self.plan["signature_policy"] = {name: {"path": str(self.root / name), "sha256": signatures.COSIGN_SHA256 if name == "cosign_binary" else "a" * 64}
            for name in ("cosign_binary", "registry_ca", "public_key", "trusted_root", "credential")}

    def signature_proof(self, selected):
        import verify_image_signature as signatures
        return {"plan_sha256": promotion.common.sha256(selected), "image": selected["image"],
            "cosign_version": signatures.COSIGN_VERSION, "cosign_sha256": signatures.COSIGN_SHA256,
            **{k + "_sha256": selected[k]["sha256"] for k in ("public_key", "trusted_root", "registry_ca")},
            "signed_annotations_sha256": promotion.common.sha256(selected["annotations"]),
            **{k: True for k in ("image_signature_verified", "transparency_inclusion_verified", "registry_tls_verified", "signed_annotations_verified")},
            **{k: False for k in ("admission_verified", "ci_evidence_verified", "release_authorization_verified", "ha_verified", "full_stack_verified")}}

    def test_signed_promotion_version_and_package_bind_only_verifier_not_signer(self):
        self.signed_plan(); promotion.validate(self.plan, self.report, self.contract)
        files = renderer.render(self.root / "evidence/reporter", self.plan)
        selected = json.loads(files[promotion.CONTRACT])
        self.assertIn("verify_image_signature.py", selected["files_sha256"])
        self.assertNotIn("signature_policy", promotion.legacy_plan(self.plan))
        self.assertFalse(any("sign_image" in name for name in files))
        bundle = self.root / "signed-package"; bundle.mkdir()
        for name, content in files.items(): (bundle / name).write_text(content, encoding="utf-8", newline="\n")
        promotion.load_bundle(bundle, json.loads(files["artifact-promotion-status.json"])["contract_sha256"])
        (bundle / "verify_image_signature.py").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "helper-changed"): promotion.load_bundle(bundle, json.loads(files["artifact-promotion-status.json"])["contract_sha256"])
        self.plan["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "explicit-signed"): promotion.validate(self.plan, self.report, self.contract)
        self.plan["schema_version"] = 3; del self.plan["signature_policy"]
        with self.assertRaisesRegex(ValueError, "explicit-signed"): promotion.validate(self.plan, self.report, self.contract)

    def test_signature_annotations_bind_exact_live_ci_and_release_scope(self):
        self.signed_plan()
        data, evidence = legacy.verified_ci(self.ci, self.api, self.report, self.contract, self.template, self.plan, self.run["metadata"]["name"])
        original = promotion.signature_annotations(self.plan, self.contract, data, evidence)
        self.assertEqual(original["platform.gitops/pipeline-run-uid"], data["uid"])
        self.assertEqual(original["platform.gitops/ci-evidence-sha256"], promotion.common.sha256(evidence))
        self.assertFalse(any("https://" in value for value in original.values()))
        for kind in ("evidence", "run", "source", "policy", "image"):
            selected_plan = copy.deepcopy(self.plan); chosen_contract = copy.deepcopy(self.contract)
            chosen_data = copy.deepcopy(data); chosen_proof = copy.deepcopy(evidence)
            if kind == "evidence": chosen_proof["source_reader_account_id"] += 1
            if kind == "run": chosen_data["uid"] = "12345678-1234-4234-8234-123456789012"
            if kind == "source": chosen_contract["plan"]["source"]["project"] += "-other"
            if kind == "policy": selected_plan["deployment_branch"] = "deploy/other"
            if kind == "image": chosen_proof["artifact"]["image"] = self.image.replace("@sha256:", "-other@sha256:")
            with self.subTest(kind=kind): self.assertNotEqual(promotion.signature_annotations(selected_plan, chosen_contract, chosen_data, chosen_proof), original)
        changed = copy.deepcopy(self.plan); changed["signature_policy"]["credential"]["sha256"] = "b" * 64
        self.assertEqual(promotion.signature_annotations(changed, self.contract, data, evidence), original)
        evidence["merged_revision"] = "b" * 40
        with self.assertRaises(ValueError): promotion.signature_annotations(self.plan, self.contract, data, evidence)

    def test_signed_gate_checks_ci_first_then_signature_before_any_git(self):
        import verify_image_signature as signatures
        self.signed_plan()
        with mock.patch.object(signatures, "operate", side_effect=ValueError("private registry response")), mock.patch.object(promotion, "ArtifactGit") as git:
            with self.assertRaisesRegex(ValueError, "signature-not-verified"): promotion.execute(*self.args, apply=True)
            git.assert_not_called()
        self.api.source.row["status"] = "NEW"
        with mock.patch.object(signatures, "operate") as verify, mock.patch.object(promotion, "ArtifactGit") as git:
            with self.assertRaises(ValueError): promotion.execute(*self.args, apply=True)
            verify.assert_not_called(); git.assert_not_called()

    def test_signed_gate_rechecks_before_push_and_never_treats_admission_as_verified(self):
        import verify_image_signature as signatures
        self.signed_plan(); git = Git(self.cd); git.image = self.image; phases = []
        with mock.patch.object(signatures, "operate", side_effect=self.signature_proof) as verify, mock.patch.object(promotion, "ArtifactGit", return_value=git), mock.patch.object(promotion.common, "cd_observed", return_value=True):
            result = promotion.execute(*self.args, apply=True, record=lambda **item: phases.append(item))
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args_list[0].args[0], verify.call_args_list[1].args[0])
        self.assertTrue(result["merged_ci_signature_verified"]); self.assertFalse(result["signed_image_admission_verified"])
        self.assertEqual(git.pushes, 1)
        self.assertIn("signature-verified", [p["phase"] for p in phases]); self.assertIn("signature-rechecked", [p["phase"] for p in phases])
        git = Git()
        def fail_second(selected):
            if fail_second.calls: raise ValueError("signature unavailable before push")
            fail_second.calls += 1
            return self.signature_proof(selected)
        fail_second.calls = 0
        with mock.patch.object(signatures, "operate", side_effect=fail_second), mock.patch.object(promotion, "ArtifactGit", return_value=git), self.assertRaisesRegex(ValueError, "signature-not-verified"):
            promotion.execute(*self.args, apply=True)
        self.assertEqual(git.pushes, 0)

    def test_signature_proof_cannot_substitute_local_inspection_or_another_scope(self):
        import verify_image_signature as signatures
        self.signed_plan()
        for field, value in (("image_signature_verified", False), ("signed_annotations_verified", False), ("registry_tls_verified", False),
                ("transparency_inclusion_verified", False), ("public_key_sha256", "b" * 64), ("plan_sha256", "b" * 64),
                ("signed_annotations_sha256", "b" * 64), ("image", OLD_IMAGE), ("admission_verified", True)):
            def wrong(selected): return {**self.signature_proof(selected), field: value}
            with self.subTest(field=field), mock.patch.object(signatures, "operate", side_effect=wrong), mock.patch.object(promotion, "ArtifactGit") as git:
                with self.assertRaisesRegex(ValueError, "proof-does-not-match"): promotion.execute(*self.args, apply=True)
                git.assert_not_called()

    def test_preflight_never_pushes_and_apply_observes_distinct_commit_and_rollout(self):
        git = Git(self.cd); git.image = self.image; phases = []
        with mock.patch.object(promotion, "ArtifactGit", return_value=git):
            result = promotion.execute(*self.args)
            self.assertTrue(result["read_only"]); self.assertEqual(git.pushes, 0); self.assertEqual(result["prepared_revision"], git.target)
            with mock.patch.object(promotion.common, "cd_observed", return_value=True) as observed:
                result = promotion.execute(*self.args, apply=True, record=lambda **p: phases.append(p))
                observed.assert_called_once(); self.assertEqual(observed.call_args.args[3], git.target)
                replay = promotion.execute(*self.args, apply=True)
        self.assertTrue(result["controller_verified"]); self.assertTrue(result["deployment_rollout_verified"])
        self.assertNotEqual(result["source_revision"], result["promoted_revision"]); self.assertEqual(git.pushes, 1)
        self.assertFalse(replay["branch_push_attempted"]); self.assertFalse(result["full_stack_verified"])
        self.assertEqual(phases[-1]["phase"], "controller-verified")

    def test_missing_artifact_receipt_unmerged_source_or_vote_never_constructs_git(self):
        objects = copy.deepcopy(self.ci.objects); row = copy.deepcopy(self.api.source.row)
        for mode in ("result", "vote", "unmerged", "source-head"):
            self.ci.objects = copy.deepcopy(objects); self.api.source.row = copy.deepcopy(row); self.api.head = "a" * 40
            if mode == "result": self.ci.objects["pipelinerun.tekton.dev/" + self.run["metadata"]["name"]]["status"]["results"] = []
            if mode == "vote": self.api.source.row["labels"]["Verified"]["all"][0]["value"] = -1
            if mode == "unmerged": self.api.source.row["status"] = "NEW"
            if mode == "source-head": self.api.head = "d" * 40
            with self.subTest(mode=mode), mock.patch.object(promotion, "ArtifactGit") as git, self.assertRaises(ValueError): promotion.execute(*self.args, apply=True)
            git.assert_not_called()

    def test_evidence_rechecked_and_source_or_destination_race_does_not_push(self):
        git = Git(); original = legacy.verified_ci(self.ci, self.api, self.report, self.contract, self.template, self.plan, self.run["metadata"]["name"])
        changed = copy.deepcopy(original); changed[1]["artifact"]["manifest_digest"] = "sha256:" + "0" * 64
        with mock.patch.object(promotion, "ArtifactGit", return_value=git), mock.patch.object(legacy, "verified_ci", side_effect=[original, changed]), self.assertRaises(ValueError):
            promotion.execute(*self.args, apply=True)
        self.assertEqual(git.pushes, 0)
        git.tip = "d" * 40
        with mock.patch.object(promotion, "ArtifactGit", return_value=git), self.assertRaises(ValueError): promotion.execute(*self.args, apply=True)
        self.assertEqual(git.pushes, 0)

    def test_controller_transform_destination_and_original_workload_drift_are_refused(self):
        obj = self.cd.objects["application.argoproj.io/application"]; original = copy.deepcopy(obj)
        for mode in ("helm", "kustomize", "directory", "destination", "uid", "active"):
            self.cd.objects["application.argoproj.io/application"] = copy.deepcopy(original); obj = self.cd.objects["application.argoproj.io/application"]
            if mode in {"helm", "kustomize", "directory"}: obj["spec"]["source"][mode] = {}
            if mode == "destination": obj["spec"]["destination"]["server"] = "https://other.example.invalid"
            if mode == "uid": obj["metadata"]["uid"] = "replacement"
            if mode == "active": obj["status"]["operationState"]["phase"] = "Running"
            self.plan["cd"]["spec_sha256"] = promotion.common.sha256(obj["spec"])
            git = Git()
            with self.subTest(mode=mode), mock.patch.object(promotion, "ArtifactGit", return_value=git), self.assertRaises(ValueError): promotion.execute(*self.args, apply=True)
            self.assertEqual(git.pushes, 0)

    def test_native_endpoint_must_match_verified_cd_reader_and_cluster_uid(self):
        endpoint = "https://192.0.2.2:6443"
        self.cd.server = endpoint
        self.plan["artifact_policy"]["destination_server"] = endpoint
        app = self.cd.objects["application.argoproj.io/application"]
        app["spec"]["destination"]["server"] = endpoint
        self.plan["cd"]["spec_sha256"] = promotion.common.sha256(app["spec"])
        git = Git()
        with mock.patch.object(promotion, "ArtifactGit", return_value=git): self.assertTrue(promotion.execute(*self.args)["preflight_verified"])
        for mode in ("endpoint", "cluster"):
            self.cd.server = "https://other.example.invalid" if mode == "endpoint" else endpoint
            self.cd.objects["namespace/kube-system"]["metadata"]["uid"] = "77777777-7777-4777-8777-777777777777" if mode == "endpoint" else "88888888-8888-4888-8888-888888888888"
            with self.subTest(mode=mode), mock.patch.object(promotion, "ArtifactGit") as untouched, self.assertRaises(ValueError): promotion.execute(*self.args, apply=True)
            untouched.assert_not_called()

    def test_rollout_requires_current_generation_image_counts_and_original_uid(self):
        live = self.cd.objects["deployment.apps/application"]; live["spec"]["template"]["spec"]["containers"][0]["image"] = self.image
        self.assertTrue(promotion.deployment_ready(self.cd, self.plan, self.image)); original = copy.deepcopy(live)
        for mode in ("image", "generation", "ready", "replicas", "failure", "uid", "deleting"):
            changed = copy.deepcopy(original)
            if mode == "image": changed["spec"]["template"]["spec"]["containers"][0]["image"] = OLD_IMAGE
            if mode == "generation": changed["status"]["observedGeneration"] = 1
            if mode == "ready": changed["status"]["readyReplicas"] = 1
            if mode == "replicas": changed["status"]["replicas"] = 3
            if mode == "failure": changed["status"]["conditions"] = [{"type": "ReplicaFailure", "status": "True"}]
            if mode == "uid": changed["metadata"]["uid"] = "replacement"
            if mode == "deleting": changed["metadata"]["deletionTimestamp"] = "now"
            self.cd.objects["deployment.apps/application"] = changed
            with self.subTest(mode=mode):
                if mode in {"uid", "deleting"}:
                    with self.assertRaises(ValueError): promotion.deployment_ready(self.cd, self.plan, self.image)
                else: self.assertFalse(promotion.deployment_ready(self.cd, self.plan, self.image))

    def test_artifact_post_push_read_timeout_recovers_without_repush(self):
        git=Git(self.cd);git.image=self.image;records=[]
        with mock.patch.object(promotion,'ArtifactGit',return_value=git),\
                mock.patch.object(promotion.common,'cd_observed',side_effect=[subprocess.TimeoutExpired(['private-read'],1),True]),\
                mock.patch.object(promotion.time,'sleep'):
            result=promotion.execute(*self.args,apply=True,record=lambda **v:records.append(v))
        self.assertTrue(result['deployment_rollout_verified']);self.assertEqual(git.pushes,1)
        self.assertTrue(any('controller_observation_transport_retry' in v for v in records))

    def test_post_push_typed_tls_timeout_recovers_without_repeating_artifact_push(self):
        from kubernetes_management import KubernetesOperationError
        git=Git(self.cd);git.image=self.image;records=[]
        with mock.patch.object(promotion,'ArtifactGit',return_value=git),mock.patch.object(promotion.common,'cd_observed',side_effect=[KubernetesOperationError(1,'net/http: TLS handshake timeout'),True]),mock.patch.object(promotion.time,'sleep'):
            result=promotion.execute(*self.args,apply=True,record=lambda **v:records.append(v))
        self.assertTrue(result['deployment_rollout_verified']);self.assertEqual(git.pushes,1)
        self.assertTrue(any('controller_observation_transport_retry' in v for v in records))

    def test_late_success_cannot_complete_an_expired_observation_deadline(self):
        git=Git(self.cd);git.image=self.image
        with mock.patch.object(promotion,'ArtifactGit',return_value=git),\
                mock.patch.object(promotion.common,'cd_observed',return_value=True),\
                mock.patch.object(promotion.time,'monotonic',side_effect=[0,31]),\
                self.assertRaisesRegex(ValueError,'rollout-not-verified'):
            promotion.execute(*self.args,apply=True)
        self.assertEqual(git.pushes,1);self.assertEqual(git.tip,git.target)

    def test_artifact_read_timeout_stays_bounded_and_keeps_promoted_ref(self):
        git=Git(self.cd);git.image=self.image
        with mock.patch.object(promotion,'ArtifactGit',return_value=git),\
                mock.patch.object(promotion.common,'cd_observed',side_effect=subprocess.TimeoutExpired(['private-read'],1)),\
                mock.patch.object(promotion.time,'monotonic',side_effect=[0,31]),\
                self.assertRaisesRegex(ValueError,'rollout-not-verified'):
            promotion.execute(*self.args,apply=True)
        self.assertEqual(git.pushes,1);self.assertEqual(git.tip,git.target)

    def test_controller_success_alone_or_stale_rollout_never_reports_success(self):
        original = copy.deepcopy(self.cd.objects["deployment.apps/application"])
        for mode in ("image", "generation", "controller"):
            self.cd.objects["deployment.apps/application"] = copy.deepcopy(original)
            git = Git(None if mode == "image" else self.cd); git.image = self.image
            if mode == "generation": self.cd.objects["deployment.apps/application"]["status"]["observedGeneration"] = 1
            phases = []
            with self.subTest(mode=mode), mock.patch.object(promotion, "ArtifactGit", return_value=git), \
                    mock.patch.object(promotion.common, "cd_observed", return_value=mode != "controller"), \
                    mock.patch.object(promotion.time, "monotonic", side_effect=[0, 31]), \
                    self.assertRaisesRegex(ValueError, "rollout-not-verified"):
                promotion.execute(*self.args, apply=True, record=lambda **p: phases.append(p))
            self.assertEqual(git.pushes, 1)
            self.assertEqual(phases[-1]["phase"], "git-promoted")
            self.assertFalse(any(p.get("controller_verified") is True for p in phases))

    def test_artifact_access_adds_only_named_deployment_get_in_workload_namespace(self):
        files = renderer.render(self.root / "evidence/reporter", self.plan); bundle = self.root / "promotion"; bundle.mkdir()
        for name, text in files.items(): (bundle / name).write_text(text, encoding="utf-8", newline="\n")
        digest = json.loads(files["artifact-promotion-status.json"])["contract_sha256"]
        policy = {"schema_version": 1, "ci": {"namespace": self.contract["plan"]["submitter_namespace"], "account": "ci-promotion-reader", "workers": ["worker-1"]},
            "cd": {"namespace": "argocd", "account": "artifact-rollout-reader"}}
        selected = access.render(bundle, digest, policy)
        objects = json.loads(selected["cd-reader-rbac.json"])["items"]
        roles = [r for r in objects if r["kind"] == "Role" and r["metadata"]["namespace"] == "application"]
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["rules"], [{"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["application"], "verbs": ["get"]}])
        for obj in objects:
            for rule in obj.get("rules", []): self.assertEqual(rule["verbs"], ["get"])
        (bundle / "gerrit-handoff-contract.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError): access.render(bundle, digest, policy)

    def test_cli_errors_never_echo_private_plan_fields(self):
        output = io.StringIO()
        with mock.patch.object(promotion, "load_bundle", side_effect=ValueError("private-value-never-log")), contextlib.redirect_stderr(output):
            self.assertEqual(promotion.main(["--bundle-dir", str(self.root), "--contract-sha256", "0" * 64, "--pipeline-run", "run"]), 1)
        self.assertNotIn("private-value-never-log", output.getvalue())


@unittest.skipUnless(os.name == "posix" and shutil.which("git"), "Actual Git objects and local transport require the Linux test fixture")
class NativeWatchOwnershipTests(unittest.TestCase):
    def test_original_process_identity_distinguishes_live_pid_reuse_later_boot_and_removal(self):
        from test_gerrit_native_watch_fixture import original_process_absent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boot = root / "sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True)
            boot.write_text("original-boot\n")
            proc = root / "321"
            proc.mkdir()
            # /proc fields after the command begin at state (field 3).
            fields = ["S"] + ["0"] * 18 + ["700"]
            stat = proc / "stat"
            stat.write_text("321 (python (watch)) " + " ".join(fields) + "\n")
            original = {"pid": 321, "start_ticks": 700, "boot_id": "original-boot"}
            self.assertFalse(original_process_absent(original, root))
            fields[19] = "701"
            stat.write_text("321 (python (watch)) " + " ".join(fields) + "\n")
            self.assertTrue(original_process_absent(original, root))
            fields[19] = "700"
            stat.write_text("321 (python (watch)) " + " ".join(fields) + "\n")
            boot.write_text("later-boot\n")
            self.assertTrue(original_process_absent(original, root))
            boot.write_text("original-boot\n")
            stat.unlink()
            self.assertTrue(original_process_absent(original, root))

    def test_unreadable_or_malformed_process_identity_never_proves_cleanup(self):
        from test_gerrit_native_watch_fixture import original_process_absent
        original = {"pid": 321, "start_ticks": 700, "boot_id": "original-boot"}
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("synthetic refusal")):
            with self.assertRaises(PermissionError): original_process_absent(original)
        with mock.patch.object(Path, "read_text", side_effect=["original-boot\n", "malformed"]):
            with self.assertRaises((IndexError, ValueError)): original_process_absent(original)

    def test_artifact_watch_requires_explicit_native_promotion_and_separate_acceptance(self):
        from test_gerrit_worker_runtime_fixture import exercise as workflow
        from test_gerrit_artifact_promotion_worker_fixture import exercise
        for selected in ({}, {"promotion": False}, {"promotion": True, "native_promoter": False}):
            with self.subTest(context=selected), self.assertRaises(ValueError):
                workflow(self, {}, native_watch=True, publication_context=selected)
        with self.assertRaises(ValueError): exercise(self, native_watch=True)


class ActualGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup); self.root = Path(self.temporary.name)
        self.repo = self.root / "remote.git"; self.source = self.root / "source"; self.source.mkdir()
        self.git(["init", "--bare", str(self.repo)]); self.git(["init", "-b", "main", str(self.source)])
        self.git(["config", "user.name", "Synthetic fixture"], self.source); self.git(["config", "user.email", "fixture@example.invalid"], self.source)
        path = self.source / "deploy/staging"; path.mkdir(parents=True); (path / "workload.json").write_text(json.dumps(deployment()), encoding="utf-8")
        (self.source / "source.txt").write_text("approved source", encoding="utf-8")
        self.git(["add", "."], self.source); self.git(["commit", "-m", "Initial fixture"], self.source)
        self.before = self.git(["rev-parse", "HEAD"], self.source).strip()
        self.git(["push", str(self.repo), "HEAD:refs/heads/deploy/staging"], self.source)
        (self.source / "source.txt").write_text("approved new source", encoding="utf-8")
        self.git(["add", "."], self.source); self.git(["commit", "-m", "Approved merged build"], self.source)
        self.revision = self.git(["rev-parse", "HEAD"], self.source).strip(); self.git(["push", str(self.repo), "HEAD:refs/heads/main"], self.source)
        self.plan = {"artifact_policy": {"manifest": "deploy/staging/workload.json", "namespace": "application", "deployment": "application", "container": "app",
                "expected_image": OLD_IMAGE, "expected_deployment_revision": self.before,
                "commit_name": "Artifact writer", "commit_email": "cd-writer@example.invalid"}, "deployment_branch": "deploy/staging", "allow_create_branch": False, "cd": {"path": "deploy/staging"}}
        self.data = {"revision": self.revision, "uid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        self.evidence = {"artifact": {"image": "registry.example.invalid/fixture/ha@sha256:" + "f" * 64}}
        self.counter = 0

    def git(self, arguments, directory=None):
        r = subprocess.run(["git", *arguments], cwd=directory, capture_output=True, text=True, check=True, timeout=20)
        return r.stdout

    def client(self):
        self.counter += 1; directory = self.root / ("objects-" + str(self.counter)); directory.mkdir()
        client = promotion.ArtifactGit(str(self.repo), "fixture-writer", self.root, directory)
        # Only this offline test instance permits file transport. Production
        # retains the shared HTTPS-only CA/askpass environment unchanged.
        client.env["GIT_ALLOW_PROTOCOL"] = "file"
        return client

    def test_actual_objects_change_only_image_and_preserve_both_ancestors_and_source(self):
        client = self.client(); before, target = client.prepare_artifact(self.plan, self.data, self.evidence)
        self.assertEqual(before, self.before); self.assertNotEqual(target, self.revision)
        document = json.loads(client.call(["show", target + ":deploy/staging/workload.json"]).stdout)
        expected = deployment(self.evidence["artifact"]["image"]); self.assertEqual(document, expected)
        self.assertEqual(client.call(["show", target + ":source.txt"]).stdout, "approved new source")
        self.assertEqual(client.call(["show", "-s", "--format=%an <%ae>|%cn <%ce>", target]).stdout.strip(),
            "Artifact writer <cd-writer@example.invalid>|Artifact writer <cd-writer@example.invalid>")
        for ancestor in (self.before, self.revision): self.assertEqual(client.call(["merge-base", "--is-ancestor", ancestor, target]).returncode, 0)
        self.assertEqual(client.call(["diff-tree", "--no-commit-id", "--name-only", "-r", self.revision, target]).stdout.strip(), "deploy/staging/workload.json")
        self.assertTrue(client.promote_artifact(self.plan["deployment_branch"], target, self.before))
        duplicate = self.client(); current, repeated = duplicate.prepare_artifact(self.plan, self.data, self.evidence)
        self.assertEqual((current, repeated), (target, target)); self.assertFalse(duplicate.promote_artifact(self.plan["deployment_branch"], repeated, self.before))
        self.assertEqual(self.git(["show", self.revision + ":deploy/staging/workload.json"], self.source), json.dumps(deployment()))

    def test_actual_raced_push_is_rejected_without_retry_or_rewriting_history(self):
        client = self.client(); _, target = client.prepare_artifact(self.plan, self.data, self.evidence)
        original = client.call; attempts = []
        def raced(arguments, **kwargs):
            if arguments[0] == "push":
                attempts.append(1); self.git(["update-ref", "refs/heads/deploy/staging", self.revision], self.repo)
            return original(arguments, **kwargs)
        with mock.patch.object(client, "call", side_effect=raced), self.assertRaisesRegex(ValueError, "push-not-verified"):
            client.promote_artifact(self.plan["deployment_branch"], target, self.before)
        self.assertEqual(len(attempts), 1); self.assertEqual(client.ref(self.plan["deployment_branch"]), self.revision)
        with self.assertRaises(ValueError): self.client().prepare_artifact(self.plan, self.data, self.evidence)

    def test_actual_zuul_commit_records_build_uuid_and_has_stable_replay(self):
        data = {"revision": self.revision, "zuul_build_uuid": "a" * 32}
        client = self.client()
        before, target = client.prepare_artifact(self.plan, data, self.evidence)
        body = client.call(["show", "-s", "--format=%b", target]).stdout
        message = json.loads(body)
        self.assertEqual(message["zuul_build_uuid"], data["zuul_build_uuid"])
        self.assertNotIn("pipeline_run_uid", message)
        self.assertEqual(message["artifact"]["image"], self.evidence["artifact"]["image"])
        self.assertTrue(client.promote_artifact("deploy/staging", target, before))
        self.assertEqual(self.client().prepare_artifact(self.plan, data, self.evidence), (target, target))

    def test_actual_flux_build_file_selects_only_the_reviewed_deployment(self):
        self.plan["cd"]["kind"] = "flux"
        with self.assertRaisesRegex(ValueError, "flux-artifact-directory-requires-kustomization"):
            self.client().prepare_artifact(self.plan, self.data, self.evidence)
        path = self.source / "deploy/staging/kustomization.yaml"
        header = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n- workload.json\n"
        for body in (header, header + "- https://example.invalid/remote.yaml\n"):
            path.write_text(body, encoding="utf-8")
            self.git(["add", "."], self.source)
            self.git(["commit", "-m", "Reviewed Flux build file"], self.source)
            revision = self.git(["rev-parse", "HEAD"], self.source).strip()
            self.git(["push", str(self.repo), "HEAD:refs/heads/main"], self.source)
            client = self.client()
            data = {"revision": revision, "zuul_build_uuid": "a" * 32}
            if body == header:
                _, target = client.prepare_artifact(self.plan, data, self.evidence)
                self.assertEqual(client.call(["show", target + ":deploy/staging/kustomization.yaml"]).stdout, header)
            else:
                with self.assertRaisesRegex(ValueError, "flux-build-file-must-select-only"):
                    client.prepare_artifact(self.plan, data, self.evidence)

    def test_actual_unreviewed_extra_manifest_duplicate_json_and_symlink_are_refused(self):
        path = self.source / "deploy/staging/workload.json"
        for mode in ("extra", "duplicate", "image", "symlink"):
            self.git(["reset", "--hard", self.revision], self.source)
            extra = path.parent / "extra.json"
            if extra.exists(): extra.unlink()
            if mode == "extra": extra.write_text("{}", encoding="utf-8")
            if mode == "duplicate": path.write_text('{"kind":"Deployment","kind":"Service"}', encoding="utf-8")
            if mode == "image": path.write_text(json.dumps(deployment("changed")), encoding="utf-8")
            if mode == "symlink": path.write_text("../../source.txt", encoding="utf-8")
            self.git(["add", "."], self.source)
            if mode == "symlink":
                # Test the actual Git mode without requiring Windows link privileges.
                blob = self.git(["hash-object", "-w", str(path)], self.source).strip()
                self.git(["update-index", "--cacheinfo", "120000," + blob + ",deploy/staging/workload.json"], self.source)
                self.assertTrue(self.git(["ls-files", "--stage", "--", "deploy/staging/workload.json"], self.source).startswith("120000 "))
            self.git(["commit", "-m", "Invalid fixture " + mode], self.source)
            bad = self.git(["rev-parse", "HEAD"], self.source).strip(); self.git(["push", str(self.repo), "HEAD:refs/heads/case-" + mode], self.source)
            expected = "artifact-directory-must-contain-only-reviewed-regular-json-manifest" if mode == "symlink" else ""
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, expected): self.client().prepare_artifact(self.plan, {**self.data, "revision": bad}, self.evidence)
            self.assertEqual(self.git(["rev-parse", "refs/heads/deploy/staging"], self.repo).strip(), self.before)

    def test_actual_new_branch_is_create_once_and_replay_keeps_exact_commit(self):
        self.plan["artifact_policy"]["expected_deployment_revision"] = None; self.plan["deployment_branch"] = "deploy/new"; self.plan["allow_create_branch"] = True
        client = self.client(); before, target = client.prepare_artifact(self.plan, self.data, self.evidence); self.assertIsNone(before)
        self.assertTrue(client.promote_artifact("deploy/new", target, None))
        duplicate = self.client(); self.assertEqual(duplicate.prepare_artifact(self.plan, self.data, self.evidence), (target, target))
        self.assertFalse(duplicate.promote_artifact("deploy/new", target, None))


if __name__ == "__main__": unittest.main()
