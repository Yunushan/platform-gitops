#!/usr/bin/env python3
"""Validate production-readiness contracts that are easy to drift.

This intentionally avoids third-party dependencies so it can run in GitHub,
Forgejo/Gitea, Woodpecker, and minimal bootstrap environments.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tempfile

from cleanup_firewalld_cni_interfaces import cleanup_zone_file

root = Path(__file__).resolve().parents[1]
SOURCE_PATH_RE = re.compile(
    r"""(?m)^\s+path:\s*(?P<quote>['"]?)(?P<path>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)

profiles_dir = root / "profiles"
premium_apps = root / "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml"
base_apps = root / "gitops/clusters/rke2-main/platform-apps.yaml"
metallb_values = root / "gitops/clusters/rke2-main/apps/metallb/values.yaml"
ingress_nginx_values = root / "gitops/clusters/rke2-main/apps/ingress-nginx/values.yaml"
alternative_gitea_values = root / "gitops/clusters/rke2-main/alternatives/gitea/values.yaml"
alternative_gitlab_ce_values = root / "gitops/clusters/rke2-main/alternatives/gitlab-ce/values.yaml"
alternative_gitlab_runner_values = root / "gitops/clusters/rke2-main/alternatives/gitlab-runner/values.yaml"
alternative_rook_ceph_values = root / "gitops/clusters/rke2-main/alternatives/rook-ceph/values.yaml"
alternative_traefik_values = root / "gitops/clusters/rke2-main/alternatives/traefik/values.yaml"
base_argocd_values = root / "gitops/clusters/rke2-main/apps/argocd-ha/values.yaml"
premium_argocd_values = root / "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/values.yaml"
base_argocd_kustomization = root / "gitops/clusters/rke2-main/apps/argocd-ha/kustomization.yaml"
premium_argocd_kustomization = root / "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/kustomization.yaml"
base_forgejo_values = root / "gitops/clusters/rke2-main/apps/forgejo/values.yaml"
premium_forgejo_values = root / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml"
base_woodpecker_values = root / "gitops/clusters/rke2-main/apps/woodpecker/values.yaml"
premium_woodpecker_values = root / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"
premium_woodpecker_kustomization = root / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/kustomization.yaml"
premium_woodpecker_server_pdb = root / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/server-pdb.yaml"
premium_woodpecker_agent_pdb = root / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/agent-pdb.yaml"
base_cert_manager_values = root / "gitops/clusters/rke2-main/apps/cert-manager/values.yaml"
premium_cert_manager_values = root / "gitops/clusters/rke2-main/premium-3node/apps/cert-manager/values.yaml"
base_traefik_values = root / "gitops/clusters/rke2-main/apps/traefik/values.yaml"
premium_traefik_values = root / "gitops/clusters/rke2-main/premium-3node/apps/traefik/values.yaml"
premium_trust_manager_values = root / "gitops/clusters/rke2-main/premium-3node/apps/trust-manager/values.yaml"
base_harbor_values = root / "gitops/clusters/rke2-main/apps/harbor/values.yaml"
premium_harbor_values = root / "gitops/clusters/rke2-main/premium-3node/apps/harbor/values.yaml"
base_cloudnativepg_values = root / "gitops/clusters/rke2-main/apps/cloudnativepg/values.yaml"
premium_cloudnativepg_values = root / "gitops/clusters/rke2-main/premium-3node/apps/cloudnativepg/values.yaml"
premium_platform_postgres_cluster = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-postgres/postgres-cluster.yaml"
premium_platform_valkey_values = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/values.yaml"
premium_platform_valkey_kustomization = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/kustomization.yaml"
premium_platform_valkey_certificate = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/server-certificate.yaml"
premium_platform_valkey_primary_service = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey/service-primary.yaml"
premium_keycloak_values = root / "gitops/clusters/rke2-main/premium-3node/apps/keycloak/values.yaml"
premium_kyverno_values = root / "gitops/clusters/rke2-main/premium-3node/apps/kyverno/values.yaml"
premium_kyverno_kustomization = root / "gitops/clusters/rke2-main/premium-3node/apps/kyverno/kustomization.yaml"
premium_no_plaintext_policy = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/no-plaintext-secrets.yaml"
premium_pod_security_policy = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-pod-security-baseline.yaml"
premium_workload_baseline_policy = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/require-workload-baseline.yaml"
premium_image_integrity_policy = root / "gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity/verify-platform-images.yaml"
policy_readiness_playbook = root / "ansible/playbooks/verify-platform-policy-readiness.yml"
active_policy_verifier = root / "scripts/verify_active_kyverno_policies.py"
kyverno_cli_installer = root / "scripts/bootstrap/install-kyverno-cli.sh"
github_validate_workflow = root / ".github/workflows/validate.yml"
github_release_workflow = root / ".github/workflows/release.yml"
platform_tls_playbook = root / "ansible/playbooks/manage-platform-tls.yml"
platform_tls_verify_playbook = root / "ansible/playbooks/verify-platform-tls.yml"
production_evidence_script = root / "scripts/verify_production_evidence.py"
production_evidence_runner = root / "scripts/bootstrap/run-platform-production-evidence.sh"
production_evidence_test = root / "scripts/test_production_evidence.py"
atomic_file_writer = root / "scripts/atomic_file.py"
atomic_file_test = root / "scripts/test_atomic_file.py"
image_inventory_capture = root / "scripts/capture_live_image_inventory.py"
image_inventory_reconciler = root / "scripts/reconcile_image_inventory.py"
image_inventory_validator = root / "scripts/verify_image_inventory_evidence.py"
image_inventory_test = root / "scripts/test_image_inventory_evidence.py"
image_inventory_wrapper = root / "scripts/bootstrap/run-platform-image-inventory.sh"
image_inventory_playbook = root / "ansible/playbooks/capture-platform-image-inventory.yml"
image_inventory_example = root / "examples/image-inventory-exceptions.example.json"
forgejo_recovery_runner = root / "scripts/run_forgejo_recovery_drill.py"
forgejo_recovery_validator = root / "scripts/verify_forgejo_recovery_evidence.py"
forgejo_recovery_wrapper = root / "scripts/bootstrap/run-forgejo-recovery-drill.sh"
forgejo_recovery_playbook = root / "ansible/playbooks/run-forgejo-recovery-drill.yml"
forgejo_recovery_example = root / "examples/forgejo-recovery-evidence.example.json"
premium_tetragon_values = root / "gitops/clusters/rke2-main/premium-3node/apps/tetragon/values.yaml"
premium_minio_values = root / "gitops/clusters/rke2-main/premium-3node/apps/minio/values.yaml"
premium_external_secrets_values = root / "gitops/clusters/rke2-main/premium-3node/apps/external-secrets/values.yaml"
premium_openbao_values = root / "gitops/clusters/rke2-main/premium-3node/apps/openbao/values.yaml"
base_longhorn_values = root / "gitops/clusters/rke2-main/apps/longhorn/values.yaml"
premium_longhorn_values = root / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/values.yaml"
premium_longhorn_storageclasses = root / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/storageclasses.yaml"
premium_longhorn_priorityclasses = root / "gitops/clusters/rke2-main/premium-3node/apps/longhorn/priorityclasses.yaml"
base_monitoring_values = root / "gitops/clusters/rke2-main/apps/monitoring/values.yaml"
premium_monitoring_values = root / "gitops/clusters/rke2-main/premium-3node/apps/monitoring/values.yaml"
base_loki_values = root / "gitops/clusters/rke2-main/apps/loki/values.yaml"
premium_loki_values = root / "gitops/clusters/rke2-main/premium-3node/apps/loki/values.yaml"
base_velero_values = root / "gitops/clusters/rke2-main/apps/velero/values.yaml"
premium_velero_values = root / "gitops/clusters/rke2-main/premium-3node/apps/velero/values.yaml"
base_velero_kustomization = root / "gitops/clusters/rke2-main/apps/velero/kustomization.yaml"
premium_velero_kustomization = root / "gitops/clusters/rke2-main/premium-3node/apps/velero/kustomization.yaml"
base_step_ca_values = root / "gitops/clusters/rke2-main/apps/step-ca/values.yaml"
premium_step_ca_values = root / "gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml"
stale_premium_root_app = root / "gitops/bootstrap/root-app-premium-3node.yaml"
health_playbook = root / "ansible/playbooks/verify-platform-app-health.yml"
service_path_consumers_playbook = root / "ansible/playbooks/repair-platform-service-path-consumers.yml"
woodpecker_repair_playbook = root / "ansible/playbooks/repair-woodpecker.yml"
woodpecker_service_path_nodes_playbook = root / "ansible/playbooks/repair-woodpecker-service-path-nodes.yml"
cilium_vxlan_overlay_repair_playbook = root / "ansible/playbooks/repair-cilium-vxlan-overlay.yml"
longhorn_runtime_repair_playbook = root / "ansible/playbooks/repair-longhorn-runtime.yml"
empty_faulted_longhorn_claim_repair = root / "scripts/repair_empty_faulted_longhorn_claims.py"
stuck_longhorn_attachment_repair = root / "scripts/repair_stuck_longhorn_attachments.py"
longhorn_bootstrap_playbook = root / "ansible/playbooks/bootstrap-longhorn.yml"
longhorn_bootstrap_runner = root / "scripts/bootstrap/run-longhorn-bootstrap.sh"
platform_app_health_runner = root / "scripts/bootstrap/run-platform-app-health.sh"
dns_repair_playbook = root / "ansible/playbooks/repair-cluster-dns.yml"
firewalld_cleanup_script = root / "scripts/cleanup_firewalld_cni_interfaces.py"
verify_rke2_playbook = root / "ansible/playbooks/verify-rke2.yml"
status_playbook = root / "ansible/playbooks/platform-status.yml"
profile_check_script = root / "scripts/check_gitops_profile.py"
validation_runner = root / "scripts/run_validation.py"
python_syntax_test = root / "scripts/test_python_syntax.py"
validation_runner_test = root / "scripts/test_validation_runner.py"
line_endings_test = root / "scripts/test_line_endings.py"
profile_check_test = root / "scripts/test_profile_checker.py"
deployable_renderer = root / "scripts/render_deployable_gitops_apps.py"
deployable_renderer_test = root / "scripts/test_deployable_renderer.py"
gitops_selection_helper_test = root / "scripts/test_gitops_selection_helper.py"
private_values_renderer = root / "scripts/render_private_platform_values.py"
private_values_renderer_test = root / "scripts/test_private_values_renderer.py"
synthetic_private_profile_helper = root / "scripts/synthetic_private_profile.py"
platform_secret_contract_test = root / "scripts/test_platform_secret_contract.py"
policy_examples_test = root / "scripts/test_policy_examples.py"
sops_age_policy_test = root / "scripts/test_sops_age_policy.py"
supply_chain_helpers_test = root / "scripts/test_supply_chain_helpers.py"
supply_chain_evidence_test = root / "scripts/test_supply_chain_evidence.py"
supply_chain_evidence_validator = root / "scripts/verify_supply_chain_evidence.py"
security_scan_script = root / "scripts/security-scan.sh"
supply_chain_posture_script = root / "scripts/supply-chain-posture.sh"
gitleaks_config = root / ".gitleaks.toml"
semgrep_config = root / ".semgrep.yml"
trivy_config = root / "trivy.yaml"
backup_restore_runbook_test = root / "scripts/test_backup_restore_runbook.py"
business_continuity_test = root / "scripts/test_business_continuity.py"
service_catalog_test = root / "scripts/test_service_catalog.py"
architecture_decisions_test = root / "scripts/test_architecture_decisions.py"
operations_runbook_test = root / "scripts/test_operations_runbook.py"
production_readiness_checklist_test = root / "scripts/test_production_readiness_checklist.py"
platform_support_test = root / "scripts/test_platform_support.py"
incident_response_runbook_test = root / "scripts/test_incident_response_runbook.py"
access_control_runbook_test = root / "scripts/test_access_control_runbook.py"
capacity_planning_runbook_test = root / "scripts/test_capacity_planning_runbook.py"
compliance_audit_runbook_test = root / "scripts/test_compliance_audit_runbook.py"
release_promotion_runbook_test = root / "scripts/test_release_promotion_runbook.py"
alerting_runbook_test = root / "scripts/test_alerting_runbook.py"
data_classification_test = root / "scripts/test_data_classification.py"
security_policy_test = root / "scripts/test_security_policy.py"
threat_model_test = root / "scripts/test_threat_model.py"
repository_governance_test = root / "scripts/test_repository_governance.py"
codeowners_starter_test = root / "scripts/test_codeowners_starter.py"
no_secrets_test = root / "scripts/test_no_secrets.py"
no_secrets_script = root / "scripts/validate_no_secrets.py"
private_artifact_boundary_test = root / "scripts/test_private_artifact_boundary.py"
ci_reference_pinning_test = root / "scripts/test_ci_reference_pinning.py"
shell_syntax_test = root / "scripts/test_shell_syntax.py"
shell_strict_mode_test = root / "scripts/test_shell_strict_mode.py"
ansible_shell_blocks_test = root / "scripts/test_ansible_shell_blocks.py"
ansible_curl_timeout_contract_test = root / "scripts/test_ansible_curl_timeout_contract.py"
ansible_until_contract_test = root / "scripts/test_ansible_until_contract.py"
ansible_failed_when_contract_test = root / "scripts/test_ansible_failed_when_contract.py"
ansible_no_log_contract_test = root / "scripts/test_ansible_no_log_contract.py"
docs_make_targets_test = root / "scripts/test_docs_make_targets.py"
markdown_links_test = root / "scripts/test_markdown_links.py"
example_templates_test = root / "scripts/test_example_templates.py"
ansible_playbook_references_test = root / "scripts/test_ansible_playbook_references.py"
gitops_application_contract_test = root / "scripts/test_gitops_application_contract.py"
kustomization_references_test = root / "scripts/test_kustomization_references.py"
gitops_helm_chart_pinning_test = root / "scripts/test_gitops_helm_chart_pinning.py"
gitops_image_pinning_test = root / "scripts/test_gitops_image_pinning.py"
makefile_help_test = root / "scripts/test_makefile_help.py"
validation_surface_parity_test = root / "scripts/test_validation_surface_parity.py"
app_secrets_playbook = root / "ansible/playbooks/configure-platform-app-secrets.yml"
makefile = root / "Makefile"
installation_doc = root / "docs/INSTALLATION.md"
premium_doc = root / "docs/PREMIUM_3NODE.md"
troubleshooting_doc = root / "docs/TROUBLESHOOTING.md"
backup_restore_doc = root / "docs/BACKUP_RESTORE.md"
business_continuity_doc = root / "docs/BUSINESS_CONTINUITY.md"
service_catalog_doc = root / "docs/SERVICE_CATALOG.md"
architecture_decisions_doc = root / "docs/ARCHITECTURE_DECISIONS.md"
architecture_decision_template = root / "docs/adr/0000-template.md"
operations_doc = root / "docs/OPERATIONS.md"
production_readiness_doc = root / "docs/PRODUCTION_READINESS.md"
platform_support_doc = root / "docs/PLATFORM_SUPPORT.md"
node_os_support_doc = root / "docs/NODE_OS_SUPPORT.md"
incident_response_doc = root / "docs/INCIDENT_RESPONSE.md"
access_control_doc = root / "docs/ACCESS_CONTROL.md"
capacity_planning_doc = root / "docs/CAPACITY_PLANNING.md"
compliance_audit_doc = root / "docs/COMPLIANCE_AUDIT.md"
release_promotion_doc = root / "docs/RELEASE_PROMOTION.md"
alerting_doc = root / "docs/ALERTING.md"
data_classification_doc = root / "docs/DATA_CLASSIFICATION.md"
security_policy_doc = root / "SECURITY.md"
readme_doc = root / "README.md"
quick_start_doc = root / "docs/QUICK_START.md"
release_guide_doc = root / "docs/RELEASE_GUIDE.md"
bootstrap_plan_script = root / "scripts/bootstrap-plan.sh"
gitignore_file = root / ".gitignore"
first_deploy_env_example = root / "config/first-deploy.env.example"
seed_git_env_example = root / "config/seed-git.env.example"
ci_validation_files = [
    root / ".github/workflows/validate.yml",
    root / ".gitea/workflows/validate.yml",
    root / ".forgejo/workflows/validate.yml",
    root / ".gitlab-ci.yml",
    root / ".woodpecker/validate.yml",
]

required_base_apps = [
    "cert-manager",
    "trust-manager",
    "step-ca",
    "metallb",
    "traefik",
    "longhorn",
    "cloudnativepg",
    "argocd-ha",
    "forgejo",
    "woodpecker",
    "harbor",
    "monitoring",
    "loki",
    "velero",
]

required_premium_apps = [
    "cert-manager",
    "trust-manager",
    "step-ca",
    "kyverno",
    "platform-policies",
    "platform-image-integrity",
    "tetragon",
    "external-secrets",
    "openbao",
    "metallb",
    "traefik",
    "longhorn",
    "cloudnativepg",
    "platform-postgres",
    "platform-valkey",
    "keycloak",
    "argocd-ha",
    "forgejo",
    "woodpecker",
    "harbor",
    "monitoring",
    "loki",
    "velero",
]

required_gui_hosts = [
    "argocd",
    "forgejo",
    "harbor",
    "woodpecker",
    "keycloak",
    "grafana",
    "prometheus",
]

required_storage_classes = [
    "longhorn-standard",
    "longhorn-critical",
    "longhorn-cache",
    "longhorn-standard-encrypted",
    "longhorn-critical-encrypted",
    "longhorn-cache-encrypted",
]


def fail(message: str) -> None:
    print(f"Platform contract validation failed: {message}")
    sys.exit(1)


def configured_longhorn_storage_over_provisioning_percentage() -> int:
    raw_value = os.environ.get(
        "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE",
        "100",
    ).strip() or "100"
    if not raw_value.isdigit() or not 100 <= int(raw_value) <= 1000:
        fail(
            "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE must be "
            "an integer from 100 through 1000"
        )
    return int(raw_value)


def yaml_integer_scalar(text: str, key: str, label: str) -> int:
    matches = re.findall(
        rf"(?m)^\s*{re.escape(key)}:\s*['\"]?(\d+)['\"]?\s*(?:#.*)?$",
        text,
    )
    if len(matches) != 1:
        fail(f"{label} must define exactly one integer {key}")
    return int(matches[0])


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"missing required file {path.relative_to(root)}")


def assert_firewalld_cleanup_behavior() -> None:
    zone_xml = """<zone target="ACCEPT">
  <short>Trusted</short>
  <interface name="cilium_host"/>
  <interface name="cilium_geneve"/>
  <interface name="cilium_wg0"/>
  <interface name="cni0"/>
  <interface name="lxc0123456789ab"/>
  <interface name="vethdeadbeef"/>
  <interface name="cni-deadbeef"/>
  <interface name="ciliumdeadbeef"/>
  <source address="192.0.2.0/24"/>
  <port protocol="tcp" port="10250"/>
</zone>
"""
    with tempfile.TemporaryDirectory(prefix="platform-firewalld-cleanup-") as temporary_dir:
        zone_path = Path(temporary_dir) / "trusted.xml"
        zone_path.write_text(zone_xml, encoding="utf-8")
        result = cleanup_zone_file(zone_path)
        if not result.changed or result.removed != 4:
            fail("firewalld CNI cleanup must remove every transient interface binding")
        cleaned = zone_path.read_text(encoding="utf-8")
        for stable in ("cilium_host", "cilium_geneve", "cilium_wg0", "cni0"):
            if f'name="{stable}"' not in cleaned:
                fail(f"firewalld CNI cleanup removed stable interface {stable}")
        for transient in ("lxc0123456789ab", "vethdeadbeef", "cni-deadbeef", "ciliumdeadbeef"):
            if transient in cleaned:
                fail(f"firewalld CNI cleanup retained transient interface {transient}")
        for retained in ("192.0.2.0/24", 'protocol="tcp" port="10250"'):
            if retained not in cleaned:
                fail(f"firewalld CNI cleanup removed unrelated zone configuration {retained}")
        second_result = cleanup_zone_file(zone_path)
        if second_result.changed or second_result.removed != 0:
            fail("firewalld CNI cleanup must be idempotent")


def application_documents(path: Path) -> list[dict[str, str]]:
    text = read(path)
    docs: list[dict[str, str]] = []
    for raw_doc in re.split(r"(?m)^---\s*$", text):
        if "kind: Application" not in raw_doc:
            continue
        metadata_name = re.search(
            r"(?m)^  name:\s*([A-Za-z0-9_.-]+)\s*$",
            raw_doc,
        )
        source_path = SOURCE_PATH_RE.search(raw_doc)
        destination_namespace = re.search(
            r"(?m)^    namespace:\s*([A-Za-z0-9_.-]+)\s*$",
            raw_doc,
        )
        repo_url = re.search(r"(?m)^\s+repoURL:\s*([^\s#]+)\s*$", raw_doc)
        docs.append(
            {
                "name": metadata_name.group(1) if metadata_name else "",
                "path": source_path.group("path") if source_path else "",
                "namespace": destination_namespace.group(1) if destination_namespace else "",
                "repoURL": repo_url.group(1) if repo_url else "",
            }
        )
    return docs


def assert_app_file(path: Path, required_apps: list[str]) -> None:
    docs = application_documents(path)
    if not docs:
        fail(f"{path.relative_to(root)} does not define any Argo CD Applications")

    names = [doc["name"] for doc in docs]
    missing_names = sorted(set(required_apps) - set(names))
    if missing_names:
        fail(f"{path.relative_to(root)} is missing apps: {', '.join(missing_names)}")
    unexpected_names = sorted(set(names) - set(required_apps))
    if unexpected_names:
        fail(
            f"{path.relative_to(root)} has apps not covered by the production health contract: "
            f"{', '.join(unexpected_names)}"
        )

    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        fail(f"{path.relative_to(root)} has duplicate apps: {', '.join(duplicate_names)}")

    for doc in docs:
        app = doc["name"] or "<unknown>"
        if not doc["name"]:
            fail(f"{path.relative_to(root)} has an Application without metadata.name")
        if not doc["path"]:
            fail(f"{path.relative_to(root)} app {app} is missing source.path")
        if not doc["namespace"]:
            fail(f"{path.relative_to(root)} app {app} is missing destination.namespace")
        if doc["repoURL"] != "<THIS_REPO_URL>":
            fail(f"{path.relative_to(root)} app {app} must keep repoURL as <THIS_REPO_URL>")
        source_path = root / doc["path"]
        if not source_path.exists():
            fail(f"{path.relative_to(root)} app {app} source.path does not exist: {doc['path']}")


def extract_default_word_list(var_name: str, text: str) -> list[str]:
    pattern = rf"{re.escape(var_name)}:.*?default\('([^']+)'"
    match = re.search(pattern, text)
    if not match:
        fail(f"could not find default list for {var_name}")
    return match.group(1).split()


def require_text(text: str, needle: str, description: str) -> None:
    if needle in text or needle in canonical_contract_text(text):
        return
    if "<PLATFORM_DOMAIN>" in needle and "rendered by scripts/render_private_platform_values.py" in text:
        placeholder_lines = [line for line in needle.splitlines() if "<PLATFORM_DOMAIN>" in line]
        if placeholder_lines and all(rendered_placeholder_line_present(text, line) for line in placeholder_lines):
            return
    if rendered_optional_postgres_contract(text, needle):
        return
    if rendered_optional_redis_contract(text, needle):
        return
    if rendered_optional_woodpecker_database_contract(text, needle):
        return
    if rendered_optional_sqlite_woodpecker_contract(text, needle):
        return
    if rendered_optional_forgejo_database_contract(text, needle):
        return
    if rendered_optional_forgejo_redis_contract(text, needle):
        return
    if rendered_optional_harbor_dependency_contract(text, needle):
        return
    if rendered_optional_grafana_database_contract(text, needle):
        return
    if rendered_optional_loki_object_storage_contract(text, needle):
        return
    if rendered_optional_velero_object_storage_contract(text, needle):
        return
    if rendered_optional_cnpg_backup_contract(text, needle):
        return
    if rendered_optional_step_ca_contract(text, needle):
        return
    if rendered_optional_private_scalar(text, needle):
        return
    if needle not in text:
        fail(description)


def reject_text(text: str, needle: str, description: str) -> None:
    if needle in text or needle in canonical_contract_text(text):
        fail(description)


def canonical_contract_text(text: str) -> str:
    """Normalize equivalent YAML scalar quoting for string-contract checks."""
    normalized = re.sub(r'(:\s*)"([^"\n]*)"', r"\1\2", text)
    normalized = re.sub(r'(^\s*-\s*)"([^"\n]*)"', r"\1\2", normalized, flags=re.MULTILINE)
    return normalized


def count_yaml_list_scalar(text: str, value: str) -> int:
    """Count exact YAML list scalars with optional single or double quotes."""
    pattern = re.compile(
        rf"""(?m)^\s*-\s*(?P<quote>['"]?){re.escape(value)}(?P=quote)\s*(?:#.*)?$"""
    )
    return sum(1 for _ in pattern.finditer(text))


def rendered_placeholder_line_present(text: str, line: str) -> bool:
    stripped = line.strip()
    if ":" not in stripped:
        return False
    key = stripped.split(":", 1)[0]
    return re.search(rf"(?m)^\s*{re.escape(key)}:\s*\"?[^\"\n<>]+\"?\s*$", text) is not None


def is_private_rendered(text: str) -> bool:
    return "rendered by scripts/render_private_platform_values.py" in text


def rendered_optional_postgres_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text):
        return False
    if "WOODPECKER_DATABASE_DRIVER" in needle and "WOODPECKER_DATABASE_DRIVER" not in text:
        return True
    if "- woodpecker-database" in needle and "WOODPECKER_DATABASE_DRIVER" not in text:
        return True
    return False


def rendered_optional_redis_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text):
        return False
    if needle in {"GITEA__cache__HOST", "GITEA__queue__CONN_STR", "ADAPTER: redis", "TYPE: redis"}:
        return "GITEA__cache__HOST" not in text
    return False


def rendered_optional_woodpecker_database_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Woodpecker" not in text:
        return False
    if needle == "replicaCount: 3" and "WOODPECKER_DATABASE_DRIVER" not in text:
        return True
    if needle == 'WOODPECKER_DATABASE_DRIVER: "postgres"' and "WOODPECKER_DATABASE_DRIVER" not in text:
        return True
    if needle == "- woodpecker-database" and "WOODPECKER_DATABASE_DRIVER" not in text:
        return True
    return False


def rendered_optional_sqlite_woodpecker_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Woodpecker" not in text:
        return False
    return needle == "replicaCount: 1" and "WOODPECKER_DATABASE_DRIVER" in text


def rendered_optional_forgejo_database_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Forgejo" not in text:
        return False
    sqlite_mode = "DB_TYPE: sqlite3" in canonical_contract_text(text)
    if sqlite_mode and needle in {
        "additionalConfigFromEnvs:",
        "GITEA__database__PASSWD",
        "name: forgejo-database",
        "DB_TYPE: postgres",
        "HOST: platform-postgres-rw.platform-databases.svc.cluster.local:5432",
        "NAME: forgejo",
        "USER: forgejo",
        "SSL_MODE: verify-full",
        "PROVIDER: db",
    }:
        return True
    if not sqlite_mode and needle in {
        "DB_TYPE: sqlite3",
        "PROVIDER: file",
        "ADAPTER: memory",
        "TYPE: level",
    }:
        return True
    return False


def rendered_optional_forgejo_redis_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Forgejo" not in text:
        return False
    redis_mode = "GITEA__cache__HOST" in text
    if not redis_mode and needle in {
        "GITEA__cache__HOST",
        "GITEA__queue__CONN_STR",
        "name: forgejo-redis",
        "ADAPTER: redis",
        "TYPE: redis",
    }:
        return True
    if redis_mode and needle in {"ADAPTER: memory", "TYPE: level"}:
        return True
    return False


def rendered_optional_harbor_dependency_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Harbor" not in text:
        return False
    normalized = canonical_contract_text(text)
    if needle.startswith("database:\n  type: internal") and "database:\n  type: external" in normalized:
        return True
    if needle.startswith("redis:\n  type: external") and "redis:\n  type: internal" in normalized:
        return True
    if needle.startswith("redis:\n  type: internal") and "redis:\n  type: external" in normalized:
        return True
    if "imageChartStorage:" in needle and "type: s3" in needle and "type: filesystem" in normalized:
        return True
    if "imageChartStorage:" in needle and "type: filesystem" in needle and "type: s3" in normalized:
        return True
    return False


def rendered_optional_grafana_database_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Grafana" not in text:
        return False
    if "grafana.ini:\n    database:" in needle and "grafana.ini:\n    database:" not in text:
        return True
    if "GF_DATABASE_PASSWORD" in needle and "GF_DATABASE_PASSWORD" not in text:
        return True
    return False


def rendered_optional_loki_object_storage_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Loki" not in text:
        return False
    if any(token in needle for token in ("LOKI_S3_ACCESS_KEY_ID", "LOKI_S3_SECRET_ACCESS_KEY")):
        return "LOKI_S3_ACCESS_KEY_ID" not in text
    return False


def rendered_optional_velero_object_storage_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "Velero" not in text:
        return False
    if "existingSecret: velero-credentials" in needle:
        return "existingSecret:" not in text
    return False


