import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bounded_subprocess import run_bounded
import prepare_woodpecker_publisher_image as image_context
import woodpecker_artifact_promotion as artifact
import woodpecker_gitops_handoff as subject
import woodpecker_static_image as publisher


class PublisherImageTests(unittest.TestCase):
    def test_context_pins_a_nonroot_entrypoint_with_only_the_publisher_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "private").mkdir()
            (root / "scripts").mkdir()
            for name, payload in image_context.program_files().items():
                (root / "scripts" / name).write_bytes(payload)
            target = root / "private" / "publisher"
            base = "docker.io/library/python@sha256:" + "a" * 64
            with self.assertRaises(ValueError):
                image_context.prepare("python:latest", target, root=root)
            with self.assertRaises(ValueError):
                image_context.prepare(base, root / "public", root=root)
            manifest = image_context.prepare(base, target, root=root)
            self.assertEqual(set(target.iterdir()), {target / name for name in (*image_context.PROGRAMS, "Dockerfile", "context-manifest.json")})
            dockerfile = (target / "Dockerfile").read_text()
            self.assertIn(f"FROM {base}\n", dockerfile)
            self.assertIn("USER 1000:1000\n", dockerfile)
            self.assertIn("ENTRYPOINT " + json.dumps(image_context.ENTRYPOINT), dockerfile)
            self.assertEqual(manifest["source_sha256"], {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                                                       for name in image_context.PROGRAMS})
            # A standard-library-only interpreter must load the exact bundled entrypoint.
            result = run_bounded([sys.executable, "-ESB", str(target / "woodpecker_static_image.py"), "--help"],
                                 text=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--plugin", result.stdout)
            with self.assertRaises(FileExistsError):
                image_context.prepare(base, target, root=root)


class WoodpeckerHandoffTests(unittest.TestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[1] / "examples/woodpecker-argocd-handoff.json"
        self.plan = json.loads(example.read_text(encoding="utf-8"))
        self.ci = self.plan["pipeline"]
        self.policy = self.plan["handoff"]
        self.policy["bridge_contract_sha256"] = subject.handoff.sha256(self.ci)
        self.policy["cd"]["spec_sha256"] = "a" * 64
        contract = subject.validate(self.plan)
        self.clone_url = contract["plan"]["source"]["clone_url"]
        self.forge = {
            "/repos/platform/example": {"id": 25, "full_name": "platform/example", "clone_url": self.clone_url},
            "/repos/platform/example/branches/main": {
                "name": "main", "protected": True, "commit": {"id": self.ci["revision"]},
            },
        }
        steps = [
            {"id": index, "name": name, "state": "success", "exit_code": 0, "finished": 1}
            for index, name in enumerate(("git", "verify-checkout"), 1)
        ]
        self.pipeline = {
            "id": 55, "number": 42, "commit": self.ci["revision"], "branch": "main", "ref": "refs/heads/main",
            "status": "success", "event": "push", "rerun_count": 0, "from_fork": False, "finished": 1,
            "workflows": [{"id": 1, "name": "woodpecker", "state": "success", "finished": 1, "children": steps}],
        }
        self.woodpecker = {
            "/repos/17": {"id": 17, "forge_id": 1, "forge_remote_id": "25", "full_name": "platform/example",
                          "clone_url": self.clone_url, "active": True},
            "/forges/1": {"id": 1, "type": "gitea", "url": self.ci["forge_origin"]},
            "/repos/17/pipelines/42": self.pipeline,
        }

    def verify(self):
        return subject.verify_pipeline(self.ci, self.clone_url, self.woodpecker.__getitem__, self.forge.__getitem__)

    def test_exact_head_and_required_steps(self):
        del self.pipeline["from_fork"]  # Woodpecker omits false here.
        self.pipeline["errors"] = [{"type": "bad_habit", "is_warning": True}]
        proof = self.verify()
        self.assertEqual(proof["revision"], self.ci["revision"])
        self.assertEqual(proof["workflows"]["woodpecker"]["steps"]["verify-checkout"]["id"], 2)
        self.pipeline["errors"][0]["is_warning"] = False
        with self.assertRaisesRegex(ValueError, "reported-errors"):
            self.verify()

    def test_unprotected_or_moved_branch_rejected(self):
        branch = self.forge["/repos/platform/example/branches/main"]
        branch["protected"] = False
        with self.assertRaisesRegex(ValueError, "unprotected-or-head-changed"):
            self.verify()
        branch.update(protected=True, commit={"id": "b" * 40})
        with self.assertRaisesRegex(ValueError, "unprotected-or-head-changed"):
            self.verify()

    def test_unrelated_repo_or_forge_rejected(self):
        repo = self.woodpecker["/repos/17"]
        repo["forge_remote_id"] = "26"
        with self.assertRaisesRegex(ValueError, "repository-identity"):
            self.verify()
        repo["forge_remote_id"] = "25"
        self.woodpecker["/forges/1"]["skip_verify"] = True
        with self.assertRaisesRegex(ValueError, "forge-identity"):
            self.verify()

    def test_pipeline_trigger_and_outcome_are_bound(self):
        original = copy.deepcopy(self.pipeline)
        for key, value in (("event", "manual"), ("rerun_count", 1), ("from_fork", True),
                           ("status", "failure"), ("commit", "b" * 40), ("ref", "refs/heads/another")):
            with self.subTest(key=key):
                self.pipeline.update(original)
                self.pipeline[key] = value
                with self.assertRaisesRegex(ValueError, "pipeline-identity-or-status"):
                    self.verify()

    def test_missing_ambiguous_skipped_or_failed_step_rejected(self):
        steps = self.pipeline["workflows"][0]["children"]
        step = steps.pop()
        with self.assertRaisesRegex(ValueError, "missing-or-ambiguous"):
            self.verify()
        steps.extend([step, copy.deepcopy(step)])
        with self.assertRaisesRegex(ValueError, "missing-or-ambiguous"):
            self.verify()
        steps.pop()
        for state, exit_code in (("skipped", 0), ("success", 1)):
            step.update(state=state, exit_code=exit_code)
            with self.assertRaisesRegex(ValueError, "step-not-successful"):
                self.verify()

    def test_unsafe_origins_and_stale_policy_digest_rejected(self):
        for value in ("http://ci.example.invalid", "https://user@ci.example.invalid",
                      "https://ci.example.invalid/path", "https://ci.example.invalid:0",
                      "https://ci.example.invalid\\invalid", "https://ci.example.invalid?key=value"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject.origin(value)
        self.ci["number"] = 43
        with self.assertRaisesRegex(ValueError, "plan-digest"):
            subject.validate(self.plan)

    def test_promotion_receives_stable_proof(self):
        def execute(plan, contract, _template, _ci, client, run, *, verify_ci, **options):
            self.assertEqual(run, "42")
            self.assertTrue(options["apply"])
            result, proof = verify_ci(None, contract, None, plan["bridge_contract_sha256"], run)
            self.assertEqual(result, {"revision": self.ci["revision"], "uid": "55"})
            self.assertEqual(proof, self.verify())
            return {"controller_verified": True}

        with patch.object(subject.handoff, "execute", side_effect=execute):
            result = subject.execute(self.plan, None, self.woodpecker.__getitem__, self.forge.__getitem__, apply=True)
        self.assertTrue(result["controller_verified"])


class SignedWoodpeckerTests(unittest.TestCase):
    def setUp(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        self.fixture = WoodpeckerHandoffTests()
        self.fixture.setUp()
        self.plan = json.loads((examples / "woodpecker-argocd-artifact-promotion.json").read_bytes())
        self.contract = json.loads((examples / "woodpecker-static-image.json").read_bytes())
        self.ci = self.plan["pipeline"]
        self.plan["handoff"].update(bridge_contract_sha256=subject.handoff.sha256(self.ci))
        self.plan["handoff"]["cd"]["spec_sha256"] = "a" * 64
        self.key = bytes(range(32))
        self.contract["receipt_key_sha256"] = hashlib.sha256(self.key).hexdigest()
        self.contract["woodpecker_ca_sha256"] = self.contract["source"]["ca_sha256"] = "b" * 64
        for key in ("ca_sha256", "leaf_sha256", "credential_sha256"):
            self.contract["registry"][key] = "b" * 64
        for name, ref in self.plan["signature_policy"].items():
            if name != "cosign_binary":
                ref["sha256"] = "c" * 64
            if os.name != "posix":
                ref["path"] = str(Path.cwd() / (name + ".fixture"))
        self.step_id = 19
        self.fixture.pipeline["workflows"][0]["children"].append({"id": self.step_id, "name": self.contract["step"],
                                                                  "state": "success", "exit_code": 0, "finished": 10})
        digest = "sha256:" + "d" * 64
        self.receipt = {"schema_version": 1, "manifest_digest": digest, "config_digest": digest, "output_layer_digest": digest,
                        "image": "registry.example.invalid/applications/example@" + digest, "source_revision": self.ci["revision"],
                        "base_digest": self.contract["registry"]["base_digest"], "output_tree_sha256": "e" * 64,
                        "artifact_publication_verified": True, "publisher_signature_verified": False,
                        "deployment_verified": False, "ha_verified": False, "full_stack_verified": False}
        self.log_path = f"/repos/17/logs/42/{self.step_id}"
        self.prepare_receipt()
        self.ci_get = Mock(side_effect=self.fixture.woodpecker.__getitem__)
        self.forge_get = self.fixture.forge.__getitem__

    def prepare_receipt(self):
        self.plan["publisher_contract_sha256"] = publisher.fingerprint(self.contract)
        job = publisher.identity(self.contract, self.ci["number"], self.ci["revision"])
        self.envelope = {"receipt": publisher.receipt_body(self.contract, job, self.receipt)}
        self.envelope["mac_sha256"] = publisher.authenticate_receipt(self.envelope["receipt"], self.key)
        self.fixture.woodpecker[self.log_path] = [self.log(self.envelope)]

    def log(self, envelope, *, step_id=None, kind=0):
        return {"id": 7, "step_id": self.step_id if step_id is None else step_id, "line": 1, "time": 1, "type": kind,
                "data": base64.b64encode(publisher.PREFIX + publisher.harbor.canonical(envelope) + b"\n").decode()}

    def request(self):
        return artifact.release_request(self.plan, self.contract, self.key, self.ci_get, self.forge_get)

    def test_both_forges_bind_the_exact_successful_step(self):
        for provider in ("forgejo", "gitea"):
            with self.subTest(provider=provider):
                self.ci["provider"] = self.contract["provider"] = self.fixture.woodpecker["/forges/1"]["type"] = provider
                self.plan["handoff"]["bridge_contract_sha256"] = subject.handoff.sha256(self.ci)
                self.prepare_receipt()
                request = self.request()
                self.assertEqual(request["image"], self.receipt["image"])
                self.assertEqual(request["annotations"]["platform.gitops/woodpecker-step-id"], str(self.step_id))
                self.assertEqual(request["annotations"]["platform.gitops/woodpecker-pipeline-id"], "55")
                self.assertEqual(sum(call.args == (self.log_path,) for call in self.ci_get.call_args_list), 1)
                self.assertNotIn(self.envelope["mac_sha256"], json.dumps(request))
                self.ci_get.reset_mock()

    def test_unauthenticated_ambiguous_or_misbound_logs_fail(self):
        wrong_run = copy.deepcopy(self.envelope)
        wrong_run["receipt"]["job"]["number"] = 41
        wrong_run["mac_sha256"] = publisher.authenticate_receipt(wrong_run["receipt"], self.key)
        tampered = {**self.envelope, "mac_sha256": "a" * 64}
        for logs, reason in (([self.log(tampered)], "authentication-failed"), ([self.log(wrong_run)], "scope-mismatch"),
                             ([self.log(self.envelope)] * 2, "missing-or-ambiguous"), ([self.log(self.envelope, kind=1)], "missing-or-ambiguous"),
                             ([self.log(self.envelope, step_id=self.step_id - 1)], "identity-mismatch")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.fixture.woodpecker[self.log_path] = logs
                self.request()

    def test_initially_empty_logs_are_retried_for_the_exact_step(self):
        expected = self.fixture.woodpecker[self.log_path]
        log_reads = iter((None, [], expected))

        def get_ci(path):
            return next(log_reads) if path == self.log_path else self.fixture.woodpecker[path]

        self.ci_get.side_effect = get_ci
        with patch.object(artifact.time, "sleep") as sleep:
            self.assertEqual(self.request()["image"], self.receipt["image"])
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sum(call.args == (self.log_path,) for call in self.ci_get.call_args_list), 3)

    def test_missing_logs_deadline_refuses_git(self):
        self.fixture.woodpecker[self.log_path] = None
        with (patch.object(artifact.time, "monotonic", side_effect=(10, 15)),
              patch.object(artifact.promotion, "ArtifactGit") as git,
              self.assertRaisesRegex(ValueError, "log-not-persisted")):
            artifact.execute(self.plan, self.contract, self.key, None, self.ci_get, self.forge_get, apply=True)
        git.assert_not_called()

    def test_changed_live_pipeline_and_missing_release_signature_refuse_git(self):
        def changed(path):
            if path == self.log_path:
                self.fixture.pipeline["finished"] += 1
            return self.fixture.woodpecker[path]
        with patch.object(artifact.promotion, "ArtifactGit") as git, self.assertRaisesRegex(ValueError, "pipeline-changed-during-artifact-read"):
            artifact.execute(self.plan, self.contract, self.key, None, changed, self.forge_get, apply=True)
        git.assert_not_called()
        with (patch.object(artifact.promotion, "ArtifactGit") as git,
              patch.object(artifact.promotion, "verify_release_signature", side_effect=ValueError("signature-missing")),
              self.assertRaisesRegex(ValueError, "signature-missing")):
            artifact.execute(self.plan, self.contract, self.key, None, self.ci_get, self.forge_get, apply=True)
        git.assert_not_called()

    def test_plugin_materializes_scoped_inputs_without_inheriting_bundle(self):
        files = {"source_ca": ("source/ca.crt", b"source-ca"), "source_token": ("source/token", b"source-reader-token"),
                 "registry_ca": ("registry/ca.crt", b"registry-ca"), "registry_credential": ("registry/credential.json", b"registry-input"),
                 "receipt_key": ("receipt-key", self.key)}
        encode = lambda value: base64.b64encode(value).decode("ascii")
        env = {"CI": "woodpecker", "PLUGIN_CONTRACT_B64": encode(publisher.harbor.canonical(self.contract)),
               "PLUGIN_CONTRACT_SHA256": publisher.fingerprint(self.contract),
               "PLUGIN_PRIVATE_INPUTS_B64": encode(publisher.harbor.canonical({name: encode(value) for name, (_, value) in files.items()}))}

        def publish(contract, inputs, job_env):
            self.assertEqual(contract, self.contract)
            self.assertEqual(job_env, {"CI": "woodpecker"})
            for path, expected in files.values():
                path = inputs / path
                self.assertEqual(path.read_bytes(), expected)
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return inputs

        with patch.object(publisher, "publish", side_effect=publish):
            path = publisher.plugin(env)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
