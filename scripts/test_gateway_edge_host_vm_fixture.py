#!/usr/bin/env python3
"""Opt-in native entry host lifecycle in two disposable Debian KVM guests."""

import hashlib
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

import yaml

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import render_cilium_gateway as gateway
import render_gateway_edge as renderer
import test_k3s_vm_fixture as vm
from test_gateway_edge_vip_fixture import KEEPALIVED_SHA, OPTIONS_SHA, PACKAGES


HAPROXY_PACKAGE = "haproxy_3.2.26-1~bpo13+1_amd64.deb"
HAPROXY_PACKAGE_SHA = "cda2d07f9a1697f5265c1a3f568db500495046e8a704bd1d4245765dc27b55b3"
HAPROXY_SHA = "0be1f08b8031166bfa22b39720a6a21d49ceca07cea11134ecc0395f5208ebbe"
HAPROXY_LIBRARIES = {
    "liblua5.4-0_5.4.7-1+b2_amd64.deb": "3456e23667f21a5bea4f2182ec878621bf4c47fbc9819fdaad8835d6d1ee6247",
    "libopentracing-c-wrapper0t64_1.1.3-3.1+b1_amd64.deb": "6012776c16169e77fe08e0e0771ccb2e6b1b597b29e34b232c2de1d46a0d7353",
    "libopentracing1_1.6.0-4+b2_amd64.deb": "451786b60ca9724e1b2a85fe99ced3a4210629eb91d1e7e055d0d29b3d791b95",
    "libjemalloc2_5.3.0-3_amd64.deb": "e7d64919e620e8ea46960104efe1e9c977d751e5b610a5d89b0b51aeae9530d7",
}
GUEST_LIBRARIES = {"libsensors5_3.6.2-2_amd64.deb": "f0a994a6d7cfa695dea5343d0d1ba7eed796c0ad920c7282998b95f60049c4f6"}
VIP = "192.0.2.50"


def sha(path):
    return hashlib.sha256(read_bounded_bytes(path, max_bytes=64 * 1024 ** 2)).hexdigest()


def scope():
    if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use the explicit root Linux KVM fixture")
    match = re.fullmatch(r"0::(/system.slice/platform-k3s-vm-edge-[a-z0-9-]+[.]service)", Path("/proc/self/cgroup").read_text().strip())
    if match is None: raise ValueError("Require the original bounded native edge VM service")
    group = Path("/sys/fs/cgroup") / match[1].lstrip("/")
    for name, maximum in (("memory.max", 4 * 1024 ** 3), ("pids.max", 512)):
        value = (group / name).read_text().strip()
        if not value.isdigit() or not 0 < int(value) <= maximum: raise ValueError("Bound VM memory and tasks")
    properties = run_bounded(["systemctl", "show", Path(match[1]).name,
        "--property=Transient,KillMode,RuntimeMaxUSec,MemorySwapMax,NoNewPrivileges"], text=True, check=True, timeout=10).stdout
    props = dict(row.split("=", 1) for row in properties.splitlines() if "=" in row)
    if any(props.get(key) != value for key, value in {"Transient": "yes", "KillMode": "control-group", "MemorySwapMax": "0", "NoNewPrivileges": "yes"}.items()):
        raise ValueError("Require original bounded transient service isolation")
    # systemd prints human-readable durations, rather than a bare microsecond count.
    duration = props.get("RuntimeMaxUSec", "")
    pieces = re.findall(r"([0-9]+)(min|s)", duration)
    if "".join(n + unit for n, unit in pieces) != duration.replace(" ", "") or not 0 < sum(int(n) * (60 if unit == "min" else 1) for n, unit in pieces) <= 600:
        raise ValueError("Use a finite VM deadline at most ten minutes")
    caps = dict(row.split(":", 1) for row in Path("/proc/self/status").read_text().splitlines() if row.startswith(("CapEff:", "CapPrm:", "CapBnd:", "CapInh:", "CapAmb:")))
    if any(int(value.strip(), 16) & (1 << 25) for value in caps.values()): raise ValueError("The parent cannot acquire CAP_SYS_TIME")
    if Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino: raise ValueError("Use a new private network namespace")
    return group, {"unit": Path(match[1]).name, "properties": props, "parent_capabilities": caps}


