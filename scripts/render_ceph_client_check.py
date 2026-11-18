#!/usr/bin/env python3
"""Render fresh RBD handoff and concurrent CephFS clients on a reviewed provider."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

import yaml

from bounded_file import read_bounded_bytes
import ceph_client_io as program
import render_ceph_storage as storage
import render_rook_bootstrap as bootstrap
from render_gerrit_router import write_bundle
from standalone_python import standalone_script
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
UNVERIFIED = {**program.UNVERIFIED, "kubernetes_client_mounts_verified": False, "kubernetes_data_roundtrip_verified": False}


def validate(plan, storage_plan, runtime):
    storage.validate(storage_plan, runtime)
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "operation_id", "storage_plan_sha256", "storage_proof_sha256", "namespace", "namespace_uid", "nodes", "image", "resources", "payload_bytes", "timeout_seconds", "admitted_sha256"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not storage.uid(plan["operation_id"])
            or not storage.sha(plan["storage_proof_sha256"]) or plan["storage_plan_sha256"] != storage.fingerprint(storage_plan)
            or not storage.name(plan["namespace"]) or not storage.uid(plan["namespace_uid"]) or not storage.image(plan["image"])
            or plan["namespace"] in {storage_plan["provider"]["namespace"], "default", "kube-system"}
            or plan["admitted_sha256"] is not None and not storage.sha(plan["admitted_sha256"])): raise ValueError("Use a separate private canary scope with original storage proof")
    nodes = plan["nodes"]
    if not isinstance(nodes, list) or len(nodes) != 2: raise ValueError("Select two independent client nodes")
    for node in nodes:
        if (not isinstance(node, dict) or set(node) != {"name", "uid", "failure_domain"} or not storage.resource(node["name"])
                or not storage.uid(node["uid"]) or not storage.name(node["failure_domain"])): raise ValueError("Pin each client node identity and domain")
    if any(len({n[k] for n in nodes}) != 2 for k in ("name", "uid", "failure_domain")): raise ValueError("Client nodes and failure domains must differ")
    access = {n["name"]: n for n in storage_plan["access_nodes"]}
    if any(access.get(n["name"]) != n for n in nodes): raise ValueError("Client nodes must retain their reviewed CSI access identities")
    bootstrap.budget(plan["resources"]); program.validate(scope(plan))


def scope(plan):
    return {"schema_version": 1, "operation_id": plan["operation_id"], "payload_bytes": plan["payload_bytes"], "timeout_seconds": plan["timeout_seconds"],
        "nodes": {role: plan["nodes"][0 if role in {"rbd-write", "cephfs-left"} else 1]["name"] for role in program.ROLES}}


def prefix(plan): return "ceph-io-" + uuid.UUID(plan["operation_id"]).hex


def objects(plan, storage_plan, runtime):
    validate(plan, storage_plan, runtime)
    base = prefix(plan); namespace = plan["namespace"]
    def meta(name): return {"name": name, "namespace": namespace, "labels": {"platform.gitops/ceph-client-check": base}}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(base + "-program"), "immutable": True,
        "data": {"client.py": standalone_script(Path(program.__file__), "strict_json"),
                 "scope.json": storage.canonical(scope(plan))}}
    claims = [{"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta(base + "-" + kind), "spec": {
        "storageClassName": storage_plan["names"][kind + "_class"], "accessModes": ["ReadWriteOnce" if kind == "rbd" else "ReadWriteMany"],
        "volumeMode": "Filesystem", "resources": {"requests": {"storage": "1Gi"}}}} for kind in ("rbd", "cephfs")]
    jobs = []
    for role in program.ROLES:
        kind = "rbd" if role.startswith("rbd-") else "cephfs"; readonly = role == "rbd-read"; label = base + "-" + role
        pod = {"automountServiceAccountToken": False, "enableServiceLinks": False, "restartPolicy": "Never", "terminationGracePeriodSeconds": 15,
            "nodeSelector": {"kubernetes.io/os": "linux"}, "affinity": bootstrap.affinity({"domain_label": storage_plan["provider"]["domain_label"]}, [scope(plan)["nodes"][role]]),
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 2000, "fsGroup": 2000, "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "canary", "image": plan["image"], "command": ["python3", "-I", "-B", "/program/client.py"],
                "args": ["--scope", "/program/scope.json", "--role", role, "--execute"],
                "env": [{"name": "NODE_NAME", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "spec.nodeName"}}}],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                "resources": bootstrap.canonical_budget(plan["resources"]), "volumeMounts": [{"name": "program", "mountPath": "/program", "readOnly": True}, {"name": "volume", "mountPath": "/volume", **({"readOnly": True} if readonly else {})}]}],
            "volumes": [{"name": "program", "configMap": {"name": config["metadata"]["name"], "defaultMode": 0o444}},
                {"name": "volume", "persistentVolumeClaim": {"claimName": base + "-" + kind, **({"readOnly": True} if readonly else {})}}]}
        bootstrap.explicit_pod_defaults(pod)
        jobs.append({"apiVersion": "batch/v1", "kind": "Job", "metadata": meta(label), "spec": {"parallelism": 1, "completions": 1, "completionMode": "NonIndexed", "backoffLimit": 0,
            "activeDeadlineSeconds": plan["timeout_seconds"] + 300, "podReplacementPolicy": "Failed", "manualSelector": True,
            "selector": {"matchLabels": {"platform.gitops/client-role": label}}, "template": {"metadata": {"labels": {"platform.gitops/client-role": label}}, "spec": pod}}})
    return [config, *claims, *jobs]


def render(plan, storage_plan, runtime):
    if yaml.__version__ != "6.0.3": raise ValueError("Use reviewed PyYAML 6.0.3")
    payload = yaml.safe_dump_all(objects(plan, storage_plan, runtime), sort_keys=False)
    return {"ceph-client-check.yaml": payload, "client-check-contract.json": storage.canonical({"schema_version": 1, "runtime": runtime, "plan": plan,
        "resources_sha256": hashlib.sha256(payload.encode()).hexdigest()}) + "\n", "render-status.json": storage.canonical({"rendered": True, **UNVERIFIED}) + "\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--storage-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=storage.RUNTIMES, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan, storage_plan = [loads_strict_json(read_bounded_bytes(p, max_bytes=256 * 1024)) for p in (args.plan, args.storage_plan)]
        write_bundle(render(plan, storage_plan, args.runtime), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"rendered": True, **UNVERIFIED})); return 0
    except Exception:
        print("Ceph client-check rendering refused; private values were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
