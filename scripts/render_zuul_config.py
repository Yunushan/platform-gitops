#!/usr/bin/env python3
"""Render private Zuul/Gerrit configuration contracts, never launch CI workers."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, RUNTIMES, dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*")
ENVIRONMENT = ("ZUUL_DATABASE_URI", "ZUUL_KEYSTORE_PASSWORD", "ZUUL_GERRIT_HTTP_PASSWORD")
PRIVATE_FILES = ("zk-client.pem", "zk-client.key", "zk-ca.pem", "postgres-ca.pem",
                 "gerrit-ca.pem", "gerrit-ssh.key", "known_hosts")


class ZuulConfigError(ValueError):
    pass


def https_root(value: object) -> bool:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and dns_name(parsed.hostname) and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def validate(plan: object) -> dict:
    fields = {"schema_version", "zuul_version", "image", "namespace", "web_url", "zookeeper", "gerrit", "tenants"}
    if not isinstance(plan, dict) or not fields <= set(plan) or set(plan) - fields - {"executor", "integrated_launcher"}:
        raise ZuulConfigError("Missing or unsupported Zuul plan fields")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ZuulConfigError("Unsupported Zuul plan schema")
    if not isinstance(plan["zuul_version"], str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", plan["zuul_version"]):
        raise ZuulConfigError("Declare an exact reviewed stable Zuul version")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ZuulConfigError("Pin the reviewed Zuul runtime image by digest")
    if not label(plan["namespace"]) or not https_root(plan["web_url"]):
        raise ZuulConfigError("Use a namespace label and credential-free HTTPS web root")
    zk = plan["zookeeper"]
    if not isinstance(zk, list) or len(zk) != 3:
        raise ZuulConfigError("Declare three separate TLS ZooKeeper endpoints")
    for endpoint in zk:
        if (not isinstance(endpoint, dict) or set(endpoint) != {"host", "port"}
                or not dns_name(endpoint["host"]) or type(endpoint["port"]) is not int
                or not 1024 <= endpoint["port"] <= 65535):
            raise ZuulConfigError("Invalid ZooKeeper endpoint")
    if len({endpoint["host"] for endpoint in zk}) != 3:
        raise ZuulConfigError("ZooKeeper endpoint hosts must be distinct")
    gerrit = plan["gerrit"]
    if (not isinstance(gerrit, dict) or not {"host", "port", "user"} <= set(gerrit) or set(gerrit)-{"host", "port", "user", "https_port"}
            or not dns_name(gerrit["host"]) or type(gerrit["port"]) is not int
            or not 1 <= gerrit["port"] <= 65535
            or not isinstance(gerrit["user"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", gerrit["user"])):
        raise ZuulConfigError("Declare the canonical Gerrit route and scoped service login")
    if 'https_port' in gerrit and (type(gerrit['https_port']) is not int or not 1<=gerrit['https_port']<=65535):
        raise ZuulConfigError('Declare an explicit Gerrit HTTPS port')
    tenants = plan["tenants"]
    if not isinstance(tenants, list) or not 1 <= len(tenants) <= 100:
        raise ZuulConfigError("Declare at least one bounded tenant")
    names = set()
    for tenant in tenants:
        tenant_fields = {"name", "config_branch", "config_projects", "untrusted_projects"}
        if not isinstance(tenant, dict) or not tenant_fields <= set(tenant) or set(tenant) - tenant_fields - {"allowed_labels"}:
            raise ZuulConfigError("Invalid tenant fields")
        if not label(tenant["name"]) or tenant["name"] in names:
            raise ZuulConfigError("Tenant names must be distinct namespace-style labels")
        names.add(tenant["name"])
        branch = tenant["config_branch"]
        if not isinstance(branch, str) or not PROJECT.fullmatch(branch) or ".." in branch or branch.endswith((".", ".lock")):
            raise ZuulConfigError("Declare an explicit simple trusted configuration branch")
        trusted, untrusted = tenant["config_projects"], tenant["untrusted_projects"]
        if not isinstance(trusted, list) or not trusted or not isinstance(untrusted, list) or len(trusted) + len(untrusted) > 10000:
            raise ZuulConfigError("Tenant project lists are missing or exceed limits")
        projects = trusted + untrusted
        if (any(not isinstance(p, str) or len(p) > 255 or not PROJECT.fullmatch(p) or ".." in p for p in projects)
                or len(set(projects)) != len(projects)):
            raise ZuulConfigError("Project names must be explicit and must not overlap trust categories")
        if "allowed_labels" in tenant:
            labels = tenant["allowed_labels"]
            if not isinstance(labels, list) or not labels or len(labels) > 1000 or any(not label(item) for item in labels) or len(set(labels)) != len(labels):
                raise ZuulConfigError("Explicit tenant worker-label restrictions must be a nonempty unique label list")
    if "executor" in plan:
        validate_executor(plan["executor"])
    if "integrated_launcher" in plan:
        launcher = plan["integrated_launcher"]
        if not isinstance(launcher, dict) or type(launcher.get("enabled")) is not bool:
            raise ZuulConfigError("Integrated launcher requires an explicit boolean enabled flag")
        required = {"enabled", "nodepool_fallback", "provider_projects"} if launcher["enabled"] else {"enabled"}
        if set(launcher) != required or (launcher["enabled"] and type(launcher["nodepool_fallback"]) is not bool):
            raise ZuulConfigError("Choose Nodepool fallback explicitly when enabling the integrated launcher")
        if launcher["enabled"] and (plan["zuul_version"] != "14.3.0" or any("allowed_labels" not in t for t in tenants)):
            raise ZuulConfigError("Integrated launcher requires reviewed Zuul 14.3.0 and explicit tenant labels")
        if launcher["enabled"]:
            selected = launcher["provider_projects"]
            if (not isinstance(selected, dict) or set(selected) != names
                    or any(not isinstance(selected[t["name"]], str) or selected[t["name"]] not in t["config_projects"] for t in tenants)
                    or len(set(selected.values())) != len(selected)):
                raise ZuulConfigError("Select a distinct trusted provider configuration project for every tenant")
    return plan


def validate_executor(value: object) -> None:
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ZuulConfigError("Executor selection requires an explicit boolean enable flag")
    if not value["enabled"]:
        if set(value) != {"enabled"}:
            raise ZuulConfigError("Disabled executor selection accepts no implicit runtime settings")
        return
    if set(value) != {"enabled", "image", "replicas", "kubernetes_seccomp", "kubernetes_apparmor", "docker_seccomp", "docker_apparmor"}:
        raise ZuulConfigError("Declare the executor image, replica count and host-policy selections explicitly")
    if not isinstance(value["image"], str) or not IMAGE.fullmatch(value["image"]):
        raise ZuulConfigError("Pin the separate reviewed executor image by digest")
    if type(value["replicas"]) is not int or not 2 <= value["replicas"] <= 16:
        raise ZuulConfigError("Executor replica count must be between two and sixteen")
    formats = {
        "kubernetes_seccomp": r"zuul/zuul-executor-[0-9a-f]{64}[.]json",
        "kubernetes_apparmor": r"zuul-executor-[0-9a-f]{64}",
        "docker_seccomp": r"/etc/docker/seccomp/zuul-executor-[0-9a-f]{64}[.]json",
        "docker_apparmor": r"zuul-executor-[0-9a-f]{64}",
    }
    for field, pattern in formats.items():
        selected = value[field]
        if not isinstance(selected, str) or (selected != "runtime-default" and not re.fullmatch(pattern, selected)):
            raise ZuulConfigError("Select runtime-default or a separately reviewed versioned local profile reference")


def configuration(plan: dict) -> str:
    plan = validate(plan)
    hosts = ",".join(f"{endpoint['host']}:{endpoint['port']}" for endpoint in plan["zookeeper"])
    gerrit = plan["gerrit"]
    https_port=gerrit.get("https_port",443)
    baseurl="https://"+gerrit["host"]+(":"+str(https_port) if https_port!=443 else "")
    result = f"""[zookeeper]
