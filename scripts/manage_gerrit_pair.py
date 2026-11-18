#!/usr/bin/env python3
"""Coordinate fresh prepared Gerrit hosts through pinned SSH and durable jobs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import time
from uuid import uuid4

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
import activate_gerrit_host as activation
import gerrit_pair_agent as agent
import manage_gateway_edge_pair as ssh_tools
from provision_ceph_storage import lock
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ssh_tools.BOOTSTRAP.replace("gateway_edge_pair_agent.py", "gerrit_pair_agent.py")
UNVERIFIED = {**activation.UNVERIFIED, "physical_failure_domains_verified": False}


def tool_files():
    if yaml.__version__ != "6.0.3": raise ValueError("Use the pinned YAML renderer on every host")
    root = ROOT / "scripts"; found, pending = {}, ["gerrit_pair_agent"]
    while pending:
        name = pending.pop()
        if name in found: continue
        raw = read_bounded_text(root / (name + ".py"), max_bytes=2 * 1024 ** 2)
        found[name] = raw
        for node in ast.walk(ast.parse(raw)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for module in modules:
                candidate = module.split(".")[0]
                if (root / (candidate + ".py")).is_file() and candidate not in found: pending.append(candidate)
    return {name + ".py": value for name, value in sorted(found.items())}


def manifest(files): return {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}


def validate(plan, tools):
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "name", "site_plan", "runtime", "toolset_sha256", "ssh_binary_sha256", "peers"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not activation.sites.label(plan["name"])
            or plan["runtime"] not in {"raw", "docker"} or plan["toolset_sha256"] != activation.values.fingerprint(tools)
            or not activation.values.sha(plan["ssh_binary_sha256"]) or not isinstance(plan["peers"], list) or len(plan["peers"]) != 2):
        raise ValueError("Use the reviewed Gerrit pair/toolset plan")
    activation.sites.validate(plan["site_plan"])
    if {p.get("host") for p in plan["peers"] if isinstance(p, dict)} != {s["name"] for s in plan["site_plan"]["sites"]}: raise ValueError("Pin both exact primaries")
    for peer in plan["peers"]:
        fields = {"host", "address", "port", "identity_env", "known_hosts_env", "remote_root", "bundle", "failure_domain", "host_plan"}
        if (set(peer) != fields or not activation.host_tools.local_runtime_path(peer["remote_root"]) or not activation.host_tools.local_runtime_path(peer["bundle"])
                or not activation.values.integer(peer["port"], 1, 65535) or any(not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", str(peer[k])) for k in ("identity_env", "known_hosts_env"))
                or not isinstance(peer["failure_domain"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", peer["failure_domain"])):
            raise ValueError("Pin SSH transport, staged paths and failure-domain labels")
        activation.validate(peer["host_plan"])
        if (peer["host_plan"]["site_plan"] != plan["site_plan"] or peer["host_plan"]["runtime"] != plan["runtime"]
                or peer["host_plan"]["member"] != peer["host"] or peer["address"] != peer["host_plan"]["health"]["address"]):
            raise ValueError("Bind pair hosts to the same reviewed site/storage plan and direct addresses")
    if (len({p["host_plan"]["machine_id_sha256"] for p in plan["peers"]}) != 2 or len({p["address"] for p in plan["peers"]}) != 2
            or len({p["failure_domain"] for p in plan["peers"]}) != 2): raise ValueError("Require distinct reviewed hosts, addresses and declared domains")
    return plan


class SSH(ssh_tools.SSH):
    def call(self, peer, request):
        command = [*self.peers[peer["host"]], shlex.join(["/usr/bin/python3", "-I", "-B", "-c", BOOTSTRAP, peer["remote_root"], self.plan["toolset_sha256"]])]
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        result = run_bounded(command, env=environment, input=activation.values.canonical(request), text=True, timeout=45, output_max_bytes=128 * 1024, check=False)
        if result.returncode: raise ValueError("Gerrit pair SSH request failed; retain original jobs and receipts")
        response = loads_strict_json(result.stdout)
        if not isinstance(response, dict) or response.get("error"): raise ValueError("Unexpected pinned agent response")
        return response


def source_hash(tools):
    return activation.values.fingerprint({"remote": tools, "coordinator": hashlib.sha256(read_bounded_bytes(Path(__file__), max_bytes=256 * 1024)).hexdigest(),
        "transport": hashlib.sha256(read_bounded_bytes(Path(ssh_tools.__file__), max_bytes=256 * 1024)).hexdigest()})


def export_tools(output):
    if output.exists() or output.is_symlink(): raise ValueError("Export to a new private directory")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts: raise ValueError("Keep the tool export private")
    files = tool_files(); tools = manifest(files)
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    ssh_tools.write_bundle(files, resolved / "scripts", private_root=ROOT / "private")
    atomic_write_text(resolved / "tools-manifest.json", activation.values.canonical(tools))
    return {"exported": True, "files": len(files), "toolset_sha256": activation.values.fingerprint(tools), "deployed": False}


def operate(plan, *, mode="local", output=None, ssh_binary=None, wait_seconds=900, transport=None):
    files = tool_files(); tools = manifest(files); validate(plan, tools); source = source_hash(tools)
    result = {"local_inputs_verified": True, "peers_checked": False, "pair_members_observed_ready": False, "read_only": True, **UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-peers", "activate", "resume"} or not activation.values.integer(wait_seconds, 5, 1800): raise ValueError("Select a bounded pair operation")
    if output is None or output.is_symlink(): raise ValueError("Keep the original private pair operation directory")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts: raise ValueError("Use private coordinator state")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = directory / "gerrit-pair-proof.json"
    with lock(directory):
        if mode == "resume":
            info, parent = receipt.lstat(), directory.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and
                    (info.st_uid != os.geteuid() or info.st_mode & 0o077 or parent.st_uid != os.geteuid() or parent.st_mode & 0o077)): raise ValueError("Private pair journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("plan_sha256") != activation.values.fingerprint(plan)
                    or state.get("source_sha256") != source or state.get("action") != "activate" or not agent.ID.fullmatch(str(state.get("operation_id")))
                    or not isinstance(state.get("jobs"), dict) or not set(state["jobs"]).issubset({phase + "/" + p["host"] for p in plan["peers"] for phase in ("activate", "observe")})
                    or any(not agent.ID.fullmatch(str(v)) for v in state["jobs"].values()) or any(state.get(k) is not False for k in UNVERIFIED)):
                raise ValueError("Resume only the unchanged original Gerrit pair")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": uuid4().hex, "plan_sha256": activation.values.fingerprint(plan),
                "source_sha256": source, "jobs": {}, **result}
        def record(phase, **changes):
            state.update(phase=phase, **changes); atomic_write_text(receipt, activation.values.canonical(state))
        def unchanged():
            if source_hash(manifest(tool_files())) != source: raise ValueError("Coordinator or remote tools changed")
        def request(peer, verb, job_id=None, action=None):
            return {"schema_version": 1, "verb": verb, "operation_id": state["operation_id"], "plan_sha256": state["plan_sha256"],
                "tools": tools, "host_plan": peer["host_plan"], "bundle": peer["bundle"], "job_id": job_id, "action": action}
        def checked(peer, observed):
            if (observed.get("checked") is not True or observed.get("machine_sha256") != peer["host_plan"]["machine_id_sha256"]
                    or observed.get("boot_id") != peer["host_plan"]["boot_id"] or observed.get("host_plan_sha256") != activation.values.fingerprint(peer["host_plan"])
                    or observed.get("package_sha256") != activation.values.fingerprint(activation.sites.render(plan["site_plan"], plan["runtime"]))):
                raise ValueError("Original peer identity or prepared package is unverified")
        record("started", peers_checked=False, pair_members_observed_ready=False)
        network = transport or SSH(plan, tools, ssh_binary)
        # Plan order is an explicit operator choice; both hosts preflight before
        # starting either primary. No router, failover or write test is implied.
        peers = list(reversed(plan["peers"]))
        try:
            with network as remote:
                for peer in peers: checked(peer, remote.call(peer, request(peer, "check")))
                record("peers-checked", peers_checked=True)
                if mode == "check-peers": return {**result, "peers_checked": True}
                for peer in peers:
                    unchanged(); record("before-reserve", peer=peer["host"], read_only=False)
                    if remote.call(peer, request(peer, "reserve")).get("reserved") is not True: raise ValueError("Original peer reservation was not acknowledged")
                for phase, action in (("activate", "activate"), ("observe", "resume")):
                    if phase == "observe" and mode == "resume":
                        # Keep every old handle until its original job is terminal.
                        # A lost final RPC must not turn an old readiness result
                        # into fresh availability evidence on a later resume.
                        previous = {k: v for k, v in state["jobs"].items() if k.startswith("observe/")}
                        for peer in peers:
                            previous_id = previous.get("observe/" + peer["host"])
                            if previous_id is None: continue
                            deadline = time.monotonic() + wait_seconds
                            while True:
                                observed = remote.call(peer, request(peer, "status", previous_id, "resume"))
                                if observed.get("state") == "completed" and observed.get("termination_verified") is True: break
                                if observed.get("state") not in {"running", "finishing"}: raise ValueError("The original prior observation is unverified; retain its handle")
                                if time.monotonic() >= deadline: raise TimeoutError("Observe the same prior job again")
                                time.sleep(1)
                        if previous:
                            state["previous_observations"] = previous
                            state["jobs"] = {k: v for k, v in state["jobs"].items() if not k.startswith("observe/")}
                            record("prior-observations-closed")
                    for peer in peers:
                        key = phase + "/" + peer["host"]; job_id = state["jobs"].get(key)
                        if job_id is None:
                            unchanged(); job_id = state["jobs"][key] = uuid4().hex
                            record("before-job-request", peer=peer["host"], job_id=job_id, read_only=False)
                            if remote.call(peer, request(peer, "start", job_id, action)).get("accepted") is not True: raise ValueError("Original job was not acknowledged")
                        deadline = time.monotonic() + wait_seconds
                        while True:
                            status = remote.call(peer, request(peer, "status", job_id, action))
                            if status.get("state") == "completed":
                                proof = status.get("result", {})
                                if (status.get("termination_verified") is not True or any(proof.get(k) is not True for k in ("host_checked", "original_member_active", "tls_health_observed", "ssh_host_key_observed"))
                                        or any(proof.get(k) is not False for k in activation.UNVERIFIED)): raise ValueError("Original job did not verify its implemented readiness gates")
                                break
                            if status.get("state") not in {"running", "finishing"}: raise ValueError("Original remote job is absent, failed or unverified; never reissue it")
                            record("waiting-for-original-job", peer=peer["host"], job_id=job_id)
                            if time.monotonic() >= deadline: raise TimeoutError("Observe the original remote job again")
                            time.sleep(1)
                        record("original-job-completed", peer=peer["host"], job_id=job_id)
                for peer in peers:
                    unchanged()
                    if remote.call(peer, request(peer, "release")).get("released") is not True: raise ValueError("Final peer observation was not acknowledged")
                result.update(peers_checked=True, pair_members_observed_ready=True, read_only=False)
                record("completed", **result); return result
        except BaseException:
            record("stopped-for-review"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path); parser.add_argument("--mode", choices=("local", "check-peers", "activate", "resume"), default="local")
    parser.add_argument("--export-tools-dir", type=Path)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--ssh-binary", type=Path); parser.add_argument("--wait-seconds", type=int, default=900); parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        if args.export_tools_dir is not None:
            if args.plan or args.mode != "local" or args.output_dir or args.ssh_binary or args.confirm: raise ValueError("Export tools separately from runtime operations")
            print(activation.values.canonical(export_tools(args.export_tools_dir))); return 0
        if args.plan is None: raise ValueError("Select the reviewed private pair plan")
        if args.mode in {"activate", "resume"} and args.confirm != "ACTIVATE_PREPARED_GERRIT_PAIR": raise ValueError("Confirm both prepared hosts")
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=256 * 1024))
        print(activation.values.canonical(operate(plan, mode=args.mode, output=args.output_dir, ssh_binary=args.ssh_binary, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Gerrit pair coordination stopped; preserve original remote jobs and private receipts.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
