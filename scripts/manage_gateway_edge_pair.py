#!/usr/bin/env python3
"""Coordinate explicitly selected, pinned entry hosts B then A with durable job handles."""

from __future__ import annotations

import argparse
import ast
import base64
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import socket
import ssl
import struct
import sys
import tempfile
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
import gateway_edge_pair_agent as agent
import gateway_edge_pair_staging as staging
import manage_gateway_edge_host as host_manager
import render_gateway_edge as renderer
from render_gerrit_router import write_bundle
from standalone_python import with_modules
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]

# This bootstrap reads only a bounded request, verifies every isolated Python
# source and root-owned path, then imports the exact reviewed agent. No caller
# field is interpolated into executable Python or a shell command.
BOOTSTRAP = '''import hashlib,json,os,pathlib,re,runpy,sys
def pairs(items):
 d={}
 for k,v in items:
  if k in d: raise ValueError("duplicate field")
  d[k]=v
 return d
assert sys.platform=="linux" and os.geteuid()==0
raw=sys.stdin.buffer.read(131073)
assert len(raw)<=131072
request=json.loads(raw,object_pairs_hook=pairs)
files=request["tools"]
assert isinstance(files,dict) and files and all(re.fullmatch(r"[a-z0-9_]+[.]py",k) and isinstance(v,str) and re.fullmatch(r"[0-9a-f]{64}",v) for k,v in files.items())
assert hashlib.sha256(json.dumps(files,sort_keys=True,separators=(",",":")).encode()).hexdigest()==sys.argv[2]
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
directory=root/"scripts"
assert directory.is_dir() and not directory.is_symlink()
assert {p.name for p in directory.iterdir()}==set(files)
for name,digest in files.items():
 p=directory/name
 assert p.is_file() and not p.is_symlink()
 for part in (p,*p.parents):
  s=part.stat(); assert s.st_uid==0 and not s.st_mode&18
 with p.open("rb") as f: data=f.read(2097153)
 assert len(data)<=2097152 and hashlib.sha256(data).hexdigest()==digest
sys.path.insert(0,str(directory))
module=runpy.run_path(str(directory/"gateway_edge_pair_agent.py"),run_name="pinned_pair_rpc")
print(json.dumps(module["Agent"]().rpc(request),sort_keys=True,separators=(",",":")))
'''


def tool_files():
    if agent.yaml.__version__ != agent.YAML_VERSION: raise ValueError("Use the project's pinned YAML renderer version locally")
    # Rendered runtime helpers are package assets even when no module imports
    # them. Remote contract regeneration needs those exact reviewed files too.
    found, pending = {}, ["gateway_edge_pair_agent", "gateway_edge_pair_staging",
        *sorted(name.removesuffix(".py") for name in host_manager.starter.PROGRAMS)]
    while pending:
        name = pending.pop()
        if name in found: continue
        path = ROOT / "scripts" / (name + ".py")
        source = read_bounded_text(path, encoding="utf-8", max_bytes=2 * 1024 ** 2)
        found[name] = source
        for node in ast.walk(ast.parse(source)):
            modules = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for module in modules:
                candidate = module.split(".")[0]
                if (ROOT / "scripts" / (candidate + ".py")).is_file(): pending.append(candidate)
    return {name + ".py": value for name, value in sorted(found.items())}


def manifest(files):
    return {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}


def staging_inputs(contract, files, gateway, sources, ca_file, artifacts):
    """Capture only regenerated packages, reviewed trust and pinned executables."""
    from deploy_cilium_gateway import load_package
    import render_cilium_gateway
    reviewed, _ = load_package(gateway, contract["plan"]["gateway_contract_sha256"])
    gateway_files = {name: value.encode() for name, value in render_cilium_gateway.render(reviewed["plan"], reviewed["runtime"]).items()}
    payloads = host_manager.payloads(contract, files, ca_file, artifacts)
    return {"tools": {"scripts/" + name: value.encode() for name, value in sources.items()}, "bundle": files,
            "gateway": gateway_files, "ca": {"payload": payloads["ca.crt"]},
            **({"artifacts": {name.removeprefix("bin/"): value for name, value in payloads.items() if name.startswith("bin/")}} if contract["runtime"] == "raw" else {})}


