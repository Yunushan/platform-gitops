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
stale_premium_root_app = root / "gitops/bootstrap/root-app-premium-3node.yaml"
health_playbook = root / "ansible/playbooks/verify-platform-app-health.yml"
service_path_consumers_playbook = root / "ansible/playbooks/repair-platform-service-path-consumers.yml"
status_playbook = root / "ansible/playbooks/platform-status.yml"
profile_check_script = root / "scripts/check_gitops_profile.py"
profile_check_test = root / "scripts/test_profile_checker.py"
deployable_renderer = root / "scripts/render_deployable_gitops_apps.py"
deployable_renderer_test = root / "scripts/test_deployable_renderer.py"
private_values_renderer = root / "scripts/render_private_platform_values.py"
private_values_renderer_test = root / "scripts/test_private_values_renderer.py"
no_secrets_test = root / "scripts/test_no_secrets.py"
no_secrets_script = root / "scripts/validate_no_secrets.py"
shell_syntax_test = root / "scripts/test_shell_syntax.py"
docs_make_targets_test = root / "scripts/test_docs_make_targets.py"
ansible_playbook_references_test = root / "scripts/test_ansible_playbook_references.py"
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


def require_unique_words(values: list[str], label: str) -> None:
    duplicate_values = sorted({value for value in values if values.count(value) > 1})
    if duplicate_values:
        fail(f"{label} contains duplicate entries: {', '.join(duplicate_values)}")


def required_destination_namespaces(*paths: Path) -> list[str]:
    namespaces: set[str] = set()
    for path in paths:
        for doc in application_documents(path):
            if doc["name"] in required_premium_apps:
                namespaces.add(doc["namespace"])
    return sorted(namespaces)


