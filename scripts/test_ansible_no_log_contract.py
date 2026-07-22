#!/usr/bin/env python3
"""Keep Ansible no_log usage intentional and secret-scoped."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_DIRS = (
    ROOT / "ansible" / "playbooks",
    ROOT / "ansible" / "tasks",
)
TASK_RE = re.compile(r"^(?P<indent>\s*)-\s+name:\s*(?P<name>.+?)\s*$")
NO_LOG_RE = re.compile(r"^(?P<indent>\s*)no_log:\s*(?P<value>.+?)\s*(?:#.*)?$")

ALLOWED_NO_LOG_TASKS = {
    ("ansible/playbooks/bootstrap-argocd.yml", "Register private Git repository credentials when provided"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve shared platform Valkey auth secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve MinIO root credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Harbor bootstrap secrets"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Harbor external database password secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Harbor external Redis credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate Harbor core Redis URL override secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Harbor registry S3 credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Grafana admin credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Grafana database password secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Keycloak admin credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Keycloak database password secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Forgejo external database password secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Forgejo Redis URI secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve CloudNativePG object storage credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Loki object storage credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Velero cloud credentials secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Woodpecker shared agent secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Generate or preserve Woodpecker database datasource secret"),
    ("ansible/playbooks/configure-platform-app-secrets.yml", "Configure Forgejo OAuth application for Woodpecker"),
    ("ansible/playbooks/repair-woodpecker.yml", "Reconcile and verify Woodpecker PostgreSQL role credentials"),
    ("ansible/playbooks/install-rke2.yml", "Resolve RKE2 token candidate"),
    ("ansible/playbooks/install-rke2.yml", "Generate or load local RKE2 token"),
    ("ansible/playbooks/install-rke2.yml", "Read existing first server RKE2 token when present"),
    ("ansible/playbooks/install-rke2.yml", "Select RKE2 token"),
    ("ansible/playbooks/install-rke2.yml", "Write RKE2 server configuration"),
    ("ansible/playbooks/install-rke2.yml", "Configure RKE2 server proxy environment"),
    ("ansible/playbooks/recover-rke2.yml", "Resolve RKE2 token candidate"),
    ("ansible/playbooks/recover-rke2.yml", "Generate or load local RKE2 token"),
    ("ansible/playbooks/recover-rke2.yml", "Read existing first server RKE2 token when present"),
    ("ansible/playbooks/recover-rke2.yml", "Select recovery RKE2 token"),
    ("ansible/playbooks/recover-rke2.yml", "Persist selected RKE2 token on controller"),
    ("ansible/playbooks/recover-rke2.yml", "Write consistent RKE2 server configuration"),
}

REQUIRED_VISIBLE_TASKS = {
    ("ansible/playbooks/bootstrap-argocd.yml", "Register platform applications in Argo CD"),
}


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def clean_task_name(raw: str) -> str:
    return raw.strip().strip("'\"")


def ansible_files() -> list[Path]:
    files: list[Path] = []
    for directory in ANSIBLE_DIRS:
        if directory.exists():
            files.extend(directory.glob("*.yml"))
    return sorted(files)


def no_log_tasks(path: Path) -> list[tuple[str, str, int]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    task_stack: list[tuple[int, str, int]] = []
    results: list[tuple[str, str, int]] = []
    rel = rel_path(path)
    for line_no, line in enumerate(lines, 1):
        task_match = TASK_RE.match(line)
        if task_match:
            indent = len(task_match.group("indent"))
            name = clean_task_name(task_match.group("name"))
            while task_stack and task_stack[-1][0] >= indent:
                task_stack.pop()
            task_stack.append((indent, name, line_no))
            continue

        no_log_match = NO_LOG_RE.match(line)
        if not no_log_match:
            continue
        no_log_indent = len(no_log_match.group("indent"))
        candidates = [task for task in task_stack if task[0] < no_log_indent]
        if not candidates:
            results.append((rel, "<unnamed task>", line_no))
            continue
        _, task_name, _ = candidates[-1]
        results.append((rel, task_name, line_no))
    return results


def visible_task_violations(path: Path, observed: set[tuple[str, str]]) -> list[str]:
    rel = rel_path(path)
    if not any(required_path == rel for required_path, _ in REQUIRED_VISIBLE_TASKS):
        return []
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for required_path, task_name in REQUIRED_VISIBLE_TASKS:
        if required_path != rel:
            continue
        if task_name not in text:
            problems.append(f"{required_path} is missing visible task contract: {task_name}")
        elif (required_path, task_name) in observed:
            problems.append(f"{required_path} task must not use no_log: {task_name}")
    return problems


def main() -> int:
    observed_with_lines: list[tuple[str, str, int]] = []
    problems: list[str] = []
    for path in ansible_files():
        observed_with_lines.extend(no_log_tasks(path))

    observed = {(path, task_name) for path, task_name, _ in observed_with_lines}
    unexpected = sorted(observed.difference(ALLOWED_NO_LOG_TASKS))
    missing = sorted(ALLOWED_NO_LOG_TASKS.difference(observed))
    if unexpected:
        details = []
        for path, task_name, line_no in observed_with_lines:
            if (path, task_name) in unexpected:
                details.append(f"{path}:{line_no} {task_name}")
        problems.append("unexpected no_log directive(s): " + "; ".join(details))
    if missing:
        problems.append(
            "required secret-scoped no_log task(s) missing: "
            + "; ".join(f"{path} {task_name}" for path, task_name in missing)
        )
    for path in ansible_files():
        problems.extend(visible_task_violations(path, observed))

    if problems:
        print("Ansible no_log contract validation failed:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    print(f"Ansible no_log contract validation passed for {len(observed)} secret-scoped tasks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
