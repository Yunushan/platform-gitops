#!/usr/bin/env python3
"""Render the read-only Kubernetes identity for a reviewed GitLab handoff."""

import argparse
from collections import defaultdict
from pathlib import Path
import re
import sys

import yaml

from bounded_file import read_bounded_text
from ci_gitops_handoff import sha256
from gitlab_gitops_handoff import materialize
from render_gerrit_sites import label
from strict_json import loads_strict_json


RBAC = "rbac.authorization.k8s.io/v1"
SYSTEM_NAMESPACES = {"default", "kube-system", "kube-public", "kube-node-lease"}


def render(plan, expected_sha256, *, namespace, account):
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or not isinstance(plan, dict) or plan.get("schema_version") != 2 or sha256(plan) != expected_sha256):
        raise ValueError("Require the exact reviewed reusable GitLab handoff plan")
    checked = materialize(plan, local_only=True)
    if not label(namespace) or namespace in SYSTEM_NAMESPACES or not label(account) or account == "default":
        raise ValueError("Use a dedicated reader identity in a dedicated namespace")
    cd = checked["handoff"]["cd"]
    target = cd["management_target"]
    name = "gitlab-handoff-" + sha256([target["cluster_uid"], namespace, account, expected_sha256])[:20]
    subject = {"kind": "ServiceAccount", "name": account, "namespace": namespace}
    annotations = {"platform.gitops/access-target-cluster-uid": target["cluster_uid"],
                   "platform.gitops/gitlab-handoff-plan-sha256": expected_sha256}

    def rule(group, resource, selected):
        return {"apiGroups": [group], "resources": [resource], "resourceNames": [selected], "verbs": ["get"]}

    def binding(kind):
        return {"roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": name},
                "subjects": [subject]}

    def resource(kind, *, object_namespace=None, object_name=name, **spec):
        metadata = {"name": object_name, "annotations": annotations}
        if object_namespace is not None:
            metadata["namespace"] = object_namespace
        return {"apiVersion": "v1" if kind == "ServiceAccount" else RBAC, "kind": kind, "metadata": metadata, **spec}

    rules = defaultdict(list)
    if cd["kind"] == "argocd":
        rules[cd["namespace"]].append(rule("argoproj.io", "applications", cd["name"]))
    else:
        rules[cd["namespace"]].append(rule("kustomize.toolkit.fluxcd.io", "kustomizations", cd["name"]))
        rules[cd["source_namespace"]].append(rule("source.toolkit.fluxcd.io", "gitrepositories", cd["source_name"]))

    objects = [
        resource("ServiceAccount", object_namespace=namespace, object_name=account, automountServiceAccountToken=False),
        resource("ClusterRole", rules=[rule("", "namespaces", "kube-system")]),
        resource("ClusterRoleBinding", **binding("ClusterRole")),
    ]
    for selected_namespace, selected_rules in sorted(rules.items()):
        objects.extend([
            resource("Role", object_namespace=selected_namespace, rules=selected_rules),
            resource("RoleBinding", object_namespace=selected_namespace, **binding("Role")),
        ])
    return objects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--namespace", required=True, help="Dedicated reader namespace on the reviewed CD cluster")
    parser.add_argument("--account", required=True, help="Dedicated read-only service account name")
    args = parser.parse_args()
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024))
        documents = render(plan, args.plan_sha256, namespace=args.namespace, account=args.account)
    except Exception:
        print("GitLab handoff access rendering failed. Review the plan and reader identity.", file=sys.stderr)
        return 1
    print(yaml.safe_dump_all(documents, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