hosts = {hosts}
tls_cert = /etc/zuul/private/zk-client.pem
tls_key = /etc/zuul/private/zk-client.key
tls_ca = /etc/zuul/private/zk-ca.pem

[database]
dburi = %(ZUUL_DATABASE_URI)s

[keystore]
password = %(ZUUL_KEYSTORE_PASSWORD)s

[scheduler]
tenant_config = /etc/zuul/tenants.yaml
command_socket = /var/lib/zuul/scheduler.socket
pidfile = /var/lib/zuul/scheduler.pid

[merger]
git_dir = /var/lib/zuul/merger-git
command_socket = /var/lib/zuul/merger.socket
pidfile = /var/lib/zuul/merger.pid

[executor]
hostname = %(ZUUL_EXECUTOR_HOSTNAME)s
finger_port = 7900
state_dir = /var/lib/zuul
git_dir = /var/lib/zuul/executor-git
job_dir = /var/lib/zuul/builds
command_socket = /var/lib/zuul/executor.socket
pidfile = /var/lib/zuul/executor.pid
private_key_file = /etc/zuul/private/build-ssh.key
manage_ansible = false

[web]
root = {plan['web_url'].rstrip('/')}/
listen_address = 0.0.0.0
port = 9000
command_socket = /var/lib/zuul/web.socket
pidfile = /var/lib/zuul/web.pid

