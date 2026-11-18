#!/usr/bin/env python3
"""Pinned real NTP for paused disposable guests; the parent cannot set time."""

import csv
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded


PACKAGE = "chrony_4.6.1-3+deb13u2_amd64.deb"
PACKAGE_SHA = "3b3fb6b13818c288ea0121038dc3220da869c6dc5d1e5c4d5f7b13647f3ff7e7"
BINARIES = {
    "chronyd": ("usr/sbin/chronyd", "a7764779efb890339cd9dc52bf39cb9406702de349f9158359e982526f5feff6"),
    "chronyc": ("usr/bin/chronyc", "b08218d1cd915bd056bf9a9992dca6afda195fb0d9e48e010e5977d96780bdea"),
}
TIME_CAPABILITY = 1 << 25


def fingerprint(path, expected):
    if path.is_symlink() or hashlib.sha256(read_bounded_bytes(path, max_bytes=8 * 1024 ** 2)).hexdigest() != expected:
        raise ValueError("Require the pinned original Chrony artifact")


def no_parent_time_capability(status):
    fields = dict(row.split(":", 1) for row in status.splitlines() if ":" in row)
    names = ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")
    if any(not re.fullmatch(r"[0-9a-fA-F]{1,32}", fields.get(name, "").strip())
            or int(fields[name].strip(), 16) & TIME_CAPABILITY for name in names):
        raise ValueError("The lab time relay must have no CAP_SYS_TIME, including its bounding set")
    return {name: fields[name].strip() for name in names}


def scope(root):
    if (root.parent != Path("/var/tmp") or not root.name.startswith("platform-k3s-vm-")
            or root.is_symlink() or root.stat().st_uid != 65534):
        raise ValueError("Require the original private VM scope")
    group = Path("/proc/self/cgroup").read_text()
    if not any(re.fullmatch(r"0::/system.slice/platform-k3s-vm-[a-z0-9-]+\.service", row) for row in group.splitlines()):
        raise ValueError("Require the original bounded VM service")
    no_parent_time_capability(Path("/proc/self/status").read_text())


def stage(assets, public):
    fingerprint(assets / PACKAGE, PACKAGE_SHA)
    for name, (relative, digest) in BINARIES.items():
        source = assets / "extracted" / relative
        fingerprint(source, digest)
        target = public / name
        with target.open("xb") as output: output.write(read_bounded_bytes(source, max_bytes=8 * 1024 ** 2))
        target.chmod(0o755)
    return {name: value[1] for name, value in BINARIES.items()}


def client_config():
    return ("server 192.0.2.1 iburst minpoll 0 maxpoll 1\nmakestep 0.1 -1\n"
        "port 0\ncmdport 0\nbindcmdaddress /var/lib/fixture-time/chronyd.sock\n"
        "pidfile /var/lib/fixture-time/chronyd.pid\ndriftfile /var/lib/fixture-time/drift\n"
        "logdir /var/lib/fixture-time\n")


def tracking(value):
    rows = list(csv.reader(io.StringIO(value)))
    if len(rows) != 1 or len(rows[0]) != 14: raise ValueError("Unexpected original Chrony tracking response")
    row = rows[0]
    # Chrony 4.6.1 CSV includes both the hexadecimal reference ID and address.
    if row[0].upper() != "C0000201" or row[1] != "192.0.2.1" or row[2] != "11" or row[-1] != "Normal":
        raise ValueError("Guest is not synchronized to the original lab relay")
    correction = float(row[4])
    if not math.isfinite(correction) or abs(correction) > 0.25: raise ValueError("Guest clock correction is not settled")
    return {"source": "192.0.2.1", "stratum": 11, "correction_seconds": correction, "leap": row[-1]}


