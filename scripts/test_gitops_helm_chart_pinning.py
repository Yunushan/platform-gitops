#!/usr/bin/env python3
"""Validate Kustomize Helm charts are local or pinned and fully declared."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from vendored_chart_inventory import DEFAULT_INVENTORY, validate_inventory


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOT = ROOT / "gitops" / "clusters" / "rke2-main"
PREMIUM_ROOT = SCAN_ROOT / "premium-3node"
SKIP_PARTS = {"charts", "crds"}
MUTABLE_VERSIONS = {"latest", "main", "master", "dev", "edge", "nightly", "snapshot"}
PRERELEASE_RE = re.compile(r"(?:^|[._+-])(alpha|beta|rc)(?:[._+-]?\d*)?$", re.I)
REQUIRED_REMOTE_CHART_FIELDS = ("name", "repo", "version", "releaseName", "namespace", "valuesFile")
REQUIRED_LOCAL_CHART_FIELDS = ("name", "releaseName", "namespace", "valuesFile")


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


def helm_chart_home(lines: list[str]) -> str:
    in_helm_globals = False
    globals_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not in_helm_globals:
            if stripped == "helmGlobals:":
                in_helm_globals = True
                globals_indent = indent
            continue
        if indent <= globals_indent:
            return ""
        if stripped.startswith("chartHome:"):
            return clean_value(stripped.split(":", 1)[1])
    return ""


def chart_metadata(path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) != len(line.lstrip(" ")) or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key in {"name", "version"}:
            metadata[key] = clean_value(value)
    return metadata


def is_static_local_path(value: str) -> bool:
    if not value or "{{" in value or "}}" in value or "://" in value or "\\" in value:
        return False
    parsed = PurePosixPath(value)
    return not parsed.is_absolute() and ".." not in parsed.parts


def matching_vendored_charts(path: Path, name: str, version: str) -> list[Path]:
    charts_root = path.parent / "charts"
    if not charts_root.is_dir() or not name or not version:
        return []
    matches: list[Path] = []
    for chart_yaml in charts_root.rglob("Chart.yaml"):
        parts = chart_yaml.relative_to(charts_root).parts
        if len(parts) not in {2, 3} or chart_yaml.parent.name != name:
            continue
        metadata = chart_metadata(chart_yaml)
        if metadata.get("name") == name and metadata.get("version") == version:
            matches.append(chart_yaml.parent)
    return sorted(matches)


def consumed_local_chart_paths(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    chart_home = helm_chart_home(lines)
    if not is_static_local_path(chart_home):
        return set()
    consumed: set[str] = set()
    for _line_number, chart in parse_helm_charts(lines):
        name = chart.get("name", "")
        if chart.get("repo") or not name:
            continue
        parsed_name = PurePosixPath(name)
        if parsed_name.is_absolute() or len(parsed_name.parts) != 1 or "\\" in name:
            continue
        chart_path = path.parent / chart_home / name
        consumed.add(chart_path.relative_to(ROOT).as_posix())
    return consumed


def find_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    kustomization_namespace = top_level_namespace(lines)
    chart_home = helm_chart_home(lines)
    charts = parse_helm_charts(lines)
    problems: list[str] = []
    for line_number, chart in charts:
        name = chart.get("name", "<unknown>")
        repo = chart.get("repo", "")
        required_fields = REQUIRED_REMOTE_CHART_FIELDS if repo else REQUIRED_LOCAL_CHART_FIELDS
        for field in required_fields:
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
        if not repo:
            if not is_static_local_path(chart_home):
                problems.append(
                    f"{rel_path(path)}:{line_number}: local Helm chart {name} "
                    "must use a static, non-escaping helmGlobals.chartHome"
                )
                continue
            chart_path = path.parent / chart_home / name
            chart_yaml = chart_path / "Chart.yaml"
            if not chart_yaml.is_file():
                problems.append(
                    f"{rel_path(path)}:{line_number}: local Helm chart {name} "
                    f"references missing {rel_path(chart_yaml)}"
                )
                continue
            metadata = chart_metadata(chart_yaml)
            if metadata.get("name") != name:
                problems.append(
                    f"{rel_path(path)}:{line_number}: local Helm chart {name} "
                    f"does not match Chart.yaml name {metadata.get('name', '<missing>')}"
                )
            local_version = metadata.get("version", "")
            if not local_version:
                problems.append(
                    f"{rel_path(path)}:{line_number}: local Helm chart {name} "
                    "must declare a Chart.yaml version"
                )
            elif local_version.lower() in MUTABLE_VERSIONS or PRERELEASE_RE.search(local_version):
                problems.append(
                    f"{rel_path(path)}:{line_number}: local Helm chart {name} "
                    f"uses non-production version {local_version}"
                )
            continue
        version = chart.get("version", "")
        if PREMIUM_ROOT in path.parents:
            problems.append(
                f"{rel_path(path)}:{line_number}: premium Helm chart {name} "
                "must use committed local chart content"
            )
        if not version:
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} must pin version")
        elif version.lower() in MUTABLE_VERSIONS:
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} uses mutable version {version}")
        elif PRERELEASE_RE.search(version):
            problems.append(f"{rel_path(path)}:{line_number}: Helm chart {name} uses prerelease version {version}")
        vendored_matches = matching_vendored_charts(path, name, version)
        if vendored_matches:
            rendered_matches = ", ".join(rel_path(candidate) for candidate in vendored_matches)
            problems.append(
                f"{rel_path(path)}:{line_number}: Helm chart {name} {version} "
                f"must use committed local chart content: {rendered_matches}"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    scanned = 0
    consumed_local_charts: set[str] = set()
    for path in sorted(SCAN_ROOT.rglob("kustomization.yaml")):
        if should_scan(path):
            scanned += 1
            problems.extend(find_problems(path))
            consumed_local_charts.update(consumed_local_chart_paths(path))

    problems.extend(
        validate_inventory(
            root=ROOT,
            inventory_path=DEFAULT_INVENTORY,
            expected_paths=consumed_local_charts,
        )
    )

    if problems:
        print("GitOps Helm chart pinning validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print(
        "GitOps Helm chart pinning validation passed for "
        f"{scanned} kustomization files and "
        f"{len(consumed_local_charts)} consumed local charts."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
