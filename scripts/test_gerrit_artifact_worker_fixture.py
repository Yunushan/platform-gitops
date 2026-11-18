#!/usr/bin/env python3
"""Actual scoped Gerrit discovery/submission, artifact-aware votes and private serving."""

import hashlib
import json
import os
from pathlib import Path

import tekton_gerrit_reporter as reporter
from test_gerrit_worker_runtime_fixture import exercise as gerrit_workflow
from test_harbor_worker_fixture import (WorkerChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks,
    DatabasePromotionChecks, ServiceChecks, RegistryChecks, serve_published_image)
from test_k3s_vm_fixture import application_workers
from test_tekton_worker_runtime_fixture import PYTHON_IMAGE


class ArtifactChecks(WorkerChecks):
    promotion = False
    native_watch = False

    def worker_workload(self, test, context, native):
        core = self.service_clients["harbor-core"]; ca = (native["root"] / "ca.crt").read_bytes(); credential = (self.robot_directory / "credential.json").read_bytes()
        host = "192.0.2.91:8443"
        directory = "/var/lib/fixture-k3s/agent/etc/containerd/certs.d/" + host
        for node in application_workers(context):
            if context.get("runtime") == "rke2":
                observed = context["ssh"](node, "sudo sha256sum /etc/rancher/rke2/fixture-harbor-ca.crt").stdout.split()[0]
                test.assertEqual(observed, hashlib.sha256(ca).hexdigest())
            else:
                context["ssh"](node, "sudo mkdir -p " + directory)
                context["ssh"](node, "sudo tee " + directory + "/ca.crt >/dev/null", payload=ca.decode())
                hosts = 'server = "https://' + host + '"\n[host."https://' + host + '"]\n  capabilities = ["pull", "resolve"]\n  ca = "' + directory + '/ca.crt"\n'
                context["ssh"](node, "sudo tee " + directory + "/hosts.toml >/dev/null", payload=hosts)
        registry = {"schema_version": 1, "origin": "https://" + host, "ca_file": "/publisher-input/ca.crt", "ca_sha256": hashlib.sha256(ca).hexdigest(),
            "leaf_sha256": core.leaf, "credential_file": "/run/robot/credential.json", "credential_sha256": hashlib.sha256(credential).hexdigest(),
            "project_id": self.robot_plan["project"]["id"], "robot_id": self.robot_id, "repository": "fixture/ha",
            "base_digest": "sha256:ad9fa4d07136a83e69a54ef00102f579d04eba431932de3b0f098cc5d5948f9f", "files": ["index.html"]}
        publication = {"publisher_image": PYTHON_IMAGE, "robot_secret": "ci-publisher-robot", "trust_secret": "ci-publisher-ca", "registry": registry}
        def on_success(plan, run, task, evidence, actual, gerrit, item):
            receipt = evidence["artifact"]
            test.assertEqual(receipt["source_revision"], item["revision"])
            test.assertEqual([v["step"] for v in evidence["steps"]], ["checkout", "build", "publish"])
            # Retain API evidence here, before guest teardown, without an external timing race.
            directory = Path(os.environ["K3S_VM_TEST_REPORT"]).resolve().parent
            directory.relative_to((Path(__file__).resolve().parents[1] / "private").resolve())
            documents = {"successful-pipeline.json": run, "successful-task.json": task, "artifact-receipt.json": receipt,
                "successful-pod.json": actual["client"].get("pod/" + task["status"]["podName"], plan["namespace"]),
                "successful-vote-receipt.json": actual["client"].get("configmap/" + run["metadata"]["name"] + "-vote", plan["namespace"])}
            for name, value in documents.items():
                descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream: json.dump(value, stream, sort_keys=True); stream.write("\n")
            test.assertEqual(documents["successful-vote-receipt.json"]["data"]["evidence_sha256"], reporter.fingerprint(evidence))
            if self.promotion:
                chosen["artifact_count_after_success"] = self.sql("SELECT count(*) FROM artifact;", "registry")
                promoted = self.promote_artifact(test, actual, gerrit, plan, run, receipt, chosen["execution"], registry)
                if getattr(self, "signature_gate", False):
                    chosen["artifact_count_after_success"] = self.signature_artifact_count_after_sign
                return {**promoted, "actual_ci_worker_registry_publication_verified": True,
                    "build_publish_robot_mount_isolation_verified": True, "artifact_receipt_verified_before_vote": True,
                    "publication_manifest_digest": receipt["manifest_digest"], "registry_image_signature_verified": False, "full_stack_verified": False}
            served = serve_published_image(test, actual, self, plan["namespace"], receipt["image"])
            chosen["artifact_count_after_success"] = self.sql("SELECT count(*) FROM artifact;", "registry")
            return {**served, "actual_ci_worker_registry_publication_verified": True, "build_publish_robot_mount_isolation_verified": True,
                "artifact_receipt_verified_before_vote": True, "publication_manifest_digest": receipt["manifest_digest"],
                "registry_image_signature_verified": False, "ci_cd_application_handoff_verified": False, "full_stack_verified": False}
        chosen = {"publication": publication, "credential": credential, "ca": ca, "on_success": on_success}
        if self.promotion:
            chosen.update(promotion=True, native_promoter=self.native_cli, source_files=self.source_files(registry))
        result = gerrit_workflow(test, context, native_watch=self.native_watch, publication_context=chosen)
        test.assertEqual(self.sql("SELECT count(*) FROM artifact;", "registry"), chosen["artifact_count_after_success"])
        result.update(current_gerrit_patchset_selection_verified=True, source_readonly_and_witness_isolation_verified=True)
        if self.native_watch:
            units = result["native_watch_units"]
            for handle in units.values():
                test.assertEqual(len(handle["processes"]), 2)
                test.assertTrue(all(v["original_process_and_cgroup_removed"] is True for v in handle["processes"]))
            proof = {"units": units, "pipeline_run_uid": result["gerrit_worker_evidence"]["uid"],
                "evidence_sha256": reporter.fingerprint(result["gerrit_worker_evidence"])}
            path = Path(os.environ["K3S_VM_TEST_REPORT"]).resolve().parent / "native-watch-runs.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream: json.dump(proof, stream, sort_keys=True); stream.write("\n")
            result.update(artifact_native_watch_ci_to_argocd_verified=True,
                native_watch_original_identities_and_cleanup_verified=True,
                native_watch_proof_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        if self.promotion: result["deployment_registry_artifact_count_unchanged"] = True
        else: result["failed_build_registry_artifact_count_unchanged"] = True
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(ArtifactChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
