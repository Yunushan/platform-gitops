"""Run the generated Gerrit entry units in two disposable Debian guests."""

import http.client
import json
import shlex
import socket
import ssl

from check_gerrit_router_backends import check_routers
import render_gerrit_router as router


VIP = "192.0.2.50"
HOST = "forge.example.com"
UNITS = ("gerrit-router.service", "gerrit-router-vrrp.service")


def stage_bundle(public, write):
    plan = {
        "schema_version": 1, "canonical_host": HOST,
        "primary_host": "192.0.2.30", "backup_host": "192.0.2.31",
        "https_port": 9443, "ssh_port": 30222, "namespace": "fixture",
        "ca_secret": "fixture-ca", "haproxy_image": "registry.example.invalid/haproxy@sha256:" + "a" * 64,
        "native_vip": {"vip": VIP + "/24", "vrid": 73, "entry_hosts": [
            {"name": "edge-a", "address": "192.0.2.10", "interface": "eth0", "failure_domain": "fixture-a"},
            {"name": "edge-b", "address": "192.0.2.11", "interface": "eth0", "failure_domain": "fixture-b"},
        ]},
    }
    for phase, updates in (
        ("regular", {"hold_primary": False}),
        ("held", {"hold_primary": True}),
        ("unavailable", {"hold_primary": True, "backup_host": "192.0.2.32"}),
    ):
        release = {**plan, **updates}
        for name, content in router.render(release, "raw").items():
            write(public / "gerrit-router" / phase / name, content)
        write(public / "gerrit-router" / phase / "plan.json", json.dumps(release))


