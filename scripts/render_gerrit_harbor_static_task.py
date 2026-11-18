#!/usr/bin/env python3
"""Optional fixed-revision Gerrit checkout, static build and Harbor publication task."""

import copy
from pathlib import Path
import re

from bounded_file import read_bounded_text
from render_harbor_static_task import task_spec as static_task
from source_bound_static_image import PROGRAMS, checkout_program_sha256, validate_source


def programs():
    return {name: read_bounded_text(Path(__file__).parent / name, max_bytes=256 * 1024) for name in PROGRAMS}


def task_spec(plan, source, *, checkout_image, build_image, publisher_image, build_argv, program_map, input_map, robot_secret, source_map, source_secret):
    validate_source(source)
    if plan["revision"] != source["revision"] or source["checkout_program_sha256"] != checkout_program_sha256(): raise ValueError("Source revision or checkout helper binding changed")
    if not isinstance(checkout_image, str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", checkout_image): raise ValueError("Pin the checkout image")
    for name in (source_map, source_secret):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 63: raise ValueError("Use explicit source Kubernetes inputs")
    if source_secret == robot_secret or source_map == input_map: raise ValueError("Separate source and registry inputs")
    task = static_task(plan, build_image=build_image, publisher_image=publisher_image, build_argv=build_argv, program_map=program_map, input_map=input_map, robot_secret=robot_secret)
    build, publish = task["steps"]
    build["volumeMounts"].append({"name": "source-tree", "mountPath": "/workspace/source", "readOnly": True})
    build["env"].append({"name": "PYTHONDONTWRITEBYTECODE", "value": "1"})
    publish["command"][-1] = publish["command"][-1].replace("from publish_harbor_static_image import main", "from source_bound_static_image import main")
    publish["args"].extend(["--mode", "publish", "--source-plan", "/source-input/source.json", "--witness", "/run/provenance/checkout.json"])
    publish["volumeMounts"].extend([{"name": "source-input", "mountPath": "/source-input", "readOnly": True}, {"name": "witness", "mountPath": "/run/provenance", "readOnly": True}])
    checkout = {"name": "checkout", "image": checkout_image, "command": ["python3", "-I", "-B", "-c",
        "import sys; sys.path.insert(0, '/publisher'); from source_bound_static_image import main; raise SystemExit(main())"],
        "args": ["--mode", "checkout", "--source-plan", "/source-input/source.json", "--workspace", "/workspace/source", "--witness", "/run/provenance/checkout.json", "--credentials", "/run/source"],
        "env": [{"name": "TMPDIR", "value": "/tmp"}], "securityContext": copy.deepcopy(build["securityContext"]), "computeResources": copy.deepcopy(build["computeResources"]),
        "volumeMounts": [{"name": "program", "mountPath": "/publisher", "readOnly": True}, {"name": "source-input", "mountPath": "/source-input", "readOnly": True},
            {"name": "source-credential", "mountPath": "/run/source", "readOnly": True}, {"name": "source-tree", "mountPath": "/workspace/source"},
            {"name": "witness", "mountPath": "/run/provenance"}, {"name": "checkout-tmp", "mountPath": "/tmp"}]}
    task["steps"].insert(0, checkout)
    task["volumes"].extend([{"name": "source-tree", "emptyDir": {"sizeLimit": "2Gi"}}, {"name": "witness", "emptyDir": {"sizeLimit": "16Ki"}},
        {"name": "checkout-tmp", "emptyDir": {"sizeLimit": "64Mi"}}, {"name": "source-input", "configMap": {"name": source_map, "defaultMode": 0o444}},
        {"name": "source-credential", "secret": {"secretName": source_secret, "defaultMode": 0o440, "items": [{"key": "token", "path": "token"}, {"key": "ca.crt", "path": "ca.crt"}]}}])
    return task
