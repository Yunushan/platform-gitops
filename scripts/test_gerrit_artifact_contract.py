#!/usr/bin/env python3
"""Optional automated Gerrit artifact contracts and rejection regressions."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

import gerrit_artifact_contract as artifact
import publish_harbor_static_image as publisher
import render_tekton_gerrit_bridge as renderer
import source_bound_static_image as binding
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter
from test_harbor_static_image import plan_fixture as registry_fixture
from test_tekton_gerrit_bridge import fixture as source_fixture, package
from test_tekton_gerrit_reporter import fixture as reporter_fixture


def fixture(root):
    plan = source_fixture(); registry, _ = registry_fixture(root)
    registry.update(ca_file="/publisher-input/ca.crt", credential_file="/run/robot/credential.json"); registry.pop("revision")
    plan.update(schema_version=2, publication={"publisher_image": plan["build_image"], "robot_secret": "ci-publisher-robot", "trust_secret": "ci-publisher-ca", "registry": registry})
    plan["build_argv"] = ["python3", "-I", "-B", "/workspace/source/build.py"]
    return plan


def receipt(plan, revision="a" * 40, ref="refs/changes/23/123/2"):
    source = artifact.source_plan(plan, revision, ref); registry = plan["publication"]["registry"]
    value = {"schema_version": 1, "image": registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@sha256:" + "f" * 64,
        "manifest_digest": "sha256:" + "f" * 64, "config_digest": "sha256:" + "e" * 64, "output_layer_digest": "sha256:" + "d" * 64,
        "output_tree_sha256": "c" * 64, "base_digest": registry["base_digest"], "source_revision": revision,
        "artifact_publication_verified": True, "publisher_signature_verified": False, "deployment_verified": False, "ha_verified": False, "full_stack_verified": False,
        "source_checkout_witness_bound": True, "checkout_witness_sha256": hashlib.sha256(binding.witness_bytes(source)).hexdigest(),
        "source_plan_sha256": hashlib.sha256(publisher.canonical(source)).hexdigest()}
    return {"status": {"results": [{"name": "image-receipt", "type": "string", "value": json.dumps(value)}]}}, value


class ArtifactContractTests(unittest.TestCase):
    def test_v1_retains_exact_two_steps_and_original_program_inventory(self):
        plan = source_fixture(); files = renderer.render(plan, "raw")
        template = json.loads(files["run-template.json"])
        self.assertEqual([s["name"] for s in template["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]["steps"]], ["checkout", "build"])
        self.assertEqual(set(json.loads(files["gerrit-event-contract.json"])["files_sha256"]), {*bridge.PROGRAMS, "run-template.json"})

    def test_v2_packages_all_runtimes_and_compiles_trusted_step_bootstraps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan = fixture(root)
            for runtime in bridge.RUNTIMES:
                files = renderer.render(plan, runtime)
                self.assertEqual(set(json.loads(files["gerrit-event-contract.json"])["files_sha256"]), {*bridge.programs(plan), "run-template.json"})
                template = json.loads(files["run-template.json"]); task = template["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]
                self.assertEqual([s["name"] for s in task["steps"]], ["checkout", "build", "publish"])
                for step in (task["steps"][0], task["steps"][2]): compile(step["command"][-1], "trusted-step.py", "exec")
                self.assertEqual({m["name"] for m in task["steps"][1]["volumeMounts"]}, {"output", "build-tmp", "source-tree"})
                self.assertEqual(template["spec"]["pipelineSpec"]["results"][0]["value"], "$(tasks.build.results.image-receipt)")
                self.assertEqual(template["spec"]["pipelineSpec"]["results"][0]["description"], "")
                self.assertNotIn("unused-", json.dumps(template))

    @unittest.skipUnless(os.name == "posix", "Actual Linux worker argv and isolated Python imports")
    def test_bootstrap_imports_the_packaged_programs_and_refuses_unknown_stage(self):
        from render_gerrit_harbor_static_task import programs
        code = artifact.bootstrap(programs())
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, "invalid-stage", "{}", "", "unused"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1); self.assertEqual(result.stderr, ""); self.assertEqual(result.stdout, "")

    def test_legacy_promotion_refuses_artifact_pipeline_without_digest_policy(self):
        import gerrit_gitops_handoff as handoff
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "artifact-digest-promotion"):
                handoff.validate({}, {}, {"plan": fixture(Path(temporary))})

    def test_package_and_reporter_accept_only_versioned_complete_helper_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan = fixture(root)
            case = root / "case"; case.mkdir()
            reporter_fixture(case, source_configuration=plan)
            # The shared package helper verifies its digest and file inventory.
            bundle = root / "bundle"; bundle.mkdir(); contract, _, digest = package(bundle, plan=plan)
            self.assertEqual(contract["plan"]["schema_version"], 2)
            (bundle / "source_bound_static_image.py").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError): bridge.load_bundle(bundle, digest)

    def evidence_fixture(self, root, runtime="rke2"):
        plan = fixture(root)
        case = root / "evidence"; case.mkdir()
        contract, template, digest, settings, report_digest, client, run, api, _ = reporter_fixture(case, runtime, source_configuration=plan)
        task = client.objects["taskrun.tekton.dev/application-task"]
        task["status"]["steps"].append({"name": "publish", "terminated": {"exitCode": 0}})
        value, _ = receipt(plan)
        task["status"]["results"] = value["status"]["results"]; run["status"]["results"] = copy.deepcopy(value["status"]["results"])
        data = {"revision": "a" * 40, "ref": "refs/changes/23/123/2"}
        pod = client.objects["pod/application-pod"]
        for index, container in enumerate(pod["spec"]["containers"]):
            container["args"] = artifact.resolve(container["args"], data, pod=True)
            if index: container["args"][container["args"].index("-wait_file") + 1] = "/tekton/run/" + str(index-1) + "/out"
        return contract, template, digest, settings, report_digest, client, run, api, pod

    def test_reporter_binds_success_to_ordered_worker_steps_and_artifact_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.evidence_fixture(Path(temporary)); contract, template, digest, settings, report_digest, client, run, api, _ = args
            _, _, proof = reporter.evidence(client, contract, template, digest, run, settings)
            self.assertIn("artifact", proof); self.assertEqual([s["step"] for s in proof["steps"]], ["checkout", "build", "publish"])
            self.assertEqual(reporter.report_run(client, api, contract, template, digest, settings, report_digest, run), "reported")
            self.assertEqual(reporter.report_run(client, api, contract, template, digest, settings, report_digest, run), "already-reported")
            self.assertEqual(len(api.posts), 1)

    def test_reporter_refuses_cross_step_credential_and_witness_mounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.evidence_fixture(Path(temporary)); contract, template, digest, settings, _, client, run, _, pod = args
            build = next(c for c in pod["spec"]["containers"] if c["name"] == "step-build")
            for name, path in (("robot", "/run/robot"), ("source-credential", "/run/source"), ("witness", "/run/provenance")):
                build["volumeMounts"].append({"name": name, "mountPath": path, "readOnly": True})
                with self.subTest(name=name), self.assertRaises(ValueError): reporter.evidence(client, contract, template, digest, run, settings)
                build["volumeMounts"].pop()
            run["status"]["results"] = []
            with self.assertRaises(ValueError): reporter.evidence(client, contract, template, digest, run, settings)

    def test_rejects_shared_secrets_mutable_images_and_static_revision_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = fixture(Path(temporary)); bridge.validate(plan)
            choices = []
            for key, value in (("robot_secret", plan["read_secret"]), ("trust_secret", plan["bridge_secret"]), ("publisher_image", "registry.example.invalid/python:latest")):
                changed = copy.deepcopy(plan); changed["publication"][key] = value; choices.append(changed)
            changed = copy.deepcopy(plan); changed["publication"]["registry"]["revision"] = "a" * 40; choices.append(changed)
            changed = copy.deepcopy(plan); changed["publication"]["registry"]["origin"] = "HTTPS://registry.example.invalid"; choices.append(changed)
            for changed in choices:
                with self.assertRaises(ValueError): bridge.validate(changed)
            legacy = source_fixture(); legacy["publication"] = plan["publication"]
            with self.assertRaises(ValueError): bridge.validate(legacy)

    def test_receipt_requires_matching_task_pipeline_revision_witness_and_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = fixture(Path(temporary)); task, value = receipt(plan); run = copy.deepcopy(task); data = {"revision": "a" * 40, "ref": "refs/changes/23/123/2"}
            self.assertEqual(artifact.verify_receipt(plan, data, task, run), value)
            for key, replacement in (("source_revision", "b" * 40), ("checkout_witness_sha256", "0" * 64), ("source_plan_sha256", "0" * 64),
                    ("image", "elsewhere.example.invalid/fixture/ha@sha256:" + "f" * 64), ("full_stack_verified", True), ("artifact_publication_verified", 1)):
                changed = copy.deepcopy(task); changed["status"]["results"][0]["value"] = json.dumps({**value, key: replacement})
                with self.subTest(key=key), self.assertRaises(ValueError): artifact.verify_receipt(plan, data, changed, changed)
            with self.assertRaises(ValueError): artifact.verify_receipt(plan, data, task, {"status": {"results": []}})
            changed = copy.deepcopy(task); changed["status"]["results"] *= 2
            with self.assertRaises(ValueError): artifact.verify_receipt(plan, data, changed, changed)


if __name__ == "__main__": unittest.main()