def child():
    parent = int(sys.argv[2]); binary, config = Path(sys.argv[3]), Path(sys.argv[4])
    if os.getppid() != parent or len(sys.argv) != 5: raise SystemExit(1)
    scope(config.parent.parent)
    fingerprint(binary, BINARIES["chronyd"][1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0) or os.getppid() != parent: raise SystemExit(1)
    os.execve(str(binary), [str(binary), "-x", "-d", "-u", "nobody", "-f", str(config)], dict(os.environ))


class LabTime:
    def __init__(self, root, public, processes, run, environment):
        scope(root)
        self.root, self.public, self.run, self.environment = root, public, run, environment
        self.state = root / "time"; self.state.mkdir(mode=0o700); os.chown(self.state, 65534, 65534)
        configuration = ("bindaddress 192.0.2.1\nallow 192.0.2.0/24\nlocal stratum 10\nport 123\ncmdport 0\n"
            "bindcmdaddress " + str(self.state / "chronyd.sock") + "\npidfile " + str(self.state / "chronyd.pid")
            + "\ndriftfile " + str(self.state / "drift") + "\nlogdir " + str(self.state) + "\n")
        config = self.state / "chrony.conf"; config.write_text(configuration); config.chmod(0o600); os.chown(config, 65534, 65534)
        self.configuration_sha256 = hashlib.sha256(configuration.encode()).hexdigest()
        self.log = (self.state / "server.log").open("w+b")
        self.process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--time-server-child", str(os.getpid()), str(public / "chronyd"), str(config)],
            env=environment, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, start_new_session=True)
        processes.append((self.process, self.log))

    def ready(self):
        if self.process.poll() is not None: raise ValueError("Original lab time relay stopped")
        path = self.state / "chronyd.sock"
        if not path.exists() or not stat.S_ISSOCK(path.stat().st_mode): return False
        pid = self.process.pid
        self.capabilities = no_parent_time_capability(Path("/proc", str(pid), "status").read_text())
        if Path("/proc", str(pid), "exe").resolve() != (self.public / "chronyd").resolve(): raise ValueError("Original lab time daemon changed")
        args = Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0")
        if b"-x" not in args or os.getpgid(pid) != pid: raise ValueError("Lab relay time-control or process-group guard changed")
        return True

    def start_clients(self, nodes, ssh, wait, runtime_seconds):
        wait(self.ready, seconds=30)
        for node in nodes:
            for binary in ("chronyd", "chronyc"):
                print(json.dumps({"lab_time_stage": "guest-binary-version", "node": node["name"], "binary": binary}), flush=True)
                observed = ssh(node, "sudo /mnt/fixtures/" + binary + " -v")
                if not re.search(r"version 4[.]6[.]1\b", observed.stdout): raise ValueError("Original guest Chrony version differs")
            ssh(node, 'for unit in systemd-timesyncd.service chrony.service; do if systemctl --quiet is-active "$unit"; then sudo systemctl stop "$unit"; fi; done')
            ssh(node, "sudo install -d -m 700 -o 65534 -g 65534 /var/lib/fixture-time")
            ssh(node, "sudo systemd-run --quiet --unit=fixture-time --property=Type=exec --property=RuntimeMaxSec=" + str(runtime_seconds)
                + " --property=Restart=on-failure --property=RestartSec=1 --property=StartLimitIntervalSec=120 --property=StartLimitBurst=3"
                " --property=MemoryMax=32M --property=TasksMax=16 --property=NoNewPrivileges=yes --property=UMask=0077"
                " /mnt/fixtures/chronyd -d -u nobody -f /etc/fixture/time.conf")
        print(json.dumps({"lab_time_stage": "guest-synchronization"}), flush=True)
        self.initial = self.synchronize(nodes, ssh, wait)
        return self.initial

    def synchronize(self, nodes, ssh, wait):
        for node in nodes:
            wait(lambda node=node: ssh(node, "sudo test -S /var/lib/fixture-time/chronyd.sock", check=False, seconds=8).returncode == 0, seconds=20)
            ssh(node, "sudo /mnt/fixtures/chronyc -h /var/lib/fixture-time/chronyd.sock waitsync 30 0.25 0 1", seconds=35)
        def observe():
            self.ready(); rows = []
            for node in nodes:
                before = time.time()
                result = ssh(node, "sudo /mnt/fixtures/chronyc -n -c -h /var/lib/fixture-time/chronyd.sock tracking && python3 -c 'import time; print(time.time())'", seconds=10)
                after = time.time(); lines = result.stdout.splitlines()
                if len(lines) != 2: raise ValueError("Unexpected guest clock observation")
                try:
                    state = tracking(lines[0]); clock = float(lines[1])
                except ValueError:
                    fields = next(csv.reader([lines[0]]))
                    print(json.dumps({"lab_time_observation": {"node": node["name"], "field_count": len(fields),
                        "reference_is_lab_ipv4": len(fields) > 1 and fields[1] == "192.0.2.1",
                        "reference_is_lab_hex": fields[0].upper() == "C0000201" if fields else False,
                        "stratum_is_expected": len(fields) > 2 and fields[2] == "11",
                        "leap_is_normal": bool(fields) and fields[-1] == "Normal"}}), flush=True)
                    raise
                if not math.isfinite(clock) or clock - after < -0.5 or clock - before > 0.5: return False
                rows.append({"node": node["name"], **state, "offset_bounds_seconds": [round(clock - after, 6), round(clock - before, 6)]})
            return rows
        self.latest = wait(observe, seconds=90)
        return self.latest

    def report(self):
        self.ready()
        return {"lab_guest_clock_sync_verified": True, "lab_time_guest_count": len(self.latest),
            "lab_ntp_parent_time_control_disabled_verified": True, "lab_ntp_parent_capabilities": self.capabilities,
            "lab_ntp_server_pid": self.process.pid, "lab_ntp_configuration_sha256": self.configuration_sha256,
            "lab_chrony_package_sha256": PACKAGE_SHA, "lab_chrony_binaries_sha256": {key: value[1] for key, value in BINARIES.items()},
            "lab_initial_guest_clocks": self.initial, "lab_final_guest_clocks": self.latest,
            "lab_time_physical_redundancy_verified": False}


