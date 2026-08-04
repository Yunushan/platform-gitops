#!/usr/bin/env python3
"""Convert a proven subset of GitLab CI or GitHub Actions to Woodpecker.

The converter is deliberately fail-closed.  A migration must never be marked
successful because a provider-specific feature was silently discarded.  The
returned report is safe to retain as evidence: it contains names and hashes,
never source variable values or secret contents.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
from pathlib import Path
import sys
from typing import Any

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from strict_yaml import StrictYamlError, loads_strict_yaml_all


MAX_PIPELINE_BYTES = 2 * 1024 * 1024
DEFAULT_IMAGE = "alpine:3.20"
DEFAULT_GATE_MARKER = "FORGE_CUTOVER_DEPLOYMENT_ENABLED"
SECRET_NAME_RE = re.compile(r"(?i)(?:secret|token|password|passwd|private|credential|api[_-]?key|access[_-]?key)")
GITLAB_SECRET_REF_RE = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")
GITHUB_SECRET_REF_RE = re.compile(r"\$\{\{\s*(?:secrets|vars)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
GITHUB_EXPRESSION_RE = re.compile(r"\$\{\{\s*([^{}]+?)\s*\}\}")
SHELL_ENV_REF_RE = re.compile(r"(?<![$A-Za-z0-9_])\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")
GITHUB_REF_RE = re.compile(r"^\$\{\{\s*github\.ref\s*\}\}$")

GITLAB_RESERVED = {
    "after_script",
    "artifacts",
    "before_script",
    "cache",
    "default",
    "include",
    "image",
    "inherit",
    "pages",
    "services",
    "stages",
    "workflow",
    "variables",
}
GITLAB_UNSUPPORTED_JOB_KEYS = {
    "artifacts",
    "cache",
    "coverage",
    "environment",
    "extends",
    "inherit",
    "parallel",
    "release",
    "resource_group",
    "retry",
    "services",
    "timeout",
    "trigger",
}
GITHUB_UNSUPPORTED_JOB_KEYS = {
    "concurrency",
    "defaults",
    "environment",
    "outputs",
    "permissions",
    "services",
    "secrets",
    "strategy",
    "timeout-minutes",
    "uses",
}
GITHUB_UNSUPPORTED_STEP_KEYS = {
    "continue-on-error",
    "id",
    "timeout-minutes",
}
GITHUB_UNSUPPORTED_WORKFLOW_KEYS = {
    "concurrency",
    "defaults",
    "permissions",
}
SUPPORTED_GITHUB_ACTIONS = {
    "actions/checkout",
    "actions/setup-go",
    "actions/setup-node",
    "actions/setup-python",
}


class PipelineConversionError(ValueError):
    """Raised for malformed input or an unsafe conversion request."""


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PipelineConversionError(f"{label} must be a mapping")
    return {str(key): child for key, child in value.items()}


def load_pipeline(text: str) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_PIPELINE_BYTES:
        raise PipelineConversionError("pipeline file exceeds the size limit")
    try:
        documents = loads_strict_yaml_all(text, yaml_12=True)
    except StrictYamlError as exc:
        raise PipelineConversionError(f"pipeline YAML is invalid: {exc}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise PipelineConversionError("pipeline must contain exactly one mapping document")
    return {str(key): value for key, value in documents[0].items()}


def _issue(issues: list[dict[str, str]], code: str, path: str, message: str) -> None:
    issues.append({"code": code, "path": path, "message": message})


def _secret_aliases(config: dict[str, Any]) -> dict[str, str]:
    aliases = config.get("secret_aliases") or {}
    if not isinstance(aliases, dict):
        raise PipelineConversionError("secret_aliases must be a mapping")
    result = {str(key): str(value) for key, value in aliases.items()}
    for value in result.values():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise PipelineConversionError(f"invalid Woodpecker secret name: {value}")
    return result


def _secret_names(config: dict[str, Any]) -> set[str]:
    values = config.get("secret_names") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise PipelineConversionError("secret_names must be an array")
    result = {str(value) for value in values if str(value).strip()}
    result.update(_secret_aliases(config))
    return result


def _source_secret_name(name: str, config: dict[str, Any]) -> str | None:
    aliases = _secret_aliases(config)
    names = _secret_names(config)
    if name in aliases:
        return aliases[name]
    if name in names or SECRET_NAME_RE.search(name):
        return name
    return None


def _secret_ref(value: Any, provider: str, config: dict[str, Any]) -> str | None:
    text = _string(value)
    if provider == "gitlab":
        match = GITLAB_SECRET_REF_RE.fullmatch(text.strip())
        if match:
            source_name = match.group("braced") or match.group("plain")
            return _source_secret_name(source_name, config)
    else:
        match = GITHUB_SECRET_REF_RE.fullmatch(text.strip())
        if match:
            return _source_secret_name(match.group(1), config) or match.group(1)
    return None


def _environment(
    values: Any,
    provider: str,
    config: dict[str, Any],
    issues: list[dict[str, str]],
    path: str,
    required_secrets: set[str],
) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for key, value in _mapping(values, path).items():
        name = str(key)
        secret = _secret_ref(value, provider, config)
        if secret is None and _source_secret_name(name, config):
            secret = _source_secret_name(name, config)
        if secret:
            required_secrets.add(secret)
            result[name] = ("secret", secret)
            continue
        text = _string(value)
        if "${{" in text or (provider == "gitlab" and "$" in text):
            _issue(issues, "unresolved-expression", f"{path}.{name}", "expression is not representable as a Woodpecker literal or secret")
            continue
        result[name] = ("literal", text)
    return result


def _rewrite_command(
    command: str,
    provider: str,
    config: dict[str, Any],
    issues: list[dict[str, str]],
    path: str,
    environment: dict[str, tuple[str, str | None]],
    required_secrets: set[str],
) -> str:
    """Preserve supported secret references and reject provider-only expressions."""
    rewritten = command
    if provider == "github":
        def replace_expression(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            expression_match = re.fullmatch(r"(secrets|vars)\.([A-Za-z_][A-Za-z0-9_]*)", expression)
            if expression_match:
                name = expression_match.group(2)
                secret = _source_secret_name(name, config) or name
                required_secrets.add(secret)
                environment.setdefault(name, ("secret", secret))
                return f"${name}"
            env_match = re.fullmatch(r"env\.([A-Za-z_][A-Za-z0-9_]*)", expression)
            if env_match and env_match.group(1) in environment:
                return f"${env_match.group(1)}"
            _issue(issues, "github-command-expression", path, f"expression {expression!r} is not representable in a Woodpecker shell step")
            return match.group(0)

        rewritten = GITHUB_EXPRESSION_RE.sub(replace_expression, rewritten)
    for match in SHELL_ENV_REF_RE.finditer(rewritten):
        name = match.group("braced") or match.group("plain")
        secret = _source_secret_name(name, config)
        if secret:
            required_secrets.add(secret)
            environment.setdefault(name, ("secret", secret))
            continue
        if provider == "github" and name.startswith(("GITHUB_", "RUNNER_", "ACTIONS_")):
            _issue(issues, "github-command-variable", path, f"provider-only variable {name!r} is not representable")
    return rewritten


def _commands(value: Any, path: str) -> list[str]:
    commands = [_string(item) for item in _list(value)]
    if not commands or any(not command.strip() for command in commands):
        raise PipelineConversionError(f"{path} must contain at least one non-empty command")
    return commands


def _image(value: Any, default: str) -> str:
    if isinstance(value, dict):
        image = value.get("name") or value.get("image")
    else:
        image = value
    image = _string(image, default).strip()
    if not image:
        raise PipelineConversionError("step image cannot be empty")
    return image


def _runner_labels(value: Any, config: dict[str, Any], issues: list[dict[str, str]], path: str) -> dict[str, str]:
    labels_config = config.get("runner_labels") or {}
    if not isinstance(labels_config, dict):
        raise PipelineConversionError("runner_labels must be a mapping")
    source_values = [_string(item) for item in _list(value) if _string(item).strip()]
    labels: dict[str, str] = {}
    for source in source_values:
        mapped = labels_config.get(source)
        if not isinstance(mapped, dict) or not mapped:
            _issue(issues, "runner-label-unmapped", path, f"runner label {source!r} has no Woodpecker label mapping")
            continue
        labels.update({str(key): str(child) for key, child in mapped.items()})
    return labels


def _branch_condition(value: Any) -> list[str]:
    return [_string(item) for item in _list(value) if _string(item).strip() and _string(item) not in {"branches", "tags"}]


def _gitlab_conditions(
    job: dict[str, Any],
    issues: list[dict[str, str]],
    path: str,
    config: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    conditions: dict[str, list[str]] = {}
    schedule_mapped = bool((config or {}).get("schedule_mappings"))
    for key in ("only", "except"):
        value = job.get(key)
        if value is None:
            continue
        entries = _list(value)
        if any(isinstance(item, dict) for item in entries):
            _issue(issues, "gitlab-condition", f"{path}.{key}", "mapping-form only/except rules require manual review")
            continue
        selectors = {_string(item).strip() for item in entries}
        special = selectors.intersection({"api", "external", "pipelines", "schedules", "triggers", "web"})
        if special:
            unsupported_special = special.difference({"schedules"} if schedule_mapped else set())
            if unsupported_special:
                _issue(issues, "gitlab-condition", f"{path}.{key}", "API, trigger, and web selectors require an explicit Woodpecker mapping")
            elif "schedules" in special and key == "only":
                conditions["event"] = ["cron"]
            elif "schedules" in special:
                _issue(issues, "gitlab-condition", f"{path}.{key}", "except schedules changes the pipeline event scope")
        branches = [item for item in entries if _string(item) not in {"branches", "tags", "api", "external", "pipelines", "schedules", "triggers", "web"}]
        if key == "only":
            branch_values = _branch_condition(branches)
            if branch_values:
                conditions["branch"] = branch_values
            if "tags" in selectors:
                conditions["event"] = ["tag"]
        else:
            if branches:
                _issue(issues, "gitlab-condition", f"{path}.{key}", "except branch expressions are not safely representable")
            if "tags" in selectors:
                _issue(issues, "gitlab-condition", f"{path}.{key}", "except tags changes the pipeline event scope")
    rules = job.get("rules")
    if rules is not None:
        for index, rule in enumerate(_list(rules)):
            if not isinstance(rule, dict):
                _issue(issues, "gitlab-rule", f"{path}.rules[{index}]", "rule must be a mapping")
                continue
            expression = _string(rule.get("if")).strip()
            when = _string(rule.get("when"), "on_success")
            if expression:
                match = re.fullmatch(r"\$CI_COMMIT_BRANCH\s*==\s*[\"']([^\"']+)[\"']", expression)
                if match:
                    conditions["branch"] = [match.group(1)]
                else:
                    match = re.fullmatch(r"\$CI_PIPELINE_SOURCE\s*==\s*[\"']([^\"']+)[\"']", expression)
                    if match and match.group(1) in {"push", "merge_request_event", "web"}:
                        conditions["event"] = [{"push": "push", "merge_request_event": "pull_request", "web": "manual"}[match.group(1)]]
                    elif match and match.group(1) == "schedule" and schedule_mapped:
                        conditions["event"] = ["cron"]
                    elif match and match.group(1) in {"schedule", "api", "trigger"}:
                        _issue(issues, "gitlab-rule", f"{path}.rules[{index}].if", "schedule, API, and trigger sources require explicit mapping")
                    else:
                        _issue(issues, "gitlab-rule", f"{path}.rules[{index}].if", "expression is not representable")
            if when == "manual":
                conditions["event"] = ["manual"]
            elif when == "never":
                _issue(issues, "gitlab-rule", f"{path}.rules[{index}].when", "when: never cannot be represented without changing pipeline graph")
            elif when not in {"on_success", "always"}:
                _issue(issues, "gitlab-rule", f"{path}.rules[{index}].when", f"unsupported rule action {when!r}")
    return conditions


def _github_conditions(job: dict[str, Any], issues: list[dict[str, str]], path: str) -> dict[str, list[str]]:
    value = _string(job.get("if")).strip()
    if not value:
        return {}
    if value in {"always()", "success()"}:
        return {}
    if value == "failure()":
        return {"status": ["failure"]}
    match = re.fullmatch(r"github\.ref\s*==\s*['\"]refs/heads/([^'\"]+)['\"]", value.replace("${{", "").replace("}}", "").strip())
    if match:
        return {"branch": [match.group(1)]}
    match = re.fullmatch(r"github\.event_name\s*==\s*['\"]([^'\"]+)['\"]", value.replace("${{", "").replace("}}", "").strip())
    if match:
        return {"event": [match.group(1)]}
    _issue(issues, "github-condition", f"{path}.if", "expression is not representable")
    return {}


def _job_is_deployment(name: str, commands: list[str], config: dict[str, Any]) -> bool:
    patterns = config.get("deployment_jobs") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    if any(fnmatch.fnmatchcase(name, _string(pattern)) for pattern in patterns):
        return True
    joined = " ".join(commands).lower()
    return bool(re.search(r"\b(?:kubectl|helm|argocd|terraform\s+apply|docker\s+push|podman\s+push|deploy|release)\b", f"{name.lower()} {joined}"))


def _render_env(environment: dict[str, tuple[str, str | None]], indent: str = "    ") -> list[str]:
    if not environment:
        return []
    lines = [f"{indent}environment:"]
    for key in sorted(environment):
        kind, value = environment[key]
        if kind == "secret":
            lines.append(f"{indent}  {key}:")
            lines.append(f"{indent}    from_secret: {json.dumps(value)}")
        else:
            lines.append(f"{indent}  {key}: {json.dumps(value or '')}")
    return lines


def _render_conditions(conditions: dict[str, list[str]], indent: str = "    ") -> list[str]:
    if not conditions:
        return []
    lines = [f"{indent}when:"]
    for index, key in enumerate(sorted(conditions)):
        values = conditions[key]
        if len(values) == 1:
            value = values[0]
        else:
            value = values
        prefix = f"{indent}  - " if index == 0 else f"{indent}    "
        lines.append(f"{prefix}{key}: {json.dumps(value)}")
    return lines


def _render_pipeline(
    steps: list[dict[str, Any]],
    services: list[str],
    events: list[str],
    labels: dict[str, str],
    marker: str,
    global_conditions: list[dict[str, Any]] | None = None,
) -> str:
    lines = ["# Generated by scripts/forge_pipeline.py; review the conversion report before activation."]
    conditions = global_conditions or [{"event": event} for event in (events or ["push", "pull_request"])]
    merged_condition = _merge_global_conditions(conditions)
    if merged_condition:
        lines.append("when:")
        for key in sorted(merged_condition):
            lines.append(f"  {key}: {json.dumps(merged_condition[key])}")
    if labels:
        lines.append("labels:")
        for key in sorted(labels):
            lines.append(f"  {key}: {json.dumps(labels[key])}")
    if services:
        lines.append("services:")
        for index, image in enumerate(services, start=1):
            lines.append(f"  - name: {json.dumps(f'service-{index}')}\n    image: {json.dumps(image)}")
    lines.append("steps:")
    for step in steps:
        lines.append(f"  - name: {json.dumps(step['name'])}")
        lines.append(f"    image: {json.dumps(step['image'])}")
        lines.append("    commands:")
        for command in step["commands"]:
            if "\n" in command:
                lines.append("      - |")
                lines.extend(f"          {line}" for line in command.splitlines())
            else:
                lines.append(f"      - {json.dumps(command)}")
        if step.get("directory"):
            lines.append(f"    directory: {json.dumps(step['directory'])}")
        environment = dict(step.get("environment") or {})
        environment.setdefault(marker, ("secret", marker))
        lines.extend(_render_env(environment, "    "))
        dependencies = step.get("depends_on") or []
        if dependencies:
            lines.append("    depends_on:")
            lines.extend(f"      - {json.dumps(value)}" for value in dependencies)
        lines.extend(_render_conditions(step.get("conditions") or {}, "    "))
    return "\n".join(lines) + "\n"


def _common_report(provider: str, source_path: str, text: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "source_path": source_path,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "supported": False,
        "unsupported": [],
        "warnings": [],
        "required_secrets": [],
        "jobs": [],
        "events": [],
        "global_conditions": [],
        "schedules": [],
        "runner_labels": {},
    }


def _translate_gitlab(document: dict[str, Any], report: dict[str, Any], config: dict[str, Any]) -> str:
    issues = report["unsupported"]
    required_secrets: set[str] = set()
    global_env = _environment(document.get("variables"), "gitlab", config, issues, "variables", required_secrets)
    default = _mapping(document.get("default"), "default")
    supported_default_keys = {"after_script", "before_script", "image", "services", "tags"}
    for key in sorted(set(default).difference(supported_default_keys)):
        _issue(issues, "gitlab-default", f"default.{key}", "default feature has no lossless Woodpecker equivalent")
    image = _image(document.get("image") or default.get("image"), _string(config.get("default_image"), DEFAULT_IMAGE))
    before = _optional_commands(
        document.get("before_script"),
        "before_script",
        _optional_commands(default.get("before_script"), "default.before_script", []),
    )
    after = _optional_commands(
        document.get("after_script"),
        "after_script",
        _optional_commands(default.get("after_script"), "default.after_script", []),
    )
    services = []
    root_services = document.get("services") if document.get("services") is not None else default.get("services")
    for index, service in enumerate(_list(root_services), start=1):
        service_map = service if isinstance(service, dict) else {"name": service}
        for key in sorted(set(service_map).difference({"name", "image"})):
            _issue(issues, "gitlab-service", f"services[{index}].{key}", "service options are not losslessly representable")
        service_image = _string(service_map.get("name") or service_map.get("image"))
        if not service_image:
            _issue(issues, "gitlab-service", f"services[{index}]", "service image is missing")
        else:
            services.append(service_image)
    stages = [_string(stage) for stage in _list(document.get("stages"))]
    jobs: list[dict[str, Any]] = []
    for name, raw_job in document.items():
        if name in GITLAB_RESERVED or name.startswith("."):
            continue
        if not isinstance(raw_job, dict):
            _issue(issues, "gitlab-job", name, "job definition must be a mapping")
            continue
        job = {str(key): value for key, value in raw_job.items()}
        unsupported_keys = sorted(set(job).intersection(GITLAB_UNSUPPORTED_JOB_KEYS))
        for key in unsupported_keys:
            _issue(issues, f"gitlab-{key}", f"{name}.{key}", "feature has no lossless Woodpecker equivalent")
        if "script" not in job:
            _issue(issues, "gitlab-job", name, "job has no script")
            continue
        job_before = _optional_commands(job.get("before_script"), f"{name}.before_script", before)
        job_after = _optional_commands(job.get("after_script"), f"{name}.after_script", after)
        commands = job_before + _commands(job.get("script"), f"{name}.script") + job_after
        environment = dict(global_env)
        environment.update(_environment(job.get("variables"), "gitlab", config, issues, f"{name}.variables", required_secrets))
        commands = [
            _rewrite_command(command, "gitlab", config, issues, f"{name}.script", environment, required_secrets)
            for command in commands
        ]
        job_image = _image(job.get("image"), image)
        dependencies = []
        needs = job.get("needs")
        if needs is not None:
            for need in _list(needs):
                dependencies.append(_string(need.get("job") if isinstance(need, dict) else need))
        elif stages and _string(job.get("stage"), stages[0]) in stages:
            stage_index = stages.index(_string(job.get("stage"), stages[0]))
            if stage_index:
                previous_stage = stages[stage_index - 1]
                dependencies = [candidate["name"] for candidate in jobs if candidate["stage"] == previous_stage]
        labels = _runner_labels(job.get("tags", default.get("tags")), config, issues, f"{name}.tags")
        conditions = _gitlab_conditions(job, issues, name, config)
        if _string(job.get("when")) == "always":
            conditions["status"] = ["success", "failure"]
        if _bool(job.get("allow_failure")):
            _issue(issues, "gitlab-allow-failure", f"{name}.allow_failure", "allow_failure changes pipeline failure semantics")
        jobs.append({"name": name, "source_job": name, "stage": _string(job.get("stage"), stages[0] if stages else "build"), "image": job_image, "commands": commands, "environment": environment, "depends_on": dependencies, "conditions": conditions, "labels": labels})
    if not jobs:
        _issue(issues, "empty-pipeline", "jobs", "no runnable GitLab jobs were found")
    if document.get("workflow") is not None:
        _issue(issues, "gitlab-workflow", "workflow", "workflow rules require explicit trigger review")
    events = {"push"}
    if config.get("schedule_mappings"):
        events.add("cron")
        mappings = config.get("schedule_mappings")
        if isinstance(mappings, dict):
            report["schedules"] = [
                {"source": _string(source), "target": _string(target)}
                for source, target in sorted(mappings.items())
            ]
    for job in jobs:
        values = job.get("conditions", {}).get("event", [])
        events.update(_string(value) for value in values if _string(value).strip())
    report["events"] = sorted(events)
    report["jobs"] = [job["name"] for job in jobs]
    return _finish_translation(report, jobs, services, config, required_secrets)


def _github_events(value: Any, report: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate GitHub triggers into one safely representable Woodpecker filter."""
    if value is None:
        entries: list[tuple[str, Any]] = [("push", None), ("pull_request", None)]
    elif isinstance(value, str):
        entries = [(value, None)]
    elif isinstance(value, list):
        entries = [(_string(item), None) for item in value]
    elif isinstance(value, dict):
        entries = [(str(event), options) for event, options in value.items()]
    else:
        _issue(report["unsupported"], "github-trigger", "on", "trigger must be a string, list, or mapping")
        return []

    conditions: list[dict[str, Any]] = []
    event_names: list[str] = []
    for index, (event, options) in enumerate(entries, start=1):
        path = f"on.{event}[{index}]"
        if event == "schedule":
            schedule_entries = _list(options)
            if not schedule_entries:
                _issue(report["unsupported"], "github-schedule-unmapped", "on.schedule", "schedule requires at least one cron mapping")
                continue
            for schedule_index, item in enumerate(schedule_entries, start=1):
                schedule = _mapping(item, f"on.schedule[{schedule_index}]")
                cron = _string(schedule.get("cron")).strip()
                if not cron:
                    _issue(report["unsupported"], "github-schedule", f"on.schedule[{schedule_index}]", "cron is required")
                    continue
                target = _schedule_target(config, cron)
                if not target:
                    _issue(report["unsupported"], "github-schedule-unmapped", f"on.schedule[{schedule_index}].cron", "schedule requires an explicit Woodpecker cron name mapping")
                    continue
                conditions.append({"event": "cron", "cron": target})
                event_names.append("cron")
                report["schedules"].append({"source": cron, "target": target})
            continue
        condition = _github_event_condition(event, options, report, path)
        if condition:
            conditions.append(condition)
            event_names.append(_string(condition.get("event")))

    if not conditions and not report["unsupported"]:
        _issue(report["unsupported"], "github-trigger", "on", "workflow does not contain a supported trigger")
    non_event_shapes = {tuple(sorted(set(condition).difference({"event"}))) for condition in conditions}
    if len(non_event_shapes) > 1:
        _issue(report["unsupported"], "github-trigger-scope", "on", "event-specific filters cannot be combined into one Woodpecker workflow")
    elif conditions and _merge_global_conditions(conditions) is None:
        _issue(report["unsupported"], "github-trigger-scope", "on", "trigger filters have conflicting scopes")
    report["events"] = list(dict.fromkeys(event_names))
    report["global_conditions"] = conditions
    return conditions


