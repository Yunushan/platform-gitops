#!/usr/bin/env python3
"""Check OpenBao packages and opt-in isolated real Raft acceptance."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

import render_openbao_ha as renderer


def fixture():
    return json.loads((Path(__file__).resolve().parents[1] / "examples/openbao-ha.json").read_text())


class OpenBaoTests(unittest.TestCase):
    def test_all_runtimes_bind_artifacts_without_claiming_live_ceremony_or_ha(self):
        for runtime in renderer.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = renderer.render(fixture(), runtime)
                contract = json.loads(files["openbao-contract.json"])
                self.assertEqual(set(contract["files_sha256"]), set(files) - {"openbao-contract.json", "render-status.json"})
                for name, digest in contract["files_sha256"].items(): self.assertEqual(hashlib.sha256(files[name].encode()).hexdigest(), digest)
                result = json.loads(files["render-status.json"])
                for name in ("deployed", "initialized", "unsealed", "audit_verified", "restricted_identities_verified", "quorum_recovery_verified", "off_platform_restore_verified", "ha_verified", "full_stack_verified"):
                    self.assertIs(result[name], False)

    def test_tls_discovery_voting_and_hmac_audit_are_explicit(self):
        plan = fixture()
        for runtime in renderer.RUNTIMES:
            for member in plan["members"]:
                config = renderer.configuration(plan, member, runtime)
                self.assertNotIn("initialize", config); self.assertNotIn("seal", config)
                self.assertTrue(config["disable_mlock"])
                self.assertTrue(config["api_addr"].startswith("https://")); self.assertTrue(config["cluster_addr"].startswith("https://"))
                listener = config["listener"][0]["tcp"]
                self.assertNotIn("tls_disable", listener); self.assertEqual(listener["tls_min_version"], "tls12")
                self.assertNotIn("unauthenticated_metrics_access", json.dumps(config))
                raft = config["storage"]["raft"]
                self.assertEqual(raft["node_id"], member["name"]); self.assertEqual(len(raft["retry_join"]), 2)
                self.assertNotIn(config["api_addr"], {peer["leader_api_addr"] for peer in raft["retry_join"]})
                self.assertTrue(all(peer["leader_api_addr"].startswith("https://") and peer["leader_ca_cert_file"].endswith("/ca.crt") for peer in raft["retry_join"]))
                audit = config["audit"][0]["file"]["platform-local"]["options"]
                self.assertEqual(audit["log_raw"], "false"); self.assertEqual(audit["mode"], "0600")
            self.assertEqual(len({renderer.configuration(plan, m, runtime)["audit"][0]["file"]["platform-local"]["options"]["file_path"] for m in plan["members"]}), 1)

    def test_kubernetes_member_identity_storage_placement_and_probe_boundaries(self):
        files = renderer.render(fixture(), "k8s"); objects = list(yaml.safe_load_all(files["openbao.yaml"]))
        workloads = [o for o in objects if o["kind"] == "Deployment"]
        self.assertEqual(len(workloads), 3)
        claims = set()
        for workload in workloads:
            self.assertEqual(workload["spec"]["strategy"]["type"], "Recreate")
            pod = workload["spec"]["template"]["spec"]; self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
            claims.update(v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v)
            container = pod["containers"][0]; self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(container["command"][0], "/usr/bin/bao")
            self.assertIn("tcpSocket", container["livenessProbe"])
            self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/v1/sys/health?standbyok=true")
            self.assertNotIn("-dev", container["command"])
            self.assertEqual(len(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]), 2)
        self.assertEqual(len(claims), 6)
        self.assertFalse(any(o["kind"] in {"Secret", "PersistentVolumeClaim", "ClusterRoleBinding"} for o in objects))
        self.assertEqual(next(o for o in objects if o["kind"] == "PodDisruptionBudget")["spec"]["minAvailable"], 2)
        policy = next(o for o in objects if o["kind"] == "NetworkPolicy")["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertNotIn("0.0.0.0/0", json.dumps(policy))

    def test_native_and_docker_do_not_initialize_unseal_or_share_member_data(self):
        plan = fixture()
        for member in plan["members"]:
            files = renderer.render(plan, "docker"); service = yaml.safe_load(files[member["name"] + "-compose.yaml"])["services"][member["name"]]
            self.assertEqual(service["user"], "1000:1000"); self.assertEqual(service["network_mode"], "host")
            self.assertEqual(service["entrypoint"], ["/usr/bin/bao"])
            self.assertEqual(service["mem_limit"], service["memswap_limit"])
            self.assertTrue(all(v["bind"]["create_host_path"] is False for v in service["volumes"]))
            self.assertNotIn("env_file", service); self.assertNotIn("/var/run/docker.sock", json.dumps(service))
            files = renderer.render(plan, "raw"); unit = files[member["name"] + ".service"]
            for value in ("User=openbao", "MemorySwapMax=0", "CapabilityBoundingSet=", "ProtectSystem=strict", "sha256sum --check --strict"):
                self.assertIn(value, unit)
            self.assertIn(plan["binary_sha256"], files[member["name"] + "-binary.sha256"])
            self.assertNotIn("operator init", unit); self.assertNotIn("operator unseal", unit)

    def test_invalid_topology_paths_versions_and_credential_fields_refuse(self):
        mutations = [lambda p: p.update(version="latest"), lambda p: p.update(image="quay.io/openbao/openbao:latest"),
            lambda p: p.update(schema_version=True), lambda p: p.update(token="<fixture-only>"),
            lambda p: p["members"].pop(), lambda p: p["members"][1].update(address=p["members"][0]["address"]),
            lambda p: p["members"][1].update(node=p["members"][0]["node"]), lambda p: p["members"][1].update(failure_domain=p["members"][0]["failure_domain"]),
            lambda p: p["members"][0].update(address="127.0.0.1"), lambda p: p["members"][0].update(address="0.0.0.0"),
            lambda p: p["members"][0].update(audit_claim=p["members"][0]["data_claim"]),
            lambda p: p["members"][0].update(data_dir=p["private_dir"] + "/data"), lambda p: p.update(audit_dir="/var/log/../secrets"),
            lambda p: p.update(binary="bao"), lambda p: p.update(binary_sha256="bad"),
            lambda p: p["network"].update(ingress_namespaces=["default", "default"])]
        for index, mutate in enumerate(mutations):
            plan = fixture(); mutate(plan)
            with self.subTest(index=index), self.assertRaises((ValueError, TypeError)): renderer.render(plan, "raw")
        with self.assertRaises(ValueError): renderer.render(fixture(), "unsupported")

    def test_cli_writes_only_new_private_output_and_suppresses_private_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "private").mkdir()
            source = root / "private/plan.json"; source.write_text(json.dumps(fixture()))
            with mock.patch.object(renderer, "ROOT", root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                args = ["--plan", str(source), "--runtime", "raw", "--output-dir", str(root / "public")]
                self.assertEqual(renderer.main(args), 1); self.assertFalse((root / "public").exists())
                args[-1] = str(root / "private/bundle")
                self.assertEqual(renderer.main(args), 0)
                original = {p.name: p.read_bytes() for p in (root / "private/bundle").iterdir()}
                self.assertEqual(renderer.main(args), 1)
                self.assertEqual(original, {p.name: p.read_bytes() for p in (root / "private/bundle").iterdir()})

    @unittest.skipUnless(os.environ.get("OPENBAO_TEST_API") == "1", "Explicit isolated Kubernetes API schema/admission fixture")
    def test_real_kubernetes_api_admission_does_not_prove_workload_availability(self):
        from test_rook_api_fixture import exercise
        files = renderer.render(fixture(), "k8s")
        exercise(self, resource_objects=list(yaml.safe_load_all(files["openbao.yaml"])))

    @unittest.skipUnless(os.environ.get("OPENBAO_TEST_RUNTIME") == "1", "Explicit isolated Linux Raft/TLS fixture; no production ceremony")
    def test_real_raft_tls_restricted_access_audit_and_leader_recovery(self):
        from test_openbao_runtime_fixture import exercise
        result = exercise(self)
        for key in ("tls_three_member_startup_verified", "untrusted_ca_rejected", "shamir_init_unseal_verified", "three_voters_verified", "restricted_policy_verified", "audit_hmac_verified",
                    "initial_root_revocation_verified", "leader_process_loss_verified", "sealed_restart_recovery_verified", "snapshot_capture_verified", "guarded_snapshot_capture_verified",
                    "fresh_target_snapshot_restore_verified", "source_processes_stopped_during_restore_verified", "original_unseal_shares_restore_verified",
                    "restored_data_and_rbac_verified", "restored_writes_committed_verified", "restored_single_voter_identity_verified",
                    "offline_backup_verification_verified", "read_only_runtime_observer_verified", "temporary_processes_and_data_removed"):
            self.assertIs(result[key], True)
        for key in ("physical_node_loss_verified", "off_platform_restore_verified", "ha_verified", "full_stack_verified", "publisher_signature_verified"):
            self.assertIs(result[key], False)


if __name__ == "__main__": unittest.main()
