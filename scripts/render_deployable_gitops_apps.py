#!/usr/bin/env python3
"""Render an Argo CD Application list, skipping apps with placeholders."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")
APPLICATION_NAME_RE = re.compile(
    r"""(?ms)^metadata:\s*\n(?:^\s+.*\n)*?^\s+name:\s*(?P<quote>['"]?)(?P<name>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)
APPLICATION_PATH_RE = re.compile(
    r"""(?m)^\s+path:\s*(?P<quote>['"]?)(?P<path>[^'"\s#]+)(?P=quote)\s*(?:#.*)?$"""
)
VENDORED_PATH_PARTS = {"charts", "crds"}
PROFILE_APP_FILES = {
    "default": "gitops/clusters/rke2-main/platform-apps.yaml",
    "premium-3node": "gitops/clusters/rke2-main/premium-3node/platform-apps.yaml",
}
APP_SYNC_WAVES = {
    "cert-manager": "0",
    "metallb": "0",
    "trust-manager": "1",
    "traefik": "1",
    "ingress-nginx": "1",
    "longhorn": "1",
    "rook-ceph": "1",
    "step-ca": "2",
    "cloudnativepg": "2",
    "argocd-ha": "2",
    "forgejo": "3",
    "gitea": "3",
    "gitlab-ce": "3",
    "monitoring": "3",
    "woodpecker": "4",
    "gitlab-runner": "4",
    "harbor": "4",
    "loki": "4",
    "velero": "5",
}


def unresolved_in_text(text: str) -> list[str]:
    return [match for match in PLACEHOLDER_RE.findall(text) if match != "<THIS_REPO_URL>"]


def display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def parse_simple_profile(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    current_list = ""

    for raw_line in path.read_text(encoding="utf-8").splitlines():
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


def append_unique(values: list[str], additions: list[str]) -> list[str]:
    result = list(values)
    for item in additions:
        if item not in result:
            result.append(item)
    return result


def resolve_profile_entries(repo_root: Path, profile: str, seen: set[str] | None = None) -> tuple[list[str], list[str]]:
    if profile in PROFILE_APP_FILES:
        app_file = repo_root / PROFILE_APP_FILES[profile]
        return [str(path.relative_to(repo_root)).replace("\\", "/") for path in application_source_paths(app_file, repo_root)], []

    seen = seen or set()
    if profile in seen:
        raise ValueError(f"profile {profile!r} has an inheritance cycle")
    seen.add(profile)

    profile_file = repo_root / "profiles" / f"{profile}.yaml"
    if not profile_file.exists():
        raise ValueError(f"unsupported profile {profile!r}")

    scalars, lists = parse_simple_profile(profile_file)
    includes: list[str] = []
    removes: list[str] = []
    inherited = scalars.get("inherits", "")
    if inherited:
        includes, removes = resolve_profile_entries(repo_root, inherited, seen)

    local_includes = lists.get("includes", [])
    local_removes = lists.get("remove", [])
    missing_entries = [
        entry
        for entry in local_includes + local_removes
        if not (repo_root / entry).exists()
    ]
    if missing_entries:
        raise ValueError(
            f"profile {profile!r} references missing path(s): {', '.join(sorted(missing_entries))}"
        )

    includes = append_unique(includes, local_includes)
    removes = append_unique(removes, local_removes)
    includes = [entry for entry in includes if entry not in set(removes)]
    return includes, removes


def profile_dependency_files(repo_root: Path, profile: str, seen: set[str] | None = None) -> list[Path]:
    if profile in PROFILE_APP_FILES:
        return [repo_root / PROFILE_APP_FILES[profile]]

    seen = seen or set()
    if profile in seen:
        raise ValueError(f"profile {profile!r} has an inheritance cycle")
    seen.add(profile)

    profile_file = repo_root / "profiles" / f"{profile}.yaml"
    if not profile_file.exists():
        raise ValueError(f"unsupported profile {profile!r}")

    scalars, _ = parse_simple_profile(profile_file)
    inherited = scalars.get("inherits", "")
    files = profile_dependency_files(repo_root, inherited, seen) if inherited else []
    files.append(profile_file)
    return files


def is_application_source(path: Path) -> bool:
    kustomization = path / "kustomization.yaml"
    if not kustomization.exists():
        return False
    text = kustomization.read_text(encoding="utf-8")
    return "helmCharts:" in text


def application_source_paths(applications_file: Path, repo_root: Path) -> list[Path]:
    text = applications_file.read_text(encoding="utf-8")
    paths: list[Path] = []
    for match in APPLICATION_PATH_RE.finditer(text):
        paths.append(repo_root / match.group("path"))
    return paths


def source_path_string(path: Path, repo_root: Path) -> str:
    return str(path.relative_to(repo_root)).replace("\\", "/")


def application_documents_from_file(applications_file: Path) -> list[str]:
    raw = applications_file.read_text(encoding="utf-8")
    return [doc.strip() for doc in re.split(r"(?m)^---\s*$", raw) if doc.strip()]


def application_doc_source_path(doc: str) -> str:
    path_match = APPLICATION_PATH_RE.search(doc)
    return path_match.group("path") if path_match else ""


def known_application_docs(repo_root: Path) -> dict[str, str]:
    docs: dict[str, str] = {}
    for rel_path in PROFILE_APP_FILES.values():
        app_file = repo_root / rel_path
        if not app_file.exists():
            continue
        for doc in application_documents_from_file(app_file):
            source_path = application_doc_source_path(doc)
            if source_path:
                docs.setdefault(source_path, doc)
    return docs


def kustomization_namespace(source_path: Path) -> str:
    text = (source_path / "kustomization.yaml").read_text(encoding="utf-8")
    namespace_match = re.search(r"(?m)^namespace:\s*([A-Za-z0-9_.-]+)\s*$", text)
    if namespace_match:
        return namespace_match.group(1)
    helm_namespace_match = re.search(r"(?m)^\s+namespace:\s*([A-Za-z0-9_.-]+)\s*$", text)
    if helm_namespace_match:
        return helm_namespace_match.group(1)
    return source_path.name


def generated_application_doc(repo_url: str, source_path: str, source_dir: Path) -> str:
    name = source_dir.name
    namespace = kustomization_namespace(source_dir)
    sync_wave = APP_SYNC_WAVES.get(name, "4")
    return f"""apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "{sync_wave}"
spec:
  project: platform
  source:
    repoURL: {repo_url}
    targetRevision: main
    path: {source_path}
  destination:
    server: https://kubernetes.default.svc
    namespace: {namespace}
  syncPolicy:
    automated:
      prune: false
      selfHeal: true
    syncOptions:
      - CreateNamespace=true"""


def selected_application_documents(repo_root: Path, profile: str, repo_url: str) -> list[str]:
    if profile in PROFILE_APP_FILES:
        return application_documents_from_file(repo_root / PROFILE_APP_FILES[profile])

    includes, _ = resolve_profile_entries(repo_root, profile)
    doc_by_path = known_application_docs(repo_root)
    docs: list[str] = []
    for entry in includes:
        source_dir = repo_root / entry
        if not is_application_source(source_dir):
            continue
        if entry in doc_by_path:
            docs.append(doc_by_path[entry])
        else:
            docs.append(generated_application_doc(repo_url, entry, source_dir))
    return docs


def scan_path(path: Path, repo_root: Path) -> list[str]:
    findings: list[str] = []
    if not path.exists():
        return [f"{display_path(path, repo_root)}: missing application path"]

    files = [path] if path.is_file() else sorted(path.rglob("*"))
    for file_path in files:
        if not file_path.is_file():
            continue
        rel_parts = set(file_path.relative_to(path if path.is_dir() else path.parent).parts)
        if rel_parts & VENDORED_PATH_PARTS:
            continue
        if file_path.suffix not in {".yaml", ".yml"}:
            continue
        if file_path.name.endswith(".example.yaml") or file_path.name.endswith(".example.yml"):
            continue

        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(lines, start=1):
            if unresolved_in_text(line):
                findings.append(
                    f"{display_path(file_path, repo_root)}:{line_number}: {line.strip()}"
                )
    return findings


def render(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    applications_file = args.applications_file.resolve() if args.applications_file else None
    required_findings: list[tuple[Path, list[str]]] = []
    for required_path in args.required_path:
        findings = scan_path((repo_root / required_path).resolve(), repo_root)
        if findings:
            required_findings.append((required_path, findings))

    if required_findings:
        print(
            "Required shared GitOps paths are incomplete and cannot be skipped.",
            file=sys.stderr,
        )
        for required_path, findings in required_findings:
            print(f"- {required_path}", file=sys.stderr)
            for finding in findings[:8]:
                print(f"  {finding}", file=sys.stderr)
            if len(findings) > 8:
                print(f"  ... {len(findings) - 8} more", file=sys.stderr)
        return 1

    if args.profile:
        try:
            profile_findings: list[str] = []
            for profile_file in profile_dependency_files(repo_root, args.profile):
                profile_findings.extend(scan_path(profile_file, repo_root))
            if profile_findings:
                print(
                    f"GitOps profile {args.profile!r} metadata is incomplete and cannot be skipped.",
                    file=sys.stderr,
                )
                for finding in profile_findings[:20]:
                    print(f"- {finding}", file=sys.stderr)
                if len(profile_findings) > 20:
                    print(f"- ... {len(profile_findings) - 20} more", file=sys.stderr)
                return 1
            documents = selected_application_documents(repo_root, args.profile, args.repo_url)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        if applications_file is None:
            print("applications file is required when profile is not set", file=sys.stderr)
            return 1
        documents = application_documents_from_file(applications_file)
    kept: list[str] = []
    skipped: list[tuple[str, list[str]]] = []

    for doc in documents:
        name_match = APPLICATION_NAME_RE.search(doc)
        path_match = APPLICATION_PATH_RE.search(doc)
        name = name_match.group("name") if name_match else "unknown"

        doc_findings = unresolved_in_text(doc)
        if path_match:
            path_findings = scan_path(repo_root / path_match.group("path"), repo_root)
        else:
            path_findings = ["application source path was not found"]

        findings = doc_findings + path_findings
        if findings:
            skipped.append((name, findings))
        else:
            kept.append(doc.replace("<THIS_REPO_URL>", args.repo_url))

    if not kept:
        print(
            "No deployable GitOps applications remain after skipping incomplete apps.",
            file=sys.stderr,
        )
        for name, findings in skipped:
            print(f"- {name}", file=sys.stderr)
            for finding in findings[:8]:
                print(f"  {finding}", file=sys.stderr)
        return 2

    args.output.write_text("---\n" + "\n---\n".join(kept) + "\n", encoding="utf-8")

    print("Deployable GitOps applications:")
    for doc in kept:
        name_match = APPLICATION_NAME_RE.search(doc)
        print(f"- {name_match.group('name') if name_match else 'unknown'}")

    if skipped:
        print()
        print("Skipped incomplete GitOps applications:")
        for name, findings in skipped:
            print(f"- {name}: {len(findings)} unresolved placeholder finding(s)")
            for finding in findings[:5]:
                print(f"  {finding}")
            if len(findings) > 5:
                print(f"  ... {len(findings) - 5} more")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--applications-file", type=Path)
    parser.add_argument("--profile", help="GitOps profile to render. Supports catalog profiles in profiles/.")
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--required-path",
        type=Path,
        action="append",
        default=[],
        help="Shared GitOps path that must be complete and cannot be skipped.",
    )
    args = parser.parse_args()
    if bool(args.applications_file) == bool(args.profile):
        parser.error("provide exactly one of --applications-file or --profile")
    return render(args)


if __name__ == "__main__":
    raise SystemExit(main())