def relay_probe(test):
    """Actual NTP reply and process permissions in a fresh bounded namespace."""
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")}
    processes = []
    with tempfile.TemporaryDirectory(prefix="platform-k3s-vm-", dir="/var/tmp") as directory:
        root = Path(directory); root.chmod(0o700); os.chown(root, 65534, 65534)
        def run(args, seconds=10): return run_bounded(list(map(str, args)), text=True, check=True, timeout=seconds, output_max_bytes=1024 ** 2)
        scope(root)
        links = json.loads(run(["ip", "-j", "link", "show"]).stdout)
        test.assertEqual([item["ifname"] for item in links], ["lo"])
        test.assertEqual(json.loads(run(["ip", "-j", "route", "show", "default"]).stdout), [])
        public = root / "public"; public.mkdir(mode=0o755)
        stage(Path(os.environ["K3S_VM_TEST_TIME_ARTIFACTS"]).resolve(strict=True), public)
        for args in (["ip", "link", "set", "lo", "up"], ["ip", "link", "add", "fixturebr0", "type", "bridge"],
                ["ip", "addr", "add", "192.0.2.1/24", "dev", "fixturebr0"], ["ip", "link", "set", "fixturebr0", "up"]): run(args)
        try:
            relay = LabTime(root, public, processes, run, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(root), "LC_ALL": "C"})
            deadline = time.monotonic() + 15
            while not relay.ready():
                if time.monotonic() > deadline: raise TimeoutError("Lab relay startup deadline")
                time.sleep(0.1)
            now = time.time(); seconds = int(now) + 2208988800
            transmit = struct.pack("!II", seconds, int((now % 1) * 2 ** 32))
            packet = b"\x23" + bytes(39) + transmit
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(3); client.sendto(packet, ("192.0.2.1", 123)); response, address = client.recvfrom(4096)
            test.assertEqual(address, ("192.0.2.1", 123)); test.assertGreaterEqual(len(response), 48)
            test.assertEqual(response[0] & 7, 4); test.assertEqual(response[0] >> 6, 0); test.assertEqual(response[1], 10)
            test.assertEqual(response[24:32], transmit)
            server_seconds, fraction = struct.unpack("!II", response[40:48])
            test.assertLess(abs(server_seconds - 2208988800 + fraction / 2 ** 32 - time.time()), 0.5)
            print(json.dumps({"real_lab_ntp_relay_verified": True, "parent_time_control_disabled_verified": True, "capabilities": relay.capabilities}), flush=True)
        finally:
            for process, log in reversed(processes):
                if process.poll() is None:
                    test.assertEqual(os.getpgid(process.pid), process.pid); os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
                log.close()
        test.assertEqual(sources, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")})
    test.assertFalse(root.exists())


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--time-server-child": child()
