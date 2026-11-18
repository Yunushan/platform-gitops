#!/usr/bin/env python3
"""Render an opt-in GitLab chart overlay for a fresh Praefect installation.

Rendering does not create a database, deploy the chart, or migrate repositories.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import dns_name, write_bundle
from render_gerrit_sites import label
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"schema_version", "fresh_install", "virtual_storage", "storage_class",
          "storage_size_gi", "postgres_host", "postgres_port", "postgres_database",
          "postgres_user", "postgres_secret", "postgres_secret_key", "postgres_ca_secret",
          "gitaly_auth_secret", "praefect_auth_secret"}
TLS_FIELDS = {"gitaly_tls_secret", "praefect_tls_secret", "repository_ca_secret"}
REPOSITORY_TIER_LABEL = "platform.gitops/repository-tier"


def repository_network_component() -> dict[str, str]:
    """Narrow the premium profile's same-namespace ingress for this release."""
    component = {
        "apiVersion": "kustomize.config.k8s.io/v1alpha1",
        "kind": "Component",
        "resources": ["repository-network-policies.yaml"],
        "patches": [{
            "target": {"group": "networking.k8s.io", "version": "v1", "kind": "NetworkPolicy",
                       "name": "platform-allow-same-namespace"},
            "patch": yaml.safe_dump([
                {"op": "test", "path": "/spec/ingress", "value": [{"from": [{"podSelector": {}}]}]},
                {"op": "remove", "path": "/spec/ingress"},
                {"op": "replace", "path": "/spec/policyTypes", "value": ["Egress"]},
            ], sort_keys=False),
        }],
    }
    policies = [{
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "gitlab-allow-same-namespace-ingress"},
        "spec": {
            "podSelector": {"matchExpressions": [
                {"key": REPOSITORY_TIER_LABEL, "operator": "NotIn", "values": ["true"]},
            ]},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": {}}]}],
        },
    }]
    clients_by_role = {
        "gitaly": ["gitaly", "praefect"],
        "praefect": ["gitaly", "gitlab-shell", "sidekiq", "toolbox", "webservice"],
    }
    for role, clients in clients_by_role.items():
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"gitlab-allow-{role}-ingress"},
            "spec": {
                "podSelector": {"matchLabels": {
                    "app": role, "release": "gitlab", REPOSITORY_TIER_LABEL: "true",
                }},
                "policyTypes": ["Ingress"],
                "ingress": [{
                    "from": [{"podSelector": {
                        "matchLabels": {"release": "gitlab"},
                        "matchExpressions": [{"key": "app", "operator": "In", "values": clients}],
                    }}],
                    "ports": [{"protocol": "TCP", "port": 8076}],
                }, {
                    "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}],
                    "ports": [{"protocol": "TCP", "port": 9236}],
                }],
            },
        })
    return {
        "kustomization.yaml": yaml.safe_dump(component, sort_keys=False),
        "repository-network-policies.yaml": yaml.safe_dump_all(policies, sort_keys=False),
    }


