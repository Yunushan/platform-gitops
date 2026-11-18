#!/usr/bin/env python3
"""Render a single explicitly started Kubernetes poller pod; never apply it."""
import argparse
from pathlib import Path
import re
import sys

import yaml
from bounded_file import read_bounded_text
from render_gerrit_router import IMAGE
from render_gerrit_sites import label
from render_tekton import selector
from strict_json import loads_strict_json


def render(plan):
    fields = {"schema_version", "name", "namespace", "image", "state_pvc", "trigger_secret", "result_secret",
              "trigger_configmap", "result_configmap", "trigger_sha256", "result_sha256", "interval_seconds", "node_selector"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("invalid-poller-plan")
    for key in ("name", "namespace", "state_pvc", "trigger_secret", "result_secret", "trigger_configmap", "result_configmap"):
        if not isinstance(plan[key], str) or not label(plan[key]) or len(plan[key]) > 63: raise ValueError("invalid-resource-name")
    if plan["namespace"] in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("dedicated-namespace-required")
    if plan["trigger_secret"] == plan["result_secret"]: raise ValueError("separate-credential-secrets-required")
    if not isinstance(plan["image"], str) or not IMAGE.fullmatch(plan["image"]): raise ValueError("digest-pinned-image-required")
    for key in ("trigger_sha256", "result_sha256"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]): raise ValueError("invalid-policy-digest")
    if type(plan["interval_seconds"]) is not int or not 10 <= plan["interval_seconds"] <= 3600: raise ValueError("invalid-poll-interval")
    selector(plan["node_selector"])
    common = ["--watch", "--interval-seconds", str(plan["interval_seconds"]), "--apply"]
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    resources = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}}
    def container(kind):
        trigger = kind == "trigger"
        args = ["/usr/local/bin/python3", "-B", "/opt/platform/scripts/" + ("scan_jenkins_gerrit.py" if trigger else "scan_jenkins_gerrit_results.py"),
                "--policy", "/run/policy/policy.json", "--policy-sha256", plan["trigger_sha256" if trigger else "result_sha256"],
                "--input-dir", "/run/credentials", *( ["--journal-dir", "/opt/platform/private/queue"] if trigger else
                ["--queue-dir", "/opt/platform/private/queue", "--review-dir", "/opt/platform/private/reviews"]), *common]
        return {"name": kind, "image": plan["image"], "imagePullPolicy": "IfNotPresent", "command": args,
                "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}], "resources": resources, "securityContext": security,
                "volumeMounts": [{"name": "state", "mountPath": "/opt/platform/private"},
                                 {"name": kind + "-credentials", "mountPath": "/run/credentials", "readOnly": True},
                                 {"name": kind + "-policy", "mountPath": "/run/policy", "readOnly": True},
                                 {"name": "tmp", "mountPath": "/tmp"}]}
    volumes = [{"name": "state", "persistentVolumeClaim": {"claimName": plan["state_pvc"]}},
               {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}]
    for kind in ("trigger", "result"):
        volumes.extend([{"name": kind + "-credentials", "secret": {"secretName": plan[kind + "_secret"], "defaultMode": 288}},
                        {"name": kind + "-policy", "configMap": {"name": plan[kind + "_configmap"], "defaultMode": 292}}])
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": plan["name"], "namespace": plan["namespace"],
            "labels": {"app.kubernetes.io/name": plan["name"], "app.kubernetes.io/component": "jenkins-gerrit-pollers"}},
            "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "nodeSelector": plan["node_selector"],
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "initContainers": [{"name": "state-check", "image": plan["image"], "imagePullPolicy": "IfNotPresent",
                         "command": ["/usr/local/bin/python3", "-B", "/opt/platform/scripts/check_jenkins_poller_state.py"],
                         "resources": resources, "securityContext": security,
                         "volumeMounts": [{"name": "state", "mountPath": "/opt/platform/private"}, {"name": "tmp", "mountPath": "/tmp"}]}],
                     "containers": [container("trigger"), container("result")], "volumes": volumes}}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try: pod = render(loads_strict_json(read_bounded_text(args.plan, max_bytes=16384)))
    except Exception:
        print("Poller pod rendering failed; review the private plan.", file=sys.stderr); return 1
    print(yaml.safe_dump(pod, sort_keys=False), end=""); return 0


if __name__ == "__main__": sys.exit(main())
