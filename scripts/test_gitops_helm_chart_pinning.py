#!/usr/bin/env python3
"""Validate remote Kustomize Helm charts are pinned and fully declared."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOT = ROOT / "gitops" / "clusters" / "rke2-main"
SKIP_PARTS = {"charts", "crds"}
MUTABLE_VERSIONS = {"latest", "main", "master", "dev", "edge", "nightly", "snapshot"}
PRERELEASE_RE = re.compile(r"(?:^|[._+-])(alpha|beta|rc)(?:[._+-]?\d*)?$", re.I)
REQUIRED_REMOTE_CHART_FIELDS = ("name", "repo", "version", "releaseName", "namespace", "valuesFile")


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    result: list[str] = []
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and in_double:
            result.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        result.append(char)
    return "".join(result).strip()


def clean_value(value: str) -> str:
    value = strip_inline_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def should_scan(path: Path) -> bool:
    if path.name != "kustomization.yaml":
        return False
    return not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)


def parse_helm_charts(lines: list[str]) -> list[tuple[int, dict[str, str]]]:
    charts: list[tuple[int, dict[str, str]]] = []
    in_helm_charts = False
    helm_indent = 0
    current_line = 0
    current_fields: dict[str, str] | None = None

    def finish_current() -> None:
        nonlocal current_fields, current_line
        if current_fields is not None:
            charts.append((current_line, current_fields))
        current_fields = None
        current_line = 0

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not in_helm_charts:
            if stripped == "helmCharts:":
                in_helm_charts = True
                helm_indent = indent
            continue
        if indent <= helm_indent and not stripped.startswith("- "):
            finish_current()
            in_helm_charts = False
            if stripped == "helmCharts:":
                in_helm_charts = True
                helm_indent = indent
            continue
        if stripped.startswith("- "):
            finish_current()
            current_line = line_number
            current_fields = {}
            field = stripped[2:]
        else:
            if current_fields is None:
                continue
            field = stripped
        if ":" not in field:
            continue
        key, value = field.split(":", 1)
        current_fields[key.strip()] = clean_value(value)
    finish_current()
    return charts


def top_level_namespace(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0 and stripped.startswith("namespace:"):
            return clean_value(stripped.split(":", 1)[1])
    return ""


def is_static_local_path(value: str) -> bool:
    return bool(value) and "{{" not in value and "}}" not in value and "://" not in value and not value.startswith("/")


def find_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    kustomization_namespace = top_level_namespace(lines)
    charts = parse_helm_charts(lines)
    problems: list[str] = []
    for line_number, chart in charts:
        name = chart.get("name", "<unknown>")
        repo = chart.get("repo", "")
        if not repo:
            continue
        for field in REQUIRED_REMOTE_CHART_FIELDS:
            if not chart.get(field, ""):
                problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} must set {field}")
        namespace = chart.get("namespace", "")
        if kustomization_namespace and namespace and namespace != kustomization_namespace:
            problems.append(
                f"{rel_path(path)}:{line_number}: Helm chart {name} namespace {namespace} "
                f"must match kustomization namespace {kustomization_namespace}"
            )
        values_file = chart.get("valuesFile", "")
        if is_static_local_path(values_file) and not (path.parent / values_file).is_file():
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} references missing valuesFile {values_file}")
        version = chart.get("version", "")
        if not version:
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} must pin version")
        elif version.lower() in MUTABLE_VERSIONS:
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} uses mutable version {version}")
        elif PRERELEASE_RE.search(version):
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} uses prerelease version {version}")
    return problems


def main() -> int:
    problems: list[str] = []
    scanned = 0
    for path in sorted(SCAN_ROOT.rglob("kustomization.yaml")):
        if should_scan(path):
            scanned += 1
            problems.extend(find_problems(path))

    if problems:
        print("GitOps Helm chart pinning validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(f"GitOps Helm chart pinning validation passed for {scanned} kustomization files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
