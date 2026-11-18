#!/usr/bin/env python3
"""Package native CephFS peers and verify their receipts against the original provider."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

from bounded_file import read_bounded_bytes, read_bounded_text
import ceph_client_io as exchange
import ceph_native_io as agent
from kubernetes_management import Client
import render_ceph_storage as storage
from render_gerrit_router import write_bundle
from standalone_python import standalone_script
from strict_json import loads_strict_json
import verify_ceph_clients as csi


ROOT = Path(__file__).resolve().parents[1]
UNVERIFIED = {**agent.UNVERIFIED, "native_cephfs_mounts_verified": False, "native_cephfs_data_roundtrip_verified": False}


def programs():
    return {
        "ceph_native_io.py": standalone_script(Path(agent.__file__), "strict_json"),
        "ceph_client_io.py": read_bounded_text(Path(exchange.__file__), encoding="utf-8", max_bytes=128 * 1024),
    }


def scope(plan, storage_plan, runtime, packaged=None):
    packaged = programs() if packaged is None else packaged
    return {**{k: plan[k] for k in ("schema_version", "operation_id", "storage_runtime", "storage_plan_sha256", "storage_proof_sha256", "hosts", "uid", "gid", "payload_bytes", "timeout_seconds", "cephfs_path")},
        "runtime": runtime, "fsid": storage_plan["provider"]["fsid"], "filesystem": storage_plan["names"]["filesystem"],
        "agent_sha256": hashlib.sha256(packaged["ceph_native_io.py"].encode()).hexdigest(),
        "engine_sha256": hashlib.sha256(packaged["ceph_client_io.py"].encode()).hexdigest()}


def validate(plan, storage_plan, runtime):
    if (runtime not in {"raw", "docker"} or not isinstance(plan, dict) or set(plan) - {"docker_hosts"} != {"schema_version", "operation_id", "storage_runtime", "storage_plan_sha256", "storage_proof_sha256", "hosts",
            "uid", "gid", "payload_bytes", "timeout_seconds", "cephfs_path", "image"} or not storage.image(plan["image"])
            or plan["storage_plan_sha256"] != storage.fingerprint(storage_plan)): raise ValueError("Use a native CephFS plan bound to the original storage scope")
    storage.validate(storage_plan, plan["storage_runtime"])
    agent.validate(scope(plan, storage_plan, runtime))
    if "docker_hosts" in plan:
        hosts = plan["docker_hosts"]
        if not isinstance(hosts, dict) or set(hosts) != {h["name"] for h in plan["hosts"]}: raise ValueError("Pin both reviewed Docker engines")
        for host in hosts.values():
            if (not isinstance(host, dict) or set(host) != {"binary_sha256", "daemon_id", "server_version", "image_id"} or not storage.sha(host["binary_sha256"])
                    or not isinstance(host["daemon_id"], str) or not re.fullmatch(r"[a-zA-Z0-9:-]{16,80}", host["daemon_id"])
                    or not isinstance(host["server_version"], str) or not re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", host["server_version"])
                    or not isinstance(host["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", host["image_id"])): raise ValueError("Pin the Docker executable, daemon, release and image ID")


def command(plan, runtime, index):
    host = plan["hosts"][index]; role = agent.ROLES[index]; package = host["package_path"]
    if runtime == "raw": return ["python3", "-I", "-B", package + "/ceph_native_io.py", "--scope", package + "/native-scope.json", "--role", role]
    return ["docker", "--host=unix:///var/run/docker.sock", "run", "--rm", "--pull=never", "--name", "cephfs-" + plan["operation_id"].replace("-", "") + "-" + str(index),
        "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=64", "--memory=512m", "--cpus=1",
        "--user", str(plan["uid"]) + ":" + str(plan["gid"]), "--log-driver=json-file", "--log-opt=max-size=1m", "--log-opt=max-file=1",
        "--mount", "type=bind,src=" + package + ",dst=/program,readonly",
        "--mount", "type=bind,src=" + host["mount_path"] + ",dst=/volume,bind-propagation=rprivate",
        "--mount", "type=bind,src=/etc/machine-id,dst=/identity/machine-id,readonly",
        "--mount", "type=bind,src=/proc/sys/kernel/random/boot_id,dst=/identity/boot-id,readonly",
        "--entrypoint=python3", plan["image"], "-I", "-B", "/program/ceph_native_io.py", "--scope", "/program/native-scope.json", "--role", role, "--container"]


def render(plan, storage_plan, runtime):
    validate(plan, storage_plan, runtime)
    files = programs()
    selected = scope(plan, storage_plan, runtime, files)
    files["native-scope.json"] = storage.canonical(selected) + "\n"
    files["render-status.json"] = storage.canonical({"rendered": True, **UNVERIFIED}) + "\n"
    for index, role in enumerate(agent.ROLES):
        files["run-" + role + ".sh"] = '#!/bin/sh\nset -eu\nexec ' + shlex.join(command(plan, runtime, index)) + ' "$@"\n'
    return files


def private_receipt(path):
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Use a private original peer receipt")
    return loads_strict_json(read_bounded_bytes(path, max_bytes=32768))


def receipts(plan, storage_plan, runtime, left, right, *, single_role=None):
    validate(plan, storage_plan, runtime); selected = scope(plan, storage_plan, runtime); expected_scope = storage.fingerprint(selected)
    times = []; evidence = {}
    expected_keys = {"schema_version", "operation_id", "scope_sha256", "role", "host", "runtime", "machine_id_sha256", "boot_id", "payload_bytes", "agent_sha256", "engine_sha256",
        "mount", "started_at", "completed_at", "cephfs_mount_observed", "payload_sha256", "peer_sha256", "peer_barrier_verified", *agent.UNVERIFIED}
    for index, (role, value) in enumerate(zip(agent.ROLES, (left, right))):
        if single_role is not None and role != single_role: continue
        host = plan["hosts"][index]; peer = agent.ROLES[1 - index]
        if (not isinstance(value, dict) or set(value) != expected_keys or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or any(value[k] != expected for k, expected in {"operation_id": plan["operation_id"], "scope_sha256": expected_scope, "role": role, "host": host["name"], "runtime": runtime,
                    "machine_id_sha256": host["machine_id_sha256"], "boot_id": host["boot_id"], "payload_bytes": plan["payload_bytes"], "agent_sha256": selected["agent_sha256"], "engine_sha256": selected["engine_sha256"],
                    "payload_sha256": exchange.expected(agent.engine_scope(selected), role), "peer_sha256": exchange.expected(agent.engine_scope(selected), peer)}.items())
                or value["cephfs_mount_observed"] is not True or value["peer_barrier_verified"] is not True or any(value[k] is not False for k in agent.UNVERIFIED)):
            raise ValueError("A native peer receipt does not match the reviewed exchange")
        mounted = value["mount"]
        if (not isinstance(mounted, dict) or set(mounted) != {"id", "device", "mount_sha256"} or not storage.integer(mounted["id"], 1, 2 ** 63 - 1)
                or not isinstance(mounted["device"], str) or not re.fullmatch(r"[0-9]+:[0-9]+", mounted["device"]) or not storage.sha(mounted["mount_sha256"])): raise ValueError("The original mounted handle is missing")
        if any(not isinstance(value[k], str) or len(value[k]) > 40 for k in ("started_at", "completed_at")): raise ValueError("Invalid native peer timing")
        start, end = (datetime.fromisoformat(value[k]) for k in ("started_at", "completed_at"))
        now = datetime.now(timezone.utc)
        if (start.tzinfo is None or end.tzinfo is None or start > end or (end - start).total_seconds() > plan["timeout_seconds"] + 30
                or (now - start).total_seconds() > 3600 or (now - end).total_seconds() < -30): raise ValueError("Native peer evidence is expired or has invalid timing")
        times.append((start, end)); evidence[role] = storage.fingerprint(value)
    if single_role is not None and single_role not in agent.ROLES: raise ValueError("Unknown native peer role")
    if max(t[0] for t in times) > min(t[1] for t in times): raise ValueError("Both independent client intervals must overlap")
    return {"scope_sha256": expected_scope, "receipts_sha256": evidence}


def verify(plan, storage_plan, runtime, proof_path, left, right, *, client=None, docker_proofs=None):
    validate(plan, storage_plan, runtime)
    receipt, objects = csi.storage_receipt(plan, storage_plan, plan["storage_runtime"], proof_path)
    checked = receipts(plan, storage_plan, runtime, left, right)
    docker_evidence = {}
    if runtime == "docker":
        from manage_ceph_native_docker import verify_proof
        if not isinstance(docker_proofs, (list, tuple)) or len(docker_proofs) != 2: raise ValueError("Require both original Docker execution proofs")
        for role, value, proof in zip(agent.ROLES, (left, right), docker_proofs):
            verify_proof(plan, storage_plan, role, proof, value)
            docker_evidence[role] = storage.fingerprint(proof)
    elif docker_proofs is not None: raise ValueError("Docker evidence is not a native Linux execution proof")
    network = client or Client(storage_plan["target"])
    with network.frozen():
        csi.shared.prerequisites(network, storage_plan)
        if not csi.shared.ready(network, storage_plan, objects, receipt): raise ValueError("The original Ceph storage resources are no longer ready")
        # Re-read private storage evidence and local sources around the live observation.
        csi.storage_receipt(plan, storage_plan, plan["storage_runtime"], proof_path)
        if checked != receipts(plan, storage_plan, runtime, left, right): raise ValueError("Native client evidence changed")
        if runtime == "docker":
            for role, value, proof in zip(agent.ROLES, (left, right), docker_proofs): verify_proof(plan, storage_plan, role, proof, value)
    return {"schema_version": 1, "runtime": runtime, "storage_runtime": plan["storage_runtime"], "operation_id": plan["operation_id"], "storage_plan_sha256": plan["storage_plan_sha256"],
        "storage_proof_sha256": plan["storage_proof_sha256"], "verified_at": datetime.now(timezone.utc).isoformat(), **checked, **UNVERIFIED,
        "native_cephfs_mounts_verified": True, "native_cephfs_data_roundtrip_verified": True,
        "docker_runtime_verified": runtime == "docker", "docker_proofs_sha256": docker_evidence}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--storage-plan", type=Path, required=True)
    parser.add_argument("--runtime", choices=("raw", "docker"), required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify", action="store_true"); parser.add_argument("--storage-proof", type=Path)
    parser.add_argument("--left-receipt", type=Path); parser.add_argument("--right-receipt", type=Path)
    parser.add_argument("--left-docker-proof", type=Path); parser.add_argument("--right-docker-proof", type=Path)
    args = parser.parse_args(argv)
    try:
        plan, storage_plan = (loads_strict_json(read_bounded_bytes(p, max_bytes=256 * 1024)) for p in (args.plan, args.storage_plan))
        if args.verify:
            if any(p is None for p in (args.storage_proof, args.left_receipt, args.right_receipt)): raise ValueError("Supply original storage and both peer receipts")
            docker_proofs = None
            if args.runtime == "docker":
                if args.left_docker_proof is None or args.right_docker_proof is None: raise ValueError("Supply both original Docker execution proofs")
                docker_proofs = [private_receipt(p) for p in (args.left_docker_proof, args.right_docker_proof)]
            elif args.left_docker_proof is not None or args.right_docker_proof is not None: raise ValueError("Docker proofs require Docker runtime")
            result = verify(plan, storage_plan, args.runtime, args.storage_proof, private_receipt(args.left_receipt), private_receipt(args.right_receipt), docker_proofs=docker_proofs)
            files = {"ceph-native-proof.json": storage.canonical(result) + "\n"}
        else:
            if any(p is not None for p in (args.storage_proof, args.left_receipt, args.right_receipt, args.left_docker_proof, args.right_docker_proof)): raise ValueError("Receipt inputs require explicit verification")
            files = render(plan, storage_plan, args.runtime)
            result = {"rendered": True, **UNVERIFIED}
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(storage.canonical(result)); return 0
    except Exception:
        print("Native CephFS package or verification refused; private values were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
