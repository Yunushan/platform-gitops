#!/usr/bin/env python3
"""Verify public Makefile targets are discoverable and internally consistent."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
SHELL_RE = re.compile(r"^SHELL\s*:?=\s*(?P<shell>.+)$", re.MULTILINE)
PHONY_RE = re.compile(r"^\.PHONY:\s*(?P<targets>.+)$", re.MULTILINE)
HELP_RE = re.compile(r'@echo "  (?P<target>[A-Za-z0-9_.-]+)\s+')
TARGET_RE = re.compile(
    r"^(?P<targets>[A-Za-z0-9][A-Za-z0-9_.-]*(?:\s+[A-Za-z0-9][A-Za-z0-9_.-]*)*)\s*:(?!=)\s*(?P<deps>[^#]*)$"
)
TARGET_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
HELP_EXEMPTIONS = {"help"}


def make_shell(text: str) -> str:
    match = SHELL_RE.search(text)
    if not match:
        raise AssertionError("Makefile is missing a SHELL declaration")
    return match.group("shell").strip()


def phony_targets(text: str) -> set[str]:
    targets: set[str] = set()
    for match in PHONY_RE.finditer(text):
        targets.update(match.group("targets").split())
    return targets


def help_targets(text: str) -> set[str]:
    return set(HELP_RE.findall(text))


def target_rules(text: str) -> dict[str, list[str]]:
    rules: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("\t") or line.startswith(".PHONY:"):
            continue
        match = TARGET_RE.match(line)
        if not match:
            continue
        deps = []
        for dep in match.group("deps").split():
            if dep == "|":
                break
            if (
                dep.startswith("-")
                or "$" in dep
                or "/" in dep
                or "%" in dep
                or "=" in dep
                or not TARGET_WORD_RE.fullmatch(dep)
            ):
                continue
            deps.append(dep)
        for target in match.group("targets").split():
            rules[target] = deps
    return rules


def find_dependency_cycles(rules: dict[str, list[str]]) -> list[str]:
    cycles: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(target: str) -> None:
        if target in visited:
            return
        if target in visiting:
            cycle = stack[stack.index(target) :] + [target]
            cycles.append(" -> ".join(cycle))
            return
        visiting.add(target)
        stack.append(target)
        for dep in rules.get(target, []):
            if dep in rules:
                visit(dep)
        stack.pop()
        visiting.remove(target)
        visited.add(target)

    for target in rules:
        visit(target)
    return cycles


def main() -> int:
    text = MAKEFILE.read_text(encoding="utf-8")
    shell = make_shell(text)
    phony = phony_targets(text)
    helped = help_targets(text)
    rules = target_rules(text)

    shell_errors: list[str] = []
    if shell not in {"bash", "/bin/bash"}:
        shell_errors.append(f"Makefile SHELL must be bash or /bin/bash, got: {shell}")
    if len(shell.split()) != 1:
        shell_errors.append("Makefile SHELL must name only the shell program; put arguments in .SHELLFLAGS")

    missing = sorted(phony - helped - HELP_EXEMPTIONS)
    extra = sorted(helped - phony)
    undeclared = sorted(phony - set(rules))
    unlisted = sorted(set(rules) - phony)
    unknown_deps = sorted(
        (target, dep)
        for target, deps in rules.items()
        for dep in deps
        if dep not in rules
    )
    cycles = find_dependency_cycles(rules)

    if shell_errors or missing or extra or undeclared or unlisted or unknown_deps or cycles:
        print("Makefile target validation failed:")
        for problem in shell_errors:
            print(f" - {problem}")
        for name in missing:
            print(f" - target is missing from help output: {name}")
        for name in extra:
            print(f" - help output references unknown target: {name}")
        for name in undeclared:
            print(f" - .PHONY target has no rule: {name}")
        for name in unlisted:
            print(f" - target is declared but missing from .PHONY: {name}")
        for target, dep in unknown_deps:
            print(f" - target has unknown dependency: {target} -> {dep}")
        for cycle in cycles:
            print(f" - target dependency cycle: {cycle}")
        return 1

    render_marker = "platform-render-private-values:\n"
    if render_marker not in text:
        raise AssertionError("Makefile is missing platform-render-private-values")
    render_block = text.split(render_marker, 1)[1].split("\n\n", 1)[0]
    for needle in (
        'PLATFORM_RENDER_ENV_FILE',
        'PLATFORM_SEED_DEPLOY_ENV_FILE',
        'PLATFORM_FIRST_DEPLOY_ENV_FILE',
        'private/seed-git.env',
        'private/first-deploy.env',
        'load_env_file "$${env_file}" preserve-existing',
    ):
        if needle not in render_block:
            raise AssertionError(
                "platform-render-private-values must load the selected private env file: "
                f"missing {needle}"
            )

    print(f"Makefile target validation passed for {len(phony)} public targets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
