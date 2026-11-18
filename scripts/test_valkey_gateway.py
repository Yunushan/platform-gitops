#!/usr/bin/env python3
"""Test TLS gateway routing policy; ordinary discovery responses are modeled."""

import copy
import hashlib
import json
import os
import unittest

import render_valkey_gateway as renderer
import valkey_gateway_health as health
import yaml
from test_valkey_runtime import Network as BaseNetwork
from test_valkey_runtime import fixture as runtime_fixture
from test_valkey_runtime import info


def fixture(runtime="raw"):
    cache_plan, _, observation = runtime_fixture()
    cache_plan["network"]["client_namespaces"] = ["cache-gateway"]
    files = renderer.cache.render(cache_plan, "raw"); contract = json.loads(files["valkey-contract.json"])
    observation["contract_sha256"] = hashlib.sha256(files["valkey-contract.json"].encode()).hexdigest()
    directory = "/etc/platform-valkey-gateway"
    for kind in ("ca", "client_cert", "client_key"): observation["trust"][kind + "_file"] = directory + "/" + kind + ".pem"
    plan = {"schema_version": 1, "name": "application-cache", "namespace": "cache-gateway", "image": "registry.example.com/gateway@sha256:" + "a" * 64,
        "native_binary": "/usr/local/libexec/haproxy", "native_binary_sha256": "b" * 64, "private_dir": directory, "tls_secret": "application-cache-private",
        "frontend": {"hostname": "cache.example.com", "port": 6381}, "tls_names": {name: name + ".example.com" for name in observation["members"]},
        "private_sha256": {"frontend.pem": "c" * 64, "upstream.pem": "d" * 64},
        "instances": [{"name": "gateway-a", "address": "192.0.2.71"}, {"name": "gateway-b", "address": "192.0.2.72"}],
        "node_selector": {"kubernetes.io/os": "linux"}, "failure_domain_key": "topology.kubernetes.io/zone", "client_namespaces": ["harbor"], "observer": observation}
    return plan, contract


class Network(BaseNetwork):
    def response(self, name, kind, args):
        if kind == "server" and args[:2] == ("INFO", "replication"):
            copies = [n for n in self.names if n != self.primary]
            return info({"role": "master", "connected_slaves": len(copies), **{"slave" + str(i): "ip=" + self.plan["members"][n]["connect_address"] + ",port=6380,state=online,offset=100,lag=0" for i, n in enumerate(copies)}})
        if kind == "server" and args[0] == "ROLE": return ["master", 100, [[self.plan["members"][n]["connect_address"], 6380, 100] for n in self.names if n != self.primary]]
        return super().response(name, kind, args)


class GatewayTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GERRIT_ADMITTED_WATCH_FAILURE_TEST_RUNTIME") == "1", "Explicit persistent native Gerrit watch, signed admission and original-worker recovery")
    def test_real_native_watched_signed_ci_admission_failure_recovery(self):
        from test_gerrit_admitted_promotion_fixture import exercise
        exercise(self,admission_failures=True,native_watch=True)

    @unittest.skipUnless(os.environ.get("GERRIT_ADMITTED_FAILURE_TEST_RUNTIME") == "1", "Explicit original signed CI-to-Argo worker loss, fail-closed admission and recovery")
    def test_real_gerrit_signed_ci_admission_failure_recovery(self):
        from test_gerrit_admitted_promotion_fixture import exercise
        exercise(self,admission_failures=True)

    @unittest.skipUnless(os.environ.get("GERRIT_ADMITTED_PROMOTION_TEST_RUNTIME") == "1", "Explicit original Gerrit CI, signed bindings and Argo rollout under three-replica admission")
    def test_real_gerrit_signed_ci_admitted_argocd_rollout(self):
        from test_gerrit_admitted_promotion_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("KYVERNO_FAILURE_TEST_RUNTIME") == "1", "Explicit original-worker Kyverno peer loss, fail-closed and recovery")
    def test_real_kyverno_worker_loss_and_fail_closed_recovery(self):
        from test_kyverno_admission_runtime_fixture import exercise
        exercise(self,admission_failures=True)

    @unittest.skipUnless(os.environ.get("KYVERNO_ADMISSION_TEST_RUNTIME") == "1", "Explicit actual three-node Kyverno admission and signed private-image execution")
    def test_real_kyverno_three_replica_image_admission(self):
        from test_kyverno_admission_runtime_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("KYVERNO_PRIVATE_TUF_TEST_RUNTIME") == "1", "Explicit real Kyverno verifier and private threshold-signed TLS TUF mirror")
    def test_real_kyverno_private_tuf_signature_verification(self):
        from test_kyverno_private_tuf_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("GERRIT_SIGNED_ARTIFACT_PROMOTION_TEST_RUNTIME") == "1", "Explicit actual Gerrit CI, release signing and native signed promotion to Argo")
    def test_real_gerrit_signed_artifact_native_cli_to_argocd(self):
        from test_gerrit_signed_promotion_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("HARBOR_IMAGE_SIGNATURE_TEST_RUNTIME") == "1", "Explicit actual Harbor image signature, TLS Rekor inclusion and isolated roles")
    def test_real_harbor_image_signature_verification(self):
        from test_sigstore_image_runtime_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("GERRIT_ARTIFACT_NATIVE_WATCH_WORKER_TEST_RUNTIME") == "1", "Explicit native watch restart/replay, artifact CI and native promotion through Argo")
    def test_real_gerrit_artifact_native_watch_to_argocd(self):
        from test_gerrit_artifact_promotion_worker_fixture import exercise
        exercise(self, native_cli=True, native_watch=True)

    @unittest.skipUnless(os.environ.get("GERRIT_ARTIFACT_NATIVE_PROMOTION_WORKER_TEST_RUNTIME") == "1", "Explicit real artifact CI and packaged non-root promoter CLI through Argo rollout")
    def test_real_gerrit_artifact_native_cli_to_argocd(self):
        from test_gerrit_artifact_promotion_worker_fixture import exercise
        exercise(self, native_cli=True)

    @unittest.skipUnless(os.environ.get("GERRIT_ARTIFACT_PROMOTION_WORKER_TEST_RUNTIME") == "1", "Explicit real artifact CI, merged promotion, Argo rollout and private serving")
    def test_real_gerrit_artifact_ci_to_argocd(self):
        from test_gerrit_artifact_promotion_worker_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("GERRIT_ARTIFACT_WORKER_TEST_RUNTIME") == "1", "Explicit artifact-aware scoped Gerrit CI, votes and private serving")
    def test_real_automated_gerrit_artifact_publication_and_votes(self):
        from test_gerrit_artifact_worker_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("GERRIT_HARBOR_WORKER_TEST_RUNTIME") == "1", "Explicit real Gerrit patchset, worker build, Harbor publication and private pull")
    def test_real_gerrit_harbor_worker_publication(self):
        from test_gerrit_harbor_worker_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.environ.get("HARBOR_WORKER_TEST_RUNTIME") == "1", "Explicit real KVM/Tekton worker publication and private digest pull")
    def test_real_harbor_worker_publication_and_digest_serving(self):
        from test_harbor_worker_fixture import exercise
        exercise(self)

    def test_all_five_runtime_packages_keep_mtls_and_authenticated_discovery(self):
        for runtime in renderer.RUNTIMES:
            plan, contract = fixture(runtime); files = renderer.render(plan, contract, runtime)
            self.assertIn("valkey_gateway_health.py", files)
            self.assertIn("gateway-checks.json", files)
            configs = [v for n, v in files.items() if n.endswith(".cfg")]
            for config in configs:
                self.assertEqual(config.count("verify required ca-file"), 3)
                self.assertEqual(config.count("verifyhost"), 3)
                self.assertIn("init-state fully-down", config)
                self.assertIn("shutdown-sessions", config)
                self.assertIn("nbsrv(serving_primary)", config)
            status = json.loads(files["render-status.json"])
            self.assertTrue(status["upstream_mtls_preserved"])
            for key in renderer.UNVERIFIED: self.assertFalse(status[key])
            self.assertEqual(contract["plan"]["settings"]["min_replicas"], 1)
            if runtime in {"rke2", "k3s", "k8s"}:
                objects = list(yaml.safe_load_all(files["gateway.yaml"]))
                deployment = next(o for o in objects if o["kind"] == "Deployment")
                self.assertEqual(deployment["spec"]["replicas"], 2)
                # On exactly two eligible domains, a surge Pod cannot satisfy
                # mandatory anti-affinity. Permit replacing one original Pod.
                self.assertEqual(deployment["spec"]["strategy"]["rollingUpdate"], {"maxSurge": 0, "maxUnavailable": 1})
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(len(pod["containers"]), 2); self.assertFalse(pod["automountServiceAccountToken"])
                self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", pod["affinity"]["podAntiAffinity"])
                self.assertIn("exec", pod["containers"][1]["readinessProbe"])
                for container in pod["containers"]:
                    private = [m for m in container["volumeMounts"] if m["name"] == "private"]
                    self.assertEqual(len(private), 5)
                    self.assertTrue(all(m.get("subPath") and m["readOnly"] for m in private))
                policy = next(o for o in objects if o["kind"] == "NetworkPolicy")
                self.assertEqual(policy["spec"]["policyTypes"], ["Ingress", "Egress"])
            if runtime == "raw":
                self.assertNotIn("docker", files["gateway-a-proxy.service"])
                self.assertIn("native-binary.sha256", files["gateway-a-proxy.service"])
                self.assertIn(plan["native_binary_sha256"], files["native-binary.sha256"])

    def test_distinct_quorum_votes_select_original_primary_and_no_commands_write_state(self):
        plan, contract = fixture(); network = Network(plan["observer"], contract["plan"])
        self.assertEqual(health.winner(network, {"contract": contract, "observer": plan["observer"]}), "cache-a")
        self.assertFalse(any(args[0] in {"SET", "DEL", "REPLICAOF", "CONFIG"} for _, _, args in network.calls))
        network.primary = "cache-b"
        self.assertEqual(health.winner(network, {"contract": contract, "observer": plan["observer"]}), "cache-b")

    def test_frontend_client_certificate_policy_preserves_upstream_verification(self):
        plan, contract = fixture()
        original = renderer.render(plan, contract, "raw")["gateway-a.cfg"]
        self.assertNotIn("/frontend.pem ca-file", original)
        plan["frontend"]["require_client_certificate"] = True
        for runtime in renderer.RUNTIMES:
            files = renderer.render(plan, contract, runtime)
            config = files.get("gateway-a.cfg", files.get("haproxy.cfg"))
            self.assertRegex(config, r"/frontend\.pem ca-file [\w/.-]+/ca\.pem verify required\n")
            self.assertEqual(config.count(" ssl verify required ca-file "), 3)
            self.assertIn("on-marked-down shutdown-sessions", config)
        plan["frontend"]["require_client_certificate"] = "false"
        with self.assertRaises(ValueError):
            renderer.render(plan, contract, "raw")

    def test_vrrp_binds_the_shared_address_and_tracks_the_local_primary_gate(self):
        plan, contract = fixture()
        plan["frontend"]["require_client_certificate"] = True
        plan["vrrp"] = {"vip": "192.0.2.70/24", "vrid": 79,
                        "interfaces": {"gateway-a": "eth0", "gateway-b": "ens160"},
                        "binary": "/usr/sbin/keepalived", "binary_sha256": "f" * 64}
        for runtime in ("raw", "docker"):
            files = renderer.render(plan, contract, runtime)
            for peer, interface in plan["vrrp"]["interfaces"].items():
                conf = files[peer + "-keepalived.conf"]
                service = files[peer + "-keepalived.service"]
                cfg = files[peer + ".cfg"]
                self.assertIn("192.0.2.70/24 dev " + interface, conf)
                self.assertIn("valkey_ready", conf)
                self.assertIn("check_unicast_src", conf)
                self.assertIn("--vrrp --use-file", service)
                self.assertIn("bind 192.0.2.70:6381 ssl", cfg)
                self.assertIn("nbsrv(serving_primary) eq 1", cfg)
                self.assertIn(health.observer.fingerprint(plan), conf)
                self.assertIn(health.observer.fingerprint(plan), cfg)
        with self.assertRaises(ValueError):
            renderer.render(plan, contract, "k8s")
        for invalid in ({"vip": "192.0.2.71/24"}, {"vip": "192.0.3.70/24"},
                        {"interfaces": {"gateway-a": "lo", "gateway-b": "ens160"}}, {"vrid": True}):
            bad = copy.deepcopy(plan)
            bad["vrrp"].update(invalid)
            with self.assertRaises(ValueError):
                renderer.render(bad, contract, "raw")

    def test_single_sentinel_stale_role_changed_invocation_lag_or_aof_error_fails_closed(self):
        plan, contract = fixture(); specification = {"contract": contract, "observer": plan["observer"]}
        def missing_votes(name, kind, args, result):
            if kind == "sentinel" and name != "cache-a": raise OSError("modeled offline Sentinel")
            return result
        mutations = [missing_votes,
            lambda n, k, a, r: ["slave", "192.0.2.52", 6380, "connected", 100] if k == "server" and a[0] == "ROLE" else r,
            lambda n, k, a, r: info({"valkey_version": "8.1.11", "run_id": "f" * 40}) if k == "server" and a == ("INFO", "server") else r,
            lambda n, k, a, r: r.replace("lag=0", "lag=1000") if k == "server" and a == ("INFO", "replication") else r,
            lambda n, k, a, r: r.replace("aof_last_write_status:ok", "aof_last_write_status:err") if k == "server" and a == ("INFO", "persistence") else r]
        for mutate in mutations:
            network = Network(plan["observer"], contract["plan"]); network.mutation = mutate
            self.assertIsNone(health.winner(network, specification))

    def test_foreign_primary_bad_discovery_tls_names_and_unapproved_namespace_refuse(self):
        plan, contract = fixture()
        mutations = [lambda p: p.update(namespace="unapproved"), lambda p: p["tls_names"].update({"cache-b": p["tls_names"]["cache-a"]}),
            lambda p: p["tls_names"].update({"cache-a": "192.0.2.51"}), lambda p: p["frontend"].update(hostname="192.0.2.71"),
            lambda p: p["observer"]["trust"].update(ca_file="relative.pem"), lambda p: p["instances"][1].update(address=p["instances"][0]["address"])]
        for mutate in mutations:
            bad = copy.deepcopy(plan); mutate(bad)
            with self.assertRaises(ValueError): renderer.render(bad, contract, "raw")
        network = Network(plan["observer"], contract["plan"])
        network.mutation = lambda n, k, a, r: ["192.0.2.200", "6380"] if a[:2] == ("SENTINEL", "GET-MASTER-ADDR-BY-NAME") else r
        self.assertIsNone(health.winner(network, {"contract": contract, "observer": plan["observer"]}))

    @unittest.skipUnless(os.environ.get("VALKEY_GATEWAY_TEST_RUNTIME") == "1", "Explicit isolated native TLS gateway and real Valkey fixture")
    def test_real_gateway_preserves_backend_mtls_and_reconnects_after_promotion(self):
        from test_valkey_gateway_runtime_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_real_tls_forwarding_verified"])
        self.assertTrue(result["valkey_gateway_primary_loss_reconnect_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("VALKEY_GATEWAY_TEST_API") == "1", "Explicit disposable Kubernetes API fixture")
    def test_kubernetes_package_admits_without_real_cache_or_secret_reads(self):
        from test_rook_api_fixture import exercise
        plan, contract = fixture("k8s")
        resources = list(yaml.safe_load_all(renderer.render(plan, contract, "k8s")["gateway.yaml"]))
        exercise(self, resource_objects=resources)

    @unittest.skipUnless(os.environ.get("HARBOR_CACHE_TEST_RUNTIME") == "1", "Explicit isolated pinned Harbor cache-client constructors")
    def test_real_harbor_cache_clients_authenticate_and_reconnect_after_promotion(self):
        from test_harbor_cache_runtime_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_core_cache_client_verified"])
        self.assertTrue(result["valkey_gateway_harbor_clients_original_process_reconnect_verified"])
        self.assertFalse(result["valkey_gateway_harbor_registry_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_ROBOT_TEST_RUNTIME") == "1", "Explicit actual restricted Harbor robot across peer losses and promotions")
    def test_real_harbor_robot_project_publication_after_failures_and_promotions(self):
        from test_harbor_robot_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_robot_project_push_pull_verified"])
        self.assertTrue(result["valkey_gateway_harbor_robot_original_credential_after_promotions_verified"])
        self.assertTrue(result["valkey_gateway_harbor_robot_cross_project_refusal_verified"])
        self.assertFalse(result["valkey_gateway_harbor_ci_worker_publication_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_POOL_HEALTH_TEST_RUNTIME") == "1", "Explicit separately pinned local Harbor pool-health candidate")
    def test_real_local_harbor_pool_health_candidate_after_peer_losses_and_promotions(self):
        from test_harbor_pool_health_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_local_pool_health_patch_verified"])
        self.assertTrue(result["valkey_gateway_harbor_original_worker_pool_after_promotions_verified"])
        self.assertFalse(result["valkey_gateway_harbor_unmodified_release_sql_continuity_verified"])
        self.assertFalse(result["valkey_gateway_harbor_candidate_upstream_supported"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_JOB_REPLICA_TEST_RUNTIME") == "1", "Explicit original Harbor worker peer loss and shared queue/log continuity")
    def test_real_harbor_jobservice_peer_loss_and_original_worker_after_promotions(self):
        from test_harbor_jobservice_replica_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_jobservice_peer_sigkill_continuity_verified"])
        self.assertTrue(result["valkey_gateway_harbor_original_worker_pool_after_promotions_verified"])
        self.assertFalse(result["valkey_gateway_harbor_jobservice_inflight_recovery_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_CORE_REPLICA_TEST_RUNTIME") == "1", "Explicit real Harbor core peer loss with token/image continuity")
    def test_real_harbor_core_peer_loss_and_survivor_after_promotions(self):
        from test_harbor_core_replica_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_core_peer_sigkill_continuity_verified"])
        self.assertTrue(result["valkey_gateway_harbor_original_core_survivor_after_promotions_verified"])
        self.assertFalse(result["valkey_gateway_harbor_core_independent_host_ha_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_JWT_TEST_RUNTIME") == "1", "Explicit real Harbor-issued JWT and OCI APIs across promotions")
    def test_real_harbor_jwt_oci_flow_after_cache_and_database_promotions(self):
        from test_harbor_jwt_runtime_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_core_jwt_oci_roundtrip_verified"])
        self.assertTrue(result["valkey_gateway_harbor_artifact_metadata_sql_verified"])
        self.assertTrue(result["valkey_gateway_harbor_original_jwt_after_promotions_verified"])
        self.assertFalse(result["valkey_gateway_harbor_full_ha_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_DATABASE_PROMOTION_TEST_RUNTIME") == "1", "Explicit actual Harbor clients across PostgreSQL promotion")
    def test_real_harbor_services_reconnect_after_planned_database_promotion(self):
        from test_harbor_database_promotion_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_original_services_after_database_promotion_verified"])
        self.assertTrue(result["valkey_gateway_harbor_database_strict_quorum_after_promotion_verified"])
        self.assertFalse(result["valkey_gateway_harbor_automatic_database_failover_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_SERVICE_TEST_RUNTIME") == "1", "Explicit isolated actual Harbor services and PostgreSQL")
    def test_real_harbor_services_execute_jobs_after_cache_promotion(self):
        from test_harbor_service_runtime_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_harbor_actual_dry_run_jobs_verified"])
        self.assertTrue(result["valkey_gateway_harbor_original_services_after_cache_promotion_verified"])
        self.assertFalse(result["valkey_gateway_harbor_full_service_ha_verified"])
        self.assertFalse(result["application_compatibility_verified"])

    @unittest.skipUnless(os.environ.get("HARBOR_REGISTRY_TEST_RUNTIME") == "1", "Explicit isolated real Harbor registry daemons")
    def test_real_registry_cross_replica_upload_and_cache_promotion(self):
        from test_harbor_registry_runtime_fixture import exercise
        result = exercise(self)
        self.assertTrue(result["valkey_gateway_registry_cross_replica_upload_verified"])
        self.assertTrue(result["valkey_gateway_registry_original_process_after_cache_promotion_verified"])
        self.assertFalse(result["valkey_gateway_registry_full_harbor_ha_verified"])
        self.assertFalse(result["application_compatibility_verified"])


if __name__ == "__main__": unittest.main()
