#!/usr/bin/env python3
"""Validate production-readiness contracts that are easy to drift.

This intentionally avoids third-party dependencies so it can run in GitHub,
Forgejo/Gitea, Woodpecker, and minimal bootstrap environments.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

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
base_forgejo_values = root / "gitops/clusters/rke2-main/apps/forgejo/values.yaml"
premium_forgejo_values = root / "gitops/clusters/rke2-main/premium-3node/apps/forgejo/values.yaml"
base_woodpecker_values = root / "gitops/clusters/rke2-main/apps/woodpecker/values.yaml"
premium_woodpecker_values = root / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"
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
premium_keycloak_values = root / "gitops/clusters/rke2-main/premium-3node/apps/keycloak/values.yaml"
premium_kyverno_values = root / "gitops/clusters/rke2-main/premium-3node/apps/kyverno/values.yaml"
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
base_step_ca_values = root / "gitops/clusters/rke2-main/apps/step-ca/values.yaml"
premium_step_ca_values = root / "gitops/clusters/rke2-main/premium-3node/apps/step-ca/values.yaml"
stale_premium_root_app = root / "gitops/bootstrap/root-app-premium-3node.yaml"
health_playbook = root / "ansible/playbooks/verify-platform-app-health.yml"
service_path_consumers_playbook = root / "ansible/playbooks/repair-platform-service-path-consumers.yml"
woodpecker_repair_playbook = root / "ansible/playbooks/repair-woodpecker.yml"
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
platform_secret_contract_test = root / "scripts/test_platform_secret_contract.py"
policy_examples_test = root / "scripts/test_policy_examples.py"
sops_age_policy_test = root / "scripts/test_sops_age_policy.py"
supply_chain_helpers_test = root / "scripts/test_supply_chain_helpers.py"
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
    "tetragon",
    "external-secrets",
    "openbao",
    "metallb",
    "traefik",
    "longhorn",
    "cloudnativepg",
    "platform-postgres",
    "platform-valkey",
    "minio",
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
]


def fail(message: str) -> None:
    print(f"Platform contract validation failed: {message}")
    sys.exit(1)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"missing required file {path.relative_to(root)}")


def application_documents(path: Path) -> list[dict[str, str]]:
    text = read(path)
    docs: list[dict[str, str]] = []
    for raw_doc in re.split(r"(?m)^---\s*$", text):
        if "kind: Application" not in raw_doc:
            continue
        metadata_name = re.search(
            r"(?ms)^metadata:\s*\n(?:^\s+.*\n)*?^\s+name:\s*([A-Za-z0-9_.-]+)\s*$",
            raw_doc,
        )
        source_path = SOURCE_PATH_RE.search(raw_doc)
        destination_namespace = re.search(
            r"(?ms)^  destination:\s*\n(?:^\s+.*\n)*?^\s+namespace:\s*([A-Za-z0-9_.-]+)\s*$",
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
        "SSL_MODE: disable",
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
            "gitops/clusters/rke2-main/premium-3node/apps/tetragon",
            "gitops/clusters/rke2-main/premium-3node/apps/external-secrets",
            "gitops/clusters/rke2-main/premium-3node/apps/openbao",
            "gitops/clusters/rke2-main/apps/metallb",
            "gitops/clusters/rke2-main/premium-3node/apps/traefik",
            "gitops/clusters/rke2-main/premium-3node/apps/longhorn",
            "gitops/clusters/rke2-main/premium-3node/apps/cloudnativepg",
            "gitops/clusters/rke2-main/premium-3node/apps/platform-postgres",
            "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey",
            "gitops/clusters/rke2-main/premium-3node/apps/minio",
            "gitops/clusters/rke2-main/premium-3node/apps/keycloak",
            "gitops/clusters/rke2-main/premium-3node/apps/argocd-ha",
            "gitops/clusters/rke2-main/premium-3node/apps/forgejo",
            "gitops/clusters/rke2-main/premium-3node/apps/woodpecker",
            "gitops/clusters/rke2-main/premium-3node/apps/harbor",
            "gitops/clusters/rke2-main/premium-3node/apps/monitoring",
            "gitops/clusters/rke2-main/premium-3node/apps/loki",
            "gitops/clusters/rke2-main/premium-3node/apps/velero",
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

    assert_app_file(base_apps, required_base_apps)
    assert_app_file(premium_apps, required_premium_apps)

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
            f"repoServer:\n  replicas: {repo_replicas}",
            "applicationSet:\n  replicas: 2\n  resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi\n    limits:\n      memory: 512Mi",
            "redis-ha:\n  enabled: true\n  haproxy:\n    resources:\n      requests:\n        cpu: 50m\n        memory: 64Mi\n      limits:\n        memory: 128Mi",
            "  redis:\n    resources:\n      requests:\n        cpu: 100m\n        memory: 128Mi\n      limits:\n        memory: 512Mi",
            "    sentinel:\n      resources:\n        requests:\n          cpu: 50m\n          memory: 64Mi\n        limits:\n          memory: 128Mi",
            "dex:\n  resources:\n    requests:\n      cpu: 50m\n      memory: 64Mi\n    limits:\n      memory: 256Mi",
            "notifications:\n  resources:\n    requests:\n      cpu: 50m\n      memory: 64Mi\n    limits:\n      memory: 256Mi",
            "controller:\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi\n    limits:\n      memory: 2Gi",
        ):
            require_text(argocd_text, needle, f"{label} must include {needle.splitlines()[0]}")

    base_forgejo_text = read(base_forgejo_values)
    premium_forgejo_text = read(premium_forgejo_values)
    for needle in (
        "replicaCount: 1",
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
        "image:\n  rootless: true",
        "ingress:\n  enabled: true\n  className: traefik",
        "secretName: forgejo-tls",
        "persistence:\n  enabled: true\n  size: 20Gi\n  storageClass: longhorn-critical",
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
        "SSL_MODE: disable",
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
        "- woodpecker-forgejo-oauth",
        "createAgentSecret: true",
        "ingressClassName: traefik",
        "traefik.ingress.kubernetes.io/router.entrypoints: websecure",
        "traefik.ingress.kubernetes.io/router.tls: \"true\"",
        "secretName: woodpecker-tls",
        "persistentVolume:\n    enabled: true\n    size: 10Gi\n    storageClass: longhorn-standard",
        "  resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "WOODPECKER_BACKEND: kubernetes",
        "WOODPECKER_BACKEND_K8S_NAMESPACE: woodpecker",
        "WOODPECKER_BACKEND_K8S_STORAGE_CLASS: longhorn-standard",
        "WOODPECKER_BACKEND_K8S_VOLUME_SIZE: 10G",
        "WOODPECKER_BACKEND_K8S_STORAGE_RWX: \"false\"",
        "WOODPECKER_MAX_WORKFLOWS: \"2\"",
        "persistence:\n    enabled: false",
        "  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
    ):
        require_text(premium_woodpecker_text, needle, f"premium Woodpecker profile must include {needle.splitlines()[0]}")
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
        "backupTargetCredentialSecret: longhorn-backup-target",
        "createDefaultDiskLabeledNodes: false",
        "defaultReplicaCount: 2",
        "defaultDataLocality: best-effort",
        "replicaAutoBalance: best-effort",
        "storageOverProvisioningPercentage: 100",
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
    premium_longhorn_storageclasses_text = read(premium_longhorn_storageclasses)
    for needle in (
        "name: longhorn-standard",
        'storageclass.kubernetes.io/is-default-class: "true"',
        "provisioner: driver.longhorn.io",
        "allowVolumeExpansion: true",
        "reclaimPolicy: Retain",
        "volumeBindingMode: WaitForFirstConsumer",
        'numberOfReplicas: "2"',
        "name: longhorn-critical",
        'numberOfReplicas: "3"',
        "name: longhorn-cache",
        "reclaimPolicy: Delete",
        'numberOfReplicas: "1"',
        "dataLocality: best-effort",
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
    for cloudnativepg_values, label, replicas in (
        (base_cloudnativepg_values, "base CloudNativePG operator profile", 1),
        (premium_cloudnativepg_values, "premium CloudNativePG operator profile", 2),
    ):
        cloudnativepg_text = read(cloudnativepg_values)
        for needle in (
            f"replicaCount: {replicas}",
            "image:\n  tag: \"1.29.1\"",
            "updateStrategy:\n  type: RollingUpdate\n  rollingUpdate:\n    maxSurge: 1\n    maxUnavailable: 0",
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
        "storageClass: longhorn-critical",
        "enablePodMonitor: true",
    ):
        require_text(
            premium_platform_postgres_text,
            needle,
            f"premium platform PostgreSQL cluster must include {needle.splitlines()[0]}",
        )

    premium_platform_valkey_text = read(premium_platform_valkey_values)
    for needle in (
        "architecture: replication",
        "existingSecret: platform-valkey-auth",
        "existingSecretPasswordKey: valkey-password",
        "replicaCount: 3",
        "sentinel:\n  enabled: true",
        "quorum: 2",
        "createPrimary: true",
        "storageClass: longhorn-critical",
        "size: 8Gi",
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(
            premium_platform_valkey_text,
            needle,
            f"premium platform Valkey profile must include {needle.splitlines()[0]}",
        )

    premium_kyverno_text = read(premium_kyverno_values)
    for needle in (
        "crds:\n  install: true",
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
        "existingSecret: minio-root",
        "rootUserSecretKey: root-user",
        "rootPasswordSecretKey: root-password",
        "replicaCount: 4",
        "storageClass: longhorn-critical",
        "prometheusAuthType: public",
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(
            premium_minio_text,
            needle,
            f"premium MinIO profile must include {needle.splitlines()[0]}",
        )

    premium_keycloak_text = read(premium_keycloak_values)
    for needle in (
        "repository: bitnami/keycloak",
        "tag: 26.3.3-debian-12-r0",
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
    ):
        require_text(
            premium_keycloak_text,
            needle,
            f"premium Keycloak profile must include {needle.splitlines()[0]}",
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
        "server:\n  enabled: true",
        "dataStorage:\n    enabled: true\n    size: 20Gi\n    storageClass: longhorn-critical",
        "persistentVolumeClaimRetentionPolicy:\n      whenDeleted: Retain\n      whenScaled: Retain",
        "auditStorage:\n    enabled: true\n    size: 10Gi\n    storageClass: longhorn-critical",
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
            "updateStrategy:\n  type: Recreate",
            "persistence:\n  enabled: true",
            "imageChartStorage:\n    type: filesystem",
            "database:\n  type: internal\n  internal:\n    resources:",
            "existingSecretAdminPassword: harbor-admin",
            "existingSecretAdminPasswordKey: HARBOR_ADMIN_PASSWORD",
            "existingSecretSecretKey: harbor-secret-key",
            "metrics:\n  enabled: true\n  serviceMonitor:\n    enabled: true",
        ):
            require_text(harbor_text, needle, f"{label} must include {needle.splitlines()[0]}")

    base_harbor_text = read(base_harbor_values)
    require_text(
        base_harbor_text,
        "redis:\n  type: internal\n  internal:\n    resources:",
        "base Harbor profile must keep internal Redis as the template default",
    )
    for needle in (
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
        "portal:\n  replicas: 1\n  resources:",
        "core:\n  replicas: 1\n  resources:",
        "jobservice:\n  replicas: 1\n  resources:",
        "registry:\n  replicas: 1\n  registry:\n    resources:",
        "  controller:\n    resources:",
        "trivy:\n  enabled: true\n  replicas: 1\n  resources:",
        "exporter:\n  resources:",
        "redis:\n  type: external",
        "addr: platform-valkey-primary.platform-cache.svc.cluster.local:6379",
        "existingSecret: harbor-redis",
    ):
        require_text(premium_harbor_text, needle, f"premium Harbor profile must include {needle.splitlines()[0]}")
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
        "prometheusSpec:\n    replicas: 2\n    retention: 15d",
        "    resources:\n      requests:\n        cpu: 500m\n        memory: 2Gi\n      limits:\n        memory: 4Gi",
        "alertmanagerSpec:\n    replicas: 3\n    resources:\n      requests:\n        cpu: 100m\n        memory: 256Mi",
        "grafana:\n  replicas: 1\n  admin:",
        "resources:\n    requests:\n      cpu: 100m\n      memory: 256Mi",
        "admin:\n    existingSecret: grafana-admin\n    userKey: admin-user\n    passwordKey: admin-password",
        "storageClassName: longhorn-standard",
        "podMonitorSelectorNilUsesHelmValues: false",
        "serviceMonitorSelectorNilUsesHelmValues: false",
    ):
        require_text(
            premium_monitoring_text,
            needle,
            f"premium monitoring profile must include {needle.splitlines()[0]}",
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

    premium_loki_text = read(premium_loki_values)
    for needle in (
        "deploymentMode: SimpleScalable",
        "replication_factor: 3",
        "write:\n  replicas: 3\n  resources:\n    requests:\n      cpu: 500m\n      memory: 1Gi",
        "read:\n  replicas: 3\n  resources:\n    requests:\n      cpu: 250m\n      memory: 512Mi",
        "backend:\n  replicas: 3\n  resources:\n    requests:\n      cpu: 500m\n      memory: 1Gi",
        "gateway:\n  enabled: true\n  resources:\n    requests:\n      cpu: 100m\n      memory: 128Mi",
        "serviceMonitor:\n    enabled: true",
    ):
        require_text(premium_loki_text, needle, f"premium Loki profile must include {needle.splitlines()[0]}")

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
            "s3Url: <OBJECT_STORAGE_ENDPOINT>",
            "existingSecret: velero-credentials",
            "deployNodeAgent: true",
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n  limits:\n    memory: 512Mi",
            "nodeAgent:\n  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
            "snapshotsEnabled: true",
            "platform-daily:",
            "schedule: <VELERO_DAILY_BACKUP_CRON>",
            "serviceMonitor:\n    enabled: true",
        ):
            require_text(velero_text, needle, f"{label} must include {needle.splitlines()[0]}")

    premium_velero_text = read(premium_velero_values)
    for needle in (
        "deployNodeAgent: true",
        "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n  limits:\n    memory: 512Mi",
        "nodeAgent:\n  resources:\n    requests:\n      cpu: 250m\n      memory: 256Mi\n    limits:\n      memory: 1Gi",
        "snapshotsEnabled: true",
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
    if "PLATFORM_APP_HEALTH_HTTP_REDIRECT" not in health_text:
        fail("platform-app-health must expose HTTP-to-HTTPS redirect enforcement")
    if "platform_app_health_http_redirect_effective" not in health_text:
        fail("platform-app-health must default HTTP redirect enforcement through an effective variable")
    for task_name in (
        "Verify Argo CD platform application health",
        "Verify Argo CD application source repositories are production-safe",
        "Verify platform namespace pod readiness",
        "Verify Argo CD runtime component coverage",
        "Verify critical HA workload replica coverage",
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
        "Probe monitoring APIs from Ansible controller",
        "Probe configured GUI HTTP redirects from Ansible controller",
        "Probe configured GUI app ingress from every RKE2 node",
        "Probe Argo CD and Woodpecker ClusterIP service paths from every RKE2 node",
        "Probe Argo CD and Woodpecker ClusterIP service paths from pods pinned to every RKE2 node",
        "Stop when platform app health checks fail",
    ):
        require_text(health_text, f"- name: {task_name}", f"platform-app-health is missing task: {task_name}")
    for result_name in (
        "platform_app_health_argocd_app_probe",
        "platform_app_health_argocd_source_probe",
        "platform_app_health_pod_probe",
        "platform_app_health_argocd_runtime_probe",
        "platform_app_health_ha_replica_probe",
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
        "argocd-service-has-no-ready-endpoints",
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
        "generated Harbor/Forgejo/Woodpecker/Keycloak/Grafana/Loki/Velero/CloudNativePG/Valkey app secrets exist with required keys",
        "platform-app-health success message must include generated app secret readiness",
    )
    require_text(
        health_text,
        "Longhorn nodes are Ready/schedulable and attached Longhorn volumes are healthy",
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
        "/api/health",
        "/-/ready",
        '"database"[[:space:]]*:[[:space:]]*"ok"',
        "unexpected-monitoring-api-http-code",
        "unexpected-monitoring-api-body",
        "PLATFORM_APP_HEALTH_MONITORING_API=false make platform-app-health",
        "Grafana /api/health and Prometheus /-/ready answer through the app VIP",
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
    renderer_test_text = read(private_values_renderer_test)
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
    if "platform-woodpecker-repair:" not in makefile_text:
        fail("Makefile is missing platform-woodpecker-repair target")
    for needle in (
        "ansible/playbooks/repair-woodpecker.yml",
        "@$(MAKE) platform-service-path-consumers-repair",
        "@$(MAKE) platform-ci-health",
    ):
        require_text(makefile_text, needle, f"platform-woodpecker-repair must cover {needle}")
    woodpecker_repair_target = re.search(
        r"(?m)^platform-woodpecker-repair:\n(?P<body>(?:\t[^\n]*\n)+)",
        makefile_text,
    )
    if not woodpecker_repair_target:
        fail("could not parse platform-woodpecker-repair target body")
    woodpecker_repair_body = woodpecker_repair_target.group("body")
    consumer_refresh = "@$(MAKE) platform-service-path-consumers-repair"
    strict_repair = "ansible/playbooks/repair-woodpecker.yml"
    first_consumer_refresh = woodpecker_repair_body.find(consumer_refresh)
    strict_repair_index = woodpecker_repair_body.find(strict_repair)
    if woodpecker_repair_body.count(consumer_refresh) != 1:
        fail("platform-woodpecker-repair must refresh service-path consumers once after strict repair")
    if not (0 <= strict_repair_index < first_consumer_refresh):
        fail("platform-woodpecker-repair must run strict Woodpecker repair before service-path consumer refresh")
    service_path_consumers_text = read(service_path_consumers_playbook)
    for needle in (
        "Refresh Woodpecker agents after service path repair",
        "Verify Woodpecker gRPC ClusterIP service path before agent refresh from every RKE2 node",
        "Verify Woodpecker gRPC ClusterIP service path after agent refresh from every RKE2 node",
        "Verify Woodpecker gRPC ClusterIP service path from pods pinned to every RKE2 node after agent refresh",
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
        "(platform_woodpecker_grpc_node_probe | default({})).rc",
        "(platform_woodpecker_grpc_pod_probe | default({})).rc",
        "(platform_woodpecker_agent_rollout | default({})).rc",
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
        '{"operation":{"sync":{"revision":"HEAD","prune":false}}}',
        "Wait for Woodpecker server and agents after repair",
        "Verify Woodpecker runtime images and service endpoints after repair",
        "woodpeckerci/woodpecker-server",
        "woodpeckerci/woodpecker-agent",
        "woodpecker-image-tag-mismatch",
        "woodpecker-server-has-no-ready-endpoints",
        "PLATFORM_WOODPECKER_REPAIR_EXPECTED_IMAGE_TAG",
        "PLATFORM_WOODPECKER_REPAIR_TIMEOUT",
        "make platform-woodpecker-repair",
    ):
        require_text(
            woodpecker_repair_text,
            needle,
            f"Woodpecker repair playbook must cover {needle}",
        )
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
        (trivy_config, ("scanners:", "vuln", "secret", "misconfig", "skip-dirs:")),
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
        "scripts/test_sops_age_policy.py",
        "scripts/test_supply_chain_helpers.py",
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
            "scripts/test_sops_age_policy.py",
            "scripts/test_supply_chain_helpers.py",
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
        "scripts/test_sops_age_policy.py",
        "scripts/test_supply_chain_helpers.py",
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
        "verifyImages:",
        "validationFailureAction: Enforce",
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
        "verifyImages:",
        "failureAction: Audit",
        "publicKeys: k8s://<NAMESPACE>/<COSIGN_PUBLIC_KEY_SECRET>",
        "Supply-chain helper validation passed",
    ):
        require_text(
            supply_chain_helpers_test_text,
            needle,
            f"supply-chain helper self-test must cover {needle}",
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
        "optional Cosign/Kyverno verification",
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
            "kind: ClusterPolicy",
            "verifyImages:",
            "imageReferences:",
            '"<REGISTRY>/<PROJECT>/*"',
            "failureAction: Audit",
            "mutateDigest: true",
            "verifyDigest: true",
            "attestors:",
            "publicKeys: k8s://<NAMESPACE>/<COSIGN_PUBLIC_KEY_SECRET>",
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
        "PRUNE_FALSE_RE",
        "SELF_HEAL_TRUE_RE",
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
        "must keep automated prune disabled",
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
    if "RKE2_VERIFY_API_VIP=false $(MAKE) rke2-verify" not in makefile_text:
        fail("platform-bootstrap must run the initial pre-VIP rke2-verify with RKE2_VERIFY_API_VIP=false")
    if "@$(MAKE) rke2-api-vip" not in makefile_text or "@$(MAKE) rke2-verify" not in makefile_text:
        fail("platform-bootstrap must deploy the API VIP and then run the strict rke2-verify gate")
    for target in ("validate", "platform-profile-check", "rke2-verify", "platform-status", "platform-app-health"):
        production_target = re.search(r"(?m)^platform-production-check:.*$", makefile_text)
        if not production_target or target not in production_target.group(0):
            fail(f"platform-production-check must depend on {target}")
    for needle in (
        "def render_loki(",
        "def render_velero(",
        "def render_cnpg_postgres_cluster(",
        "--loki-values",
        "--velero-values",
        "--cnpg-postgres-cluster",
        "LOKI_OBJECT_STORAGE_SECRET_NAME",
        "VELERO_CREDENTIALS_SECRET_NAME",
        "CNPG_OBJECT_STORE_SECRET_NAME",
        "CNPG_RENDER_POSTGRES_CLUSTER",
        "CNPG_BACKUP_ENABLED",
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
        "createPrimary: true",
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
        "portal:\\n  replicas: 1\\n  resources:",
        "registry:\\n  replicas: 1\\n  registry:\\n    resources:",
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
        "prometheusSpec:\\n    replicas: 2\\n    retention: 15d",
        "alertmanagerSpec:\\n    replicas: 3\\n    resources:",
        "grafana:\\n  replicas: 1\\n  admin:",
        "write:\\n  replicas: 3\\n  resources:",
        "read:\\n  replicas: 3\\n  resources:",
        "backend:\\n  replicas: 3\\n  resources:",
        "gateway:\\n  enabled: true\\n  resources:",
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
        "Validate CI actions and container images avoid floating refs",
        "CI_FILES",
        "DOCKERFILES",
        "MUTABLE_REFS",
        "ACTION_SHA_RE",
        "action reference must include @ref",
        "action reference uses floating ref",
        "action reference must pin a full commit SHA",
        "container image must pin a tag or sha256 digest",
        "container image uses floating tag",
        "CI reference pinning validation passed",
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
            "CNPG_BACKUP_ENABLED=false",
            "PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE=false",
            "CNPG_S3_ACCESS_KEY_ID",
            "CNPG_S3_SECRET_ACCESS_KEY",
        ):
            if needle not in env_text:
                fail(f"{env_example.relative_to(root)} must document CloudNativePG private rendering/secret value: {needle}")
        for needle in (
            "FORGEJO_REDIS_MODE=redis",
            "FORGEJO_REDIS_SECRET_NAME=forgejo-redis",
            "PLATFORM_VALKEY_AUTH_SECRET_NAME=platform-valkey-auth",
            "PLATFORM_VALKEY_PASSWORD_KEY=valkey-password",
            "PLATFORM_VALKEY_PRIMARY_HOST=platform-valkey-primary.platform-cache.svc.cluster.local",
            "PLATFORM_VALKEY_REPLICA_COUNT=3",
            "PLATFORM_VALKEY_AUTO_GENERATE=true",
            "HARBOR_REDIS_MODE=external",
            "HARBOR_REDIS_ADDR=platform-valkey-primary.platform-cache.svc.cluster.local:6379",
            "HARBOR_REDIS_SECRET_NAME=harbor-redis",
        ):
            if needle not in env_text:
                fail(f"{env_example.relative_to(root)} must document shared Valkey private rendering/secret value: {needle}")
        for needle in (
            "MINIO_ROOT_SECRET_NAME=minio-root",
            "MINIO_ROOT_USER=platform-admin",
            "MINIO_ROOT_AUTO_GENERATE=true",
            "MINIO_DATA_SIZE=50Gi",
            "MINIO_STORAGE_CLASS=longhorn-critical",
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
