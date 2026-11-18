#!/usr/bin/env python3
"""Actual merged Gerrit CI image, isolated release signer and native signed promotion."""

import copy
import hashlib
from pathlib import Path

import gerrit_artifact_promotion as promotion
from test_gerrit_artifact_promotion_worker_fixture import NativePromotionChecks, retain
from test_sigstore_image_runtime_fixture import SignatureChecks


class SignedPromotionChecks(NativePromotionChecks, SignatureChecks):
    signature_gate = True

    def worker_workload(self, test, context, native):
        self.signature_context = native
        return super().worker_workload(test, context, native)

    def prepare_signed_promotion(self, test, plan, report, contract, template, arguments):
        selected = copy.deepcopy(plan); selected["schema_version"] = 3
        _, _, _, _, ci, _, api, run_name = arguments
        data, evidence = promotion.legacy.verified_ci(ci, api, report, contract, template, selected, run_name)
        annotations = promotion.signature_annotations(selected, contract, data, evidence)
        self.signature_setup(self.signature_context, image=evidence["artifact"]["image"], annotations=annotations)
        # Only public verification material and the pull-only token enter each
        # native promoter's private credential mount. Signing keys remain under
        # their original distinct signer/log numeric identities.
        self.signature_native_inputs = {name: Path(self.signature_plan[name]["path"])
            for name in ("cosign_binary", "registry_ca", "public_key", "trusted_root", "credential")}
        selected["signature_policy"] = {name: {"path": "/usr/local/bin/cosign" if name == "cosign_binary" else
            "/run/platform/gerrit-promoter/signature-" + name, "sha256": self.signature_plan[name]["sha256"]}
            for name in self.signature_native_inputs}
        promotion.validate(selected, report, contract)
        test.assertEqual(annotations, promotion.signature_annotations(selected, contract, data, evidence))
        refreshed = promotion.legacy.verified_ci(ci, api, report, contract, template, selected, run_name)
        test.assertEqual((data, evidence), refreshed)
        test.assertIs(evidence["artifact"]["publisher_signature_verified"], False)
        self.signed_ci_binding = {"data": data, "evidence": evidence, "annotations": annotations,
            "promotion_plan": selected, "ci_signature_handoff_verified": False,
            "publisher_signature_verified": False, "signed_image_admission_verified": False, "full_stack_verified": False}
        self.signature_artifact_count_after_sign = self.sql("SELECT count(*) FROM artifact;", "registry")
        return selected

    def exercise(self, context):
        result = super().exercise(context)
        test = context["test"]
        for name in ("merged_ci_signature_verified", "actual_artifact_ci_to_argocd_rollout_verified",
                "actual_promoted_digest_serving_on_" + ("three" if self.application_replicas == 3 else "two") + "_workers_verified", "artifact_native_four_cli_modes_verified",
                "artifact_receipt_verified_before_vote", "source_readonly_and_witness_isolation_verified"):
            test.assertIs(result["valkey_gateway_harbor_" + name], True)
        runs = self.signature_native_results; test.assertEqual(len(runs), 4)
        for item in runs[1:]:
            proof = item["journal"]["signature_proof"]
            test.assertTrue(item["journal"]["merged_ci_signature_verified"])
            test.assertEqual(item["journal"]["pipeline_run_uid"], self.signed_ci_binding["data"]["uid"])
            test.assertEqual(proof["signed_annotations_sha256"], promotion.common.sha256(self.signed_ci_binding["annotations"]))
            test.assertEqual(proof["public_key_sha256"], self.signature_plan["public_key"]["sha256"])
            test.assertNotIn(item["uid"], self.signature_uids.values())
            test.assertTrue(item["original_process"]["original_process_and_cgroup_removed"])
            test.assertFalse(item["result"].get("signed_image_admission_verified", False))
        self.signed_ci_binding.update(ci_signature_handoff_verified=True,
            native_runs_sha256=hashlib.sha256(promotion.bridge.common.canonical(runs)).hexdigest())
        sha = retain("signed-ci-binding.json", self.signed_ci_binding)
        result.update(valkey_gateway_harbor_ci_signature_handoff_verified=True,
            valkey_gateway_harbor_original_merged_ci_signed_native_promotion_verified=True,
            valkey_gateway_harbor_signed_ci_binding_proof_sha256={"proof": sha})
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    from test_harbor_core_replica_fixture import CoreReplicaChecks
    from test_harbor_database_promotion_fixture import DatabasePromotionChecks
    from test_harbor_jobservice_replica_fixture import JobReplicaChecks
    from test_harbor_jwt_runtime_fixture import JWTChecks
    from test_harbor_pool_health_fixture import PoolHealthChecks
    from test_harbor_registry_runtime_fixture import RegistryChecks
    from test_harbor_service_runtime_fixture import ServiceChecks
    class Application(SignedPromotionChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks,
            DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
