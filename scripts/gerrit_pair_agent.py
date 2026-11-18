#!/usr/bin/env python3
"""Pinned root RPC and PID-1-owned jobs for a prepared Gerrit host pair."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
import activate_gerrit_host as activation
import gateway_edge_pair_agent as identities
import gateway_edge_systemd_job as jobs
from provision_ceph_storage import lock
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"[0-9a-f]{32}")


def private_directory(path):
    if path.exists() or path.is_symlink():
        info = activation.host_tools.trusted(path, private=True, directory=True)
        if info.st_mode & 0o777 != 0o700: raise ValueError("Require the original private agent directory")
    else:
        activation.host_tools.trusted(path.parent, directory=True)
        path.mkdir(mode=0o700)
    return path


def private_json(path):
    activation.host_tools.trusted(path, private=True)
    return loads_strict_json(read_bounded_bytes(path, max_bytes=256 * 1024))


def tool_check(tools):
    if yaml.__version__ != "6.0.3" or not isinstance(tools, dict) or "gerrit_pair_agent.py" not in tools:
        raise ValueError("Require the exact first-party toolset and YAML version")
    root = ROOT / "scripts"; activation.host_tools.trusted(root, directory=True)
    if set(tools) != {p.name for p in root.iterdir()}: raise ValueError("Require an isolated closed tool directory")
    for name, digest in tools.items():
        if not re.fullmatch(r"[a-z0-9_]+\.py", name) or not activation.values.sha(digest): raise ValueError("Invalid pinned tool inventory")
        path = root / name; activation.host_tools.trusted(path)
        if hashlib.sha256(read_bounded_bytes(path, max_bytes=2 * 1024 ** 2)).hexdigest() != digest: raise ValueError("A remote tool changed")


class Agent:
    def __init__(self, host=None): self.host = host

    def context(self, request):
        fields = {"schema_version", "verb", "operation_id", "plan_sha256", "tools", "host_plan", "bundle", "job_id", "action"}
        if (not isinstance(request, dict) or set(request) != fields or type(request["schema_version"]) is not int or request["schema_version"] != 1
                or request["verb"] not in {"check", "reserve", "start", "status", "release"} or not ID.fullmatch(str(request["operation_id"]))
                or not activation.values.sha(request["plan_sha256"]) or not activation.host_tools.local_runtime_path(request["bundle"])
                or request["verb"] in {"start", "status"} and (not ID.fullmatch(str(request["job_id"])) or request["action"] not in {"activate", "resume"})
                or request["verb"] not in {"start", "status"} and (request["job_id"] is not None or request["action"] is not None)):
            raise ValueError("Invalid finite Gerrit pair request")
        if sys.platform != "linux" or os.geteuid() != 0 or sys.version_info < (3, 12): raise ValueError("Require an explicit root Linux agent with Python 3.12 or newer")
        activation.validate(request["host_plan"]); tool_check(request["tools"])
        host_plan = request["host_plan"]; bundle = Path(request["bundle"])
        machine = read_bounded_bytes(Path("/etc/machine-id"), max_bytes=128).strip()
        boot = read_bounded_bytes(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).decode().strip()
        if (not re.fullmatch(rb"[0-9a-f]{32}", machine) or hashlib.sha256(machine).hexdigest() != host_plan["machine_id_sha256"]
                or boot != host_plan["boot_id"]): raise ValueError("Original peer machine or boot changed")
        digest = activation.package(bundle, host_plan)
        private = private_directory(ROOT / "private")
        base = private_directory(private / "gerrit-pair-agent")
        member = private_directory(base / host_plan["member"])
        lease = {"schema_version": 1, "operation_id": request["operation_id"], "plan_sha256": request["plan_sha256"],
            "host_plan_sha256": activation.values.fingerprint(host_plan), "tools_sha256": activation.values.fingerprint(request["tools"]),
            "bundle": str(bundle), "package_sha256": digest}
        return self.host or activation.Host(host_plan["docker"]), member, lease

    def job(self, base, request):
        path = base / (request["job_id"] + ".json")
        if not path.exists() and not path.is_symlink(): return None
        record = private_json(path)
        expected = {k: v for k, v in request.items() if k != "verb"}
        if (not isinstance(record, dict) or set(record) != {"schema_version", "request", "state", "launcher", "worker", "result"}
                or record.get("request") != expected or record.get("state") not in {"prepared", "running", "completed", "failed"}
                or type(record.get("schema_version")) is not int or record.get("schema_version") != 1
                or record.get("launcher") is not None and not isinstance(record["launcher"], dict)
                or record.get("worker") is not None and not isinstance(record["worker"], dict)):
            raise ValueError("Original pair job record changed")
        return record

    def status(self, machine, base, request):
        record = self.job(base, request)
        if record is None: return {"state": "not-found"}
        live = [s for s in (record["launcher"], record["worker"]) if s is not None and identities.signature_live(s)]
        termination = jobs.termination(request["job_id"], machine.run)
        if live or termination is None:
            return {"state": "finishing" if record["state"] in {"completed", "failed"} else "running", "termination_verified": False}
        if record["state"] in {"prepared", "running"}:
            return {"state": "unverified", "termination_verified": True}
        if record["worker"] is None: return {"state": "unverified", "termination_verified": True}
        result = record.get("result")
        if not isinstance(result, dict) or any(result.get(k) is not False for k in activation.UNVERIFIED): raise ValueError("Invalid original job result")
        return {"state": record["state"], "termination_verified": True, "result": result}

    def rpc(self, request):
        machine, member, lease = self.context(request)
        with lock(member):
            reservation_path = member / "reservation.json"
            reservation = private_json(reservation_path) if reservation_path.exists() or reservation_path.is_symlink() else None
            if reservation is not None and reservation != lease: raise ValueError("Another pair operation owns this prepared member")
            if request["verb"] == "check":
                machine.prerequisites(Path(request["bundle"]), request["host_plan"])
                if reservation is None and machine.existing(Path(request["bundle"]), request["host_plan"]) is not None:
                    raise ValueError("Fresh pair cannot adopt an existing member")
                return {"checked": True, "machine_sha256": request["host_plan"]["machine_id_sha256"], "boot_id": request["host_plan"]["boot_id"],
                    "host_plan_sha256": lease["host_plan_sha256"], "package_sha256": lease["package_sha256"]}
            if request["verb"] == "reserve":
                if reservation is None:
                    machine.prerequisites(Path(request["bundle"]), request["host_plan"])
                    if machine.existing(Path(request["bundle"]), request["host_plan"]) is not None: raise ValueError("An unrelated member appeared")
                    atomic_write_text(reservation_path, activation.values.canonical(lease))
                return {"reserved": True}
            if reservation != lease: raise ValueError("The original pair reservation is missing")
            base = private_directory(member / request["operation_id"])
            if request["verb"] == "status": return self.status(machine, base, request)
            if request["verb"] == "release":
                intent = private_json(base / "launch-intent.json")
                if intent["action"] != "resume": raise ValueError("Require a final observation of the original member")
                observed = self.status(machine, base, {**request, "verb": "status", **intent})
                if observed.get("state") != "completed" or observed.get("result", {}).get("original_member_active") is not True:
                    raise ValueError("The original final observation has not completed")
                # Preserve the reservation and proof. Release marks coordination
                # complete; it never authorizes another operation to adopt a member.
                atomic_write_text(base / "coordination-completed.json", activation.values.canonical(lease))
                return {"released": True}
            record = self.job(base, request)
            if record is not None: return {"accepted": True, "reissued": False}
            if (base / "launch-intent.json").exists():
                prior = private_json(base / "launch-intent.json")
                if prior["job_id"] == request["job_id"]: raise ValueError("An uncertain original launch has no job record; never repeat it")
                previous = self.status(machine, base, {**request, "verb": "status", **prior})
                if previous.get("state") != "completed" or request["action"] != "resume": raise ValueError("Observe the original job before requesting another member observation")
            elif request["action"] != "activate": raise ValueError("Fresh pair operations start with one activation job")
            if len(list(base.glob("*.json"))) > 128: raise ValueError("Review the retained original job history before adding observations")
            path = base / (request["job_id"] + ".json")
            record = {"schema_version": 1, "request": {k: v for k, v in request.items() if k != "verb"}, "state": "prepared",
                "launcher": identities.process_identity(os.getpid()), "worker": None, "result": None}
            atomic_write_text(path, activation.values.canonical(record))
            atomic_write_text(base / "launch-intent.json", activation.values.canonical({"job_id": request["job_id"], "action": request["action"]}))
            command = [sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve()), "--run-job", request["host_plan"]["member"], request["operation_id"], request["job_id"]]
            # Existing PID-1 launcher gives the job a dedicated bounded cgroup.
            # Losing SSH never creates a second activation/start operation.
            jobs.launch(request["job_id"], base / (request["job_id"] + ".log"), command, machine.run)
            # The worker writes its own identity/state. A late launcher must
            # never overwrite a running/completed worker record with prepared.
            return {"accepted": True, "reissued": False}

    def execute(self, member, operation, job_id):
        if not activation.sites.label(member) or not ID.fullmatch(operation) or not ID.fullmatch(job_id): raise ValueError("Invalid recorded job handle")
        base = ROOT / "private/gerrit-pair-agent" / member / operation
        activation.host_tools.trusted(base, private=True, directory=True)
        path = base / (job_id + ".json")
        with lock(base):
            record = private_json(path); request = {**record["request"], "verb": "start"}
            machine, directory, lease = self.context(request)
            if (record["state"] != "prepared" or record["request"]["job_id"] != job_id
                    or private_json(directory / "reservation.json") != lease): raise ValueError("Job is not the original prepared activation")
            record.update(state="running", worker=identities.process_identity(os.getpid()))
            atomic_write_text(path, activation.values.canonical(record))
            try:
                result = activation.operate(Path(request["bundle"]), request["host_plan"], mode=request["action"], output=base / "activation", host=machine, wait_seconds=600)
                record.update(state="completed", result=result)
            except BaseException:
                record.update(state="failed", result={"original_member_active": False, "error_code": "gerrit-member-job-failed", **activation.UNVERIFIED})
                atomic_write_text(path, activation.values.canonical(record)); raise
            atomic_write_text(path, activation.values.canonical(record))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--run-job", nargs=3, required=True)
    args = parser.parse_args(argv)
    try: Agent().execute(*args.run_job); return 0
    except BaseException:
        print("Original Gerrit pair job stopped; preserve its private state.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
