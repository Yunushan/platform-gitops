#!/usr/bin/env python3
"""Render clustered Grafana Alloy pod logging for an explicit Kubernetes target."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import yaml

from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE, dns_name, write_bundle
from render_gerrit_sites import label
from render_host_log_collector import RECEIVER, render_loki_writer, validate_gateway
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("rke2", "k3s", "k8s")
NAME = "platform-pod-logs"
CONFIG_DIR = "/etc/alloy/platform"
CREDENTIALS = "/run/loki"
PORT = 12345
POD_LABELS = {"app.kubernetes.io/name": NAME}


def validate(plan, runtime):
    fields = {"schema_version", "cluster", "namespace", "dns_domain", "source_namespaces",
              "gateway", "tenant", "username", "credential_secret", "image"}
    if (runtime not in RUNTIMES or not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or any(not label(plan[key]) for key in ("cluster", "namespace", "tenant", "credential_secret"))
            or not dns_name(plan["dns_domain"])):
        raise ValueError("Declare a supported Kubernetes runtime, cluster, and collector namespace")
    namespaces = plan["source_namespaces"]
    if (not isinstance(namespaces, list) or not 1 <= len(namespaces) <= 64
            or any(not label(namespace) for namespace in namespaces) or len(set(namespaces)) != len(namespaces)):
        raise ValueError("Select one to 64 distinct source namespaces")
    if plan["namespace"] in namespaces:
        raise ValueError("The collector namespace must be separate from the source namespaces")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("Select a dedicated collector namespace")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("Pin the Alloy image by digest")
    validate_gateway(plan)


def alloy_config(plan):
    # The original discovery labels must remain available to Alloy for log routing and clustering.
    labels = {
        "__meta_kubernetes_namespace": "namespace",
        "__meta_kubernetes_pod_name": "pod",
        "__meta_kubernetes_pod_container_name": "container",
        "__meta_kubernetes_pod_node_name": "node",
        "__meta_kubernetes_pod_label_app_kubernetes_io_name": "app",
    }
    rules = "\n".join(f'''  rule {{
    source_labels = ["{source}"]
    target_label = "{target}"
  }}''' for source, target in labels.items())
    return f'''discovery.kubernetes "workloads" {{
  role = "pod"
  namespaces {{
    names = {json.dumps(sorted(plan["source_namespaces"]))}
  }}
}}

discovery.relabel "workloads" {{
  targets = discovery.kubernetes.workloads.targets
{rules}
  rule {{
    target_label = "cluster"
    replacement = "{plan["cluster"]}"
  }}
}}

loki.source.kubernetes "workloads" {{
  targets = discovery.relabel.workloads.output
  forward_to = [{RECEIVER}]
  clustering {{
    enabled = true
  }}
}}

''' + render_loki_writer(plan, json.dumps(CREDENTIALS))


def render(plan, runtime):
    validate(plan, runtime)
    namespace = plan["namespace"]
    config = alloy_config(plan)
    metadata = {"name": NAME, "namespace": namespace}
    selector = {"matchLabels": POD_LABELS}
    peers = f"{NAME}.{namespace}.svc.{plan['dns_domain']}"
    objects = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": {"config.alloy": config}},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata, "spec": {
            "clusterIP": "None", "publishNotReadyAddresses": True, "selector": POD_LABELS,
            "ports": [{"name": "http", "port": PORT, "targetPort": "http"}],
        }},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": metadata,
         "spec": {"minAvailable": 1, "selector": selector}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata,
         "spec": {"podSelector": selector, "policyTypes": ["Ingress"], "ingress": [
             {"from": [{"podSelector": selector}], "ports": [{"protocol": "TCP", "port": PORT}]},
         ]}},
    ]
    for source_namespace in sorted(plan["source_namespaces"]):
        source_metadata = {"name": f"{NAME}-{namespace}", "namespace": source_namespace}
        objects.extend([
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": source_metadata, "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": source_metadata,
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": source_metadata["name"]},
             "subjects": [{"kind": "ServiceAccount", **metadata}]},
        ])
    container = {
        "name": "alloy", "image": plan["image"], "imagePullPolicy": "IfNotPresent",
        "args": ["run", f"--server.http.listen-addr=0.0.0.0:{PORT}", "--disable-reporting",
                 "--server.http.enable-pprof=false", "--server.http.disable-support-bundle",
                 "--storage.path=/var/lib/alloy", "--cluster.enabled", f"--cluster.name={plan['cluster']}-{namespace}-logs",
                 f"--cluster.join-addresses=dns+{peers}:{PORT}", "--cluster.wait-for-size=1",
                 "--cluster.rejoin-interval=5s", CONFIG_DIR + "/config.alloy"],
        "ports": [{"name": "http", "containerPort": PORT}],
        "readinessProbe": {"httpGet": {"path": "/-/ready", "port": "http"}, "periodSeconds": 5},
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                      "limits": {"memory": "768Mi", "ephemeral-storage": "512Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [
            {"name": "config", "mountPath": CONFIG_DIR, "readOnly": True},
            {"name": "gateway", "mountPath": CREDENTIALS, "readOnly": True},
            {"name": "state", "mountPath": "/var/lib/alloy"},
        ],
    }
    objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata, "spec": {
        "replicas": 2, "selector": selector,
        "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
        "template": {
            "metadata": {"labels": POD_LABELS,
                         "annotations": {"platform.gitops/config-sha256": hashlib.sha256(config.encode()).hexdigest()}},
            "spec": {
                "serviceAccountName": NAME,
                "nodeSelector": {"kubernetes.io/os": "linux"},
                "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                    {"labelSelector": selector, "topologyKey": "kubernetes.io/hostname"},
                ]}},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534, "fsGroup": 65534,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container],
                "volumes": [
                    {"name": "config", "configMap": {"name": NAME}},
                    {"name": "gateway", "secret": {"secretName": plan["credential_secret"], "defaultMode": 0o440,
                                                  "items": [{"key": key, "path": key}
                                                            for key in ("loki-ca.pem", "loki-password")]}},
                    {"name": "state", "emptyDir": {"sizeLimit": "256Mi"}},
                ],
            },
        },
    }})
    return {"pod-logs.yaml": yaml.safe_dump_all(objects, sort_keys=False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=65536))
        write_bundle(render(plan, args.runtime), args.output_dir, private_root=ROOT / "private")
    except (ValueError, OSError):
        print("Kubernetes pod log collector render failed. Review the private plan and output path.", file=sys.stderr)
        return 1
    print(json.dumps({"rendered": True, "deployed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
