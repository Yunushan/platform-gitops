#!/usr/bin/env python3
"""Exercise every requested stack/runtime without touching deployment state."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import plan_platform_stack as planner


class StackPlanTests(unittest.TestCase):
    def test_jenkins_bridge_is_discoverable_without_claiming_live_support(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                matches = [a for a in plan["auxiliary_adapters"] if a["component"] == "jenkins-gerrit-review-bridge"]
                self.assertEqual(bool(matches), stack == "gerrit-jenkins-argocd")
                if matches:
                    bridge = matches[0]
                    self.assertTrue(bridge["artifacts_available"])
                    self.assertTrue(bridge["argocd_handoff_available"])
                    self.assertTrue(bridge["result_scanner_available"])
                    self.assertTrue(bridge["host_poller_renderer_available"])
                    self.assertTrue(bridge["kubernetes_poller_renderer_available"])
                    self.assertTrue(bridge["poller_image_context_preparer_available"])
                    self.assertFalse(bridge["poller_image_and_storage_verified"])
                    self.assertTrue(bridge["disposable_reconciliation_fixture_available"])
                    self.assertTrue(bridge["requires_explicit_kubernetes_target"])
                    self.assertTrue(bridge["explicit_controller_trust_required"])
                    for field in ("independent_checkout_provenance_verified", "production_event_and_vote_integration_verified", "gitops_handoff_verified", "live_verified"):
                        self.assertFalse(bridge[field])
                    self.assertFalse(plan["deployment_ready"])
        with tempfile.TemporaryDirectory() as directory:
            plan = planner.stack_plan("gerrit-jenkins-argocd", "raw", root=Path(directory))
            bridge = next(a for a in plan["auxiliary_adapters"] if a["component"] == "jenkins-gerrit-review-bridge")
            self.assertFalse(bridge["artifacts_available"])
            self.assertFalse(bridge["argocd_handoff_available"])
            self.assertFalse(bridge["result_scanner_available"])
            self.assertFalse(bridge["host_poller_renderer_available"])
            self.assertFalse(bridge["kubernetes_poller_renderer_available"])
            self.assertFalse(bridge["poller_image_context_preparer_available"])
            self.assertFalse(bridge["disposable_reconciliation_fixture_available"])

    def test_gitlab_template_does_not_close_repository_ha_or_runner_gaps(self):
        expected = {
            "gitlab:praefect-gitaly-cluster-and-metadata-database",
            "gitlab:repository-migration-and-replica-consistency",
            "gitlab:runner-registration-execution-and-gitops-handoff",
        }
        for stack, components in planner.STACKS.items():
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                actual = {gap for gap in plan["gaps"] if gap.startswith("gitlab:")}
                self.assertEqual(actual, expected if "gitlab-ce" in components else set())
                adapters = [a for a in plan["auxiliary_adapters"] if a["component"] == "gitlab-praefect-fresh-chart-overlay"]
                self.assertEqual(len(adapters), int("gitlab-ce" in components))
                if adapters:
                    self.assertEqual(adapters[0]["runtime_supported"], runtime in {"rke2", "k3s", "k8s"})
                    self.assertTrue(adapters[0]["artifacts_available"])
                    self.assertFalse(adapters[0]["repository_failover_verified"])
                hosts = [a for a in plan["auxiliary_adapters"] if a["component"] == "gitlab-repository-hosts"]
                self.assertEqual(len(hosts), int("gitlab-ce" in components))
                if hosts:
                    self.assertEqual(hosts[0]["runtime_supported"], runtime in {"raw", "docker"})
                    self.assertTrue(hosts[0]["artifacts_available"])
                    self.assertFalse(hosts[0]["live_verified"])
                entry = [a for a in plan["auxiliary_adapters"] if a["component"] == "gitlab-traefik-ssh-entry"]
                self.assertEqual(len(entry), int("gitlab-ce" in components))
                if entry:
                    self.assertTrue(entry[0]["artifacts_available"])
                    self.assertFalse(entry[0]["git_ssh_verified"])
                self.assertFalse(plan["deployment_ready"])

    def test_gerrit_review_submission_does_not_certify_pipeline_execution_or_votes(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapters = [a for a in plan["auxiliary_adapters"] if a["component"] == "gerrit-tekton-review-submission"]
                self.assertEqual(bool(adapters), stack == "gerrit-tekton-argocd")
                if adapters:
                    adapter = adapters[0]
                    self.assertTrue(adapter["artifacts_available"])
                    self.assertTrue(adapter["current_patchset_polling_implemented"])
                    self.assertTrue(adapter["manual_patchset_event_submission_implemented"])
                    self.assertTrue(adapter["separate_verified_result_reporter_implemented"])
                    self.assertEqual(adapter["reporter"], "scripts/tekton_gerrit_reporter.py")
                    self.assertTrue(adapter["explicit_merged_exact_head_gitops_handoff_implemented"])
                    self.assertEqual(adapter["handoff_renderer"], "scripts/render_gerrit_gitops_handoff.py")
                    self.assertEqual(adapter["handoff"], "scripts/gerrit_gitops_handoff.py")
                    self.assertTrue(adapter["requires_explicit_kubernetes_target"])
                    for key in ("event_integration_verified", "result_reporting_verified", "cd_handoff_verified", "application_ci_verified", "live_verified"):
                        self.assertIs(adapter[key], False)
                    self.assertIn("integration:tekton:" + runtime, plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_missing_gerrit_handoff_cannot_be_advertised_as_available(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = planner.stack_plan("gerrit-tekton-argocd", "raw", root=Path(directory))
            adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "gerrit-tekton-review-submission")
            self.assertFalse(adapter["artifacts_available"])
            self.assertFalse(adapter["cd_handoff_verified"])
            self.assertFalse(plan["deployment_ready"])

    def test_valkey_sentinel_package_does_not_certify_applications_or_lossless_failover(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "valkey-replication-sentinel-package")
                for field in ("artifacts_available", "runtime_packaging_implemented", "tls_replication_and_sentinel_configuration_implemented", "scoped_private_auth_materialization_implemented", "preserved_mutable_role_state_implemented"):
                    self.assertIs(adapter[field], True)
                self.assertEqual(adapter["deployment_helper"], "scripts/deploy_valkey_ha.py")
                self.assertEqual(adapter["runtime_verification_helper"], "scripts/verify_valkey_runtime.py")
                self.assertTrue(adapter["runtime_observation_implemented"])
                self.assertTrue(adapter["expiring_replication_canary_implemented"])
                self.assertTrue(adapter["guarded_installation_implemented"])
                self.assertEqual(adapter["host_activation_helper"], "scripts/activate_valkey_host.py")
                self.assertEqual(adapter["argocd_cache_client_helper"], "scripts/argocd_valkey.py")
                self.assertTrue(adapter["argocd_cache_client_preparation_implemented"])
                self.assertEqual(adapter["host_activation_implemented"], runtime in {"raw", "docker"})
                for field in ("application_compatibility_verified", "lossless_failover_verified", "physical_node_loss_verified", "ha_verified", "live_verified"):
                    self.assertIs(adapter[field], False)
                self.assertIn("foundation:application-compatible-valkey-replication-and-failover", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_openbao_package_is_not_production_key_custody_or_cluster_acceptance(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "openbao-raft-package")
                self.assertTrue(adapter["artifacts_available"]); self.assertTrue(adapter["runtime_packaging_implemented"])
                self.assertEqual(adapter["guarded_installation_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(adapter["deployment_helper"], "scripts/deploy_openbao_ha.py")
                self.assertEqual(adapter["snapshot_capture_helper"], "scripts/openbao_snapshot.py")
                self.assertEqual(adapter["runtime_verification_helper"], "scripts/verify_openbao_runtime.py")
                self.assertEqual(adapter["host_activation_helper"], "scripts/activate_openbao_host.py")
                self.assertEqual(adapter["guarded_host_activation_implemented"], runtime in {"docker", "raw"})
                self.assertTrue(adapter["read_only_runtime_observations_implemented"])
                self.assertTrue(adapter["scoped_snapshot_capture_implemented"])
                self.assertTrue(adapter["offline_snapshot_integrity_check_implemented"])
                for field in ("production_key_custody_verified", "restricted_workload_identities_verified",
                              "off_platform_restore_verified", "physical_node_loss_verified", "ha_verified", "live_verified"):
                    self.assertFalse(adapter[field])
                self.assertIn("foundation:openbao-ha-unseal-recovery-identities-and-audit", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_identity_packages_do_not_certify_mfa_or_session_failover(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "keycloak-ha-package")
                self.assertTrue(adapter["artifacts_available"]); self.assertTrue(adapter["runtime_packaging_implemented"])
                self.assertTrue(adapter["new_realm_mfa_definition_implemented"])
                self.assertTrue(adapter["guarded_new_realm_bootstrap_implemented"])
                self.assertEqual(adapter["realm_bootstrap_helper"], "scripts/bootstrap_keycloak_realm.py")
                self.assertTrue(adapter["guarded_argocd_client_and_private_handoff_implemented"])
                self.assertEqual(adapter["argocd_identity_helper"], "scripts/configure_keycloak_argocd.py")
                self.assertEqual(adapter["argocd_application_helper"], "scripts/apply_argocd_identity.py")
                self.assertTrue(adapter["guarded_argocd_configuration_and_restart_implemented"])
                self.assertTrue(adapter["disposable_runtime_fixture_available"])
                self.assertEqual(adapter["guarded_installation_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(adapter["host_activation_helper"], "scripts/activate_keycloak_host.py")
                self.assertEqual(adapter["guarded_host_activation_implemented"], runtime in {"docker", "raw"})
                for key in ("application_integration_verified", "mfa_verified", "session_failover_verified", "live_verified"): self.assertFalse(adapter[key])
                self.assertIn("foundation:keycloak-ha-database-mfa-and-authentication-integration", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_gerrit_storage_guard_is_available_without_claiming_storage_acceptance(self):
        for stack in planner.STACKS:
            if "gerrit" not in planner.STACKS[stack]: continue
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "gerrit-initialized-sites")
                self.assertTrue(adapter["artifacts_available"]); self.assertTrue(adapter["shared_cephfs_guard_implemented"])
                self.assertFalse(adapter["storage_acceptance_verified"]); self.assertFalse(plan["deployment_ready"])

    def test_ceph_provider_resources_do_not_imply_client_or_failure_acceptance(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                storage = next(a for a in planner.stack_plan(stack, runtime)["auxiliary_adapters"] if a["component"] == "ceph-replicated-storage")
                self.assertTrue(storage["artifacts_available"]); self.assertTrue(storage["storage_resource_provisioning_implemented"])
                self.assertTrue(storage["guarded_cluster_creation_implemented"]); self.assertTrue(storage["device_probe_implemented"])
                self.assertTrue(storage["operator_bootstrap_implemented"])
                self.assertTrue(storage["separate_operator_namespace_implemented"]); self.assertTrue(storage["csi_access_topology_implemented"])
                self.assertTrue(storage["kubernetes_client_io_check_implemented"]); self.assertFalse(storage["native_client_io_check_implemented"])
                self.assertTrue(storage["native_cephfs_io_check_implemented"])
                self.assertTrue(storage["docker_cephfs_execution_implemented"])
                self.assertTrue(storage["native_cephfs_mount_preparation_implemented"])
                self.assertEqual(storage["provider"], "prepared-rook-kubernetes-cluster"); self.assertEqual(storage["consumer_runtime"], runtime)
                for field in ("provider_installation_implemented", "client_mounts_verified", "data_roundtrip_verified", "node_loss_verified", "ha_verified", "live_verified"):
                    self.assertFalse(storage[field])

    def test_key_recovery_discovery_requires_the_complete_optional_package(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                storage = next(a for a in plan["auxiliary_adapters"] if a["component"] == "ceph-replicated-storage")
                self.assertEqual(storage["host_osd_key_recovery_helper"], "scripts/ceph_key_recovery.py")
                self.assertTrue(storage["host_osd_key_recovery_available"])
                for field in ("provider_key_capture_verified", "provider_key_escrow_verified", "provider_restoration_verified"):
                    self.assertIs(storage[field], False)
                self.assertFalse(plan["deployment_ready"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ("scripts/ceph_key_recovery.py", "scripts/ceph_key_material.py",
                     "examples/ceph-key-recovery.json", "docs/CEPH_KEY_RECOVERY.md")
            for path in paths:
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("fixture", encoding="utf-8")
            def available():
                plan = planner.stack_plan("gerrit-zuul-flux", "raw", root=root)
                return next(a for a in plan["auxiliary_adapters"] if a["component"] == "ceph-replicated-storage")["host_osd_key_recovery_available"]
            self.assertTrue(available())
            for path in paths:
                file = root / path
                file.unlink()
                self.assertFalse(available(), path)
                file.write_text("fixture", encoding="utf-8")

    def test_database_policy_is_available_without_claiming_database_acceptance(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                database = next(a for a in plan["auxiliary_adapters"] if a["component"] == "postgresql-ha-policy")
                self.assertTrue(database["artifacts_available"])
                self.assertEqual(database["backend"], "cloudnative-pg" if runtime in {"rke2", "k3s", "k8s"} else "patroni-zookeeper")
                self.assertEqual(database["credential_materialization_implemented"], runtime in {"docker", "raw"})
                self.assertEqual(database["guarded_host_startup_implemented"], runtime in {"docker", "raw"})
                self.assertEqual(database["patroni_wheel_builder"],
                                 "scripts/build_patroni_zookeeper_tls.py" if runtime in {"docker", "raw"} else None)
                self.assertEqual(database["host_application_provisioner"],
                                 "scripts/provision_patroni_application.py" if runtime in {"docker", "raw"} else None)
                self.assertEqual(database["host_write_endpoint_renderer"],
                                 "scripts/render_postgres_ha.py" if runtime in {"docker", "raw"} else None)
                self.assertEqual(database["guarded_installation_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(database["runtime_observation_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(database["planned_switchover_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertTrue(database["physical_backup_capture_implemented"])
                self.assertTrue(database["database_restore_drill_implemented"])
                self.assertFalse(database["off_platform_backup_verified"])
                self.assertEqual(database["operator_installation_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(database["operator_bootstrap_helper"],
                                 "scripts/deploy_cnpg_operator.py" if runtime in {"rke2", "k3s", "k8s"} else None)
                for field in ("upgrade_implemented", "synchronous_replication_verified", "promotion_verified", "restore_verified", "live_verified"):
                    self.assertFalse(database[field])
                self.assertIn("foundation:postgresql-ha-promotion-and-synchronous-replication-policy", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_full_requested_cross_product(self):
        self.assertEqual(len(planner.STACKS), 10)
        self.assertEqual(set(planner.RUNTIMES), {"rke2", "docker", "raw", "k3s", "k8s"})
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                with self.subTest(stack=stack, runtime=runtime):
                    plan = planner.stack_plan(stack, runtime)
                    self.assertTrue(plan["read_only"])
                    self.assertFalse(plan["deployment_ready"])
                    self.assertEqual(len(plan["components"]), 3)
                    self.assertEqual(len(set(plan["gaps"])), len(plan["gaps"]))
                    for requirement in planner.FOUNDATION:
                        self.assertIn(f"foundation:{requirement}", plan["gaps"])
                    for requirement in planner.ACCEPTANCE:
                        self.assertIn(f"acceptance:{requirement}", plan["gaps"])

    def test_optional_operator_discovery_requires_the_complete_package(self):
        paths = ("scripts/render_cnpg_operator.py", "scripts/deploy_cnpg_operator.py", "scripts/cnpg_release.py",
                 "examples/cnpg-operator.json", "docs/CNPG_OPERATOR_BOOTSTRAP.md")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("fixture", encoding="utf-8")
            def available(runtime):
                plan = planner.stack_plan("gerrit-zuul-flux", runtime, root=root)
                return next(a for a in plan["auxiliary_adapters"] if a["component"] == "postgresql-ha-policy")["operator_installation_implemented"]
            self.assertTrue(available("rke2"))
            self.assertFalse(available("raw"))
            self.assertFalse(available("docker"))
            for path in paths:
                file = root / path
                file.unlink()
                self.assertFalse(available("k8s"), path)
                file.write_text("fixture", encoding="utf-8")

    def test_existing_paths_are_real(self):
        for path in planner.EXISTING_COMPONENT_PATHS.values():
            self.assertTrue((planner.ROOT / path).is_dir(), path)

    def test_cni_packages_preserve_managed_migration_and_live_acceptance_gaps(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "cilium-cni-lifecycle")
                self.assertTrue(adapter["artifacts_available"])
                self.assertTrue(adapter["fresh_installation_implemented"])
                self.assertTrue(adapter["owned_patch_upgrade_implemented"])
                self.assertFalse(adapter["rke2_managed_migration_implemented"])
                self.assertFalse(adapter["live_verified"])
                self.assertFalse(plan["deployment_ready"])

    def test_gateway_routes_do_not_replace_cni_bootstrap_or_shared_foundation_acceptance(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "cilium-gateway-routes")
                self.assertTrue(adapter["artifacts_available"])
                self.assertTrue(adapter["requires_explicit_kubernetes_target"])
                self.assertFalse(adapter["cni_installation_implemented"])
                self.assertFalse(adapter["external_entry_verified"])
                self.assertIn("foundation:cilium-gateway-api-envoy-redundant-serving-nodes", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_external_entry_packages_preserve_failover_and_native_installation_gaps(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(a for a in plan["auxiliary_adapters"] if a["component"] == "gateway-external-entry")
                self.assertTrue(adapter["artifacts_available"])
                self.assertEqual(adapter["requires_explicit_kubernetes_target"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(adapter["guarded_kubernetes_rollout_implemented"], runtime in {"rke2", "k3s", "k8s"})
                self.assertEqual(adapter["native_host_installation_implemented"], runtime == "raw")
                self.assertEqual(adapter["host_lifecycle_implemented"], runtime in {"docker", "raw"})
                self.assertEqual(adapter["staged_pair_orchestration_implemented"], runtime in {"docker", "raw"})
                self.assertEqual(adapter["pair_input_distribution_implemented"], runtime in {"docker", "raw"})
                self.assertEqual(adapter["pair_terminated_job_recovery_implemented"], runtime in {"docker", "raw"})
                self.assertFalse(adapter["vip_failover_verified"])
                self.assertFalse(adapter["application_health_verified"])
                self.assertFalse(adapter["live_verified"])
                self.assertFalse(plan["deployment_ready"])

    def test_gerrit_router_is_not_mistaken_for_gerrit_installation(self):
        for runtime in planner.RUNTIMES:
            plan = planner.stack_plan("gerrit-zuul-argocd", runtime)
            adapter = plan["auxiliary_adapters"][0]
            self.assertTrue(adapter["artifacts_available"])
            self.assertFalse(adapter["live_verified"])
            self.assertIn(f"deployment-adapter:gerrit:{runtime}", plan["gaps"])
            self.assertFalse(plan["deployment_ready"])

    def test_absent_templates_are_not_reported_as_available(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = planner.stack_plan("forgejo-woodpecker-argocd", "rke2", root=Path(directory))
            self.assertTrue(all(c["existing_template"] is None for c in plan["components"]))

    def test_canary_is_never_treated_as_application_ci_or_full_stack_acceptance(self):
        for runtime in planner.RUNTIMES:
            plan = planner.stack_plan("gerrit-zuul-argocd", runtime)
            canary = next(item for item in plan["auxiliary_adapters"] if item["component"] == "zuul-review-worker-canary")
            self.assertTrue(canary["artifacts_available"])
            self.assertFalse(canary["application_ci_verified"])
            self.assertFalse(canary["live_verified"])
            self.assertIn(f"integration:zuul:{runtime}", plan["gaps"])
            with tempfile.TemporaryDirectory() as directory:
                absent = planner.stack_plan("gerrit-zuul-flux", runtime, root=Path(directory))
                item = next(item for item in absent["auxiliary_adapters"] if item["component"] == "zuul-review-worker-canary")
                self.assertFalse(item["artifacts_available"])

    def test_coordination_packages_do_not_certify_a_live_quorum(self):
        for stack in ("gerrit-zuul-argocd", "gerrit-zuul-flux"):
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                coordination = next(item for item in plan["auxiliary_adapters"] if item["component"] == "zookeeper-three-member")
                self.assertTrue(coordination["artifacts_available"])
                self.assertFalse(coordination["quorum_verified"])
                self.assertFalse(coordination["live_verified"])
                self.assertFalse(plan["deployment_ready"])

    def test_kubernetes_files_do_not_claim_native_or_compose_support(self):
        for runtime in ("docker", "raw"):
            for forge in ("forgejo", "gitea"):
                plan = planner.stack_plan(forge + "-woodpecker-argocd", runtime)
                components = {row["component"]: row for row in plan["components"]}
                self.assertEqual(components[forge]["existing_template"], "scripts/render_forge_host.py")
                self.assertTrue(components[forge]["template_family_matches_runtime"])
                self.assertFalse(components[forge]["integration_verified"])
                self.assertEqual(components["woodpecker"]["existing_template"], "scripts/render_woodpecker_host.py")
                self.assertEqual(components["woodpecker"]["host_agent_renderer"], "scripts/render_woodpecker_agent.py")
                self.assertTrue(components["woodpecker"]["template_family_matches_runtime"])
                self.assertFalse(components["woodpecker"]["integration_verified"])
                self.assertFalse(components["argocd"]["template_family_matches_runtime"])
                self.assertIn("explicit-kubernetes-management-and-workload-target", plan["gaps"])

    def test_additional_forges_not_lost(self):
        self.assertEqual(set(planner.ADDITIONAL_FORGES), {
            "onedev", "scm-manager", "allura", "rhodecode", "sourcehut",
            "gitbucket", "kallithea", "gogs",
        })

    def test_additional_forge_artifacts_are_visible_without_certifying_deployment(self):
        for runtime in planner.RUNTIMES:
            with self.subTest(runtime=runtime):
                plan = planner.additional_forge_plan("gitbucket", runtime)
                self.assertEqual(plan["renderer"], "scripts/render_gitbucket.py")
                self.assertEqual(plan["topology"], "single-active")
                self.assertFalse(plan["deployment_ready"])
                self.assertIn("acceptance:git-ssh-https-clone-push-and-write-routing-tests", plan["gaps"])
                sourcehut = planner.additional_forge_plan("sourcehut", runtime)
                self.assertEqual(sourcehut["renderer"], "scripts/render_sourcehut.py")
                self.assertFalse(sourcehut["deployment_ready"])
                self.assertIn("integration:sourcehut-ssh-write-routing", sourcehut["gaps"])
                rhodecode = planner.additional_forge_plan("rhodecode", runtime)
                self.assertEqual(rhodecode["renderer"], "scripts/render_rhodecode.py")
                self.assertEqual(rhodecode["topology"], "single-active")
                self.assertFalse(rhodecode["deployment_ready"])
                gogs = planner.additional_forge_plan("gogs", runtime)
                self.assertEqual(gogs["renderer"], "scripts/render_gogs.py")
                self.assertFalse(gogs["deployment_ready"])
                scm_manager = planner.additional_forge_plan("scm-manager", runtime)
                self.assertEqual(scm_manager["renderer"], "scripts/render_scm_manager.py")
                self.assertEqual(scm_manager["topology"], "single-active")
                self.assertFalse(scm_manager["deployment_ready"])
                onedev = planner.additional_forge_plan("onedev", runtime)
                self.assertEqual(onedev["renderer"], "scripts/render_onedev.py")
                self.assertEqual(onedev["topology"], "single-active")
                self.assertFalse(onedev["deployment_ready"])
                kallithea = planner.additional_forge_plan("kallithea", runtime)
                self.assertEqual(kallithea["renderer"], "scripts/render_kallithea.py")
                self.assertEqual(kallithea["topology"], "single-active")
                self.assertFalse(kallithea["deployment_ready"])
                allura = planner.additional_forge_plan("allura", runtime)
                self.assertEqual(allura["renderer"], "scripts/render_allura.py")
                self.assertEqual(allura["topology"], "single-active")
                self.assertFalse(allura["deployment_ready"])
                self.assertIn("integration:allura-gateway", allura["gaps"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(planner.main(["--forge", "gitbucket", "--require-ready"]), 2)

    def test_flux_adapter_requires_kubernetes_and_preserves_full_stack_acceptance_gaps(self):
        for stack in ("gerrit-zuul-flux", "gitlab-ce-runner-flux"):
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(item for item in plan["auxiliary_adapters"] if item["component"] == "flux-management-target")
                self.assertTrue(adapter["artifacts_available"])
                self.assertTrue(adapter["requires_explicit_kubernetes_target"])
                self.assertFalse(adapter["artifact_failover_verified"])
                self.assertFalse(adapter["live_verified"])
                self.assertIn(f"integration:flux:{runtime}", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_tekton_packages_do_not_claim_scm_integration_or_live_pipeline_acceptance(self):
        for stack in ("gerrit-tekton-argocd", "forgejo-tekton-argocd", "gitea-tekton-argocd"):
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                adapter = next(item for item in plan["auxiliary_adapters"] if item["component"] == "tekton-management-target")
                self.assertTrue(adapter["artifacts_available"])
                self.assertTrue(adapter["requires_explicit_kubernetes_target"])
                for field in ("canary_completed", "event_integration_verified", "application_ci_verified", "live_verified"):
                    self.assertFalse(adapter[field])
                self.assertIn(f"integration:tekton:{runtime}", plan["gaps"])
                self.assertFalse(plan["deployment_ready"])

    def test_cli_and_readiness_gate(self):
        for args, expected in (([], 0), (["--require-ready"], 2), (["--list"], 0)):
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(planner.main(args), expected)
            self.assertIsInstance(json.loads(stream.getvalue()), dict)

    def test_push_bridge_is_only_offered_for_the_matching_forges(self):
        for runtime in planner.RUNTIMES:
            for forge in ("forgejo", "gitea"):
                plan = planner.stack_plan(forge + "-tekton-argocd", runtime)
                bridge = next(item for item in plan["auxiliary_adapters"] if item["component"] == "forge-tekton-push-bridge")
                self.assertTrue(bridge["artifacts_available"])
                self.assertFalse(bridge["event_integration_verified"])
                self.assertFalse(bridge["result_reporting_verified"])
                self.assertFalse(bridge["cd_handoff_verified"])
                self.assertFalse(bridge["application_ci_verified"])
            plan = planner.stack_plan("gerrit-tekton-argocd", runtime)
            self.assertNotIn("forge-tekton-push-bridge", {item["component"] for item in plan["auxiliary_adapters"]})

    def test_trusted_static_publication_and_promotion_are_scoped_to_zuul(self):
        for stack in planner.STACKS:
            for runtime in planner.RUNTIMES:
                plan = planner.stack_plan(stack, runtime)
                matches = [value for value in plan["auxiliary_adapters"] if value["component"] == "zuul-static-image-publication"]
                self.assertEqual(len(matches), 1 if "zuul" in planner.STACKS[stack] else 0)
                if matches:
                    self.assertTrue(matches[0]["artifacts_available"])
                    self.assertTrue(matches[0]["trusted_post_review_job_preparation_implemented"])
                    self.assertTrue(matches[0]["artifact_promotion_implemented"])
                    for key in ("artifact_publication_verified", "application_ci_verified", "live_verified"):
                        self.assertFalse(matches[0][key])
                self.assertFalse(plan["deployment_ready"])

    def test_invalid_selection_rejected(self):
        with self.assertRaises(ValueError):
            planner.stack_plan("unknown", "rke2")
        with self.assertRaises(ValueError):
            planner.stack_plan("forgejo-woodpecker-argocd", "unknown")


if __name__ == "__main__":
    unittest.main()
