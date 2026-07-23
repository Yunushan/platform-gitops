#!/usr/bin/env python3
"""Validate Argo CD Application declarations against their GitOps app paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_FILES = [
    ROOT / "gitops" / "clusters" / "rke2-main" / "platform-apps.yaml",
    ROOT / "gitops" / "clusters" / "rke2-main" / "premium-3node" / "platform-apps.yaml",
]
PROJECT_FILE = ROOT / "gitops" / "clusters" / "rke2-main" / "projects" / "platform-project.yaml"
APPLICATION_KIND_RE = re.compile(r"(?m)^kind:\s*Application\s*$")
PROJECT_KIND_RE = re.compile(r"(?m)^kind:\s*AppProject\s*$")
MONITORING_BLOCK_RE = re.compile(r"^(?P<indent>\s*)(?P<key>serviceMonitor|podMonitor):\s*(?P<value>.*)$", re.I)
MONITORING_FLAG_RE = re.compile(r"^\s*(?:serviceMonitorEnabled|podMonitorEnabled|enablePodMonitor):\s*true\s*(?:#.*)?$", re.I)
ENABLED_TRUE_RE = re.compile(r"^\s*enabled:\s*true\s*(?:#.*)?$", re.I)
FIELD_PATTERNS = {
    "metadata_name": re.compile(r"(?m)^  name:\s*(?P<value>[^\s#]+)"),
    "metadata_namespace": re.compile(r"(?m)^  namespace:\s*(?P<value>[^\s#]+)"),
    "sync_wave": re.compile(r"(?m)^\s+argocd\.argoproj\.io/sync-wave:\s*(?P<value>[^\s#]+)"),
    "project": re.compile(r"(?m)^  project:\s*(?P<value>[^\s#]+)"),
    "repo_url": re.compile(r"(?m)^\s+repoURL:\s*(?P<value>[^\s#]+)"),
    "target_revision": re.compile(r"(?m)^\s+targetRevision:\s*(?P<value>[^\s#]+)"),
    "source_path": re.compile(r"(?m)^\s+path:\s*(?P<value>[^\s#]+)"),
    "destination_server": re.compile(r"(?m)^\s+server:\s*(?P<value>[^\s#]+)"),
    "destination_namespace": re.compile(r"(?m)^    namespace:\s*(?P<value>[^\s#]+)"),
}
REQUIRED_APP_DEPENDENCIES = {
    "trust-manager": {"cert-manager"},
    "step-ca": {"cert-manager", "trust-manager"},
    "platform-policies": {"kyverno"},
    "tetragon": {"monitoring"},
    "external-secrets": {"monitoring"},
    "openbao": {"longhorn", "monitoring"},
    "traefik": {"metallb"},
    "platform-postgres": {"cloudnativepg", "longhorn"},
    "platform-valkey": {"longhorn", "monitoring"},
    "minio": {"longhorn", "monitoring"},
    "keycloak": {"platform-postgres", "traefik", "monitoring"},
    "forgejo": {"cloudnativepg", "platform-postgres", "platform-valkey", "longhorn", "traefik"},
    "woodpecker": {"forgejo", "traefik"},
    "harbor": {"platform-valkey", "longhorn", "traefik"},
    "monitoring": {"longhorn", "traefik"},
    "loki": {"longhorn", "monitoring", "traefik"},
    "velero": {"longhorn"},
}
SERVER_SIDE_APPLY_APPS = {"kyverno", "velero"}
REQUIRED_CLUSTER_RESOURCE_WHITELIST = {
    ("", "Namespace"),
    ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"),
    ("apiregistration.k8s.io", "APIService"),
    ("cert-manager.io", "ClusterIssuer"),
    ("external-secrets.io", "ClusterExternalSecret"),
    ("external-secrets.io", "ClusterGenerator"),
    ("external-secrets.io", "ClusterPushSecret"),
    ("external-secrets.io", "ClusterSecretStore"),
    ("kyverno.io", "ClusterPolicy"),
    ("networking.k8s.io", "IngressClass"),
    ("rbac.authorization.k8s.io", "ClusterRole"),
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    ("scheduling.k8s.io", "PriorityClass"),
    ("snapshot.storage.k8s.io", "VolumeSnapshotClass"),
    ("storage.k8s.io", "CSIDriver"),
    ("storage.k8s.io", "StorageClass"),
    ("trust.cert-manager.io", "Bundle"),
}
REQUIRED_NAMESPACE_RESOURCE_BLACKLIST = {
    ("argoproj.io", "Application"),
    ("argoproj.io", "ApplicationSet"),
    ("argoproj.io", "AppProject"),
}
REQUIRED_ADDITIONAL_DESTINATION_NAMESPACES = {"kube-system"}
KUSTOMIZE_NAMESPACE_RE = re.compile(r"(?m)^namespace:\s*(?P<value>[^\s#]+)")
HELM_NAMESPACE_RE = re.compile(r"(?m)^\s+namespace:\s*(?P<value>[^\s#]+)")
HELM_VALUES_FILE_RE = re.compile(r"(?m)^\s+valuesFile:\s*(?P<value>[^\s#]+)")
SYNC_OPTION_RE = re.compile(r"(?m)^\s+-\s*(?P<value>[A-Za-z][A-Za-z0-9]+=[^\s#]+)")
AUTOMATED_SYNC_RE = re.compile(r"(?m)^\s+automated:\s*$")
PRUNE_FALSE_RE = re.compile(r"(?m)^\s+prune:\s*false\s*$")
SELF_HEAL_TRUE_RE = re.compile(r"(?m)^\s+selfHeal:\s*true\s*$")
SKIP_SOURCE_PARTS = {"charts", "crds"}
EXAMPLE_SUFFIXES = (".example.yaml", ".example.yml")


def strip_scalar(value: str) -> str:
    return value.strip().strip("'\"")


def project_list_items(text: str, key: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    items: list[dict[str, str]] = []
    in_section = False
    section_indent = 0
    current: dict[str, str] | None = None

    def finish_current() -> None:
        nonlocal current
        if current is not None:
            items.append(current)
        current = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not in_section:
            if stripped == f"{key}:":
                in_section = True
                section_indent = indent
            continue
        if indent <= section_indent:
            break
        if stripped.startswith("- "):
            finish_current()
            current = {}
            field = stripped[2:]
        else:
            if current is None:
                continue
            field = stripped
        if ":" in field:
            item_key, value = field.split(":", 1)
            current[item_key.strip()] = strip_scalar(value)
        elif current is not None:
            current["value"] = strip_scalar(field)
    finish_current()
    return items


def application_documents(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [doc for doc in re.split(r"(?m)^---\s*$", text) if APPLICATION_KIND_RE.search(doc)]


def field(doc: str, name: str) -> str:
    match = FIELD_PATTERNS[name].search(doc)
    return strip_scalar(match.group("value")) if match else ""


def app_label(app_file: Path, app_name: str) -> str:
    return f"{app_file.relative_to(ROOT)}::{app_name or '<missing-name>'}"


def strip_inline_comment(value: str) -> str:
    return value.split("#", 1)[0].strip()


def source_yaml_files(source_path: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(source_path.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            continue
        if set(path.relative_to(source_path).parts) & SKIP_SOURCE_PARTS:
            continue
        if path.name.endswith(EXAMPLE_SUFFIXES):
            continue
        files.append(path)
    return files


def file_enables_monitoring_crd(path: Path) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    active_monitor_block_indent: int | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if active_monitor_block_indent is not None and indent <= active_monitor_block_indent:
            active_monitor_block_indent = None
        if MONITORING_FLAG_RE.match(line):
            return True
        block_match = MONITORING_BLOCK_RE.match(line)
        if block_match:
            value = strip_inline_comment(block_match.group("value"))
            if "enabled: true" in value.lower():
                return True
            active_monitor_block_indent = indent
            continue
        if active_monitor_block_indent is not None and indent > active_monitor_block_indent and ENABLED_TRUE_RE.match(line):
            return True
    return False


def source_enables_monitoring_crds(source_path: Path) -> bool:
    return any(file_enables_monitoring_crd(path) for path in source_yaml_files(source_path))


def check_application_doc(app_file: Path, doc: str) -> list[str]:
    problems: list[str] = []
    app_name = field(doc, "metadata_name")
    label = app_label(app_file, app_name)
    metadata_namespace = field(doc, "metadata_namespace")
    project = field(doc, "project")
    repo_url = field(doc, "repo_url")
    sync_wave = field(doc, "sync_wave")
    target_revision = field(doc, "target_revision")
    source_path = field(doc, "source_path")
    destination_server = field(doc, "destination_server")
    destination_namespace = field(doc, "destination_namespace")
    sync_options = set(SYNC_OPTION_RE.findall(doc))

    expected_values = {
        "metadata namespace": (metadata_namespace, "argocd"),
        "project": (project, "platform"),
        "repoURL": (repo_url, "<THIS_REPO_URL>"),
        "targetRevision": (target_revision, "main"),
        "destination server": (destination_server, "https://kubernetes.default.svc"),
    }
    for description, (actual, expected) in expected_values.items():
        if actual != expected:
            problems.append(f"{label} has unexpected {description}: {actual or '<missing>'}")
    if not sync_wave:
        problems.append(f"{label} is missing annotation argocd.argoproj.io/sync-wave")
    else:
        try:
            int(sync_wave)
        except ValueError:
            problems.append(f"{label} has non-numeric sync wave: {sync_wave}")
    if "CreateNamespace=true" not in sync_options:
        problems.append(f"{label} is missing sync option CreateNamespace=true")
    if app_name in SERVER_SIDE_APPLY_APPS and "ServerSideApply=true" not in sync_options:
        problems.append(f"{label} must use server-side apply for large chart CRDs")
    if not AUTOMATED_SYNC_RE.search(doc):
        problems.append(f"{label} is missing automated sync policy")
    if not PRUNE_FALSE_RE.search(doc):
        problems.append(f"{label} must keep automated prune disabled")
    if not SELF_HEAL_TRUE_RE.search(doc):
        problems.append(f"{label} must keep automated selfHeal enabled")

    if not source_path:
        problems.append(f"{label} is missing source path")
        return problems
    app_path = ROOT / source_path
    if not app_path.exists():
        problems.append(f"{label} source path does not exist: {source_path}")
        return problems
    kustomization = app_path / "kustomization.yaml"
    if not kustomization.exists():
        problems.append(f"{label} source path is missing kustomization.yaml: {source_path}")
        return problems

    kustomization_text = kustomization.read_text(encoding="utf-8")
    kustomize_namespace_match = KUSTOMIZE_NAMESPACE_RE.search(kustomization_text)
    helm_namespace_match = HELM_NAMESPACE_RE.search(kustomization_text)
    kustomize_namespace = strip_scalar(kustomize_namespace_match.group("value")) if kustomize_namespace_match else ""
    helm_namespace = strip_scalar(helm_namespace_match.group("value")) if helm_namespace_match else ""
    if kustomize_namespace != destination_namespace:
        problems.append(
            f"{label} destination namespace {destination_namespace or '<missing>'} "
            f"does not match kustomization namespace {kustomize_namespace or '<missing>'}"
        )
    if helm_namespace and helm_namespace != destination_namespace:
        problems.append(
            f"{label} destination namespace {destination_namespace or '<missing>'} "
            f"does not match Helm namespace {helm_namespace}"
        )

    for values_match in HELM_VALUES_FILE_RE.finditer(kustomization_text):
        values_file = strip_scalar(values_match.group("value"))
        if values_file and not (app_path / values_file).exists():
            problems.append(f"{label} references missing Helm values file: {source_path}/{values_file}")
    return problems


def sync_wave_for(app_file: Path, app_name: str, doc: str, problems: list[str]) -> int | None:
    sync_wave = field(doc, "sync_wave")
    if not sync_wave:
        return None
    try:
        return int(sync_wave)
    except ValueError:
        problems.append(f"{app_label(app_file, app_name)} has non-numeric sync wave: {sync_wave}")
        return None


def check_dependency_waves(app_file: Path, docs_by_name: dict[str, str]) -> list[str]:
    problems: list[str] = []
    waves: dict[str, int] = {}
    for app_name, doc in docs_by_name.items():
        wave = sync_wave_for(app_file, app_name, doc, problems)
        if wave is not None:
            waves[app_name] = wave

    for app_name, dependencies in REQUIRED_APP_DEPENDENCIES.items():
        if app_name not in docs_by_name:
            continue
        app_wave = waves.get(app_name)
        if app_wave is None:
            continue
        for dependency in sorted(dependencies):
            if dependency not in docs_by_name:
                continue
            dependency_wave = waves.get(dependency)
            if dependency_wave is None:
                continue
            if dependency_wave >= app_wave:
                problems.append(
                    f"{app_label(app_file, app_name)} sync wave {app_wave} "
                    f"must be after dependency {dependency} wave {dependency_wave}"
                )
    return problems


def check_inferred_monitoring_dependencies(app_file: Path, docs_by_name: dict[str, str]) -> list[str]:
    problems: list[str] = []
    if "monitoring" not in docs_by_name:
        return problems
    monitoring_wave = sync_wave_for(app_file, "monitoring", docs_by_name["monitoring"], problems)
    if monitoring_wave is None:
        return problems

    for app_name, doc in docs_by_name.items():
        if app_name == "monitoring":
            continue
        source_path = field(doc, "source_path")
        if not source_path:
            continue
        app_path = ROOT / source_path
        if not app_path.exists() or not source_enables_monitoring_crds(app_path):
            continue
        app_wave = sync_wave_for(app_file, app_name, doc, problems)
        if app_wave is None:
            continue
        if monitoring_wave >= app_wave:
            problems.append(
                f"{app_label(app_file, app_name)} enables ServiceMonitor/PodMonitor resources "
                f"but sync wave {app_wave} is not after monitoring wave {monitoring_wave}"
            )
    return problems


def application_destination_namespaces() -> set[str]:
    namespaces: set[str] = set()
    for app_file in APPLICATION_FILES:
        if not app_file.exists():
            continue
        for doc in application_documents(app_file):
            namespace = field(doc, "destination_namespace")
            if namespace:
                namespaces.add(namespace)
    return namespaces


def check_project_contract() -> list[str]:
    problems: list[str] = []
    if not PROJECT_FILE.exists():
        return [f"missing AppProject file: {PROJECT_FILE.relative_to(ROOT)}"]

    text = PROJECT_FILE.read_text(encoding="utf-8")
    label = PROJECT_FILE.relative_to(ROOT)
    if not PROJECT_KIND_RE.search(text):
        problems.append(f"{label} must define kind: AppProject")
    if not re.search(r"(?m)^  name:\s*platform\s*$", text):
        problems.append(f"{label} must use metadata.name platform")
    if not re.search(r"(?m)^  namespace:\s*argocd\s*$", text):
        problems.append(f"{label} must live in namespace argocd")

    source_repos = [item.get("value", "") for item in project_list_items(text, "sourceRepos")]
    if source_repos != ["<THIS_REPO_URL>"]:
        problems.append(f"{label} sourceRepos must be exactly <THIS_REPO_URL>, got {source_repos or '<none>'}")
    if "*" in source_repos:
        problems.append(f"{label} sourceRepos must not allow wildcard repositories")

    expected_namespaces = application_destination_namespaces() | REQUIRED_ADDITIONAL_DESTINATION_NAMESPACES
    destinations = project_list_items(text, "destinations")
    destination_namespaces = {item.get("namespace", "") for item in destinations if item.get("namespace")}
    wildcard_namespaces = sorted(namespace for namespace in destination_namespaces if "*" in namespace)
    if wildcard_namespaces:
        problems.append(f"{label} destinations must not include wildcard namespaces: {', '.join(wildcard_namespaces)}")
    if destination_namespaces != expected_namespaces:
        missing = sorted(expected_namespaces - destination_namespaces)
        extra = sorted(destination_namespaces - expected_namespaces)
        problems.append(
            f"{label} destinations must match Application namespaces; "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    for item in destinations:
        if item.get("server") != "https://kubernetes.default.svc":
            problems.append(
                f"{label} destination {item.get('namespace', '<missing>')} has unexpected server "
                f"{item.get('server', '<missing>')}"
            )

    whitelist = project_list_items(text, "clusterResourceWhitelist")
    actual_whitelist = {(item.get("group", ""), item.get("kind", "")) for item in whitelist}
    if ("*", "*") in actual_whitelist:
        problems.append(f"{label} clusterResourceWhitelist must not allow */*")
    if actual_whitelist != REQUIRED_CLUSTER_RESOURCE_WHITELIST:
        missing = sorted(REQUIRED_CLUSTER_RESOURCE_WHITELIST - actual_whitelist)
        extra = sorted(actual_whitelist - REQUIRED_CLUSTER_RESOURCE_WHITELIST)
        problems.append(
            f"{label} clusterResourceWhitelist must match the explicit production allowlist; "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )

    namespace_blacklist = project_list_items(text, "namespaceResourceBlacklist")
    actual_namespace_blacklist = {(item.get("group", ""), item.get("kind", "")) for item in namespace_blacklist}
    if ("*", "*") in actual_namespace_blacklist:
        problems.append(f"{label} namespaceResourceBlacklist must not deny */*")
    if actual_namespace_blacklist != REQUIRED_NAMESPACE_RESOURCE_BLACKLIST:
        missing = sorted(REQUIRED_NAMESPACE_RESOURCE_BLACKLIST - actual_namespace_blacklist)
        extra = sorted(actual_namespace_blacklist - REQUIRED_NAMESPACE_RESOURCE_BLACKLIST)
        problems.append(
            f"{label} namespaceResourceBlacklist must deny child Argo CD control-plane resources; "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    if not re.search(r"(?ms)^  orphanedResources:\n    warn:\s*true\s*$", text):
        problems.append(f"{label} must warn on orphaned resources")
    return problems


def main() -> int:
    problems: list[str] = check_project_contract()
    for app_file in APPLICATION_FILES:
        if not app_file.exists():
            problems.append(f"missing Application file: {app_file.relative_to(ROOT)}")
            continue
        docs = application_documents(app_file)
        if not docs:
            problems.append(f"{app_file.relative_to(ROOT)} does not define any Argo CD Applications")
            continue
        seen_names: set[str] = set()
        docs_by_name: dict[str, str] = {}
        for doc in docs:
            app_name = field(doc, "metadata_name")
            if not app_name:
                problems.append(f"{app_file.relative_to(ROOT)} has an Application without metadata.name")
            elif app_name in seen_names:
                problems.append(f"{app_file.relative_to(ROOT)} repeats Application name: {app_name}")
            else:
                docs_by_name[app_name] = doc
            seen_names.add(app_name)
            problems.extend(check_application_doc(app_file, doc))
        problems.extend(check_dependency_waves(app_file, docs_by_name))
        problems.extend(check_inferred_monitoring_dependencies(app_file, docs_by_name))

    if problems:
        print("GitOps Application contract validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    print(f"GitOps Application contract validation passed for {len(APPLICATION_FILES)} app files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