def staging_request(peer, inputs):
    roots = {}
    for label, files in inputs.items():
        roots[label] = {"path": peer["remote_root"] if label == "tools" else peer["paths"][label], "kind": "file" if label == "ca" else "directory",
            "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest(),
                            "mode": 0o555 if label == "artifacts" and name in host_manager.ROLES else 0o644} for name, value in files.items()}}
    header = {"schema_version": 1, "machine_sha256": peer["machine_sha256"], "roots": roots}
    staging.validate(header)
    return header


def staging_wire(header, inputs):
    encoded = staging.canonical(header)
    return struct.pack(">I", len(encoded)) + encoded + b"".join(value for label in sorted(inputs) for _, value in sorted(inputs[label].items()))


def remote_path(value):
    return isinstance(value, str) and bool(re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value)) and not any(part in {".", ".."} for part in value.split("/"))


def validate(plan, contract, tools):
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "name", "edge_contract_sha256", "toolset_sha256", "ssh_binary_sha256", "peers"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or plan["name"] != contract["plan"]["name"]
            or not agent.SHA.fullmatch(str(plan["ssh_binary_sha256"])) or plan["toolset_sha256"] != agent.fingerprint(tools)
            or not isinstance(plan["peers"], list) or len(plan["peers"]) != 2): raise ValueError("Invalid reviewed pair plan/toolset")
    hosts = {item["name"]: item for item in contract["plan"]["entry_hosts"]}
    if {peer.get("host") for peer in plan["peers"] if isinstance(peer, dict)} != set(hosts): raise ValueError("Declare both exact entry hosts")
    for peer in plan["peers"]:
        if (set(peer) != {"host", "address", "port", "machine_sha256", "identity_env", "known_hosts_env", "remote_root", "paths", "expected_current"}
                or peer["address"] != hosts[peer["host"]]["address"] or type(peer["port"]) is not int or not 1 <= peer["port"] <= 65535
                or not isinstance(peer["machine_sha256"], str) or not agent.SHA.fullmatch(peer["machine_sha256"])
                or any(not isinstance(peer[key], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", peer[key]) for key in ("identity_env", "known_hosts_env"))
                or not remote_path(peer["remote_root"]) or not isinstance(peer["paths"], dict) or set(peer["paths"]) != {"bundle", "gateway", "ca", "artifacts"}
                or any(not remote_path(value) for key, value in peer["paths"].items() if not (key == "artifacts" and value is None))
                or contract["runtime"] == "raw" and peer["paths"]["artifacts"] is None
                or peer["expected_current"] != "absent" and (not isinstance(peer["expected_current"], str) or not agent.SHA.fullmatch(peer["expected_current"]))):
            raise ValueError("Bind exact SSH, physical-host and staged input identities")
    if len({peer["machine_sha256"] for peer in plan["peers"]}) != 2: raise ValueError("Pair peers cannot share the same machine identity")


def known_host_snapshot(text, peer):
    expected = peer["address"] if peer["port"] == 22 else "[" + peer["address"] + "]:" + str(peer["port"])
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        fields = line.split()
        if len(fields) < 3 or fields[0] != expected or fields[1] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
            raise ValueError("Use only exact approved host-key entries for this peer")
        blob = base64.b64decode(fields[2], validate=True)
        if len(blob) < 8 or len(blob) > 8192: raise ValueError("Invalid SSH host key")
        size = struct.unpack(">I", blob[:4])[0]
        if blob[4:4 + size] != fields[1].encode() or len(blob) <= 4 + size: raise ValueError("SSH key type does not match its encoded content")
        rows.append(" ".join(fields[:3]))
    if not 1 <= len(rows) <= 4: raise ValueError("Review one to four exact peer host keys")
    return "\n".join(rows) + "\n"


class SSH:
    def __init__(self, plan, tools, binary):
        if hashlib.sha256(read_bounded_bytes(binary, max_bytes=64 * 1024 ** 2)).hexdigest() != plan["ssh_binary_sha256"]:
            raise ValueError("Local SSH executable differs from its reviewed fingerprint")
        self.plan, self.tools, self.binary = plan, tools, str(binary.resolve(strict=True))
        self.directory, self.peers = None, {}

    def configure(self):
        self.directory = tempfile.TemporaryDirectory(prefix="platform-entry-ssh-")
        root = Path(self.directory.name)
        empty = root / "empty-config"; atomic_write_text(empty, "")
        for peer in self.plan["peers"]:
            identity = Path(os.environ.get(peer["identity_env"], "")).resolve(strict=True)
            if not identity.is_file() or os.name == "posix" and identity.stat().st_mode & 0o077: raise ValueError("Use a private, explicitly selected SSH identity file")
            known = Path(os.environ.get(peer["known_hosts_env"], ""))
            content = known_host_snapshot(read_bounded_text(known, max_bytes=64 * 1024), peer)
            target = root / (peer["host"] + ".known-hosts"); atomic_write_text(target, content)
            def quoted(path): return '"' + path.as_posix().replace('"', '\\"') + '"'
            prefix = [self.binary, "-F", str(empty), "-T", "-p", str(peer["port"]), "-i", str(identity)]
            options = ["BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none", "StrictHostKeyChecking=yes", "ConnectTimeout=5", "ConnectionAttempts=1",
                "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "ForwardAgent=no", "ClearAllForwardings=yes", "ControlMaster=no", "ControlPath=none",
                "PermitLocalCommand=no", "ProxyCommand=none", "ProxyJump=none", "UserKnownHostsFile=" + quoted(target), "GlobalKnownHostsFile=" + quoted(empty)]
            for option in options: prefix += ["-o", option]
            prefix += ["root@" + peer["address"]]
            self.peers[peer["host"]] = prefix
        return self

    def __enter__(self):
        try: return self.configure()
        except BaseException:
            if self.directory: self.directory.cleanup()
            raise

    def __exit__(self, *args):
        if self.directory: self.directory.cleanup()

    def call(self, peer, request):
        command = [*self.peers[peer["host"]], shlex.join(["/usr/bin/python3", "-I", "-B", "-c", BOOTSTRAP, peer["remote_root"], self.plan["toolset_sha256"]])]
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        result = run_bounded(command, env=environment, input=agent.canonical(request), text=True, timeout=20, output_max_bytes=256 * 1024, check=False)
        if result.returncode: raise ValueError("SSH pair operation failed; private reservations and job handles were retained")
        response = loads_strict_json(result.stdout)
        if not isinstance(response, dict) or response.get("error"): raise ValueError("Unexpected root-agent response")
        return response

    def stage(self, peer, header, inputs, source):
        if hashlib.sha256(source.encode()).hexdigest() != self.tools["gateway_edge_pair_staging.py"]:
            raise ValueError("Staging bootstrap differs from the reviewed toolset")
        decoder = read_bounded_text(ROOT / "scripts/strict_json.py", encoding="utf-8", max_bytes=128 * 1024)
        if hashlib.sha256(decoder.encode()).hexdigest() != self.tools["strict_json.py"]:
            raise ValueError("Staging decoder differs from the reviewed toolset")
        program = with_modules(source, {"strict_json": decoder})
        command = [*self.peers[peer["host"]], shlex.join(["/usr/bin/python3", "-I", "-B", "-c", program, staging.fingerprint(header)])]
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        result = run_bounded(command, env=environment, input=staging_wire(header, inputs), timeout=180, output_max_bytes=256 * 1024, check=False)
        if result.returncode: raise ValueError("SSH input staging incomplete; preserve the private journal and retry the exact inputs")
        response = loads_strict_json(result.stdout)
        if response != {"staged": True, "manifest_sha256": staging.fingerprint(header), "machine_sha256": peer["machine_sha256"]}:
            raise ValueError("Remote input staging was not confirmed")
        return response


def observer_context(contract, ca_file):
    ca = read_bounded_bytes(ca_file, max_bytes=256 * 1024)
    if hashlib.sha256(ca).hexdigest() != contract["plan"]["ca_sha256"]: raise ValueError("Observer CA differs from the reviewed entry trust")
    return ssl.create_default_context(cadata=ca.decode("ascii"))


def probe(contract, ca_file=None, *, context=None, check_ssh=True):
    context = context or observer_context(contract, ca_file)
    plan = contract["plan"]
    for route in plan["routes"]:
        with socket.create_connection((plan["vip"].split("/")[0], plan["https_port"]), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname=route["hostname"]) as tls:
                if route["health_path"] is not None:
                    tls.sendall(("GET " + route["health_path"] + " HTTP/1.1\r\nHost: " + route["hostname"] + "\r\nConnection: close\r\n\r\n").encode("ascii"))
                    with http.client.HTTPResponse(tls) as response:
                        response.begin()
                        if response.status != route["health_status"]: raise ValueError("VIP route readiness did not pass")
    if check_ssh:
        for service in renderer.SSH_SERVICES:
            if service not in plan:
                continue
            with socket.create_connection((plan["vip"].split("/")[0], plan[service]["port"]), timeout=3) as raw:
                with raw.makefile("rb") as stream:
                    if not stream.readline(256).startswith(b"SSH-2.0-"):
                        raise ValueError("A configured SSH banner was not reachable through the VIP")
    return {"vip_reachable": True, "application_health_verified": False}


@contextmanager
def coordinator_lock(directory):
    path = directory / "controller.lock"
    if path.is_symlink(): raise ValueError("Coordinator lock cannot be a symlink")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0: os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def operate(plan, bundle, gateway, *, mode="local", output=None, ssh_binary=None, ca_file=None, artifacts=None, distribute_inputs=False, recover_terminated_jobs=False, wait_seconds=180, transport=None, observer=None):
    contract, generated = host_manager.capture(bundle, plan["edge_contract_sha256"], gateway)
    files = tool_files(); tools = manifest(files)
    validate(plan, contract, tools)
    if type(recover_terminated_jobs) is not bool or recover_terminated_jobs and mode != "resume":
        raise ValueError("Terminated-job recovery requires an explicit resume of the original journal")
    result = {"local_inputs_verified": True, "pair_services_ready": False, "vip_reachable": False, "vip_failover_verified": False,
              "application_health_verified": False, "ha_verified": False, "full_stack_verified": False}
    if mode == "local": return result
    if mode == "prepare-tools":
        if output is None or output.exists() or output.is_symlink(): raise ValueError("Use a new private tool output directory")
        write_bundle(files, output / "scripts", private_root=ROOT / "private")
        atomic_write_text(output / "tool-manifest.json", agent.canonical(tools))
        return {**result, "tools_prepared": True, "toolset_sha256": agent.fingerprint(tools)}
    if mode not in {"apply", "rollback", "resume", "cancel"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 1800:
        raise ValueError("Select an explicit pair operation, bounded wait and private journal directory")
    if type(distribute_inputs) is not bool or mode == "cancel" and distribute_inputs:
        raise ValueError("Reservation cancellation cannot distribute inputs")
    inputs = staging_inputs(contract, generated, gateway, files, ca_file, artifacts) if distribute_inputs else None
    # Validate every destination locally before any SSH connection or journal mutation.
    headers = {p["host"]: staging_request(p, inputs) for p in plan["peers"]} if inputs else None
    private = (ROOT / "private").resolve()
    resolved = output.resolve()
    if not resolved.relative_to(private).parts or output.is_symlink(): raise ValueError("Use a private coordinator journal")
    receipt = resolved / "pair-proof.json"
    plan_sha = agent.fingerprint(plan)
    if mode in {"resume", "cancel"}:
        if receipt.is_symlink(): raise ValueError("Coordinator journal cannot be a symlink")
        state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1
                or state.get("plan_sha256") != plan_sha or state.get("target") != plan["edge_contract_sha256"]
                or not isinstance(state.get("operation_id"), str) or not agent.ID.fullmatch(state["operation_id"]) or type(state.get("rollback")) is not bool
                or mode != "cancel" and state.get("staging") != ({name: staging.fingerprint(header) for name, header in headers.items()} if headers else None)
                or not isinstance(state.get("peers"), dict) or set(state["peers"]) - {p["host"] for p in plan["peers"]}
                or any(not isinstance(p, dict) or set(p) != {"job_id", "released"} or type(p["released"]) is not bool
                       or p["job_id"] is not None and (not isinstance(p["job_id"], str) or not agent.ID.fullmatch(p["job_id"])) for p in state["peers"].values())
                or any(state.get(key) is not False for key in ("vip_failover_verified", "application_health_verified", "ha_verified", "full_stack_verified"))):
            raise ValueError("Resume only the original valid private plan, target and job receipt")
    else:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
        state = {"schema_version": 1, "operation_id": uuid4().hex, "plan_sha256": plan_sha, "target": plan["edge_contract_sha256"], "rollback": mode == "rollback",
                 "peers": {}, "phase": "local-validated", **result}
        if headers: state["staging"] = {name: staging.fingerprint(header) for name, header in headers.items()}
        atomic_write_text(receipt, agent.canonical(state))
    def record(phase, **extra):
        if phase == "completed":
            failure = {key: state.pop(key) for key in ("failed_at", "error_code") if key in state}
            if failure: state["last_failure"] = failure
            state["job_state"] = "completed"
        state.update(phase=phase, **extra); atomic_write_text(receipt, agent.canonical(state))
    by_name = {peer["host"]: peer for peer in plan["peers"]}
    peers = [by_name[h["name"]] for h in reversed(contract["plan"]["entry_hosts"])]
    def request(peer, verb, job_id=None):
        return {"verb": verb, "operation_id": state["operation_id"], "plan_sha256": plan_sha, "target": plan["edge_contract_sha256"], "host": peer["host"],
                "machine_sha256": peer["machine_sha256"], "paths": peer["paths"], "expected_current": peer["expected_current"], "rollback": state["rollback"], "tools": tools, "job_id": job_id}
    network = transport or SSH(plan, tools, ssh_binary)
    context = observer_context(contract, ca_file) if observer is None and mode != "cancel" else None
    with coordinator_lock(resolved), network as remote:
        try:
            if mode == "cancel":
                for peer in peers:
                    record("before-reservation-cancellation", peer=peer["host"], write_outcome="unverified")
                    if remote.call(peer, request(peer, "cancel")).get("released") is not True: raise ValueError("Reservation cancellation not acknowledged")
                record("cancelled", **result)
                return {**result, "cancelled": True}
            if inputs:
                for peer in peers:
                    record("before-input-distribution", peer=peer["host"], write_outcome="unverified")
                    response = remote.stage(peer, headers[peer["host"]], inputs, files["gateway_edge_pair_staging.py"])
                    if response.get("manifest_sha256") != state["staging"][peer["host"]] or response.get("staged") is not True:
                        raise ValueError("Peer inputs were not verified")
                    record("inputs-staged", peer=peer["host"], write_outcome="acknowledged")
            for peer in peers:
                record("before-reservation", peer=peer["host"], write_outcome="unverified")
                if remote.call(peer, request(peer, "reserve")).get("reserved") is not True: raise ValueError("Peer reservation was not confirmed")
                state["peers"].setdefault(peer["host"], {"job_id": None, "released": False})
                record("reserved", peer=peer["host"], write_outcome="acknowledged")
            # Inspect both physical hosts and existing services before starting B.
            for peer in peers:
                snapshot = remote.call(peer, request(peer, "inspect"))
                if snapshot.get("machine_sha256") != peer["machine_sha256"]: raise ValueError("Physical-host identity changed")
                if snapshot.get("stable") not in {None if peer["expected_current"] == "absent" else peer["expected_current"], state["target"]}:
                    raise ValueError("Unexpected peer revision")
                if mode != "resume" and snapshot.get("stable") is not None and snapshot.get("healthy") is not True: raise ValueError("An existing peer is not healthy")
            for index, peer in enumerate(peers):
                progress = state["peers"][peer["host"]]
                job_id = progress["job_id"]
                if job_id is None:
                    progress["job_id"] = job_id = uuid4().hex
                    record("before-job-start", peer=peer["host"], write_outcome="unverified")
                    remote.call(peer, request(peer, "start", job_id))
                deadline = time.monotonic() + wait_seconds
                retry_allowed = mode == "resume"
                while True:
                    status = remote.call(peer, request(peer, "status", job_id))
                    if status.get("state") == "completed":
                        if status.get("result", {}).get("host_ready") is not True: raise ValueError("Completed job did not verify host readiness")
                        break
                    if status.get("state") == "not-found" and retry_allowed:
                        remote.call(peer, request(peer, "start", job_id))
                    elif status.get("state") == "failed" and retry_allowed:
                        progress["job_id"] = job_id = uuid4().hex
                        record("before-explicit-retry", peer=peer["host"], write_outcome="unverified")
                        remote.call(peer, request(peer, "start", job_id))
                        retry_allowed = False  # One explicit retry; never loop failed jobs.
                    elif status.get("state") == "unverified" and recover_terminated_jobs:
                        record("before-job-recovery", peer=peer["host"], job_id=job_id, write_outcome="unverified")
                        recovered = remote.call(peer, request(peer, "recover", job_id))
                        if recovered.get("state") not in {"unverified", "running", "completed", "failed", "finishing"}:
                            raise ValueError("Recorded job recovery was not confirmed")
                        if recovered.get("state") == "failed" and recovered.get("result", {}).get("termination_verified") is not True:
                            raise ValueError("Remote job termination was not verified")
                        record("job-recovery-observed", peer=peer["host"], job_id=job_id,
                            termination_verified=recovered.get("result", {}).get("termination_verified") is True)
                    elif status.get("state") not in {"running", "accepted", "prepared", "unverified", "finishing"}:
                        raise ValueError("Remote job has a failed or unverified outcome; retain its handle")
                    if time.monotonic() >= deadline: raise TimeoutError("Remote job wait expired; do not restart a possibly running operation")
                    record("waiting-for-job", peer=peer["host"], job_state=status["state"])
                    time.sleep(1)
                snapshot = remote.call(peer, request(peer, "inspect"))
                if snapshot.get("stable") != state["target"] or snapshot.get("pending") or snapshot.get("healthy") is not True:
                    raise ValueError("Peer no longer runs the current healthy release")
                # During a rolling enablement the old peer can still own the
                # VIP. Require HTTPS continuity there and test the new listener
                # after both peers have the target release.
                observed = (observer() if observer else
                            probe(contract, context=context, check_ssh=index == len(peers) - 1))
                if observed.get("vip_reachable") is not True: raise ValueError("VIP was not reachable before advancing to the other peer")
                record("peer-and-vip-ready", peer=peer["host"], vip_reachable=True, write_outcome="acknowledged")
            for peer in peers:
                snapshot = remote.call(peer, request(peer, "inspect"))
                if snapshot.get("stable") != state["target"] or snapshot.get("pending") or snapshot.get("healthy") is not True: raise ValueError("Final peer verification failed")
            record("pair-services-ready", pair_services_ready=True)
            for peer in peers:
                record("before-reservation-release", peer=peer["host"], write_outcome="unverified")
                if remote.call(peer, request(peer, "release")).get("released") is not True: raise ValueError("Peer reservation release not acknowledged")
                state["peers"][peer["host"]]["released"] = True
                record("reservation-released", peer=peer["host"], write_outcome="acknowledged")
            record("completed", **{**result, "pair_services_ready": True, "vip_reachable": True})
            return {**result, "pair_services_ready": True, "vip_reachable": True}
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="pair-operation-incomplete")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolset-info", action="store_true")
    parser.add_argument("--plan", type=Path); parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--gateway-bundle-dir", type=Path); parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ssh-binary", type=Path); parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--distribute-inputs", action="store_true")
    parser.add_argument("--recover-terminated-jobs", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=180)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("prepare-tools", "apply", "rollback", "resume", "cancel"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    if args.toolset_info:
        files = manifest(tool_files()); print(agent.canonical({"toolset_sha256": agent.fingerprint(files), "files_sha256": files})); return 0
    if any(value is None for value in (args.plan, args.bundle_dir, args.gateway_bundle_dir)): parser.error("Provide the private plan, entry bundle and Gateway bundle")
    mode = next((name.replace("_", "-") for name in ("prepare_tools", "apply", "rollback", "resume", "cancel") if getattr(args, name)), "local")
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=128 * 1024))
        print(agent.canonical(operate(plan, args.bundle_dir, args.gateway_bundle_dir, mode=mode, output=args.output_dir,
            ssh_binary=args.ssh_binary, ca_file=args.ca_file, artifacts=args.artifacts_dir, distribute_inputs=args.distribute_inputs,
            recover_terminated_jobs=args.recover_terminated_jobs, wait_seconds=args.wait_seconds)))
        return 0
    except Exception:
        print("Entry pair stopped. Preserve the private journal, reservations and remote job handles before explicit resume.", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
