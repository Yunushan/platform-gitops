#!/usr/bin/env python3
"""Render opt-in GitLab and Traefik chart overlays for a dedicated SSH entry point."""
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
FIELDS = {"schema_version", "ssh_host", "public_port", "listener_port", "entrypoint", "ingress_class",
          "gitlab_namespace", "gitlab_release", "traefik_namespace", "traefik_instance"}


def render(plan: object) -> dict[str, str]:
    if (not isinstance(plan, dict) or set(plan) != FIELDS or type(plan["schema_version"]) is not int
            or plan["schema_version"] != 1 or not dns_name(plan["ssh_host"])):
        raise ValueError("Invalid GitLab SSH entry plan")
    for name in ("entrypoint", "ingress_class", "gitlab_namespace", "gitlab_release", "traefik_namespace", "traefik_instance"):
        if not isinstance(plan[name], str) or not label(plan[name]):
            raise ValueError("Invalid entry point or class")
    if plan["gitlab_namespace"] == plan["traefik_namespace"]:
        raise ValueError("Use a separately scoped ingress namespace")
    if (len(plan["entrypoint"]) > 15 or "--" in plan["entrypoint"] or not re.search(r"[a-z]", plan["entrypoint"])
            or plan["entrypoint"] in {"web", "websecure", "traefik", "metrics"}):
        raise ValueError("Use a distinct Kubernetes service port name")
    for field, minimum in (("public_port", 1), ("listener_port", 1024)):
        if type(plan[field]) is not int or not minimum <= plan[field] <= 65535:
            raise ValueError("Invalid TCP port")
    if plan["public_port"] in {80, 443} or plan["listener_port"] in {8000, 8443, 9000, 9100}:
        raise ValueError("Entry port collides with a standard web or management listener")
    gitlab = {"global": {"hosts": {"ssh": plan["ssh_host"]},
                         "ingress": {"provider": "traefik", "class": plan["ingress_class"]},
                         "shell": {"port": plan["public_port"]}},
              "gitlab": {"gitlab-shell": {"traefik": {"apiVersion": "traefik.io/v1alpha1", "entrypoint": plan["entrypoint"],
                                                         "ingressClassName": plan["ingress_class"]}}}}
    traefik = {"ports": {plan["entrypoint"]: {"port": plan["listener_port"],
               "expose": {"default": True}, "exposedPort": plan["public_port"], "protocol": "TCP"}}}
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "gitlab-shell-from-traefik", "namespace": plan["gitlab_namespace"]},
              "spec": {"podSelector": {"matchLabels": {"app": "gitlab-shell", "release": plan["gitlab_release"]}},
                       "policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                           "kubernetes.io/metadata.name": plan["traefik_namespace"]}}, "podSelector": {"matchLabels": {
                           "app.kubernetes.io/name": "traefik", "app.kubernetes.io/instance": plan["traefik_instance"]}}}],
                           "ports": [{"protocol": "TCP", "port": 2222}]}]}}
    status = {"schema_version": 1, "deployed": False, "ssh_verified": False,
              "requires": ["dedicated_uncontested_tcp_listener_and_dns", "traefik_crd_provider_and_ingressroute_tcp_crd",
                           "external_firewall_and_load_balancer", "gitlab_ssh_host_keys_and_authentication",
                           "git_clone_push_and_node_failure_validation"]}
    return {"gitlab-ssh-values.yaml": yaml.safe_dump(gitlab, sort_keys=False),
            "traefik-ssh-values.yaml": yaml.safe_dump(traefik, sort_keys=False),
            "gitlab-ssh-network-policy.yaml": yaml.safe_dump(policy, sort_keys=False),
            "render-status.json": json.dumps(status, indent=2) + "\n"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(plan), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("GitLab SSH render failed; check the private plan and new output directory.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "deployed": False, "ssh_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
