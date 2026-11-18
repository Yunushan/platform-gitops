#!/usr/bin/env python3
"""Render an opt-in single-active Jenkins controller, not an HA claim."""
import argparse
from pathlib import Path
import sys

import yaml
from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE
from render_gerrit_sites import label
from render_tekton import selector
from strict_json import loads_strict_json


def render(plan):
    fields = {"schema_version", "name", "namespace", "image", "home_pvc", "node_selector"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-controller-plan")
    for key in ("name", "namespace", "home_pvc"):
        if not isinstance(plan[key], str) or not label(plan[key]) or len(plan[key]) > 63:
            raise ValueError("invalid-resource-name")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("dedicated-namespace-required")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("digest-pinned-controller-image-required")
    selector(plan["node_selector"])
    name, namespace = plan["name"], plan["namespace"]
    metadata = {"name": name, "namespace": namespace}
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/component": "jenkins-controller"}
    http = {"httpGet": {"path": "/login", "port": "http"}, "timeoutSeconds": 5}
    pod = {"serviceAccountName": name, "automountServiceAccountToken": False,
           "nodeSelector": plan["node_selector"],
           "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                               "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [{"name": "jenkins", "image": plan["image"], "imagePullPolicy": "IfNotPresent",
                           "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                           "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "3Gi"}},
                           "startupProbe": {**http, "periodSeconds": 10, "failureThreshold": 60},
                           "readinessProbe": {**http, "periodSeconds": 10, "failureThreshold": 3},
                           "livenessProbe": {**http, "periodSeconds": 20, "failureThreshold": 6},
                           "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                           "volumeMounts": [{"name": "home", "mountPath": "/var/jenkins_home"}, {"name": "tmp", "mountPath": "/tmp"}]}],
           "volumes": [{"name": "home", "persistentVolumeClaim": {"claimName": plan["home_pvc"]}},
                       {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}}]}
    return [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata, "automountServiceAccountToken": False},
            {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
             "spec": {"type": "ClusterIP", "selector": labels, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
             "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": pod}}}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = render(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
    except Exception:
        print("Jenkins controller render failed; review the plan.", file=sys.stderr)
        return 1
    print(yaml.safe_dump_all(result, sort_keys=False), end="")
    return 0


if __name__ == "__main__": sys.exit(main())
