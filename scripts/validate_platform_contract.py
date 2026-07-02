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

premium_apps = root / "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml"
base_apps = root / "gitops/clusters/rke2-main/platform-apps.yaml"
base_woodpecker_values = root / "gitops/clusters/rke2-main/apps/woodpecker/values.yaml"
premium_woodpecker_values = root / "gitops/clusters/rke2-main/premium-3node/apps/woodpecker/values.yaml"
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

required_premium_apps = [
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

required_gui_hosts = [
    "argocd",
    "forgejo",
    "harbor",
    "woodpecker",
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
    if needle not in text:
        fail(description)


def reject_text(text: str, needle: str, description: str) -> None:
    if needle in text:
        fail(description)


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

    assert_app_file(base_apps, required_premium_apps)
    assert_app_file(premium_apps, required_premium_apps)

    base_woodpecker_text = read(base_woodpecker_values)
    premium_woodpecker_text = read(premium_woodpecker_values)
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
        "3.16.0",
        "default Woodpecker profile",
    )
    require_woodpecker_role_image_pin(
        base_woodpecker_text,
        "agent",
        "woodpeckerci/woodpecker-agent",
        "3.16.0",
        "default Woodpecker profile",
    )
    require_text(
        premium_woodpecker_text,
        "replicaCount: 2",
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
    require_woodpecker_role_image_pin(
        premium_woodpecker_text,
        "server",
        "woodpeckerci/woodpecker-server",
        "3.16.0",
        "premium Woodpecker profile",
    )
    require_woodpecker_role_image_pin(
        premium_woodpecker_text,
        "agent",
        "woodpeckerci/woodpecker-agent",
        "3.16.0",
        "premium Woodpecker profile",
    )

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
        "PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG=3.16.0",
        'expected_tag="{{ platform_app_health_woodpecker_image_tag_effective }}"',
    ):
        require_text(
            health_text,
            needle,
            f"platform-app-health must enforce Woodpecker runtime image tag drift: {needle}",
        )
    require_text(
        health_text,
        "generated Harbor/Woodpecker/Loki/Velero app secrets exist with required keys",
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
        "WOODPECKER_FORGEJO_CLIENT",
        "WOODPECKER_FORGEJO_SECRET",
        "WOODPECKER_DATABASE_DATASOURCE",
        "platform_woodpecker_database_secret_name_effective",
        "LOKI_S3_ACCESS_KEY_ID",
        "LOKI_S3_SECRET_ACCESS_KEY",
        "VELERO_CREDENTIALS_SECRET_NAME",
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
    require_text(
        bootstrap_argocd_text,
        "scripts/check_gitops_profile.py",
        "Argo CD bootstrap must use the same profile checker as platform-profile-check",
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
        r"(?ms)^platform-woodpecker-repair:\n(?P<body>(?:\t.*\n)+)",
        makefile_text,
    )
    if not woodpecker_repair_target:
        fail("could not parse platform-woodpecker-repair target body")
    woodpecker_repair_body = woodpecker_repair_target.group("body")
    consumer_refresh = "@$(MAKE) platform-service-path-consumers-repair"
    strict_repair = "ansible/playbooks/repair-woodpecker.yml"
    first_consumer_refresh = woodpecker_repair_body.find(consumer_refresh)
    strict_repair_index = woodpecker_repair_body.find(strict_repair)
    last_consumer_refresh = woodpecker_repair_body.rfind(consumer_refresh)
    if woodpecker_repair_body.count(consumer_refresh) < 2:
        fail("platform-woodpecker-repair must refresh service-path consumers before and after strict repair")
    if not (0 <= first_consumer_refresh < strict_repair_index < last_consumer_refresh):
        fail("platform-woodpecker-repair must run service-path consumer refresh before strict Woodpecker repair")
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
        "scripts/test_ansible_no_log_contract.py",
        "project validator must require the Ansible no_log contract self-test",
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
        "scripts/run_validation.py",
        "scripts/validate_no_secrets.py",
    ):
        require_text(
            validation_runner_test_text,
            needle,
            f"validation runner self-test must cover {needle}",
        )
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
        "strict",
        "skip-incomplete",
        "Unsupported PLATFORM_PROFILE",
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
        "--loki-values",
        "--velero-values",
        "LOKI_OBJECT_STORAGE_SECRET_NAME",
        "VELERO_CREDENTIALS_SECRET_NAME",
        "WOODPECKER_DATABASE_MODE",
        "WOODPECKER_DATABASE_SECRET_NAME",
        "WOODPECKER_IMAGE_TAG",
        "woodpeckerci/woodpecker-server",
        "woodpeckerci/woodpecker-agent",
        "WOODPECKER_SERVER_REPLICAS",
        "WOODPECKER_DATABASE_DATASOURCE",
        "default_server_replicas",
        "WOODPECKER_SERVER_REPLICAS must be 1 when WOODPECKER_DATABASE_MODE=sqlite",
        "WOODPECKER_IMAGE_TAG must be a stable release tag",
        "OBJECT_STORAGE_ENDPOINT",
        "BACKUP_BUCKET",
        "crds:\n  enabled: true",
    ):
        require_text(renderer_text, needle, f"private values renderer must cover {needle}")
    for needle in (
        "render_loki",
        "render_velero",
        "render_real_premium_profile",
        "shutil.copytree",
        'check_profile(repo, "premium-3node")',
        "assert_no_placeholders",
        "platform-test-loki-chunks",
        "platform-test-velero",
        "WOODPECKER_DATABASE_MODE",
        "woodpecker-db-test",
        'WOODPECKER_DATABASE_DRIVER: "postgres"',
        "replicaCount: 1",
        "repository: woodpeckerci/woodpecker-server",
        "repository: woodpeckerci/woodpecker-agent",
        'tag: "3.16.0"',
        "SQLite-backed Woodpecker accepted multiple server replicas",
        "Woodpecker renderer accepted a mutable image tag",
        "${LOKI_S3_ACCESS_KEY_ID}",
        "${LOKI_S3_SECRET_ACCESS_KEY}",
    ):
        require_text(renderer_test_text, needle, f"private values renderer self-test must cover {needle}")
    for needle in (
        "CONTRACTS",
        "HARBOR_ADMIN_SECRET_NAME",
        "HARBOR_SECRET_KEY_SECRET_NAME",
        "WOODPECKER_FORGEJO_OAUTH_SECRET_NAME",
        "WOODPECKER_DATABASE_SECRET_NAME",
        "WOODPECKER_DATABASE_DATASOURCE",
        "LOKI_OBJECT_STORAGE_SECRET_NAME",
        "VELERO_CREDENTIALS_SECRET_NAME",
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
        "action reference must include @ref",
        "action reference uses floating ref",
        "container image must pin a tag or sha256 digest",
        "container image uses floating tag",
        "CI reference pinning validation passed",
    ):
        require_text(
            ci_reference_pinning_test_text,
            needle,
            f"CI reference pinning self-test must cover {needle}",
        )
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
        "Generate or preserve Harbor bootstrap secrets",
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
        "Generate or preserve Loki object storage credentials secret",
        "Generate or preserve Velero cloud credentials secret",
        "Generate or preserve Woodpecker database datasource secret",
        "Check Loki object storage credentials secret state",
        "Check Velero cloud credentials secret state",
        "Check Woodpecker database datasource secret state",
        "Require object storage credentials secrets when enabled",
        "Require Woodpecker database datasource secret when enabled",
        "PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE",
        "PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE",
        "WOODPECKER_DATABASE_DATASOURCE",
        "WOODPECKER_DATABASE_HOST",
        "WOODPECKER_DATABASE_PASSWORD",
        "from urllib.parse import quote",
        "quote(password, safe='')",
        "LOKI_S3_ACCESS_KEY_ID",
        "LOKI_S3_SECRET_ACCESS_KEY",
        "VELERO_CLOUD_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
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
        if "WOODPECKER_IMAGE_TAG=3.16.0" not in env_text:
            fail(f"{env_example.relative_to(root)} must document the pinned Woodpecker image tag")
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