def render(plan: object) -> dict[str, str]:
    if (not isinstance(plan, dict) or type(plan.get("schema_version")) is not int
            or plan["schema_version"] not in (1, 2, 3) or plan.get("fresh_install") is not True):
        raise ValueError("A complete fresh-install plan is required")
    repository_tls = plan["schema_version"] >= 2
    repository_isolation = plan["schema_version"] == 3
    if set(plan) != FIELDS | (TLS_FIELDS if repository_tls else set()):
        raise ValueError("A complete fresh-install plan is required")
    for key in ("virtual_storage", "storage_class", "postgres_secret", "postgres_ca_secret",
                "gitaly_auth_secret", "praefect_auth_secret"):
        if not isinstance(plan[key], str) or not label(plan[key]):
            raise ValueError("Invalid Kubernetes name")
    if plan["virtual_storage"] != "default":
        raise ValueError("The fresh-install chart requires a default virtual storage")
    if (not isinstance(plan["postgres_host"], str) or not dns_name(plan["postgres_host"])
            or type(plan["postgres_port"]) is not int or not 1 <= plan["postgres_port"] <= 65535
            or type(plan["storage_size_gi"]) is not int or not 20 <= plan["storage_size_gi"] <= 100000
            or not isinstance(plan["postgres_secret_key"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", plan["postgres_secret_key"])):
        raise ValueError("Invalid database or storage settings")
    for key in ("postgres_database", "postgres_user"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", plan[key]):
            raise ValueError("Invalid PostgreSQL identifier")
    if len({plan["postgres_secret"], plan["gitaly_auth_secret"], plan["praefect_auth_secret"]}) != 3:
        raise ValueError("Use separate database and service credentials")
    if repository_tls:
        if any(not label(plan[key]) for key in TLS_FIELDS):
            raise ValueError("Invalid repository TLS Secret name")
        secret_names = [plan[key] for key in (*TLS_FIELDS, "postgres_secret", "postgres_ca_secret",
                                             "gitaly_auth_secret", "praefect_auth_secret")]
        if len(set(secret_names)) != len(secret_names):
            raise ValueError("Use distinct Secrets for trust roots, service certificates and credentials")
    overlay = {
        "global": {
            "gitaly": {"authToken": {"secret": plan["gitaly_auth_secret"], "key": "token"}},
            "praefect": {
                "enabled": True, "replaceInternalGitaly": True, "autoMigrate": False,
                "authToken": {"secret": plan["praefect_auth_secret"], "key": "token"},
                "dbSecret": {"secret": plan["postgres_secret"], "key": plan["postgres_secret_key"]},
                "psql": {"host": plan["postgres_host"], "port": plan["postgres_port"],
                         "user": plan["postgres_user"], "dbName": plan["postgres_database"],
                         "sslMode": "verify-full"},
                "virtualStorages": [{"name": plan["virtual_storage"], "gitalyReplicas": 3,
                                     "defaultReplicationFactor": 3, "maxUnavailable": 1}],
            },
            "certificates": {"customCAs": [{"secret": plan["postgres_ca_secret"], "keys": ["postgres-ca.crt"]}]
                             if repository_tls else [{"secret": plan["postgres_ca_secret"]}]},
        },
        "gitlab": {
            "gitaly": {"antiAffinity": "hard", "affinity": {"podAntiAffinity": {"topologyKey": "kubernetes.io/hostname"}},
                       "persistence": {"storageClass": plan["storage_class"], "size": f'{plan["storage_size_gi"]}Gi'}},
            "praefect": {"replicas": 3, "antiAffinity": "hard",
                         "migrationJob": {"enabled": True},
                         "affinity": {"podAntiAffinity": {"topologyKey": "kubernetes.io/hostname"}},
                         "extraEnv": {"PGSSLROOTCERT": "/etc/ssl/certs/ca-certificates.crt"}},
        },
    }
    if repository_tls:
        values = overlay["global"]
        values["gitaly"]["tls"] = {"enabled": True}
        values["praefect"]["tls"] = {"enabled": True, "secretName": plan["praefect_tls_secret"]}
        values["praefect"]["virtualStorages"][0]["tlsSecretName"] = plan["gitaly_tls_secret"]
        values["certificates"]["customCAs"].append(
            {"secret": plan["repository_ca_secret"], "keys": ["repository-ca.crt"]}
        )
    network_files = {}
    if repository_isolation:
        for role in ("gitaly", "praefect"):
            overlay["gitlab"][role]["podLabels"] = {REPOSITORY_TIER_LABEL: "true"}
        network_files = repository_network_component()
    status = {"schema_version": 1, "fresh_install_only": True, "deployed": False, "ha_verified": False,
              "requires": ["separately_provisioned_praefect_database_and_credentials", "trusted_database_ca_and_hostname",
                           "three_independent_storage_nodes", "chart_render_and_tls_runtime_verification",
                           "repository_migration_and_restore_plan", "node_failure_and_ci_acceptance"]}
    if repository_isolation:
        status["requires"].append("premium_gitlab_release_and_enforced_repository_network_policies")
    return {"praefect-values.yaml": yaml.safe_dump(overlay, sort_keys=False),
            "render-status.json": json.dumps(status, indent=2) + "\n", **network_files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(plan), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("GitLab Praefect render failed; check the private fresh-install plan and output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "deployed": False, "ha_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
