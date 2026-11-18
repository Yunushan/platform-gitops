#!/usr/bin/env python3
"""Render an opt-in GitLab GitOps scheduler CronJob; never install it."""
import argparse
from ipaddress import ip_address
import json
from pathlib import Path
import re
import sys

import yaml
from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE
from render_gerrit_sites import label
from strict_json import loads_strict_json


FIELDS = {"schema_version", "name", "namespace", "image", "schedule", "plan_configmap", "plan_sha256",
          "credential_secret", "proof_pvc", "suspend", "apply", "wait_seconds", "active_deadline_seconds"}


def destination_ports(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 8
            or any(type(port) is not int or not 1 <= port <= 65535 for port in values)
            or len(set(values)) != len(values)):
        raise ValueError("invalid-egress-ports")
    return [{"ports": [{"port": str(port), "protocol": "TCP"} for port in sorted(values)]}]


def destination_addresses(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError("invalid-egress-addresses")
    addresses = []
    for value in values:
        if not isinstance(value, str) or "%" in value:
            raise ValueError("invalid-egress-address")
        address = ip_address(value)
        if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:
            raise ValueError("invalid-egress-address")
        addresses.append(f"{address}/{address.max_prefixlen}")
    if len(set(addresses)) != len(addresses):
        raise ValueError("duplicate-egress-address")
    return sorted(addresses)


def render_egress(plan, labels):
    policy = plan["egress"]
    if not isinstance(policy, dict) or set(policy) != {"gitlab_addresses", "gitlab_port", "api"}:
        raise ValueError("invalid-egress-policy")
    api = policy["api"]
    if not isinstance(api, dict) or set(api) not in ({"kind", "ports"}, {"kind", "ports", "addresses"}):
        raise ValueError("invalid-api-egress")
    if api["kind"] == "in-cluster" and "addresses" not in api:
        api_peer = {"toEntities": ["kube-apiserver"]}
    elif api["kind"] == "external" and "addresses" in api:
        api_peer = {"toCIDR": destination_addresses(api["addresses"])}
    else:
        raise ValueError("invalid-api-egress")
    name = plan["name"]
    return {
        "apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
        "metadata": {"name": name, "namespace": plan["namespace"]},
        "spec": {"endpointSelector": {"matchLabels": {
            **labels, "io.cilium.k8s.policy.serviceaccount": name,
        }}, "egress": [
            {"toEndpoints": [{"matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns",
            }}], "toPorts": [{"ports": [{"port": "53", "protocol": "UDP"}, {"port": "53", "protocol": "TCP"}]}]},
            {"toCIDR": destination_addresses(policy["gitlab_addresses"]),
             "toPorts": destination_ports([policy["gitlab_port"]])},
            {**api_peer, "toPorts": destination_ports(api["ports"])},
        ]},
    }


def valid_schedule(value):
    if not isinstance(value, str) or len(value) > 80:
        return False
    fields = value.split(" ")
    if len(fields) != 5:
        return False
    for field, (low, high) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        for part in field.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", part)
            if not match:
                return False
            base, step = match.groups()
            if step is not None and not 1 <= int(step) <= high - low + 1:
                return False
            if base != "*":
                bounds = [int(n) for n in base.split("-")]
                if any(not low <= n <= high for n in bounds) or len(bounds) == 2 and bounds[0] > bounds[1]:
                    return False
    return True


def render(plan):
    if (not isinstance(plan, dict) or type(plan.get("schema_version")) is not int
            or plan["schema_version"] not in {1, 2}
            or set(plan) != FIELDS | ({"egress"} if plan["schema_version"] == 2 else set())):
        raise ValueError("invalid-plan")
    for key in ("name", "namespace", "plan_configmap", "credential_secret", "proof_pvc"):
        if not isinstance(plan[key], str) or not label(plan[key]) or len(plan[key]) > (52 if key == "name" else 63):
            raise ValueError("invalid-resource-name")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("dedicated-namespace-required")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("digest-pinned-image-required")
    if not isinstance(plan["plan_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["plan_sha256"]):
        raise ValueError("reviewed-plan-digest-required")
    if not valid_schedule(plan["schedule"]):
        raise ValueError("invalid-cron-schedule")
    if any(type(plan[k]) is not bool for k in ("suspend", "apply")):
        raise ValueError("explicit-execution-policy-required")
    wait, deadline = plan["wait_seconds"], plan["active_deadline_seconds"]
    if (type(wait) is not int or not 0 <= wait <= 3600 or type(deadline) is not int
            or not 120 <= deadline <= 7200 or deadline < wait + 120):
        raise ValueError("invalid-deadline")
    name, namespace = plan["name"], plan["namespace"]
    args = ["/opt/platform/platform-gitops/scripts/gitlab_gitops_schedule.py", "--plan", "/run/reviewed/plan.json",
            "--plan-sha256", plan["plan_sha256"], "--wait-seconds", str(wait)]
    if plan["apply"]: args.append("--apply")
    pod = {"serviceAccountName": name, "automountServiceAccountToken": False, "restartPolicy": "Never",
           "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "fsGroup": 65532,
                               "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [{"name": "handoff", "image": plan["image"], "imagePullPolicy": "IfNotPresent",
                           "command": ["/usr/bin/python3", "-B"], "args": args,
                           "env": [{"name": "HOME", "value": "/tmp"}],
                           "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                           "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                               "capabilities": {"drop": ["ALL"]}},
                           "volumeMounts": [{"name": "credentials", "mountPath": "/run/platform/promoter", "readOnly": True},
                                            {"name": "reviewed", "mountPath": "/run/reviewed", "readOnly": True},
                                            {"name": "proof", "mountPath": "/opt/platform/platform-gitops/private"},
                                            {"name": "tmp", "mountPath": "/tmp"}]}],
           "volumes": [{"name": "credentials", "secret": {"secretName": plan["credential_secret"], "defaultMode": 288,
                        "items": [{"key": key, "path": key} for key in ("api-token", "token", "ca.crt", "cd-kubeconfig")]}},
                       {"name": "reviewed", "configMap": {"name": plan["plan_configmap"], "items": [{"key": "plan.json", "path": "plan.json"}]}},
                       {"name": "proof", "persistentVolumeClaim": {"claimName": plan["proof_pvc"]}},
                       {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]}
    metadata = {"name": name, "namespace": namespace}
    labels = {"app.kubernetes.io/name": name}
    objects = [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata, "automountServiceAccountToken": False},
               {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": metadata,
             "spec": {"schedule": plan["schedule"], "suspend": plan["suspend"], "concurrencyPolicy": "Forbid",
                      "startingDeadlineSeconds": 120, "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
                      "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": deadline,
                                               "template": {"metadata": {"labels": labels}, "spec": pod}}}}}]
    if plan["schema_version"] == 2:
        objects.insert(1, render_egress(plan, labels))
    return objects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        documents = render(loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024)))
    except Exception:
        print("Scheduler rendering failed; review the private plan.", file=sys.stderr)
        return 1
    print(yaml.safe_dump_all(documents, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