def rendered_optional_cnpg_backup_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text):
        return False
    if any(token in needle for token in ("destinationPath:", "endpointURL:", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")):
        return "barmanObjectStore:" not in text
    return False


def rendered_optional_step_ca_contract(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "step-ca" not in text:
        return False
    return "STEP_CA_" in needle


def rendered_optional_private_scalar(text: str, needle: str) -> bool:
    if not is_private_rendered(text) or "\n" in needle or ":" not in needle:
        return False
    key, expected = needle.split(":", 1)
    if "<" not in expected and "." not in expected and "://" not in expected:
        return False
    return re.search(rf"(?m)^\s*{re.escape(key.strip())}:\s*\"?[^\"\n<>]+\"?\s*$", text) is not None


def require_ansible_task_block(text: str, task_name: str, label: str) -> str:
    pattern = re.compile(
        rf"(?ms)^    - name:\s*{re.escape(task_name)}\s*\n"
        rf"(?P<block>.*?)(?=^    - name:\s|\Z)"
    )
    match = pattern.search(text)
    if not match:
        fail(f"{label} is missing task: {task_name}")
    return match.group("block")


def require_unique_words(values: list[str], label: str) -> None:
    duplicate_values = sorted({value for value in values if values.count(value) > 1})
    if duplicate_values:
        fail(f"{label} contains duplicate entries: {', '.join(duplicate_values)}")


def parse_profile_file(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    current_list = ""

    for raw_line in read(path).splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        scalar_match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", stripped)
        if scalar_match:
            key, value = scalar_match.groups()
            current_list = ""
            if value:
                scalars[key] = value
            else:
                lists.setdefault(key, [])
                current_list = key
            continue

        list_match = re.match(r"^-\s+(.+?)\s*$", stripped)
        if list_match and current_list:
            lists[current_list].append(list_match.group(1))

    return scalars, lists


def assert_profile_catalog() -> None:
    for profile_file in sorted(profiles_dir.glob("*.yaml")):
        profile_text = read(profile_file)
        if re.search(r"(?m)^include:", profile_text):
            fail(f"{profile_file.relative_to(root)} must use includes:, not include:")

        scalars, lists = parse_profile_file(profile_file)
        profile_name = scalars.get("profile", "")
        expected_name = profile_file.stem
        if profile_name != expected_name:
            fail(f"{profile_file.relative_to(root)} profile name must match file stem {expected_name!r}")

        inherited = scalars.get("inherits", "")
        if inherited and not (profiles_dir / f"{inherited}.yaml").exists():
            fail(f"{profile_file.relative_to(root)} inherits missing profile {inherited!r}")

        for key in ("includes", "remove"):
            entries = lists.get(key, [])
            duplicate_entries = sorted({entry for entry in entries if entries.count(entry) > 1})
            if duplicate_entries:
                fail(f"{profile_file.relative_to(root)} has duplicate {key} entries: {', '.join(duplicate_entries)}")
            for entry in entries:
                if not (root / entry).exists():
                    fail(f"{profile_file.relative_to(root)} {key} references missing path: {entry}")

        if not lists.get("includes"):
            fail(f"{profile_file.relative_to(root)} must declare at least one includes entry")

    required_profile_includes = {
        "default-forgejo-woodpecker-argocd": [
            "gitops/clusters/rke2-main/apps/cert-manager",
            "gitops/clusters/rke2-main/apps/trust-manager",
            "gitops/clusters/rke2-main/apps/step-ca",
            "gitops/clusters/rke2-main/apps/metallb",
            "gitops/clusters/rke2-main/apps/traefik",
            "gitops/clusters/rke2-main/apps/longhorn",
            "gitops/clusters/rke2-main/apps/cloudnativepg",
            "gitops/clusters/rke2-main/apps/argocd-ha",
            "gitops/clusters/rke2-main/apps/forgejo",
            "gitops/clusters/rke2-main/apps/woodpecker",
            "gitops/clusters/rke2-main/apps/harbor",
            "gitops/clusters/rke2-main/apps/monitoring",
            "gitops/clusters/rke2-main/apps/loki",
            "gitops/clusters/rke2-main/apps/velero",
        ],
        "gitea-woodpecker-argocd": [
            "gitops/clusters/rke2-main/apps/cert-manager",
            "gitops/clusters/rke2-main/apps/trust-manager",
            "gitops/clusters/rke2-main/apps/step-ca",
            "gitops/clusters/rke2-main/apps/metallb",
            "gitops/clusters/rke2-main/apps/traefik",
            "gitops/clusters/rke2-main/apps/longhorn",
            "gitops/clusters/rke2-main/apps/cloudnativepg",
            "gitops/clusters/rke2-main/apps/argocd-ha",
            "gitops/clusters/rke2-main/alternatives/gitea",
            "gitops/clusters/rke2-main/apps/woodpecker",
            "gitops/clusters/rke2-main/apps/harbor",
            "gitops/clusters/rke2-main/apps/monitoring",
            "gitops/clusters/rke2-main/apps/loki",
            "gitops/clusters/rke2-main/apps/velero",
        ],
        "gitlab-ce-runner-argocd": [
            "gitops/clusters/rke2-main/apps/cert-manager",
            "gitops/clusters/rke2-main/apps/trust-manager",
            "gitops/clusters/rke2-main/apps/step-ca",
            "gitops/clusters/rke2-main/apps/metallb",
            "gitops/clusters/rke2-main/apps/traefik",
            "gitops/clusters/rke2-main/apps/longhorn",
            "gitops/clusters/rke2-main/apps/cloudnativepg",
            "gitops/clusters/rke2-main/apps/argocd-ha",
            "gitops/clusters/rke2-main/alternatives/gitlab-ce",
            "gitops/clusters/rke2-main/alternatives/gitlab-runner",
            "gitops/clusters/rke2-main/apps/harbor",
            "gitops/clusters/rke2-main/apps/monitoring",
            "gitops/clusters/rke2-main/apps/loki",
            "gitops/clusters/rke2-main/apps/velero",
        ],
        "premium-3node": [
            "gitops/clusters/rke2-main/premium-3node",
            "gitops/clusters/rke2-main/premium-3node/apps/cert-manager",
            "gitops/clusters/rke2-main/premium-3node/apps/trust-manager",
            "gitops/clusters/rke2-main/premium-3node/apps/step-ca",
            "gitops/clusters/rke2-main/premium-3node/apps/kyverno",
            "gitops/clusters/rke2-main/premium-3node/apps/platform-policies",
            "gitops/clusters/rke2-main/premium-3node/apps/platform-image-integrity",
            "gitops/clusters/rke2-main/premium-3node/apps/tetragon",
            "gitops/clusters/rke2-main/premium-3node/apps/external-secrets",
            "gitops/clusters/rke2-main/premium-3node/apps/openbao",
            "gitops/clusters/rke2-main/apps/metallb",
            "gitops/clusters/rke2-main/premium-3node/apps/traefik",
            "gitops/clusters/rke2-main/premium-3node/apps/longhorn",
            "gitops/clusters/rke2-main/premium-3node/apps/cloudnativepg",
            "gitops/clusters/rke2-main/premium-3node/apps/platform-postgres",
            "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey",
            "gitops/clusters/rke2-main/premium-3node/apps/keycloak",
            "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha",
            "gitops/clusters/rke2-main/premium-3node/apps/forgejo",
            "gitops/clusters/rke2-main/premium-3node/apps/woodpecker",
            "gitops/clusters/rke2-main/premium-3node/apps/harbor",
            "gitops/clusters/rke2-main/premium-3node/apps/monitoring",
            "gitops/clusters/rke2-main/premium-3node/apps/loki",
            "gitops/clusters/rke2-main/premium-3node/apps/velero",
        ],
        "premium-3node-lab": [
            "gitops/clusters/rke2-main/premium-3node/apps/minio",
        ],
    }
    required_profile_removals = {
        "gitea-woodpecker-argocd": ["gitops/clusters/rke2-main/apps/forgejo"],
        "gitlab-ce-runner-argocd": [
            "gitops/clusters/rke2-main/apps/forgejo",
            "gitops/clusters/rke2-main/apps/woodpecker",
        ],
        "ingress-nginx": ["gitops/clusters/rke2-main/apps/traefik"],
        "ingress-traefik": ["gitops/clusters/rke2-main/apps/ingress-nginx"],
        "storage-rook-ceph": ["gitops/clusters/rke2-main/apps/longhorn"],
    }

    for profile_name, required_entries in required_profile_includes.items():
        _, lists = parse_profile_file(profiles_dir / f"{profile_name}.yaml")
        missing_entries = sorted(set(required_entries) - set(lists.get("includes", [])))
        if missing_entries:
            fail(f"profiles/{profile_name}.yaml is missing includes entries: {', '.join(missing_entries)}")

    for profile_name, required_entries in required_profile_removals.items():
        _, lists = parse_profile_file(profiles_dir / f"{profile_name}.yaml")
        missing_entries = sorted(set(required_entries) - set(lists.get("remove", [])))
        if missing_entries:
            fail(f"profiles/{profile_name}.yaml is missing remove entries: {', '.join(missing_entries)}")


def require_top_level_block(text: str, key: str, label: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(key)}:\n(?P<block>.*?)(?=^[A-Za-z0-9_-]+:|\Z)", text)
    if not match:
        fail(f"{label} is missing top-level {key}: block")
    return match.group("block")


def require_woodpecker_role_image_pin(text: str, role: str, repository: str, tag: str, label: str) -> None:
    role_block = require_top_level_block(text, role, label)
    image_match = re.search(r"(?ms)^  image:\n(?P<block>(?:    .*(?:\n|$))*)", role_block)
    if not image_match:
        fail(f"{label} must define {role}.image")
    image_block = image_match.group("block")
    for needle in (
        "    registry: docker.io",
        f"    repository: {repository}",
        f'    tag: "{tag}"',
    ):
        if needle not in image_block:
            fail(f"{label} must pin {role}.image with {needle.strip()}")


def application_names(*paths: Path) -> list[str]:
    names: set[str] = set()
    for path in paths:
        for doc in application_documents(path):
            if doc["name"]:
                names.add(doc["name"])
    return sorted(names)


def required_destination_namespaces(*paths: Path) -> list[str]:
    namespaces: set[str] = set()
    for path in paths:
        for doc in application_documents(path):
            if doc["namespace"]:
                namespaces.add(doc["namespace"])
    return sorted(namespaces)


def main() -> None:
    if stale_premium_root_app.exists():
        fail(f"stale premium root app file still exists: {stale_premium_root_app.relative_to(root)}")

    assert_profile_catalog()
    assert_firewalld_cleanup_behavior()

    assert_app_file(base_apps, required_base_apps)
    assert_app_file(premium_apps, required_premium_apps)

    premium_apps_text = read(premium_apps)
    reject_text(
        premium_apps_text,
        "path: gitops/clusters/rke2-main/premium-3node/apps/minio",
        "premium production application set must not register archived MinIO; use premium-3node-lab only for non-production testing",
    )
    if "- /spec/imageName" in premium_apps_text:
        fail(
            "premium Argo CD applications must not ignore /spec/imageName; "
            "container image upgrades must remain Git-managed"
        )

    metallb_text = read(metallb_values)
    for needle in (
        "controller:\n  logLevel: info\n  resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi\n    limits:\n      memory: 256Mi",
        "speaker:\n  logLevel: info\n  resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi\n    limits:\n      memory: 256Mi",
    ):
        require_text(metallb_text, needle, f"MetalLB profile must include {needle.splitlines()[0]}")

    ingress_nginx_text = read(ingress_nginx_values)
    for needle in (
        "controller:\n  replicaCount: 3\n  minAvailable: 2",
        "updateStrategy:\n    rollingUpdate:\n      maxUnavailable: 1",
        "resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi\n    limits:\n      memory: 512Mi",
        "externalTrafficPolicy: Local",
        "ssl-redirect: \"true\"",
        "force-ssl-redirect: \"true\"",
        "use-forwarded-headers: \"true\"",
        "metrics:\n    enabled: true\n    serviceMonitor:\n      enabled: false",
    ):
        require_text(ingress_nginx_text, needle, f"ingress-nginx profile must include {needle.splitlines()[0]}")

    alternative_traefik_text = read(alternative_traefik_values)
    for needle in (
        "deployment:\n  replicas: 3",
        "podDisruptionBudget:\n  enabled: true\n  minAvailable: 2",
        "externalTrafficPolicy: Local",
        "internalTrafficPolicy: Local",
        "serviceMonitor:\n      enabled: false",
        "resources:\n  requests:\n    cpu: 100m\n    memory: 128Mi\n  limits:\n    memory: 512Mi",
    ):
        require_text(alternative_traefik_text, needle, f"alternative Traefik profile must include {needle.splitlines()[0]}")

    alternative_gitea_text = read(alternative_gitea_values)
    for needle in (
        "replicaCount: 1",
        "strategy:\n  type: Recreate",
        "image:\n  rootless: true",
        "className: traefik",
        "gitea.<PLATFORM_DOMAIN>",
        "postgresql:\n  enabled: false",
        "redis-cluster:\n  enabled: false",
        "persistence:\n  enabled: true\n  size: <GITEA_DATA_SIZE>\n  storageClass: <GITEA_STORAGE_CLASS>",
        "DISABLE_REGISTRATION: true",
        "DB_TYPE: sqlite3",
        "resources:\n  requests:\n    cpu: 250m\n    memory: 512Mi\n  limits:\n    memory: 2Gi",
    ):
        require_text(alternative_gitea_text, needle, f"alternative Gitea profile must include {needle.splitlines()[0]}")

    alternative_gitlab_ce_text = read(alternative_gitlab_ce_values)
    for needle in (
        "edition: ce",
        "gitlab.<PLATFORM_DOMAIN>",
        "registry-gitlab.<PLATFORM_DOMAIN>",
        "nginx-ingress:\n  enabled: false",
        "prometheus:\n  install: false",
        "gitlab-runner:\n  install: false",
        "webservice:\n    minReplicas: 1\n    maxReplicas: 2\n    resources:",
        "sidekiq:\n    minReplicas: 1\n    maxReplicas: 2\n    resources:",
        "gitlab-shell:\n    resources:",
        "storageClass: <GITLAB_STORAGE_CLASS>",
        "size: <GITLAB_GITALY_SIZE>",
        "registry:\n  enabled: true",
        "postgresql:\n  install: true",
        "size: <GITLAB_POSTGRES_SIZE>",
        "redis:\n  install: true",
        "size: <GITLAB_REDIS_SIZE>",
    ):
        require_text(alternative_gitlab_ce_text, needle, f"alternative GitLab CE profile must include {needle.splitlines()[0]}")

    alternative_gitlab_runner_text = read(alternative_gitlab_runner_values)
    for needle in (
        "gitlabUrl: https://gitlab.<PLATFORM_DOMAIN>",
        "rbac:\n  create: true",
        "concurrent: 10",
        "runners:\n  locked: false\n  config: |",
        "privileged = false",
        "memory_limit = \"1Gi\"",
        "resources:\n  requests:\n    cpu: 100m\n    memory: 128Mi\n  limits:\n    memory: 512Mi",
        "metrics:\n  enabled: true\n  serviceMonitor:\n    enabled: false",
    ):
        require_text(alternative_gitlab_runner_text, needle, f"alternative GitLab Runner profile must include {needle.splitlines()[0]}")

    alternative_rook_ceph_text = read(alternative_rook_ceph_values)
    for needle in (
        "crds:\n  enabled: true",
        "logLevel: INFO",
        "resources:\n  requests:\n    cpu: 250m\n    memory: 512Mi\n  limits:\n    memory: 1Gi",
        "enableRbdDriver: true",
        "enableCephfsDriver: true",
        "provisionerReplicas: 2",
        "pluginPriorityClassName: system-node-critical",
        "provisionerPriorityClassName: system-cluster-critical",
        "monitoring:\n  enabled: true\n  createPrometheusRules: true",
    ):
        require_text(alternative_rook_ceph_text, needle, f"alternative Rook/Ceph profile must include {needle.splitlines()[0]}")

    for argocd_values, label, repo_replicas in (
        (base_argocd_values, "base Argo CD HA profile", 2),
        (premium_argocd_values, "premium Argo CD HA profile", 3),
    ):
        argocd_text = read(argocd_values)
        for needle in (
            "server:\n  replicas: 3",
            "  resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
            f"repoServer:\n  replicas: {repo_replicas}\n  deploymentStrategy:\n    type: RollingUpdate\n    rollingUpdate:\n      maxSurge: 0\n      maxUnavailable: 1",
            "  readinessProbe:\n    enabled: true\n    initialDelaySeconds: 10\n    periodSeconds: 10\n    timeoutSeconds: 10\n    failureThreshold: 18",
            "  livenessProbe:\n    enabled: true\n    initialDelaySeconds: 30\n    periodSeconds: 10\n    timeoutSeconds: 10\n    failureThreshold: 18",
            "  startupProbe:\n    enabled: true\n    initialDelaySeconds: 10\n    periodSeconds: 10\n    timeoutSeconds: 10\n    failureThreshold: 60",
            "      cpu: 500m\n      memory: 512Mi\n    limits:\n      memory: 2Gi",
            "applicationSet:\n  replicas: 2\n  resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi\n    limits:\n      memory: 512Mi",
            "redis-ha:\n  enabled: true\n  haproxy:\n    deploymentStrategy:\n      type: RollingUpdate\n      rollingUpdate:\n        maxSurge: 0\n        maxUnavailable: 1\n    resources:\n      requests:\n        cpu: 50m\n        memory: 64Mi\n      limits:\n        memory: 128Mi",
            "  redis:\n    resources:\n      requests:\n        cpu: 100m\n        memory: 128Mi\n      limits:\n        memory: 512Mi",
            "    sentinel:\n      resources:\n        requests:\n          cpu: 50m\n          memory: 64Mi\n        limits:\n          memory: 128Mi",
            "dex:\n  resources:\n    requests:\n      cpu: 50m\n      memory: 64Mi\n    limits:\n      memory: 256Mi",
            "notifications:\n  resources:\n    requests:\n      cpu: 50m\n      memory: 64Mi\n    limits:\n      memory: 256Mi",
            "controller:\n  env:\n    - name: KUBERNETES_SERVICE_HOST\n      value: kubernetes.default.svc\n  readinessProbe:\n    initialDelaySeconds: 10\n    periodSeconds: 10\n    timeoutSeconds: 10\n    failureThreshold: 18",
            "  resources:\n    requests:\n      cpu: 500m\n      memory: 1Gi\n    limits:\n      memory: 2Gi",
            '    controller.status.processors: "10"',
            '    controller.operation.processors: "5"',
            '    controller.kubectl.parallelism.limit: "5"',
            '    controller.repo.server.timeout.seconds: "120"',
            '    reposerver.parallelism.limit: "2"',
            "    timeout.reconciliation.jitter: 60s",
        ):
            require_text(argocd_text, needle, f"{label} must include {needle.splitlines()[0]}")

        if not re.search(
            r'^\s+admin\.enabled:\s*["\']?(?:true|false)["\']?\s*$',
            argocd_text,
            flags=re.MULTILINE,
        ):
            fail(f"{label} must explicitly configure configs.cm admin.enabled")
        if re.search(
            r'^\s+admin\.enabled:\s*["\']?false["\']?\s*$',
            argocd_text,
            flags=re.MULTILINE,
        ) and not re.search(
            r"^\s+(?:oidc|dex)\.config:\s*\S+",
            argocd_text,
            flags=re.MULTILINE,
        ):
            fail(f"{label} must not disable admin login without an OIDC or Dex login provider")

    for kustomization, label in (
        (base_argocd_kustomization, "base Argo CD HA profile"),
        (premium_argocd_kustomization, "premium Argo CD HA profile"),
    ):
        kustomization_text = read(kustomization)
        for needle in (
            "kind: Deployment\n      name: argo-cd-argocd-repo-server",
            "kind: StatefulSet\n      name: argo-cd-argocd-application-controller",
            "/usr/bin/timeout",
            "/usr/bin/bash",
            "/dev/tcp/127.0.0.1/8084",
            "/dev/tcp/127.0.0.1/8082",
            "GET /healthz HTTP/1.0",
            "GET /healthz?full=true HTTP/1.0",
            "/usr/bin/head -n 1 <&3 | /usr/bin/grep -q ' 200 '",
        ):
            require_text(
                kustomization_text,
                needle,
                f"{label} local health probes must include {needle}",
            )
        for probe_path, minimum in (
            ("path: /spec/template/spec/containers/0/readinessProbe", 2),
            ("path: /spec/template/spec/containers/0/livenessProbe", 1),
            ("path: /spec/template/spec/containers/0/startupProbe", 2),
        ):
            if kustomization_text.count(probe_path) < minimum:
                fail(
                    f"{label} must patch at least {minimum} {probe_path.rsplit('/', 1)[-1]} entries"
                )
        if "kubelet-health-policy.yaml" in kustomization_text:
            fail(f"{label} must not reference the obsolete kubelet health policy")

    for stale_policy in (
        root / "gitops/clusters/rke2-main/apps/argocd-ha/kubelet-health-policy.yaml",
        root / "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha/kubelet-health-policy.yaml",
    ):
        if stale_policy.exists():
            fail(f"obsolete Argo CD kubelet health policy must not exist: {stale_policy.relative_to(root)}")

    premium_argocd_kustomization_text = read(premium_argocd_kustomization)
    for needle in (
        "name: argo-cd-argocd-repo-server",
        "name: argo-cd-redis-ha-haproxy",
        'path: /spec/clusterIP\n        value: "None"',
        "path: /spec/publishNotReadyAddresses\n        value: false",
    ):
        require_text(
            premium_argocd_kustomization_text,
            needle,
            f"premium Argo CD HA internal service bypass must include {needle.splitlines()[0]}",
        )

    base_forgejo_text = read(base_forgejo_values)
    premium_forgejo_text = read(premium_forgejo_values)
    for needle in (
        "replicaCount: 1",
        "strategy:\n  type: Recreate",
        "podDisruptionBudget:\n  minAvailable: 1",
        "image:\n  rootless: true",
        "ingress:\n  enabled: true\n  className: traefik",
        "host: forgejo.<PLATFORM_DOMAIN>",
        "postgresql:\n  enabled: false",
        "redis-cluster:\n  enabled: false",
        "persistence:\n  enabled: true\n  size: <FORGEJO_DATA_SIZE>",
        "resources:\n  requests:\n    cpu: 250m\n    memory: 512Mi\n  limits:\n    memory: 2Gi",
    ):
        require_text(base_forgejo_text, needle, f"default Forgejo profile must include {needle.splitlines()[0]}")

    for needle in (
        "replicaCount: 1",
        "strategy:\n  type: Recreate",
        "podDisruptionBudget:\n  minAvailable: 1",
        "image:\n  rootless: true",
        "ingress:\n  enabled: true\n  className: traefik",
        "secretName: forgejo-tls",
        "persistence:\n  enabled: true\n  size: 20Gi\n  storageClass: longhorn-critical-encrypted",
        "DOMAIN: forgejo.<PLATFORM_DOMAIN>",
        "ROOT_URL: https://forgejo.<PLATFORM_DOMAIN>/",
        "SSH_DOMAIN: forgejo.<PLATFORM_DOMAIN>",
        "START_SSH_SERVER: true",
        "DISABLE_REGISTRATION: true",
        "REQUIRE_SIGNIN_VIEW: true",
        "DEFAULT_BRANCH: main",
        "additionalConfigFromEnvs:",
        "GITEA__database__PASSWD",
        "name: forgejo-database",
        "DB_TYPE: postgres",
        "HOST: platform-postgres-rw.platform-databases.svc.cluster.local:5432",
        "NAME: forgejo",
        "USER: forgejo",
        "SSL_MODE: verify-full",
        "name: platform-postgres-ca",
        "name: platform-internal-roots",
        "mountPath: /data/gitea/git/.postgresql",
        "name: SSL_CERT_FILE",
        "value: /etc/ssl/platform/ca-certificates.crt",
        "mountPath: /etc/ssl/platform",
        "PROVIDER: db",
        "GITEA__cache__HOST",
        "name: forgejo-redis",
        "ADAPTER: redis",
        "TYPE: redis",
        "resources:\n  requests:\n    cpu: 250m\n    memory: 512Mi\n  limits:\n    memory: 2Gi",
    ):
        require_text(premium_forgejo_text, needle, f"premium Forgejo profile must include {needle.splitlines()[0]}")

    base_woodpecker_text = read(base_woodpecker_values)
    premium_woodpecker_text = read(premium_woodpecker_values)
    for needle in (
        "WOODPECKER_OPEN: \"false\"",
        "WOODPECKER_FORGEJO: \"true\"",
        "WOODPECKER_FORGEJO_URL: https://forgejo.<PLATFORM_DOMAIN>",
        'WOODPECKER_SERVER_ADDR: ":8000"',
        'WOODPECKER_GRPC_ADDR: ":9000"',
        'WOODPECKER_LOG_LEVEL: "info"',
        "probes:\n    liveness:\n      timeoutSeconds: 10\n      periodSeconds: 10\n      successThreshold: 1\n      failureThreshold: 30",
        "ingressClassName: traefik",
        "traefik.ingress.kubernetes.io/router.entrypoints: websecure",
        "traefik.ingress.kubernetes.io/router.tls: \"true\"",
        "host: woodpecker.<PLATFORM_DOMAIN>",
        "persistentVolume:\n    enabled: true\n    size: <WOODPECKER_DATA_SIZE>",
        "  resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "WOODPECKER_BACKEND: kubernetes",
        "WOODPECKER_BACKEND_K8S_NAMESPACE: woodpecker",
        "  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
    ):
        require_text(base_woodpecker_text, needle, f"default Woodpecker profile must include {needle.splitlines()[0]}")
    require_text(
        base_woodpecker_text,
        "replicaCount: 1",
        "default Woodpecker profile must use one server replica without a shared PostgreSQL datasource",
    )
    reject_text(
        base_woodpecker_text,
        'WOODPECKER_DATABASE_DRIVER: "postgres"',
        "default Woodpecker profile must not declare PostgreSQL without generated DB secret plumbing",
    )
    reject_text(
        base_woodpecker_text,
        "- woodpecker-database",
        "default Woodpecker profile must not require the production database secret",
    )
    require_woodpecker_role_image_pin(
        base_woodpecker_text,
        "server",
        "woodpeckerci/woodpecker-server",
        "v3.16.0",
        "default Woodpecker profile",
    )
    require_woodpecker_role_image_pin(
        base_woodpecker_text,
        "agent",
        "woodpeckerci/woodpecker-agent",
        "v3.16.0",
        "default Woodpecker profile",
    )
    require_text(
        premium_woodpecker_text,
        "replicaCount: 3",
        "premium Woodpecker profile must keep HA server replicas",
    )
    require_text(
        premium_woodpecker_text,
        'WOODPECKER_DATABASE_DRIVER: "postgres"',
        "premium Woodpecker profile must use PostgreSQL-backed state for HA server replicas",
    )
    require_text(
        premium_woodpecker_text,
        "- woodpecker-database",
        "premium Woodpecker profile must consume the generated database datasource secret",
    )
    for needle in (
        "WOODPECKER_ADMIN: \"admin\"",
        "WOODPECKER_OPEN: \"false\"",
        "WOODPECKER_FORGEJO: \"true\"",
        "WOODPECKER_FORGEJO_URL: https://forgejo.<PLATFORM_DOMAIN>",
        'WOODPECKER_SERVER_ADDR: ":8000"',
        'WOODPECKER_GRPC_ADDR: ":9000"',
        'WOODPECKER_LOG_LEVEL: "info"',
        "probes:\n    liveness:\n      timeoutSeconds: 10\n      periodSeconds: 10\n      successThreshold: 1\n      failureThreshold: 30",
        "- woodpecker-agent-secret",
        "- woodpecker-forgejo-oauth",
        "createAgentSecret: false",
        "mapAgentSecret: false",
        "app.kubernetes.io/name: server\n              app.kubernetes.io/instance: woodpecker\n          topologyKey: kubernetes.io/hostname",
        "app.kubernetes.io/name: agent\n              app.kubernetes.io/instance: woodpecker\n          topologyKey: kubernetes.io/hostname",
        "topologySpreadConstraints:\n    - maxSkew: 1\n      topologyKey: kubernetes.io/hostname\n      whenUnsatisfiable: DoNotSchedule",
        "ingressClassName: traefik",
        "traefik.ingress.kubernetes.io/router.entrypoints: websecure",
        "traefik.ingress.kubernetes.io/router.tls: \"true\"",
        "secretName: woodpecker-tls",
        "persistentVolume:\n    enabled: true\n    size: 10Gi\n    storageClass: longhorn-standard-encrypted",
        "  resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "WOODPECKER_BACKEND: kubernetes",
        "WOODPECKER_BACKEND_K8S_NAMESPACE: woodpecker",
        "WOODPECKER_BACKEND_K8S_STORAGE_CLASS: longhorn-standard-encrypted",
        "WOODPECKER_BACKEND_K8S_VOLUME_SIZE: 10G",
        "WOODPECKER_BACKEND_K8S_STORAGE_RWX: \"false\"",
        "WOODPECKER_MAX_WORKFLOWS: \"2\"",
        "persistence:\n    enabled: false",
        "  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
    ):
        require_text(premium_woodpecker_text, needle, f"premium Woodpecker profile must include {needle.splitlines()[0]}")
    if count_yaml_list_scalar(premium_woodpecker_text, "woodpecker-agent-secret") != 2:
        fail("premium Woodpecker profile must map the same managed agent secret into server and agent")
    premium_woodpecker_kustomization_text = read(premium_woodpecker_kustomization)
    for resource_name, resource_path, workload_name in (
        ("server-pdb.yaml", premium_woodpecker_server_pdb, "server"),
        ("agent-pdb.yaml", premium_woodpecker_agent_pdb, "agent"),
    ):
        require_text(
            premium_woodpecker_kustomization_text,
            f"- {resource_name}",
            f"premium Woodpecker kustomization must include {resource_name}",
        )
        pdb_text = read(resource_path)
        for needle in (
            "kind: PodDisruptionBudget",
            "maxUnavailable: 1",
            "unhealthyPodEvictionPolicy: AlwaysAllow",
            f"app.kubernetes.io/name: {workload_name}",
            "app.kubernetes.io/instance: woodpecker",
        ):
            require_text(
                pdb_text,
                needle,
                f"premium Woodpecker {workload_name} PDB must include {needle}",
            )
    require_woodpecker_role_image_pin(
        premium_woodpecker_text,
        "server",
        "woodpeckerci/woodpecker-server",
        "v3.16.0",
        "premium Woodpecker profile",
    )
    require_woodpecker_role_image_pin(
        premium_woodpecker_text,
        "agent",
        "woodpeckerci/woodpecker-agent",
        "v3.16.0",
        "premium Woodpecker profile",
    )
    for cert_manager_values, label, replicas in (
        (base_cert_manager_values, "base cert-manager profile", 1),
        (premium_cert_manager_values, "premium cert-manager profile", 2),
    ):
        cert_manager_text = read(cert_manager_values)
        for needle in (
            f"webhook:\n  replicaCount: {replicas}\n  timeoutSeconds: 10\n  resources:\n    requests:\n      cpu: 50m\n      memory: 128Mi\n    limits:\n      memory: 256Mi",
            f"cainjector:\n  replicaCount: {replicas}\n  resources:\n    requests:\n      cpu: 50m\n      memory: 128Mi\n    limits:\n      memory: 256Mi",
            "startupapicheck:\n  resources:\n    requests:\n      cpu: 25m\n      memory: 64Mi\n    limits:\n      memory: 128Mi",
        ):
            require_text(cert_manager_text, needle, f"{label} must include {needle.splitlines()[0]}")
    for traefik_values, label in (
        (base_traefik_values, "base Traefik profile"),
        (premium_traefik_values, "premium Traefik profile"),
    ):
        traefik_text = read(traefik_values)
        for needle in (
            "deployment:\n  replicas: 3",
            "podDisruptionBudget:\n  enabled: true\n  minAvailable: 2",
            "updateStrategy:\n  type: RollingUpdate\n  rollingUpdate:\n    maxUnavailable: 1\n    maxSurge: 0",
            "affinity:\n  podAntiAffinity:\n    requiredDuringSchedulingIgnoredDuringExecution:",
            "topologySpreadConstraints:\n  - maxSkew: 1\n    topologyKey: kubernetes.io/hostname\n    whenUnsatisfiable: DoNotSchedule",
            "ingressClass:\n  enabled: true\n  isDefaultClass: true\n  name: traefik",
            "externalTrafficPolicy: Local",
            "internalTrafficPolicy: Local",
            "--entrypoints.web.http.redirections.entrypoint.permanent=true",
            "--entrypoints.websecure.http.tls=true",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 128Mi\n  limits:\n    memory: 512Mi",
        ):
            require_text(traefik_text, needle, f"{label} must include {needle.splitlines()[0]}")
    premium_trust_manager_text = read(premium_trust_manager_values)
    for needle in (
        "replicaCount: 2",
        "podDisruptionBudget:\n  enabled: true\n  minAvailable: 1",
        "defaultPackage:\n  enabled: true",
        "secretTargets:\n  enabled: false",
        "webhook:\n    timeoutSeconds: 10",
        "resources:\n  requests:\n    cpu: 50m\n    memory: 128Mi\n  limits:\n    memory: 512Mi",
    ):
        require_text(
            premium_trust_manager_text,
            needle,
            f"premium trust-manager profile must include {needle.splitlines()[0]}",
        )
    base_longhorn_text = read(base_longhorn_values)
    for needle in (
        "persistence:\n  defaultClass: true\n  defaultClassReplicaCount: 2\n  reclaimPolicy: Retain",
        "defaultSettings:\n  defaultDataPath: /var/lib/longhorn",
        "defaultReplicaCount: 2",
        "defaultDataLocality: best-effort",
        "replicaAutoBalance: best-effort",
        "storageOverProvisioningPercentage: 100",
        "storageMinimalAvailablePercentage: 10",
        "orphanAutoDeletion: true",
        "concurrentAutomaticEngineUpgradePerNodeLimit: 1",
        "longhornManager:\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi\n    limits:\n      memory: 1Gi",
    ):
        require_text(base_longhorn_text, needle, f"base Longhorn profile must include {needle.splitlines()[0]}")

    premium_longhorn_text = read(premium_longhorn_values)
    for needle in (
        "persistence:\n  defaultClass: false",
        "backupTarget: \"<LONGHORN_BACKUP_TARGET>\"",
        "backupTargetCredentialSecret: <LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME>",
        "createDefaultDiskLabeledNodes: false",
        "defaultReplicaCount: 2",
        "defaultDataLocality: best-effort",
        "replicaAutoBalance: best-effort",
        "storageMinimalAvailablePercentage: 10",
        "orphanAutoDeletion: true",
        "concurrentAutomaticEngineUpgradePerNodeLimit: 1",
        "longhornManager:\n  priorityClass: longhorn-critical\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi\n    limits:\n      memory: 1Gi",
        "longhornDriver:\n  priorityClass: longhorn-critical",
    ):
        require_text(
            premium_longhorn_text,
            needle,
            f"premium Longhorn profile must include {needle.splitlines()[0]}",
        )
    configured_over_provisioning = configured_longhorn_storage_over_provisioning_percentage()
    rendered_over_provisioning = yaml_integer_scalar(
        premium_longhorn_text,
        "storageOverProvisioningPercentage",
        "premium Longhorn profile",
    )
    if rendered_over_provisioning != configured_over_provisioning:
        fail(
            "premium Longhorn profile storageOverProvisioningPercentage must "
            "match PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE "
            f"({configured_over_provisioning}), got {rendered_over_provisioning}"
        )
    premium_longhorn_storageclasses_text = read(premium_longhorn_storageclasses)
    for needle in (
        'name: longhorn-standard\n  annotations:\n    storageclass.kubernetes.io/is-default-class: "false"',
        "name: longhorn-standard-encrypted",
        'storageclass.kubernetes.io/is-default-class: "true"',
        "provisioner: driver.longhorn.io",
        "allowVolumeExpansion: true",
        "reclaimPolicy: Retain",
        "volumeBindingMode: WaitForFirstConsumer",
        'numberOfReplicas: "2"',
        "name: longhorn-critical",
        "name: longhorn-critical-encrypted",
        'numberOfReplicas: "3"',
        "name: longhorn-cache",
        "name: longhorn-cache-encrypted",
        "reclaimPolicy: Delete",
        'numberOfReplicas: "1"',
        "dataLocality: best-effort",
        'encrypted: "true"',
        "csi.storage.k8s.io/provisioner-secret-name: longhorn-crypto",
        "csi.storage.k8s.io/node-publish-secret-name: longhorn-crypto",
        "csi.storage.k8s.io/node-stage-secret-name: longhorn-crypto",
        "csi.storage.k8s.io/node-expand-secret-name: longhorn-crypto",
        "csi.storage.k8s.io/node-expand-secret-namespace: longhorn-system",
    ):
        require_text(
            premium_longhorn_storageclasses_text,
            needle,
            f"premium Longhorn storage classes must include {needle.splitlines()[0]}",
        )
    premium_longhorn_priorityclasses_text = read(premium_longhorn_priorityclasses)
    for needle in (
        "kind: PriorityClass",
        "name: longhorn-critical",
        "value: 1000000",
        "globalDefault: false",
    ):
        require_text(
            premium_longhorn_priorityclasses_text,
            needle,
            f"premium Longhorn priority classes must include {needle.splitlines()[0]}",
        )
    for cloudnativepg_values, label, replicas, rollout_values in (
        (
            base_cloudnativepg_values,
            "base CloudNativePG operator profile",
            1,
            "maxSurge: 1\n    maxUnavailable: 0",
        ),
        (
            premium_cloudnativepg_values,
            "premium CloudNativePG operator profile",
            2,
            "maxSurge: 0\n    maxUnavailable: 1",
        ),
    ):
        cloudnativepg_text = read(cloudnativepg_values)
        for needle in (
            f"replicaCount: {replicas}",
            "image:\n  tag: \"1.30.0\"",
            f"updateStrategy:\n  type: RollingUpdate\n  rollingUpdate:\n    {rollout_values}",
            "crds:\n  create: false",
            "monitoring:\n  podMonitorEnabled: false",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n  limits:\n    memory: 512Mi",
        ):
            require_text(
                cloudnativepg_text,
                needle,
                f"{label} must include {needle.splitlines()[0]}",
            )

    premium_cloudnativepg_text = read(premium_cloudnativepg_values)
    for needle in (
        "topologySpreadConstraints:\n  - maxSkew: 1\n    topologyKey: kubernetes.io/hostname\n    whenUnsatisfiable: DoNotSchedule",
        "app.kubernetes.io/name: cloudnative-pg\n        app.kubernetes.io/instance: cloudnative-pg",
        "affinity:\n  podAntiAffinity:\n    requiredDuringSchedulingIgnoredDuringExecution:",
        "topologyKey: kubernetes.io/hostname",
        "webhook:\n  mutating:\n    failurePolicy: Ignore\n  validating:\n    failurePolicy: Ignore",
    ):
        require_text(
            premium_cloudnativepg_text,
            needle,
            f"premium CloudNativePG operator profile must include {needle.splitlines()[0]}",
        )

    premium_platform_postgres_text = read(premium_platform_postgres_cluster)
    for needle in (
        "kind: Cluster",
        "name: platform-postgres",
        "namespace: platform-databases",
        "instances: 3",
        "database: forgejo",
        "owner: forgejo",
        "name: forgejo-database",
        "managed:\n    roles:",
        "name: keycloak",
        "login: true",
        "name: keycloak-database",
        "name: woodpecker",
        "name: woodpecker-database",
        "storageClass: longhorn-critical-encrypted",
        "enablePodMonitor: true",
    ):
        require_text(
            premium_platform_postgres_text,
            needle,
            f"premium platform PostgreSQL cluster must include {needle.splitlines()[0]}",
        )

    premium_platform_valkey_kustomization_text = read(premium_platform_valkey_kustomization)
    require_text(
        premium_platform_valkey_kustomization_text,
        "- server-certificate.yaml",
        "premium platform Valkey profile must include its managed TLS Certificate",
    )
    premium_platform_valkey_certificate_text = read(premium_platform_valkey_certificate)
    for needle in (
        "name: platform-valkey-server",
        "secretName: platform-valkey-tls",
        "rotationPolicy: Always",
        "platform-valkey-primary.platform-cache.svc.cluster.local",
        '"*.platform-valkey-headless.platform-cache.svc.cluster.local"',
        "name: platform-internal-ca",
    ):
        require_text(
            premium_platform_valkey_certificate_text,
            needle,
            f"premium platform Valkey Certificate must include {needle}",
        )

    premium_platform_valkey_text = read(premium_platform_valkey_values)
    for needle in (
        "fullnameOverride: platform-valkey",
        'tag: "9.1.0"',
        "usersExistingSecret: platform-valkey-auth",
        "passwordKey: valkey-password",
        "tls:\n  enabled: true",
        "existingSecret: platform-valkey-tls",
        "requireClientCertificate: false",
        "tls-auto-reload-interval 300",
        "replicas: 2",
        "podDisruptionBudget:\n  enabled: true",
        "minAvailable: 2",
        "whenUnsatisfiable: DoNotSchedule",
        "storageClass: longhorn-critical-encrypted",
        "size: 8Gi",
        "name: configure-ha",
        'password="$(cat /auth/valkey-password)"',
        "sentinel monitor platform-valkey",
        "sentinel down-after-milliseconds platform-valkey 5000",
        "tls-port 26379",
        "tls-replication yes",
        "name: sentinel",
        "name: primary-proxy",
        "image: haproxy:3.4.2-alpine",
        "tcp-check expect string role:master",
        "check-ssl",
        "verify required",
        "ca-file /trust/ca-certificates.crt",
        "REDIS_ADDR: rediss://localhost:6379",
        'REDIS_EXPORTER_SKIP_TLS_VERIFICATION: "false"',
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(
            premium_platform_valkey_text,
            needle,
            f"premium platform Valkey profile must include {needle.splitlines()[0]}",
        )

    premium_platform_valkey_primary_service_text = read(premium_platform_valkey_primary_service)
    for needle in (
        "name: platform-valkey-primary",
        "app.kubernetes.io/component: primary-proxy",
        "targetPort: primary-proxy",
    ):
        require_text(
            premium_platform_valkey_primary_service_text,
            needle,
            f"premium platform Valkey primary Service must include {needle}",
        )
    reject_text(
        premium_platform_valkey_primary_service_text,
        "statefulset.kubernetes.io/pod-name",
        "premium platform Valkey primary Service must not pin traffic to pod 0",
    )

    premium_kyverno_text = read(premium_kyverno_values)
    for needle in (
        "crds:\n  install: true",
        "migration:\n    enabled: false",
        "admissionController:\n  replicas: 3",
        "backgroundController:\n  replicas: 2",
        "cleanupController:\n  replicas: 2",
        "reportsController:\n  replicas: 2",
        "serviceMonitor:\n    enabled: true",
        "release: monitoring",
    ):
        require_text(
            premium_kyverno_text,
            needle,
            f"premium Kyverno profile must include {needle.splitlines()[0]}",
        )
    kyverno_secret_reader = "resources:\n            - secrets\n          verbs:\n            - get"
    if premium_kyverno_text.count(kyverno_secret_reader) != 2:
        fail(
            "premium Kyverno admission and background controllers must each have "
            "exactly get-only Secret access for Pod imagePullSecrets"
        )
    premium_kyverno_kustomization_text = read(premium_kyverno_kustomization)
    require_text(
        premium_kyverno_kustomization_text,
        "includeCRDs: true",
        "premium Kyverno Kustomization must render chart CRDs",
    )
    for needle in (
        'name: ".*policies\\\\.kyverno\\\\.io"',
        "- op: remove\n        path: /metadata/labels",
    ):
        require_text(
            premium_kyverno_kustomization_text,
            needle,
            "premium Kyverno Kustomization must remove normalized empty labels from policy CRDs",
        )

    policy_default_contracts = (
        (premium_no_plaintext_policy, 1),
        (premium_pod_security_policy, 2),
        (premium_workload_baseline_policy, 1),
    )
    for policy_path, expected_validation_count in policy_default_contracts:
        policy_text = read(policy_path)
        for needle in (
            "apiVersion: policies.kyverno.io/v1",
            "kind: ValidatingPolicy",
            "admission:\n      enabled: true",
            "background:\n      enabled: true",
            "failurePolicy: Fail",
            "validationActions:\n    - Audit",
            "matchConstraints:",
            "validations:",
        ):
            require_text(
                policy_text,
                needle,
                f"{policy_path.relative_to(root)} must use the stable Kyverno CEL admission contract",
            )
        if policy_text.count("- expression:") != expected_validation_count:
            fail(
                f"{policy_path.relative_to(root)} must define exactly "
                f"{expected_validation_count} CEL validation(s)"
            )
    platform_policy_kustomization_text = read(
        root / "gitops/clusters/rke2-main/premium-3node/apps/platform-policies/kustomization.yaml"
    )
    require_text(
        platform_policy_kustomization_text,
        "require-pod-security-baseline.yaml",
        "platform policy Kustomization must include the stable pod security policy",
    )
    if "namespace:" in platform_policy_kustomization_text:
        fail("cluster-scoped platform policy Kustomization must not inject metadata.namespace")

    premium_image_integrity_text = read(premium_image_integrity_policy)
    for needle in (
        "apiVersion: policies.kyverno.io/v1",
        "kind: ImageValidatingPolicy",
        "name: verify-platform-image-signatures",
        "validationActions:\n    - Audit",
        "failurePolicy: Fail",
        "image.registry == '<PLATFORM_IMAGE_REGISTRY>'",
        "<PLATFORM_COSIGN_PUBLIC_KEY>",
        "<PLATFORM_COSIGN_REKOR_URL>",
        "insecureIgnoreTlog: false",
        "mutateDigest: true",
        "required: true",
        "verifyDigest: true",
        "verifyImageSignatures(image, [attestors.platformCosign])",
    ):
        require_text(
            premium_image_integrity_text,
            needle,
            f"premium image-integrity policy must retain {needle}",
        )
    for forbidden in ("kind: ClusterPolicy", "verifyImages:", "PRIVATE KEY"):
        reject_text(
            premium_image_integrity_text,
            forbidden,
            f"premium image-integrity policy must not retain {forbidden}",
        )

    premium_tetragon_text = read(premium_tetragon_values)
    for needle in (
        "priorityClassName: system-node-critical",
        "hostNetwork: true",
        "dnsPolicy: Default",
        "repository: quay.io/cilium/tetragon",
        "tag: v1.6.0",
        "resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "exportFilePerm: \"600\"",
        "exportRateLimit: 5000",
        "redactionFilters: |-",
        "serviceMonitor:\n      enabled: true",
        "release: monitoring",
        "enablePolicyFilter: true",
        "enableProcessCred: true",
        "enableProcessNs: true",
        "tetragonOperator:\n  enabled: true\n  replicas: 2",
        "repository: quay.io/cilium/tetragon-operator",
        "failoverLease:\n    enabled: true",
        "installMethod: helm",
        "rthooks:\n  enabled: false",
    ):
        require_text(
            premium_tetragon_text,
            needle,
            f"premium Tetragon profile must include {needle.splitlines()[0]}",
        )

    premium_minio_text = read(premium_minio_values)
    for needle in (
        "mode: distributed",
        "repository: bitnamilegacy/os-shell",
        "tag: 12-debian-12-r50",
        "existingSecret: minio-root",
        "rootUserSecretKey: root-user",
        "rootPasswordSecretKey: root-password",
        "replicaCount: 4",
        "storageClass: longhorn-critical-encrypted",
        "prometheusAuthType: public",
        "serviceMonitor:\n    enabled: true",
        "name: platform-velero-backups",
    ):
        require_text(
            premium_minio_text,
            needle,
            f"premium MinIO profile must include {needle.splitlines()[0]}",
        )

    premium_keycloak_text = read(premium_keycloak_values)
    for needle in (
        "security:\n    allowInsecureImages: true",
        "registry: quay.io",
        "repository: keycloak/keycloak",
        'tag: "26.7.0"',
        "command:\n  - /opt/keycloak/bin/kc.sh",
        "args:\n  - start",
        "automountServiceAccountToken: false",
        "runAsUser: 1000",
        "runAsGroup: 0",
        "readOnlyRootFilesystem: false",
        "defaultInitContainers:\n  prepareWriteDirs:\n    enabled: false",
        "name: KC_DB\n    value: postgres",
        "startupProbe:\n  enabled: true",
        "existingSecret: keycloak-admin",
        "passwordSecretKey: admin-password",
        "production: true",
        "proxyHeaders: xforwarded",
        "hostnameStrict: true",
        "replicaCount: 2",
        "podAntiAffinityPreset: hard",
        "pdb:\n  create: true\n  minAvailable: 1",
        "postgresql:\n  enabled: false",
        "host: platform-postgres-rw.platform-databases.svc.cluster.local",
        "user: keycloak",
        "database: keycloak",
        "existingSecret: keycloak-database",
        "hostname: sso.<PLATFORM_DOMAIN>",
        "ingressClassName: traefik",
        "secretName: keycloak-tls",
        "networkPolicy:\n  enabled: true",
        "serviceMonitor:\n    enabled: true",
        "keycloakConfigCli:",
        "repository: adorsys/keycloak-config-cli",
        'tag: "6.5.1"',
        "- /app/keycloak-config-cli.jar",
        "runAsUser: 65534",
        "IMPORT_VARSUBSTITUTION_ENABLED",
        "extraEnvVarsSecret: platform-sso-clients",
        '"protocolMapper": "oidc-usermodel-realm-role-mapper"',
        '"name": "prometheus-audience"',
    ):
        require_text(
            premium_keycloak_text,
            needle,
            f"premium Keycloak profile must include {needle.splitlines()[0]}",
        )
    reject_text(
        premium_keycloak_text,
        "bitnamilegacy/keycloak",
        "premium Keycloak profile must not use the unsupported Bitnami legacy image",
    )

    premium_external_secrets_text = read(premium_external_secrets_values)
    for needle in (
        "installCRDs: true",
        "replicaCount: 2",
        "leaderElect: true",
        "podDisruptionBudget:\n  enabled: true\n  minAvailable: 1",
        "serviceMonitor:\n  enabled: true",
        "renderMode: skipIfMissing",
        "release: monitoring",
        "webhook:\n  replicaCount: 2\n  failurePolicy: Fail",
        "certController:\n  replicaCount: 2",
    ):
        require_text(
            premium_external_secrets_text,
            needle,
            f"premium External Secrets profile must include {needle.splitlines()[0]}",
        )

    premium_openbao_text = read(premium_openbao_values)
    for needle in (
        "failurePolicy: Fail",
        "namespaceSelector:\n      matchLabels:",
        'platform.gitops/openbao-injection: "enabled"',
        "key: kubernetes.io/metadata.name\n          operator: NotIn",
        "server:\n  enabled: true",
        "statefulSet:\n    securityContext:\n      container:\n        allowPrivilegeEscalation: false\n        capabilities:\n          drop:\n            - ALL",
        "dataStorage:\n    enabled: true\n    size: 20Gi\n    storageClass: longhorn-critical-encrypted",
        "persistentVolumeClaimRetentionPolicy:\n      whenDeleted: Retain\n      whenScaled: Retain",
        "auditStorage:\n    enabled: true\n    size: 10Gi\n    storageClass: longhorn-critical-encrypted",
        "standalone:\n    enabled: false",
        "ha:\n    enabled: true\n    replicas: 3",
        "raft:\n      enabled: true\n      setNodeId: true",
        "storage \"raft\"",
        "service_registration \"kubernetes\"",
        "disruptionBudget:\n    enabled: true\n    maxUnavailable: 1",
        "serverTelemetry:\n  serviceMonitor:\n    enabled: true",
        "grafanaDashboard:\n    enabled: true",
    ):
        require_text(
            premium_openbao_text,
            needle,
            f"premium OpenBao profile must include {needle.splitlines()[0]}",
        )

    for harbor_values, label in (
        (base_harbor_values, "base Harbor profile"),
        (premium_harbor_values, "premium Harbor profile"),
    ):
        harbor_text = read(harbor_values)
        for needle in (
            "expose:\n  type: ingress\n  tls:\n    enabled: true\n    certSource: auto",
            "externalURL: https://harbor.<PLATFORM_DOMAIN>",
            "existingSecretAdminPassword: harbor-admin",
            "existingSecretAdminPasswordKey: HARBOR_ADMIN_PASSWORD",
            "existingSecretSecretKey: harbor-secret-key",
            "metrics:\n  enabled: true\n  serviceMonitor:\n    enabled: true",
        ):
            require_text(harbor_text, needle, f"{label} must include {needle.splitlines()[0]}")

    base_harbor_text = read(base_harbor_values)
    for needle in (
        "updateStrategy:\n  type: Recreate",
        "persistence:\n  enabled: true",
        "imageChartStorage:\n    type: filesystem",
        "database:\n  type: internal\n  internal:\n    resources:",
        "redis:\n  type: internal\n  internal:\n    resources:",
        "storageClass: <HARBOR_STORAGE_CLASS>",
        "size: <HARBOR_REGISTRY_SIZE>",
        "size: <HARBOR_JOBLOG_SIZE>",
        "size: <HARBOR_DATABASE_SIZE>",
        "size: <HARBOR_REDIS_SIZE>",
        "size: <HARBOR_TRIVY_SIZE>",
    ):
        require_text(base_harbor_text, needle, f"base Harbor profile must include {needle}")

    premium_harbor_text = read(premium_harbor_values)
    for needle in (
        "portal:\n  replicas: 2\n  podDisruptionBudget:\n    enabled: true\n    minAvailable: 1",
        "core:\n  replicas: 2\n  podDisruptionBudget:\n    enabled: true\n    minAvailable: 1",
        "extraEnvVars:\n    - name: _REDIS_URL_CORE\n      valueFrom:\n        secretKeyRef:\n          name: harbor-redis-url\n          key: REDIS_URL_CORE",
        "jobservice:\n  replicas: 2\n  podDisruptionBudget:",
        "jobLoggers:\n    - database",
        "registry:\n  replicas: 2\n  podDisruptionBudget:",
        "  controller:\n    resources:",
        "trivy:\n  enabled: true\n  replicas: 2\n  podDisruptionBudget:",
        "exporter:\n  replicas: 2\n  podDisruptionBudget:",
        "topologyKey: kubernetes.io/hostname\n      whenUnsatisfiable: DoNotSchedule",
        "updateStrategy:\n  type: RollingUpdate",
        "persistence:\n  enabled: false",
        "imageChartStorage:\n    disableredirect: true\n    type: s3",
        "regionendpoint: <HARBOR_S3_ENDPOINT>",
        "existingSecret: <HARBOR_S3_SECRET_NAME>",
        "database:\n  type: external",
        "host: platform-postgres-rw.platform-databases.svc.cluster.local",
        "existingSecret: harbor-database",
        "redis:\n  type: external",
        "addr: platform-valkey-primary.platform-cache.svc.cluster.local:6379",
        "tlsOptions:\n      enable: true",
        "existingSecret: harbor-redis",
    ):
        require_text(premium_harbor_text, needle, f"premium Harbor profile must include {needle.splitlines()[0]}")
    for forbidden in (
        "imageChartStorage:\n    type: filesystem",
        "database:\n  type: internal",
        "updateStrategy:\n  type: Recreate",
    ):
        reject_text(
            premium_harbor_text,
            forbidden,
            f"premium Harbor profile must not include {forbidden.splitlines()[0]}",
        )
    base_monitoring_text = read(base_monitoring_values)
    for needle in (
        "crds:\n  enabled: true",
        "prometheusSpec:\n    replicas: 1\n    retention: 7d",
        "    resources:\n      requests:\n        cpu: 250m\n        memory: 1Gi\n      limits:\n        memory: 2Gi",
        "alertmanagerSpec:\n    replicas: 1\n    resources:\n      requests:\n        cpu: 50m\n        memory: 128Mi",
        "grafana:\n  replicas: 1\n  admin:",
        "resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi",
        "admin:\n    existingSecret: grafana-admin\n    userKey: admin-user\n    passwordKey: admin-password",
        "storageClassName: longhorn",
        "podMonitorSelectorNilUsesHelmValues: false",
        "serviceMonitorSelectorNilUsesHelmValues: false",
        "prometheus.<PLATFORM_DOMAIN>",
        "grafana.<PLATFORM_DOMAIN>",
    ):
        require_text(
            base_monitoring_text,
            needle,
            f"base monitoring profile must include {needle.splitlines()[0]}",
        )

    premium_monitoring_text = read(premium_monitoring_values)
    for needle in (
        "prometheus:\n  podDisruptionBudget:\n    enabled: true\n    minAvailable: 1",
        "prometheusSpec:\n    replicas: 2\n    podAntiAffinity: hard\n    podAntiAffinityTopologyKey: kubernetes.io/hostname",
        "retention: 15d",
        "    resources:\n      requests:\n        cpu: 250m\n        memory: 2Gi\n      limits:\n        memory: 4Gi",
        "alertmanager:\n  enabled: true\n  podDisruptionBudget:\n    enabled: true\n    minAvailable: 2",
        "alertmanagerSpec:\n    useExistingSecret: true\n    configSecret: alertmanager-platform-config\n    replicas: 3\n    podAntiAffinity: hard\n    podAntiAffinityTopologyKey: kubernetes.io/hostname\n    resources:\n      requests:\n        cpu: 100m\n        memory: 256Mi",
        "grafana:\n  replicas: 2\n  deploymentStrategy:\n    type: RollingUpdate",
        "podDisruptionBudget:\n    minAvailable: 1",
        "topologyKey: kubernetes.io/hostname\n      whenUnsatisfiable: DoNotSchedule",
        "resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi",
        "admin:\n    existingSecret: grafana-admin\n    userKey: admin-user\n    passwordKey: admin-password",
        "envValueFrom:\n    GF_DATABASE_PASSWORD:",
        "name: grafana-database\n        key: password",
        "grafana.ini:\n    database:\n      type: postgres",
        "host: platform-postgres-rw.platform-databases.svc.cluster.local:5432",
        "persistence:\n    enabled: false",
        "storageClassName: longhorn-standard-encrypted",
        "podMonitorSelectorNilUsesHelmValues: false",
        "serviceMonitorSelectorNilUsesHelmValues: false",
    ):
        require_text(
            premium_monitoring_text,
            needle,
            f"premium monitoring profile must include {needle.splitlines()[0]}",
        )
    reject_text(
        premium_monitoring_text,
        "persistence:\n    enabled: true",
        "premium Grafana must not use a single-writer PVC",
    )
    base_loki_text = read(base_loki_values)
    for needle in (
        "deploymentMode: SimpleScalable",
        "replication_factor: 1",
        "chunks: <LOKI_CHUNKS_BUCKET>",
        "endpoint: <OBJECT_STORAGE_ENDPOINT>",
        "schema: v13",
        "write:\n  replicas: 1\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi",
        "read:\n  replicas: 1\n  resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi",
        "backend:\n  replicas: 1\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi",
        "storageClass: <LOKI_STORAGE_CLASS>",
        "size: <LOKI_WRITE_CACHE_SIZE>",
        "size: <LOKI_BACKEND_CACHE_SIZE>",
        "gateway:\n  enabled: true\n  resources:\n    requests:\n      cpu: 50m\n      memory: 64Mi",
        "loki.<PLATFORM_DOMAIN>",
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(base_loki_text, needle, f"base Loki profile must include {needle.splitlines()[0]}")
    if base_loki_text.count("enableStatefulSetAutoDeletePVC: false") < 2:
        fail(
            "base Loki profile must retain both write and backend StatefulSet claims"
        )

    premium_loki_text = read(premium_loki_values)
    for needle in (
        "deploymentMode: SimpleScalable",
        "replication_factor: 3",
        "write:\n  replicas: 3\n  resources:\n    requests:\n      cpu: 250m\n      memory: 1Gi",
        "read:\n  replicas: 3\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi",
        "backend:\n  replicas: 3\n  resources:\n    requests:\n      cpu: 250m\n      memory: 1Gi",
        "gateway:\n  enabled: true\n  replicas: 3\n  basicAuth:\n    enabled: true\n    existingSecret: loki-gateway-basic-auth",
        "resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi",
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(premium_loki_text, needle, f"premium Loki profile must include {needle.splitlines()[0]}")
    if premium_loki_text.count("enableStatefulSetAutoDeletePVC: false") < 2:
        fail(
            "premium Loki profile must retain both write and backend StatefulSet claims"
        )

    for velero_values, label in (
        (base_velero_values, "base Velero profile"),
        (premium_velero_values, "premium Velero profile"),
    ):
        velero_text = read(velero_values)
        for needle in (
            "features: EnableCSI",
            "defaultVolumesToFsBackup: false",
            "provider: <BACKUP_PROVIDER>",
            "bucket: <BACKUP_BUCKET>",
            "s3Url: <BACKUP_OBJECT_STORAGE_ENDPOINT>",
            "existingSecret: velero-credentials",
            "deployNodeAgent: true",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n  limits:\n    memory: 512Mi",
            "nodeAgent:\n  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
            "snapshotsEnabled: true",
            "snapshotMoveData: true",
            "platform-daily:",
            "schedule: <VELERO_DAILY_BACKUP_CRON>",
            "serviceMonitor:\n    enabled: true",
        ):
            require_text(velero_text, needle, f"{label} must include {needle.splitlines()[0]}")

    for velero_kustomization, label in (
        (base_velero_kustomization, "base Velero Kustomization"),
        (premium_velero_kustomization, "premium Velero Kustomization"),
    ):
        require_text(
            read(velero_kustomization),
            "includeCRDs: true",
            f"{label} must render chart CRDs",
        )

    premium_velero_text = read(premium_velero_values)
    for needle in (
        "deployNodeAgent: true",
        "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n  limits:\n    memory: 512Mi",
        "nodeAgent:\n  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "snapshotsEnabled: true",
        "snapshotMoveData: true",
        "platform-daily:",
        "existingSecret: velero-credentials",
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(premium_velero_text, needle, f"premium Velero profile must include {needle.splitlines()[0]}")
    for step_ca_values, label in (
        (base_step_ca_values, "base step-ca profile"),
        (premium_step_ca_values, "premium step-ca profile"),
    ):
        step_ca_text = read(step_ca_values)
        for needle in (
            "kind: StatefulSet",
            "replicaCount: 1",
            "service:\n  type: ClusterIP\n  port: 443\n  targetPort: 9000",
            "address: :9000",
            "accessModes:\n      - ReadWriteOnce",
            "ssh:\n    enabled: false",
            "autocert:\n  enabled: false",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n  limits:\n    memory: 1Gi",
        ):
            require_text(step_ca_text, needle, f"{label} must include {needle.splitlines()[0]}")

    health_text = read(health_playbook)
    if re.search(r"\beval\s", health_text):
        fail("platform-app-health must not use eval in inline shell probes")
    catalog_apps = application_names(base_apps, premium_apps)
    health_apps = extract_default_word_list("platform_app_health_required_apps_effective", health_text)
    require_unique_words(health_apps, "platform-app-health required app defaults")
    missing_health_apps = sorted(set(catalog_apps) - set(health_apps))
    if missing_health_apps:
        fail(f"platform-app-health does not enforce apps: {', '.join(missing_health_apps)}")
    if "PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS" not in health_text:
        fail("platform-app-health must expose whether existing Argo CD Applications are included in app health checks")
    if "platform_app_health_include_existing_apps_effective" not in health_text:
        fail("platform-app-health must default existing Argo CD Application inclusion through an effective variable")
    if "PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO" not in health_text:
        fail("platform-app-health must expose temporary seed Git/source repository enforcement")
    if "platform_app_health_forbid_temporary_repo_effective" not in health_text:
        fail("platform-app-health must default temporary seed Git/source repository enforcement through an effective variable")
    if "PLATFORM_APP_HEALTH_OPENBAO_READY" not in health_text:
        fail("platform-app-health must expose OpenBao initialization readiness enforcement")
    if "platform_app_health_openbao_ready_effective" not in health_text:
        fail("platform-app-health must default OpenBao readiness enforcement through an effective variable")
    for needle in (
        "openbao-initialization-ceremony-pending",
        "openbao-uninitialized-and-sealed-bootstrap-state",
        '"initialized"[[:space:]]*:[[:space:]]*false',
        '"sealed"[[:space:]]*:[[:space:]]*true',
    ):
        require_text(health_text, needle, f"platform-app-health must safely classify OpenBao bootstrap readiness: {needle}")
    if "PLATFORM_APP_HEALTH_EXPECTED_REPO_URL" not in health_text:
        fail("platform-app-health must expose exact production Argo CD source repository enforcement")
    if "platform_app_health_expected_repo_url_effective" not in health_text:
        fail("platform-app-health must default exact production source repository checks through an effective variable")
    health_namespaces = extract_default_word_list("platform_app_health_namespaces_effective", health_text)
    require_unique_words(health_namespaces, "platform-app-health namespace defaults")
    missing_health_namespaces = sorted(
        set(required_destination_namespaces(base_apps, premium_apps)) - set(health_namespaces)
    )
    if missing_health_namespaces:
        fail(f"platform-app-health does not check required app namespaces: {', '.join(missing_health_namespaces)}")
    if "PLATFORM_APP_HEALTH_STORAGE_CLASSES" not in health_text:
        fail("platform-app-health must expose required StorageClass enforcement")
    health_storage_classes = extract_default_word_list("platform_app_health_storage_classes_effective", health_text)
    require_unique_words(health_storage_classes, "platform-app-health StorageClass defaults")
    missing_health_storage_classes = sorted(set(required_storage_classes) - set(health_storage_classes))
    if missing_health_storage_classes:
        fail(f"platform-app-health does not check StorageClasses: {', '.join(missing_health_storage_classes)}")
    if "PLATFORM_APP_HEALTH_LONGHORN_RUNTIME" not in health_text:
        fail("platform-app-health must expose Longhorn runtime node/volume enforcement")
    if "platform_app_health_longhorn_runtime_effective" not in health_text:
        fail("platform-app-health must default Longhorn runtime checks through an effective variable")
    if "PLATFORM_APP_HEALTH_HA_REPLICAS" not in health_text:
        fail("platform-app-health must expose critical HA workload replica enforcement")
    if "platform_app_health_ha_replicas_effective" not in health_text:
        fail("platform-app-health must default critical HA workload replica checks through an effective variable")
    if "PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG" not in health_text:
        fail("platform-app-health must expose expected Woodpecker runtime image tag enforcement")
    if "platform_app_health_woodpecker_image_tag_effective" not in health_text:
        fail("platform-app-health must default Woodpecker runtime image tag checks through an effective variable")
    if "PLATFORM_APP_HEALTH_CNPG_CLUSTERS" not in health_text:
        fail("platform-app-health must expose CloudNativePG cluster readiness enforcement")
    if "platform_app_health_cnpg_clusters_effective" not in health_text:
        fail("platform-app-health must default CloudNativePG cluster checks through an effective variable")
    if "PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET" not in health_text:
        fail("platform-app-health must expose CloudNativePG object-storage secret enforcement")
    if "platform_app_health_cnpg_object_storage_secret_effective" not in health_text:
        fail("platform-app-health must default CloudNativePG object-storage secret checks through an effective variable")
    if "PLATFORM_APP_HEALTH_CERTIFICATES" not in health_text:
        fail("platform-app-health must expose cert-manager Certificate readiness enforcement")
    if "platform_app_health_certificates_effective" not in health_text:
        fail("platform-app-health must default cert-manager Certificate checks through an effective variable")
    if "PLATFORM_APP_HEALTH_TRUST_BUNDLES" not in health_text:
        fail("platform-app-health must expose trust-manager Bundle readiness enforcement")
    if "platform_app_health_trust_bundles_effective" not in health_text:
        fail("platform-app-health must default trust-manager Bundle checks through an effective variable")
    if "PLATFORM_APP_HEALTH_STEP_CA_API" not in health_text:
        fail("platform-app-health must expose step-ca API readiness enforcement")
    if "platform_app_health_step_ca_api_effective" not in health_text:
        fail("platform-app-health must default step-ca API checks through an effective variable")
    if "PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT" not in health_text:
        fail("platform-app-health must expose node-originated VIP strict mode")
    if "platform_app_health_node_ingress_strict_effective" not in health_text:
        fail("platform-app-health must default node-originated VIP checks through an effective strict variable")
    if "PLATFORM_APP_HEALTH_GUI_APPS" not in health_text:
        fail("platform-app-health must expose a GUI app filter for subset profiles")
    health_gui_apps = extract_default_word_list("platform_app_health_gui_apps_effective", health_text)
    require_unique_words(health_gui_apps, "platform-app-health GUI app defaults")
    missing_health_gui_apps = sorted(set(required_gui_hosts) - set(health_gui_apps))
    if missing_health_gui_apps:
        fail(f"platform-app-health does not probe GUI apps by default: {', '.join(missing_health_gui_apps)}")
    if "PLATFORM_APP_HEALTH_REGISTRY_API" not in health_text:
        fail("platform-app-health must expose Harbor registry API enforcement")
    if "platform_app_health_registry_api_effective" not in health_text:
        fail("platform-app-health must default Harbor registry API checks through an effective variable")
    if "PLATFORM_APP_HEALTH_MONITORING_API" not in health_text:
        fail("platform-app-health must expose Grafana/Prometheus API enforcement")
    if "platform_app_health_monitoring_api_effective" not in health_text:
        fail("platform-app-health must default Grafana/Prometheus API checks through an effective variable")
    if "PLATFORM_APP_HEALTH_LOKI_API" not in health_text:
        fail("platform-app-health must expose Loki API readiness enforcement")
    if "platform_app_health_loki_api_effective" not in health_text:
        fail("platform-app-health must default Loki API checks through an effective variable")
    if "PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE" not in health_text:
        fail("platform-app-health must expose Velero BackupStorageLocation enforcement")
    if "platform_app_health_velero_backup_storage_effective" not in health_text:
        fail("platform-app-health must default Velero BackupStorageLocation checks through an effective variable")
    if "PLATFORM_APP_HEALTH_VELERO_SCHEDULES" not in health_text:
        fail("platform-app-health must expose Velero backup schedule enforcement")
    if "platform_app_health_velero_schedules_effective" not in health_text:
        fail("platform-app-health must default Velero backup schedule checks through an effective variable")
    if "PLATFORM_APP_HEALTH_APP_SECRETS" not in health_text:
        fail("platform-app-health must expose generated app secret contract enforcement")
    if "platform_app_health_app_secrets_effective" not in health_text:
        fail("platform-app-health must default generated app secret contract checks through an effective variable")
    if "PLATFORM_APP_HEALTH_ARGOCD_RUNTIME" not in health_text:
        fail("platform-app-health must expose Argo CD runtime component/service enforcement")
    if "platform_app_health_argocd_runtime_effective" not in health_text:
        fail("platform-app-health must default Argo CD runtime checks through an effective variable")
    if "PLATFORM_APP_HEALTH_ARGOCD_GUARDED_PRUNE" not in health_text:
        fail("platform-app-health must expose live Argo CD guarded pruning enforcement")
    if "platform_app_health_argocd_guarded_prune_effective" not in health_text:
        fail("platform-app-health must default live Argo CD guarded pruning checks through an effective variable")
    if "PLATFORM_APP_HEALTH_FORGEJO_SINGLETON_SAFETY" not in health_text:
        fail("platform-app-health must expose Forgejo singleton disruption-safety enforcement")
    if "platform_app_health_forgejo_singleton_safety_effective" not in health_text:
        fail("platform-app-health must default Forgejo singleton disruption-safety checks through an effective variable")
    if "PLATFORM_APP_HEALTH_HTTP_REDIRECT" not in health_text:
        fail("platform-app-health must expose HTTP-to-HTTPS redirect enforcement")
    if "platform_app_health_http_redirect_effective" not in health_text:
        fail("platform-app-health must default HTTP redirect enforcement through an effective variable")
    for task_name in (
        "Verify Argo CD platform application health",
        "Verify live Argo CD Applications use guarded pruning",
        "Verify Argo CD application source repositories are production-safe",
        "Verify platform namespace pod readiness",
        "Verify Argo CD runtime component coverage",
        "Verify critical HA workload replica coverage",
        "Verify Forgejo singleton disruption safeguards",
        "Verify Woodpecker runtime image tags",
        "Verify cert-manager Certificate readiness",
        "Verify trust-manager Bundle readiness",
        "Verify step-ca API readiness",
        "Verify CloudNativePG cluster readiness",
        "Verify Loki API readiness",
        "Verify Velero BackupStorageLocation readiness",
        "Verify Velero backup schedule readiness",
        "Verify generated platform app secret contracts",
        "Verify required platform StorageClasses",
        "Verify platform namespace PVC readiness",
        "Verify Longhorn node and volume runtime health",
        "Verify configured GUI ingress backend endpoints",
        "Probe configured GUI app ingress from Ansible controller",
        "Probe Harbor registry API from Ansible controller",
        "Verify monitoring public authentication boundary from Ansible controller",
        "Probe configured GUI HTTP redirects from Ansible controller",
        "Probe configured GUI app ingress from every RKE2 node",
        "Probe Argo CD and Woodpecker ClusterIP service paths from every RKE2 node",
        "Probe Argo CD and Woodpecker ClusterIP service paths from pods pinned to every RKE2 node",
        "Stop when platform app health checks fail",
    ):
        require_text(health_text, f"- name: {task_name}", f"platform-app-health is missing task: {task_name}")
    for result_name in (
        "platform_app_health_argocd_app_probe",
        "platform_app_health_argocd_guarded_prune_probe",
        "platform_app_health_argocd_source_probe",
        "platform_app_health_pod_probe",
        "platform_app_health_argocd_runtime_probe",
        "platform_app_health_ha_replica_probe",
        "platform_app_health_forgejo_singleton_safety_probe",
        "platform_app_health_woodpecker_image_probe",
        "platform_app_health_certificate_probe",
        "platform_app_health_trust_bundle_probe",
        "platform_app_health_step_ca_api_probe",
        "platform_app_health_cnpg_cluster_probe",
        "platform_app_health_loki_api_probe",
        "platform_app_health_velero_backup_storage_probe",
        "platform_app_health_velero_schedule_probe",
        "platform_app_health_app_secret_probe",
        "platform_app_health_storage_class_probe",
        "platform_app_health_pvc_probe",
        "platform_app_health_longhorn_runtime_probe",
        "platform_app_health_ingress_backend_probe",
        "platform_app_health_controller_ingress_probe",
        "platform_app_health_registry_api_probe",
        "platform_app_health_monitoring_api_probe",
        "platform_app_health_http_redirect_probe",
        "platform_app_health_ingress_probe",
        "platform_app_health_service_probe",
        "platform_app_health_pod_service_probe",
        "platform_app_health_failed",
    ):
        require_text(health_text, result_name, f"platform-app-health is missing result variable: {result_name}")
    require_text(
        health_text,
        "delegate_to: localhost",
        "platform-app-health must verify controller/client app VIP access from localhost",
    )
    require_text(
        health_text,
        "platform_app_health_node_ingress_strict_effective | bool",
        "platform-app-health must use node-ingress strict mode in the failure verdict",
    )
    require_text(
        health_text,
        "map('bool') | select('equalto', true)",
        "platform-app-health final failure checks must coerce host facts to booleans",
    )
    require_text(
        health_text,
        "not-required-by-platform-app-health-gui-apps",
        "platform-app-health must skip GUI probes excluded by PLATFORM_APP_HEALTH_GUI_APPS",
    )
    require_text(
        health_text,
        "platform PVCs are Bound",
        "platform-app-health success message must include PVC readiness",
    )
    require_text(
        health_text,
        ".status.operationState.phase",
        "platform-app-health must inspect Argo CD operation state",
    )
    require_text(
        health_text,
        "no active or failed operations",
        "platform-app-health success message must mention clean Argo CD operations",
    )
    require_text(
        health_text,
        "live Argo CD Applications enable self-healing and approval-gated foreground pruning in the final sync wave with empty-target protection",
        "platform-app-health success message must include live guarded pruning policy",
    )
    for needle in (
        "automated.prune=true",
        "automated.selfHeal=true",
        "automated.allowEmpty=false",
        "Prune=confirm",
        "PruneLast=true",
        "PrunePropagationPolicy=foreground",
        "reason=unguarded-pruning-policy",
        "PLATFORM_APP_HEALTH_ARGOCD_GUARDED_PRUNE=false make platform-app-health",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify live Argo CD guarded pruning: {needle}",
        )
    for needle in (
        "forgejo_replicas=",
        "rollout_strategy=",
        "min_available=",
        "reason=forgejo-singleton-overlapping-rollout-risk",
        "reason=missing-forgejo-singleton-pdb",
        "PLATFORM_APP_HEALTH_FORGEJO_SINGLETON_SAFETY=false make platform-app-health",
        "singleton Forgejo uses a Recreate rollout and a minAvailable=1 PodDisruptionBudget",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify live Forgejo singleton safeguards: {needle}",
        )
    operations_text = read(operations_doc)
    require_text(
        operations_text,
        "Its live\nApplication probe verifies that pruning, self-healing, empty-target protection,\nconfirmation, final-wave ordering, and foreground propagation",
        "operations runbook must require live guarded-pruning verification",
    )
    production_readiness_text = read(production_readiness_doc)
    require_text(
        production_readiness_text,
        "make platform-app-health` proves every live Application has self-healing plus approval-gated, last-wave foreground pruning",
        "production readiness checklist must require live guarded-pruning evidence",
    )
    require_text(
        health_text,
        "production-safe repository sources instead of temporary seed Git or insecure git:// URLs",
        "platform-app-health success message must include production-safe Argo CD source repositories",
    )
    require_text(
        health_text,
        "match the expected production repo URL when one is set",
        "platform-app-health success message must include exact production source repository matching",
    )
    for needle in (
        "temporary-or-insecure-git-protocol",
        "temporary-seed-git-source",
        "unexpected-repo-url",
        "PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false make platform-app-health",
        "PLATFORM_APP_HEALTH_EXPECTED_REPO_URL=<PRIVATE_REPO_URL> make platform-app-health",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must enforce production-safe Argo CD source repositories: {needle}",
        )
    require_text(
        health_text,
        "Argo CD runtime components and configured repo-server/Redis service endpoints are present",
        "platform-app-health success message must include Argo CD runtime component coverage",
    )
    for needle in (
        "PLATFORM_APP_HEALTH_ARGOCD_RUNTIME=false make platform-app-health",
        "configured_repo_server=${repo_server} repo_service=${repo_service}",
        "configured_redis_server=${redis_server} redis_service=${redis_service}",
        "service_mode=headless",
        "argocd-service-has-no-address",
        "argocd-service-has-no-ready-endpoints",
        "reason=surplus-rollout-pod",
        "platform_app_health_argocd_runtime_probe",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Argo CD runtime component/service coverage: {needle}",
        )
    require_text(
        health_text,
        "required StorageClasses exist",
        "platform-app-health success message must include StorageClass readiness",
    )
    require_text(
        health_text,
        "Woodpecker server and agent pods run the expected pinned image tag",
        "platform-app-health success message must include Woodpecker runtime image tag readiness",
    )
    for needle in (
        "woodpecker-image-tag-mismatch",
        "PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG=v3.16.0",
        'expected_tag="{{ platform_app_health_woodpecker_image_tag_effective }}"',
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must enforce Woodpecker runtime image tag drift: {needle}",
        )
    require_text(
        health_text,
        "generated Harbor/Forgejo/Woodpecker/Keycloak/Grafana/Loki/Velero/CloudNativePG/Valkey and SSO secrets exist with required keys",
        "platform-app-health success message must include generated app secret readiness",
    )
    require_text(
        health_text,
        "Longhorn nodes are Ready/schedulable and Longhorn volumes backing Bound PVCs are healthy",
        "platform-app-health success message must include Longhorn runtime readiness",
    )
    require_text(
        health_text,
        "critical HA workloads meet minimum desired and ready replica coverage",
        "platform-app-health success message must include critical HA replica coverage",
    )
    require_text(
        health_text,
        "CloudNativePG clusters are Ready when present or explicitly required",
        "platform-app-health success message must include CloudNativePG readiness",
    )
    for needle in (
        "certificates.cert-manager.io",
        "cert-manager-certificate-crd-missing",
        "cert-manager-certificate-not-ready",
        "cert-manager-certificate-secret-missing",
        'PLATFORM_APP_HEALTH_CERTIFICATES="argocd/argocd-server-tls"',
        "cert-manager Certificates are Ready when present or explicitly required",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify cert-manager Certificate readiness: {needle}",
        )
    for needle in (
        "bundles.trust.cert-manager.io",
        "trust-manager-bundle-crd-missing",
        "trust-bundle-not-ready",
        'PLATFORM_APP_HEALTH_TRUST_BUNDLES="platform-public-roots"',
        "trust-manager Bundles are synced when present or explicitly required",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify trust-manager Bundle readiness: {needle}",
        )
    for needle in (
        "https://${cluster_ip}:${port}/health",
        "step-ca step-certificates step-ca-step-certificates",
        "unexpected-step-ca-health-http-code",
        "no-known-step-ca-service",
        "PLATFORM_APP_HEALTH_STEP_CA_API=false make platform-app-health",
        "step-ca answers /health on its ClusterIP service when required",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify step-ca API readiness: {needle}",
        )
    for needle in (
        "clusters.postgresql.cnpg.io",
        "cnpg-cluster-not-ready",
        "cnpg-ready-instances-below-desired",
        "cnpg-current-primary-missing",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "ACCESS_KEY_ID",
        "SECRET_ACCESS_KEY",
        "cnpg-object-storage-secret-contract-disabled",
        "PLATFORM_APP_HEALTH_CNPG_CLUSTERS=\"platform-databases/platform-postgres\"",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify CloudNativePG cluster readiness: {needle}",
        )
    for needle in (
        "loki-gateway:80:/ready",
        "loki-read:3100:/ready",
        "no-known-loki-service",
        "no-loki-service-ready",
        "PLATFORM_APP_HEALTH_LOKI_API=false make platform-app-health",
        "Loki answers a known /ready endpoint",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Loki API readiness: {needle}",
        )
    for needle in (
        "backupstoragelocations.velero.io",
        "velero-backupstoragelocation-crd-missing",
        "velero-backupstoragelocation-not-available",
        "PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false make platform-app-health",
        "Velero BackupStorageLocations are Available",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Velero backup storage readiness: {needle}",
        )
    for needle in (
        "schedules.velero.io",
        "velero-schedule-crd-missing",
        "no-velero-backup-schedules",
        "velero-schedule-paused",
        "PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false make platform-app-health",
        "an enabled Velero backup schedule exists",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Velero backup schedule readiness: {needle}",
        )
    for needle in (
        "HARBOR_ADMIN_PASSWORD",
        "secretKey",
        "PLATFORM_VALKEY_AUTH_SECRET_NAME",
        "PLATFORM_VALKEY_PASSWORD_KEY",
        "platform_valkey_auth_secret_name_effective",
        "platform_valkey_password_key_effective",
        "platform-valkey-not-required-by-platform-app-health-required-apps",
        "MINIO_ROOT_SECRET_NAME",
        "platform_minio_root_secret_name_effective",
        "minio-not-required-by-platform-app-health-required-apps",
        "PLATFORM_APP_HEALTH_HARBOR_PRODUCTION_SECRETS",
        "platform_app_health_harbor_production_secrets_effective",
        "platform_harbor_database_secret_name_effective",
        "platform_harbor_redis_secret_name_effective",
        "platform_harbor_s3_secret_name_effective",
        "REGISTRY_STORAGE_S3_ACCESSKEY",
        "REGISTRY_STORAGE_S3_SECRETKEY",
        "harbor-production-secret-contracts-disabled",
        "FORGEJO_DATABASE_SECRET_NAME",
        "FORGEJO_REDIS_MODE",
        "FORGEJO_REDIS_SECRET_NAME",
        "PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS",
        "platform_app_health_forgejo_production_secrets_effective",
        "platform_forgejo_database_secret_name_effective",
        "platform_forgejo_redis_mode_effective",
        "platform_forgejo_redis_secret_name_effective",
        "username password",
        "forgejo-redis-secret-contract-disabled",
        "forgejo-production-secret-contracts-disabled",
        "WOODPECKER_FORGEJO_CLIENT",
        "WOODPECKER_FORGEJO_SECRET",
        "WOODPECKER_DATABASE_DATASOURCE",
        "platform_woodpecker_database_secret_name_effective",
        "GRAFANA_ADMIN_SECRET_NAME",
        "platform_grafana_admin_secret_name_effective",
        "GRAFANA_DATABASE_SECRET_NAME",
        "PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET",
        "platform_app_health_grafana_database_secret_effective",
        "platform_grafana_database_secret_name_effective",
        "grafana-database-secret-contract-disabled",
        "admin-user",
        "admin-password",
        "go-template={{ '{{' }} index .data",
        "LOKI_S3_ACCESS_KEY_ID",
        "LOKI_S3_SECRET_ACCESS_KEY",
        "VELERO_CREDENTIALS_SECRET_NAME",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET",
        "missing-platform-app-secret",
        "missing-platform-app-secret-key",
        "invalid-platform-app-secret-key-encoding",
        "PLATFORM_APP_HEALTH_APP_SECRETS=skip make platform-app-health",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify generated app secret contracts: {needle}",
        )
    require_text(
        health_text,
        "driver.longhorn.io",
        "platform-app-health must verify Longhorn StorageClass provisioners",
    )
    for needle in (
        "nodes.longhorn.io",
        "volumes.longhorn.io",
        "longhorn-node-not-ready",
        "longhorn-node-scheduling-disabled",
        "longhorn-attached-volume-not-healthy",
        "PLATFORM_APP_HEALTH_LONGHORN_RUNTIME=false make platform-app-health",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Longhorn runtime storage health: {needle}",
        )
    for needle in (
        "ha-workload-replicas-below-minimum",
        "ha-workload-ready-replicas-below-minimum",
        "statefulset/argocd-redis-ha-server",
        "statefulset/woodpecker-agent",
        "deployment/platform-traefik deployment/traefik",
        "PLATFORM_APP_HEALTH_HA_REPLICAS=false make platform-app-health",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify critical HA workload replica coverage: {needle}",
        )
    require_text(
        health_text,
        "ready backend endpoints",
        "platform-app-health must verify GUI ingress backend endpoints",
    )
    for needle in (
        "https://${HOST}/v2/",
        "Docker-Distribution-Api-Version",
        "registry/2.0",
        "unexpected-registry-api-http-code",
        "PLATFORM_APP_HEALTH_REGISTRY_API=false make platform-app-health",
        "Harbor registry API answers on /v2/",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Harbor registry API readiness: {needle}",
        )
    require_text(
        health_text,
        "no-ingress-or-ingressroute-for-host",
        "platform-app-health must fail when a GUI host has no ingress route",
    )
    for needle in (
        "monitoring-public-authentication-not-enforced",
        "authentication_boundary=protected",
        "PLATFORM_APP_HEALTH_MONITORING_API=false make platform-app-health",
        "Grafana and Prometheus deny or redirect unauthenticated public access",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must verify Grafana/Prometheus API readiness: {needle}",
        )
    require_text(
        health_text,
        ".status.phase",
        "platform-app-health must inspect PVC phase",
    )
    require_text(
        health_text,
        "--resolve \"${host}:80:${VIP}\"",
        "platform-app-health must verify HTTP redirects through the app VIP",
    )
    require_text(
        health_text,
        "nodeName: \"${node}\"",
        "platform-app-health must pin service-path probe pods to every node",
    )
    require_text(
        health_text,
        "woodpecker-server.woodpecker.svc.cluster.local 9000",
        "platform-app-health must verify Woodpecker gRPC from pod networking",
    )
    require_text(
        health_text,
        "make platform-service-path-repair",
        "platform-app-health failure message must point ClusterIP failures to the service-path repair alias",
    )
    require_text(
        health_text,
        "PLATFORM_APP_HEALTH_SERVICE_CHECK_IMAGE",
        "platform-app-health must expose a diagnostic pod image override",
    )
    require_text(
        health_text,
        "PLATFORM_APP_HEALTH_SERVICE_CHECK_TIMEOUT",
        "platform-app-health must expose a diagnostic pod timeout override",
    )
    for needle in (
        "PLATFORM_APP_HEALTH_SERVICE_CHECK_CREATE_ATTEMPTS",
        'job_manifest="$(mktemp)"',
        'apply -f "${job_manifest}"',
        "job_manifest node=${node} job=${job_name} bytes=${manifest_bytes:-0}",
        'case "\\$1" in',
        "service-path-check-job-create-failed",
        "job_apply_summary expected=${expected} created=${created}",
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must retry and diagnose service-path probe creation: {needle}",
        )
    require_text(
        health_text,
        "no-curl-or-wget",
        "platform-app-health pod HTTP probe must report missing probe tools",
    )

    status_text = read(status_playbook)
    require_text(
        status_text,
        "Argo CD Application readiness:",
        "platform-status must summarize Argo CD Application readiness",
    )
    require_text(
        status_text,
        "Argo CD runtime service readiness:",
        "platform-status must summarize Argo CD runtime service readiness",
    )
    for needle in (
        "configured repo-server:",
        "configured Redis:",
        "ready_endpoints=",
        "service_mode=headless",
        "Argo CD runtime readiness: NOT READY",
        "Run: make platform-argocd-service-repair",
        "service_ready_endpoint_count",
    ):
        require_text(
            status_text,
            needle,
            f"platform-status must report Argo CD repo-server/Redis endpoint readiness: {needle}",
        )
    require_text(
        status_text,
        "===== Woodpecker runtime =====",
        "platform-status must summarize Woodpecker server/agent runtime readiness",
    )
    for needle in (
        "PLATFORM_STATUS_WOODPECKER_IMAGE_TAG",
        "Expected Woodpecker image tag:",
        "check_woodpecker_role server",
        "check_woodpecker_role agent",
        "woodpecker-status-image-tag-mismatch",
        "Woodpecker runtime readiness: NOT READY",
    ):
        require_text(
            status_text,
            needle,
            f"platform-status must report Woodpecker server/agent image tag drift: {needle}",
        )
    require_text(
        status_text,
        "GUI host HTTP reachability through app VIP:",
        "platform-status must summarize GUI host HTTP reachability through the app VIP",
    )
    for needle in (
        '--resolve "${fqdn}:443:${INGRESS_VIP}"',
        "GUI HTTP readiness: NOT READY",
        "502/504 usually point to Traefik backend service",
        "404 usually means no matching router",
        "published host(s) did not answer cleanly through",
    ):
        require_text(
            status_text,
            needle,
            f"platform-status must report app VIP GUI HTTP failures: {needle}",
        )
    require_text(
        status_text,
        "Controller/client GUI HTTP reachability through app VIP",
        "platform-status must summarize controller/client GUI HTTP reachability through the app VIP",
    )
    for needle in (
        "platform_status_controller_gui_probe",
        "delegate_to: localhost",
        "become: false",
        "Controller/client GUI HTTP readiness: NOT READY",
        "000 usually means VIP routing, firewall, or client reachability",
    ):
        require_text(
            status_text,
            needle,
            f"platform-status must report controller/client app VIP GUI HTTP failures: {needle}",
        )
    require_text(
        status_text,
        "Production readiness: NOT READY",
        "platform-status must clearly warn when app sync/health is not production-ready",
    )
    for needle in (
        "Argo CD Application source repositories:",
        "Expected production repo URL:",
        "PLATFORM_STATUS_EXPECTED_REPO_URL",
        "Production source readiness: NOT READY",
        "temporary seed Git, git://, missing repo URLs, or a repo URL different from the expected production source",
        "unexpected-repo-url",
        "make platform-seed-git-remove",
    ):
        require_text(
            status_text,
            needle,
            f"platform-status must report temporary Argo CD source repository usage: {needle}",
        )
    require_text(
        status_text,
        ".status.operationState.phase",
        "platform-status must inspect Argo CD operation state",
    )
    require_text(
        status_text,
        "operation_unhealthy",
        "platform-status must count active or failed Argo CD operations as not-ready",
    )
    require_text(
        status_text,
        "sync/health/operation state is clean",
        "platform-status success message must mention clean Argo CD operation state",
    )
    require_text(
        status_text,
        "Run: make platform-app-health",
        "platform-status must point failed app readiness to the app health gate",
    )

    verify_rke2_text = read(verify_rke2_playbook)
    for needle in (
        "RKE2_VERIFY_API_VIP",
        "rke2_api_vip_effective",
        "rke2_api_dns_effective",
        "Verify API VIP TCP/TLS readyz path from every node",
        "--server=https://{{ rke2_api_vip_effective }}:6443 get --raw=/readyz",
        "--server=https://{{ rke2_api_dns_effective }}:6443 get --raw=/readyz",
        "Show API VIP provider state",
        "kube-vip daemonset is absent; API VIP is expected to be provided externally.",
        "Stop after API VIP verification failure",
    ):
        require_text(
            verify_rke2_text,
            needle,
            f"rke2-verify must provide production API VIP/DNS proof: {needle}",
        )

    install_rke2_text = read(root / "ansible" / "playbooks" / "install-rke2.yml")
    for needle in (
        "Write RKE2 Kubernetes API audit policy",
        "audit-policy-file=/etc/rancher/rke2/audit-policy.yaml",
        "audit-log-path=/var/lib/rancher/rke2/server/logs/audit.log",
        "RKE2_AUDIT_POLICY_ENABLED",
        "rke2_audit_policy_enabled_effective",
        "Verify RKE2 Secret encryption at rest",
        "rke2 secrets-encrypt status",
        "Server Encryption Hashes: All hashes match",
        "RKE2_VERIFY_SECRETS_ENCRYPTION",
        "Require immutable RKE2 release inputs in production",
        "RKE2_INSTALL_SCRIPT_SHA256",
        "Do not use the moving stable channel",
    ):
        require_text(
            install_rke2_text,
            needle,
            f"RKE2 production install must retain API audit evidence: {needle}",
        )

    for needle in (
        "Verify RKE2 Secret encryption at rest",
        "rke2 secrets-encrypt status",
        "Encryption Status: Enabled",
        "Server Encryption Hashes: All hashes match",
    ):
        require_text(
            verify_rke2_text,
            needle,
            f"rke2-verify must retain Secret-encryption proof: {needle}",
        )

    policy_readiness_text = read(policy_readiness_playbook)
    for needle in (
        "PLATFORM_POLICY_ENFORCEMENT",
        "require-private-secret-workflow",
        "require-pod-security-baseline",
        "require-workload-baseline",
        "validatingpolicy.policies.kyverno.io",
        "validationActions[0]",
        "conditionStatus.ready",
        "clusterpolicy.kyverno.io",
        "approve its guarded prune",
        "policyreports.wgpolicyk8s.io",
        "managed_policy_violations",
        "platform_policy_validation_action",
        "PLATFORM_IMAGE_INTEGRITY_REQUIRED",
        "PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE",
        "imagevalidatingpolicy.policies.kyverno.io",
        "create --dry-run=server",
        "unsigned-image-was-admitted",
        "signed_image_admission=passed",
        "unverifiable_image_rejection=passed",
    ):
        require_text(
            policy_readiness_text,
            needle,
            f"policy readiness gate must retain enforcement proof: {needle}",
        )

    active_policy_verifier_text = read(active_policy_verifier)
    for needle in (
        "Kyverno CLI is required",
        "no-plaintext-secrets.yaml",
        "require-pod-security-baseline.yaml",
        "require-workload-baseline.yaml",
        "compliant.yaml",
        "violating.yaml",
        "privilege-escalation.yaml",
        "privileged-namespace.yaml",
        "verify-platform-images.yaml",
        "render_image_policy",
        'if "error: 0" not in output',
        "Active Kyverno CEL and image policy verification passed",
    ):
        require_text(
            active_policy_verifier_text,
            needle,
            f"active Kyverno policy verifier must cover {needle}",
        )
    kyverno_cli_installer_text = read(kyverno_cli_installer)
    for needle in (
        'version="1.18.1"',
        'sha256="5e6bba9ca85beec6c93e94ca7fb0972a66df3b2e67636a08bef090cd3fc6535c"',
        "releases/download/v${version}/${archive_name}",
        "sha256sum --check --strict",
    ):
        require_text(
            kyverno_cli_installer_text,
            needle,
            f"Kyverno CLI installer must retain pinned artifact proof: {needle}",
        )
    for workflow_path in (github_validate_workflow, github_release_workflow):
        workflow_text = read(workflow_path)
        for needle in (
            "scripts/bootstrap/install-kyverno-cli.sh",
            "python scripts/verify_active_kyverno_policies.py",
            "KYVERNO_BIN: ${{ runner.temp }}/platform-tools/kyverno",
        ):
            require_text(
                workflow_text,
                needle,
                f"{workflow_path.relative_to(root)} must enforce active CEL policy proof",
            )

    platform_tls_text = read(platform_tls_playbook)
    for needle in (
        "PLATFORM_WILDCARD_TLS_CERT_FILE",
        "PLATFORM_WILDCARD_TLS_KEY_FILE",
        "openssl x509",
        "openssl pkey",
        "checkhost",
        "argocd:argocd-server-tls",
        "keycloak:keycloak-tls",
        "platform_tls_remote_directory",
        "trap cleanup EXIT",
    ):
        require_text(
            platform_tls_text,
            needle,
            f"wildcard TLS automation must retain certificate safety controls: {needle}",
        )

    platform_tls_verify_text = read(platform_tls_verify_playbook)
    for needle in (
        "rke2_ingress_vip_effective",
        "openssl s_client",
        "-servername",
        "checkhost",
        "expected_fingerprint",
        "served_fingerprint",
        "trap cleanup EXIT",
        "keycloak keycloak-tls",
    ):
        require_text(
            platform_tls_verify_text,
            needle,
            f"TLS verification gate must retain ingress proof: {needle}",
        )

    for path in (
        production_evidence_script,
        production_evidence_runner,
        production_evidence_test,
        atomic_file_writer,
        atomic_file_test,
        image_inventory_capture,
        image_inventory_reconciler,
        image_inventory_validator,
        image_inventory_test,
        image_inventory_wrapper,
        image_inventory_playbook,
        image_inventory_example,
    ):
        if not path.is_file():
            fail(f"production evidence gate is missing required file: {path.relative_to(root)}")

    for path in (
        forgejo_recovery_runner,
        forgejo_recovery_validator,
        forgejo_recovery_wrapper,
        forgejo_recovery_playbook,
        forgejo_recovery_example,
    ):
        if not path.is_file():
            fail(f"Forgejo failover proof is missing required file: {path.relative_to(root)}")

    forgejo_recovery_runner_text = read(forgejo_recovery_runner)
    for needle in (
        'CONFIRMATION = "FAILOVER_FORGEJO_SINGLETON"',
        'kube.run("cordon", source_node)',
        'kube.run("uncordon", source_node)',
        '"schemaVersion": 2',
        '"recoveryMode": "node-failover"',
        '"sourceNodeRestoredSchedulable"',
        '"encryptionSecretRefs"',
    ):
        require_text(
            forgejo_recovery_runner_text,
            needle,
            f"Forgejo failover runner must retain production proof: {needle}",
        )

    forgejo_recovery_validator_text = read(forgejo_recovery_validator)
    for needle in (
        "schemaVersion must be 2",
        "sourceNode and targetNode must differ",
        "Forgejo must recover on a different node",
        "sourceNodeRestoredSchedulable must be true",
        "encryptionSecretRefs",
    ):
        require_text(
            forgejo_recovery_validator_text,
            needle,
            f"Forgejo failover validator must retain production proof: {needle}",
        )

    for path in (
        forgejo_recovery_wrapper,
        forgejo_recovery_playbook,
        backup_restore_doc,
        operations_doc,
        production_readiness_doc,
    ):
        require_text(
            read(path),
            "FAILOVER_FORGEJO_SINGLETON",
            f"Forgejo failover surface must require explicit approval: {path.relative_to(root)}",
        )

    production_evidence_text = read(production_evidence_script)
    for needle in (
        "REQUIRED_GATES",
        '"sourceProvenance"',
        '"profile"',
        '"renderedSchema"',
        '"supplyChain"',
        '"runtimeImageInventory"',
        '"networkIsolation"',
        '"internalTls"',
        '"openbaoReadiness"',
        '"openbaoCeremony"',
        '"observability"',
        '"capacity"',
        "logSha256",
        "operator and approver must be different people",
        "commit must be a 40-character lowercase Git SHA",
        "source.clean must be true",
        "source.expectedRef must belong to source.remote",
        "retained image inventory hash does not match imageInventory.sha256",
    ):
        require_text(
            production_evidence_text,
            needle,
            f"production evidence validator must retain release proof: {needle}",
        )

    production_evidence_runner_text = read(production_evidence_runner)
    for needle in (
        "umask 077",
        "PLATFORM_RELEASE_ID",
        "PLATFORM_EVIDENCE_OPERATOR",
        "PLATFORM_EVIDENCE_APPROVER",
        "PLATFORM_PRODUCTION_EVIDENCE_EXPECTED_REF",
        "git status --porcelain=v1 --untracked-files=all",
        "refs/remotes/",
        "HEAD to exactly match",
        "make platform-production-check",
        '"schemaVersion": 6',
        '"sourceProvenance": "passed"',
        '"renderedSchema": "passed"',
        '"supplyChain": "passed"',
        '"runtimeImageInventory": "passed"',
        '"imageInventory"',
        '"networkIsolation": "passed"',
        '"internalTls": "passed"',
        '"openbaoReadiness": "passed"',
        '"openbaoCeremony": "passed"',
        '"observability": "passed"',
        '"capacity": "passed"',
        "from scripts.atomic_file import atomic_write_text",
        "atomic_write_text(",
        "sha256sum",
        "verify_production_evidence.py",
    ):
        require_text(
            production_evidence_runner_text,
            needle,
            f"production evidence runner must retain release proof: {needle}",
        )

    atomic_file_writer_text = read(atomic_file_writer)
    for needle in (
        "tempfile.mkstemp(",
        "os.fchmod(descriptor, mode)",
        "os.fsync(handle.fileno())",
        "os.replace(temporary, destination)",
        "temporary.unlink(missing_ok=True)",
        "PRIVATE_FILE_MODE = 0o600",
    ):
        require_text(
            atomic_file_writer_text,
            needle,
            f"private artifact writer must remain atomic and owner-only: {needle}",
        )

    atomic_file_test_text = read(atomic_file_test)
    for needle in (
        "ATOMIC_ARTIFACT_PRODUCERS",
        "simulated replace failure",
        "failed atomic write damaged the prior artifact",
        "production evidence runner is missing private atomic output control",
    ):
        require_text(
            atomic_file_test_text,
            needle,
            f"private artifact writer self-test must retain failure coverage: {needle}",
        )

    image_inventory_capture_text = read(image_inventory_capture)
    for needle in (
        'CONTAINER_GROUPS = (',
        '"initContainerStatuses"',
        '"ephemeralContainerStatuses"',
        '"digestImage"',
        '"unresolved"',
    ):
        require_text(
            image_inventory_capture_text,
            needle,
            f"live image capture must retain sanitized digest proof: {needle}",
        )

    image_inventory_reconciler_text = read(image_inventory_reconciler)
    for needle in (
        "rendered images were neither observed live nor resolved by exception",
        "private/supply-chain",
        "must expire within 90 days",
        "image coverage is incomplete",
        '"signatureVerified"',
        '"admissionEnforced"',
    ):
        require_text(
            image_inventory_reconciler_text,
            needle,
            f"image inventory reconciler must fail closed: {needle}",
        )

    image_inventory_validator_text = read(image_inventory_validator)
    for needle in (
        "private-registry image lacks signature or admission coverage",
        "outside-registry image lacks an admission-scope exception",
        "rendered.unresolved must be zero",
        "live.unresolved must be zero",
    ):
        require_text(
            image_inventory_validator_text,
            needle,
            f"image inventory evidence validator must fail closed: {needle}",
        )

    image_inventory_wrapper_text = read(image_inventory_wrapper)
    for needle in (
        "capture-platform-image-inventory.yml",
        "reconcile_image_inventory.py",
        "verify_image_inventory_evidence.py",
        "PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE",
    ):
        require_text(
            image_inventory_wrapper_text,
            needle,
            f"image inventory wrapper must retain production proof: {needle}",
        )

    host_entries = re.findall(r"(?m)^\s+- app:\s*([A-Za-z0-9_.-]+)\s*$", read(root / "ansible/vars/platform-hostnames.yml"))
    missing_gui_hosts = sorted(set(required_gui_hosts) - set(host_entries))
    if missing_gui_hosts:
        fail(f"platform host entries are missing GUI apps: {', '.join(missing_gui_hosts)}")

    makefile_text = read(makefile)
    profile_check_text = read(profile_check_script)
    profile_check_test_text = read(profile_check_test)
    deployable_renderer_text = read(deployable_renderer)
    deployable_renderer_test_text = read(deployable_renderer_test)
    gitops_selection_helper_test_text = read(gitops_selection_helper_test)
    renderer_text = read(private_values_renderer)
    renderer_test_text = read(private_values_renderer_test) + read(
        synthetic_private_profile_helper
    )
    platform_secret_contract_test_text = read(platform_secret_contract_test)
    app_secrets_text = read(app_secrets_playbook)
    bootstrap_argocd_text = read(root / "ansible/playbooks/bootstrap-argocd.yml")
    argocd_repo_credentials_task = require_ansible_task_block(
        bootstrap_argocd_text,
        "Register private Git repository credentials when provided",
        "Argo CD bootstrap",
    )
    argocd_application_registration_task = require_ansible_task_block(
        bootstrap_argocd_text,
        "Register platform applications in Argo CD",
        "Argo CD bootstrap",
    )
    if "no_log: true" not in argocd_repo_credentials_task:
        fail("Argo CD private repository credential registration must keep no_log: true")
    if "no_log: true" in argocd_application_registration_task:
        fail("Argo CD platform Application registration must not use no_log: true")
    require_text(
        argocd_application_registration_task,
        "platform_applications_manifest_effective",
        "Argo CD platform Application registration must apply the rendered Application manifest",
    )
    require_text(
        profile_check_text,
        "unresolved placeholders",
        "profile check script must fail unresolved GitOps placeholders",
    )
    for needle in (
        "Public template checkouts are expected to contain placeholders",
        "do not use skip-incomplete output as production proof",
        "platform-production-check",
    ):
        require_text(
            profile_check_text,
            needle,
            f"profile check script must explain production placeholder handling: {needle}",
        )
    require_text(
        profile_check_text,
        "premium-3node",
        "profile check script must support the premium profile",
    )
    for needle in (
        "resolve_profile_entries",
        "profiles",
        "does not include any deployable GitOps application sources",
    ):
        require_text(
            profile_check_text,
            needle,
            f"profile check script must support catalog profiles: {needle}",
        )
    require_text(
        bootstrap_argocd_text,
        "scripts/check_gitops_profile.py",
        "Argo CD bootstrap must use the same profile checker as platform-profile-check",
    )
    require_text(
        bootstrap_argocd_text,
        "platform_profile_catalog_file",
        "Argo CD bootstrap must accept profile files from profiles/",
    )
    require_text(
        bootstrap_argocd_text,
        "scripts/render_deployable_gitops_apps.py",
        "Argo CD bootstrap must render the selected profile into an Application manifest",
    )
    require_text(
        bootstrap_argocd_text,
        "platform_python_from_env",
        "Argo CD bootstrap must honor PYTHON for GitOps profile validation",
    )
    require_text(
        bootstrap_argocd_text,
        "command -v python3",
        "Argo CD bootstrap must discover python3/python when PYTHON is unset",
    )
    require_text(
        bootstrap_argocd_text,
        "install python3 or set PYTHON",
        "Argo CD bootstrap must provide a clear Python override error",
    )
    require_text(
        bootstrap_argocd_text,
        "--required-path gitops/clusters/rke2-main/projects",
        "Argo CD bootstrap skip-incomplete mode must require shared project manifests",
    )
    require_text(
        bootstrap_argocd_text,
        "--profile {{ platform_profile_effective | quote }}",
        "Argo CD bootstrap must quote the selected profile in shell commands",
    )
    if "python3 scripts/" in bootstrap_argocd_text:
        fail("Argo CD bootstrap must not hard-code python3 for repository scripts")
    require_text(
        bootstrap_argocd_text,
        "platform_placeholder_scan.rc | int != 0",
        "Argo CD bootstrap must gate incomplete profiles on profile checker exit code",
    )
    legacy_bootstrap_text = read(root / "scripts/bootstrap/bootstrap-argocd.sh")
    gitops_selection_helper_text = read(root / "scripts/bootstrap/validate-gitops-selection.sh")
    require_text(
        legacy_bootstrap_text,
        "make platform-argocd",
        "legacy bootstrap wrapper must delegate to the maintained Argo CD bootstrap target",
    )
    require_text(
        legacy_bootstrap_text,
        "PLATFORM_APPLY_GITOPS=true",
        "legacy bootstrap wrapper must register applications through the maintained bootstrap path",
    )
    for doc in (
        root / "README.md",
        root / "docs/INSTALLATION.md",
        root / "docs/QUICK_START.md",
        root / "docs/COMPONENT_SWITCHING.md",
        premium_doc,
    ):
        doc_text = read(doc)
        if "root-app-premium-3node.yaml" in doc_text:
            fail(f"{doc.relative_to(root)} references the removed premium root-app file")
    require_text(
        gitops_selection_helper_text,
        "scripts/check_gitops_profile.py",
        "GitOps selection helper must run the strict profile checker in strict mode",
    )
    require_text(
        gitops_selection_helper_text,
        "PYTHON",
        "GitOps selection helper must allow selecting the Python interpreter",
    )
    require_text(
        gitops_selection_helper_text,
        "scripts/render_deployable_gitops_apps.py",
        "GitOps selection helper must render a deployable subset in skip-incomplete mode",
    )
    require_text(
        gitops_selection_helper_text,
        '--profile "${profile}"',
        "GitOps selection helper must render by selected profile instead of a hard-coded app file",
    )
    require_text(
        gitops_selection_helper_text,
        "--required-path gitops/clusters/rke2-main/projects",
        "GitOps selection helper must require shared Argo CD project manifests in skip-incomplete mode",
    )
    if "platform-app-health:" not in makefile_text:
        fail("Makefile is missing platform-app-health target")
    require_text(
        makefile_text,
        "bash scripts/bootstrap/run-platform-app-health.sh",
        "platform-app-health must load its private deployment profile through the health runner",
    )
    platform_app_health_runner_text = read(platform_app_health_runner)
    for needle in (
        "load_env_file",
        "private/platform-apps.rendered.yaml",
        "PLATFORM_APP_HEALTH_MODE",
        "health_mode=bootstrap",
        "PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false",
        "PLATFORM_APP_HEALTH_OPENBAO_READY=false",
        "PLATFORM_APP_HEALTH_REQUIRED_APPS",
        "PLATFORM_APP_HEALTH_NAMESPACES",
        "PLATFORM_APP_HEALTH_STEP_CA_API=false",
        "ansible/playbooks/verify-platform-app-health.yml",
    ):
        require_text(
            platform_app_health_runner_text,
            needle,
            f"platform app health runner must cover {needle}",
        )
    if "platform-ci-health:" not in makefile_text:
        fail("Makefile is missing platform-ci-health target")
    for needle in (
        'PLATFORM_APP_HEALTH_REQUIRED_APPS="traefik woodpecker"',
        "PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false",
        "PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false",
        'PLATFORM_APP_HEALTH_NAMESPACES="argocd traefik woodpecker"',
        'PLATFORM_APP_HEALTH_GUI_APPS="argocd woodpecker"',
        "PLATFORM_APP_HEALTH_ARGOCD_RUNTIME=true",
        "PLATFORM_APP_HEALTH_REGISTRY_API=false",
        "PLATFORM_APP_HEALTH_MONITORING_API=false",
        "PLATFORM_APP_HEALTH_LOKI_API=false",
        "PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false",
        "PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false",
        "ansible/playbooks/verify-platform-app-health.yml",
    ):
        require_text(makefile_text, needle, f"platform-ci-health must pin focused health behavior: {needle}")
    if "PYTHON ?= python3" not in makefile_text:
        fail("Makefile must expose PYTHON override for repository validation targets")
    if "python3 scripts/" in makefile_text:
        fail("Makefile validation targets must use $(PYTHON), not hard-coded python3")
    if "platform-service-path-repair:" not in makefile_text:
        fail("Makefile is missing platform-service-path-repair target")
    if "@$(MAKE) platform-dns-repair" not in makefile_text:
        fail("platform-service-path-repair must delegate to the shared DNS/ClusterIP service-path repair")
    if "platform-service-path-consumers-repair:" not in makefile_text:
        fail("Makefile is missing platform-service-path-consumers-repair target")
    if "ansible/playbooks/repair-platform-service-path-consumers.yml" not in makefile_text:
        fail("platform-service-path-consumers-repair target must invoke the consumer refresh playbook")
    if "@$(MAKE) platform-service-path-consumers-repair" not in makefile_text:
        fail("platform-service-path-repair must refresh service-path consumers after DNS/CNI repair")
    argocd_repair_target = re.search(
        r"(?m)^platform-argocd-service-repair:\n(?P<body>(?:\t[^\n]*\n)+)",
        makefile_text,
    )
    if not argocd_repair_target:
        fail("could not parse platform-argocd-service-repair target body")
    argocd_repair_body = argocd_repair_target.group("body")
    argocd_dns_repair_index = argocd_repair_body.find("@$(MAKE) platform-dns-repair")
    argocd_playbook_index = argocd_repair_body.find("ansible/playbooks/repair-argocd-service-path.yml")
    if not (0 <= argocd_dns_repair_index < argocd_playbook_index):
        fail("platform-argocd-service-repair must preflight shared DNS and API service paths")
    if "platform-woodpecker-repair:" not in makefile_text:
        fail("Makefile is missing platform-woodpecker-repair target")
    for needle in (
        "ansible/playbooks/repair-woodpecker.yml",
        "ansible/playbooks/repair-woodpecker-service-path-nodes.yml",
        "ansible/playbooks/repair-cilium-vxlan-overlay.yml",
        "@$(MAKE) platform-service-path-consumers-repair",
        "@$(MAKE) platform-ci-health",
        "PLATFORM_DNS_FORCE_SERVICE_PATH_REPAIR=true",
        "reason=postgres-endpoint-path-unreachable",
        "cnpg-webhook-service.*(i/o timeout|context deadline exceeded|connection refused)",
        "driver name driver\\.longhorn\\.io not found",
        "AttachVolume\\.Attach failed.*volume .*not ready for workloads",
        "reason=woodpecker-server-replica-volume-not-ready",
        "VolumeBinding.*binding volumes: context deadline exceeded",
        "Woodpecker prerequisite classification:",
        "PLATFORM_LONGHORN_RUNTIME_FORCE_RESTART=true",
        "$(MAKE) platform-longhorn-runtime-repair",
        'longhorn_runtime_rc="$$?"',
        "Focused Longhorn runtime recovery failed; escalating to guarded Longhorn disk bootstrap.",
        "A replacement zero-byte Woodpecker volume is still faulted after runtime recovery",
        "$(MAKE) platform-longhorn-bootstrap",
        "Retrying Woodpecker repair after Longhorn disk bootstrap.",
        "all-node CNI/firewalld recovery",
        "focused Longhorn runtime recovery",
        'repair_rc="$${PIPESTATUS[0]}"',
        'retry_rc="$${PIPESTATUS[0]}"',
        'overlay_retry_rc="$${PIPESTATUS[0]}"',
        "documented Cilium VXLAN remote-ICMP-success/TCP-timeout condition",
        "guarded rolling RKE2 restart",
        "automatic fallback skipped",
    ):
        require_text(makefile_text, needle, f"platform-woodpecker-repair must cover {needle}")
    woodpecker_repair_target = re.search(
        r"(?m)^platform-woodpecker-repair:\n(?P<body>(?:\t[^\n]*\n)+)",
        makefile_text,
    )
    if not woodpecker_repair_target:
        fail("could not parse platform-woodpecker-repair target body")
    woodpecker_repair_body = woodpecker_repair_target.group("body")
    argocd_repair = "@$(MAKE) platform-argocd-service-repair"
    consumer_refresh = "@$(MAKE) platform-service-path-consumers-repair"
    strict_repair = "ansible/playbooks/repair-woodpecker.yml"
    first_consumer_refresh = woodpecker_repair_body.find(consumer_refresh)
    strict_repair_index = woodpecker_repair_body.find(strict_repair)
    argocd_repair_index = woodpecker_repair_body.find(argocd_repair)
    if not (0 <= argocd_repair_index < strict_repair_index):
        fail("platform-woodpecker-repair must repair Argo CD and its shared service paths before Woodpecker")
    if "@$(MAKE) platform-dns-repair" in woodpecker_repair_body:
        fail("platform-woodpecker-repair must not duplicate the Argo CD DNS/API service-path preflight")
    longhorn_runtime_index = woodpecker_repair_body.find("$(MAKE) platform-longhorn-runtime-repair")
    longhorn_bootstrap_index = woodpecker_repair_body.find("$(MAKE) platform-longhorn-bootstrap")
    if not (strict_repair_index < longhorn_runtime_index < longhorn_bootstrap_index < first_consumer_refresh):
        fail(
            "platform-woodpecker-repair must attempt focused Longhorn runtime recovery "
            "before guarded disk bootstrap"
        )
    if woodpecker_repair_body.count(consumer_refresh) != 1:
        fail("platform-woodpecker-repair must refresh service-path consumers once after strict repair")
    if not (0 <= strict_repair_index < first_consumer_refresh):
        fail("platform-woodpecker-repair must run strict Woodpecker repair before service-path consumer refresh")
    overlay_repair_index = woodpecker_repair_body.find("ansible/playbooks/repair-cilium-vxlan-overlay.yml")
    node_restart_index = woodpecker_repair_body.find("ansible/playbooks/repair-woodpecker-service-path-nodes.yml")
    if not (strict_repair_index < overlay_repair_index < node_restart_index < first_consumer_refresh):
        fail("platform-woodpecker-repair must try guarded Cilium overlay recovery before rolling node restart")
    if "platform-longhorn-runtime-repair:" not in makefile_text:
        fail("Makefile is missing platform-longhorn-runtime-repair target")
    if "ansible/playbooks/repair-longhorn-runtime.yml" not in makefile_text:
        fail("platform-longhorn-runtime-repair target must invoke the focused Longhorn runtime playbook")
    longhorn_runtime_repair_text = read(longhorn_runtime_repair_playbook)
    for needle in (
        "missing_csi_nodes",
        "driver.longhorn.io",
        "daemonset/longhorn-csi-plugin",
        "longhorn-csi-registration-timeout",
        "PLATFORM_LONGHORN_RUNTIME_FORCE_RESTART",
        "csi-attacher csi-provisioner csi-resizer csi-snapshotter",
        "len(spec_disks) <= 1",
        "scheduled-data-present",
        'disk_name != "default-disk"',
        "scheduledBackingImage",
        "storageScheduled",
        "action=remove-empty-duplicate",
        "action=fail reason=empty-duplicate-removal-timeout",
        "PLATFORM_LONGHORN_RUNTIME_STALE_REPLICA_REPAIR",
        "Remove stopped Longhorn replicas whose registered disk no longer exists",
        "len(healthy_peers) < 2",
        "safe_degraded_attach_recovery",
        'int(volume_spec.get("numberOfReplicas") or 0) == 2',
        'int(volume_status.get("actualSize") or 0) > 0',
        'volume_status.get("state") == "attaching"',
        'recovery_mode = (\n                    "degraded-attach-recovery"',
        'f"mode={recovery_mode}"',
        "action=remove-invalid-disk-reference",
        "defer_data_bearing_stuck_attachment",
        "action=defer-invalid-disk-reference-to-attachment-repair",
        "PLATFORM_LONGHORN_RUNTIME_EMPTY_UNSCHEDULED_REPLICA_REPAIR",
        "Reset zero-byte unscheduled Longhorn replica placeholders",
        "ReplicaSchedulingFailure",
        "action=remove-empty-unscheduled-placeholder",
        "reason=empty-volume-reschedule-timeout",
        "volumeattachments.storage.k8s.io",
        "action=remove-stale-unattached-record",
        "action=retain-unmanaged-nonready-consumer",
        "action=refresh-controller-managed-consumer",
        "result=workload-recovered",
        "reason=empty-volume-workload-recovery-timeout",
        "PLATFORM_LONGHORN_RUNTIME_EMPTY_FAULTED_CLAIM_REPAIR",
        "Recreate only empty faulted StatefulSet claims without recovery sources",
        "scripts/repair_empty_faulted_longhorn_claims.py",
        "PLATFORM_LONGHORN_RUNTIME_STUCK_ATTACHMENT_REPAIR",
        "PLATFORM_LONGHORN_RUNTIME_STUCK_ATTACHMENT_MIN_AGE",
        "Quarantine unmapped replica blocking data-bearing Longhorn attachment",
        "scripts/repair_stuck_longhorn_attachments.py",
        "action=quarantine-unmapped-replica",
    ):
        require_text(
            longhorn_runtime_repair_text,
            needle,
            f"focused Longhorn runtime repair must cover {needle}",
        )
    empty_faulted_claim_repair_text = read(empty_faulted_longhorn_claim_repair)
    for needle in (
        'status.get("actualSize")',
        'status.get("state") != "detached"',
        'status.get("robustness") != "faulted"',
        "volume-has-backup-history",
        "volume-has-replicas",
        "volume-has-snapshots",
        "volume-has-backups",
        "ordinal-zero-not-automatically-recycled",
        "fewer-than-two-ready-peers",
        "statefulset-pvc-retention-not-retain",
        "statefulset_retains_claims(statefulset)",
        "pause-for-empty-faulted-claim-repair",
        "scale-below-empty-faulted-ordinal",
        "assert_destructive_contract",
        "delete-empty-faulted-statefulset-claim",
        "delete-empty-faulted-retained-pv",
        "delete-empty-faulted-volume",
        "recycle-empty-faulted-statefulset-claim result=healthy",
    ):
        require_text(
            empty_faulted_claim_repair_text,
            needle,
            f"empty faulted Longhorn claim repair must preserve safety gate {needle}",
        )
    stuck_attachment_repair_text = read(stuck_longhorn_attachment_repair)
    for needle in (
        'status.get("state") != "attaching"',
        "volume-has-no-proven-data",
        "volume-has-insufficient-declared-redundancy",
        "volume-not-nonmigratable-rwo",
        "volume-migration-active",
        "engine-not-stopped-unassigned",
        "insufficient-safe-running-replicas",
        "unmapped-stopped-replica-cardinality-not-one",
        "active-unsatisfied-csi-ticket-absent",
        "old-failed-native-attachment-cardinality-not-one",
        "pending-controller-managed-consumer-absent",
        '"failedAt"',
        '"lastFailedAt"',
        "action=quarantine-unmapped-replica",
        "attachment-reconciliation-timeout",
    ):
        require_text(
            stuck_attachment_repair_text,
            needle,
            f"stuck Longhorn attachment repair must preserve safety gate {needle}",
        )
    longhorn_bootstrap_text = read(longhorn_bootstrap_playbook)
    for needle in (
        "same-filesystem-as-default-disk",
        "same-filesystem-id-as-default-disk",
        "action=remove-stale-auto-extra-disk",
        "action=retain-active-extra-disk",
        "action=remove-empty-default-companion",
        "action=request-default-companion-evacuation",
        "action=request-replica-eviction-direct",
        "action=remove-evacuated-default-companion",
        "reason=default-companion-replica-eviction-request-failed",
        "reason=linked-clone-blocks-default-companion-evacuation",
        "reason=default-companion-evacuation-capacity-insufficient",
        "reason=default-companion-evacuation-timeout",
        "reason=active-auto-extra-disk-companion-not-safe-to-remove",
        "companion_path_replica_names",
        "companion_blocking_path_replica_names",
        "companion_uuid or companion_eviction_requested",
        "current_uuid or current_eviction_requested",
        "request_replica_evictions",
        '"evictionRequested": True',
        'get("cloneMode") == "linked-clone"',
        "evictionRequestedReplicaObjects",
        "requiredEvacuationOverProvisioningPercentage",
        "boundVolumeProvisioningPercentage",
        "allVolumeProvisioningPercentage",
        "releasedVolumeCount",
        "PLATFORM_LONGHORN_STORAGE_OVER_PROVISIONING_PERCENTAGE",
        "Reconcile live Longhorn storage overprovisioning setting",
        "settings.longhorn.io/${setting}",
        "reason=longhorn-live-setting-reconciliation-failed",
        "are being syncing and please retry later",
        "defaultCompanionPathReplicaObjects",
        "defaultCompanionBlockingPathReplicaObjects",
        "defaultCompanionEvictionRequested",
        "PLATFORM_LONGHORN_FAILED_RELEASE_REPAIR",
        "Repair a failed Longhorn Helm release with an in-place upgrade",
        "action=repair-failed-longhorn-release result=ok",
        "reason=longhorn-in-place-helm-upgrade-failed",
        "helm upgrade --install platform-longhorn platform-longhorn/longhorn",
        '"job/${controller_job}" --type=merge -p \'{"spec":{"suspend":true}}\'',
        "reason=stale-auto-extra-disk-removal-timeout",
        'disk_name != "default-disk"',
        "scheduledBackingImage",
        "storageScheduled",
        "range(12)",
        "readiness_json=\"$(mktemp)\"",
        "python3 - \"${readiness_json}\" <<'PY'",
    ):
        require_text(
            longhorn_bootstrap_text,
            needle,
            f"Longhorn bootstrap stale extra-disk reconciliation must cover {needle}",
        )
    longhorn_bootstrap_runner_text = read(longhorn_bootstrap_runner)
    for needle in (
        "PLATFORM_LONGHORN_ENV_FILE",
        "private/seed-git.env",
        "private/first-deploy.env",
        'load_env_file "${env_file}" preserve-existing',
        "ansible/playbooks/bootstrap-longhorn.yml",
    ):
        require_text(
            longhorn_bootstrap_runner_text,
            needle,
            f"Longhorn bootstrap private environment runner must cover {needle}",
        )
    require_text(
        makefile_text,
        "@bash scripts/bootstrap/run-longhorn-bootstrap.sh",
        "platform-longhorn-bootstrap must load private deployment settings",
    )
    woodpecker_repair_text = read(woodpecker_repair_playbook)
    for needle in (
        'role}" = "replica"',
        "replica-volume-has-data",
        "replica-volume-not-detached",
        "clear-zero-byte-failed-replica-pvc-finalizer",
        "delete-zero-byte-failed-replica-pv",
        "delete-zero-byte-failed-replica-longhorn-volume",
        "pv_claim_uid",
        "longhorn_cleanup_allowed",
        "ensure_cnpg_webhook_fail_open",
        "action=set-failure-policy-ignore",
        '"path": f"/webhooks/{index}/failurePolicy"',
        "PLATFORM_WOODPECKER_REPAIR_RECYCLE_STALE_POSTGRES_INSTANCE",
        "PLATFORM_WOODPECKER_REPAIR_STALE_POSTGRES_INSTANCE_MIN_AGE",
        "recycle_stale_postgres_instance",
        "Instance Status Extraction Error: HTTP communication issue",
        "action=recycle-stale-unready-cnpg-instance-pod",
        "reason=pvc-retained",
        'delete "pod/${current_primary}"',
        "pvc_contract_safe",
        "PLATFORM_WOODPECKER_REPAIR_RESET_FAILED_SERVER_REPLICA_PVCS",
        "woodpecker-server-[1-9][0-9]*",
        "delete-zero-byte-failed-server-replica-pvc",
        "recycled-zero-byte-failed-server-replica",
        "container-has-started",
        "pvc-uid-changed",
        "argocd.argoproj.io/skip-reconcile=true",
        "pause-for-empty-replica-repair",
        "resume_argocd_reconcile",
        "action=reassert-scale-down",
        "woodpecker-agent-secret",
        "woodpecker-default-agent-secret",
        "woodpecker_default_agent_secret_sync=updated",
        "env_value_from_secrets=",
    ):
        require_text(
            woodpecker_repair_text,
            needle,
            f"Woodpecker failed-replica cleanup must preserve its data-safety gate: {needle}",
        )
    if 'delete "pvc/${current_primary}"' in woodpecker_repair_text:
        fail("stale CNPG primary recovery must never delete its PVC")
    dns_repair_text = read(dns_repair_playbook)
    for needle in (
        "PLATFORM_DNS_FORCE_SERVICE_PATH_REPAIR",
        "platform_dns_force_service_path_repair_effective",
        "platform_dns_service_path_repair_required",
        "platform_dns_force_service_path_repair_effective | bool",
        "Reload and recover firewalld after CNI DNS service path repair",
        "firewalld_reload_action=rolling-restart",
        "firewalld_reload_result=recovered",
        "firewalld_reload_result=completed-after-dbus-timeout",
        "verify_firewalld_runtime",
        "platform_dns_service_path_firewalld_state.rc | default(1)",
        "throttle: 1",
        "cleanup_firewalld_cni_interfaces.py",
        "firewalld_ephemeral_interface_cleanup=changed",
        "firewalld_state_recovery_action=restart-after-interface-cleanup",
        "systemctl reset-failed firewalld",
        "firewalld_runtime_missing=forward:${source}->${destination}",
        "run_kubernetes_api_service_check",
        "Kubernetes API ClusterIP TLS service-path probe",
        "https://kubernetes.default.svc",
        "platform-kubernetes-api-check",
        "PLATFORM_KUBERNETES_API_SERVICE_PATH=true",
        "platform_dns_kubernetes_api_service_path_ok",
        "PLATFORM_DNS_CILIUM_API_BOOTSTRAP",
        "PLATFORM_DNS_CILIUM_API_HOST",
        "PLATFORM_DNS_CILIUM_API_PORT",
        "Configure node-local Kubernetes API service routing before pod service probes",
        "Read existing RKE2 Cilium API bootstrap values",
        "Merge RKE2 Cilium API bootstrap endpoint with existing values",
        "Wait for Cilium API bootstrap endpoint rollout",
        "'k8sServiceHost': platform_dns_cilium_api_host_effective",
        "'k8sServicePort': platform_dns_cilium_api_port_effective | int",
        "patch-endpoint-topology",
        "kubernetes.io/service-name=kubernetes",
        "endpoint.get(\"nodeName\", \"\")",
        "patch-local-service",
        "kubernetes_api_service_routing=local",
    ):
        require_text(dns_repair_text, needle, f"DNS repair must support forced CNI service-path recovery: {needle}")
    cleanup_script_text = read(firewalld_cleanup_script)
    for needle in (
        "TRANSIENT_INTERFACE_RE",
        "STABLE_INTERFACES",
        "os.replace",
        "firewalld_ephemeral_interface_cleanup",
    ):
        require_text(cleanup_script_text, needle, f"firewalld CNI cleanup must preserve its safety contract: {needle}")
    for playbook in (
        root / "ansible/playbooks/prepare-nodes.yml",
        root / "ansible/playbooks/recover-rke2.yml",
        root / "ansible/playbooks/repair-cluster-dns.yml",
        root / "ansible/playbooks/deploy-platform-ingress.yml",
    ):
        playbook_text = read(playbook)
        require_text(
            playbook_text,
            "cleanup_firewalld_cni_interfaces.py",
            f"{playbook.relative_to(root)} must prune stale CNI firewalld bindings",
        )
        require_text(
            playbook_text,
            "for destination in ${pod_cidrs}; do",
            f"{playbook.relative_to(root)} must permit every source/destination pod-CIDR pair",
        )
        require_text(
            playbook_text,
            '-s "${source}" -d "${destination}" -j ACCEPT',
            f"{playbook.relative_to(root)} must install cross-node pod-CIDR forwarding rules",
        )
        require_text(
            playbook_text,
            "8223/udp",
            f"{playbook.relative_to(root)} must permit the supported alternate Cilium VXLAN port",
        )
        require_text(
            playbook_text,
            "cilium_geneve",
            f"{playbook.relative_to(root)} must recognize the stable Cilium Geneve interface",
        )
        if '-s "${source}" -d "${source}" -j ACCEPT' in playbook_text:
            fail(
                f"{playbook.relative_to(root)} must not limit pod forwarding "
                "to same-CIDR traffic"
            )
        if "trusted active CNI interface" in playbook_text:
            fail(f"{playbook.relative_to(root)} must not persist transient CNI interfaces")
    if "platform-monitoring-repair:" not in makefile_text:
        fail("Makefile is missing platform-monitoring-repair target")
    if "platform-monitoring-health:" not in makefile_text:
        fail("Makefile is missing platform-monitoring-health target")
    for needle in (
        "ansible/playbooks/repair-monitoring.yml",
        "@$(MAKE) platform-monitoring-health",
        'PLATFORM_APP_HEALTH_REQUIRED_APPS="traefik monitoring"',
        'PLATFORM_APP_HEALTH_GUI_APPS="grafana prometheus"',
    ):
        require_text(makefile_text, needle, f"monitoring repair targets must cover {needle}")
    monitoring_repair_text = read(root / "ansible/playbooks/repair-monitoring.yml")
    for needle in (
        "Hard refresh and sync monitoring application",
        "Wait for Grafana and Prometheus ready service endpoints",
        "Probe Grafana and Prometheus ClusterIP APIs",
        "kube-prometheus-stack-grafana",
        "kube-prometheus-stack-prometheus",
        "PLATFORM_MONITORING_REPAIR_TIMEOUT",
        "Longhorn capacity and monitoring volumes",
    ):
        require_text(monitoring_repair_text, needle, f"monitoring repair playbook must cover {needle}")
    service_path_consumers_text = read(service_path_consumers_playbook)
    for needle in (
        "Refresh Woodpecker agents after service path repair",
        "Verify Woodpecker gRPC ClusterIP service path before agent refresh from every RKE2 node",
        "Verify Woodpecker gRPC ClusterIP service path after agent refresh from every RKE2 node",
        "Verify Woodpecker gRPC ClusterIP service path from pods pinned to every RKE2 node after agent refresh",
        "Reconcile Woodpecker Argo CD application after consumer refresh",
        "Stop after Woodpecker gRPC node service-path failure",
        "Stop after Woodpecker consumer refresh failure",
        "statefulset/woodpecker-agent",
        "woodpecker-server",
        "/dev/tcp/${svc_ip}/9000",
        "nodeName: \"${node}\"",
        "platform-woodpecker-grpc-check",
        "woodpecker-server.woodpecker.svc.cluster.local:9000",
        "PLATFORM_SERVICE_PATH_CONSUMER_REPAIR_CHECK_IMAGE",
        "PLATFORM_SERVICE_PATH_CONSUMER_REPAIR_TIMEOUT",
        "MAX_ATTEMPTS=3",
        "action=sync-requested attempt=${attempt}",
        "action=sync-finished phase=${phase:-none}",
        "(platform_woodpecker_grpc_node_probe | default({})).rc",
        "(platform_woodpecker_grpc_pod_probe | default({})).rc",
        "(platform_woodpecker_agent_rollout | default({})).rc",
        "(platform_woodpecker_argocd_reconcile_after_consumer_refresh | default({})).rc",
    ):
        require_text(
            service_path_consumers_text,
            needle,
            f"service-path consumer repair playbook must cover {needle}",
        )
    if (
        "register: platform_woodpecker_grpc_node_probe_before\n"
        "      changed_when: false\n"
        "      failed_when: false"
    ) not in service_path_consumers_text:
        fail("initial Woodpecker gRPC service-path probe must be diagnostic-only")
    woodpecker_repair_text = read(woodpecker_repair_playbook)
    for needle in (
        "Repair Woodpecker CI rollout and runtime drift",
        "Refresh and sync Woodpecker Argo CD application",
        "argocd.argoproj.io/refresh=hard",
        "{.spec.source.targetRevision}",
        "revision\\\":\\\"%s",
        "Wait for Woodpecker server and agents after repair",
        "Verify Woodpecker runtime images and service endpoints after repair",
        "woodpeckerci/woodpecker-server",
        "woodpeckerci/woodpecker-agent",
        "woodpecker-image-tag-mismatch",
        "woodpecker-server-has-no-ready-endpoints",
        "PLATFORM_WOODPECKER_REPAIR_EXPECTED_IMAGE_TAG",
        "PLATFORM_WOODPECKER_REPAIR_TIMEOUT",
        "PLATFORM_WOODPECKER_REPAIR_CHECK_IMAGE",
        "PLATFORM_WOODPECKER_REPAIR_AUTO_SERVICE_PATH",
        "PLATFORM_WOODPECKER_REPAIR_SERVICE_PATH_ROLLOUT_TIMEOUT",
        "PLATFORM_WOODPECKER_REPAIR_SERVICE_PATH_CONVERGENCE_TIMEOUT",
        "Verify PostgreSQL service path from the Woodpecker server node",
        "platform-woodpecker-postgres-check-${CHECK_ID}",
        "woodpecker-postgres-check-pod-create-failed",
        "postgres_ready_endpoint_records=",
        'for endpoint in item.get("endpoints", [])',
        "postgres_service_path_probe_attempt=",
        "postgres_service_path_convergence_timeout=",
        "automatic_postgres_service_path_repair=converging",
        "service_path_repair_started=false",
        "service_path_component=${component}",
        "woodpecker-postgres-service-path-components-refreshed",
        "tool=bash-dev-tcp",
        "postgres-service-dns-unreachable",
        "postgres-clusterip-service-path-unreachable",
        "postgres-endpoint-path-unreachable",
        "postgres-service-internal-traffic-policy",
        "Reconcile and verify Woodpecker PostgreSQL role credentials",
        "woodpecker-to-postgres-service-path-unreachable",
        "component_pods_on_node",
        "(rke2-)?kube-proxy-",
        "refresh_cilium_operator",
        "cilium-health status --verbose",
        "cilium-dbg bpf tunnel list",
        "make platform-woodpecker-repair",
    ):
        require_text(
            woodpecker_repair_text,
            needle,
            f"Woodpecker repair playbook must cover {needle}",
        )
    if 'timeout 10 sh -c ":</dev/tcp/' in woodpecker_repair_text:
        fail("Woodpecker PostgreSQL probe must use Bash, not POSIX sh, for /dev/tcp")
    if "{range .items[*].endpoints[*].addresses[*]}" in woodpecker_repair_text:
        fail("Woodpecker PostgreSQL endpoint discovery must flatten nested EndpointSlice arrays structurally")
    woodpecker_service_path_nodes_text = read(woodpecker_service_path_nodes_playbook)
    for needle in (
        "PLATFORM_WOODPECKER_REPAIR_FAILED_NODE_RESTART",
        "PLATFORM_WOODPECKER_REPAIR_FAILED_NODE_RESTART_TIMEOUT",
        "serial: 1",
        "ready_peers=",
        "systemctl --no-block restart rke2-server",
        "ActiveEnterTimestampMonotonic",
        "Wait for restarted Kubernetes node to report Ready",
        "Wait for Cilium on restarted node",
        "Wait for kube-proxy on restarted node",
        "^(?:rke2-)?kube-proxy-",
        "platform-postgres-rw",
        "PVCs, PVs, Longhorn volumes, and database objects are retained",
    ):
        require_text(
            woodpecker_service_path_nodes_text,
            needle,
            f"guarded Woodpecker service-path node recovery must cover {needle}",
        )
    cilium_vxlan_overlay_repair_text = read(cilium_vxlan_overlay_repair_playbook)
    for needle in (
        "PLATFORM_CILIUM_VXLAN_WORKAROUND",
        "PLATFORM_CILIUM_VXLAN_TUNNEL_PORT",
        "PLATFORM_CILIUM_VXLAN_ROLLOUT_TIMEOUT",
        "cilium-health status --verbose",
        "remote-endpoint-icmp-ok-http-timeout",
        "cilium_remote_host_http_timeout=",
        "helmchartconfig.helm.cattle.io/rke2-cilium",
        "platform_cilium_vxlan_values_content_merged",
        "'tunnelProtocol': 'vxlan'",
        "'tunnelPort': platform_cilium_vxlan_tunnel_port_effective | int",
        "| from_yaml",
        "| combine(",
        "'valuesContent': hostvars[rke2_first_server]",
        "configmap/cilium-config",
        "daemonset/cilium",
        "alternate_vxlan_port=",
        "8223",
    ):
        require_text(
            cilium_vxlan_overlay_repair_text,
            needle,
            f"guarded Cilium VXLAN overlay recovery must cover {needle}",
        )
    cilium_stdin_apply = '\n          - apply\n          - -f\n          - "-"\n        stdin:'
    require_text(
        cilium_vxlan_overlay_repair_text,
        cilium_stdin_apply,
        "guarded Cilium VXLAN overlay recovery must pass its generated manifest to kubectl over stdin",
    )
    if "\n          - apply\n          - -f\n          - -\n" in cilium_vxlan_overlay_repair_text:
        fail("guarded Cilium VXLAN overlay recovery must quote the kubectl stdin filename")
    gitignore_text = read(gitignore_file)
    for needle in ("__pycache__/", ".shell-syntax-*/", ".ansible-shell-syntax-*/", ".venv/", ".pytest_cache/", "*.pyc"):
        require_text(gitignore_text, needle, f".gitignore must ignore generated validation/cache artifacts: {needle}")
    validate_project_text = read(root / "scripts/validate_project.py")
    validation_runner_text = read(validation_runner)
    for needle in ("conflict_marker_re", "Git conflict markers found"):
        require_text(
            validate_project_text,
            needle,
            f"project validator must detect unresolved merge conflicts: {needle}",
        )
    require_text(
        validate_project_text,
        "part.startswith('.ansible-shell-syntax-')",
        "project validator must ignore temporary Ansible inline shell syntax directories",
    )
    require_text(
        validate_project_text,
        "scripts/test_python_syntax.py",
        "project validator must require the Python syntax self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_validation_runner.py",
        "project validator must require the validation runner self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_line_endings.py",
        "project validator must require the line ending self-test",
    )
    require_text(
        validate_project_text,
        "scripts/run_validation.py",
        "project validator must require the portable validation runner",
    )
    require_text(
        validate_project_text,
        "scripts/test_shell_syntax.py",
        "project validator must require the shell syntax self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_shell_strict_mode.py",
        "project validator must require the shell strict mode self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_ansible_shell_blocks.py",
        "project validator must require the Ansible inline shell block self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_ansible_curl_timeout_contract.py",
        "project validator must require the Ansible curl timeout contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_ansible_until_contract.py",
        "project validator must require the Ansible until contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_ansible_failed_when_contract.py",
        "project validator must require the Ansible failed_when contract self-test",
    )
    require_text(
        validate_project_text,
        "renovate.json",
        "project validator must require the Renovate helper config",
    )
    require_text(
        validate_project_text,
        "config/sops.age.example.yaml",
        "project validator must require the SOPS age starter policy",
    )
    require_text(
        validate_project_text,
        "docs/BACKUP_RESTORE.md",
        "project validator must require the backup/restore runbook",
    )
    require_text(
        validate_project_text,
        "docs/BUSINESS_CONTINUITY.md",
        "project validator must require the business continuity runbook",
    )
    require_text(
        validate_project_text,
        "docs/SERVICE_CATALOG.md",
        "project validator must require the service catalog",
    )
    require_text(
        validate_project_text,
        "docs/ARCHITECTURE_DECISIONS.md",
        "project validator must require the architecture decision process",
    )
    require_text(
        validate_project_text,
        "docs/adr/0000-template.md",
        "project validator must require the ADR template",
    )
    require_text(
        validate_project_text,
        "docs/OPERATIONS.md",
        "project validator must require the operations runbook",
    )
    require_text(
        validate_project_text,
        "docs/PRODUCTION_READINESS.md",
        "project validator must require the production readiness checklist",
    )
    require_text(
        validate_project_text,
        "docs/PLATFORM_SUPPORT.md",
        "project validator must require the platform support policy",
    )
    require_text(
        validate_project_text,
        "docs/NODE_OS_SUPPORT.md",
        "project validator must require the node OS support policy",
    )
    require_text(
        validate_project_text,
        "docs/INCIDENT_RESPONSE.md",
        "project validator must require the incident response runbook",
    )
    require_text(
        validate_project_text,
        "docs/ACCESS_CONTROL.md",
        "project validator must require the access control runbook",
    )
    require_text(
        validate_project_text,
        "docs/ALERTING.md",
        "project validator must require the alerting runbook",
    )
    require_text(
        validate_project_text,
        "docs/DATA_CLASSIFICATION.md",
        "project validator must require the data classification runbook",
    )
    require_text(
        validate_project_text,
        "docs/THREAT_MODEL.md",
        "project validator must require the threat model",
    )
    require_text(
        validate_project_text,
        "scripts/test_ansible_no_log_contract.py",
        "project validator must require the Ansible no_log contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_policy_examples.py",
        "project validator must require the policy example self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_image_integrity_contract.py",
        "project validator must require the image-integrity contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_pod_security_contract.py",
        "project validator must require the pod-security contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_sops_age_policy.py",
        "project validator must require the SOPS age policy self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_supply_chain_helpers.py",
        "project validator must require the supply-chain helper self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_supply_chain_evidence.py",
        "project validator must require the supply-chain evidence self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_image_inventory_evidence.py",
        "project validator must require exact runtime image reconciliation",
    )
    require_text(
        validate_project_text,
        "scripts/test_capacity_runtime_contract.py",
        "project validator must require the runtime capacity contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_rendered_schema_contract.py",
        "project validator must require the rendered schema contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_backup_restore_runbook.py",
        "project validator must require the backup/restore runbook self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_business_continuity.py",
        "project validator must require the business continuity runbook self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_service_catalog.py",
        "project validator must require the service catalog self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_architecture_decisions.py",
        "project validator must require the architecture decision self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_operations_runbook.py",
        "project validator must require the operations runbook self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_production_readiness_checklist.py",
        "project validator must require the production readiness checklist self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_platform_support.py",
        "project validator must require the platform support policy self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_incident_response_runbook.py",
        "project validator must require the incident response runbook self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_access_control_runbook.py",
        "project validator must require the access control runbook self-test",
    )
    require_text(
        validate_project_text,
        "docs/CAPACITY_PLANNING.md",
        "project validator must require the capacity planning runbook",
    )
    require_text(
        validate_project_text,
        "scripts/test_capacity_planning_runbook.py",
        "project validator must require the capacity planning runbook self-test",
    )
    require_text(
        validate_project_text,
        "docs/COMPLIANCE_AUDIT.md",
        "project validator must require the compliance and audit evidence guide",
    )
    require_text(
        validate_project_text,
        "scripts/test_compliance_audit_runbook.py",
        "project validator must require the compliance and audit evidence self-test",
    )
    require_text(
        validate_project_text,
        "docs/RELEASE_PROMOTION.md",
        "project validator must require the release promotion runbook",
    )
    require_text(
        validate_project_text,
        "scripts/test_release_promotion_runbook.py",
        "project validator must require the release promotion runbook self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_alerting_runbook.py",
        "project validator must require the alerting runbook self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_data_classification.py",
        "project validator must require the data classification runbook self-test",
    )
    for needle in ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "NOTICE"):
        require_text(
            validate_project_text,
            needle,
            f"project validator must require root governance file {needle}",
        )
    for needle in (
        ".github/pull_request_template.md",
        ".github/CODEOWNERS.example",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
    ):
        require_text(
            validate_project_text,
            needle,
            f"project validator must require repository governance template {needle}",
        )
    require_text(
        validate_project_text,
        "scripts/test_security_policy.py",
        "project validator must require the security policy self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_threat_model.py",
        "project validator must require the threat model self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_repository_governance.py",
        "project validator must require the repository governance template self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_codeowners_starter.py",
        "project validator must require the CODEOWNERS starter self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_docs_make_targets.py",
        "project validator must require the documented make target self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_ansible_playbook_references.py",
        "project validator must require the Ansible playbook reference self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_gitops_application_contract.py",
        "project validator must require the GitOps Application contract self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_kustomization_references.py",
        "project validator must require the Kustomization reference self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_gitops_image_pinning.py",
        "project validator must require the GitOps image pinning self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_gitops_helm_chart_pinning.py",
        "project validator must require the GitOps Helm chart pinning self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_gitops_selection_helper.py",
        "project validator must require the GitOps selection helper self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_makefile_help.py",
        "project validator must require the Makefile help coverage self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_validation_surface_parity.py",
        "project validator must require the validation surface parity self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_private_artifact_boundary.py",
        "project validator must require the private artifact boundary self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_ci_reference_pinning.py",
        "project validator must require the CI reference pinning self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_markdown_links.py",
        "project validator must require the Markdown link self-test",
    )
    require_text(
        validate_project_text,
        "scripts/test_example_templates.py",
        "project validator must require the example template self-test",
    )
    for needle in (
        ".gitattributes",
        ".gitattributes text eol=lf",
        ".gitignore text eol=lf",
        ".helmignore text eol=lf",
        ".gitkeep text eol=lf",
        "LICENSE text eol=lf",
        "Makefile text eol=lf",
        "Dockerfile text eol=lf",
        "*.env text eol=lf",
        "*.env.example text eol=lf",
        "*.json text eol=lf",
        "*.lock text eol=lf",
        "*.gotmpl text eol=lf",
        "*.tpl text eol=lf",
        "*.txt text eol=lf",
        "*.sh text eol=lf",
        "*.py text eol=lf",
        "*.yml text eol=lf",
        "*.yaml text eol=lf",
        "*.ini text eol=lf",
        "*.cfg text eol=lf",
        "*.md text eol=lf",
        "Missing required git attribute",
    ):
        require_text(
            validate_project_text,
            needle,
            f"project validator must enforce Git attributes: {needle}",
        )
    for needle in (
        "part.startswith('.shell-syntax-')",
        "'__pycache__'",
        "'private'",
        "'rendered'",
        "'secrets'",
        "'.venv'",
        "'.pytest_cache'",
        "should_skip",
    ):
        require_text(validate_project_text, needle, f"project validator must ignore generated validation/cache artifacts: {needle}")
    if "platform-profile-check:" not in makefile_text:
        fail("Makefile is missing platform-profile-check target")
    if "scripts/check_gitops_profile.py" not in makefile_text:
        fail("platform-profile-check target does not invoke the profile checker")
    if "scripts/run_validation.py" not in makefile_text:
        fail("validate target must invoke the portable validation runner")
    if "SHELL := bash" not in makefile_text:
        fail("Makefile must use bash as the recipe shell without embedding shell arguments in SHELL")
    if "security-scan:" not in makefile_text:
        fail("Makefile is missing security-scan target")
    if "bash scripts/security-scan.sh" not in makefile_text:
        fail("security-scan target must invoke scripts/security-scan.sh")
    if "supply-chain-posture:" not in makefile_text:
        fail("Makefile is missing supply-chain-posture target")
    if "bash scripts/supply-chain-posture.sh" not in makefile_text:
        fail("supply-chain-posture target must invoke scripts/supply-chain-posture.sh")
    security_scan_text = read(security_scan_script)
    for needle in (
        "set -euo pipefail",
        "require_tool trivy",
        "require_tool gitleaks",
        "require_tool semgrep",
        "TRIVY_SEVERITY",
        "TRIVY_EXIT_CODE",
        "SEMGREP_CONFIG",
        "${ROOT}/.semgrep.yml",
        "trivy_args",
        "gitleaks_args",
        "semgrep_args",
    ):
        require_text(security_scan_text, needle, f"security scan wrapper must include {needle}")
    supply_chain_posture_text = read(supply_chain_posture_script)
    for needle in (
        "set -euo pipefail",
        "require_tool syft",
        "spdx-json",
        "scorecard --local",
        "SCORECARD_REPO",
        "COSIGN_IMAGE",
        "COSIGN_PUBLIC_KEY",
        "cosign verify",
        "rendered/supply-chain",
    ):
        require_text(supply_chain_posture_text, needle, f"supply-chain posture wrapper must include {needle}")
    for config_path, required_needles in (
        (gitleaks_config, ("[extend]", "useDefault = true", "allowlists")),
        (semgrep_config, ("rules:", "shell-curl-pipe-shell", "kubernetes-latest-image-tag", "kubernetes-privileged-container")),
        (
            trivy_config,
            (
                "scan:",
                "scanners:",
                "vuln",
                "secret",
                "misconfig",
                "skip-dirs:",
                '"**/charts"',
                "scripts/fixtures",
                "skip-files:",
                '"**/*-patch.yaml"',
                ".clusterfuzzlite/Dockerfile",
                "vulnerability:",
                "ignore-unfixed: true",
            ),
        ),
    ):
        config_text = read(config_path)
        for needle in required_needles:
            require_text(config_text, needle, f"{config_path.relative_to(root)} must include {needle}")
    for needle in (
        "VALIDATION_SCRIPTS",
        "PYTHONDONTWRITEBYTECODE",
        "PLATFORM_RUN_NO_SECRETS",
        "--skip-no-secrets",
        "subprocess.run",
    ):
        require_text(
            validation_runner_text,
            needle,
            f"validation runner must cover {needle}",
        )
    for script_name in (
        "scripts/validate_project.py",
        "scripts/test_python_syntax.py",
        "scripts/test_validation_runner.py",
        "scripts/test_line_endings.py",
        "scripts/test_profile_checker.py",
        "scripts/test_deployable_renderer.py",
        "scripts/test_gitops_selection_helper.py",
        "scripts/test_private_values_renderer.py",
        "scripts/test_platform_secret_contract.py",
        "scripts/test_policy_examples.py",
        "scripts/test_image_integrity_contract.py",
        "scripts/test_pod_security_contract.py",
        "scripts/test_sops_age_policy.py",
        "scripts/test_supply_chain_helpers.py",
        "scripts/test_supply_chain_evidence.py",
        "scripts/test_image_inventory_evidence.py",
        "scripts/test_github_governance.py",
        "scripts/test_github_governance_configuration.py",
        "scripts/test_capacity_runtime_contract.py",
        "scripts/test_rendered_schema_contract.py",
        "scripts/test_backup_restore_runbook.py",
        "scripts/test_business_continuity.py",
        "scripts/test_service_catalog.py",
        "scripts/test_architecture_decisions.py",
        "scripts/test_operations_runbook.py",
        "scripts/test_production_readiness_checklist.py",
        "scripts/test_platform_support.py",
        "scripts/test_incident_response_runbook.py",
        "scripts/test_access_control_runbook.py",
        "scripts/test_capacity_planning_runbook.py",
        "scripts/test_compliance_audit_runbook.py",
        "scripts/test_release_promotion_runbook.py",
        "scripts/test_alerting_runbook.py",
        "scripts/test_data_classification.py",
        "scripts/test_security_policy.py",
        "scripts/test_threat_model.py",
        "scripts/test_repository_governance.py",
        "scripts/test_codeowners_starter.py",
        "scripts/test_no_secrets.py",
        "scripts/test_private_artifact_boundary.py",
        "scripts/test_ci_reference_pinning.py",
        "scripts/test_shell_syntax.py",
        "scripts/test_shell_strict_mode.py",
        "scripts/test_ansible_shell_blocks.py",
        "scripts/test_ansible_curl_timeout_contract.py",
        "scripts/test_ansible_until_contract.py",
        "scripts/test_ansible_failed_when_contract.py",
        "scripts/test_ansible_no_log_contract.py",
        "scripts/test_docs_make_targets.py",
        "scripts/test_markdown_links.py",
        "scripts/test_example_templates.py",
        "scripts/test_ansible_playbook_references.py",
        "scripts/test_gitops_application_contract.py",
        "scripts/test_kustomization_references.py",
        "scripts/test_gitops_helm_chart_pinning.py",
        "scripts/test_gitops_image_pinning.py",
        "scripts/test_makefile_help.py",
        "scripts/test_validation_surface_parity.py",
        "scripts/validate_platform_contract.py",
        "scripts/validate_no_secrets.py",
    ):
        require_text(
            validation_runner_text,
            script_name,
            f"validation runner must run {script_name}",
        )
    if "Render first-deploy private values for platform apps" not in makefile_text:
        fail("Makefile help must describe the full private values renderer scope")
    for bootstrap_script in (
        "scripts/bootstrap/private-first-deploy.sh",
        "scripts/bootstrap/seed-first-deploy.sh",
        "scripts/bootstrap/sync-seed-git.sh",
    ):
        bootstrap_script_text = read(root / bootstrap_script)
        require_text(
            bootstrap_script_text,
            "scripts/bootstrap/validate-gitops-selection.sh",
            f"{bootstrap_script} must run the selected GitOps profile validation before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "PLATFORM_RUN_PROFILE_CHECK",
            f"{bootstrap_script} must expose a guard for the selected GitOps profile check",
        )
        require_text(
            bootstrap_script_text,
            "PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES",
            f"{bootstrap_script} must explicitly control private hostname handling in the safety scan",
        )
        require_text(
            bootstrap_script_text,
            "resolve_python()",
            f"{bootstrap_script} must discover Python or explain how to set PYTHON",
        )
        require_text(
            bootstrap_script_text,
            'PYTHON_BIN="$(resolve_python)"',
            f"{bootstrap_script} must use the discovered Python interpreter for validation and rendering",
        )
        require_text(
            bootstrap_script_text,
            "install python3 or set PYTHON",
            f"{bootstrap_script} must provide a clear Python installation/override error",
        )
        require_text(
            bootstrap_script_text,
            'PYTHON="${PYTHON_BIN}" bash scripts/bootstrap/validate-gitops-selection.sh .',
            f"{bootstrap_script} must pass the selected Python interpreter to the GitOps selection helper",
        )
        require_text(
            bootstrap_script_text,
            "scripts/run_validation.py",
            f"{bootstrap_script} must run the shared validation runner before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "PLATFORM_RUN_NO_SECRETS",
            f"{bootstrap_script} must pass no-secrets control to the shared validation runner",
        )
    if 'PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-true}"' not in read(root / "scripts/bootstrap/private-first-deploy.sh"):
        fail("private-first-deploy must allow private hostnames by default while keeping secret scanning enabled")
    if 'PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-true}"' not in read(root / "scripts/bootstrap/seed-first-deploy.sh"):
        fail("seed-first-deploy must allow private hostnames by default while keeping secret scanning enabled")
    if 'PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-false}"' not in read(root / "scripts/bootstrap/sync-seed-git.sh"):
        fail("seed sync must keep public-template hostname leakage detection enabled by default")
    seed_sync_text = read(root / "scripts/bootstrap/sync-seed-git.sh")
    if 'load_env_file "${env_file}" preserve-existing' not in seed_sync_text:
        fail("seed sync must let explicit environment overrides win over the private deployment environment file")
    if 'PLATFORM_SEED_SYNC_PUSH_ORIGIN="${PLATFORM_SEED_SYNC_PUSH_ORIGIN:-false}"' not in seed_sync_text:
        fail("seed sync must keep source remote push opt-in by default")
    if "Set PLATFORM_SEED_SYNC_PUSH_ORIGIN=true" not in seed_sync_text:
        fail("seed sync must print how to opt into source remote pushes")
    if "skipping source remote pull" not in seed_sync_text:
        fail("seed sync must skip missing source remotes during optional pull")
    if "Optionally pull source remote, then mirror the current branch to temporary seed Git" not in makefile_text:
        fail("Makefile help must describe seed sync as source-push opt-in")
    for ci_file in ci_validation_files:
        ci_text = read(ci_file)
        for script_name in (
            "scripts/validate_project.py",
            "scripts/test_python_syntax.py",
            "scripts/test_validation_runner.py",
            "scripts/test_line_endings.py",
            "scripts/test_profile_checker.py",
            "scripts/test_deployable_renderer.py",
            "scripts/test_gitops_selection_helper.py",
            "scripts/test_private_values_renderer.py",
            "scripts/test_platform_secret_contract.py",
            "scripts/test_policy_examples.py",
            "scripts/test_image_integrity_contract.py",
            "scripts/test_pod_security_contract.py",
            "scripts/test_sops_age_policy.py",
            "scripts/test_supply_chain_helpers.py",
            "scripts/test_supply_chain_evidence.py",
            "scripts/test_image_inventory_evidence.py",
            "scripts/test_capacity_runtime_contract.py",
            "scripts/test_rendered_schema_contract.py",
            "scripts/test_backup_restore_runbook.py",
            "scripts/test_business_continuity.py",
            "scripts/test_service_catalog.py",
            "scripts/test_architecture_decisions.py",
            "scripts/test_operations_runbook.py",
            "scripts/test_production_readiness_checklist.py",
            "scripts/test_platform_support.py",
            "scripts/test_incident_response_runbook.py",
            "scripts/test_access_control_runbook.py",
            "scripts/test_capacity_planning_runbook.py",
            "scripts/test_compliance_audit_runbook.py",
            "scripts/test_release_promotion_runbook.py",
            "scripts/test_alerting_runbook.py",
            "scripts/test_data_classification.py",
            "scripts/test_security_policy.py",
            "scripts/test_threat_model.py",
            "scripts/test_repository_governance.py",
            "scripts/test_codeowners_starter.py",
            "scripts/test_no_secrets.py",
            "scripts/test_private_artifact_boundary.py",
            "scripts/test_ci_reference_pinning.py",
            "scripts/test_shell_syntax.py",
            "scripts/test_shell_strict_mode.py",
            "scripts/test_ansible_shell_blocks.py",
            "scripts/test_ansible_curl_timeout_contract.py",
            "scripts/test_ansible_until_contract.py",
            "scripts/test_ansible_failed_when_contract.py",
            "scripts/test_ansible_no_log_contract.py",
            "scripts/test_docs_make_targets.py",
            "scripts/test_markdown_links.py",
            "scripts/test_example_templates.py",
            "scripts/test_ansible_playbook_references.py",
            "scripts/test_gitops_application_contract.py",
            "scripts/test_kustomization_references.py",
            "scripts/test_gitops_helm_chart_pinning.py",
            "scripts/test_gitops_image_pinning.py",
            "scripts/test_makefile_help.py",
            "scripts/test_validation_surface_parity.py",
            "scripts/validate_platform_contract.py",
            "scripts/validate_no_secrets.py",
        ):
            require_text(
                ci_text,
                script_name,
                f"{ci_file.relative_to(root)} must run {script_name}",
            )
    for needle in (
        "ast.parse",
        "python_files",
        "Python syntax validation failed",
        "__pycache__",
        "part.startswith(\".shell-syntax-\")",
    ):
        require_text(
            read(python_syntax_test),
            needle,
            f"Python syntax self-test must cover {needle}",
        )
    validation_runner_test_text = read(validation_runner_test)
    for needle in (
        "VALIDATION_SCRIPTS",
        "selected_scripts",
        "skip_no_secrets",
        "env_flag",
        "PYTHONDONTWRITEBYTECODE",
        "subprocess.run",
        "sys.executable",
        "test_main_list_mode",
        "test_main_stops_on_first_failure",
        "--list mode must not execute validation scripts",
        "first failing validation script exit code",
        "scripts/test_validation_runner.py",
        "scripts/test_ansible_shell_blocks.py",
        "scripts/test_ansible_curl_timeout_contract.py",
        "scripts/test_ansible_until_contract.py",
        "scripts/test_ansible_failed_when_contract.py",
        "scripts/test_ansible_no_log_contract.py",
        "scripts/test_policy_examples.py",
        "scripts/test_image_integrity_contract.py",
        "scripts/test_pod_security_contract.py",
        "scripts/test_sops_age_policy.py",
        "scripts/test_supply_chain_helpers.py",
        "scripts/test_supply_chain_evidence.py",
        "scripts/test_image_inventory_evidence.py",
        "scripts/test_capacity_runtime_contract.py",
        "scripts/test_rendered_schema_contract.py",
        "scripts/test_backup_restore_runbook.py",
        "scripts/test_business_continuity.py",
        "scripts/test_service_catalog.py",
        "scripts/test_architecture_decisions.py",
        "scripts/test_operations_runbook.py",
        "scripts/test_production_readiness_checklist.py",
        "scripts/test_platform_support.py",
        "scripts/test_incident_response_runbook.py",
        "scripts/test_access_control_runbook.py",
        "scripts/test_capacity_planning_runbook.py",
        "scripts/test_compliance_audit_runbook.py",
        "scripts/test_release_promotion_runbook.py",
        "scripts/test_alerting_runbook.py",
        "scripts/test_data_classification.py",
        "scripts/test_security_policy.py",
        "scripts/test_threat_model.py",
        "scripts/test_repository_governance.py",
        "scripts/test_codeowners_starter.py",
        "scripts/run_validation.py",
        "scripts/validate_no_secrets.py",
    ):
        require_text(
            validation_runner_test_text,
            needle,
            f"validation runner self-test must cover {needle}",
        )
    policy_examples_test_text = read(policy_examples_test)
    for needle in (
        "Validate optional policy examples stay documented and safe by default",
        "EXPECTED_FILES",
        "kyverno/no-plaintext-secrets.example.yaml",
        "kyverno/require-workload-baseline.example.yaml",
        "kyverno/verify-signed-images.example.yaml",
        "network/default-deny.example.yaml",
        "network/allow-platform-dns-and-ingress.example.yaml",
        "validationFailureAction: Audit",
        "ImageValidatingPolicy",
        "matchImageReferences:",
        "validationActions:",
        "validationFailureAction: Enforce",
        "validationActions:\\n    - Deny",
        "platform.gitops/secret-source",
        "replace placeholders such as `<NAMESPACE>`",
        "unexpected policy example(s) without contract coverage",
        "Policy example validation passed",
    ):
        require_text(
            policy_examples_test_text,
            needle,
            f"policy example self-test must cover {needle}",
        )
    readme_text = read(root / "README.md")
    release_guide_text = read(root / "docs/RELEASE_GUIDE.md")
    sops_age_policy_test_text = read(sops_age_policy_test)
    for needle in (
        "Validate the SOPS + age starter policy is safe and useful",
        "config/sops.age.example.yaml",
        "AGE-SECRET-KEY-",
        "age1REPLACE_WITH_PUBLIC_AGE_RECIPIENT",
        "path_regex: ^(private|secrets|rendered)/.*",
        "path_regex: ^(config|inventory)/.*",
        "path_regex: ^gitops/.*/.*(secret|credential|datasource).*",
        "encrypted_regex:",
        "private age key must stay outside Git",
        "SOPS age policy validation passed",
    ):
        require_text(
            sops_age_policy_test_text,
            needle,
            f"SOPS age policy self-test must cover {needle}",
        )
    sops_age_policy_text = read(root / "config/sops.age.example.yaml")
    for needle in (
        "creation_rules:",
        "age: age1REPLACE_WITH_PUBLIC_AGE_RECIPIENT",
        "path_regex: ^(private|secrets|rendered)/.*",
        "path_regex: ^(config|inventory)/.*",
        "path_regex: ^gitops/.*/.*(secret|credential|datasource).*",
        "encrypted_regex:",
        "data",
        "stringData",
        "password",
        "token",
        "secretKey",
        "accessKey",
        "secretAccessKey",
        "clientSecret",
        "credentials",
        "datasource",
    ):
        require_text(
            sops_age_policy_text,
            needle,
            f"SOPS age starter policy must include {needle}",
        )
    if "AGE-SECRET-KEY-" in sops_age_policy_text:
        fail("SOPS age starter policy must not contain an age private key")
    secrets_privacy_text = read(root / "docs/SECRETS_AND_PRIVACY.md")
    for needle in (
        "config/sops.age.example.yaml",
        "age-keygen",
        "age1REPLACE_WITH_PUBLIC_AGE_RECIPIENT",
        "private age key must stay outside Git",
        "Review the rules with your security team",
    ):
        require_text(secrets_privacy_text, needle, f"secrets/privacy docs must document SOPS age policy: {needle}")
    for needle in (
        "config/sops.age.example.yaml",
        "SOPS + age starter policy",
        "keep age private keys outside Git",
    ):
        require_text(readme_text, needle, f"README must document SOPS age policy: {needle}")
    require_text(
        release_guide_text,
        "SOPS/age recipient policy",
        "release guide must document SOPS age recipient policy review",
    )
    supply_chain_helpers_test_text = read(supply_chain_helpers_test)
    for needle in (
        "Validate supply-chain helper examples for Renovate and Cosign/Kyverno",
        "renovate.json",
        "https://docs.renovatebot.com/renovate-schema.json",
        "config:recommended",
        "pinDigests",
        "dependencyDashboardApproval",
        "verify-signed-images.example.yaml",
        "ImageValidatingPolicy",
        "matchImageReferences:",
        "validationActions:",
        "<COSIGN_PUBLIC_KEY>",
        "Supply-chain helper validation passed",
    ):
        require_text(
            supply_chain_helpers_test_text,
            needle,
            f"supply-chain helper self-test must cover {needle}",
        )
    supply_chain_evidence_test_text = read(supply_chain_evidence_test)
    for needle in (
        "Supply-chain evidence validator self-test passed.",
        "below-threshold Scorecard",
        "tag-only Cosign image",
        "empty SBOM",
    ):
        require_text(
            supply_chain_evidence_test_text,
            needle,
            f"supply-chain evidence self-test must cover {needle}",
        )
    supply_chain_evidence_validator_text = read(supply_chain_evidence_validator)
    for needle in (
        "validate_sbom",
        "validate_scorecard",
        "validate_signature_report",
        "strict evidence requires an OpenSSF Scorecard report",
        "strict evidence requires a Cosign signature report",
    ):
        require_text(
            supply_chain_evidence_validator_text,
            needle,
            f"supply-chain evidence validator must include {needle}",
        )
    backup_restore_runbook_test_text = read(backup_restore_runbook_test)
    for needle in (
        "Validate the production backup and restore drill runbook",
        "docs/BACKUP_RESTORE.md",
        "RPO/RTO Targets",
        "Evidence Before Production",
        "Restore Drill Scope",
        "Velero BackupStorageLocation",
        "CloudNativePG backup plus WAL archive",
        "Longhorn backup target",
        "git clone",
        "docker pull",
        "DRILL_ID",
        "Backup and restore runbook validation passed",
    ):
        require_text(
            backup_restore_runbook_test_text,
            needle,
            f"backup/restore runbook self-test must cover {needle}",
        )
    backup_restore_text = read(backup_restore_doc)
    for needle in (
        "## Required Backups",
        "## Off-Cluster Requirement",
        "## RPO/RTO Targets",
        "## Evidence Before Production",
        "## Restore Drill Scope",
        "## Drill Cadence",
        "make platform-production-check",
        "make platform-app-health",
        "Etcd snapshots",
        "Velero BackupStorageLocation",
        "CloudNativePG backup plus WAL archive",
        "Longhorn backup target",
        "scratch PVC",
        "git clone",
        "git fsck",
        "docker pull",
        "crane digest",
        "SOPS age private key material",
        "DRILL_ID",
    ):
        require_text(
            backup_restore_text,
            needle,
            f"backup/restore runbook must include {needle}",
        )
    for doc, label in (
        (readme_doc, "README"),
        (quick_start_doc, "quick start"),
        (release_guide_doc, "release guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "private deployment guide"),
        (root / "docs/ARCHITECTURE.md", "architecture guide"),
    ):
        require_text(
            read(doc),
            "docs/BACKUP_RESTORE.md",
            f"{label} must link the backup/restore runbook",
        )
    business_continuity_test_text = read(business_continuity_test)
    for needle in (
        "Validate the public-safe business continuity and disaster recovery runbook",
        "docs/BUSINESS_CONTINUITY.md",
        "# Business Continuity and Disaster Recovery",
        "Minimum Viable Platform",
        "Dependency Recovery Order",
        "Scenario Matrix",
        "RPO and RTO Model",
        "Failover and Failback",
        "Continuity Exercises",
        "Continuity Evidence",
        "Production Gate",
        "Business continuity runbook validation passed",
    ):
        require_text(
            business_continuity_test_text,
            needle,
            f"business continuity runbook self-test must cover {needle}",
        )
    business_continuity_text = read(business_continuity_doc)
    for needle in (
        "# Business Continuity and Disaster Recovery",
        "public-safe business continuity and disaster recovery model",
        "## Continuity Principles",
        "## Scope",
        "## Minimum Viable Platform",
        "RKE2 API, etcd quorum",
        "CNI, CoreDNS, and kube-proxy service path",
        "GitOps source of truth",
        "Velero BackupStorageLocation",
        "Longhorn or alternate storage",
        "CloudNativePG",
        "Traefik or alternate ingress",
        "Forgejo/Gitea/GitLab",
        "Harbor",
        "Woodpecker",
        "Prometheus, Grafana, Loki, Alertmanager",
        "cert-manager Certificate readiness",
        "trust-manager Bundle readiness",
        "step-ca health",
        "## Dependency Recovery Order",
        "## Scenario Matrix",
        "Single node loss",
        "Control-plane quorum risk",
        "Storage data loss",
        "GitOps source unavailable",
        "Registry unavailable",
        "Backup target unavailable",
        "Ingress/VIP failure",
        "PKI or trust failure",
        "Region or site loss",
        "## RPO and RTO Model",
        "Maximum accepted time",
        "Maximum accepted data loss",
        "rollback, forward recovery, or restore-from-backup",
        "## Failover and Failback",
        "Failover is allowed only",
        "Failback should not begin",
        "## Continuity Exercises",
        "Quarterly for restore and minimum viable platform tabletop",
        "## Continuity Evidence",
        "Open continuity exceptions",
        "accepting authority",
        "## Production Gate",
        "Do not commit private continuity records",
    ):
        require_text(
            business_continuity_text,
            needle,
            f"business continuity runbook must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/BUSINESS_CONTINUITY.md", "README"),
        (root / "docs/README.md", "BUSINESS_CONTINUITY.md", "documentation index"),
        (root / "docs/ARCHITECTURE.md", "docs/BUSINESS_CONTINUITY.md", "architecture guide"),
        (backup_restore_doc, "docs/BUSINESS_CONTINUITY.md", "backup/restore runbook"),
        (operations_doc, "docs/BUSINESS_CONTINUITY.md", "operations runbook"),
        (production_readiness_doc, "docs/BUSINESS_CONTINUITY.md", "production readiness checklist"),
        (compliance_audit_doc, "docs/BUSINESS_CONTINUITY.md", "compliance and audit guide"),
        (release_guide_doc, "docs/BUSINESS_CONTINUITY.md", "release guide"),
        (release_promotion_doc, "docs/BUSINESS_CONTINUITY.md", "release promotion runbook"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/BUSINESS_CONTINUITY.md", "private deployment guide"),
        (security_policy_doc, "docs/BUSINESS_CONTINUITY.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention business continuity",
        )
    service_catalog_test_text = read(service_catalog_test)
    for needle in (
        "Validate the public-safe service catalog and ownership model",
        "docs/SERVICE_CATALOG.md",
        "# Service Catalog and Ownership",
        "Catalog Principles",
        "Required Fields",
        "Platform Service Matrix",
        "Dependency Map",
        "Ownership Review",
        "Production Acceptance",
        "Evidence",
        "Service catalog validation passed",
    ):
        require_text(
            service_catalog_test_text,
            needle,
            f"service catalog self-test must cover {needle}",
        )
    service_catalog_text = read(service_catalog_doc)
    for needle in (
        "# Service Catalog and Ownership",
        "public-safe service catalog model",
        "## Catalog Principles",
        "## Required Fields",
        "Service name",
        "Criticality",
        "Owner",
        "Backup owner",
        "Support tier",
        "Data classification",
        "SLO/SLA target",
        "RPO/RTO target",
        "Backup and restore",
        "Access model",
        "Observability",
        "Capacity signals",
        "Release model",
        "Continuity role",
        "## Platform Service Matrix",
        "RKE2 API and etcd",
        "Cilium, CoreDNS, and kube-proxy path",
        "kube-vip and MetalLB",
        "Traefik or alternate ingress",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI or selected runner",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Prometheus, Grafana, and Loki",
        "cert-manager and trust-manager",
        "step-ca",
        "## Dependency Map",
        "## Ownership Review",
        "Monthly for P0 and P1 services",
        "## Production Acceptance",
        "Catalog entry is complete and owner-approved",
        "## Evidence",
        "Do not commit private service catalogs",
    ):
        require_text(
            service_catalog_text,
            needle,
            f"service catalog must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/SERVICE_CATALOG.md", "README"),
        (root / "docs/README.md", "SERVICE_CATALOG.md", "documentation index"),
        (root / "docs/ARCHITECTURE.md", "docs/SERVICE_CATALOG.md", "architecture guide"),
        (operations_doc, "docs/SERVICE_CATALOG.md", "operations runbook"),
        (production_readiness_doc, "docs/SERVICE_CATALOG.md", "production readiness checklist"),
        (business_continuity_doc, "docs/SERVICE_CATALOG.md", "business continuity runbook"),
        (compliance_audit_doc, "docs/SERVICE_CATALOG.md", "compliance and audit guide"),
        (alerting_doc, "docs/SERVICE_CATALOG.md", "alerting runbook"),
        (access_control_doc, "docs/SERVICE_CATALOG.md", "access control runbook"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/SERVICE_CATALOG.md", "private deployment guide"),
        (security_policy_doc, "docs/SERVICE_CATALOG.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention service catalog",
        )
    architecture_decisions_test_text = read(architecture_decisions_test)
    for needle in (
        "Validate the public-safe architecture decision record process",
        "docs/ARCHITECTURE_DECISIONS.md",
        "docs/adr/0000-template.md",
        "# Architecture Decision Records",
        "When to Write an ADR",
        "ADR Lifecycle",
        "Required ADR Fields",
        "Decision Review Gates",
        "Public-Safe Guidance",
        "Architecture decision record validation passed",
    ):
        require_text(
            architecture_decisions_test_text,
            needle,
            f"architecture decision self-test must cover {needle}",
        )
    architecture_decisions_text = read(architecture_decisions_doc)
    for needle in (
        "# Architecture Decision Records",
        "public-safe architecture decision record process",
        "## Principles",
        "## When to Write an ADR",
        "## ADR Lifecycle",
        "Proposed",
        "Accepted",
        "Superseded",
        "Deprecated",
        "## Required ADR Fields",
        "Title",
        "Status",
        "Owner",
        "Review date",
        "Context",
        "Decision drivers",
        "Options considered",
        "Consequences",
        "Validation",
        "Rollback or exit plan",
        "docs/adr/0000-template.md",
        "## Decision Review Gates",
        "## Public-Safe Guidance",
        "## Evidence",
        "Do not commit private ADRs",
    ):
        require_text(
            architecture_decisions_text,
            needle,
            f"architecture decision process must include {needle}",
        )
    architecture_decision_template_text = read(architecture_decision_template)
    for needle in (
        "# ADR 0000: <Decision Title>",
        "Status: Proposed",
        "Owner: <PRIVATE_OWNER_OR_TEAM>",
        "## Context",
        "## Decision Drivers",
        "## Options Considered",
        "## Decision",
        "## Consequences",
        "## Validation",
        "## Rollback or Exit Plan",
        "## Related Records",
    ):
        require_text(
            architecture_decision_template_text,
            needle,
            f"ADR template must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/ARCHITECTURE_DECISIONS.md", "README"),
        (root / "docs/README.md", "ARCHITECTURE_DECISIONS.md", "documentation index"),
        (root / "docs/ARCHITECTURE.md", "docs/ARCHITECTURE_DECISIONS.md", "architecture guide"),
        (operations_doc, "docs/ARCHITECTURE_DECISIONS.md", "operations runbook"),
        (production_readiness_doc, "docs/ARCHITECTURE_DECISIONS.md", "production readiness checklist"),
        (release_promotion_doc, "docs/ARCHITECTURE_DECISIONS.md", "release promotion runbook"),
        (root / "docs/THREAT_MODEL.md", "docs/ARCHITECTURE_DECISIONS.md", "threat model"),
        (compliance_audit_doc, "docs/ARCHITECTURE_DECISIONS.md", "compliance and audit guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/ARCHITECTURE_DECISIONS.md", "private deployment guide"),
        (security_policy_doc, "docs/ARCHITECTURE_DECISIONS.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention architecture decisions",
        )
    operations_runbook_test_text = read(operations_runbook_test)
    for needle in (
        "Validate the production day-2 operations runbook",
        "docs/OPERATIONS.md",
        "Operating Principles",
        "Change Management",
        "Maintenance Windows",
        "Upgrade Procedure",
        "Access Control",
        "Break-Glass Access",
        "Incident Response",
        "Credential Rotation",
        "Capacity and Retention",
        "Operations runbook validation passed",
    ):
        require_text(
            operations_runbook_test_text,
            needle,
            f"operations runbook self-test must cover {needle}",
        )
    operations_text = read(operations_doc)
    for needle in (
        "## Operating Principles",
        "## Ownership",
        "## Routine Checks",
        "## Change Management",
        "## Maintenance Windows",
        "## Upgrade Procedure",
        "## Access Control",
        "## Break-Glass Access",
        "## Incident Response",
        "## Drift Management",
        "## Credential Rotation",
        "## Capacity and Retention",
        "## Production Evidence",
        "make platform-status",
        "make platform-app-health",
        "make platform-ci-health",
        "PLATFORM_PROFILE=premium-3node make platform-production-check",
        "least privilege",
        "cluster-admin",
        "incident commander",
        "SEV1",
        "manual cluster changes are temporary",
        "make platform-app-secrets",
        "SOPS age recipients and private keys",
        "Longhorn capacity",
        "Harbor registry storage",
    ):
        require_text(
            operations_text,
            needle,
            f"operations runbook must include {needle}",
        )
    for doc, label in (
        (readme_doc, "README"),
        (root / "docs/README.md", "documentation index"),
        (root / "docs/USER_GUIDE.md", "user guide"),
        (release_guide_doc, "release guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "private deployment guide"),
        (root / "docs/ARCHITECTURE.md", "architecture guide"),
    ):
        require_text(
            read(doc),
            "docs/OPERATIONS.md" if doc != root / "docs/README.md" else "OPERATIONS.md",
            f"{label} must link the operations runbook",
        )
    production_readiness_checklist_test_text = read(production_readiness_checklist_test)
    for needle in (
        "Validate the public-safe production readiness checklist",
        "docs/PRODUCTION_READINESS.md",
        "# Production Readiness Checklist",
        "public-safe go/no-go model",
        "Readiness Scope",
        "Go/No-Go Checklist",
        "Required Live Gates",
        "Component Acceptance Matrix",
        "Exceptions and Deferrals",
        "Launch Decision",
        "Post-Launch Validation",
        "Production Evidence",
        "Production readiness checklist validation passed",
    ):
        require_text(
            production_readiness_checklist_test_text,
            needle,
            f"production readiness checklist self-test must cover {needle}",
        )
    production_readiness_text = read(production_readiness_doc)
    for needle in (
        "# Production Readiness Checklist",
        "public-safe go/no-go model",
        "Repository safety",
        "RKE2 cluster",
        "GitOps source",
        "Ingress",
        "Stateful data",
        "Backup and recovery",
        "Platform apps",
        "Access control",
        "Security and supply chain",
        "Operations",
        "python scripts/run_validation.py",
        "make no-secrets",
        "PLATFORM_PROFILE=<PROFILE> make platform-profile-check",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "make platform-app-health",
        "make rke2-verify",
        "make platform-status",
        "PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-production-check",
        "RKE2 and etcd",
        "Cilium, CoreDNS, and kube-proxy path",
        "MetalLB, kube-vip, and ingress",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Prometheus, Grafana, and Loki",
        "cert-manager and trust-manager",
        "step-ca",
        "A skipped gate is an exception",
        "Expired exceptions block launch",
        "Decision:",
        "Evidence package location:",
        "Post-launch monitoring window:",
        "Do not commit private readiness packets",
        "internal hostnames",
        "launch approvals",
    ):
        require_text(
            production_readiness_text,
            needle,
            f"production readiness checklist must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/PRODUCTION_READINESS.md", "README"),
        (root / "docs/README.md", "PRODUCTION_READINESS.md", "documentation index"),
        (quick_start_doc, "docs/PRODUCTION_READINESS.md", "quick start"),
        (installation_doc, "docs/PRODUCTION_READINESS.md", "installation guide"),
        (premium_doc, "docs/PRODUCTION_READINESS.md", "premium profile"),
        (operations_doc, "docs/PRODUCTION_READINESS.md", "operations runbook"),
        (release_guide_doc, "docs/PRODUCTION_READINESS.md", "release guide"),
        (release_promotion_doc, "docs/PRODUCTION_READINESS.md", "release promotion runbook"),
        (compliance_audit_doc, "docs/PRODUCTION_READINESS.md", "compliance and audit guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/PRODUCTION_READINESS.md", "private deployment guide"),
        (root / "docs/ARCHITECTURE.md", "docs/PRODUCTION_READINESS.md", "architecture guide"),
        (security_policy_doc, "docs/PRODUCTION_READINESS.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention production readiness",
        )
    platform_support_test_text = read(platform_support_test)
    for needle in (
        "Validate the public-safe platform support and lifecycle policy",
        "docs/PLATFORM_SUPPORT.md",
        "docs/NODE_OS_SUPPORT.md",
        "# Platform Support",
        "Support Scope",
        "Support Tiers",
        "Component Support Matrix",
        "Version and Lifecycle Policy",
        "Compatibility Gates",
        "Upgrade and Deprecation Policy",
        "Support Evidence",
        "# Node OS Support",
        "Production acceptance",
        "Lifecycle review",
        "Platform support policy validation passed",
    ):
        require_text(
            platform_support_test_text,
            needle,
            f"platform support policy self-test must cover {needle}",
        )
    platform_support_text = read(platform_support_doc)
    for needle in (
        "# Platform Support",
        "public-safe support and lifecycle policy",
        "## Support Scope",
        "## Support Tiers",
        "Enterprise validated",
        "Compatible / best effort",
        "Deprecated or unsupported",
        "## Component Support Matrix",
        "RKE2",
        "Cilium",
        "kube-vip API VIP",
        "MetalLB app VIP",
        "Argo CD",
        "Forgejo",
        "Gitea",
        "GitLab CE",
        "Woodpecker",
        "Harbor",
        "CloudNativePG",
        "Longhorn",
        "Rook Ceph",
        "Velero",
        "Prometheus, Grafana, and Loki",
        "cert-manager, trust-manager, and optional step-ca",
        "## Version and Lifecycle Policy",
        "End-of-life operating systems",
        "owner, expiration, and compensating control",
        "## Compatibility Gates",
        "python scripts/run_validation.py",
        "PLATFORM_PROFILE=<PROFILE> make platform-profile-check",
        "make platform-production-check",
        "## Upgrade and Deprecation Policy",
        "rollback or roll-forward plan",
        "## Support Evidence",
        "Do not commit private support inventories",
    ):
        require_text(
            platform_support_text,
            needle,
            f"platform support policy must include {needle}",
        )
    node_os_support_text = read(node_os_support_doc)
    for needle in (
        "# Node OS Support",
        "Use this matrix with [Platform Support](PLATFORM_SUPPORT.md)",
        "## Support meaning",
        "Enterprise validated",
        "Compatible / best effort",
        "Workstation only",
        "## Required node capabilities",
        "Swap disabled",
        "## Production acceptance",
        "make rke2-verify",
        "make platform-production-check",
        "## Lifecycle review",
        "end-of-life OS",
        "owner, expiration date, compensating control",
        "## Validation sources",
    ):
        require_text(
            node_os_support_text,
            needle,
            f"node OS support policy must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/PLATFORM_SUPPORT.md", "README"),
        (root / "docs/README.md", "PLATFORM_SUPPORT.md", "documentation index"),
        (installation_doc, "docs/PLATFORM_SUPPORT.md", "installation guide"),
        (operations_doc, "docs/PLATFORM_SUPPORT.md", "operations runbook"),
        (production_readiness_doc, "docs/PLATFORM_SUPPORT.md", "production readiness checklist"),
        (release_guide_doc, "docs/PLATFORM_SUPPORT.md", "release guide"),
        (platform_support_doc, "NODE_OS_SUPPORT.md", "platform support policy"),
        (node_os_support_doc, "PLATFORM_SUPPORT.md", "node OS support policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention platform support",
        )
    incident_response_runbook_test_text = read(incident_response_runbook_test)
    for needle in (
        "Validate the public-safe production incident response runbook",
        "docs/INCIDENT_RESPONSE.md",
        "# Incident Response Runbook",
        "Severity Declaration",
        "Incident commander",
        "Operations lead",
        "Communications lead",
        "Scribe",
        "Security lead",
        "Service owner",
        "SEV1",
        "SEV2",
        "SEV3",
        "First 15 Minutes",
        "Component Triage Matrix",
        "Recovery Validation",
        "Post-Incident Review",
        "Incident response runbook validation passed",
    ):
        require_text(
            incident_response_runbook_test_text,
            needle,
            f"incident response runbook self-test must cover {needle}",
        )
    incident_response_text = read(incident_response_doc)
    for needle in (
        "# Incident Response Runbook",
        "public-safe incident response workflow",
        "private incident record",
        "## Principles",
        "## Severity Declaration",
        "## Roles",
        "## First 15 Minutes",
        "## Stabilization Actions",
        "## Component Triage Matrix",
        "## Communications",
        "## Evidence Collection",
        "## Recovery Validation",
        "## Post-Incident Review",
        "## Production Evidence",
        "Incident commander",
        "Operations lead",
        "Communications lead",
        "Scribe",
        "Security lead",
        "Service owner",
        "SEV1",
        "SEV2",
        "SEV3",
        "Freeze nonessential deployments",
        "Preserve volatile evidence",
        "make platform-status",
        "make platform-app-health",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "Pause Argo CD automated sync",
        "Treat break-glass access as temporary",
        "rotate affected credentials",
        "Kubernetes API and etcd",
        "CNI, CoreDNS, kube-proxy, and service path",
        "Ingress and VIP",
        "Argo CD",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and backups",
        "Prometheus, Grafana, Loki",
        "cert-manager, trust-manager, step-ca",
        "Root cause",
        "Contributing factors",
        "Data or secret exposure",
        "Manual changes made",
        "Monitoring gaps",
        "Runbook gaps",
        "Preventive actions",
        "Owners and due dates",
    ):
        require_text(
            incident_response_text,
            needle,
            f"incident response runbook must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/INCIDENT_RESPONSE.md", "README"),
        (root / "docs/README.md", "INCIDENT_RESPONSE.md", "documentation index"),
        (operations_doc, "docs/INCIDENT_RESPONSE.md", "operations runbook"),
        (alerting_doc, "docs/INCIDENT_RESPONSE.md", "alerting runbook"),
        (root / "docs/THREAT_MODEL.md", "docs/INCIDENT_RESPONSE.md", "threat model"),
        (data_classification_doc, "docs/INCIDENT_RESPONSE.md", "data classification"),
        (security_policy_doc, "docs/INCIDENT_RESPONSE.md", "security policy"),
        (root / "docs/USER_GUIDE.md", "docs/INCIDENT_RESPONSE.md", "user guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/INCIDENT_RESPONSE.md", "private deployment guide"),
        (release_guide_doc, "docs/INCIDENT_RESPONSE.md", "release guide"),
        (root / "docs/ARCHITECTURE.md", "docs/INCIDENT_RESPONSE.md", "architecture guide"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention incident response",
        )
    access_control_runbook_test_text = read(access_control_runbook_test)
    for needle in (
        "Validate the public-safe production access control runbook",
        "docs/ACCESS_CONTROL.md",
        "# Access Control Runbook",
        "Access Domains",
        "Human Access",
        "Kubernetes RBAC",
        "Argo CD Access",
        "Git and Branch Protection",
        "CI and Robot Accounts",
        "Break-Glass Access",
        "Access Review",
        "Removal and Rotation",
        "Production Evidence",
        "Platform operators",
        "Security operators",
        "Emergency break-glass users",
        "Access control runbook validation passed",
    ):
        require_text(
            access_control_runbook_test_text,
            needle,
            f"access control runbook self-test must cover {needle}",
        )
    access_control_text = read(access_control_doc)
    for needle in (
        "# Access Control Runbook",
        "public-safe access control model",
        "## Principles",
        "## Access Domains",
        "## Human Access",
        "## Kubernetes RBAC",
        "## Argo CD Access",
        "## Git and Branch Protection",
        "## CI and Robot Accounts",
        "## Break-Glass Access",
        "## Access Review",
        "## Removal and Rotation",
        "## Production Evidence",
        "least privilege",
        "MFA",
        "Git hosting",
        "Kubernetes",
        "Registry",
        "Database",
        "Storage and backup",
        "Observability",
        "PKI and trust",
        "Operator workstations",
        "Platform operators",
        "Security operators",
        "Source-control administrators",
        "CI administrators",
        "Registry administrators",
        "Database administrators",
        "Storage and backup administrators",
        "Observability administrators",
        "Read-only auditors",
        "Emergency break-glass users",
        "cluster-admin",
        "ClusterRoleBindings",
        "ServiceAccount token automounting",
        "Argo CD projects",
        "Protected main or production branches",
        "Required reviews",
        "Required validation checks",
        ".github/CODEOWNERS.example",
        "Robot accounts",
        "docs/INCIDENT_RESPONSE.md",
        "Monthly for high-value admin and robot access",
        "Quarterly for all platform roles",
        "SOPS age recipients",
        "Confirm Argo CD does not reapply stale credentials from Git",
        "Current role-to-system access matrix",
        "Current Kubernetes RBAC review",
        "Current Git branch protection and CODEOWNERS review",
        "Latest break-glass use",
        "Latest credential rotation",
    ):
        require_text(
            access_control_text,
            needle,
            f"access control runbook must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/ACCESS_CONTROL.md", "README"),
        (root / "docs/README.md", "ACCESS_CONTROL.md", "documentation index"),
        (operations_doc, "docs/ACCESS_CONTROL.md", "operations runbook"),
        (root / "docs/THREAT_MODEL.md", "docs/ACCESS_CONTROL.md", "threat model"),
        (data_classification_doc, "docs/ACCESS_CONTROL.md", "data classification"),
        (security_policy_doc, "docs/ACCESS_CONTROL.md", "security policy"),
        (root / "docs/USER_GUIDE.md", "docs/ACCESS_CONTROL.md", "user guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/ACCESS_CONTROL.md", "private deployment guide"),
        (release_guide_doc, "docs/ACCESS_CONTROL.md", "release guide"),
        (root / "docs/ARCHITECTURE.md", "docs/ACCESS_CONTROL.md", "architecture guide"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention access control",
        )
    capacity_planning_runbook_test_text = read(capacity_planning_runbook_test)
    for needle in (
        "Validate the public-safe production capacity planning runbook",
        "docs/CAPACITY_PLANNING.md",
        "# Capacity Planning Runbook",
        "Capacity Domains",
        "Baseline Inventory",
        "Saturation Signals",
        "Load and Scale Tests",
        "Scaling Decisions",
        "Component Planning",
        "Review Cadence",
        "Production Evidence",
        "Kubernetes nodes and API",
        "Woodpecker CI",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Capacity planning runbook validation passed",
    ):
        require_text(
            capacity_planning_runbook_test_text,
            needle,
            f"capacity planning runbook self-test must cover {needle}",
        )
    capacity_planning_text = read(capacity_planning_doc)
    for needle in (
        "# Capacity Planning Runbook",
        "public-safe production capacity planning model",
        "## Principles",
        "## Capacity Domains",
        "## Baseline Inventory",
        "## Saturation Signals",
        "## Load and Scale Tests",
        "## Scaling Decisions",
        "## Component Planning",
        "## Review Cadence",
        "## Production Evidence",
        "Kubernetes nodes and API",
        "CNI, CoreDNS, kube-proxy, and service path",
        "Ingress and VIP",
        "Argo CD",
        "Forgejo, Gitea, or GitLab",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Prometheus, Grafana, and Loki",
        "cert-manager, trust-manager, and step-ca",
        "MetalLB, kube-vip, and ingress",
        "Node CPU and memory pressure",
        "Disk usage and inode usage",
        "Pod scheduling failures",
        "Kubernetes API latency",
        "VIP reachability",
        "WAL growth",
        "CI queue depth",
        "Registry storage usage",
        "Prometheus retention size",
        "Loki ingestion rate",
        "Velero backup age",
        "Git clone, push, pull request, and webhook traffic",
        "representative Woodpecker pipelines",
        "Argo CD reconciliation load",
        "Velero backup and restore drills",
        "make platform-status",
        "make platform-app-health",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "docs/DATA_CLASSIFICATION.md",
        "docs/ACCESS_CONTROL.md",
        "private deployment repository or operations system",
        "Do not commit private capacity reports",
    ):
        require_text(
            capacity_planning_text,
            needle,
            f"capacity planning runbook must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/CAPACITY_PLANNING.md", "README"),
        (root / "docs/README.md", "CAPACITY_PLANNING.md", "documentation index"),
        (operations_doc, "docs/CAPACITY_PLANNING.md", "operations runbook"),
        (root / "docs/ARCHITECTURE.md", "docs/CAPACITY_PLANNING.md", "architecture guide"),
        (data_classification_doc, "docs/CAPACITY_PLANNING.md", "data classification"),
        (root / "docs/THREAT_MODEL.md", "docs/CAPACITY_PLANNING.md", "threat model"),
        (release_guide_doc, "docs/CAPACITY_PLANNING.md", "release guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/CAPACITY_PLANNING.md", "private deployment guide"),
        (root / "docs/USER_GUIDE.md", "docs/CAPACITY_PLANNING.md", "user guide"),
        (security_policy_doc, "docs/CAPACITY_PLANNING.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention capacity planning",
        )
    compliance_audit_runbook_test_text = read(compliance_audit_runbook_test)
    for needle in (
        "Validate the public-safe compliance and audit evidence guide",
        "docs/COMPLIANCE_AUDIT.md",
        "# Compliance and Audit Evidence",
        "Control Domains",
        "Required Evidence Records",
        "Audit Logging Expectations",
        "Exceptions and Risk Acceptance",
        "Review Cadence",
        "Control Mapping Template",
        "Production Evidence",
        "Source control",
        "Supply chain",
        "Audit logging",
        "Compliance and audit evidence validation passed",
    ):
        require_text(
            compliance_audit_runbook_test_text,
            needle,
            f"compliance and audit evidence self-test must cover {needle}",
        )
    compliance_audit_text = read(compliance_audit_doc)
    for needle in (
        "# Compliance and Audit Evidence",
        "public-safe compliance and audit evidence model",
        "It is not a legal compliance statement",
        "## Principles",
        "## Control Domains",
        "## Required Evidence Records",
        "## Audit Logging Expectations",
        "## Exceptions and Risk Acceptance",
        "## Review Cadence",
        "## Control Mapping Template",
        "## Production Evidence",
        "Source control",
        "Change management",
        "Access control",
        "Secrets management",
        "CI/CD separation",
        "Supply chain",
        "Backup and recovery",
        "Incident response",
        "Observability",
        "Capacity management",
        "Data classification",
        "Vulnerability management",
        "Audit logging",
        "Disaster recovery",
        "PKI and trust",
        "python scripts/run_validation.py",
        "make no-secrets",
        "python scripts/validate_no_secrets.py",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "make platform-app-health",
        "docs/BACKUP_RESTORE.md",
        "docs/OPERATIONS.md",
        "docs/INCIDENT_RESPONSE.md",
        "docs/ACCESS_CONTROL.md",
        "docs/CAPACITY_PLANNING.md",
        "docs/ALERTING.md",
        "docs/DATA_CLASSIFICATION.md",
        "docs/THREAT_MODEL.md",
        "SECURITY.md",
        "Who changed production desired state",
        "Which Argo CD Application applied the change",
        "Which Kubernetes resources changed",
        "Git hosting audit logs",
        "Argo CD Application history",
        "Kubernetes events and audit logs",
        "Harbor audit logs",
        "Woodpecker build history",
        "skipped health gate",
        "temporary admin access",
        "broad alert silence",
        "missing backup target",
        "unpinned dependency",
        "expired restore drill",
        "Compensating control",
        "Expiration date",
        "Expired exceptions should block production release",
        "Do not commit private audit exports",
    ):
        require_text(
            compliance_audit_text,
            needle,
            f"compliance and audit evidence guide must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/COMPLIANCE_AUDIT.md", "README"),
        (root / "docs/README.md", "COMPLIANCE_AUDIT.md", "documentation index"),
        (operations_doc, "docs/COMPLIANCE_AUDIT.md", "operations runbook"),
        (root / "docs/ARCHITECTURE.md", "docs/COMPLIANCE_AUDIT.md", "architecture guide"),
        (data_classification_doc, "docs/COMPLIANCE_AUDIT.md", "data classification"),
        (root / "docs/THREAT_MODEL.md", "docs/COMPLIANCE_AUDIT.md", "threat model"),
        (release_guide_doc, "docs/COMPLIANCE_AUDIT.md", "release guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/COMPLIANCE_AUDIT.md", "private deployment guide"),
        (root / "docs/USER_GUIDE.md", "docs/COMPLIANCE_AUDIT.md", "user guide"),
        (security_policy_doc, "docs/COMPLIANCE_AUDIT.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention compliance and audit evidence",
        )
    release_promotion_runbook_test_text = read(release_promotion_runbook_test)
    for needle in (
        "Validate the public-safe release and environment promotion runbook",
        "docs/RELEASE_PROMOTION.md",
        "# Release and Environment Promotion",
        "Environment Model",
        "Source and Artifact Flow",
        "Promotion Gates",
        "Change Windows and Freezes",
        "Rollback and Roll-Forward",
        "Hotfix Flow",
        "Versioning and Tags",
        "Argo CD Promotion Modes",
        "Production Evidence",
        "Development",
        "Staging",
        "Production",
        "Release promotion runbook validation passed",
    ):
        require_text(
            release_promotion_runbook_test_text,
            needle,
            f"release promotion runbook self-test must cover {needle}",
        )
    release_promotion_text = read(release_promotion_doc)
    for needle in (
        "# Release and Environment Promotion",
        "public-safe release and environment promotion model",
        "## Principles",
        "## Environment Model",
        "## Source and Artifact Flow",
        "## Promotion Gates",
        "## Change Windows and Freezes",
        "## Rollback and Roll-Forward",
        "## Hotfix Flow",
        "## Versioning and Tags",
        "## Argo CD Promotion Modes",
        "## Production Evidence",
        "Development",
        "Staging",
        "Production",
        "gitops/apps-dev",
        "gitops/apps-stage",
        "gitops/apps-prod",
        "pull request review",
        "CI test, scan, sign, and publish",
        "immutable image tag or digest",
        "GitOps pull request updates desired state",
        "python scripts/run_validation.py",
        "make no-secrets",
        "PLATFORM_PROFILE=<PROFILE> make platform-profile-check",
        "make platform-status",
        "make platform-app-health",
        "PLATFORM_PROFILE=<PROFILE> make platform-production-check",
        "docs/COMPLIANCE_AUDIT.md",
        "docs/CAPACITY_PLANNING.md",
        "maintenance window",
        "change freeze",
        "incident is active",
        "error budget is exhausted",
        "Previous known-good Git revision",
        "Previous known-good image tag or digest",
        "Database and storage rollback constraints",
        "Argo CD sync action or revert commit",
        "roll-forward fix",
        "Declare the incident or urgent change owner",
        "Freeze unrelated promotions",
        "Create the smallest safe Git change",
        "Promote through staging when time allows",
        "Git commit SHA",
        "Image digest or stable release tag",
        "Pinned Helm chart version",
        "Pinned CI Action commit SHA",
        "Do not use mutable tags",
        "Directory promotion",
        "Branch promotion",
        "Repository promotion",
        "ApplicationSet promotion",
        "temporary seed Git",
        "insecure repository URLs",
        "Do not commit private release records",
    ):
        require_text(
            release_promotion_text,
            needle,
            f"release promotion runbook must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/RELEASE_PROMOTION.md", "README"),
        (root / "docs/README.md", "RELEASE_PROMOTION.md", "documentation index"),
        (operations_doc, "docs/RELEASE_PROMOTION.md", "operations runbook"),
        (release_guide_doc, "docs/RELEASE_PROMOTION.md", "release guide"),
        (root / "docs/USER_GUIDE.md", "docs/RELEASE_PROMOTION.md", "user guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "docs/RELEASE_PROMOTION.md", "private deployment guide"),
        (root / "docs/ARCHITECTURE.md", "docs/RELEASE_PROMOTION.md", "architecture guide"),
        (compliance_audit_doc, "docs/RELEASE_PROMOTION.md", "compliance and audit guide"),
        (root / "docs/THREAT_MODEL.md", "docs/RELEASE_PROMOTION.md", "threat model"),
        (security_policy_doc, "docs/RELEASE_PROMOTION.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention release promotion",
        )
    alerting_runbook_test_text = read(alerting_runbook_test)
    for needle in (
        "Validate the production alerting and SLO runbook",
        "docs/ALERTING.md",
        "Alerting Principles",
        "Severity Model",
        "Required Receivers",
        "Required Platform Signals",
        "SLO and Error Budget Expectations",
        "Alert Routing Tests",
        "Silences and Maintenance",
        "Alert Review",
        "Alerting runbook validation passed",
    ):
        require_text(
            alerting_runbook_test_text,
            needle,
            f"alerting runbook self-test must cover {needle}",
        )
    alerting_text = read(alerting_doc)
    for needle in (
        "## Alerting Principles",
        "## Severity Model",
        "## Required Receivers",
        "## Required Platform Signals",
        "## SLO and Error Budget Expectations",
        "## Alert Routing Tests",
        "## Silences and Maintenance",
        "## Alert Review",
        "## Production Evidence",
        "Alerts must be actionable",
        "Every paging alert must have an owner and a runbook",
        "Immediate page",
        "Platform critical receiver",
        "Backup and restore receiver",
        "monitoring/alertmanager-main",
        "Kubernetes API",
        "CNI/service path",
        "Ingress/VIP",
        "GitOps",
        "Woodpecker server unavailable",
        "Harbor core/registry unavailable",
        "CloudNativePG primary unavailable",
        "Longhorn node not schedulable",
        "Velero BackupStorageLocation unavailable",
        "cert-manager Certificate not ready",
        "make platform-app-health",
        "error budget",
        "Send a test alert for each receiver",
        "Do not use broad namespace-wide or severity-wide silences",
        "Review alerts monthly",
        "Latest Alertmanager receiver test",
        "Do not commit private receiver details",
    ):
        require_text(
            alerting_text,
            needle,
            f"alerting runbook must include {needle}",
        )
    for doc, label in (
        (readme_doc, "README"),
        (root / "docs/README.md", "documentation index"),
        (operations_doc, "operations guide"),
        (release_guide_doc, "release guide"),
        (root / "docs/PRIVATE_DEPLOYMENT.md", "private deployment guide"),
        (root / "docs/ARCHITECTURE.md", "architecture guide"),
    ):
        require_text(
            read(doc),
            "docs/ALERTING.md" if doc != root / "docs/README.md" else "ALERTING.md",
            f"{label} must link the alerting runbook",
        )
    data_classification_test_text = read(data_classification_test)
    for needle in (
        "Validate the public-safe data classification and retention runbook",
        "docs/DATA_CLASSIFICATION.md",
        "# Data Classification and Retention",
        "Classification Levels",
        "Component Data Map",
        "Retention Baseline",
        "Handling Rules",
        "Disposal and Erasure",
        "Public template data",
        "Internal deployment metadata",
        "Confidential operational data",
        "Restricted secrets and access material",
        "Regulated or customer data",
        "Forgejo, Gitea, or GitLab",
        "Argo CD",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG PostgreSQL",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "RKE2 and etcd",
        "Data classification validation passed",
    ):
        require_text(
            data_classification_test_text,
            needle,
            f"data classification self-test must cover {needle}",
        )
    data_classification_text = read(data_classification_doc)
    for needle in (
        "# Data Classification and Retention",
        "public template",
        "private deployment repository",
        "## Classification Levels",
        "## Component Data Map",
        "## Retention Baseline",
        "## Handling Rules",
        "## Disposal and Erasure",
        "## Evidence",
        "Public template data",
        "Internal deployment metadata",
        "Confidential operational data",
        "Restricted secrets and access material",
        "Regulated or customer data",
        "Forgejo, Gitea, or GitLab",
        "Argo CD",
        "Woodpecker CI",
        "Harbor",
        "CloudNativePG PostgreSQL",
        "Longhorn or alternate storage",
        "Velero and object storage",
        "Loki",
        "Prometheus",
        "Grafana",
        "cert-manager and trust-manager",
        "step-ca",
        "RKE2 and etcd",
        "Git repositories and pull requests",
        "CI logs and artifacts",
        "Registry artifacts",
        "Database backups and WAL",
        "Etcd snapshots",
        "Velero backups",
        "Encrypt restricted secrets",
        "Redact logs",
        "synthetic data for restore",
        "Confirming Argo CD does not recreate deleted resources from Git",
        "Do not promise customer or user erasure",
        "docs/BACKUP_RESTORE.md",
        "docs/ALERTING.md",
        "docs/THREAT_MODEL.md",
        "SECURITY.md",
    ):
        require_text(
            data_classification_text,
            needle,
            f"data classification runbook must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/DATA_CLASSIFICATION.md", "README"),
        (root / "docs/README.md", "DATA_CLASSIFICATION.md", "documentation index"),
        (root / "docs/SECRETS_AND_PRIVACY.md", "docs/DATA_CLASSIFICATION.md", "secrets/privacy docs"),
        (operations_doc, "docs/DATA_CLASSIFICATION.md", "operations runbook"),
        (root / "docs/THREAT_MODEL.md", "docs/DATA_CLASSIFICATION.md", "threat model"),
        (security_policy_doc, "docs/DATA_CLASSIFICATION.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention data classification",
        )
    security_policy_test_text = read(security_policy_test)
    for needle in (
        "Validate public security policy and governance references",
        "SECURITY.md",
        "Supported Scope",
        "Supported Versions",
        "Reporting a Vulnerability",
        "Response Expectations",
        "Secret Exposure Handling",
        "Dependency and Supply-Chain Security",
        "Secure Configuration Baseline",
        "Disclosure and Safe Harbor",
        "Security policy validation passed",
    ):
        require_text(
            security_policy_test_text,
            needle,
            f"security policy self-test must cover {needle}",
        )
    security_policy_text = read(security_policy_doc)
    for needle in (
        "## Supported Scope",
        "## Supported Versions",
        "## Reporting a Vulnerability",
        "## Response Expectations",
        "## Secret Exposure Handling",
        "## Dependency and Supply-Chain Security",
        "## Secure Configuration Baseline",
        "## Disclosure and Safe Harbor",
        "public-safe platform template",
        "must not contain live organization secrets",
        "latest-main security support model",
        "| `main` | Supported |",
        "Use a private security report",
        "Do not paste",
        "Target first response",
        "Rotate the exposed credential or key immediately",
        "make no-secrets",
        "python scripts/run_validation.py",
        "Rewrite Git history only in private repositories",
        "renovate.json",
        "CI workflows pinned to full commit SHAs",
        "verify-signed-images.example.yaml",
        "SBOMs and attestations",
        "make platform-production-check",
        "docs/BACKUP_RESTORE.md",
        "docs/OPERATIONS.md",
        "docs/INCIDENT_RESPONSE.md",
        "docs/ALERTING.md",
        "docs/THREAT_MODEL.md",
        "docs/DATA_CLASSIFICATION.md",
        "temporary seed Git URL",
        "Good-faith research",
        "Public disclosure should wait",
    ):
        require_text(
            security_policy_text,
            needle,
            f"SECURITY.md must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "SECURITY.md", "README"),
        (root / "docs/README.md", "../SECURITY.md", "documentation index"),
        (root / "CONTRIBUTING.md", "SECURITY.md", "contributing guide"),
        (release_guide_doc, "SECURITY.md", "release guide"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention the security policy",
        )
    threat_model_test_text = read(threat_model_test)
    for needle in (
        "Validate the public-safe production threat model",
        "docs/THREAT_MODEL.md",
        "# Threat Model",
        "public-safe",
        "## Assets",
        "## Trust Boundaries",
        "## Threat Scenarios",
        "## High-Risk Changes",
        "## Evidence",
        "Kubernetes API and etcd",
        "GitOps repository",
        "Argo CD credentials and projects",
        "Secret leakage",
        "Unauthorized production change",
        "Supply-chain compromise",
        "CI credential misuse",
        "Ingress or VIP exposure",
        "Storage or database loss",
        "python scripts/run_validation.py",
        "Threat model validation passed",
    ):
        require_text(
            threat_model_test_text,
            needle,
            f"threat model self-test must cover {needle}",
        )
    threat_model_text = read(root / "docs/THREAT_MODEL.md")
    for needle in (
        "# Threat Model",
        "public-safe",
        "private deployment",
        "## Scope",
        "## Assumptions",
        "## Assets",
        "## Trust Boundaries",
        "## Threat Scenarios",
        "## High-Risk Changes",
        "## Evidence",
        "## Review Cadence",
        "Kubernetes API and etcd",
        "GitOps repository",
        "Argo CD credentials and projects",
        "Forgejo repositories and admin users",
        "Woodpecker secrets and agents",
        "Harbor projects and robot accounts",
        "CloudNativePG data and backups",
        "Longhorn or alternate storage volumes",
        "Velero and object storage credentials",
        "cert-manager, trust-manager, and step-ca material",
        "SOPS age recipients and private keys",
        "Secret leakage",
        "Unauthorized production change",
        "Supply-chain compromise",
        "CI credential misuse",
        "Argo CD over-privilege",
        "Ingress or VIP exposure",
        "Storage or database loss",
        "Backup target compromise",
        "PKI or trust compromise",
        "Observability data leak",
        "Service-network failure",
        ".github/CODEOWNERS.example",
        "branch protection",
        "required reviews",
        "Renovate dashboard approval",
        "staged Cosign/Kyverno verification",
        "restore drills",
        "platform-production-check",
        "docs/DATA_CLASSIFICATION.md",
    ):
        require_text(
            threat_model_text,
            needle,
            f"threat model must include {needle}",
        )
    for doc, needle, label in (
        (readme_doc, "docs/THREAT_MODEL.md", "README"),
        (root / "docs/README.md", "THREAT_MODEL.md", "documentation index"),
        (root / "docs/ARCHITECTURE.md", "docs/THREAT_MODEL.md", "architecture docs"),
        (security_policy_doc, "docs/THREAT_MODEL.md", "security policy"),
    ):
        require_text(
            read(doc),
            needle,
            f"{label} must link or mention the threat model",
        )
    repository_governance_test_text = read(repository_governance_test)
    for needle in (
        "Validate repository review and issue governance templates",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        "Public-Safety Check",
        "Production Impact",
        "Rollback",
        "blank_issues_enabled: false",
        "Security vulnerability or secret exposure",
        "Repository governance template validation passed",
    ):
        require_text(
            repository_governance_test_text,
            needle,
            f"repository governance self-test must cover {needle}",
        )
    pr_template_text = read(root / ".github/pull_request_template.md")
    for needle in (
        "## Summary",
        "## Change Type",
        "## Public-Safety Check",
        "## Validation",
        "## Production Impact",
        "## Rollback",
        "No real domains, private IPs, customer names",
        "python scripts/run_validation.py",
        "make no-secrets",
        "Required maintenance window",
        "Required restore, alerting, security, or operations evidence update",
        "Data-loss risk",
        "SECURITY.md",
        "docs/OPERATIONS.md",
        "docs/ALERTING.md",
        "docs/BACKUP_RESTORE.md",
    ):
        require_text(
            pr_template_text,
            needle,
            f"pull request template must include {needle}",
        )
    issue_config_text = read(root / ".github/ISSUE_TEMPLATE/config.yml")
    for needle in (
        "blank_issues_enabled: false",
        "Security vulnerability or secret exposure",
        "SECURITY.md",
        "Do not open a public issue with private details",
        "Private deployment support",
    ):
        require_text(
            issue_config_text,
            needle,
            f"issue template config must include {needle}",
        )
    for issue_template, label in (
        (root / ".github/ISSUE_TEMPLATE/bug_report.yml", "bug issue template"),
        (root / ".github/ISSUE_TEMPLATE/feature_request.yml", "feature issue template"),
    ):
        issue_text = read(issue_template)
        for needle in (
            "Do not include",
            "Production impact" if "feature" in issue_template.name else "Validation output",
            "Operations, alerting, backup, or restore",
        ):
            require_text(
                issue_text,
                needle,
                f"{label} must include {needle}",
            )
    contributing_text = read(root / "CONTRIBUTING.md")
    for needle in (
        "pull request and issue templates",
        "production impact",
        "rollback",
        "public-safety",
        ".github/CODEOWNERS.example",
        ".github/CODEOWNERS",
        "branch protection",
        "required reviews",
    ):
        require_text(
            contributing_text,
            needle,
            f"contributing guide must document repository governance templates: {needle}",
        )
    codeowners_starter_test_text = read(codeowners_starter_test)
    for needle in (
        "Validate the public-safe CODEOWNERS starter for private deployments",
        ".github/CODEOWNERS.example",
        ".github/CODEOWNERS",
        "branch protection",
        "required reviews",
        "required reviewers",
        "@org/platform-maintainers",
        "@org/security-maintainers",
        "CODEOWNERS starter validation passed",
    ):
        require_text(
            codeowners_starter_test_text,
            needle,
            f"CODEOWNERS starter self-test must cover {needle}",
        )
    codeowners_text = read(root / ".github/CODEOWNERS.example")
    for needle in (
        "Copy this file to .github/CODEOWNERS",
        "Replace @org/...",
        "branch protection",
        "required reviews",
        "Keep real owner names",
        "* @org/platform-maintainers",
        "/SECURITY.md @org/security-maintainers",
        "/.github/ @org/platform-maintainers @org/security-maintainers",
        "/ansible/ @org/platform-automation-maintainers",
        "/scripts/ @org/platform-automation-maintainers @org/security-maintainers",
        "/gitops/ @org/gitops-maintainers @org/security-maintainers",
        "/policies/ @org/security-maintainers",
        "/renovate.json @org/supply-chain-maintainers",
        "/docs/BACKUP_RESTORE.md @org/platform-operations @org/backup-owners",
        "/docs/BUSINESS_CONTINUITY.md @org/platform-operations @org/backup-owners @org/security-maintainers",
        "/docs/SERVICE_CATALOG.md @org/platform-operations @org/security-maintainers",
        "/docs/ARCHITECTURE_DECISIONS.md @org/platform-maintainers @org/security-maintainers",
        "/docs/adr/ @org/platform-maintainers @org/security-maintainers",
        "/docs/OPERATIONS.md @org/platform-operations",
        "/docs/PRODUCTION_READINESS.md @org/platform-operations @org/security-maintainers",
        "/docs/PLATFORM_SUPPORT.md @org/platform-maintainers @org/platform-operations",
        "/docs/NODE_OS_SUPPORT.md @org/platform-maintainers @org/platform-operations",
        "/docs/ALERTING.md @org/platform-operations @org/observability-owners",
        "/docs/SECRETS_AND_PRIVACY.md @org/security-maintainers",
    ):
        require_text(
            codeowners_text,
            needle,
            f"CODEOWNERS starter must include {needle}",
        )
    operations_text = read(operations_doc)
    for needle in (
        ".github/CODEOWNERS.example",
        ".github/CODEOWNERS",
        "required reviewers",
        "branch protection",
    ):
        require_text(
            operations_text,
            needle,
            f"operations runbook must document private CODEOWNERS routing: {needle}",
        )
    renovate_text = read(root / "renovate.json")
    for needle in (
        "https://docs.renovatebot.com/renovate-schema.json",
        "config:recommended",
        '"dependencyDashboard": true',
        '"automerge": false',
        '"prConcurrentLimit": 5',
        '"prHourlyLimit": 2',
        '"rangeStrategy": "pin"',
        '"docker"',
        '"pinDigests": true',
        '"helm"',
        '"dependencyDashboardApproval": true',
    ):
        require_text(
            renovate_text,
            needle,
            f"renovate.json must include {needle}",
        )
    readme_text = read(root / "README.md")
    for needle in (
        "Cosign + Renovate supply-chain helpers",
        "make supply-chain-posture",
        "OpenSSF Scorecard",
        "SPDX SBOM",
        "renovate.json",
        "verify-signed-images.example.yaml",
    ):
        require_text(readme_text, needle, f"README must document {needle}")
    architecture_text = read(root / "docs/ARCHITECTURE.md")
    for needle in (
        "Renovate",
        "Cosign",
        "Syft",
        "OpenSSF Scorecard",
        "make supply-chain-posture",
        "image signature",
        "dependency update",
    ):
        require_text(architecture_text, needle, f"architecture docs must document {needle}")
    premium_docs_text = read(root / "docs/PREMIUM_3NODE.md")
    for needle in (
        "renovate.json",
        "Cosign",
        "Syft",
        "OpenSSF Scorecard",
        "make supply-chain-posture",
        "verify-signed-images.example.yaml",
        "pinDigests",
    ):
        require_text(premium_docs_text, needle, f"premium docs must document {needle}")
    policies_readme_text = read(root / "policies/README.md")
    for needle in (
        "not applied by default",
        "audit/starter posture",
        "replace placeholders such as `<NAMESPACE>`",
        "python scripts/test_policy_examples.py",
        "kyverno/no-plaintext-secrets.example.yaml",
        "kyverno/verify-signed-images.example.yaml",
        "network/default-deny.example.yaml",
        "Cosign",
        "Renovate",
        "renovate.json",
    ):
        require_text(
            policies_readme_text,
            needle,
            f"policy README must document {needle}",
        )
    for rel_path, required_needles in {
        "policies/kyverno/no-plaintext-secrets.example.yaml": (
            "validationFailureAction: Audit",
            "platform.gitops/secret-source",
            "external-secrets",
            "sealed-secrets",
            "manual-bootstrap",
        ),
        "policies/kyverno/require-workload-baseline.example.yaml": (
            "validationFailureAction: Audit",
            "require-resource-requests",
            "require-non-root",
            "allowPrivilegeEscalation: false",
        ),
        "policies/kyverno/verify-signed-images.example.yaml": (
            "apiVersion: policies.kyverno.io/v1",
            "kind: ImageValidatingPolicy",
            "matchImageReferences:",
            "image.registry == '<REGISTRY>'",
            "validationActions:",
            "- Audit",
            "failurePolicy: Fail",
            "mutateDigest: true",
            "required: true",
            "verifyDigest: true",
            "attestors:",
            "<COSIGN_PUBLIC_KEY>",
            "https://rekor.sigstore.dev",
        ),
        "policies/network/default-deny.example.yaml": (
            "namespace: <NAMESPACE>",
            "policyTypes:",
            "- Ingress",
            "- Egress",
        ),
        "policies/network/allow-platform-dns-and-ingress.example.yaml": (
            "namespace: <NAMESPACE>",
            "allow-dns-and-traefik",
            "kubernetes.io/metadata.name: traefik",
            "kubernetes.io/metadata.name: kube-system",
            "port: 53",
        ),
    }.items():
        policy_text = read(root / rel_path)
        for needle in required_needles:
            require_text(policy_text, needle, f"{rel_path} must include {needle}")
    line_endings_test_text = read(line_endings_test)
    for needle in (
        "LF_PATTERNS",
        "ls-files",
        "--exclude-standard",
        "ALLOWED_PRIVATE_FILES",
        "SKIP_PARTS",
        "requires_lf",
        "has_cr_line_endings",
        "CRLF or CR line endings",
        ".helmignore",
        "Dockerfile",
        "*.env.example",
        "*.json",
        "*.gotmpl",
        "*.tpl",
        "*.yaml",
        "*.md",
    ):
        require_text(
            line_endings_test_text,
            needle,
            f"line ending self-test must cover {needle}",
        )
    for needle in (
        "tempfile",
        "premium-3node",
        '"default"',
        "gitops/clusters/rke2-main/platform-apps.yaml",
        "gitops/clusters/rke2-main/apps/default-app",
        "<THIS_REPO_URL>",
        "<FORGEJO_DATA_SIZE>",
        "quoted and commented path",
        "missing Application source path",
        "missing path",
        "references missing path(s)",
        "inherited profile placeholder",
    ):
        require_text(
            profile_check_test_text,
            needle,
            f"profile checker self-test must cover {needle}",
        )
    for needle in (
        "APPLICATION_PATH_RE",
        "missing application path",
        "--required-path",
        "Required shared GitOps paths are incomplete",
        "resolve_profile_entries",
        "selected_application_documents",
        "profile_dependency_files",
        "--profile",
        "profiles/",
        "references missing path(s)",
        "metadata is incomplete and cannot be skipped",
    ):
        require_text(
            deployable_renderer_text,
            needle,
            f"deployable renderer must robustly handle {needle}",
        )
    for needle in (
        "quoted and commented path",
        "Skipped incomplete GitOps applications",
        "No deployable GitOps applications remain",
        "deployable-renderer-required-",
        "<PROJECT_REPO_URL>",
        "IGNORED_CHART_PLACEHOLDER",
        "missing-app",
        "gitea-woodpecker-argocd",
        "expected generated Gitea Application from profile include",
        "deployable-renderer-inherited-placeholder-",
        "missing profile include path",
    ):
        require_text(
            deployable_renderer_test_text,
            needle,
            f"deployable renderer self-test must cover {needle}",
        )
    for needle in (
        "validate-gitops-selection.sh",
        "premium-3node",
        '"default"',
        "gitea-woodpecker-argocd",
        "strict",
        "skip-incomplete",
        "unsupported profile 'unknown-profile'",
        "Unsupported PLATFORM_GITOPS_PLACEHOLDER_MODE",
        "do not use skip-incomplete output as production proof",
        ".platform-gitops-selection-*.yaml",
        "PLATFORM_REPO_URL",
        "PYTHON",
    ):
        require_text(
            gitops_selection_helper_test_text,
            needle,
            f"GitOps selection helper self-test must cover {needle}",
        )
    docs_make_targets_test_text = read(docs_make_targets_test)
    for needle in (
        "MAKE_LINE_RE",
        "iter_make_snippets",
        "unknown make target",
        "README.md",
        "docs",
    ):
        require_text(
            docs_make_targets_test_text,
            needle,
            f"documented make target self-test must cover {needle}",
        )
    ansible_playbook_references_test_text = read(ansible_playbook_references_test)
    for needle in (
        "PLAYBOOK_REF_RE",
        "TASK_INCLUDE_RE",
        "VARS_FILES_RE",
        "internal_ansible_sources",
        "check_internal_ansible_file_references",
        "includes missing task file",
        "references missing vars file",
        "referenced playbook",
        "unreferenced playbook",
        "referenced_paths",
        "ansible/playbooks",
        "CONFLICT_MARKER_RE",
        "hosts:",
        "YAML document marker",
    ):
        require_text(
            ansible_playbook_references_test_text,
            needle,
            f"Ansible playbook reference self-test must cover {needle}",
        )
    gitops_application_contract_test_text = read(gitops_application_contract_test)
    for needle in (
        "APPLICATION_FILES",
        "PROJECT_FILE",
        "APPLICATION_KIND_RE",
        "PROJECT_KIND_RE",
        "<THIS_REPO_URL>",
        "sync_wave",
        "REQUIRED_APP_DEPENDENCIES",
        "REQUIRED_CLUSTER_RESOURCE_WHITELIST",
        "REQUIRED_NAMESPACE_RESOURCE_BLACKLIST",
        "MONITORING_BLOCK_RE",
        "MONITORING_FLAG_RE",
        "source_enables_monitoring_crds",
        "AUTOMATED_SYNC_RE",
        "PRUNE_TRUE_RE",
        "SELF_HEAL_TRUE_RE",
        "ALLOW_EMPTY_FALSE_RE",
        "REQUIRED_PRUNE_SYNC_OPTIONS",
        "check_root_application_contract",
        "check_pruning_runbook",
        "project_list_items",
        "application_destination_namespaces",
        "check_project_contract",
        "sourceRepos must be exactly <THIS_REPO_URL>",
        "destinations must match Application namespaces",
        "clusterResourceWhitelist must not allow */*",
        "clusterResourceWhitelist must match the explicit production allowlist",
        "namespaceResourceBlacklist must deny child Argo CD control-plane resources",
        "must warn on orphaned resources",
        "check_inferred_monitoring_dependencies",
        "enables ServiceMonitor/PodMonitor resources",
        "argocd.argoproj.io/sync-wave",
        "must enable automated prune behind explicit confirmation",
        "must keep automated allowEmpty disabled",
        "is missing guarded prune sync option",
        "must keep automated selfHeal enabled",
        "check_dependency_waves",
        "must be after dependency",
        "CreateNamespace=true",
        "source path does not exist",
        "source path is missing kustomization.yaml",
        "destination namespace",
        "kustomization namespace",
        "Helm namespace",
        "references missing Helm values file",
        "repeats Application name",
    ):
        require_text(
            gitops_application_contract_test_text,
            needle,
            f"GitOps Application contract self-test must cover {needle}",
        )
    kustomization_references_test_text = read(kustomization_references_test)
    for needle in (
        "KUSTOMIZATION_NAMES",
        "LOCAL_PATH_SECTIONS",
        "resources",
        "components",
        "patchesStrategicMerge",
        "VALUES_FILE_RE",
        "PATCH_PATH_RE",
        "references missing",
        "directory without kustomization",
        "Kustomization reference validation passed",
    ):
        require_text(
            kustomization_references_test_text,
            needle,
            f"Kustomization reference self-test must cover {needle}",
        )
    gitops_helm_chart_pinning_test_text = read(gitops_helm_chart_pinning_test)
    for needle in (
        "Validate remote Kustomize Helm charts are pinned and fully declared",
        "SCAN_ROOT",
        "helmCharts:",
        "REQUIRED_REMOTE_CHART_FIELDS",
        "releaseName",
        "valuesFile",
        "top_level_namespace",
        "must set",
        "must match kustomization namespace",
        "references missing valuesFile",
        "Helm chart {name} must pin version",
        "uses mutable version",
        "uses prerelease version",
        "GitOps Helm chart pinning validation passed",
    ):
        require_text(
            gitops_helm_chart_pinning_test_text,
            needle,
            f"GitOps Helm chart pinning self-test must cover {needle}",
        )
    gitops_image_pinning_test_text = read(gitops_image_pinning_test)
    for needle in (
        "SCAN_ROOTS",
        "MUTABLE_TAGS",
        "latest",
        "next",
        "nightly",
        "explicit_image_tag",
        "repository_has_explicit_pin",
        "finish_image_block",
        "image reference must pin a tag or sha256 digest",
        "image repository embeds mutable tag",
        "must set a non-empty tag or sha256 digest",
        "GitOps image pinning validation passed",
        "gitops/clusters/rke2-main/premium-3node/apps",
    ):
        require_text(
            gitops_image_pinning_test_text,
            needle,
            f"GitOps image pinning self-test must cover {needle}",
        )
    makefile_help_test_text = read(makefile_help_test)
    for needle in (
        "SHELL_RE",
        "make_shell",
        "Makefile SHELL must name only the shell program",
        "PHONY_RE",
        "HELP_RE",
        "TARGET_RE",
        "find_dependency_cycles",
        "target is missing from help output",
        "help output references unknown target",
        "target has unknown dependency",
        "target dependency cycle",
        "HELP_EXEMPTIONS",
    ):
        require_text(
            makefile_help_test_text,
            needle,
            f"Makefile help coverage self-test must cover {needle}",
        )
    markdown_links_test_text = read(markdown_links_test)
    for needle in (
        "MARKDOWN_LINK_RE",
        "HTML_HREF_RE",
        "README.md",
        "DOC_ROOT",
        "EXTERNAL_SCHEMES",
        "links outside the repository",
        "missing",
        "href",
    ):
        require_text(
            markdown_links_test_text,
            needle,
            f"Markdown link self-test must cover {needle}",
        )
    example_templates_test_text = read(example_templates_test)
    for needle in (
        "EXPECTED_LANGUAGES",
        "SERVICE_TEMPLATE_FILES",
        "GITOPS_TEMPLATE_FILES",
        "DISALLOWED_SERVICE_TEMPLATE_PATHS",
        "CI_NAMES",
        "GitHub Actions",
        "Gitea Actions",
        "Forgejo Actions",
        "GitLab CI",
        "Woodpecker CI",
        "legacy duplicate path",
        "20 language scaffolds",
        "examples/service-template",
    ):
        require_text(
            example_templates_test_text,
            needle,
            f"example template self-test must cover {needle}",
        )
    validation_surface_parity_test_text = read(validation_surface_parity_test)
    for needle in (
        "RUNNER",
        "runner_validation_scripts",
        "make_validate_scripts",
        "CI_SURFACE_FILES",
        "RUNNER_SURFACE_FILES",
        "check_ci_surface",
        "check_runner_surface",
        "ALLOWED_EXTRA_SCRIPTS",
        "scripts/run_validation.py",
        "missing validation script",
        "unexpected Python script",
        "allowlist contains unused",
        "render_private_platform_values.py",
        "make validate order",
        ".github",
        "sync-seed-git.sh",
    ):
        require_text(
            validation_surface_parity_test_text,
            needle,
            f"validation surface parity self-test must cover {needle}",
        )
    if "ansible/playbooks/verify-platform-app-health.yml" not in makefile_text:
        fail("platform-app-health target does not invoke the health playbook")
    if "platform-ci-health:" not in makefile_text or "PLATFORM_APP_HEALTH_STORAGE_CLASSES=skip" not in makefile_text:
        fail("platform-ci-health target must run the focused Argo CD/Woodpecker health gate")
    if "platform-production-check:" not in makefile_text:
        fail("Makefile is missing platform-production-check target")
    require_text(
        makefile_text,
        "platform-tls:",
        "Makefile is missing the pre-issued wildcard TLS target",
    )
    require_text(
        makefile_text,
        "ansible/playbooks/manage-platform-tls.yml",
        "platform-tls must invoke the managed wildcard TLS workflow",
    )
    if "RKE2_VERIFY_API_VIP=false $(MAKE) rke2-verify" not in makefile_text:
        fail("platform-bootstrap must run the initial pre-VIP rke2-verify with RKE2_VERIFY_API_VIP=false")
    if "@$(MAKE) rke2-api-vip" not in makefile_text or "@$(MAKE) rke2-verify" not in makefile_text:
        fail("platform-bootstrap must deploy the API VIP and then run the strict rke2-verify gate")
    for target in (
        "validate",
        "platform-profile-check",
        "rke2-verify",
        "platform-status",
        "platform-tls-verify",
    ):
        production_target = re.search(r"(?m)^platform-production-check:.*$", makefile_text)
        if not production_target or target not in production_target.group(0):
            fail(f"platform-production-check must depend on {target}")
    require_text(
        makefile_text,
        "PLATFORM_APP_HEALTH_MODE=production bash scripts/bootstrap/run-platform-app-health.sh",
        "platform-production-check must force strict production-mode app health",
    )
    require_text(
        makefile_text,
        "policy-cel-verify:\n\t@$(PYTHON) scripts/verify_active_kyverno_policies.py",
        "Makefile must expose the active Kyverno CEL verification target",
    )
    require_text(
        makefile_text,
        "\t@$(MAKE) policy-cel-verify",
        "platform-production-check must compile and behavior-test active Kyverno CEL policies",
    )
    require_text(
        makefile_text,
        "platform-image-inventory-verify: rendered-schema-verify rendered-private-schema-verify supply-chain-verify",
        "image inventory gate must depend on exact rendering and signature evidence",
    )
    require_text(
        makefile_text,
        "\t@$(MAKE) platform-image-inventory-verify",
        "platform production check must reconcile rendered and live runtime images",
    )
    require_text(
        makefile_text,
        "PLATFORM_POLICY_ENFORCEMENT=Enforce PLATFORM_IMAGE_INTEGRITY_MODE=Enforce PLATFORM_IMAGE_INTEGRITY_REQUIRED=true $(MAKE) platform-policy-readiness",
        "platform production check must require live Kyverno and image-integrity Enforce mode",
    )
    for needle in (
        "def render_loki(",
        "def render_velero(",
        "def render_cnpg_postgres_cluster(",
        "def render_platform_image_integrity(",
        "--loki-values",
        "--velero-values",
        "--cnpg-postgres-cluster",
        "LOKI_OBJECT_STORAGE_SECRET_NAME",
        "VELERO_CREDENTIALS_SECRET_NAME",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "CNPG_RENDER_POSTGRES_CLUSTER",
        "CNPG_BACKUP_ENABLED",
        "PLATFORM_IMAGE_INTEGRITY_MODE",
        "PLATFORM_COSIGN_PUBLIC_KEY_FILE",
        "WOODPECKER_DATABASE_MODE",
        "WOODPECKER_DATABASE_SECRET_NAME",
        "WOODPECKER_IMAGE_TAG",
        "woodpeckerci/woodpecker-server",
        "woodpeckerci/woodpecker-agent",
        "WOODPECKER_SERVER_REPLICAS",
        "default_server_replicas",
        "WOODPECKER_SERVER_REPLICAS must be 1 when WOODPECKER_DATABASE_MODE=sqlite",
        "WOODPECKER_IMAGE_TAG must be a stable release tag",
        "HARBOR_DATABASE_MODE",
        "HARBOR_DATABASE_SECRET_NAME",
        "HARBOR_REDIS_MODE",
        "HARBOR_REDIS_SECRET_NAME",
        "HARBOR_STORAGE_MODE",
        "HARBOR_S3_BUCKET",
        "HARBOR_S3_SECRET_NAME",
        "OBJECT_STORAGE_ENDPOINT",
        "PLATFORM_VALKEY_AUTH_SECRET_NAME",
        "PLATFORM_VALKEY_PASSWORD_KEY",
        "PLATFORM_VALKEY_REPLICA_COUNT",
        "PLATFORM_VALKEY_PRIMARY_HOST",
        "MINIO_ROOT_SECRET_NAME",
        "MINIO_REPLICA_COUNT",
        "MINIO_DATA_SIZE",
        "FORGEJO_DATABASE_MODE",
        "FORGEJO_DATABASE_SECRET_NAME",
        "FORGEJO_DATABASE_SSL_MODE",
        "FORGEJO_REDIS_SECRET_NAME",
        "GITEA__database__PASSWD",
        "GITEA__cache__HOST",
        "GITEA__queue__CONN_STR",
        "GRAFANA_DATABASE_MODE",
        "GRAFANA_DATABASE_SECRET_NAME",
        "$__env{GF_DATABASE_PASSWORD}",
        "BACKUP_BUCKET",
        'INTERNAL_MINIO_ENDPOINT = "http://platform-minio.object-storage.svc.cluster.local:9000"',
        "crds:\n  enabled: true",
    ):
        require_text(renderer_text, needle, f"private values renderer must cover {needle}")
    for needle in (
        "render_platform_valkey",
        "render_minio",
        "render_loki",
        "render_velero",
        "render_real_premium_profile",
        "shutil.copytree",
        'check_profile(repo, "premium-3node")',
        "assert_no_placeholders",
        "platform-test-loki-chunks",
        "platform-test-velero",
        "platform-test-cnpg",
        "cnpg-object-test",
        "platform-valkey-test",
        "valkey-password-test",
        "usersExistingSecret:",
        "minio-root-test",
        "root-password-test",
        "MINIO_REPLICA_COUNT",
        "MINIO_DATA_SIZE",
        "distributed MinIO",
        "FORGEJO_DATABASE_MODE",
        "forgejo-db-test",
        "forgejo-redis-test",
        "additionalConfigFromEnvs:",
        "GITEA__database__PASSWD",
        "GITEA__queue__CONN_STR",
        "WOODPECKER_DATABASE_MODE",
        "woodpecker-db-test",
        'WOODPECKER_DATABASE_DRIVER: "postgres"',
        "replicaCount: 1",
        "repository: woodpeckerci/woodpecker-server",
        "repository: woodpeckerci/woodpecker-agent",
        'tag: "v3.16.0"',
        "SQLite-backed Woodpecker accepted multiple server replicas",
        "Woodpecker renderer accepted a mutable image tag",
        "portal:\\n  replicas: 2\\n  podDisruptionBudget:",
        "registry:\\n  replicas: 2\\n  podDisruptionBudget:",
        "database:\\n  type: internal\\n  internal:\\n    resources:",
        "redis:\\n  type: internal\\n  internal:\\n    resources:",
        "HARBOR_DATABASE_MODE",
        "HARBOR_REDIS_MODE",
        "HARBOR_STORAGE_MODE",
        "harbor-db-test",
        "harbor-redis-test",
        "harbor-s3-test",
        "imageChartStorage:\\n    disableredirect: true\\n    type: s3",
        "database:\\n  type: external",
        "redis:\\n  type: external",
        "GRAFANA_DATABASE_MODE",
        "grafana-db-test",
        "GF_DATABASE_PASSWORD",
        'password: "$__env{GF_DATABASE_PASSWORD}"',
        "prometheusSpec:\\n    replicas: 2\\n    podAntiAffinity: hard",
        "alertmanagerSpec:\\n    useExistingSecret: true\\n    configSecret: alertmanager-platform-config\\n    replicas: 3\\n    podAntiAffinity: hard",
        "grafana:\\n  replicas: 2\\n  deploymentStrategy:\\n    type: RollingUpdate",
        "grafana:\\n  replicas: 1\\n  admin:",
        "write:\\n  replicas: 3\\n  resources:",
        "read:\\n  replicas: 3\\n  resources:",
        "backend:\\n  replicas: 3\\n  resources:",
        "gateway:\\n  enabled: true\\n  replicas: 3\\n  basicAuth:\\n    enabled: true\\n    existingSecret: loki-gateway-basic-auth",
        "deployNodeAgent: true\\n\\nresources:",
        "nodeAgent:\\n  resources:",
        "service:\\n  type: ClusterIP\\n  port: 443\\n  targetPort: 9000",
        "autocert:\\n  enabled: false",
        "resources:\\n  requests:\\n    cpu: 100m\\n    memory: 256Mi",
        "${LOKI_S3_ACCESS_KEY_ID}",
        "${LOKI_S3_SECRET_ACCESS_KEY}",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "platform-test-cnpg",
        "cnpg-object-test",
    ):
        require_text(renderer_test_text, needle, f"private values renderer self-test must cover {needle}")
    for needle in (
        "CONTRACTS",
        "HARBOR_ADMIN_SECRET_NAME",
        "HARBOR_SECRET_KEY_SECRET_NAME",
        "HARBOR_DATABASE_SECRET_NAME",
        "HARBOR_REDIS_SECRET_NAME",
        "HARBOR_S3_SECRET_NAME",
        "PLATFORM_VALKEY_AUTH_SECRET_NAME",
        "platform-valkey-custom",
        "valkey-password-custom",
        "REGISTRY_STORAGE_S3_ACCESSKEY",
        "REGISTRY_STORAGE_S3_SECRETKEY",
        "FORGEJO_DATABASE_SECRET_NAME",
        "FORGEJO_REDIS_SECRET_NAME",
        "forgejo-db-custom",
        "forgejo-redis-custom",
        "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME",
        "WOODPECKER_DATABASE_SECRET_NAME",
        "WOODPECKER_DATABASE_DATASOURCE",
        "GRAFANA_ADMIN_SECRET_NAME",
        "GRAFANA_DATABASE_SECRET_NAME",
        "grafana-db-custom",
        "GF_DATABASE_PASSWORD",
        "admin-user",
        "admin-password",
        "LOKI_OBJECT_STORAGE_SECRET_NAME",
        "VELERO_CREDENTIALS_SECRET_NAME",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "cnpg-object-custom",
        "ACCESS_KEY_ID",
        "SECRET_ACCESS_KEY",
        "existingSecretAdminPassword",
        "extraSecretNamesForEnvFrom",
        "existingSecret: velero-credentials",
        "--from-literal=",
        "render_with_custom_secret_names",
    ):
        require_text(
            platform_secret_contract_test_text,
            needle,
            f"platform app secret contract self-test must cover {needle}",
        )
    no_secrets_test_text = read(no_secrets_test)
    private_artifact_boundary_test_text = read(private_artifact_boundary_test)
    for needle in (
        "company domain fragment",
        "private deployment hostname",
        "include_internal_markers=False",
        "private IP-like value",
        "private node username",
        "possible plaintext secret",
        "forgejo.<PLATFORM_DOMAIN>",
        ".shell-syntax-leftover",
        "__pycache__",
        "private",
        "rendered",
        "secrets",
    ):
        require_text(no_secrets_test_text, needle, f"secret/privacy scanner self-test must cover {needle}")
    no_secrets_text = read(no_secrets_script)
    for needle in (
        "part.startswith('.shell-syntax-')",
        "'private'",
        "'rendered'",
        "'secrets'",
        "'.venv'",
        "'.pytest_cache'",
        "'__pycache__'",
    ):
        require_text(no_secrets_text, needle, f"secret/privacy scanner must ignore generated/private artifacts: {needle}")
    for needle in (
        "ALLOWED_TRACKED_PATHS",
        "REQUIRED_GITIGNORE_RULES",
        "git ls-files",
        "private/README.md",
        "private/.gitkeep",
        "secrets/README.md",
        "secrets/.gitkeep",
        "rendered/",
        "Disallowed tracked private artifact path",
    ):
        require_text(
            private_artifact_boundary_test_text,
            needle,
            f"private artifact boundary self-test must cover {needle}",
        )
    ci_reference_pinning_test_text = read(ci_reference_pinning_test)
    for needle in (
        "Validate CI references, credentials, runners, and execution bounds",
        "CI_FILES",
        "ACTIONS_WORKFLOW_FILES",
        "GITLAB_CI_FILES",
        "DOCKERFILES",
        "MUTABLE_REFS",
        "ACTION_SHA_RE",
        "MAX_JOB_TIMEOUT_MINUTES",
        "action reference must include @ref",
        "action reference uses floating ref",
        "action reference must pin a full commit SHA",
        "container image must pin a tag or sha256 digest",
        "container image uses floating tag",
        "checkout must set persist-credentials: false",
        "uses moving runner label",
        "GitLab job",
        "WOODPECKER_DEFAULT_PIPELINE_TIMEOUT",
        "WOODPECKER_MAX_PIPELINE_TIMEOUT",
        "CI execution and reference validation passed",
    ):
        require_text(
            ci_reference_pinning_test_text,
            needle,
            f"CI reference pinning self-test must cover {needle}",
        )
    push_ready_text = read(root / "docs/PUSH_READY.md")
    release_guide_text = read(root / "docs/RELEASE_GUIDE.md")
    for needle in (
        "full commit SHAs",
        "upstream tag kept as a comment",
        "make validate",
    ):
        require_text(push_ready_text, needle, f"push-ready guide must document CI action SHA pinning: {needle}")
    for needle in (
        "full commit SHAs",
        "moving tags such as `v4` or `v5`",
        "Third-party CI actions are pinned by full commit SHA",
    ):
        require_text(release_guide_text, needle, f"release guide must document CI action SHA pinning: {needle}")
    shell_syntax_test_text = read(shell_syntax_test)
    for needle in (
        "bash is required for shell syntax validation",
        "subprocess.run",
        "'-n'",
        "*.sh",
        "part.startswith('.shell-syntax-')",
        "'private'",
        "'rendered'",
        "'secrets'",
        "'.venv'",
        ".ansible-shell-syntax-",
    ):
        require_text(shell_syntax_test_text, needle, f"shell syntax self-test must cover {needle}")
    shell_strict_mode_test_text = read(shell_strict_mode_test)
    for needle in (
        "Validate production shell scripts use Bash strict mode",
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "must set",
        "Shell strict mode validation passed",
    ):
        require_text(
            shell_strict_mode_test_text,
            needle,
            f"shell strict mode self-test must cover {needle}",
        )
    ansible_shell_blocks_test_text = read(ansible_shell_blocks_test)
    for needle in (
        "Syntax-check inline shell blocks embedded in Ansible playbooks",
        "SHELL_BLOCK_RE",
        "normalize_jinja",
        "ansible.builtin",
        "bash is required for Ansible inline shell syntax validation",
        "subprocess.run",
        '"-n"',
        ".ansible-shell-syntax-",
        "Ansible inline shell syntax validation failed",
    ):
        require_text(
            ansible_shell_blocks_test_text,
            needle,
            f"Ansible inline shell block self-test must cover {needle}",
        )
    ansible_curl_timeout_contract_test_text = read(ansible_curl_timeout_contract_test)
    for needle in (
        "Keep Ansible and bootstrap curl probes bounded",
        "ANSIBLE_DIRS",
        "SCRIPT_DIRS",
        "CURL_COMMAND_RE",
        "--connect-timeout",
        "--max-time",
        "run_bounded",
        "curl probe must set --connect-timeout and --max-time",
        "Ansible curl timeout contract validation passed",
    ):
        require_text(
            ansible_curl_timeout_contract_test_text,
            needle,
            f"Ansible curl timeout contract self-test must cover {needle}",
        )
    ansible_until_contract_test_text = read(ansible_until_contract_test)
    for needle in (
        "Keep Ansible retry loops bounded and intentional",
        "ANSIBLE_DIRS",
        "UNTIL_RE",
        "RETRIES_RE",
        "DELAY_RE",
        "until loop must set",
        "Ansible until contract validation passed",
    ):
        require_text(
            ansible_until_contract_test_text,
            needle,
            f"Ansible until contract self-test must cover {needle}",
        )
    ansible_failed_when_contract_test_text = read(ansible_failed_when_contract_test)
    for needle in (
        "Keep suppressed Ansible failures diagnosable",
        "FAILED_WHEN_FALSE_RE",
        "REGISTER_RE",
        "DIAGNOSTIC_ACTION_RE",
        "task must register suppressed failure result",
        "Ansible failed_when contract validation passed",
    ):
        require_text(
            ansible_failed_when_contract_test_text,
            needle,
            f"Ansible failed_when contract self-test must cover {needle}",
        )
    ansible_no_log_contract_test_text = read(ansible_no_log_contract_test)
    for needle in (
        "Keep Ansible no_log usage intentional and secret-scoped",
        "ANSIBLE_DIRS",
        'ROOT / "ansible" / "tasks"',
        "ALLOWED_NO_LOG_TASKS",
        "REQUIRED_VISIBLE_TASKS",
        "Register private Git repository credentials when provided",
        "Generate or preserve shared platform Valkey auth secret",
        "Generate or preserve Harbor bootstrap secrets",
        "Generate or preserve Grafana admin credentials secret",
        "Generate or preserve Grafana database password secret",
        "Configure Forgejo OAuth application for Woodpecker",
        "Register platform applications in Argo CD",
        "unexpected no_log directive",
        "required secret-scoped no_log task",
        "must not use no_log",
        "Ansible no_log contract validation passed",
    ):
        require_text(
            ansible_no_log_contract_test_text,
            needle,
            f"Ansible no_log contract self-test must cover {needle}",
        )
    for needle in (
        "Generate or preserve shared platform Valkey auth secret",
        "PLATFORM_VALKEY_PASSWORD",
        "PLATFORM_VALKEY_AUTO_GENERATE",
        "platform-cache",
        "platform-valkey-auth",
        "valkey-password",
        "Generate or preserve MinIO root credentials secret",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "MINIO_ROOT_AUTO_GENERATE",
        "object-storage",
        "minio-root",
        "root-password",
        "Generate or preserve Loki object storage credentials secret",
        "Generate or preserve Velero cloud credentials secret",
        'credentials_source="minio-root"',
        'result_state="reconciled"',
        "'state=reconciled' in platform_velero_secret_result.stdout",
        "platform_minio_root_user_secret_key",
        "platform_minio_root_password_secret_key",
        "set-explicit-cloud-credentials-or-create-minio-root-secret",
        "Generate or preserve Woodpecker database datasource secret",
        "Generate or preserve Grafana admin credentials secret",
        "Generate or preserve Grafana database password secret",
        "Generate or preserve Harbor external database password secret",
        "Generate or preserve Harbor external Redis credentials secret",
        "Generate or preserve Harbor registry S3 credentials secret",
        "Check Harbor external database password secret state",
        "Check Harbor external Redis credentials secret state",
        "Check Harbor registry S3 credentials secret state",
        "Require Harbor production dependency secrets when enabled",
        "Check Loki object storage credentials secret state",
        "Check Velero cloud credentials secret state",
        "Generate or preserve CloudNativePG object storage credentials secret",
        "Check CloudNativePG object storage credentials secret state",
        "Generate or preserve Forgejo external database password secret",
        "Generate or preserve Forgejo Redis URI secret",
        "Check Forgejo external database password secret state",
        "Check Forgejo Redis URI secret state",
        "Require Forgejo production dependency secrets when enabled",
        "Check Woodpecker database datasource secret state",
        "Check Grafana database password secret state",
        "Require object storage credentials secrets when enabled",
        "Require Woodpecker database datasource secret when enabled",
        "Require Grafana database password secret when enabled",
        "PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE",
        "PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE",
        "PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE",
        "PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE",
        "PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE",
        "PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS",
        "PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE",
        "PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE",
        "PLATFORM_APP_SECRET_REQUIRE_FORGEJO_REDIS",
        "HARBOR_DATABASE_PASSWORD",
        "HARBOR_REDIS_PASSWORD",
        "HARBOR_S3_ACCESS_KEY_ID",
        "HARBOR_S3_SECRET_ACCESS_KEY",
        "REGISTRY_STORAGE_S3_ACCESSKEY",
        "REGISTRY_STORAGE_S3_SECRETKEY",
        "FORGEJO_DATABASE_PASSWORD",
        "FORGEJO_REDIS_URL",
        "FORGEJO_REDIS_HOST",
        "FORGEJO_REDIS_PASSWORD",
        "FORGEJO_REDIS_TLS",
        "HARBOR_REDIS_TLS",
        "FORGEJO_REDIS_TLS:-true",
        "HARBOR_REDIS_TLS:-true",
        'scheme = "rediss"',
        "WOODPECKER_DATABASE_DATASOURCE",
        "WOODPECKER_DATABASE_HOST",
        "WOODPECKER_DATABASE_PASSWORD",
        "from urllib.parse import quote",
        "quote(password, safe='')",
        "LOKI_S3_ACCESS_KEY_ID",
        "LOKI_S3_SECRET_ACCESS_KEY",
        "VELERO_CLOUD_CREDENTIALS",
        "CNPG_S3_ACCESS_KEY_ID",
        "CNPG_S3_SECRET_ACCESS_KEY",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "ACCESS_KEY_ID",
        "SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GRAFANA_ADMIN_SECRET_NAME",
        "GRAFANA_DATABASE_SECRET_NAME",
        "GRAFANA_DATABASE_PASSWORD",
        "admin-user",
        "admin-password",
    ):
        require_text(app_secrets_text, needle, f"platform app secret automation must cover {needle}")

    for doc in (installation_doc, premium_doc, troubleshooting_doc):
        doc_text = read(doc)
        doc_compact = " ".join(doc_text.split())
        if doc != troubleshooting_doc and "make platform-app-health" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the platform app health gate")
        if doc != troubleshooting_doc and "make platform-profile-check" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the GitOps profile check")
        if "make platform-ci-health" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the focused Argo CD/Woodpecker health gate")
        if "make platform-woodpecker-repair" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the focused Woodpecker repair target")
        if "make platform-production-check" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the production readiness gate")
        if "PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document node-originated VIP strict mode")
        if "PLATFORM_APP_HEALTH_GUI_APPS" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document GUI app filtering for subset profiles")
        if "PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document existing Argo CD Application filtering for subset profiles")
        if "PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO" not in doc_text or "seed Git" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document temporary seed Git source repository enforcement")
        if "PLATFORM_APP_HEALTH_EXPECTED_REPO_URL" not in doc_text or "PLATFORM_REPO_URL" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document exact production repository source enforcement")
        if "PLATFORM_APP_HEALTH_HTTP_REDIRECT" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document HTTP redirect enforcement")
        if "PLATFORM_APP_HEALTH_STORAGE_CLASSES" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document StorageClass enforcement")
        if "PVC" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document PVC readiness")
        if "PLATFORM_APP_HEALTH_LONGHORN_RUNTIME" not in doc_text or "Longhorn runtime" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Longhorn runtime node/volume enforcement")
        if "PLATFORM_APP_HEALTH_ARGOCD_RUNTIME" not in doc_text or "repo-server/Redis" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Argo CD runtime component/service enforcement")
        if "PLATFORM_APP_HEALTH_HA_REPLICAS" not in doc_text or "HA replica" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document critical HA replica enforcement")
        if "PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG" not in doc_text or "Woodpecker" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Woodpecker runtime image tag enforcement")
        if "PLATFORM_APP_HEALTH_CERTIFICATES" not in doc_text or "Certificate" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document cert-manager Certificate readiness enforcement")
        if "PLATFORM_APP_HEALTH_TRUST_BUNDLES" not in doc_text or "Bundle" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document trust-manager Bundle readiness enforcement")
        if "PLATFORM_APP_HEALTH_STEP_CA_API" not in doc_text or "step-ca" not in doc_text or "/health" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document step-ca API readiness enforcement")
        if "PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document production object-storage secret enforcement")
        if "backend" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document GUI backend endpoint readiness")
        if "make platform-service-path-repair" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document ClusterIP service-path repair")
        if "PLATFORM_APP_HEALTH_CNPG_CLUSTERS" not in doc_text or "CloudNativePG" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document CloudNativePG cluster readiness enforcement")
        if "PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET" not in doc_text or "CNPG_OBJECT_STORE_SECRET_NAME" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document CloudNativePG object-storage secret enforcement")
        if "PLATFORM_APP_HEALTH_REGISTRY_API" not in doc_text or "Docker Distribution" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Harbor registry API readiness enforcement")
        if "PLATFORM_APP_HEALTH_MONITORING_API" not in doc_text or "Grafana" not in doc_text or "Prometheus" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Grafana/Prometheus API readiness enforcement")
        if "PLATFORM_APP_HEALTH_LOKI_API" not in doc_text or "Loki" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Loki API readiness enforcement")
        if "PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE" not in doc_text or "BackupStorageLocation" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Velero BackupStorageLocation readiness enforcement")
        if "PLATFORM_APP_HEALTH_VELERO_SCHEDULES" not in doc_text or "Velero backup schedule" not in doc_compact:
            fail(f"{doc.relative_to(root)} does not document Velero backup schedule readiness enforcement")
        if "PLATFORM_APP_HEALTH_APP_SECRETS" not in doc_text or "generated app secret" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document generated app secret contract enforcement")
        if "WOODPECKER_DATABASE_DATASOURCE" not in doc_text or "PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Woodpecker database secret automation")
        if (
            "HARBOR_DATABASE_MODE" not in doc_text
            or "HARBOR_REDIS_MODE" not in doc_text
            or "HARBOR_STORAGE_MODE" not in doc_text
            or "PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE" not in doc_text
            or "PLATFORM_APP_HEALTH_HARBOR_PRODUCTION_SECRETS" not in doc_text
        ):
            fail(f"{doc.relative_to(root)} does not document Harbor production dependency automation")
        if (
            "FORGEJO_DATABASE_MODE" not in doc_text
            or "FORGEJO_DATABASE_PASSWORD" not in doc_text
            or "FORGEJO_REDIS_URL" not in doc_text
            or "FORGEJO_DATABASE_SECRET_NAME" not in doc_text
            or "FORGEJO_REDIS_SECRET_NAME" not in doc_text
            or "PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE" not in doc_text
            or "PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS" not in doc_text
        ):
            fail(f"{doc.relative_to(root)} does not document Forgejo production dependency automation")
        if (
            "GRAFANA_DATABASE_MODE" not in doc_text
            or "GRAFANA_DATABASE_PASSWORD" not in doc_text
            or "PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE" not in doc_text
            or "PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET" not in doc_text
        ):
            fail(f"{doc.relative_to(root)} does not document Grafana database secret automation")
        if "WOODPECKER_IMAGE_TAG" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Woodpecker image tag pinning")
        if "Woodpecker gRPC ClusterIP" not in doc_compact:
            fail(f"{doc.relative_to(root)} does not document Woodpecker consumer refresh after service-path repair")
        if "diagnostic pods pinned to every RKE2 node" not in doc_compact:
            fail(f"{doc.relative_to(root)} does not document pod-pinned Woodpecker service-path proof")
    for doc in (installation_doc, root / "docs/PRIVATE_DEPLOYMENT.md"):
        doc_text = read(doc)
        if "PLATFORM_RUN_PROFILE_CHECK" not in doc_text:
            fail(f"{doc.relative_to(root)} must document selected GitOps profile validation before push")
        if "WOODPECKER_DATABASE_DATASOURCE" not in doc_text or "PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE" not in doc_text:
            fail(f"{doc.relative_to(root)} must document private Woodpecker database secret automation")
        if "HARBOR_DATABASE_MODE" not in doc_text or "PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE" not in doc_text:
            fail(f"{doc.relative_to(root)} must document private Harbor production dependency automation")
        if "FORGEJO_DATABASE_MODE" not in doc_text or "PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE" not in doc_text:
            fail(f"{doc.relative_to(root)} must document private Forgejo production dependency automation")
        if "GRAFANA_DATABASE_MODE" not in doc_text or "PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE" not in doc_text:
            fail(f"{doc.relative_to(root)} must document private Grafana database secret automation")
        if "WOODPECKER_IMAGE_TAG" not in doc_text:
            fail(f"{doc.relative_to(root)} must document private Woodpecker image tag pinning")
        if "PLATFORM_SEED_SYNC_PUSH_ORIGIN" not in doc_text:
            fail(f"{doc.relative_to(root)} must document seed sync source remote push opt-in")
    for doc in (installation_doc, premium_doc, root / "docs/PRIVATE_DEPLOYMENT.md"):
        doc_text = read(doc)
        if "FORGEJO_DATABASE_SSL_MODE=disable" in doc_text:
            fail(
                f"{doc.relative_to(root)} must not override production Forgejo "
                "database TLS verification"
            )
    for doc in (installation_doc, premium_doc):
        doc_text = read(doc)
        for setting in (
            "PLATFORM_APP_HEALTH_SERVICE_CHECK_IMAGE",
            "PLATFORM_APP_HEALTH_SERVICE_CHECK_TIMEOUT",
        ):
            if setting not in doc_text:
                fail(f"{doc.relative_to(root)} does not document {setting}")
    for env_example in (first_deploy_env_example, seed_git_env_example):
        env_text = read(env_example)
        if "PLATFORM_RUN_PROFILE_CHECK=true" not in env_text:
            fail(f"{env_example.relative_to(root)} must keep selected profile checks enabled by default")
        if "PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES=true" not in env_text:
            fail(f"{env_example.relative_to(root)} must document private hostname safety-scan behavior")
        if "PYTHON=/usr/bin/python3" not in env_text:
            fail(f"{env_example.relative_to(root)} must document the PYTHON interpreter override")
        if "WOODPECKER_IMAGE_TAG=v3.16.0" not in env_text:
            fail(f"{env_example.relative_to(root)} must document the pinned Woodpecker image tag")
        for needle in (
            "CNPG_OBJECT_STORE_SECRET_NAME=cnpg-object-store",
            "CNPG_BACKUP_DESTINATION=s3://platform-cnpg-backups/platform-postgres",
            "CNPG_RENDER_POSTGRES_CLUSTER=true",
            "CNPG_BACKUP_ENABLED=true",
            "CNPG_POSTGRES_IMAGE=ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie",
            "PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE=true",
            "CNPG_S3_ACCESS_KEY_ID",
            "CNPG_S3_SECRET_ACCESS_KEY",
        ):
            if needle not in env_text:
                fail(f"{env_example.relative_to(root)} must document CloudNativePG private rendering/secret value: {needle}")
        for needle in (
            "PLATFORM_PRODUCTION_STRICT=true",
            "LONGHORN_BACKUP_TARGET=s3://platform-longhorn-backups@us-east-1/",
            "LONGHORN_BACKUP_CREDENTIAL_SECRET_NAME=longhorn-backup-target",
            "LONGHORN_ENCRYPTION_SECRET_NAME=longhorn-crypto",
            "LONGHORN_ENCRYPTION_AUTO_GENERATE=true",
            "LONGHORN_ENCRYPTION_RECOVERY_FILE=private/longhorn-encryption.key",
            "BACKUP_OBJECT_STORAGE_ENDPOINT=https://s3.amazonaws.com",
            "PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true",
            "PLATFORM_APP_SECRET_REQUIRE_LONGHORN_BACKUP=true",
            "PLATFORM_APP_SECRET_REQUIRE_LONGHORN_ENCRYPTION=true",
            "LONGHORN_BACKUP_ACCESS_KEY_ID",
            "LONGHORN_BACKUP_SECRET_ACCESS_KEY",
        ):
            if needle not in env_text:
                fail(f"{env_example.relative_to(root)} must document production backup value: {needle}")
        for needle in (
            "FORGEJO_REDIS_MODE=redis",
            "FORGEJO_REDIS_SECRET_NAME=forgejo-redis",
            "PLATFORM_VALKEY_AUTH_SECRET_NAME=platform-valkey-auth",
            "PLATFORM_VALKEY_PASSWORD_KEY=valkey-password",
            "PLATFORM_VALKEY_PRIMARY_HOST=platform-valkey-primary.platform-cache.svc.cluster.local",
            "FORGEJO_REDIS_TLS=true",
            "PLATFORM_VALKEY_REPLICA_COUNT=3",
            "PLATFORM_VALKEY_AUTO_GENERATE=true",
            "HARBOR_REDIS_MODE=external",
            "HARBOR_REDIS_ADDR=platform-valkey-primary.platform-cache.svc.cluster.local:6379",
            "HARBOR_REDIS_TLS=true",
            "HARBOR_REDIS_SECRET_NAME=harbor-redis",
        ):
            if needle not in env_text:
                fail(f"{env_example.relative_to(root)} must document shared Valkey private rendering/secret value: {needle}")
        for needle in (
            "MINIO_ROOT_SECRET_NAME=minio-root",
            "MINIO_ROOT_USER=platform-admin",
            "MINIO_ROOT_AUTO_GENERATE=true",
            "MINIO_DATA_SIZE=50Gi",
            "MINIO_STORAGE_CLASS=longhorn-critical-encrypted",
            "MINIO_REPLICA_COUNT=4",
            "MINIO_ZONES=1",
            "MINIO_DRIVES_PER_NODE=1",
        ):
            if needle not in env_text:
                fail(f"{env_example.relative_to(root)} must document MinIO private rendering/secret value: {needle}")
    seed_env_text = read(seed_git_env_example)
    if "PLATFORM_SEED_SYNC_PUSH_ORIGIN=false" not in seed_env_text:
        fail("seed-git.env.example must keep source remote push disabled by default")
    if "origin is your intended private deployment repo" not in seed_env_text:
        fail("seed-git.env.example must explain when source remote push is safe")
    for doc in (installation_doc, root / "docs/PRIVATE_DEPLOYMENT.md", root / "docs/SECRETS_AND_PRIVACY.md"):
        doc_text = read(doc)
        if "PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES" not in doc_text:
            fail(f"{doc.relative_to(root)} must document private hostname safety-scan behavior")
        if "PYTHON=/path/to/python" not in doc_text:
            fail(f"{doc.relative_to(root)} must document the PYTHON interpreter override")
        if "platform-argocd" not in doc_text:
            fail(f"{doc.relative_to(root)} must document the PYTHON override for Argo CD bootstrap")
    for doc in (readme_doc, quick_start_doc, release_guide_doc):
        doc_text = read(doc)
        for needle in (
            "repository-only",
            "Python 3 and Bash",
            "does not contact a live cluster",
            "make platform-production-check",
        ):
            if needle not in doc_text:
                fail(f"{doc.relative_to(root)} must distinguish local validation from live production proof: {needle}")

    if "make platform-production-check" not in read(bootstrap_plan_script):
        fail("scripts/bootstrap-plan.sh does not include the production readiness gate")

    print("Platform production contract validation passed.")


if __name__ == "__main__":
    main()
