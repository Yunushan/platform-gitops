#!/usr/bin/env python3
"""Root-only, pinned-tool RPC for durable entry-pair reservations and host jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import yaml

from atomic_file import atomic_write_text, _fsync_directory
from bounded_file import read_bounded_bytes, read_bounded_text, read_bounded_stream
import manage_gateway_edge_host as manager
import gateway_edge_systemd_job as systemd_job
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[0-9a-f]{32}")
YAML_VERSION = "6.0.3"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def tools_verified(files):
    if yaml.__version__ != YAML_VERSION: raise ValueError("Use the project's pinned YAML renderer version on both hosts")
    if (not isinstance(files, dict) or not files or any(not re.fullmatch(r"[a-z0-9_]+[.]py", name) or not isinstance(value, str) or not SHA.fullmatch(value) for name, value in files.items())
            or "gateway_edge_pair_agent.py" not in files): raise ValueError("Invalid reviewed tool inventory")
    directory = ROOT / "scripts"
    manager.starter.trusted_paths([directory])
    if {p.name for p in directory.iterdir()} != set(files): raise ValueError("Use an isolated exact tool directory without caches or unrelated modules")
    for name, value in files.items():
        path = directory / name
        manager.starter.trusted_paths([path])
        if path.is_symlink() or manager.sha(read_bounded_bytes(path, max_bytes=2 * 1024 ** 2)) != value:
            raise ValueError("Remote tool differs from its reviewed source")


def process_identity(pid):
    if type(pid) is not int or pid <= 1: return None
    try:
        fields = read_bounded_text(Path("/proc") / str(pid) / "stat", max_bytes=64 * 1024).rsplit(") ", 1)[1].split()
        if fields[0] == "Z": return None
        return {"pid": pid, "start_ticks": int(fields[19]), "boot_id": read_bounded_text(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).strip()}
    except (FileNotFoundError, ProcessLookupError): return None


def signature_live(signature):
    if (not isinstance(signature, dict) or set(signature) != {"pid", "start_ticks", "boot_id"}
            or type(signature["pid"]) is not int or signature["pid"] <= 1
            or type(signature["start_ticks"]) is not int or signature["start_ticks"] < 0
            or not isinstance(signature["boot_id"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", signature["boot_id"])):
        raise ValueError("Unverified recorded process identity")
    return process_identity(signature["pid"]) == signature


class Agent:
    def __init__(self, machine=None):
        self.machine = machine or manager.Host()

    def context(self, request):
        required = {"verb", "operation_id", "plan_sha256", "target", "host", "machine_sha256", "paths", "expected_current", "rollback", "tools", "job_id"}
        if (not isinstance(request, dict) or set(request) != required or request["verb"] not in {"identity", "inspect", "reserve", "start", "status", "release", "cancel", "recover"}
                or not isinstance(request["operation_id"], str) or not ID.fullmatch(request["operation_id"])
                or any(not isinstance(request[k], str) or not SHA.fullmatch(request[k]) for k in ("plan_sha256", "target", "machine_sha256"))
                or request["expected_current"] != "absent" and (not isinstance(request["expected_current"], str) or not SHA.fullmatch(request["expected_current"]))
                or type(request["rollback"]) is not bool or request["job_id"] is not None and (not isinstance(request["job_id"], str) or not ID.fullmatch(request["job_id"]))):
            raise ValueError("Invalid bounded host-job request")
        if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Pair agent requires root Linux")
        machine_id = read_bounded_bytes(Path("/etc/machine-id"), max_bytes=256).strip()
        if not re.fullmatch(rb"[0-9a-f]{32}", machine_id): raise ValueError("Initialize a distinct Linux machine ID")
        actual = manager.sha(machine_id)
        if actual != request["machine_sha256"]: raise ValueError("Remote machine identity differs")
        tools_verified(request["tools"])
        if request["verb"] == "identity": return None, None, None
        paths = request["paths"]
        if not isinstance(paths, dict) or set(paths) != {"bundle", "gateway", "ca", "artifacts"}: raise ValueError("Invalid staged input paths")
        for key, value in paths.items():
            if value is None and key == "artifacts": continue
            if not isinstance(value, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value) or any(p in {".", ".."} for p in Path(value).parts):
                raise ValueError("Use reviewed absolute staged input paths")
            manager.starter.trusted_paths([Path(value)])
        contract, _ = manager.capture(Path(paths["bundle"]), request["target"], Path(paths["gateway"]))
        if request["host"] not in {h["name"] for h in contract["plan"]["entry_hosts"]}: raise ValueError("Unknown entry host")
        self.machine.prerequisites(contract, request["host"])
        manager.starter.trusted_paths([Path("/usr/bin/systemd-run"), Path("/usr/bin/systemctl")])
        version = re.match(r"systemd ([0-9]+)", self.machine.run(["/usr/bin/systemctl", "--version"]))
        if version is None or int(version.group(1)) < 240: raise ValueError("Pair jobs require systemd 240 or later")
        if self.machine.run(["/usr/bin/systemctl", "is-active", "init.scope"]).strip() != "active":
            raise ValueError("Pair jobs require an active host systemd manager")
        owner = {"name": contract["plan"]["name"], "runtime": contract["runtime"], "host": request["host"]}
        lease = {"schema_version": 1, "operation_id": request["operation_id"], "plan_sha256": request["plan_sha256"], "target": request["target"], "owner": owner}
        return contract, owner, lease

    def read_state(self, owner):
        path = self.machine.state / "state.json"
        if not path.exists(): return None
        manager.starter.trusted_paths([path])
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600: raise ValueError("Untrusted host receipt")
        state = loads_strict_json(read_bounded_bytes(path))
        manager.validate_state(state, owner)
        return state

    def jobs(self):
        return self.machine.state / "pair-jobs"

    def job_path(self, job_id):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id): raise ValueError("Reviewed job ID required")
        return self.jobs() / (job_id + ".json")

    def job_read(self, job_id):
        path = self.job_path(job_id)
        if not path.exists(): return None
        manager.starter.trusted_paths([path])
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600: raise ValueError("Untrusted host-job record")
        job = loads_strict_json(read_bounded_bytes(path, max_bytes=256 * 1024))
        if (not isinstance(job, dict) or set(job) != {"schema_version", "request", "state", "launcher", "process", "result"}
                or type(job["schema_version"]) is not int or job["schema_version"] != 1 or not isinstance(job["request"], dict) or job["request"].get("job_id") != job_id
                or job["state"] not in {"prepared", "running", "completed", "failed"}): raise ValueError("Invalid host-job receipt")
        return job

    def status(self, request):
        job = self.job_read(request["job_id"])
        if job is None: return {"state": "not-found"}
        recorded = {**job["request"], "verb": request["verb"]}
        if recorded != request: raise ValueError("Job request identity differs")
        process = job["process"]
        if job["state"] in {"completed", "failed"}:
            if not self.drained(job): return {"state": "finishing", "process": process}
            return {"state": job["state"], "result": job["result"], "process": process}
        signature = process if process is not None else job["launcher"]
        live = isinstance(signature, dict) and process_identity(signature.get("pid")) == signature
        return {"state": "running" if live else "unverified", "process": signature}

    def drained(self, job):
        if job["launcher"] is None: raise ValueError("Restore the original launcher identity before observing termination")
        for signature in (job["launcher"], job["process"]):
            if signature is not None and signature_live(signature): return False
        proof = systemd_job.termination(job["request"]["job_id"], self.machine.run)
        if proof is None: return False
        if proof.get("termination_verified") is not True or proof.get("cgroup_empty") is not True: raise ValueError("Incomplete job termination evidence")
        return True

    def recover(self, request, lease):
        if request["job_id"] is None: raise ValueError("Recover only a recorded job handle")
        try:
            with manager.host_lock(self.jobs() / "launch.lock"), manager.host_lock(self.jobs() / (request["job_id"] + ".lock")):
                status = self.status(request)
                if status["state"] in {"completed", "failed"}:
                    # A worker may have finished between the controller's
                    # status read and this locked request. status already
                    # verified unit drainage; retain its original result.
                    return {**status, "result": {**status["result"], "termination_verified": True}}
                if status["state"] == "not-found": return status
                job = self.job_read(request["job_id"])
                # Reject damaged receipts; never use a missing/guessed process
                # identity as authority to replace a potentially live worker.
                if job["launcher"] is None: raise ValueError("Restore the original launcher identity before recovery")
                for signature in (job["launcher"], job["process"]):
                    if signature is None: continue
                    if signature_live(signature): return {"state": "running", "process": signature}
                with manager.host_lock(self.machine.state / "operation.lock"):
                    if manager.pair_reservation(self.machine.state, lease["owner"]) != lease: raise ValueError("Recovery does not own the host reservation")
                    state = self.read_state(lease["owner"])
                    marker = {key: lease[key] for key in ("operation_id", "plan_sha256", "target")}
                    if state is None or state.get("coordination") != marker: raise ValueError("Restore the matching host receipt before job recovery")
                    proof = systemd_job.termination(request["job_id"], self.machine.run)
                    if proof is None: return {"state": "unverified", "termination_verified": False}
                    if proof.get("termination_verified") is not True or proof.get("cgroup_empty") is not True: raise ValueError("Incomplete job termination evidence")
                    # The locks prevent any late worker from progressing: it
                    # will see failed instead of prepared and refuse execution.
                    job.update(state="failed", result={"error_code": "job-termination-verified", **proof, "ha_verified": False, "full_stack_verified": False})
                    atomic_write_text(self.job_path(request["job_id"]), canonical(job))
                return self.status(request)
        except BlockingIOError:
            return {"state": "unverified", "termination_verified": False}

    def lease_jobs(self, operation_id):
        if not self.jobs().exists(): return []
        result = []
        for path in self.jobs().glob("*.json"):
            job = self.job_read(path.stem)
            if job["request"]["operation_id"] == operation_id: result.append(job)
        return result

    def healthy(self, state, owner):
        if not state or not state["stable"] or state["pending"]: return False
        directory = self.machine.state / "releases" / state["stable"]
        files = manager.release_files(directory, owner, state["releases"][state["stable"]])
        contract = loads_strict_json(files["edge-contract.json"])
        self.machine.ownership(directory, files, contract, owner["host"])
        for role in manager.ROLES: self.machine.verify_role(directory, files, contract, owner["host"], role)
        return manager.health.healthy(contract["plan"]["health_port"], manager.renderer.edge_identity(contract["plan"]))

    def rpc(self, request):
        contract, owner, lease = self.context(request)
        if request["verb"] == "identity": return {"machine_sha256": request["machine_sha256"], "toolset_sha256": fingerprint(request["tools"])}
        if request["verb"] == "status": return self.status(request)
        if request["verb"] == "recover": return self.recover(request, lease)
        manager.secure_directory(self.machine.state)
        if request["verb"] == "start": return self.launch(request, lease)
        observed_lease = manager.pair_reservation(self.machine.state, owner)
        marker = {key: lease[key] for key in ("operation_id", "plan_sha256", "target")}
        if request["verb"] == "reserve" and observed_lease == lease and (self.read_state(owner) or {}).get("coordination") == marker:
            return {"reserved": True}
        if request["verb"] == "inspect":
            if observed_lease is not None and observed_lease != lease: raise ValueError("Another pair operation owns the host")
            try:
                with manager.host_lock(self.machine.state / "operation.lock"):
                    state = self.read_state(owner)
                    return {"machine_sha256": request["machine_sha256"], "stable": (state or {}).get("stable"), "previous": (state or {}).get("previous"),
                        "pending": (state or {}).get("pending"), "healthy": self.healthy(state, owner) if state and not state["pending"] else False, "busy": False}
            except BlockingIOError:
                if observed_lease != lease: raise ValueError("Unrelated host operation is busy") from None
                state = self.read_state(owner)
                return {"machine_sha256": request["machine_sha256"], "stable": (state or {}).get("stable"), "previous": (state or {}).get("previous"),
                        "pending": (state or {}).get("pending"), "healthy": False, "busy": True}
        with manager.host_lock(self.machine.state / "operation.lock"):
            current_lease = manager.pair_reservation(self.machine.state, owner)
            state = self.read_state(owner)
            if request["verb"] == "cancel" and current_lease is None:
                if self.lease_jobs(request["operation_id"]) or state and (state["pending"] or state.get("coordination") not in (None, marker)):
                    raise ValueError("Cancellation cannot discard jobs, pending writes or another reservation")
                if state and state.get("coordination") == marker:
                    state.pop("coordination")
                    if state["stable"] is None and not state["releases"]: (self.machine.state / "state.json").unlink()
                    else: atomic_write_text(self.machine.state / "state.json", canonical(state))
                return {"released": True}
            if request["verb"] == "reserve":
                marker = {key: lease[key] for key in ("operation_id", "plan_sha256", "target")}
                if state and state.get("coordination") not in (None, marker): raise ValueError("Another durable pair marker owns this host")
                if current_lease is not None:
                    if current_lease != lease: raise ValueError("Host is durably reserved by another coordinator")
                    if state is None or state.get("coordination") != marker: raise ValueError("Restore the matching durable host receipt before resuming")
                else:
                    if state and state["pending"]: raise ValueError("An uncoordinated partial host operation requires recovery first")
                    if ((state or {}).get("stable") or "absent") not in {request["expected_current"], request["target"]}:
                        raise ValueError("Unexpected current host release")
                # Install the receipt marker first. Previous managers with an
                # exact state schema also refuse operations during maintenance.
                state = state or {"schema_version": 1, "owner": owner, "stable": None, "previous": None, "pending": None, "releases": {}}
                state["coordination"] = marker
                atomic_write_text(self.machine.state / "state.json", canonical(state))
                atomic_write_text(self.machine.state / "pair-reservation.json", canonical(lease))
                return {"reserved": True}
            if current_lease != lease: raise ValueError("No matching host reservation")
            if state is None: raise ValueError("Restore the missing host receipt before releasing its reservation")
            jobs = self.lease_jobs(request["operation_id"])
            if any(job["state"] not in {"completed", "failed"} or not self.drained(job) for job in jobs): raise ValueError("A job may still be running; reservation retained")
            if request["verb"] == "cancel":
                if jobs: raise ValueError("Cancellation is allowed only before any host job exists")
            elif not state or state["stable"] != request["target"] or state["pending"] or not self.healthy(state, owner):
                raise ValueError("Do not release a partial or unhealthy host")
            (self.machine.state / "pair-reservation.json").unlink()
            _fsync_directory(self.machine.state)
            state.pop("coordination", None)
            if request["verb"] == "cancel" and state["stable"] is None and state["pending"] is None and not state["releases"]:
                (self.machine.state / "state.json").unlink()
                _fsync_directory(self.machine.state)
            else: atomic_write_text(self.machine.state / "state.json", canonical(state))
            return {"released": True}

    def launch(self, request, lease):
        if request["job_id"] is None: raise ValueError("Persist a job ID before launch")
        manager.secure_directory(self.jobs())
        with manager.host_lock(self.jobs() / "launch.lock"):
            existing = self.job_read(request["job_id"])
            if existing is not None: return self.status(request)
            # The child binds its own durable process identity before calling
            # the host manager. A lost launcher acknowledgment does not spawn a
            # second job: this job ID is immutable and launch is idempotent.
            with manager.host_lock(self.jobs() / (request["job_id"] + ".lock")):
                with manager.host_lock(self.machine.state / "operation.lock"):
                    if manager.pair_reservation(self.machine.state, lease["owner"]) != lease: raise ValueError("Job does not own the reservation")
                    if any(job["state"] not in {"completed", "failed"} or not self.drained(job) for job in self.lease_jobs(request["operation_id"])):
                        raise ValueError("A previous job has an unresolved process outcome")
                    job = {"schema_version": 1, "request": request, "state": "prepared", "launcher": process_identity(os.getpid()), "process": None, "result": None}
                    path = self.job_path(request["job_id"])
                    atomic_write_text(path, canonical(job))
                try:
                    job["process"] = process_identity(self.spawn(request["job_id"]))
                    atomic_write_text(path, canonical(job))
                except Exception:
                    job.update(state="failed", result={"error_code": "job-launch-failed"})
                    atomic_write_text(path, canonical(job))
                    raise
            return {"state": "accepted"}

    def spawn(self, job_id):
        log = self.jobs() / (job_id + ".log")
        # PID 1 owns the job's cgroup; losing an SSH login cannot kill a host
        # activation midway through session cleanup. Child CLI processes share
        # that cgroup and its finite time/resource limits.
        return systemd_job.launch(job_id, log, [sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve()), "--run-job", job_id], self.machine.run)

    def execute(self, job_id):
        path = self.job_path(job_id)
        with manager.host_lock(self.jobs() / (job_id + ".lock"), blocking=True):
            job = self.job_read(job_id)
            if job["state"] != "prepared": raise ValueError("Job is not a new prepared operation")
            job.update(state="running", process=process_identity(os.getpid()))
            atomic_write_text(path, canonical(job))
            try:
                request = job["request"]
                _, owner, lease = self.context(request)
                with manager.host_lock(self.machine.state / "operation.lock"):
                    if manager.pair_reservation(self.machine.state, owner) != lease: raise ValueError("Job reservation changed")
                    state = self.read_state(owner)
                stable = (state or {}).get("stable")
                if (stable or "absent") not in {request["expected_current"], request["target"]}: raise ValueError("Host revision changed outside this pair operation")
                if (state or {}).get("pending"):
                    mode = "resume"
                elif stable == request["target"]:
                    mode = "verify"
                else: mode = "rollback" if request["rollback"] else "apply"
                paths = request["paths"]
                result = manager.operate(Path(paths["bundle"]), request["target"], Path(paths["gateway"]), request["host"], mode=mode,
                    ca_file=Path(paths["ca"]), artifacts=Path(paths["artifacts"]) if paths["artifacts"] else None,
                    expected_current=stable or "absent", machine=self.machine, pair_token=request["operation_id"])
                job.update(state="completed", result={key: result.get(key) for key in ("host_ready", "activated", "ha_verified", "full_stack_verified")})
            except BaseException:
                job.update(state="failed", result={"error_code": "host-operation-failed", "ha_verified": False, "full_stack_verified": False})
            atomic_write_text(path, canonical(job))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-job")
    args = parser.parse_args(argv)
    try:
        agent = Agent()
        if args.run_job:
            agent.execute(args.run_job)
        else:
            request = loads_strict_json(read_bounded_stream(sys.stdin.buffer, max_bytes=128 * 1024, label="entry pair RPC"))
            print(canonical(agent.rpc(request)))
        return 0
    except Exception:
        print(canonical({"error": "entry-pair-agent-refused", "details_suppressed": True}))
        return 1


if __name__ == "__main__": raise SystemExit(main())
