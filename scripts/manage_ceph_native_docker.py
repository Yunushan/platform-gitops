#!/usr/bin/env python3
"""Create one scoped Docker CephFS peer, or observe that original attempt on resume."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import ceph_native_io as agent
import render_ceph_native_check as renderer
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
LABEL = "platform.gitops/ceph-docker-"
CLEAN = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent", "LC_ALL": "C"}
FALSE_FLAGS = {**agent.UNVERIFIED, "native_cephfs_mounts_verified": False, "native_cephfs_data_roundtrip_verified": False}


def toolset():
    return renderer.storage.fingerprint({"provider_tools": renderer.csi.toolset(), **{name: hashlib.sha256(read_bounded_bytes(Path(__file__).parent / name, max_bytes=256 * 1024)).hexdigest()
        for name in ("manage_ceph_native_docker.py", "render_ceph_native_check.py", "ceph_native_io.py", "start_gateway_edge.py")}})


def name(plan, role): return "cephfs-" + plan["operation_id"].replace("-", "") + "-" + str(agent.ROLES.index(role))
def cid(value): return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
def policy(row): return {k: row.get(k) for k in ("Config", "HostConfig", "Mounts", "Image", "AppArmorProfile")}


def labels(state):
    return {LABEL + k: state[v] for k, v in {"attempt": "attempt_id", "scope": "scope_sha256", "plan": "native_plan_sha256", "role": "role", "tools": "toolset_sha256"}.items()}


def create_arguments(plan, role, state):
    body = renderer.command(plan, "docker", agent.ROLES.index(role))[3:]
    body.remove("--rm")
    offset = body.index("--entrypoint=python3")
    extra = ["--memory-swap=512m", "--ipc=none", "--cgroupns=private", "--userns=host", "--runtime=runc", "--restart=no", "--stop-timeout=15", "--workdir=/", "--hostname=ceph-canary", "--no-healthcheck"]
    for key, value in sorted(labels(state).items()): extra += ["--label", key + "=" + value]
    body[offset:offset] = extra
    return ["create", *body, "--execute"]


def check_container(plan, role, state, row, image):
    config, host = row.get("Config", {}), row.get("HostConfig", {})
    expected_labels = {**(image.get("Config", {}).get("Labels") or {}), **labels(state)}
    command = ["-I", "-B", "/program/ceph_native_io.py", "--scope", "/program/native-scope.json", "--role", role, "--container", "--execute"]
    if (not cid(row.get("Id")) or state["container_id"] is not None and row["Id"] != state["container_id"] or row.get("Name") != "/" + name(plan, role)
            or config.get("Image") != plan["image"] or row.get("Image") != image["Id"] or config.get("User") != f'{plan["uid"]}:{plan["gid"]}'
            or config.get("Entrypoint") != ["python3"] or config.get("Cmd") != command or config.get("Labels") != expected_labels
            or config.get("Env", []) != (image.get("Config", {}).get("Env") or []) or config.get("WorkingDir") != "/" or config.get("Hostname") != "ceph-canary"
            or config.get("Tty") is not False or config.get("OpenStdin") is not False or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            or row.get("RestartCount") != 0 or row.get("AppArmorProfile") not in {"", "docker-default"}): raise ValueError("The original Docker peer identity or command changed")
    required = {"ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "none", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc",
        "PidsLimit": 64, "Memory": 512 * 1024 ** 2, "MemorySwap": 512 * 1024 ** 2, "NanoCpus": 1000000000, "AutoRemove": False,
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}}}
    if (any(host.get(k) != v for k, v in required.items()) or host.get("CapDrop") != ["ALL"] or host.get("SecurityOpt") not in (["no-new-privileges:true"], ["no-new-privileges"])
            or any(host.get(k) for k in ("CapAdd", "Binds", "VolumesFrom", "Devices", "DeviceRequests", "DeviceCgroupRules", "GroupAdd", "Tmpfs", "Links", "PortBindings", "PublishAllPorts", "PidMode", "UTSMode", "ExtraHosts", "Sysctls", "Init"))
            or any(k != "none" for k in row.get("NetworkSettings", {}).get("Networks", {}))): raise ValueError("Docker injected unreviewed privileges, networking or storage")
    selected = plan["hosts"][agent.ROLES.index(role)]
    expected = {"/program": (selected["package_path"], False), "/volume": (selected["mount_path"], True),
        "/identity/machine-id": ("/etc/machine-id", False), "/identity/boot-id": ("/proc/sys/kernel/random/boot_id", False)}
    mounts = row.get("Mounts", [])
    if (not isinstance(mounts, list) or len(mounts) != 4 or {m.get("Destination") for m in mounts} != set(expected)
            or any(m.get("Type") != "bind" or (m.get("Source"), m.get("RW")) != expected[m["Destination"]] or m.get("Propagation") != "rprivate" for m in mounts)
            or len(host.get("Mounts", [])) != 4): raise ValueError("The original four bind mounts changed")
    fingerprint = renderer.storage.fingerprint(policy(row))
    if state["container_sha256"] is not None and state["container_sha256"] != fingerprint: raise ValueError("The original inspected container policy changed")
    return fingerprint


class Host:
    def __init__(self, *, binary=Path("/usr/bin/docker"), socket=Path("/var/run/docker.sock")):
        self.binary, self.socket = binary, socket

    def run(self, arguments):
        result = run_bounded([str(self.binary), "--host=unix://" + str(self.socket), *arguments], input="", text=True, env=CLEAN, timeout=30, output_max_bytes=1024 ** 2, check=False)
        if result.returncode: raise ValueError("Docker command failed; retain local state for inspection")
        return result.stdout

    def prerequisites(self, plan, storage_plan, role):
        if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Docker peer management requires an explicit root Linux host session")
        selected = plan["hosts"][agent.ROLES.index(role)]; pins = plan["docker_hosts"][selected["name"]]
        machine = agent.read(Path("/etc/machine-id"), 128).decode().strip(); boot = agent.read(Path("/proc/sys/kernel/random/boot_id"), 128).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{32}", machine) or hashlib.sha256(machine.encode()).hexdigest() != selected["machine_id_sha256"] or boot != selected["boot_id"]: raise ValueError("The Docker client host identity changed")
        from start_gateway_edge import trusted_paths
        socket = self.socket.resolve(strict=True); binary = self.binary
        trusted_paths([binary, socket.parent])
        info = socket.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o007: raise ValueError("Use the private local root-owned Docker socket")
        if hashlib.sha256(read_bounded_bytes(binary, max_bytes=128 * 1024 ** 2)).hexdigest() != pins["binary_sha256"]: raise ValueError("The Docker executable changed")
        facts = loads_strict_json(self.run(["info", "--format", '{"id":{{json .ID}},"os":{{json .OSType}},"security":{{json .SecurityOptions}},"runtime":{{json .DefaultRuntime}}}']))
        version = loads_strict_json(self.run(["version", "--format", '{{json .Server.Version}}']))
        if (facts.get("id") != pins["daemon_id"] or version != pins["server_version"] or facts.get("os") != "linux" or facts.get("runtime") != "runc"
                or not isinstance(facts.get("security"), list) or any("rootless" in s or "userns" in s for s in facts["security"])
                or not any("name=seccomp" in s and "profile=builtin" in s for s in facts["security"])): raise ValueError("Require the pinned rootful Linux Docker engine and default seccomp")
        rows = loads_strict_json(self.run(["image", "inspect", plan["image"]]))
        if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("Id") != pins["image_id"] or plan["image"] not in rows[0].get("RepoDigests", [])
                or rows[0].get("Os") != "linux" or rows[0].get("Architecture") != {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine)
                or rows[0].get("Config", {}).get("Volumes")): raise ValueError("Preload the exact reviewed image without declared anonymous volumes")
        for filename, expected in renderer.render(plan, storage_plan, "docker").items():
            path = Path(selected["package_path"]) / filename; fileinfo = path.lstat()
            if (path.is_symlink() or not stat.S_ISREG(fileinfo.st_mode) or fileinfo.st_nlink != 1 or fileinfo.st_uid not in {0, plan["uid"]} or fileinfo.st_mode & 0o022
                    or read_bounded_bytes(path, max_bytes=256 * 1024) != expected.encode()): raise ValueError("The reviewed private client package changed")
        mounted = agent.mount_identity(renderer.scope(plan, storage_plan, "docker"), selected, selected["mount_path"])
        return {"image": rows[0], "mount": mounted, "socket": {"device": info.st_dev, "inode": info.st_ino}}

    def find(self, container_name):
        values = self.run(["container", "ls", "--all", "--no-trunc", "--filter", "name=^/" + container_name + "$", "--format", '{{.ID}}']).split()
        if len(values) > 1 or any(not cid(v) for v in values): raise ValueError("Ambiguous original Docker peer")
        return values[0] if values else None

    def inspect(self, identity):
        rows = loads_strict_json(self.run(["container", "inspect", identity]))
        if not isinstance(rows, list) or len(rows) != 1: raise ValueError("The original Docker peer disappeared")
        return rows[0]

    def create(self, arguments): return self.run(arguments).strip()
    def start(self, identity): self.run(["container", "start", identity])
    def logs(self, identity): return self.run(["container", "logs", "--tail=4", identity])


def verify_proof(plan, storage_plan, role, proof, receipt):
    renderer.validate(plan, storage_plan, "docker")
    pins = plan.get("docker_hosts", {}).get(plan["hosts"][agent.ROLES.index(role)]["name"])
    if (pins is None or not isinstance(proof, dict) or set(proof) != {"schema_version", "native_plan_sha256", "scope_sha256", "role", "attempt_id", "toolset_sha256", "docker", "container", "agent_receipt", "docker_runtime_verified"}
            or type(proof["schema_version"]) is not int or proof["schema_version"] != 1 or proof["native_plan_sha256"] != renderer.storage.fingerprint(plan)
            or proof["scope_sha256"] != renderer.storage.fingerprint(renderer.scope(plan, storage_plan, "docker")) or proof["role"] != role
            or not isinstance(proof["attempt_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", proof["attempt_id"]) or proof["toolset_sha256"] != toolset()
            or proof["docker"] != pins or proof["docker_runtime_verified"] is not True or proof["agent_receipt"] != receipt): raise ValueError("The Docker execution proof does not match the reviewed peer")
    container = proof["container"]
    if (not isinstance(container, dict) or set(container) != {"id", "policy_sha256", "started_at", "finished_at"} or not cid(container["id"]) or not renderer.storage.sha(container["policy_sha256"])): raise ValueError("The original Docker container handle is missing")
    started, finished = (datetime.fromisoformat(container[k]) for k in ("started_at", "finished_at"))
    before, after = (datetime.fromisoformat(receipt[k]) for k in ("started_at", "completed_at"))
    if (any(t.tzinfo is None for t in (started, finished, before, after)) or started > before or finished < after or (finished - started).total_seconds() > plan["timeout_seconds"] + 60): raise ValueError("Container timing does not contain the peer exchange")
    renderer.receipts(plan, storage_plan, "docker", receipt if role == agent.ROLES[0] else None, receipt if role == agent.ROLES[1] else None, single_role=role)


def operate(plan, storage_plan, role, *, mode="local", output=None, host=None, wait_seconds=300):
    renderer.validate(plan, storage_plan, "docker")
    if role not in agent.ROLES or "docker_hosts" not in plan or not renderer.storage.integer(wait_seconds, 5, 600): raise ValueError("Review both Docker host pins and a bounded wait")
    if mode not in {"local", "check-host", "create", "resume"}: raise ValueError("Select an explicit Docker peer operation")
    result = {"local_inputs_verified": True, **FALSE_FLAGS}
    if mode == "local": return result
    host = host or Host(); current = host.prerequisites(plan, storage_plan, role)
    if mode == "check-host": return {**result, "host_checked": True}
    if output is None: raise ValueError("Use an original private attempt directory")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private original attempt directory")
    source = toolset(); scope_hash = renderer.storage.fingerprint(renderer.scope(plan, storage_plan, "docker")); plan_hash = renderer.storage.fingerprint(plan)
    exists = resolved.exists()
    if (mode == "create" and exists) or (mode == "resume" and not exists): raise ValueError("Create needs a new attempt; resume needs the original journal")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=mode == "resume")
    info = resolved.stat()
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError("Untrusted Docker attempt directory")
    path = resolved / "docker-peer-journal.json"
    with renderer.csi.shared.lock(resolved):
        if mode == "resume":
            state = renderer.private_receipt(path)
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state.get("schema_version") != 1 or state.get("native_plan_sha256") != plan_hash or state.get("scope_sha256") != scope_hash
                    or state.get("role") != role or state.get("toolset_sha256") != source or state.get("mount") != current["mount"] or state.get("socket") != current["socket"]
                    or type(state.get("create_intent")) is not bool or type(state.get("start_intent")) is not bool
                    or state.get("start_intent") and not state.get("create_intent")
                    or not isinstance(state.get("attempt_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["attempt_id"])
                    or state.get("container_id") is not None and not cid(state["container_id"])
                    or state.get("container_sha256") is not None and not renderer.storage.sha(state["container_sha256"])): raise ValueError("The original Docker attempt changed")
        else:
            if host.find(name(plan, role)) is not None: raise ValueError("An existing Docker peer cannot be adopted")
            state = {"schema_version": 1, "native_plan_sha256": plan_hash, "scope_sha256": scope_hash, "role": role, "toolset_sha256": source, "attempt_id": uuid4().hex,
                "mount": current["mount"], "socket": current["socket"], "create_intent": False, "start_intent": False, "container_id": None, "container_sha256": None}
        def record(phase):
            state["phase"] = phase; atomic_write_text(path, renderer.storage.canonical(state) + "\n")
        def inspect():
            row = host.inspect(state["container_id"])
            state["container_sha256"] = check_container(plan, role, state, row, current["image"])
            return row
        def unchanged():
            if toolset() != source or host.prerequisites(plan, storage_plan, role) != current: raise ValueError("Docker peer prerequisites changed before a request")
        try:
            record("prepared")
            found = host.find(name(plan, role))
            if state["create_intent"]:
                if found is None or state["container_id"] is not None and found != state["container_id"]: raise ValueError("An uncertain or deleted Docker peer is never recreated")
                state["container_id"] = found; inspect(); record("create-observed")
            else:
                if found is not None: raise ValueError("An unrelated Docker peer appeared")
                unchanged()
                state["create_intent"] = True; record("create-intent")
                created = host.create(create_arguments(plan, role, state))
                if not cid(created): raise ValueError("Docker did not return an original container ID")
                state["container_id"] = created; inspect(); record("created")
            row = inspect()
            if not state["start_intent"]:
                if row.get("State", {}).get("Status") != "created" or row["State"].get("Running") is not False or row["State"].get("Error"): raise ValueError("An unstarted peer changed state")
                unchanged()
                state["start_intent"] = True; record("start-intent")
                host.start(state["container_id"])
            elif row.get("State", {}).get("Status") == "created": raise ValueError("An uncertain start request is never repeated")
            deadline = time.monotonic() + wait_seconds
            while True:
                row = inspect(); runtime = row.get("State", {})
                if runtime.get("Status") == "exited": break
                if runtime.get("Status") != "running" or runtime.get("Restarting") or runtime.get("Dead") or runtime.get("OOMKilled") or runtime.get("Error"): raise ValueError("The original Docker peer failed")
                record("observing-original-peer")
                if time.monotonic() >= deadline: raise TimeoutError("Observe the same Docker peer again; no restart was requested")
                time.sleep(0.2)
            if (runtime.get("ExitCode") != 0 or runtime.get("Running") is not False or runtime.get("Restarting") or runtime.get("Dead") or runtime.get("OOMKilled") or runtime.get("Error")): raise ValueError("The original Docker peer did not exit cleanly")
            lines = [s for s in host.logs(state["container_id"]).splitlines() if s.strip()]
            if len(lines) != 1: raise ValueError("Require one complete original peer receipt")
            receipt = loads_strict_json(lines[0])
            pins = plan["docker_hosts"][plan["hosts"][agent.ROLES.index(role)]["name"]]
            proof = {"schema_version": 1, "native_plan_sha256": plan_hash, "scope_sha256": scope_hash, "role": role, "attempt_id": state["attempt_id"], "toolset_sha256": source,
                "docker": pins, "container": {"id": state["container_id"], "policy_sha256": state["container_sha256"], "started_at": runtime.get("StartedAt"), "finished_at": runtime.get("FinishedAt")},
                "agent_receipt": receipt, "docker_runtime_verified": True}
            verify_proof(plan, storage_plan, role, proof, receipt)
            final = host.prerequisites(plan, storage_plan, role)
            if final != current or inspect() != row or toolset() != source: raise ValueError("Docker peer evidence changed before completion")
            atomic_write_text(resolved / "docker-peer-proof.json", renderer.storage.canonical(proof) + "\n")
            atomic_write_text(resolved / "agent-receipt.json", renderer.storage.canonical(receipt) + "\n")
            record("completed")
            return {**result, "docker_runtime_verified": True}
        except BaseException:
            record("failed-observe-original"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--storage-plan", type=Path, required=True); parser.add_argument("--role", choices=agent.ROLES, required=True)
    modes = parser.add_mutually_exclusive_group(); modes.add_argument("--check-host", action="store_true"); modes.add_argument("--create", action="store_true"); modes.add_argument("--resume", action="store_true")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        plan, storage_plan = (loads_strict_json(read_bounded_bytes(p, max_bytes=256 * 1024)) for p in (args.plan, args.storage_plan))
        mode = "create" if args.create else "resume" if args.resume else "check-host" if args.check_host else "local"
        print(renderer.storage.canonical(operate(plan, storage_plan, args.role, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Docker CephFS peer stopped; retain its private journal and original container for inspection or resume.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