[connection "gerrit"]
driver = gerrit
server = {gerrit['host']}
canonical_hostname = {gerrit['host']}
baseurl = {baseurl}
port = {gerrit['port']}
user = {gerrit['user']}
sshkey = /etc/zuul/private/gerrit-ssh.key
password = %(ZUUL_GERRIT_HTTP_PASSWORD)s
keepalive = 60
"""
    if plan.get("integrated_launcher", {}).get("enabled", False):
        result += "\n[launcher]\ncommand_socket = /var/lib/zuul/launcher.socket\npidfile = /var/lib/zuul/launcher.pid\n\n[connection \"platform-static\"]\ndriver = static\n"
    return result


def tenant_configuration(plan: dict) -> str:
    plan = validate(plan)
    objects = []
    for tenant in plan["tenants"]:
        launcher = plan.get("integrated_launcher", {})
        provider_project = launcher.get("provider_projects", {}).get(tenant["name"]) if launcher.get("enabled", False) else None
        objects.append({"tenant": {"name": tenant["name"], "source": {"gerrit": {
            "config-projects": [{project: {"load-branch": tenant["config_branch"],
                **({"include-provider-config": True} if project == provider_project else {})}} for project in tenant["config_projects"]],
            "untrusted-projects": tenant["untrusted_projects"],
        }}}})
        if "allowed_labels" in tenant:
            objects[-1]["tenant"]["allowed-labels"] = ["^" + re.escape(value) + "$" for value in tenant["allowed_labels"]]
        if launcher.get("enabled", False):
            objects[-1]["tenant"]["use-nodepool"] = launcher["nodepool_fallback"]
    return yaml.safe_dump(objects, sort_keys=False)


def render(plan: dict, runtime: str) -> dict[str, str]:
    plan = validate(plan)
    if runtime not in RUNTIMES:
        raise ZuulConfigError("Unknown target runtime")
    contract = {"schema_version": 1, "environment": list(ENVIRONMENT),
        "private_files": list(PRIVATE_FILES), "executor_additional_files": ["build-ssh.key"],
        "executor_additional_environment": ["ZUUL_EXECUTOR_HOSTNAME"],
        "mount_root": "/etc/zuul/private", "requests_ca_bundle": "/etc/zuul/private/gerrit-ca.pem",
        "postgresql_uri_requirements": {"scheme": "postgresql", "sslmode": "verify-full",
                                         "sslrootcert": "/etc/zuul/private/postgres-ca.pem"}}
    status = {"schema_version": 1, "component": "zuul-configuration", "runtime": runtime,
        "declared_version": plan["zuul_version"], "image": plan["image"],
        "rendered": True, "deployed": False, "ci_verified": False, "ha_verified": False,
        "remaining": ["runtime-daemon-packaging", "zookeeper-quorum-and-tls", "database-provisioning-and-backups",
                      "gerrit-service-account-permissions-and-host-key-verification", "tenant-config-repository-and-jobs",
                      "node-supply-or-nodepool", "executor-bubblewrap-and-preinstalled-ansible",
                      "web-sso-and-gateway", "event-loss-and-replay-design", "node-loss-restore-and-live-job-acceptance"]}
    return {"zuul.conf": configuration(plan), "tenants.yaml": tenant_configuration(plan),
            "secret-contract.json": json.dumps(contract, indent=2) + "\n",
            "render-status.json": json.dumps(status, indent=2) + "\n"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        files = render(plan, args.runtime)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError, RuntimeError):
        print("Zuul configuration render failed; inspect private plan fields and new private output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "runtime": args.runtime, "deployed": False, "ci_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
