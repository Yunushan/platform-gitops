#!/usr/bin/env python3
"""Validate example service and GitOps templates match documented claims."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
SERVICE_TEMPLATE = EXAMPLES / "service-template"
GITOPS_TEMPLATE = EXAMPLES / "gitops-app-template"
LANGUAGES = EXAMPLES / "languages"

EXPECTED_LANGUAGES = {
    "c",
    "cpp",
    "csharp",
    "dart",
    "elixir",
    "go",
    "java",
    "javascript",
    "julia",
    "kotlin",
    "perl",
    "php",
    "python",
    "r",
    "ruby",
    "rust",
    "scala",
    "shell",
    "swift",
    "typescript",
}
SERVICE_TEMPLATE_FILES = {
    "README.md",
    "Dockerfile",
    "app.sh",
    ".github/workflows/ci.yml",
    ".gitea/workflows/ci.yml",
    ".forgejo/workflows/ci.yml",
    ".gitlab-ci.yml",
    ".woodpecker.yml",
}
DISALLOWED_SERVICE_TEMPLATE_PATHS = {
    ".woodpecker",
}
GITOPS_TEMPLATE_FILES = {
    "README.md",
    "kustomization.yaml",
    "deployment.yaml",
    "service.yaml",
}
CI_NAMES = {
    "GitHub Actions",
    "Gitea Actions",
    "Forgejo Actions",
    "GitLab CI",
    "Woodpecker CI",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def missing_files(base: Path, expected: set[str]) -> list[str]:
    return sorted(str(Path(item)) for item in expected if not (base / item).exists())


def check_service_template() -> list[str]:
    problems: list[str] = []
    missing = missing_files(SERVICE_TEMPLATE, SERVICE_TEMPLATE_FILES)
    if missing:
        problems.append("service-template is missing file(s): " + ", ".join(missing))
    for item in sorted(DISALLOWED_SERVICE_TEMPLATE_PATHS):
        if (SERVICE_TEMPLATE / item).exists():
            problems.append(f"service-template has legacy duplicate path: {item}")
    readme = read(SERVICE_TEMPLATE / "README.md")
    for ci_name in CI_NAMES:
        if ci_name not in readme:
            problems.append(f"service-template README does not mention {ci_name}")
    for ci_path in SERVICE_TEMPLATE_FILES:
        if ci_path.startswith(".") and (SERVICE_TEMPLATE / ci_path).exists():
            text = read(SERVICE_TEMPLATE / ci_path)
            if "app.sh" not in text:
                problems.append(f"service-template CI file does not smoke-test app.sh: {ci_path}")
    return problems


def check_gitops_template() -> list[str]:
    problems: list[str] = []
    missing = missing_files(GITOPS_TEMPLATE, GITOPS_TEMPLATE_FILES)
    if missing:
        problems.append("gitops-app-template is missing file(s): " + ", ".join(missing))
    if (GITOPS_TEMPLATE / "kustomization.yaml").exists():
        text = read(GITOPS_TEMPLATE / "kustomization.yaml")
        for manifest in ("deployment.yaml", "service.yaml"):
            if manifest not in text:
                problems.append(f"gitops-app-template kustomization does not reference {manifest}")
    return problems


def check_language_scaffolds() -> list[str]:
    problems: list[str] = []
    actual = {path.name for path in LANGUAGES.iterdir() if path.is_dir()}
    missing = sorted(EXPECTED_LANGUAGES.difference(actual))
    unexpected = sorted(actual.difference(EXPECTED_LANGUAGES))
    if missing:
        problems.append("missing language scaffold(s): " + ", ".join(missing))
    if unexpected:
        problems.append("unexpected language scaffold(s): " + ", ".join(unexpected))
    for language in sorted(EXPECTED_LANGUAGES.intersection(actual)):
        path = LANGUAGES / language
        for required in ("README.md", "Dockerfile"):
            if not (path / required).exists():
                problems.append(f"{language} scaffold is missing {required}")
        source_files = [
            item
            for item in path.iterdir()
            if item.is_file() and item.name not in {"README.md", "Dockerfile"}
        ]
        if not source_files:
            problems.append(f"{language} scaffold has no source file")
        readme = read(path / "README.md") if (path / "README.md").exists() else ""
        if "examples/service-template" not in readme:
            problems.append(f"{language} scaffold README does not point to examples/service-template")
    return problems


def check_documented_counts() -> list[str]:
    problems: list[str] = []
    examples_readme = read(EXAMPLES / "README.md")
    root_readme = read(ROOT / "README.md")
    if "20 programming languages" not in examples_readme:
        problems.append("examples README does not document the 20 language scaffold count")
    if "20 language scaffolds" not in root_readme:
        problems.append("root README does not document the 20 language scaffold count")
    return problems


def main() -> int:
    problems = (
        check_service_template()
        + check_gitops_template()
        + check_language_scaffolds()
        + check_documented_counts()
    )
    if problems:
        print("Example template validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    print(f"Example template validation passed for {len(EXPECTED_LANGUAGES)} language scaffolds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