def _github_action_setup(action: str, with_values: dict[str, Any], default_image: str) -> str | None:
    name = action.split("@", 1)[0]
    version = _string(with_values.get("node-version") or with_values.get("python-version") or with_values.get("go-version"), "latest")
    if name == "actions/setup-node":
        return f"node:{version}"
    if name == "actions/setup-python":
        return f"python:{version}"
    if name == "actions/setup-go":
        return f"golang:{version}"
    if name == "actions/checkout":
        return default_image
    return None


def _optional_commands(value: Any, path: str, fallback: list[str]) -> list[str]:
    """Apply provider override semantics while allowing an explicit empty list."""
    if value is None:
        return list(fallback)
    if value == []:
        return []
    return _commands(value, path)


def _merge_conditions(
    left: dict[str, list[str]],
    right: dict[str, list[str]],
    issues: list[dict[str, str]],
    path: str,
) -> dict[str, list[str]]:
    merged = {key: list(values) for key, values in left.items()}
    for key, values in right.items():
        normalized = list(values)
        if key in merged and set(merged[key]) != set(normalized):
            _issue(issues, "github-condition-scope", path, f"conflicting {key} filters cannot be merged safely")
            continue
        merged[key] = normalized
    return merged


def _safe_step_name(value: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._") or fallback
    return name[:60]


def _schedule_target(config: dict[str, Any], source: str) -> str | None:
    mappings = config.get("schedule_mappings") or {}
    if isinstance(mappings, dict):
        value = mappings.get(source)
        if isinstance(value, dict):
            value = value.get("target_name") or value.get("target") or value.get("name")
        return _string(value).strip() or None
    if isinstance(mappings, list):
        for item in mappings:
            if not isinstance(item, dict):
                continue
            sources = {
                _string(item.get("source")),
                _string(item.get("cron")),
                _string(item.get("schedule")),
            }
            if source in sources:
                return _string(item.get("target_name") or item.get("target") or item.get("name")).strip() or None
    return None


def _github_event_condition(
    event: str,
    options: Any,
    report: dict[str, Any],
    path: str,
) -> dict[str, Any] | None:
    issues = report["unsupported"]
    options_map = _mapping(options, path) if options not in (None, {}) else {}
    if event == "schedule":
        return None
    if event == "workflow_dispatch":
        inputs = options_map.get("inputs")
        if inputs:
            _issue(issues, "github-dispatch-inputs", f"{path}.inputs", "workflow_dispatch inputs need an explicit Woodpecker parameter mapping")
        return {"event": "manual"}
    if event not in {"push", "pull_request"}:
        _issue(issues, "github-trigger", path, "trigger has no proven Woodpecker equivalent")
        return None
    condition: dict[str, Any] = {"event": event}
    for positive, woodpecker_key in (("branches", "branch"), ("paths", "path")):
        value = options_map.get(positive)
        if value is not None:
            values = [_string(item) for item in _list(value) if _string(item).strip()]
            if not values:
                _issue(issues, "github-trigger-filter", f"{path}.{positive}", "filter must contain at least one pattern")
            else:
                condition[woodpecker_key] = values
    for ignored in ("branches-ignore", "paths-ignore", "tags", "tags-ignore"):
        if options_map.get(ignored) is not None:
            _issue(issues, "github-trigger-filter", f"{path}.{ignored}", "ignore and tag filters are not losslessly representable")
    types = options_map.get("types")
    if types is not None:
        values = {_string(item) for item in _list(types)}
        default_types = {"opened", "synchronize", "reopened"}
        if event != "pull_request" or values != default_types:
            _issue(issues, "github-trigger-types", f"{path}.types", "event activity types require an explicit Woodpecker mapping")
    return condition


def _merge_global_conditions(
    conditions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not conditions:
        return None
    merged: dict[str, Any] = {}
    for condition in conditions:
        for key, value in condition.items():
            if key == "event":
                values = value if isinstance(value, list) else [value]
                existing = merged.setdefault("event", [])
                for item in values:
                    if item not in existing:
                        existing.append(item)
                continue
            if key in merged and merged[key] != value:
                return None
            merged[key] = value
    if isinstance(merged.get("event"), list) and len(merged["event"]) == 1:
        merged["event"] = merged["event"][0]
    return merged


def _translate_github(document: dict[str, Any], report: dict[str, Any], config: dict[str, Any]) -> str:
    issues = report["unsupported"]
    required_secrets: set[str] = set()
    for key in sorted(set(document).intersection(GITHUB_UNSUPPORTED_WORKFLOW_KEYS)):
        _issue(issues, f"github-workflow-{key}", key, "workflow feature has no lossless Woodpecker equivalent")
    _github_events(document.get("on"), report, config)
    top_env = _environment(document.get("env"), "github", config, issues, "env", required_secrets)
    jobs_document = _mapping(document.get("jobs"), "jobs")
    job_records: list[dict[str, Any]] = []
    default_image = _string(config.get("default_image"), DEFAULT_IMAGE)
    for name, raw_job in jobs_document.items():
        job_path = f"jobs.{name}"
        job = _mapping(raw_job, job_path)
        for key in sorted(set(job).intersection(GITHUB_UNSUPPORTED_JOB_KEYS)):
            _issue(issues, f"github-{key}", f"{job_path}.{key}", "feature has no lossless Woodpecker equivalent")
        runner = job.get("runs-on")
        if runner is None:
            _issue(issues, "github-runs-on", f"{job_path}.runs-on", "job runner is required")
        labels = _runner_labels(runner, config, issues, f"{job_path}.runs-on")
        image = default_image
        raw_container = job.get("container")
        if isinstance(raw_container, str):
            image = _image(raw_container, image)
        elif raw_container is not None:
            container = _mapping(raw_container, f"{job_path}.container")
            for key in sorted(set(container).difference({"image"})):
                _issue(issues, "github-container", f"{job_path}.container.{key}", "container option has no lossless Woodpecker equivalent")
            if container:
                image = _image(container.get("image"), image)
        job_environment = dict(top_env)
        job_environment.update(_environment(job.get("env"), "github", config, issues, f"{job_path}.env", required_secrets))
        job_conditions = _github_conditions(job, issues, job_path)
        raw_needs = job.get("needs")
        needs: list[str] = []
        for need in _list(raw_needs):
            if isinstance(need, dict):
                _issue(issues, "github-needs", f"{job_path}.needs", "needs result conditions require an explicit mapping")
                continue
            value = _string(need).strip()
            if value:
                needs.append(value)
        run_steps: list[dict[str, Any]] = []
        current_image = image
        for index, raw_step in enumerate(_list(job.get("steps")), start=1):
            step_path = f"{job_path}.steps[{index}]"
            step = _mapping(raw_step, step_path)
            for key in sorted(set(step).intersection(GITHUB_UNSUPPORTED_STEP_KEYS)):
                _issue(issues, f"github-step-{key}", f"{step_path}.{key}", "feature has no lossless Woodpecker equivalent")
            step_env = _environment(step.get("env"), "github", config, issues, f"{step_path}.env", required_secrets)
            has_uses = step.get("uses") is not None
            has_run = step.get("run") is not None
            if has_uses and has_run:
                _issue(issues, "github-step", step_path, "step cannot contain both uses and run")
                continue
            if has_uses:
                action = _string(step.get("uses"))
                action_name = action.split("@", 1)[0]
                if action_name not in SUPPORTED_GITHUB_ACTIONS:
                    _issue(issues, "github-action", f"{step_path}.uses", f"action {action_name!r} cannot be executed losslessly by Woodpecker")
                    continue
                if step_env:
                    _issue(issues, "github-action-env", f"{step_path}.env", "action-only step environment cannot be preserved after action elimination")
                action_conditions = _github_conditions(step, issues, step_path)
                if action_conditions:
                    _issue(issues, "github-action-condition", f"{step_path}.if", "conditional action execution cannot be preserved after action elimination")
                with_values = _mapping(step.get("with"), f"{step_path}.with")
                if action_name == "actions/checkout":
                    if with_values:
                        _issue(issues, "github-checkout", f"{step_path}.with", "checkout options cannot be represented because Woodpecker performs the clone")
                else:
                    allowed_key = {
                        "actions/setup-node": "node-version",
                        "actions/setup-python": "python-version",
                        "actions/setup-go": "go-version",
                    }[action_name]
                    for key in sorted(set(with_values).difference({allowed_key})):
                        _issue(issues, "github-setup", f"{step_path}.with.{key}", "setup action option has no lossless Woodpecker equivalent")
                    if raw_container is not None:
                        _issue(issues, "github-setup-container", step_path, "setup action cannot be replaced with a different image inside a custom container")
                    current_image = _github_action_setup(action, with_values, current_image) or current_image
                continue
            if not has_run:
                _issue(issues, "github-step", step_path, "step must contain run or a supported action")
                continue
            shell = _string(step.get("shell")).strip().lower()
            if shell and shell not in {"bash", "sh"}:
                _issue(issues, "github-shell", f"{step_path}.shell", "only bash and sh are representable")
            conditions = _merge_conditions(job_conditions, _github_conditions(step, issues, step_path), issues, step_path)
            environment = dict(job_environment)
            environment.update(step_env)
            commands = [
                _rewrite_command(command, "github", config, issues, f"{step_path}.run", environment, required_secrets)
                for command in _commands(step.get("run"), f"{step_path}.run")
            ]
            run_step_name = _safe_step_name(f"{name}-{_string(step.get('name'), f'step-{index}')}", f"{name}-{index}")
            run_steps.append({
                "name": run_step_name,
                "source_job": name,
                "image": current_image,
                "commands": commands,
                "environment": environment,
                "depends_on": [],
                "conditions": conditions,
                "labels": labels,
                "directory": _string(step.get("working-directory")).strip(),
            })
        if not run_steps:
            _issue(issues, "empty-job", job_path, "job has no runnable commands")
            continue
        job_records.append({"name": name, "needs": needs, "steps": run_steps})
    first_steps = {
        record["name"]: record["steps"][0]["name"]
        for record in job_records
        if record["steps"]
    }
    jobs: list[dict[str, Any]] = []
    for record in job_records:
        previous: str | None = None
        for step_index, step in enumerate(record["steps"]):
            if step_index == 0:
                for need in record["needs"]:
                    if need not in first_steps:
                        _issue(issues, "github-needs", f"jobs.{record['name']}.needs", f"needed job {need!r} has no converted runnable step")
                    else:
                        step["depends_on"].append(first_steps[need])
            if previous:
                step["depends_on"].append(previous)
            previous = step["name"]
            jobs.append(step)
    if not jobs:
        _issue(issues, "empty-pipeline", "jobs", "no runnable GitHub jobs were found")
    report["jobs"] = [record["name"] for record in job_records]
    return _finish_translation(report, jobs, [], config, required_secrets)


def _finish_translation(report: dict[str, Any], jobs: list[dict[str, Any]], services: list[str], config: dict[str, Any], required_secrets: set[str]) -> str:
    label_scopes = {
        json.dumps(job.get("labels") or {}, sort_keys=True)
        for job in jobs
    }
    if len(label_scopes) > 1:
        _issue(report["unsupported"], "runner-label-scope", "jobs", "different jobs require different runner labels, but Woodpecker labels are workflow-scoped")
    labels: dict[str, str] = {}
    if len(label_scopes) == 1 and jobs:
        labels = dict(jobs[0].get("labels") or {})
    report["runner_labels"] = dict(sorted(labels.items()))
    report["required_secrets"] = sorted(required_secrets | {str(config.get("deployment_gate_marker") or DEFAULT_GATE_MARKER)})
    deployment_patterns = config.get("deployment_jobs") or []
    if isinstance(deployment_patterns, str):
        deployment_patterns = [deployment_patterns]
    marker = _string(config.get("deployment_gate_marker"), DEFAULT_GATE_MARKER)
    checked_deployment_jobs: set[str] = set()
    for job in jobs:
        source_job = _string(job.get("source_job") or job["name"])
        if source_job in checked_deployment_jobs:
            continue
        checked_deployment_jobs.add(source_job)
        if _job_is_deployment(source_job, job["commands"], config) and not any(fnmatch.fnmatchcase(source_job, _string(pattern)) for pattern in deployment_patterns):
            _issue(report["unsupported"], "deployment-job-unmapped", source_job, "deployment-like job needs an explicit deployment_jobs mapping")
    rendered = _render_pipeline(
        jobs,
        services,
        report.get("events") or [],
        labels,
        marker,
        report.get("global_conditions") or None,
    )
    report["rendered_sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    report["supported"] = not report["unsupported"]
    return rendered


def convert_pipeline(provider: str, text: str, source_path: str = "pipeline.yml", config: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    provider = provider.strip().lower()
    if provider not in {"gitlab", "github"}:
        raise PipelineConversionError("provider must be gitlab or github")
    config = dict(config or {})
    document = load_pipeline(text)
    report = _common_report(provider, source_path, text)
    rendered = _translate_gitlab(document, report, config) if provider == "gitlab" else _translate_github(document, report, config)
    if not report["supported"]:
        return "", report
    return rendered, report


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _parse_cli_mappings(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        source, separator, target = raw.partition("=")
        if not separator or not source.strip() or not target.strip():
            raise PipelineConversionError(f"{label} must use SOURCE=TARGET syntax")
        if source in result:
            raise PipelineConversionError(f"duplicate {label} source: {source}")
        result[source.strip()] = target.strip()
    return result


def _parse_runner_mappings(values: list[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for raw in values:
        source, separator, fields = raw.partition("=")
        if not separator or not source.strip() or not fields.strip():
            raise PipelineConversionError("runner-label must use SOURCE=KEY:VALUE[,KEY:VALUE] syntax")
        if source.strip() in result:
            raise PipelineConversionError(f"duplicate runner-label source: {source.strip()}")
        mapping: dict[str, str] = {}
        for field in fields.split(","):
            key, field_separator, value = field.partition(":")
            if not field_separator or not key.strip() or not value.strip():
                raise PipelineConversionError("runner-label must use SOURCE=KEY:VALUE[,KEY:VALUE] syntax")
            mapping[key.strip()] = value.strip()
        result[source.strip()] = mapping
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("gitlab", "github"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--deployment-gate-marker", default=DEFAULT_GATE_MARKER)
    parser.add_argument("--default-image", default=DEFAULT_IMAGE)
    parser.add_argument("--secret-name", action="append", default=[])
    parser.add_argument("--deployment-job", action="append", default=[])
    parser.add_argument("--runner-label", action="append", default=[], help="SOURCE=KEY:VALUE[,KEY:VALUE]")
    parser.add_argument("--schedule-mapping", action="append", default=[], help="SOURCE_CRON=WOODPECKER_CRON_NAME")
    args = parser.parse_args(argv)
    try:
        text = read_bounded_text(args.source, encoding="utf-8", max_bytes=MAX_PIPELINE_BYTES)
        rendered, report = convert_pipeline(
            args.provider,
            text,
            str(args.source),
            {
                "deployment_gate_marker": args.deployment_gate_marker,
                "default_image": args.default_image,
                "secret_names": args.secret_name,
                "deployment_jobs": args.deployment_job,
                "runner_labels": _parse_runner_mappings(args.runner_label),
                "schedule_mappings": _parse_cli_mappings(args.schedule_mapping, "schedule-mapping"),
            },
        )
        _write_json(args.report, report)
        if not report["supported"]:
            print(json.dumps(report, indent=2, sort_keys=True), file=sys.stderr)
            return 1
        atomic_write_text(args.output, rendered)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (OSError, PipelineConversionError, yaml.YAMLError) as exc:
        print(f"forge pipeline conversion failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
