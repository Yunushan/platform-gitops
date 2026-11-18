#!/usr/bin/env python3
"""Launch bounded entry jobs under PID 1 and read authoritative termination evidence."""

import os
from pathlib import Path
import re

from bounded_file import read_bounded_text


ID = re.compile(r"[0-9a-f]{32}")
PROPERTIES = {"LoadState", "ActiveState", "SubState", "MainPID", "ControlPID", "ControlGroup", "Transient", "Restart"}


def unit_name(job_id):
    if not isinstance(job_id, str) or not ID.fullmatch(job_id): raise ValueError("A recorded job ID is required")
    return "platform-entry-job-" + job_id + ".service"


def launch(job_id, log, command, run):
    unit = unit_name(job_id)
    descriptor = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    run(["/usr/bin/systemd-run", "--unit=" + unit, "--slice=system.slice", "--service-type=exec", "--collect", "--quiet",
        "--property=UMask=0077", "--property=NoNewPrivileges=yes", "--property=KillMode=control-group", "--property=TimeoutStopSec=10",
        "--property=RuntimeMaxSec=900", "--property=MemoryMax=512M", "--property=TasksMax=128", "--property=Restart=no",
        "--property=Delegate=no", "--property=SendSIGKILL=yes",
        "--property=StandardOutput=append:" + str(log), "--property=StandardError=inherit", *command])
    value = run(["/usr/bin/systemctl", "show", unit, "--property=MainPID", "--value"]).strip()
    if not value.isdigit() or int(value) <= 1: raise ValueError("Transient job process identity was not confirmed")
    return int(value)


def properties(job_id, run):
    output = run(["/usr/bin/systemctl", "show", unit_name(job_id), "--property=" + ",".join(sorted(PROPERTIES))])
    result = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in PROPERTIES or key in result: raise ValueError("Unverified systemd job properties")
        result[key] = value
    if set(result) != PROPERTIES: raise ValueError("Incomplete systemd job properties")
    return result


def termination(job_id, run):
    """Return evidence only for an inactive unit with no remaining cgroup members.

    Callers must also hold the job and host-operation locks and verify both
    recorded launcher/worker process identities. A missing PID alone is not
    evidence that a helper subprocess or queued launch has finished.
    """
    values = properties(job_id, run)
    absent = values["LoadState"] == "not-found"
    if (values["LoadState"] not in {"loaded", "not-found"} or values["ActiveState"] not in {"inactive", "failed"}
            or values["SubState"] not in {"dead", "failed"} or values["MainPID"] != "0" or values["ControlPID"] != "0"
            or values["Restart"] != "no" or not absent and values["Transient"] != "yes"):
        return None
    group = values["ControlGroup"]
    if absent and group: return None
    if group:
        if group != "/system.slice/" + unit_name(job_id): return None
        # cgroup v2's populated flag includes descendants, not just the main PID.
        events = read_bounded_text(Path("/sys/fs/cgroup" + group) / "cgroup.events", max_bytes=4096)
        rows = [line.split() for line in events.splitlines()]
        if any(len(row) != 2 for row in rows) or len({row[0] for row in rows}) != len(rows): return None
        if dict(rows).get("populated") != "0": return None
    return {"termination_verified": True, "unit_absent": absent, "active_state": values["ActiveState"],
            "cgroup_empty": True, "boot_id": read_bounded_text(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).strip()}