def stage_native_assets(public, archives, hp_assets, ka_assets, run, write):
    # Guest-only dependency provisioning. No package or library is installed on the parent.
    libraries = public / "libraries"; archives.mkdir(mode=0o700)
    archive_pins = {HAPROXY_PACKAGE: HAPROXY_PACKAGE_SHA, **PACKAGES, **HAPROXY_LIBRARIES, **GUEST_LIBRARIES}
    for name, digest in archive_pins.items():
        source = hp_assets / name if name == HAPROXY_PACKAGE else ka_assets / name if name.startswith("keepalived_") else (hp_assets if name in HAPROXY_LIBRARIES else ka_assets) / "dependencies" / name
        value = read_bounded_bytes(source, max_bytes=64 * 1024 ** 2)
        if source.is_symlink() or hashlib.sha256(value).hexdigest() != digest: raise ValueError("Pinned guest package changed")
        write(archives / name, value); run(["dpkg-deb", "-x", archives / name, libraries])
    artifacts = public / "artifacts"; artifacts.mkdir(mode=0o755)
    for name, origin, digest in (("haproxy", "usr/sbin/haproxy", HAPROXY_SHA), ("keepalived", "usr/sbin/keepalived", KEEPALIVED_SHA), ("keepalived.config-opts", "etc/keepalived/keepalived.config-opts", OPTIONS_SHA)):
        value = read_bounded_bytes(libraries / origin, max_bytes=64 * 1024 ** 2)
        if hashlib.sha256(value).hexdigest() != digest: raise ValueError("Guest binary or sidecar differs")
        write(artifacts / name, value, 0o444 if name.endswith("config-opts") else 0o555)
    return archive_pins


