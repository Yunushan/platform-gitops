"""Qualify a disposable KVM watchdog for the Patroni service test."""

import json
import socket
import time

from test_k3s_vm_fixture import qmp


GUEST_PROBE = """\
import array
import fcntl
import os
from pathlib import Path
import time

assert os.geteuid() == 65534
descriptor = os.open('/dev/watchdog', os.O_WRONLY | os.O_CLOEXEC)
timeout = array.array('i', [5])
fcntl.ioctl(descriptor, 0xc0045706, timeout)  # Linux WDIOC_SETTIMEOUT
assert 4 <= timeout[0] <= 10
for _ in range(7):
    os.write(descriptor, b'1')
    time.sleep(1)
Path('/run/fixture-watchdog/ready').write_text(str(timeout[0]))
time.sleep(30)  # Intentionally stop petting the disposable device.
"""


def exercise(test, context):
    target, *survivors = context["nodes"]
    ssh = context["ssh"]
    for guest in context["nodes"]:
        ssh(guest, "sudo modprobe i6300esb")
        result = ssh(guest, "test -c /dev/watchdog && cat /sys/class/watchdog/watchdog0/identity")
        test.assertIn("i6300", result.stdout.casefold())
    ssh(target, "sudo install -d -m 0700 -o 65534 -g 65534 /run/fixture-watchdog && "
                "sudo chown 65534:65534 /dev/watchdog && "
                "sudo install -m 0644 /dev/null /run/fixture-watchdog-probe.py && "
                "sudo tee /run/fixture-watchdog-probe.py >/dev/null", payload=GUEST_PROBE)

    monitor = target["directory"] / "qmp.sock"
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(25)
        connection.connect(str(monitor))
        with connection.makefile("rwb") as stream:
            def message():
                raw = stream.readline(65537)
                if not raw or len(raw) > 65536:
                    raise ValueError("Invalid watchdog monitor message")
                return json.loads(raw)

            test.assertIn("QMP", message())
            stream.write(b'{"execute":"qmp_capabilities"}\n')
            stream.flush()
            test.assertIn("return", message())
            ssh(target, "sudo systemd-run --quiet --unit fixture-watchdog-probe --uid=65534 --gid=65534 "
                        "--property=NoNewPrivileges=yes --property=DevicePolicy=closed "
                        "--property='DeviceAllow=/dev/watchdog rw' --property=RuntimeMaxSec=45 "
                        "/usr/bin/python3 -I -B /run/fixture-watchdog-probe.py")
            deadline = time.monotonic() + 15
            while ssh(target, "sudo test -s /run/fixture-watchdog/ready", check=False, seconds=4).returncode != 0:
                if time.monotonic() >= deadline:
                    raise TimeoutError("The unprivileged watchdog did not reach its last keepalive")
                time.sleep(0.25)
            stream.write(b'{"execute":"query-status"}\n')
            stream.flush()
            test.assertTrue(message()["return"]["running"])
            event = message()
            test.assertEqual(event.get("event"), "WATCHDOG")
            test.assertEqual(event.get("data", {}).get("action"), "pause")
    test.assertEqual(qmp(monitor, "query-status")["status"], "watchdog")
    for guest in survivors:
        test.assertTrue(qmp(guest["directory"] / "qmp.sock", "query-status")["running"])
        test.assertEqual(ssh(guest, "hostname").stdout.strip(), guest["name"])
    return {"i6300esb_guest_device_count": len(context["nodes"]), "unprivileged_watchdog_keepalive_verified": True,
            "watchdog_expiry_paused_original_guest": True, "watchdog_other_guests_remained_reachable": True,
            "patroni_service_verified": False, "postgres_failover_verified": False}