def verify_units(test, nodes, ssh, wait, public):
    regular, held = (json.loads((public / "gerrit-router" / phase / "plan.json").read_text(encoding="utf-8"))
                     for phase in ("regular", "held"))
    inventory = [{"address": node["address"], "port": router.READINESS_PORT} for node in nodes]
    context = ssl.create_default_context(cafile=str(public / "ca.crt"))
    context.verify_flags |= ssl.VERIFY_X509_STRICT

    def service(node, action, *units, check=True):
        return ssh(node, shlex.join(["sudo", "systemctl", action, *units]), check=check)

    def owners():
        owners = []
        for node in nodes:
            rows = json.loads(ssh(node, "ip -j -4 address show dev eth0").stdout)
            if any(address["local"] == VIP for row in rows for address in row["addr_info"]):
                owners.append(node["name"])
        test.assertLessEqual(len(owners), 1)
        return owners

    def serves(role="primary"):
        with socket.create_connection((VIP, 8443), timeout=2) as transport:
            with context.wrap_socket(transport, server_hostname=HOST) as connection:
                request = http.client.HTTPSConnection(HOST, context=context)
                request.sock = connection
                request.request("GET", "/", headers={"Host": HOST, "Connection": "close"})
                response = request.getresponse()
                if response.status != 200 or response.read(64) != f"gerrit-{role}".encode("ascii"):
                    return False
        with socket.create_connection((VIP, 29418), timeout=2) as connection:
            with connection.makefile("rb") as stream:
                return stream.readline(128) == f"SSH-2.0-fixture-gerrit-{role}\r\n".encode("ascii")

    def apply_args(node, phase):
        release = "/mnt/fixtures/gerrit-router/"
        previous = node.get("release")
        args = ["sudo", "python3", "-B", "/mnt/fixtures/scripts/apply_gerrit_native_vip.py",
                "--plan", release + phase + "/plan.json", "--host", node["name"], "--backend-ca", "/mnt/fixtures/ca.crt"]
        args += ["--previous-plan", release + previous + "/plan.json"] if previous else ["--fresh"]
        return args

    def configure(node, phase, *, interrupt=False):
        host = node["name"]
        release = "/mnt/fixtures/gerrit-router/"
        previous = node.get("release")
        args = apply_args(node, phase)
        if interrupt:
            # Kill the CLI immediately after one committed configuration write.
            # The guests, systemd, files, preflight and parsers are unchanged.
            fault = """
import os
import sys
sys.path.insert(0, "/mnt/fixtures/scripts")
import apply_gerrit_native_vip as apply
write = apply.atomic_write_text
def interrupted(path, *args, **kwargs):
    write(path, *args, **kwargs)
    if path == apply.CONFIG / "haproxy.cfg":
        os._exit(97)
apply.atomic_write_text = interrupted
sys.exit(apply.main(sys.argv[1:]))
"""
            killed = ssh(node, shlex.join(["sudo", "python3", "-B", "-c", fault, *args[4:]]), check=False)
            test.assertEqual(killed.returncode, 97, (killed.stdout + killed.stderr)[-3000:])
            ssh(node, "sudo test -f /var/lib/gerrit-router/pending.json")
            ssh(node, f"sudo cmp /etc/haproxy/haproxy.cfg {release}{phase}/haproxy.cfg")
            ssh(node, f"sudo cmp /etc/haproxy/gerrit-router-vrrp.conf {release}{previous}/keepalived-{host}.conf")
            unexpected = [*args[:5], release + previous + "/plan.json", *args[6:]]
            test.assertNotEqual(ssh(node, shlex.join(unexpected), check=False).returncode, 0)
            ssh(node, "sudo test -f /var/lib/gerrit-router/pending.json")
            original_boot = ssh(node, "cat /proc/sys/kernel/random/boot_id").stdout.strip()
            ssh(node, "sudo systemctl reboot --no-block", check=False)

            def rebooted():
                result = ssh(node, "cat /proc/sys/kernel/random/boot_id", check=False, seconds=8)
                return result.returncode == 0 and result.stdout.strip() not in {"", original_boot}

            wait(rebooted, 90)
            ssh(node, "sudo mount -o ro -L FIXTURETOOLS /mnt/fixtures")
            ssh(node, "sudo test -f /var/lib/gerrit-router/pending.json")
            for unit in UNITS:
                test.assertEqual(service(node, "is-enabled", unit).stdout.strip(), "enabled")
                service(node, "start", unit)
                test.assertNotEqual(service(node, "is-active", unit, check=False).returncode, 0)
                status = ssh(node, f"systemctl show {unit} -p ConditionResult -p MainPID").stdout.splitlines()
                test.assertIn("ConditionResult=no", status)
                test.assertIn("MainPID=0", status)
        installed = ssh(node, shlex.join(args), check=False)
        if installed.returncode:
            parsed = ssh(node, "cd / && sudo -u haproxy /usr/sbin/haproxy -c -f /etc/haproxy/haproxy.cfg", check=False)
            print(json.dumps({"synthetic_haproxy_parser": (parsed.stdout + parsed.stderr)[-3000:]}), flush=True)
        test.assertEqual(installed.returncode, 0, (installed.stdout + installed.stderr)[-3000:])
        test.assertTrue(json.loads(installed.stdout)["host_applied"])
        test.assertTrue(json.loads(installed.stdout)["changed"])
        repeated = ssh(node, shlex.join(args))
        test.assertFalse(json.loads(repeated.stdout)["changed"])
        ssh(node, "sudo test ! -e /var/lib/gerrit-router/pending.json")
        node["release"] = phase
        for unit in UNITS:
            test.assertEqual(service(node, "is-active", unit).stdout.strip(), "active")
            result = ssh(node, f"systemctl show {unit} -p FragmentPath -p MainPID -p DropInPaths")
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            test.assertEqual(properties["FragmentPath"], "/etc/systemd/system/" + unit)
            test.assertEqual(properties["DropInPaths"], "")
            test.assertGreater(int(properties["MainPID"]), 1)

    records = []
    for node in nodes:
        commands = [
            "if ! getent passwd haproxy >/dev/null; then useradd --system --user-group --shell /usr/sbin/nologin haproxy; fi",
            "install -d -m 0755 /etc/haproxy/backend-ca",
            "install -m 0755 /mnt/fixtures/artifacts/haproxy /usr/sbin/haproxy",
            "install -m 0755 /mnt/fixtures/artifacts/keepalived /usr/sbin/keepalived",
            "install -m 0644 /mnt/fixtures/artifacts/keepalived.config-opts /usr/sbin/keepalived.config-opts",
        ]
        ssh(node, "sudo sh -ec " + shlex.quote("\n".join(commands)))
        configure(node, "regular")
        service(node, "enable", *UNITS)
        records.append({"host": node["name"], "preflight_verified": True, "systemd_units_verified": True})

    owner = wait(lambda: next(iter(owners()), None) if serves() else None, 35)
    original = next(node for node in nodes if node["name"] == owner)
    other = next(node for node in nodes if node is not original)
    service(original, "stop", UNITS[0])
    wait(lambda: owners() == [other["name"]] and serves(), 35)
    service(original, "start", UNITS[0])
    wait(lambda: owners() == [other["name"]] and serves(), 35)
    for node in nodes:
        service(node, "stop", UNITS[0])
    wait(lambda: owners() == [], 35)
    for port in (8443, 29418):
        with test.assertRaises(OSError), socket.create_connection((VIP, port), timeout=2):
            pass
    for node in nodes:
        service(node, "start", UNITS[0])
    wait(lambda: len(owners()) == 1 and serves(), 35)

    current = owners()[0]
    standby = next(node for node in nodes if node["name"] != current)
    failed_args = apply_args(standby, "unavailable")
    rejected = ssh(standby, shlex.join(failed_args), check=False)
    test.assertEqual(rejected.returncode, 1)
    ssh(standby, "sudo test -f /var/lib/gerrit-router/activating.json")
    test.assertNotEqual(service(standby, "is-active", UNITS[1], check=False).returncode, 0)
    test.assertEqual(owners(), [current])
    test.assertTrue(serves())
    wrong_args = apply_args(standby, "held") + ["--rollback-pending"]
    test.assertNotEqual(ssh(standby, shlex.join(wrong_args), check=False).returncode, 0)
    restored = ssh(standby, shlex.join([*failed_args, "--rollback-pending"]))
    test.assertTrue(json.loads(restored.stdout)["host_applied"])
    ssh(standby, "sudo test ! -e /var/lib/gerrit-router/pending.json && sudo test ! -e /var/lib/gerrit-router/activating.json")
    test.assertTrue(check_routers(regular, inventory)["router_rollout_verified"])
    test.assertEqual(owners(), [current])
    test.assertTrue(serves())

    def rollout(phase, target, peer_role):
        current = wait(lambda: next(iter(owners()), None), 15)
        inactive = next(node for node in nodes if node["name"] != current)
        active = next(node for node in nodes if node["name"] == current)
        configure(inactive, phase, interrupt=phase == "held")
        test.assertEqual(owners(), [current])
        report = check_routers(target, inventory)
        test.assertFalse(report["router_rollout_verified"])
        test.assertEqual(report["routers_matching"], 1)
        service(active, "stop", UNITS[1], UNITS[0])
        wait(lambda: owners() == [inactive["name"]] and serves(peer_role), 35)
        configure(active, phase)
        test.assertTrue(check_routers(target, inventory)["router_rollout_verified"])
        test.assertEqual(owners(), [inactive["name"]])
        test.assertTrue(serves(peer_role))

    rollout("held", held, "backup")
    rollout("regular", regular, "primary")
    for record in records:
        record.update(hold_verified=True, rollback_verified=True)
        if record["host"] == standby["name"]:
            record["failed_activation_rollback_verified"] = True
    for node in nodes:
        service(node, "stop", UNITS[1], UNITS[0])
    wait(lambda: owners() == [], 20)
    return records