def main() -> None:
    if stale_premium_root_app.exists():
        fail(f"stale premium root app file still exists: {stale_premium_root_app.relative_to(root)}")

    assert_app_file(base_apps, required_premium_apps)
    assert_app_file(premium_apps, required_premium_apps)

    health_text = read(health_playbook)
    health_apps = extract_default_word_list("platform_app_health_required_apps_effective", health_text)
    require_unique_words(health_apps, "platform-app-health required app defaults")
    missing_health_apps = sorted(set(required_premium_apps) - set(health_apps))
    if missing_health_apps:
        fail(f"platform-app-health does not enforce apps: {', '.join(missing_health_apps)}")
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
    if "PLATFORM_APP_HEALTH_HTTP_REDIRECT" not in health_text:
        fail("platform-app-health must expose HTTP-to-HTTPS redirect enforcement")
    if "platform_app_health_http_redirect_effective" not in health_text:
        fail("platform-app-health must default HTTP redirect enforcement through an effective variable")
    for task_name in (
        "Verify Argo CD platform application health",
        "Verify platform namespace pod readiness",
        "Verify required platform StorageClasses",
        "Verify platform namespace PVC readiness",
        "Verify configured GUI ingress backend endpoints",
        "Probe configured GUI app ingress from Ansible controller",
        "Probe configured GUI HTTP redirects from Ansible controller",
        "Probe configured GUI app ingress from every RKE2 node",
        "Probe Argo CD and Woodpecker ClusterIP service paths from every RKE2 node",
        "Probe Argo CD and Woodpecker ClusterIP service paths from pods pinned to every RKE2 node",
        "Stop when platform app health checks fail",
    ):
        require_text(health_text, f"- name: {task_name}", f"platform-app-health is missing task: {task_name}")
    for result_name in (
        "platform_app_health_argocd_app_probe",
        "platform_app_health_pod_probe",
        "platform_app_health_storage_class_probe",
        "platform_app_health_pvc_probe",
        "platform_app_health_ingress_backend_probe",
        "platform_app_health_controller_ingress_probe",
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
        "required StorageClasses exist",
        "platform-app-health success message must include StorageClass readiness",
    )
    require_text(
        health_text,
        "driver.longhorn.io",
        "platform-app-health must verify Longhorn StorageClass provisioners",
    )
    require_text(
        health_text,
        "ready backend endpoints",
        "platform-app-health must verify GUI ingress backend endpoints",
    )
    require_text(
        health_text,
        "no-ingress-or-ingressroute-for-host",
        "platform-app-health must fail when a GUI host has no ingress route",
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
        "Production readiness: NOT READY",
        "platform-status must clearly warn when app sync/health is not production-ready",
    )
    require_text(
        status_text,
        "Run: make platform-app-health",
        "platform-status must point failed app readiness to the app health gate",
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
    renderer_text = read(private_values_renderer)
    renderer_test_text = read(private_values_renderer_test)
    app_secrets_text = read(app_secrets_playbook)
    bootstrap_argocd_text = read(root / "ansible/playbooks/bootstrap-argocd.yml")
    require_text(
        profile_check_text,
        "unresolved placeholders",
        "profile check script must fail unresolved GitOps placeholders",
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
    service_path_consumers_text = read(service_path_consumers_playbook)
    for needle in (
        "Refresh Woodpecker agents after service path repair",
        "Verify Woodpecker gRPC ClusterIP service path before agent refresh from every RKE2 node",
        "Verify Woodpecker gRPC ClusterIP service path after agent refresh from every RKE2 node",
        "statefulset/woodpecker-agent",
        "woodpecker-server",
        "/dev/tcp/${svc_ip}/9000",
        "PLATFORM_SERVICE_PATH_CONSUMER_REPAIR_TIMEOUT",
    ):
        require_text(
            service_path_consumers_text,
            needle,
            f"service-path consumer repair playbook must cover {needle}",
        )
    gitignore_text = read(gitignore_file)
    for needle in ("__pycache__/", ".shell-syntax-*/", ".venv/", ".pytest_cache/", "*.pyc"):
        require_text(gitignore_text, needle, f".gitignore must ignore generated validation/cache artifacts: {needle}")
    validate_project_text = read(root / "scripts/validate_project.py")
    for needle in ("conflict_marker_re", "Git conflict markers found"):
        require_text(
            validate_project_text,
            needle,
            f"project validator must detect unresolved merge conflicts: {needle}",
        )
    require_text(
        validate_project_text,
        "scripts/test_shell_syntax.py",
        "project validator must require the shell syntax self-test",
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
    if "scripts/test_profile_checker.py" not in makefile_text:
        fail("validate target must run the profile checker self-test")
    if "scripts/test_deployable_renderer.py" not in makefile_text:
        fail("validate target must run the deployable renderer self-test")
    if "scripts/test_private_values_renderer.py" not in makefile_text:
        fail("validate target must run the private values renderer self-test")
    if "scripts/test_no_secrets.py" not in makefile_text:
        fail("validate target must run the secret/privacy scanner self-test")
    if "scripts/test_shell_syntax.py" not in makefile_text:
        fail("validate target must run the shell syntax self-test")
    if "scripts/test_docs_make_targets.py" not in makefile_text:
        fail("validate target must run the documented make target self-test")
    if "scripts/test_ansible_playbook_references.py" not in makefile_text:
        fail("validate target must run the Ansible playbook reference self-test")
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
            "scripts/test_profile_checker.py",
            f"{bootstrap_script} must run the profile checker self-test before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "scripts/test_deployable_renderer.py",
            f"{bootstrap_script} must run the deployable renderer self-test before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "scripts/test_private_values_renderer.py",
            f"{bootstrap_script} must run the private values renderer self-test before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "scripts/test_no_secrets.py",
            f"{bootstrap_script} must run the secret/privacy scanner self-test before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "scripts/test_shell_syntax.py",
            f"{bootstrap_script} must run the shell syntax self-test before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "scripts/test_docs_make_targets.py",
            f"{bootstrap_script} must run the documented make target self-test before pushing rendered values",
        )
        require_text(
            bootstrap_script_text,
            "scripts/test_ansible_playbook_references.py",
            f"{bootstrap_script} must run the Ansible playbook reference self-test before pushing rendered values",
        )
    if 'PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-true}"' not in read(root / "scripts/bootstrap/private-first-deploy.sh"):
        fail("private-first-deploy must allow private hostnames by default while keeping secret scanning enabled")
    if 'PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-true}"' not in read(root / "scripts/bootstrap/seed-first-deploy.sh"):
        fail("seed-first-deploy must allow private hostnames by default while keeping secret scanning enabled")
    if 'PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES="${PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES:-false}"' not in read(root / "scripts/bootstrap/sync-seed-git.sh"):
        fail("seed sync must keep public-template hostname leakage detection enabled by default")
    for ci_file in ci_validation_files:
        ci_text = read(ci_file)
        for script_name in (
            "scripts/validate_project.py",
            "scripts/test_profile_checker.py",
            "scripts/test_deployable_renderer.py",
            "scripts/test_private_values_renderer.py",
            "scripts/test_no_secrets.py",
            "scripts/test_shell_syntax.py",
            "scripts/test_docs_make_targets.py",
            "scripts/test_ansible_playbook_references.py",
            "scripts/validate_platform_contract.py",
            "scripts/validate_no_secrets.py",
        ):
            require_text(
                ci_text,
                script_name,
                f"{ci_file.relative_to(root)} must run {script_name}",
            )
    for needle in (
        "tempfile",
        "premium-3node",
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
        "referenced playbook",
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
    if "ansible/playbooks/verify-platform-app-health.yml" not in makefile_text:
        fail("platform-app-health target does not invoke the health playbook")
    if "platform-production-check:" not in makefile_text:
        fail("Makefile is missing platform-production-check target")
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
        "${LOKI_S3_ACCESS_KEY_ID}",
        "${LOKI_S3_SECRET_ACCESS_KEY}",
    ):
        require_text(renderer_test_text, needle, f"private values renderer self-test must cover {needle}")
    no_secrets_test_text = read(no_secrets_test)
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
    ):
        require_text(shell_syntax_test_text, needle, f"shell syntax self-test must cover {needle}")
    for needle in (
        "Generate or preserve Loki object storage credentials secret",
        "Generate or preserve Velero cloud credentials secret",
        "Check Loki object storage credentials secret state",
        "Check Velero cloud credentials secret state",
        "Require object storage credentials secrets when enabled",
        "PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE",
        "LOKI_S3_ACCESS_KEY_ID",
        "LOKI_S3_SECRET_ACCESS_KEY",
        "VELERO_CLOUD_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        require_text(app_secrets_text, needle, f"platform app secret automation must cover {needle}")

    for doc in (installation_doc, premium_doc, troubleshooting_doc):
        doc_text = read(doc)
        if doc != troubleshooting_doc and "make platform-app-health" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the platform app health gate")
        if doc != troubleshooting_doc and "make platform-profile-check" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the GitOps profile check")
        if "make platform-production-check" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document the production readiness gate")
        if "PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document node-originated VIP strict mode")
        if "PLATFORM_APP_HEALTH_GUI_APPS" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document GUI app filtering for subset profiles")
        if "PLATFORM_APP_HEALTH_HTTP_REDIRECT" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document HTTP redirect enforcement")
        if "PLATFORM_APP_HEALTH_STORAGE_CLASSES" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document StorageClass enforcement")
        if "PVC" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document PVC readiness")
        if "PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document production object-storage secret enforcement")
        if "backend" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document GUI backend endpoint readiness")
        if "make platform-service-path-repair" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document ClusterIP service-path repair")
        if "Woodpecker gRPC ClusterIP from every RKE2 node" not in doc_text:
            fail(f"{doc.relative_to(root)} does not document Woodpecker consumer refresh after service-path repair")
    for doc in (installation_doc, root / "docs/PRIVATE_DEPLOYMENT.md"):
        if "PLATFORM_RUN_PROFILE_CHECK" not in read(doc):
            fail(f"{doc.relative_to(root)} must document selected GitOps profile validation before push")
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
