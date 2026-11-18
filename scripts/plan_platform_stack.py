#!/usr/bin/env python3
"""Read-only implementation-gap planner for the requested multi-runtime stacks.

This is not a deployment command or an HA certification. Existing templates are
reported separately from the missing integration and live acceptance evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("rke2", "docker", "raw", "k3s", "k8s")
STACKS = {
    "gerrit-zuul-argocd": ("gerrit", "zuul", "argocd"),
    "gitlab-ce-runner-argocd": ("gitlab-ce", "gitlab-runner", "argocd"),
    "gerrit-zuul-flux": ("gerrit", "zuul", "flux"),
    "gerrit-tekton-argocd": ("gerrit", "tekton", "argocd"),
    "gitlab-ce-runner-flux": ("gitlab-ce", "gitlab-runner", "flux"),
    "gerrit-jenkins-argocd": ("gerrit", "jenkins", "argocd"),
    "forgejo-tekton-argocd": ("forgejo", "tekton", "argocd"),
    "forgejo-woodpecker-argocd": ("forgejo", "woodpecker", "argocd"),
    "gitea-tekton-argocd": ("gitea", "tekton", "argocd"),
    "gitea-woodpecker-argocd": ("gitea", "woodpecker", "argocd"),
}
ADDITIONAL_FORGES = (
    "onedev", "scm-manager", "allura", "rhodecode", "sourcehut",
    "gitbucket", "kallithea", "gogs",
)
# These are implementation starting points, NOT assertions of full support.
EXISTING_COMPONENT_PATHS = {
    "forgejo": "gitops/clusters/rke2-main/premium-3node/apps/forgejo",
    "gitea": "gitops/clusters/rke2-main/alternatives/gitea",
    "gitlab-ce": "gitops/clusters/rke2-main/premium-3node/apps/gitlab-ce",
    "gitlab-runner": "gitops/clusters/rke2-main/premium-3node/apps/gitlab-runner",
    "woodpecker": "gitops/clusters/rke2-main/premium-3node/apps/woodpecker",
    "argocd": "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha",
}
FORGE_HOSTS = {"forgejo", "gitea"}
FOUNDATION = (
    "three-control-plane-quorum-and-stable-api",
    "cilium-network-policy-and-redundant-network-paths",
    "cilium-gateway-api-envoy-redundant-serving-nodes",
    "haproxy-community-redundant-vip-and-git-write-routing",
    "postgresql-ha-promotion-and-synchronous-replication-policy",
    "ceph-failure-domains-access-services-and-recovery-capacity",
    "keycloak-ha-database-mfa-and-authentication-integration",
    "openbao-ha-unseal-recovery-identities-and-audit",
    "harbor-ha-database-cache-and-storage",
    "application-compatible-valkey-replication-and-failover",
    "kyverno-ha-and-cosign-image-verification",
    "monitoring-centralized-logs-and-alerts",
    "off-platform-backups-tested-restore-and-independent-bootstrap",
)
ACCEPTANCE = (
    "pinned-artifact-provenance-license-and-edition-review",
    "install-render-schema-and-idempotence-tests",
    "upgrade-backup-restore-and-rollback-tests",
    "sso-rbac-secret-isolation-and-mail-tests",
    "git-ssh-https-clone-push-and-write-routing-tests",
    "review-event-ci-trigger-result-and-artifact-tests",
    "gitops-reconciliation-drift-and-rollout-tests",
    "one-node-loss-recovery-quorum-rpo-rto-and-load-tests",
)


def component_evidence(component: str, runtime: str, root: Path) -> dict:
    path = EXISTING_COMPONENT_PATHS.get(component)
    # A Kubernetes template is not evidence for Docker/native deployment.
    applicable = runtime in {"rke2", "k3s", "k8s"}
    present = bool(path and (root / path).is_dir())
    if component in FORGE_HOSTS and runtime in {"docker", "raw"}:
        applicable = True
        path = "scripts/render_forge_host.py"
        present = (root / path).is_file()
    host_woodpecker = component == "woodpecker" and runtime in {"docker", "raw"}
    if host_woodpecker:
        applicable = True
        path = "scripts/render_woodpecker_host.py"
        present = (root / path).is_file()
    evidence = {
        "component": component,
        "existing_template": path if present else None,
        "template_family_matches_runtime": applicable and present,
        "integration_verified": False,
    }
    if host_woodpecker:
        renderer = "scripts/render_woodpecker_agent.py"
        evidence["host_agent_renderer"] = renderer if (root / renderer).is_file() else None
    return evidence


def additional_forge_plan(forge: str, runtime: str, *, root: Path = ROOT) -> dict:
    if forge not in ADDITIONAL_FORGES or runtime not in RUNTIMES:
        raise ValueError("Unknown forge or runtime")
    renderer = {
        "allura": "scripts/render_allura.py",
        "gitbucket": "scripts/render_gitbucket.py",
        "gogs": "scripts/render_gogs.py",
        "kallithea": "scripts/render_kallithea.py",
        "onedev": "scripts/render_onedev.py",
        "rhodecode": "scripts/render_rhodecode.py",
        "scm-manager": "scripts/render_scm_manager.py",
        "sourcehut": "scripts/render_sourcehut.py",
    }.get(forge)
    available = renderer is not None and (root / renderer).is_file()
    return {
        "schema_version": 1,
        "forge": forge,
        "runtime": runtime,
        "read_only": True,
        "renderer": renderer if available else None,
        "artifacts_available": available,
        "topology": "single-active" if available else None,
        "deployment_ready": False,
        "gaps": ([f"deployment-adapter:{forge}:{runtime}"] if not available else [])
        + (["integration:allura-gateway", "integration:allura-external-mongodb-and-solr"]
           if forge == "allura" and available else [])
        + (["integration:sourcehut-ssh-write-routing", "integration:sourcehut-postgresql-redis-and-mail"]
           if forge == "sourcehut" and available else [])
        + [f"foundation:{requirement}" for requirement in FOUNDATION]
        + [f"acceptance:{requirement}" for requirement in ACCEPTANCE],
    }


def stack_plan(stack: str, runtime: str, *, root: Path = ROOT) -> dict:
    if stack not in STACKS or runtime not in RUNTIMES:
        raise ValueError("Unknown stack or runtime")
    components = [component_evidence(c, runtime, root) for c in STACKS[stack]]
    gaps = [f"foundation:{requirement}" for requirement in FOUNDATION]
    gaps += [f"acceptance:{requirement}" for requirement in ACCEPTANCE]
    for component in components:
        if not component["template_family_matches_runtime"]:
            gaps.append(f"deployment-adapter:{component['component']}:{runtime}")
        gaps.append(f"integration:{component['component']}:{runtime}")
    if runtime != "rke2":
        gaps.append(f"runtime-bootstrap-and-lifecycle:{runtime}")
    if runtime in {"docker", "raw"}:
        gaps.append("explicit-kubernetes-management-and-workload-target")
    if "gitlab-ce" in STACKS[stack]:
        gaps.extend((
            "gitlab:praefect-gitaly-cluster-and-metadata-database",
            "gitlab:repository-migration-and-replica-consistency",
            "gitlab:runner-registration-execution-and-gitops-handoff",
        ))
    return {
        "schema_version": 1,
        "stack": stack,
        "runtime": runtime,
        "read_only": True,
        "deployment_ready": False,
        "components": components,
        "auxiliary_adapters": [{
            "component": "offline-gitops-source-recovery",
            "bundle_tool": "scripts/bootstrap_git_bundle.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/bootstrap_git_bundle.py", "scripts/bounded_subprocess.py",
            )),
            "off_platform_bootstrap_verified": False,
            "live_verified": False,
        }, {
            "component": "offline-argocd-bootstrap",
            "kit_tool": "scripts/prepare_argocd_recovery.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/prepare_argocd_recovery.py", "scripts/verify_oci_archive.py",
                "scripts/render_argocd_offline_images.py", "examples/argocd-v3.5.0-linux-amd64-images.json",
            )),
            "image_platform": "linux/amd64",
            "live_verified": False,
        }, {
            "component": "external-endpoint-monitoring",
            "renderer": "scripts/render_external_monitoring.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/render_external_monitoring.py", "examples/external-monitoring.json",
            )),
            "monitor_host_runtimes": ["raw", "docker"],
            "platform_runtime": runtime,
            "live_verified": False,
        }, {
            "component": "centralized-host-logs",
            "renderer": "scripts/render_host_log_collector.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/render_host_log_collector.py", "examples/host-log-collector.json",
            )),
            "runtime_supported": runtime in {"raw", "docker"},
            "requires_authenticated_loki_gateway": True,
            "live_verified": False,
        }, {
            "component": "centralized-kubernetes-pod-logs",
            "renderer": "scripts/render_kubernetes_log_collector.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/render_kubernetes_log_collector.py", "examples/kubernetes-log-collector.json",
            )),
            "runtime_supported": runtime in {"rke2", "k3s", "k8s"},
            "requires_authenticated_loki_gateway": True,
            "live_verified": False,
        }, {
            "component": "replicated-loki-receiver",
            "renderer": "scripts/render_loki_receiver.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/render_loki_receiver.py", "examples/loki-receiver.json",
                "gitops/clusters/rke2-main/premium-3node/apps/loki/charts/loki/Chart.yaml",
            )),
            "receiver_host_runtimes": ["rke2", "k3s", "k8s"],
            "requires_external_s3": True,
            "live_verified": False,
        }] + ([{
            "component": "jenkins-single-active-controller",
            "renderer": "scripts/render_jenkins_host.py" if runtime in {"raw", "docker"} else "scripts/render_jenkins_controller.py",
            "artifacts_available": (root / ("scripts/render_jenkins_host.py" if runtime in {"raw", "docker"} else "scripts/render_jenkins_controller.py")).is_file(),
            "host_renderer": "scripts/render_jenkins_host.py",
            "host_render_available": (root / "scripts/render_jenkins_host.py").is_file(),
            "runtime_supported": (root / ("scripts/render_jenkins_host.py" if runtime in {"raw", "docker"} else "scripts/render_jenkins_controller.py")).is_file(),
            "gerrit_integration_verified": False,
            "controller_recovery_verified": False,
            "live_verified": False,
        }, {
            "component": "jenkins-gerrit-review-bridge",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in (
                "render_jenkins_gerrit_job.py", "scan_jenkins_gerrit.py", "trigger_jenkins_gerrit.py",
                "resolve_jenkins_gerrit_build.py", "verify_jenkins_gerrit.py", "report_jenkins_gerrit.py")),
            "job_renderer": "scripts/render_jenkins_gerrit_job.py",
            "scanner": "scripts/scan_jenkins_gerrit.py",
            "result_reporter": "scripts/report_jenkins_gerrit.py",
            "result_scanner": "scripts/scan_jenkins_gerrit_results.py",
            "result_scanner_available": (root / "scripts/scan_jenkins_gerrit_results.py").is_file(),
            "host_poller_renderer": "scripts/render_jenkins_pollers.py",
            "host_poller_renderer_available": (root / "scripts/render_jenkins_pollers.py").is_file(),
            "kubernetes_poller_renderer": "scripts/render_jenkins_pollers_k8s.py",
            "kubernetes_poller_renderer_available": (root / "scripts/render_jenkins_pollers_k8s.py").is_file(),
            "poller_image_context_preparer": "scripts/prepare_jenkins_poller_image.py",
            "poller_image_context_preparer_available": (root / "scripts/prepare_jenkins_poller_image.py").is_file(),
            "poller_image_and_storage_verified": False,
            "argocd_handoff": "scripts/jenkins_gerrit_gitops_handoff.py",
            "argocd_handoff_available": (root / "scripts/jenkins_gerrit_gitops_handoff.py").is_file(),
            "disposable_reconciliation_fixture": "scripts/test_jenkins_gerrit_real_fixture.py",
            "disposable_reconciliation_fixture_available": all((root / "scripts" / name).is_file() for name in (
                "test_jenkins_gerrit_real_fixture.py", "test_jenkins_native_runtime_fixture.py", "test_argocd_reconciliation_fixture.py")),
            "requires_explicit_kubernetes_target": True,
            "explicit_controller_trust_required": True,
            "independent_checkout_provenance_verified": False,
            "production_event_and_vote_integration_verified": False,
            "gitops_handoff_verified": False,
            "live_verified": False,
        }, {
            "component": "jenkins-signed-artifact-promotion",
            "publisher": "scripts/jenkins_static_image.py",
            "promotion": "scripts/jenkins_artifact_promotion.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/jenkins_static_image.py", "scripts/jenkins_artifact_promotion.py",
                "examples/jenkins-static-image.json", "examples/jenkins-argocd-artifact-promotion.json")),
            "runtime_supported": runtime in RUNTIMES,
            "signed_image_admission_verified": False,
            "live_verified": False,
        }] if "jenkins" in STACKS[stack] else []) + ([{
            "component": "gitlab-traefik-ssh-entry",
            "renderer": "scripts/render_gitlab_traefik_entry.py",
            "artifacts_available": (root / "scripts/render_gitlab_traefik_entry.py").is_file(),
            "runtime_supported": runtime in {"rke2", "k3s", "k8s"},
            "external_listener_verified": False,
            "git_ssh_verified": False,
            "live_verified": False,
        }, {
            "component": "gitlab-praefect-fresh-chart-overlay",
            "renderer": "scripts/render_gitlab_praefect.py",
            "artifacts_available": (root / "scripts/render_gitlab_praefect.py").is_file(),
            "runtime_supported": runtime in {"rke2", "k3s", "k8s"},
            "fresh_install_only": True,
            "repository_migration_implemented": False,
            "database_tls_verified": False,
            "repository_failover_verified": False,
            "live_verified": False,
        }, {
            "component": "gitlab-repository-hosts",
            "renderer": "scripts/render_gitlab_repository_hosts.py",
            "artifacts_available": (root / "scripts/render_gitlab_repository_hosts.py").is_file(),
            "runtime_supported": runtime in {"docker", "raw"},
            "fresh_install_only": True,
            "repository_migration_implemented": False,
            "live_verified": False,
        }, {
            "component": "gitlab-signed-artifact-promotion",
            "publisher": "scripts/gitlab_static_image.py",
            "promotion": "scripts/gitlab_artifact_promotion.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/gitlab_static_image.py", "scripts/gitlab_artifact_promotion.py",
                "examples/gitlab-static-image.json", "examples/gitlab-argocd-artifact-promotion.json",
                "docs/GITLAB_PIPELINE_VERIFICATION.md")),
            "runtime_supported": runtime in RUNTIMES,
            "signed_image_admission_verified": False,
            "live_verified": False,
        }] if "gitlab-ce" in STACKS[stack] else []) + ([{
            "component": "gerrit-router",
            "renderer": "scripts/render_gerrit_router.py",
            "artifacts_available": (root / "scripts/render_gerrit_router.py").is_file(),
            "runtime": runtime,
            "live_verified": False,
        }, {
            "component": "gerrit-initialized-sites",
            "renderer": "scripts/render_gerrit_sites.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_gerrit_sites.py", "gerrit_storage.py")),
            "shared_cephfs_guard_implemented": True,
            "storage_acceptance_verified": False,
            "runtime": runtime,
            "live_verified": False,
        }, {
            "component": "gerrit-fresh-native-initialization",
            "renderer": "scripts/initialize_gerrit_sites.py",
            "artifacts_available": (root / "scripts/initialize_gerrit_sites.py").is_file(),
            "runtime_supported": runtime == "raw",
            "live_verified": False,
        }, {
            "component": "gerrit-container-preparation",
            "renderer": "scripts/render_gerrit_preparation.py",
            "artifacts_available": (root / "scripts/render_gerrit_preparation.py").is_file(),
            "runtime_supported": runtime in {"rke2", "k3s", "k8s", "docker"},
            "live_verified": False,
        }] if "gerrit" in STACKS[stack] else []) + ([{
            "component": "zookeeper-three-member",
            "renderer": "scripts/render_zookeeper.py",
            "artifacts_available": (root / "scripts/render_zookeeper.py").is_file(),
            "runtime": runtime,
            "quorum_verified": False,
            "live_verified": False,
        }, {
            "component": "zuul-configuration",
            "renderer": "scripts/render_zuul_config.py",
            "artifacts_available": (root / "scripts/render_zuul_config.py").is_file(),
            "runtime": runtime,
            "live_verified": False,
        }, {
            "component": "zuul-core-services",
            "renderer": "scripts/render_zuul_services.py",
            "artifacts_available": (root / "scripts/render_zuul_services.py").is_file(),
            "runtime": runtime,
            "executor_container_packaging": True,
            "executor_isolation_verified": False,
            "live_verified": False,
        }, {
            "component": "nodepool-static-configuration",
            "renderer": "scripts/render_nodepool_static.py",
            "artifacts_available": (root / "scripts/render_nodepool_static.py").is_file(),
            "runtime": runtime,
            "allocation_verified": False,
            "live_verified": False,
        }, {
            "component": "nodepool-static-launcher",
            "renderer": "scripts/render_nodepool_services.py",
            "artifacts_available": (root / "scripts/render_nodepool_services.py").is_file(),
            "runtime": runtime,
            "allocation_verified": False,
            "live_verified": False,
        }, {
            "component": "zuul-review-worker-canary",
            "renderer": "scripts/render_zuul_canary.py",
            "artifacts_available": (root / "scripts/render_zuul_canary.py").is_file(),
            "runtime": runtime,
            "application_ci_verified": False,
            "live_verified": False,
        }, {
            "component": "zuul-static-image-publication",
            "renderer": "scripts/render_zuul_static_image.py",
            "publisher": "scripts/zuul_static_image.py",
            "promotion": "scripts/zuul_artifact_promotion.py",
            "runtime": runtime,
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/render_zuul_static_image.py", "scripts/zuul_static_image.py",
                "scripts/source_bound_static_image.py", "scripts/tekton_checkout.py",
                "scripts/publish_harbor_static_image.py", "scripts/atomic_file.py",
                "scripts/zuul_artifact_promotion.py", "scripts/gerrit_artifact_promotion.py",
                "examples/zuul-static-image.json", "examples/zuul-argocd-artifact-promotion.json",
                "examples/zuul-flux-artifact-promotion.json", "docs/ZUUL_STATIC_IMAGE.md",
            )),
            "trusted_post_review_job_preparation_implemented": True,
            "artifact_publication_verified": False,
            "artifact_promotion_implemented": (root / "scripts/zuul_artifact_promotion.py").is_file(),
            "application_ci_verified": False,
            "live_verified": False,
        }, {
            "component": "zuul-gitops-handoff",
            "handoff": "scripts/zuul_gitops_handoff.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in
                                       ("zuul_gitops_handoff.py", "ci_gitops_handoff.py")),
            "runtime": runtime,
            "controller_kind": STACKS[stack][2],
            "requires_explicit_kubernetes_target": True,
            "live_verified": False,
        }] if "zuul" in STACKS[stack] else []) + ([{
            "component": "flux-management-target",
            "renderer": "scripts/render_flux.py",
            "deployment_helper": "scripts/deploy_flux.py",
            "offline_recovery_cache_helper": "scripts/prepare_flux_recovery.py" if (root / "scripts/prepare_flux_recovery.py").is_file() else None,
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_flux.py", "deploy_flux.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "artifact_failover_verified": False,
            "live_verified": False,
        }] if "flux" in STACKS[stack] else []) + ([{
            "component": "tekton-management-target",
            "renderer": "scripts/render_tekton.py",
            "deployment_helper": "scripts/deploy_tekton.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_tekton.py", "deploy_tekton.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "canary_completed": False,
            "event_integration_verified": False,
            "application_ci_verified": False,
            "live_verified": False,
        }] if "tekton" in STACKS[stack] else []) + ([{
            "component": "gerrit-tekton-review-submission",
            "renderer": "scripts/render_tekton_gerrit_bridge.py",
            "submitter": "scripts/tekton_gerrit_bridge.py",
            "reporter_renderer": "scripts/render_tekton_gerrit_reporter.py",
            "reporter": "scripts/tekton_gerrit_reporter.py",
            "handoff_renderer": "scripts/render_gerrit_gitops_handoff.py",
            "handoff": "scripts/gerrit_gitops_handoff.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_tekton_gerrit_bridge.py", "tekton_gerrit_bridge.py", "tekton_checkout.py", "render_tekton_gerrit_reporter.py", "tekton_gerrit_reporter.py", "render_gerrit_gitops_handoff.py", "gerrit_gitops_handoff.py", "ci_gitops_handoff.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "current_patchset_polling_implemented": True,
            "manual_patchset_event_submission_implemented": True,
            "separate_verified_result_reporter_implemented": True,
            "explicit_merged_exact_head_gitops_handoff_implemented": True,
            "event_integration_verified": False,
            "result_reporting_verified": False,
            "cd_handoff_verified": False,
            "application_ci_verified": False,
            "live_verified": False,
        }] if "gerrit" in STACKS[stack] and "tekton" in STACKS[stack] else []) + ([{
            "component": "forge-tekton-push-bridge",
            "renderer": "scripts/render_tekton_forge_bridge.py",
            "receiver": "scripts/tekton_forge_bridge.py",
            "reporter": "scripts/tekton_forge_reporter.py",
            "handoff": "scripts/ci_gitops_handoff.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_tekton_forge_bridge.py", "tekton_forge_bridge.py", "tekton_checkout.py", "tekton_forge_reporter.py", "ci_gitops_handoff.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "event_integration_verified": False,
            "result_reporting_verified": False,
            "cd_handoff_verified": False,
            "application_ci_verified": False,
            "live_verified": False,
        }, {
            "component": "forge-tekton-signed-artifact-promotion",
            "renderer": "scripts/render_tekton_forge_bridge.py",
            "promotion": "scripts/tekton_forge_artifact_promotion.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/tekton_forge_artifact_promotion.py", "scripts/source_bound_static_image.py",
                "examples/tekton-forge-artifact-bridge.json", "examples/tekton-forge-argocd-artifact-promotion.json")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "signed_image_admission_verified": False,
            "live_verified": False,
        }] if "tekton" in STACKS[stack] and any(forge in STACKS[stack] for forge in ("forgejo", "gitea")) else []) + ([{
            "component": "woodpecker-argocd-handoff",
            "handoff": "scripts/woodpecker_gitops_handoff.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/woodpecker_gitops_handoff.py", "scripts/ci_gitops_handoff.py",
                "examples/woodpecker-argocd-handoff.json",
            )),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "explicit_exact_head_gitops_handoff_implemented": True,
            "live_verified": False,
        }, {
            "component": "woodpecker-signed-artifact-promotion",
            "publisher": "scripts/woodpecker_static_image.py",
            "publisher_image_context": "scripts/prepare_woodpecker_publisher_image.py",
            "promotion": "scripts/woodpecker_artifact_promotion.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/woodpecker_static_image.py", "scripts/woodpecker_artifact_promotion.py", "scripts/prepare_woodpecker_publisher_image.py",
                "examples/woodpecker-static-image.json", "examples/woodpecker-argocd-artifact-promotion.json")),
            "runtime_supported": runtime in RUNTIMES,
            "requires_explicit_kubernetes_target": True,
            "signed_image_admission_verified": False,
            "live_verified": False,
        }] if "woodpecker" in STACKS[stack] else []) + ([{
            "component": "argocd-docker-reconciliation",
            "renderer": "scripts/render_argocd_host.py",
            "artifacts_available": all((root / path).is_file() for path in (
                "scripts/render_argocd_host.py", "examples/argocd-host-docker.json",
            )),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "api_server_packaged": all((root / path).is_file() for path in (
                "scripts/render_argocd_host.py", "examples/argocd-host-docker-ui.json",
            )),
            "applicationset_controller_packaged": all((root / path).is_file() for path in (
                "scripts/render_argocd_host.py", "examples/argocd-host-docker-ui.json",
            )),
            "notifications_controller_packaged": all((root / path).is_file() for path in (
                "scripts/render_argocd_host.py", "examples/argocd-host-docker-ui.json",
            )),
            "live_verified": False,
        }] if runtime == "docker" and "argocd" in STACKS[stack] else []) + [{
            "component": "cilium-gateway-routes",
            "renderer": "scripts/render_cilium_gateway.py",
            "deployment_helper": "scripts/deploy_cilium_gateway.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_cilium_gateway.py", "deploy_cilium_gateway.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "cni_installation_implemented": False,
            "external_entry_verified": False,
            "live_verified": False,
        }, {
            "component": "cilium-cni-lifecycle",
            "renderer": "scripts/render_cilium_installation.py",
            "deployment_helper": "scripts/deploy_cilium_installation.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_cilium_installation.py", "deploy_cilium_installation.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": True,
            "fresh_installation_implemented": True,
            "owned_patch_upgrade_implemented": True,
            "rke2_managed_migration_implemented": False,
            "live_verified": False,
        }, {
            "component": "gateway-external-entry",
            "renderer": "scripts/render_gateway_edge.py",
            "deployment_helper": "scripts/deploy_gateway_edge.py",
            "host_launcher": "scripts/start_gateway_edge.py",
            "host_lifecycle": "scripts/manage_gateway_edge_host.py",
            "pair_coordinator": "scripts/manage_gateway_edge_pair.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_gateway_edge.py", "deploy_gateway_edge.py", "start_gateway_edge.py", "gateway_edge_health.py", "manage_gateway_edge_host.py", "manage_gateway_edge_pair.py", "gateway_edge_pair_agent.py", "gateway_edge_pair_staging.py", "gateway_edge_systemd_job.py")),
            "runtime": runtime,
            "requires_explicit_kubernetes_target": runtime in {"rke2", "k3s", "k8s"},
            "guarded_kubernetes_rollout_implemented": runtime in {"rke2", "k3s", "k8s"},
            "native_host_installation_implemented": runtime == "raw",
            "host_lifecycle_implemented": runtime in {"docker", "raw"},
            "staged_pair_orchestration_implemented": runtime in {"docker", "raw"},
            "pair_input_distribution_implemented": runtime in {"docker", "raw"},
            "pair_terminated_job_recovery_implemented": runtime in {"docker", "raw"},
            "vip_failover_verified": False,
            "application_health_verified": False,
            "live_verified": False,
        }, {
            "component": "keycloak-ha-package",
            "renderer": "scripts/render_keycloak_ha.py",
            "deployment_helper": "scripts/deploy_keycloak_ha.py",
            "host_activation_helper": "scripts/activate_keycloak_host.py",
            "native_activation_runtime_fixture": "scripts/test_keycloak_native_runtime.py",
            "docker_engine_prerequisite_fixture": "scripts/test_keycloak_docker_runtime.py",
            "docker_activation_runtime_fixture": "scripts/test_keycloak_docker_runtime.py",
            "guarded_host_activation_implemented": runtime in {"docker", "raw"},
            "realm_bootstrap_helper": "scripts/bootstrap_keycloak_realm.py",
            "argocd_identity_helper": "scripts/configure_keycloak_argocd.py",
            "argocd_application_helper": "scripts/apply_argocd_identity.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_keycloak_ha.py", "deploy_keycloak_ha.py", "activate_keycloak_host.py", "bootstrap_keycloak_realm.py", "configure_keycloak_argocd.py", "apply_argocd_identity.py")),
            "runtime": runtime,
            "runtime_packaging_implemented": True,
            "new_realm_mfa_definition_implemented": True,
            "guarded_new_realm_bootstrap_implemented": True,
            "guarded_argocd_client_and_private_handoff_implemented": True,
            "guarded_argocd_configuration_and_restart_implemented": True,
            "argocd_runtime_fixture": "scripts/test_argocd_identity_runtime.py",
            "disposable_runtime_fixture": "scripts/test_keycloak_runtime_fixture.py",
            "disposable_runtime_fixture_available": (root / "scripts/test_keycloak_runtime_fixture.py").is_file(),
            "guarded_installation_implemented": runtime in {"rke2", "k3s", "k8s"},
            "application_integration_verified": False,
            "mfa_verified": False,
            "session_failover_verified": False,
            "live_verified": False,
        }, {
            "component": "openbao-raft-package",
            "renderer": "scripts/render_openbao_ha.py",
            "deployment_helper": "scripts/deploy_openbao_ha.py",
            "snapshot_capture_helper": "scripts/openbao_snapshot.py",
            "runtime_verification_helper": "scripts/verify_openbao_runtime.py",
            "host_activation_helper": "scripts/activate_openbao_host.py",
            "guarded_host_activation_implemented": runtime in {"docker", "raw"},
            "runtime_fixture": "scripts/test_openbao_ha.py",
            "installation_api_fixture": "scripts/test_openbao_installation.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_openbao_ha.py", "deploy_openbao_ha.py", "openbao_snapshot.py", "verify_openbao_runtime.py", "activate_openbao_host.py", "test_openbao_host.py", "test_openbao_ha.py", "test_openbao_runtime_fixture.py", "test_openbao_installation.py", "test_openbao_installation_api_fixture.py")),
            "runtime": runtime,
            "runtime_packaging_implemented": True,
            "tls_raft_and_hmac_audit_configuration_implemented": True,
            "scoped_snapshot_capture_implemented": True,
            "offline_snapshot_integrity_check_implemented": True,
            "read_only_runtime_observations_implemented": True,
            "guarded_installation_implemented": runtime in {"rke2", "k3s", "k8s"},
            "production_key_custody_verified": False,
            "restricted_workload_identities_verified": False,
            "off_platform_restore_verified": False,
            "physical_node_loss_verified": False,
            "ha_verified": False,
            "live_verified": False,
        }, {
            "component": "valkey-replication-sentinel-package",
            "renderer": "scripts/render_valkey_ha.py",
            "deployment_helper": "scripts/deploy_valkey_ha.py",
            "installation_api_fixture": "scripts/test_valkey_installation.py",
            "credential_materializer": "scripts/materialize_valkey_auth.py",
            "runtime_fixture": "scripts/test_valkey_ha.py",
            "runtime_verification_helper": "scripts/verify_valkey_runtime.py",
            "runtime_verification_tests": "scripts/test_valkey_runtime.py",
            "host_activation_helper": "scripts/activate_valkey_host.py",
            "host_activation_tests": "scripts/test_valkey_host.py",
            "argocd_cache_client_helper": "scripts/argocd_valkey.py",
            "argocd_cache_client_tests": "scripts/test_argocd_valkey.py",
            "tls_application_gateway_renderer": "scripts/render_valkey_gateway.py",
            "tls_application_gateway_checker": "scripts/valkey_gateway_health.py",
            "tls_application_gateway_tests": "scripts/test_valkey_gateway.py",
            "tls_application_gateway_live_verified": False,
            "argocd_cache_client_preparation_implemented": True,
            "artifacts_available": all((root / "scripts" / n).is_file() for n in ("render_valkey_ha.py", "materialize_valkey_auth.py", "deploy_valkey_ha.py", "test_valkey_installation.py", "test_valkey_installation_api_fixture.py", "test_valkey_ha.py", "test_valkey_runtime_fixture.py", "verify_valkey_runtime.py", "valkey_protocol.py", "test_valkey_runtime.py", "activate_valkey_host.py", "test_valkey_host.py", "test_valkey_host_fixture.py", "test_valkey_docker_host_fixture.py", "argocd_valkey.py", "test_argocd_valkey.py", "test_argocd_valkey_fixture.py")),
            "runtime": runtime,
            "runtime_packaging_implemented": True,
            "tls_replication_and_sentinel_configuration_implemented": True,
            "scoped_private_auth_materialization_implemented": True,
            "preserved_mutable_role_state_implemented": True,
            "guarded_installation_implemented": True,
            "host_activation_implemented": runtime in {"raw", "docker"},
            "runtime_observation_implemented": True,
            "expiring_replication_canary_implemented": True,
            "application_compatibility_verified": False,
            "lossless_failover_verified": False,
            "physical_node_loss_verified": False,
            "ha_verified": False,
            "live_verified": False,
        }, {
            "component": "postgresql-ha-policy",
            "renderer": "scripts/render_postgres_ha.py",
            "credential_materializer": "scripts/materialize_patroni.py",
            "host_startup_helper": "scripts/start_patroni.py",
            "patroni_wheel_builder": "scripts/build_patroni_zookeeper_tls.py" if runtime in {"docker", "raw"} else None,
            "host_application_provisioner": "scripts/provision_patroni_application.py" if runtime in {"docker", "raw"} else None,
            "host_write_endpoint_renderer": "scripts/render_postgres_ha.py" if runtime in {"docker", "raw"} else None,
            "deployment_helper": "scripts/deploy_postgres_ha.py",
            "runtime_verifier": "scripts/verify_postgres_runtime.py",
            "planned_switchover_helper": "scripts/manage_postgres_switchover.py",
            "physical_backup_helper": "scripts/postgres_physical_backup.py",
            "restore_drill_helper": "scripts/restore_postgres_backup.py",
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_postgres_ha.py", "postgres_ha_policy.py", "materialize_patroni.py", "deploy_postgres_ha.py", "verify_postgres_runtime.py", "manage_postgres_switchover.py", "postgres_physical_backup.py", "restore_postgres_backup.py", "postgres_restore_child.py")),
            "runtime": runtime,
            "backend": "cloudnative-pg" if runtime in {"rke2", "k3s", "k8s"} else "patroni-zookeeper",
            "credential_materialization_implemented": runtime in {"docker", "raw"},
            "guarded_host_startup_implemented": runtime in {"docker", "raw"} and all(
                (root / "scripts" / name).is_file() for name in ("start_patroni.py", "build_patroni_zookeeper_tls.py")
            ),
            "guarded_installation_implemented": runtime in {"rke2", "k3s", "k8s"},
            "runtime_observation_implemented": runtime in {"rke2", "k3s", "k8s"},
            "planned_switchover_implemented": runtime in {"rke2", "k3s", "k8s"},
            "physical_backup_capture_implemented": True,
            "database_restore_drill_implemented": True,
            "off_platform_backup_verified": False,
            "operator_bootstrap_renderer": "scripts/render_cnpg_operator.py" if runtime in {"rke2", "k3s", "k8s"} else None,
            "operator_bootstrap_helper": "scripts/deploy_cnpg_operator.py" if runtime in {"rke2", "k3s", "k8s"} else None,
            "operator_installation_implemented": runtime in {"rke2", "k3s", "k8s"} and all((root / path).is_file() for path in (
                "scripts/render_cnpg_operator.py", "scripts/deploy_cnpg_operator.py", "scripts/cnpg_release.py",
                "examples/cnpg-operator.json", "docs/CNPG_OPERATOR_BOOTSTRAP.md",
            )),
            "upgrade_implemented": False,
            "synchronous_replication_verified": False,
            "promotion_verified": False,
            "restore_verified": False,
            "live_verified": False,
        }, {
            "component": "ceph-replicated-storage",
            "renderer": "scripts/render_ceph_storage.py",
            "provisioner": "scripts/provision_ceph_storage.py",
            "provider_renderer": "scripts/render_ceph_provider.py",
            "provider_deployment_helper": "scripts/deploy_ceph_provider.py",
            "device_probe": "scripts/ceph_device_probe.py",
            "operator_bootstrap_renderer": "scripts/render_rook_bootstrap.py",
            "operator_bootstrap_helper": "scripts/deploy_rook_bootstrap.py",
            "operator_bootstrap_implemented": True,
            "separate_operator_namespace_implemented": True,
            "csi_access_topology_implemented": True,
            "kubernetes_client_io_verifier": "scripts/verify_ceph_clients.py",
            "kubernetes_client_io_check_implemented": True,
            "native_client_io_check_implemented": False,
            "native_cephfs_io_check_implemented": True,
            "native_cephfs_checker": "scripts/render_ceph_native_check.py",
            "docker_cephfs_execution_implemented": True,
            "docker_cephfs_execution_helper": "scripts/manage_ceph_native_docker.py",
            "native_cephfs_mount_preparation_implemented": True,
            "native_cephfs_mount_helper": "scripts/mount_cephfs_client.py",
            "host_osd_key_recovery_helper": "scripts/ceph_key_recovery.py",
            "host_osd_key_recovery_available": all((root / path).is_file() for path in (
                "scripts/ceph_key_recovery.py", "scripts/ceph_key_material.py",
                "examples/ceph-key-recovery.json", "docs/CEPH_KEY_RECOVERY.md",
            )),
            "provider_key_capture_verified": False,
            "provider_key_escrow_verified": False,
            "provider_restoration_verified": False,
            "artifacts_available": all((root / "scripts" / name).is_file() for name in ("render_ceph_storage.py", "provision_ceph_storage.py", "render_ceph_provider.py", "deploy_ceph_provider.py", "ceph_device_probe.py", "render_rook_bootstrap.py", "deploy_rook_bootstrap.py", "ceph_client_io.py", "render_ceph_client_check.py", "verify_ceph_clients.py", "ceph_native_io.py", "render_ceph_native_check.py", "manage_ceph_native_docker.py", "mount_cephfs_client.py")) and all((root / "schemas/rook/v1.21.2" / name).is_file() for name in ("storage.json", "cluster.json", "bootstrap.json")),
            "consumer_runtime": runtime,
            "provider": "prepared-rook-kubernetes-cluster",
            "storage_resource_provisioning_implemented": True,
            "guarded_cluster_creation_implemented": True,
            "device_probe_implemented": True,
            "provider_installation_implemented": False,
            "client_mounts_verified": False,
            "data_roundtrip_verified": False,
            "node_loss_verified": False,
            "ha_verified": False,
            "live_verified": False,
        }],
        "gaps": gaps,
        "additional_forge_scope": list(ADDITIONAL_FORGES),
        "note": "Template presence is not proof of integrated runtime or HA support.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--stack", choices=STACKS)
    selection.add_argument("--forge", choices=ADDITIONAL_FORGES, help="Inspect an additional forge")
    parser.add_argument("--runtime", choices=RUNTIMES, default="rke2")
    parser.add_argument("--list", action="store_true", help="List requested combinations")
    parser.add_argument("--require-ready", action="store_true", help="Exit 2 if support is incomplete")
    args = parser.parse_args(argv)
    if args.list:
        if args.require_ready:
            parser.error("--require-ready requires an individual plan, not --list")
        print(json.dumps({"stacks": STACKS, "additional_forges": ADDITIONAL_FORGES, "runtimes": RUNTIMES}, indent=2))
        return 0
    plan = (additional_forge_plan(args.forge, args.runtime) if args.forge else
            stack_plan(args.stack or "forgejo-woodpecker-argocd", args.runtime))
    print(json.dumps(plan, indent=2))
    return 2 if args.require_ready and not plan["deployment_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
