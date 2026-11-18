#!/usr/bin/env python3
"""Optional inline Tekton task for isolated static build and Harbor publication steps."""

import re

from publish_harbor_static_image import validate


def task_spec(plan, *, build_image, publisher_image, build_argv, program_map, input_map, robot_secret):
    validate(plan)
    for image in (build_image, publisher_image):
        if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image): raise ValueError("Pin both task images by digest")
    for value in (program_map, input_map, robot_secret):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 63: raise ValueError("Use explicit Kubernetes input names")
    if not isinstance(build_argv, list) or not 1 <= len(build_argv) <= 32 or any(not isinstance(a, str) or not a or len(a) > 8192 or "$(" in a for a in build_argv): raise ValueError("Use explicit literal build arguments")
    if plan["credential_file"] != "/run/robot/credential.json" or plan["ca_file"] != "/publisher-input/ca.crt": raise ValueError("Use the isolated reviewed publication mounts")
    security = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
    resources = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}}
    return {"results": [{"name": "image-receipt", "type": "string"}], "steps": [
        {"name": "build", "image": build_image, "command": build_argv, "workingDir": "/workspace/output",
            "env": [{"name": "TMPDIR", "value": "/tmp"}, {"name": "HOME", "value": "/tmp"}],
            "securityContext": security, "computeResources": resources,
            "volumeMounts": [{"name": "output", "mountPath": "/workspace/output"}, {"name": "build-tmp", "mountPath": "/tmp"}]},
        {"name": "publish", "image": publisher_image, "command": ["python3", "-I", "-B", "-c",
            "import sys; sys.path.insert(0, '/publisher'); from publish_harbor_static_image import main; "
            "f=open(sys.argv[1], 'w'); sys.stdout=f; code=main(sys.argv[2:]); f.flush(); f.close(); raise SystemExit(code)"],
            "args": ["$(results.image-receipt.path)", "--plan", "/publisher-input/plan.json", "--output", "/workspace/output"],
            "env": [{"name": "TMPDIR", "value": "/tmp"}], "securityContext": security, "computeResources": resources,
            "volumeMounts": [{"name": "output", "mountPath": "/workspace/output", "readOnly": True},
                {"name": "program", "mountPath": "/publisher", "readOnly": True}, {"name": "input", "mountPath": "/publisher-input", "readOnly": True},
                {"name": "robot", "mountPath": "/run/robot", "readOnly": True}, {"name": "publisher-tmp", "mountPath": "/tmp"}]}],
        "volumes": [{"name": "output", "emptyDir": {"sizeLimit": "16Mi"}}, {"name": "build-tmp", "emptyDir": {"sizeLimit": "64Mi"}},
            {"name": "publisher-tmp", "emptyDir": {"sizeLimit": "64Mi"}}, {"name": "program", "configMap": {"name": program_map, "defaultMode": 0o444}},
            {"name": "input", "configMap": {"name": input_map, "defaultMode": 0o444}},
            {"name": "robot", "secret": {"secretName": robot_secret, "defaultMode": 0o440, "items": [{"key": "credential.json", "path": "credential.json"}]}}]}