def exercise(test, fixture, *, pair_workload=None, gerrit_router=False):
    if pair_workload is not None and not callable(pair_workload): raise ValueError("Use one explicit real pair workload callback")
    group, original_service = scope()
    repository = Path(__file__).resolve().parents[1]
    sources = {p.name: sha(p) for p in (repository / "scripts").glob("*.py")}
    output = Path(os.environ["GATEWAY_EDGE_HOST_VM_REPORT"]).resolve()
    output.relative_to((repository / "private").resolve())
    if output.exists(): raise ValueError("Use a new create-only private VM report")
    image = Path(os.environ["GATEWAY_EDGE_HOST_VM_IMAGE"]).resolve(strict=True)
    packages = Path(os.environ["GATEWAY_EDGE_HOST_VM_QEMU"]).resolve(strict=True)
    hp_assets = Path(os.environ["GATEWAY_EDGE_HOST_VM_HAPROXY"]).resolve(strict=True)
    ka_assets = Path(os.environ["GATEWAY_EDGE_HOST_VM_KEEPALIVED"]).resolve(strict=True)
    vm.fingerprint(image, vm.IMAGE_SHA512, "sha512")
    vm.fingerprint(packages / "packages.json", vm.PACKAGES_SHA)
    inventory = json.loads((packages / "packages.json").read_text())
    if len(inventory) != 18: raise ValueError("Unexpected reviewed QEMU package inventory")
    links = json.loads(run_bounded(["ip", "-j", "link", "show"], text=True, check=True, timeout=10).stdout)
    if [r["ifname"] for r in links] != ["lo"]: raise ValueError("Require the original empty parent network")
    kernel = vm.kernel_state(); processes = []; servers = []; threads = []; report = {}; stage = "preparation"
    workspace, filesystem = vm.disk_workspace()
    with tempfile.TemporaryDirectory(prefix="platform-k3s-vm-", dir=workspace) as temporary:
        root = Path(temporary); root.chmod(0o700); os.chown(root, 65534, 65534)
        clean = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(root), "LC_ALL": "C"}
        def run(arguments, *, payload=None, seconds=30, check=True, environment=None):
            result = run_bounded(list(map(str, arguments)), input=payload, env=environment or clean, text=True,
                timeout=seconds, check=False, output_max_bytes=4 * 1024 ** 2)
            if check and result.returncode: raise ValueError("Native guest fixture command failed: " + Path(str(arguments[0])).name)
            return result
        def write(path, value, mode=0o444):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value if isinstance(value, bytes) else value.encode()); path.chmod(mode)
        def wait(predicate, seconds=120):
            deadline = time.monotonic() + seconds
            while True:
                if any(p.poll() is not None for p, _ in processes): raise ValueError("An original guest stopped")
                try:
                    value = predicate()
                    if value: return value
                except (OSError, ValueError): pass
                if time.monotonic() >= deadline: raise TimeoutError("Native guest readiness deadline")
                time.sleep(0.5)
        try:
            tools = root / "tools"; tools.mkdir(mode=0o755)
            run(["udevadm", "settle", "--timeout=20"]); wait(vm.kvm_device_ready, 30)
            for item in inventory.values():
                package = packages / Path(item["Filename"]).name; vm.fingerprint(package, item["SHA256"])
                run(["dpkg-deb", "-x", package, tools])
            executable = tools / "usr/bin"
            environment = {**clean, "LD_LIBRARY_PATH": str(tools / "usr/lib/x86_64-linux-gnu"), "QEMU_MODULE_DIR": str(tools / "usr/lib/x86_64-linux-gnu/qemu")}
            test.assertIn("QEMU emulator version 10.2.1", run([executable / "qemu-system-x86_64", "--version"], environment=environment).stdout)
            shutil.copyfile(image, root / "base.qcow2"); (root / "base.qcow2").chmod(0o444)
            base = json.loads(run([executable / "qemu-img", "info", "--output=json", root / "base.qcow2"], environment=environment).stdout)
            if base.get("format") != "qcow2" or base.get("backing-filename"): raise ValueError("Require a standalone pinned base image")
            public = root / "public"; public.mkdir(mode=0o755)
            archive_pins = stage_native_assets(public, root / "archives", hp_assets, ka_assets, run, write)
            for path in (repository / "scripts").glob("*.py"): write(public / "scripts" / path.name, path.read_bytes())
            # Pure Python renderer dependency, captured byte-for-byte with an explicit inventory.
            if yaml.__version__ != "6.0.3": raise ValueError("Use the reviewed renderer version")
            yaml_sources = {p.name: sha(p) for p in Path(yaml.__file__).parent.glob("*.py")}
            for path in Path(yaml.__file__).parent.glob("*.py"): write(public / "scripts/yaml" / path.name, path.read_bytes())
            # Observe caught errors without replacing the production CLI or retrying a write.
            write(public / "invoke_host.py", '''import json,runpy,sys
sys.path.insert(0,"/mnt/fixtures/scripts")
records=[]
def trace(frame,event,value):
    if event=="exception" and frame.f_code.co_filename.startswith("/mnt/fixtures/scripts/"):
        kind,error,_=value
        if isinstance(error,(ValueError,OSError,ImportError)):
            records.append({"type":kind.__name__,"file":frame.f_code.co_filename.rsplit("/",1)[-1],"line":frame.f_lineno,"message":str(error)[:220]})
            del records[:-12]
    return trace
sys.settrace(trace)
try: runpy.run_path("/mnt/fixtures/scripts/manage_gateway_edge_host.py",run_name="__main__")
finally:
    sys.settrace(None)
    if records: print(json.dumps({"synthetic_guest_caught_errors":records}),file=sys.stderr)
''')
            for directory in public.rglob("*"):
                if directory.is_dir() and not directory.is_symlink(): directory.chmod(0o755)
            private = root / "tls"; private.mkdir(mode=0o700)
            run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Native guest fixture CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", private / "ca.key", "-out", public / "ca.crt"])
            run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=forge.example.com", "-keyout", private / "server.key", "-out", private / "server.csr"])
            write(private / "server.ext", "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:forge.example.com,DNS:rpc.example.com\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n", 0o600)
            run(["openssl", "x509", "-req", "-in", private / "server.csr", "-CA", public / "ca.crt", "-CAkey", private / "ca.key", "-set_serial", "2", "-days", "1", "-extfile", private / "server.ext", "-out", private / "server.crt"])
            plan, reviewed, gateway_dir = fixture(public, "raw")
            reviewed["plan"]["https_port"] = 9443
            gateway_files = gateway.render(reviewed["plan"], "raw")
            for name, value in gateway_files.items(): write(gateway_dir / name, value)
            reviewed = json.loads(gateway_files["deployment-contract.json"])
            plan.update(https_port=8443, client_cidrs=["192.0.2.0/24"], ca_sha256=sha(public / "ca.crt"), gateway_contract_sha256=hashlib.sha256(gateway_files["deployment-contract.json"].encode()).hexdigest())
            plan["binaries"]["haproxy"]["sha256"] = HAPROXY_SHA
            plan["binaries"]["keepalived"].update(sha256=KEEPALIVED_SHA, config_opts_sha256=OPTIONS_SHA)
            releases = []
            for label, threshold in (("initial", 2), ("updated", 1)):
                plan["minimum_ready_routes"] = threshold
                files = renderer.render(plan, reviewed, "raw")
                for name, value in files.items(): write(public / label / name, value)
                releases.append(json.loads(files["render-status.json"])["contract_sha256"])
            if gerrit_router:
                from test_gerrit_router_vm_fixture import stage_bundle

                stage_bundle(public, write)
            public_pins = {p.relative_to(public).as_posix(): sha(p) for p in public.rglob("*") if p.is_file() and not p.is_symlink()}
            write(public / "artifact-sums", "\n".join(value + "  " + name for name, value in sorted(public_pins.items())) + "\n")
            # The parent UMask=0077 also affects mkdir(mode=0755). Public ISO
            # paths must remain traversable by the real non-root service user.
            public.chmod(0o755)
            for directory in public.rglob("*"):
                if directory.is_dir() and not directory.is_symlink(): directory.chmod(0o755)
            run([executable / "genisoimage", "-quiet", "-output", root / "tools.iso", "-volid", "FIXTURETOOLS", "-joliet", "-rock", public], seconds=60, environment=environment)
            (root / "tools.iso").chmod(0o444)
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "native-guest-client", "-f", root / "client-key"])
            stage = "private-network"
            for args in (["ip", "link", "set", "lo", "up"], ["ip", "link", "add", "fixturebr0", "type", "bridge"], ["ip", "addr", "add", "192.0.2.1/24", "dev", "fixturebr0"], ["ip", "link", "set", "fixturebr0", "up"]): run(args)
            for address in ("192.0.2.30", "192.0.2.31"): run(["ip", "addr", "add", address + "/32", "dev", "fixturebr0"])
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(private / "server.crt", private / "server.key")
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    body = ("gerrit-" + self.server.peer_role).encode() if gerrit_router else b"native-fixture-backend"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            class Server(ThreadingHTTPServer):
                daemon_threads = True
                def get_request(self):
                    client, address = self.socket.accept(); client.settimeout(2)
                    try: return tls.wrap_socket(client, server_side=True), address
                    except BaseException: client.close(); raise
                def handle_error(self, *args): pass
            class SSHBanner(socketserver.BaseRequestHandler):
                def handle(self):
                    self.request.sendall(f"SSH-2.0-fixture-gerrit-{self.server.peer_role}\r\n".encode("ascii"))

            for role, address in (("primary", "192.0.2.30"), ("backup", "192.0.2.31")):
                server = Server((address, 9443), Handler)
                server.peer_role = role
                servers.append(server)
                thread = threading.Thread(target=server.serve_forever, daemon=True); threads.append(thread); thread.start()
                if gerrit_router:
                    ssh_server = socketserver.ThreadingTCPServer((address, 30222), SSHBanner)
                    ssh_server.peer_role = role
                    servers.append(ssh_server)
                    thread = threading.Thread(target=ssh_server.serve_forever, daemon=True)
                    threads.append(thread)
                    thread.start()
            nodes = [{"name": "edge-a", "address": "192.0.2.10"}, {"name": "edge-b", "address": "192.0.2.11"}]; known = []
            for index, node in enumerate(nodes):
                directory = root / node["name"]; directory.mkdir(mode=0o700); os.chown(directory, 65534, 65534); node["directory"] = directory
                run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", node["name"], "-f", directory / "host-key"])
                known.append(node["address"] + " " + " ".join((directory / "host-key.pub").read_text().split()[:2]))
                mac = "52:54:00:56:78:" + str(index + 1).zfill(2)
                config = {"package_update": False, "package_upgrade": False, "ssh_pwauth": False, "ssh_deletekeys": False,
                    "ssh_keys": {"ed25519_private": (directory / "host-key").read_text(), "ed25519_public": (directory / "host-key.pub").read_text()},
                    "users": [{"name": "fixture", "lock_passwd": True, "shell": "/bin/sh", "sudo": "ALL=(ALL) NOPASSWD:ALL", "ssh_authorized_keys": [(root / "client-key.pub").read_text().strip()]}],
                    "runcmd": [["mkdir", "-p", "/mnt/fixtures"], ["mount", "-o", "ro", "-L", "FIXTURETOOLS", "/mnt/fixtures"],
                        ["sh", "-ec", "cd /mnt/fixtures; sha256sum --quiet -c artifact-sums; printf '/mnt/fixtures/libraries/usr/lib/x86_64-linux-gnu\\n' > /etc/ld.so.conf.d/fixture-edge.conf; ldconfig; touch /run/fixture-artifacts-ready"]]}
                if pair_workload is not None:
                    config["disable_root"] = False
                    config["users"].append({"name": "root", "lock_passwd": True, "ssh_authorized_keys": [(root / "client-key.pub").read_text().strip()]})
                seed = directory / "seed"; seed.mkdir(mode=0o700)
                write(seed / "user-data", "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False), 0o600)
                write(seed / "meta-data", yaml.safe_dump({"instance-id": uuid4().hex, "local-hostname": node["name"]}), 0o600)
                write(seed / "network-config", yaml.safe_dump({"version": 2, "ethernets": {"fixture": {"match": {"macaddress": mac}, "set-name": "eth0", "dhcp4": False, "dhcp6": False, "addresses": [node["address"] + "/24"]}}}), 0o600)
                run([executable / "genisoimage", "-quiet", "-output", directory / "seed.iso", "-volid", "CIDATA", "-joliet", "-rock", seed], environment=environment)
                run([executable / "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", root / "base.qcow2", directory / "disk.qcow2", "8G"], environment=environment)
                for name in ("seed.iso", "disk.qcow2"):
                    (directory / name).chmod(0o600); os.chown(directory / name, 65534, 65534)
                tap = "fixturetap" + str(index)
                for args in (["ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", "65534"], ["ip", "link", "set", tap, "master", "fixturebr0"], ["ip", "link", "set", tap, "up"]): run(args)
                log = (directory / "qemu.log").open("w+b")
                command = [str(executable / "qemu-system-x86_64"), "-no-user-config", "-nodefaults",
                    *([] if gerrit_router else ["-no-reboot"]), "-no-shutdown", "-machine", "pc,accel=kvm,smm=off", "-cpu", "Nehalem", "-smp", "2", "-m", "1024", "-display", "none", "-L", str(tools / "usr/share/qemu"), "-bios", str(tools / "usr/share/seabios/bios-256k.bin"),
                    "-drive", "file=" + str(directory / "disk.qcow2") + ",if=virtio,format=qcow2", "-drive", "file=" + str(directory / "seed.iso") + ",if=virtio,format=raw,readonly=on", "-drive", "file=" + str(root / "tools.iso") + ",if=virtio,format=raw,readonly=on",
                    "-netdev", "tap,id=fixture,ifname=" + tap + ",script=no,downscript=no", "-device", "virtio-net-pci,netdev=fixture,mac=" + mac + ",romfile=", "-device", "virtio-rng-pci", "-serial", "file:" + str(directory / "serial.log"), "-qmp", "unix:" + str(directory / "qmp.sock") + ",server=on,wait=off", "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]
                process = subprocess.Popen([sys.executable, "-B", str(Path(vm.__file__).resolve()), "--qemu-child", str(os.getpid()), *command], env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                processes.append((process, log)); node["pid"] = process.pid
            write(root / "known-hosts", "\n".join(known) + "\n", 0o600)
            def ssh(node, command, *, check=True, seconds=60):
                return run(["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(root / "known-hosts"), "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1", "-i", root / "client-key", "fixture@" + node["address"], command], check=check, seconds=seconds)
            stage = "guest-boot"
            for node in nodes:
                wait(lambda node=node: ssh(node, "test -f /run/fixture-artifacts-ready", check=False, seconds=8).returncode == 0, 180)
                test.assertEqual(ssh(node, "hostname").stdout.strip(), node["name"])
                test.assertEqual(ssh(node, "ip -4 route show default").stdout.strip(), "")
                test.assertIs(vm.qmp(node["directory"] / "qmp.sock", "query-kvm")["enabled"], True)
            def operate(node, label, mode, expected="absent"):
                digest = releases[0 if label == "initial" else 1]
                command = "sudo /usr/bin/python3 -B /mnt/fixtures/invoke_host.py --bundle-dir /mnt/fixtures/" + label + " --gateway-bundle-dir /mnt/fixtures/gateway --contract-sha256 " + digest + " --host " + node["name"] + " --ca-file /mnt/fixtures/ca.crt --artifacts-dir /mnt/fixtures/artifacts --expected-current " + expected + " --" + mode
                result = ssh(node, command, check=False, seconds=110)
                if result.returncode:
                    print(json.dumps({"native_host_operation_failed": {"host": node["name"], "label": label, "mode": mode, "return_code": result.returncode}}), flush=True)
                    print(json.dumps({"synthetic_guest_cli_diagnostics": result.stderr[-5000:]}), flush=True)
                    raise ValueError("Original native host operation failed")
                return json.loads(result.stdout)
            if gerrit_router:
                from test_gerrit_router_vm_fixture import verify_units

                stage = "native-gerrit-router-systemd"
                records = verify_units(test, nodes, ssh, wait, public)
                report.update(gerrit_router_systemd_verified=True, gerrit_router_vip_ownership_verified=True,
                              gerrit_router_vip_withdrawal_verified=True, modeled_gerrit_backends=True,
                              gerrit_router_mixed_rollout_rejected=True,
                              gerrit_router_interrupted_apply_resumed=True,
                              gerrit_router_reboot_with_pending_guard_verified=True,
                              gerrit_router_failed_activation_rollback_verified=True,
                              gerrit_router_native_configuration_rollback_verified=True)
            elif pair_workload is not None:
                stage = "native-pair-coordinator"
                records, pair_evidence = pair_workload(test, {"root": root, "public": public, "nodes": nodes, "ssh": ssh,
                    "run": run, "wait": wait, "releases": releases, "write": write})
                test.assertIs(pair_evidence["pair_coordinator_verified"], True)
                report.update(pair_evidence)
            else:
                stage = "native-host-activation"
                records = []
                for node in nodes:
                    for role, option in (("haproxy", "-v"), ("keepalived", "--version")):
                        version = ssh(node, "sudo /mnt/fixtures/artifacts/" + role + " " + option, check=False)
                        print(json.dumps({"synthetic_guest_version_probe": {"host": node["name"], "role": role, "return_code": version.returncode,
                            "stdout": version.stdout[:1800], "stderr": version.stderr[:1800]}}), flush=True)
                        if version.returncode:
                            libraries = ssh(node, "sudo ldd /mnt/fixtures/artifacts/" + role, check=False)
                            print(json.dumps({"synthetic_guest_linker_diagnostics": libraries.stdout[:4000] + libraries.stderr[:1000]}), flush=True)
                            raise ValueError("The exact guest daemon cannot run with the staged dependency closure")
                    test.assertTrue(operate(node, "initial", "check-host")["host_checked"])
                    test.assertTrue(operate(node, "initial", "apply")["host_ready"])
                    test.assertTrue(operate(node, "initial", "verify", releases[0])["host_ready"])
                    records.append({"host": node["name"], "initial_contract_sha256": releases[0]})
                context = ssl.create_default_context(cafile=str(public / "ca.crt")); context.verify_flags |= ssl.VERIFY_X509_STRICT
                def serves():
                    with socket.create_connection((VIP, 8443), timeout=2) as raw:
                        with context.wrap_socket(raw, server_hostname="forge.example.com") as connection:
                            request = HTTPSConnection("forge.example.com", context=context); request.sock = connection
                            request.request("GET", "/", headers={"Host": "forge.example.com"}); response = request.getresponse()
                            return response.status == 200 and response.read() == b"native-fixture-backend"
                wait(serves, 45)
                stage = "native-host-release-update-and-rollback"
                for node in nodes:
                    test.assertTrue(operate(node, "updated", "apply", releases[0])["host_ready"])
                    test.assertTrue(operate(node, "updated", "verify", releases[1])["host_ready"])
                    test.assertTrue(operate(node, "initial", "rollback", releases[1])["host_ready"])
                    test.assertTrue(operate(node, "initial", "verify", releases[0])["host_ready"])
                    state = json.loads(ssh(node, "sudo cat /var/lib/platform-gateway-edge/state.json").stdout)
                    test.assertEqual(state["stable"], releases[0]); test.assertEqual(state["previous"], releases[1]); test.assertIsNone(state["pending"])
                    records[nodes.index(node)].update(retained_release_count=len(state["releases"]), rollback_verified=True)
                    test.assertEqual(len(state["releases"]), 2)
                wait(serves, 45)
            report.update(native_host_activation_verified=True, native_host_configuration_update_verified=not gerrit_router,
                native_host_retained_rollback_verified=not gerrit_router,
                strict_tls_through_guest_vip_verified=True, guest_image_sha512=vm.IMAGE_SHA512, qemu_packages_sha256=vm.PACKAGES_SHA,
                guest_package_sha256=archive_pins, haproxy_sha256=HAPROXY_SHA, keepalived_sha256=KEEPALIVED_SHA,
                keepalived_options_sha256=OPTIONS_SHA, yaml_version=yaml.__version__, yaml_sources_sha256=yaml_sources,
                public_files_sha256=public_pins, guest_records=records, original_vm_pids=[n["pid"] for n in nodes],
                original_scope_path=str(root), original_service=original_service, disk_filesystem=filesystem,
                original_parent_network_namespace_inode=Path("/proc/self/ns/net").stat().st_ino,
                kvm_guest_isolation_verified=True, actual_gateway_cluster_verified=False, pair_coordinator_verified=pair_workload is not None,
                daemon_version_upgrade_verified=False, physical_node_ha_verified=False, full_stack_verified=False, publisher_signatures_verified=False)
            test.assertEqual(run(["ip", "-4", "route", "show", "default"]).stdout.strip(), "")
            report["parent_cgroup_memory"] = vm.memory_snapshot()
            test.assertEqual(report["parent_cgroup_memory"]["events"].get("oom_kill", 0), 0)
        except BaseException:
            # Only synthetic, isolated guest diagnostics; never inspect production hosts.
            print(json.dumps({"native_guest_failure_stage": stage}), flush=True)
            diagnostics = []
            for process, _ in processes:
                diagnostics.append({"original_pid": process.pid, "return_code": process.poll()})
            for path in root.glob("edge-*/qemu.log"):
                try: text = read_bounded_bytes(path, max_bytes=256 * 1024).decode(errors="replace")[-3000:]
                except (OSError, ValueError): text = "QEMU diagnostics unavailable or over the bounded limit"
                diagnostics.append({"guest": path.parent.name, "qemu_stderr": text})
            print(json.dumps({"synthetic_guest_qemu_diagnostics": diagnostics, "parent_cgroup_memory_at_failure": vm.memory_snapshot()}), flush=True)
            if stage not in {"preparation", "private-network", "guest-boot"}:
                for node in nodes:
                    result = ssh(node, "sudo journalctl -u edge-" + node["name"] + "-haproxy -u edge-" + node["name"] + "-keepalived --no-pager -n 12 -o cat", check=False, seconds=8)
                    print(json.dumps({"synthetic_guest_service_diagnostics": result.stdout[-5000:]}), flush=True)
            raise
        finally:
            for process, log in reversed(processes):
                if process.poll() is None:
                    if os.getpgid(process.pid) != process.pid: raise ValueError("Original VM process group changed")
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=15)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
                log.close()
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=3); test.assertFalse(thread.is_alive())
            test.assertEqual(vm.kernel_state(), kernel)
    test.assertFalse(root.exists())
    if {p.name: sha(p) for p in (repository / "scripts").glob("*.py")} != sources: raise ValueError("Source files changed during native VM acceptance")
    test.assertEqual((group / "cgroup.procs").read_text().split(), [str(os.getpid())])
    report.update(source_files_sha256=sources, original_guests_and_scope_removed=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream: json.dump(report, stream, sort_keys=True)
    return report
