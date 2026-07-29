#!/usr/bin/env python3
"""Validate namespace-only Argo CD services cannot inherit operator powers."""

from __future__ import annotations

import re
from pathlib import Path

from test_gitops_application_contract import (
    APPLICATION_FILES,
    NAMESPACE_ONLY_APPS,
    REQUIRED_NAMESPACE_RESOURCE_BLACKLIST,
    application_documents,
    field,
    project_list_items,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = ROOT / "gitops/clusters/rke2-main/projects/platform-project.yaml"
ACCESS_CONTROL = ROOT / "docs/ACCESS_CONTROL.md"
THREAT_MODEL = ROOT / "docs/THREAT_MODEL.md"
SERVICE_NAMESPACES = {
    "forgejo",
    "harbor",
    "keycloak",
    "object-storage",
    "platform-cache",
    "platform-databases",
    "step-ca",
    "woodpecker",
}
EXPECTED_MANAGED_SERVICES = NAMESPACE_ONLY_APPS - {"gitea", "minio"}


def project_documents() -> dict[str, str]:
    text = PROJECT_FILE.read_text(encoding="utf-8")
    documents: dict[str, str] = {}
    for document in re.split(r"(?m)^---\s*$", text):
        if not re.search(r"(?m)^kind:\s*AppProject\s*$", document):
            continue
        match = re.search(r"(?m)^\s{2}name:\s*([^\s#]+)", document)
        if not match:
            raise AssertionError("AppProject document is missing metadata.name")
        name = match.group(1).strip("'\"")
        if name in documents:
            raise AssertionError(f"duplicate AppProject name: {name}")
        documents[name] = document
    return documents


def item_pairs(document: str, key: str) -> set[tuple[str, str]]:
    return {
        (item.get("group", ""), item.get("kind", ""))
        for item in project_list_items(document, key)
    }


def main() -> int:
    projects = project_documents()
    if set(projects) != {"platform", "platform-services"}:
        raise AssertionError(f"expected platform and platform-services AppProjects, got {sorted(projects)}")

    service_project = projects["platform-services"]
    source_repos = [item.get("value", "") for item in project_list_items(service_project, "sourceRepos")]
    if source_repos != ["<THIS_REPO_URL>"]:
        raise AssertionError("platform-services must trust only <THIS_REPO_URL>")

    destinations = project_list_items(service_project, "destinations")
    destination_namespaces = {item.get("namespace", "") for item in destinations}
    if destination_namespaces != SERVICE_NAMESPACES:
        raise AssertionError(
            "platform-services destination mismatch; "
            f"missing={sorted(SERVICE_NAMESPACES - destination_namespaces)} "
            f"extra={sorted(destination_namespaces - SERVICE_NAMESPACES)}"
        )
    for destination in destinations:
        if destination.get("server") != "https://kubernetes.default.svc":
            raise AssertionError("platform-services may target only the in-cluster API server")

    whitelist = item_pairs(service_project, "clusterResourceWhitelist")
    if whitelist != {("", "Namespace")}:
        raise AssertionError(
            "platform-services must be namespace-only apart from Namespace creation; "
            f"got {sorted(whitelist)}"
        )
    blacklist = item_pairs(service_project, "namespaceResourceBlacklist")
    if blacklist != REQUIRED_NAMESPACE_RESOURCE_BLACKLIST:
        raise AssertionError("platform-services must deny nested Argo CD control-plane resources")
    if "orphanedResources:\n    warn: true" not in service_project:
        raise AssertionError("platform-services must warn on orphaned resources")

    seen_services: set[str] = set()
    for path in APPLICATION_FILES:
        for document in application_documents(path):
            name = field(document, "metadata_name")
            project = field(document, "project")
            expected = "platform-services" if name in NAMESPACE_ONLY_APPS else "platform"
            if project != expected:
                raise AssertionError(
                    f"{path.relative_to(ROOT)}::{name} uses project {project!r}; expected {expected!r}"
                )
            if expected == "platform-services":
                seen_services.add(name)
    if not EXPECTED_MANAGED_SERVICES.issubset(seen_services):
        raise AssertionError(
            "namespace-only service coverage is incomplete: "
            f"{sorted(EXPECTED_MANAGED_SERVICES - seen_services)}"
        )

    for path, required in (
        (ACCESS_CONTROL, "platform-services"),
        (THREAT_MODEL, "namespace-only AppProject"),
    ):
        if required not in path.read_text(encoding="utf-8"):
            raise AssertionError(f"{path.relative_to(ROOT)} is missing {required!r}")

    print(
        "Argo CD project isolation passed: namespace-only services cannot create "
        "cluster RBAC, CRDs, webhooks, or storage classes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
