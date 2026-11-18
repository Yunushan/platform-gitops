#!/usr/bin/env python3
"""Disposable KVM-backed K3s workers; no borrowed cluster or host sysctl writes."""

import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock
from uuid import uuid4

import yaml
from bounded_subprocess import run_bounded
from kubernetes_management import Client, verify_target

ARTIFACTS = {"k3s": "959c9310a6ab893958d1c95bc5d7609de9d7884630c8832180f059369b6dc331",
    "k3s-airgap-images-amd64.tar.zst": "2e3d6d14bbbeb8c16f1849cca8da48887c5a7ddceb1cc2bf60be63e4fa8c63f3"}
IMAGE_SHA512 = "d25999de0770d4cde1be72c299ba3ad94eac7ea8fd4d18f8cc86c69df69337e2e8a91d64465a9bfd8f2a78c566fcbcb0842eacc91c09efdef72d719d9f13b535"
PACKAGES_SHA = "12ef01f92c478f1759941b16acab999cd65df31ce5e136662a8d9fd8e6fba423"
KUBECTL_SHA = "a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989"
PROBE_INDEX = "101b4afd76732482eff9b95cae5f94bcf295e521fbec4e01b69c5421f3f3f3e5"
PROBE_MANIFEST = "ad9fa4d07136a83e69a54ef00102f579d04eba431932de3b0f098cc5d5948f9f"
PROBE_IMAGE = "docker.io/rancher/mirrored-library-busybox@sha256:" + PROBE_INDEX
KERNEL_KEYS = ("vm/overcommit_memory", "vm/panic_on_oom", "kernel/panic", "kernel/panic_on_oops", "kernel/keys/root_maxkeys", "kernel/keys/root_maxbytes")
PROBE = r'''test "$(id -u)" = 1000
test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token
grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status
grep -Eq '^Seccomp:[[:space:]]+2$' /proc/self/status
grep -Eq '^CapEff:[[:space:]]+0+$' /proc/self/status
awk '$5 == "/" {print $6}' /proc/self/mountinfo | grep -Eq '^ro(,|$)'
test "$(cat /sys/fs/cgroup/memory.max)" = 67108864
printf '{"schema_version":1,"pod_uid":"%s","node":"%s","kernel_security_verified":true}\n' "$POD_UID" "$NODE_NAME"
sleep 10
'''


def fingerprint(path, expected, algorithm="sha256"):
    if path.is_symlink() or not path.is_file(): raise ValueError("Require an original pinned public artifact")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, algorithm).hexdigest() != expected: raise ValueError("Fixture artifact checksum changed")


def kernel_state(): return {name: Path("/proc/sys", name).read_text().strip() for name in KERNEL_KEYS}


def kvm_device_ready():
    import grp
    import stat
    state = Path("/dev/kvm").stat()
    # On a cold distro start udev can still be assigning the device group.
    # Never grant permissions or snapshot the temporary root group into a VM.
    return (state.st_uid == 0 and state.st_gid != 0 and grp.getgrgid(state.st_gid).gr_name == "kvm"
        and stat.S_IMODE(state.st_mode) == 0o660)


def disk_workspace():
    root = Path("/var/tmp").resolve(strict=True)
    if root != Path("/var/tmp") or not root.is_dir(): raise ValueError("Require the original disk-backed fixture workspace")
    filesystem = run_bounded(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(root)], text=True, timeout=10, check=True, output_max_bytes=1024).stdout.strip()
    if filesystem not in {"ext4", "xfs", "btrfs"}: raise ValueError("VM disk images require a reviewed disk-backed filesystem, not tmpfs")
    if shutil.disk_usage(root).free < 16 * 1024 ** 3: raise ValueError("Require at least 16 GiB free for disposable VM disks")
    return root, filesystem


def qmp(path, command, *, arguments=None, expected_event=None):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5); connection.connect(str(path)); stream = connection.makefile("rwb")
        events = []
        def message():
            raw = stream.readline(128 * 1024 + 1)
            if not raw or len(raw) > 128 * 1024: raise ValueError("Invalid bounded QMP response")
            return json.loads(raw)
        def response():
            for _ in range(32):
                value = message()
                if "event" not in value: return value
                events.append(value)
            raise ValueError("Unexpected QMP event flood")
        if "QMP" not in response(): raise ValueError("Invalid private VM monitor")
        stream.write(b'{"execute":"qmp_capabilities"}\n'); stream.flush(); response()
        events.clear()
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        stream.write((json.dumps(request) + "\n").encode()); stream.flush()
        value = response()
        if "return" not in value: raise ValueError("Private VM monitor command failed")
        if expected_event is not None:
            for _ in range(32):
                event, reason = expected_event
                if any(entry.get("event") == event and entry.get("data", {}).get("reason") == reason
                       for entry in events):
                    break
                events.append(message())
            else:
                raise ValueError("Private VM monitor did not report the expected event")
        return value["return"]


def child():
    parent = int(sys.argv[2])
    if os.getppid() != parent: raise SystemExit(1)
    group = Path("/dev/kvm").stat().st_gid
    os.setgroups([group]); os.setgid(65534); os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or libc.prctl(38, 1, 0, 0, 0) or os.getppid() != parent: raise SystemExit(1)
    if Path(sys.argv[3]).name == "qemu-system-x86_64":
        # Check access using exactly the dropped identity, not root's access.
        # This never changes device permissions or falls back to emulation.
        descriptor = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        os.close(descriptor)
    os.execve(sys.argv[3], sys.argv[3:], dict(os.environ))


def memory_snapshot():
    name = next((line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")), "")
    if not re.fullmatch(r"/system.slice/platform-k3s-vm-[a-z0-9-]+\.service", name): raise ValueError("Require the original VM cgroup")
    group = Path("/sys/fs/cgroup") / name.lstrip("/")
    result = {field: (group / ("memory." + field)).read_text().strip() for field in ("current", "peak", "high", "max")}
    result["events"] = {key: int(value) for key, value in (row.split() for row in (group / "memory.events").read_text().splitlines())}
    return result


def native_entry_memory_profile(group):
    expected = {"memory.high": 11776 * 1024 ** 2, "memory.max": 12 * 1024 ** 3, "memory.swap.max": 0}
    for name, value in expected.items():
        if (group / name).read_text().strip() != str(value):
            raise ValueError("The seven-guest profile requires MemoryHigh=11776M, MemoryMax=12G and MemorySwapMax=0")
    return {name: value for name, value in expected.items()}


def application_workers(context):
    return context["application_workers"] if context.get("runtime") == "rke2" else context["nodes"][1:]


def guest_identity_lifetime(context=None):
    seconds=1100 if context is None else context.get('guest_runtime_limit_seconds',1100)
    if type(seconds) is not int or seconds not in (1100,1700,2300):raise ValueError('Require a bounded guest runtime profile')
    # The extended failure case can outlive ten-minute Git and Kubernetes
    # credentials. Keep identities finite and cover only its bounded lifetime.
    return {1100: '10m', 1700: '30m', 2300: '40m'}[seconds]


def guest_runtime_profile(seconds,*,quorum=False,network_bootstrap=None,admission_workers=False):
    if type(quorum) is not bool or type(admission_workers) is not bool:raise ValueError('Require explicit boolean guest runtime profiles')
    extended=(quorum and network_bootstrap is not None) or admission_workers
    allowed = {1100, 1700, 2300} if admission_workers else {1100, 1700} if extended else {1100}
    if type(seconds) is not int or seconds not in allowed:
        raise ValueError('Select the default or explicit extended Cilium/admission guest runtime profile')
    return seconds


def guest_failure_journals(nodes,ssh,sensitive):
    diagnostics={}
    for node in nodes:
        try:result=ssh(node,"sudo journalctl -u fixture-k3s --no-pager -n 10 -o cat",check=False,seconds=10)
        except (subprocess.SubprocessError,OSError,ValueError) as error:
            diagnostics[node['name']]={'capture_failed':True,'exception_type':type(error).__name__};continue
        value=result.stdout+result.stderr
        for private_value in sensitive:value=value.replace(private_value,'<redacted>')
        diagnostics[node['name']]=re.sub(r'[A-Za-z0-9_+/=-]{32,}','<redacted>',value)[-3500:]
    return diagnostics


def zuul_rollout_bridge(links, group, taps=()):
    """Use only the dedicated ZooKeeper bridge in the combined rollout test."""
    members = {"zp1", "zp2", "zp3", *taps}
    if (os.environ.get("ZUUL_ARTIFACT_ROLLOUT_TEST_RUNTIME") != "1"
            or not re.fullmatch(r"/system.slice/platform-k3s-vm-zuul-artifact-[a-z0-9-]+\.service", group)
            or Path("/proc/self/ns/net").stat().st_ino == Path("/proc/1/ns/net").stat().st_ino
            or {row.get("ifname") for row in links} != {"lo", "brzk", *members}
            or any(row.get("master") != "brzk" for row in links if row.get("ifname") in members)):
        raise ValueError("Require the original isolated Zuul rollout bridge")
    def read_ip(*args):
        return json.loads(run_bounded(["/usr/sbin/ip", "-j", *args], text=True, timeout=10, check=True).stdout)
    addresses = read_ip("addr", "show", "dev", "brzk")
    if (len(addresses) != 1 or not any(row.get("local") == "192.0.2.1" and row.get("prefixlen") == 24
            for row in addresses[0].get("addr_info", [])) or read_ip("route", "show", "default")):
        raise ValueError("The original Zuul bridge address or route changed")
    return "brzk"


def exercise(test, *, workload=None, quorum=False, network_bootstrap=None, guest_runtime_seconds=1100,
             native_edge_hosts=False, admission_workers=False, flannel_network_policy=False,
             cilium_repository_workers=False, guest_cpu_model="Nehalem", server_fixture=None,
             server_fixture_agent=False, server_fixture_extra_agent=False, server_fixture_watchdog=False,
             server_fixture_page_reporting=False,
             first_worker_memory_mib=2048, second_worker_memory_mib=2048, zuul_rollout=False):
    if server_fixture is not None and (not callable(server_fixture) or workload is not None or quorum or admission_workers or network_bootstrap is not None):
        raise ValueError("Select only one disposable server fixture")
    if type(server_fixture_agent) is not bool or (server_fixture_agent and server_fixture is None):
        raise ValueError("Select the extra agent only for a disposable server fixture")
    if type(server_fixture_extra_agent) is not bool or (server_fixture_extra_agent and (
            not server_fixture_agent or server_fixture is None or native_edge_hosts
            or (first_worker_memory_mib, second_worker_memory_mib) != (2048, 2048))):
        raise ValueError("A second server-fixture agent requires the fixed five-guest resource profile")
    if type(server_fixture_watchdog) is not bool or (server_fixture_watchdog and server_fixture is None):
        raise ValueError("Select emulated watchdogs only for a disposable server fixture")
    if type(server_fixture_page_reporting) is not bool or (server_fixture_page_reporting and server_fixture is None):
        raise ValueError("Select guest free-page reporting only for a disposable server fixture")
    if guest_cpu_model not in ("Nehalem", "host"):
        raise ValueError("Select the default or host KVM CPU model")
    worker_memory = (first_worker_memory_mib, second_worker_memory_mib)
    cilium_large_workers = (quorum is True and callable(network_bootstrap) and callable(workload)
                            and not (admission_workers or native_edge_hosts or cilium_repository_workers)
                            and worker_memory == (3072, 3072))
    if (any(type(memory) is not int or not 2048 <= memory <= 3584 for memory in worker_memory)
            or (worker_memory != (2048, 2048)
                and not cilium_large_workers
                and ((workload is None and server_fixture is None) or quorum or admission_workers))):
        raise ValueError("Extra memory requires a bounded single-control workload or two 3072 MiB Cilium workers")
    if type(cilium_repository_workers) is not bool or cilium_repository_workers and (
            not quorum or network_bootstrap is None or admission_workers or native_edge_hosts):
        raise ValueError("The repository fixture requires three controls, three workers and an external CNI")
    if type(flannel_network_policy) is not bool or (flannel_network_policy and quorum):
        raise ValueError("Flannel policy testing requires the single-control fixture")
    if type(admission_workers) is not bool or admission_workers and (quorum or network_bootstrap is not None or native_edge_hosts):
        raise ValueError("Select the separate three-worker admission fixture explicitly")
    if type(native_edge_hosts) is not bool or (native_edge_hosts and server_fixture is None
            and (not quorum or network_bootstrap is None or guest_runtime_seconds != 1700)):
        raise ValueError("Native edge guests require an explicit Cilium/quorum or server fixture")
    time_assets = os.environ.get("K3S_VM_TEST_TIME_ARTIFACTS", "")
    if quorum and not time_assets: raise ValueError("Quorum pause tests require explicit pinned lab clock synchronization artifacts")
    guest_runtime_profile(guest_runtime_seconds,quorum=quorum,network_bootstrap=network_bootstrap,admission_workers=admission_workers)
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/dev/kvm").is_char_device(): raise ValueError("Use the explicit root Linux KVM fixture")
    group_name = next((line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")), "")
    if not re.fullmatch(r"/system.slice/platform-k3s-vm-[a-z0-9-]+\.service", group_name): raise ValueError("Require an original bounded disposable VM service")
    group = Path("/sys/fs/cgroup") / group_name.lstrip("/")
    # The extended admission profile can run both Tekton and Kyverno on the same workers.
    memory_gib = 13 if admission_workers and guest_runtime_seconds == 2300 else 12
    for name, maximum in (("memory.max", memory_gib * 1024 ** 3), ("pids.max", 4096)):
        value = (group / name).read_text().strip()
        if not value.isdigit() or not 0 < int(value) <= maximum: raise ValueError("Require finite VM service memory and process limits")
    resource_profile = native_entry_memory_profile(group) if native_edge_hosts else None
    links = json.loads(run_bounded(["/usr/sbin/ip", "-j", "link", "show"], text=True, timeout=10, check=True).stdout)
    bridge_name = "fixturebr0"
    if zuul_rollout:
        if (zuul_rollout is not True or not callable(workload) or any((quorum, native_edge_hosts, server_fixture))
                or (admission_workers and os.environ.get("ZUUL_ADMITTED_ARTIFACT_ROLLOUT_TEST_RUNTIME") != "1")):
            raise ValueError("Select the single-control Zuul rollout workload")
        bridge_name = zuul_rollout_bridge(links, group_name)
    elif len(links) != 1 or links[0].get("ifname") != "lo":
        raise ValueError("Require a new empty network namespace")
    if workload is not None and not callable(workload): raise ValueError("Require one explicit fixture workload callback")
    if type(quorum) is not bool: raise ValueError("Select the explicit three-control-plane fixture")
    if network_bootstrap is not None and (not quorum or not callable(network_bootstrap)):
        raise ValueError("An external CNI fixture requires the explicit five-guest topology and callback")
    repository = Path(__file__).resolve().parents[1]
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repository / "scripts").glob("*.py")}
    assets = Path(os.environ["K3S_VM_TEST_ARTIFACTS"]).resolve(strict=True)
    packages = Path(os.environ["K3S_VM_TEST_QEMU"]).resolve(strict=True)
    kubectl_source = Path(os.environ["K3S_VM_TEST_KUBECTL"]).resolve(strict=True)
    for name, digest in ARTIFACTS.items(): fingerprint(assets / name, digest)
    fingerprint(assets / "debian-13-genericcloud-amd64.qcow2", IMAGE_SHA512, "sha512")
    fingerprint(packages / "packages.json", PACKAGES_SHA); fingerprint(kubectl_source, KUBECTL_SHA)
    inventory = json.loads((packages / "packages.json").read_text())
    if len(inventory) != 18: raise ValueError("Unexpected reviewed QEMU package inventory")
    host_kernel = kernel_state(); processes = []; sensitive = []; stage = "preparation"; report = {}; lab_time = None
    guest_taps = []
    workspace, filesystem = disk_workspace()
    with tempfile.TemporaryDirectory(prefix="platform-k3s-vm-", dir=workspace) as temporary:
        root = Path(temporary); root.chmod(0o700); os.chown(root, 65534, 65534)
        def write(name, value):
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value, encoding="utf-8"); path.chmod(0o600); os.chown(path, 65534, 65534); return path
        tools = root / "tools"; tools.mkdir(mode=0o755)
        clean = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(root), "LC_ALL": "C"}
        def run(arguments, *, payload=None, seconds=30, check=True, environment=None):
            result = run_bounded(list(map(str, arguments)), input=payload, env=environment or clean, text=True, timeout=seconds, check=False, output_max_bytes=4 * 1024 ** 2)
            if check and result.returncode:
                executable = Path(str(arguments[0])).name
                if executable in {"ssh", "scp"}:
                    error = result.stderr.casefold()
                    print(json.dumps({"guest_transport_failure": {"executable": executable, "return_code": result.returncode,
                        "signals": {key: key in error for key in ("connection timed out", "connection reset", "connection closed", "connection refused",
                            "no route to host", "host key verification failed", "permission denied", "no such file or directory",
                            "broken pipe", "no space left", "connection lost", "failure")}}}), flush=True)
                raise ValueError("Disposable VM fixture command failed; private diagnostics suppressed")
            return result
        def healthy():
            if any(p.poll() is not None for p, _ in processes): raise ValueError("An original disposable VM process stopped")
            for path in root.glob("*/qmp.sock"):
                try: state = qmp(path, "query-status").get("status")
                except (OSError, ValueError): continue
                if state in {"shutdown", "internal-error", "guest-panicked"}: raise ValueError("A disposable guest stopped before readiness")
        def wait(function, seconds=180):
            deadline = time.monotonic() + seconds
            while True:
                healthy()
                try:
                    result = function()
                    if result: return result
                except (OSError, ValueError): pass
                if time.monotonic() >= deadline: raise TimeoutError("Disposable VM fixture readiness deadline")
                time.sleep(1)
        try:
            run(["udevadm", "settle", "--timeout=20"], seconds=25)
            wait(kvm_device_ready, seconds=30)
            for item in inventory.values():
                path = packages / Path(item["Filename"]).name; fingerprint(path, item["SHA256"])
                run(["dpkg-deb", "-x", path, tools])
            executable = tools / "usr/bin"
            environment = {**clean, "LD_LIBRARY_PATH": str(tools / "usr/lib/x86_64-linux-gnu"), "QEMU_MODULE_DIR": str(tools / "usr/lib/x86_64-linux-gnu/qemu")}
            for name, expected in (("qemu-system-x86_64", "QEMU emulator version 10.2.1"), ("qemu-img", "qemu-img version 10.2.1")):
                test.assertIn(expected, run([executable / name, "--version"], environment=environment).stdout)
            local_bin = root / "bin"; local_bin.mkdir(mode=0o755)
            shutil.copyfile(kubectl_source, local_bin / "kubectl"); (local_bin / "kubectl").chmod(0o755)
            shutil.copyfile(assets / "debian-13-genericcloud-amd64.qcow2", root / "base.qcow2"); (root / "base.qcow2").chmod(0o444)
            base = json.loads(run([executable / "qemu-img", "info", "--output=json", root / "base.qcow2"], environment=environment).stdout)
            if base.get("format") != "qcow2" or base.get("backing-filename"): raise ValueError("Require a standalone pinned guest base image")
            public = root / "public"; public.mkdir(mode=0o755)
            public_artifacts = dict(ARTIFACTS)
            for name in ARTIFACTS:
                shutil.copyfile(assets / name, public / name); (public / name).chmod(0o755 if name == "k3s" else 0o444)
            if time_assets:
                import test_k3s_time_fixture as clock
                clock.scope(root)
                public_artifacts.update(clock.stage(Path(time_assets).resolve(strict=True), public))
            edge_inputs = None
            if native_edge_hosts:
                from test_cilium_edge_runtime_fixture import stage_assets
                edge_inputs = stage_assets(root, public, run)
                public_artifacts.update(edge_inputs["public_files_sha256"])
            run([executable / "genisoimage", "-quiet", "-output", root / "tools.iso", "-volid", "FIXTURETOOLS", "-joliet", "-rock", public], seconds=60, environment=environment)
            (root / "tools.iso").chmod(0o444)
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fixture-client", "-f", root / "client-key"])
            sensitive.append((root / "client-key").read_text()); known_hosts = []
            stage = "private-network"
            run(["/usr/sbin/ip", "link", "set", "lo", "up"])
            if not zuul_rollout:
                run(["/usr/sbin/ip", "link", "add", bridge_name, "type", "bridge"])
                run(["/usr/sbin/ip", "addr", "add", "192.0.2.1/24", "dev", bridge_name])
                run(["/usr/sbin/ip", "link", "set", bridge_name, "up"])
            if time_assets:
                lab_time = clock.LabTime(root, public, processes, run, clean)
                wait(lab_time.ready, seconds=30)
            nodes = [{"name": "fixture-control", "address": "192.0.2.2", "memory": 1536},
                     {"name": "fixture-worker-1", "address": "192.0.2.3", "memory": first_worker_memory_mib},
                     {"name": "fixture-worker-2", "address": "192.0.2.4", "memory": second_worker_memory_mib}]
            if admission_workers or cilium_repository_workers:
                nodes.append({"name": "fixture-worker-3", "address": "192.0.2.7", "memory": 2048})
            if server_fixture is not None:
                for node in nodes:
                    node["memory"] = 2304 if native_edge_hosts else 2560
                for node, memory in zip(nodes[1:], worker_memory):
                    if memory != 2048:
                        node["memory"] = memory
            if server_fixture_agent:
                nodes.append({"name": "fixture-agent", "address": "192.0.2.8", "memory": 1536})
            if server_fixture_extra_agent:
                nodes.append({"name": "fixture-agent-2", "address": "192.0.2.9", "memory": 1536})
            workers = nodes[1:]
            controls = [nodes[0]]
            if quorum:
                controls.extend([{"name": "fixture-control-2", "address": "192.0.2.5", "memory": 1536}, {"name": "fixture-control-3", "address": "192.0.2.6", "memory": 1536}])
                if network_bootstrap is not None and not (cilium_repository_workers or cilium_large_workers):
                    for node in controls: node["memory"] = 1792
                nodes.extend(controls[1:])
            edge_nodes = [dict(node) for node in edge_inputs["nodes"]] if edge_inputs is not None else []
            all_guests = [*nodes, *edge_nodes]
            for index, node in enumerate(all_guests):
                directory = root / node["name"]; directory.mkdir(mode=0o700); os.chown(directory, 65534, 65534)
                key = directory / "host-key"; run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", node["name"], "-f", key])
                host_key = key.read_text(); sensitive.append(host_key)
                public_key = " ".join((directory / "host-key.pub").read_text().split()[:2]); known_hosts.append(node["address"] + " " + public_key)
                mac = "52:54:00:12:34:" + str(index + 1).zfill(2)
                config = {"package_update": False, "package_upgrade": False, "ssh_pwauth": False, "ssh_deletekeys": False,
                    "ssh_keys": {"ed25519_private": host_key, "ed25519_public": public_key}, "users": [{"name": "fixture", "lock_passwd": True, "shell": "/bin/sh", "sudo": "ALL=(ALL) NOPASSWD:ALL", "ssh_authorized_keys": [(root / "client-key.pub").read_text().strip()]}],
                    "runcmd": [["mkdir", "-p", "/mnt/fixtures"], ["mount", "-o", "ro", "-L", "FIXTURETOOLS", "/mnt/fixtures"], ["sh", "-ec", "cd /mnt/fixtures; sha256sum -c /etc/fixture/artifact-sums; touch /run/fixture-artifacts-ready"]],
                    "write_files": [{"path": "/etc/fixture/artifact-sums", "permissions": "0600", "content": "\n".join(digest + "  " + name for name, digest in public_artifacts.items()) + "\n"}]}
                if lab_time:
                    config["write_files"].append({"path": "/etc/fixture/time.conf", "permissions": "0600", "content": clock.client_config()})
                if network_bootstrap is not None and node not in edge_nodes:
                    # With Flannel disabled K3s leaves CNI directories to the
                    # external plugin. Declare both explicitly before containerd
                    # starts, rather than assuming the Flannel data paths apply.
                    config["write_files"].append({"path": "/var/lib/fixture-k3s/agent/etc/containerd/config-v3.toml.tmpl", "permissions": "0600",
                        "content": '{{ template "base" . }}\n[plugins.\'io.containerd.cri.v1.runtime\'.cni]\nbin_dirs = ["/var/lib/fixture-k3s/data/cni"]\nconf_dir = "/var/lib/fixture-k3s/agent/etc/cni/net.d"\n'})
                if node in edge_nodes:
                    from test_cilium_edge_runtime_fixture import configure_guest
                    configure_guest(config, (root / "client-key.pub").read_text().strip())
                seed = directory / "seed"; seed.mkdir(mode=0o700)
                (seed / "user-data").write_text("#cloud-config\n" + yaml.safe_dump(config, sort_keys=False))
                (seed / "meta-data").write_text(yaml.safe_dump({"instance-id": uuid4().hex, "local-hostname": node["name"]}))
                (seed / "network-config").write_text(yaml.safe_dump({"version": 2, "ethernets": {"fixture": {"match": {"macaddress": mac}, "set-name": "eth0", "dhcp4": False, "dhcp6": False, "addresses": [node["address"] + "/24"], "routes": [] if node in edge_nodes else [{"to": "0.0.0.0/0", "via": "192.0.2.1"}]}}}))
                run([executable / "genisoimage", "-quiet", "-output", directory / "seed.iso", "-volid", "CIDATA", "-joliet", "-rock", seed], environment=environment)
                run([executable / "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", root / "base.qcow2", directory / "disk.qcow2", "16G"], environment=environment)
                for name in ("seed.iso", "disk.qcow2"):
                    (directory / name).chmod(0o600); os.chown(directory / name, 65534, 65534)
                tap = "fixturetap" + str(index)
                run(["/usr/sbin/ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", "65534"])
                guest_taps.append(tap)
                run(["/usr/sbin/ip", "link", "set", tap, "master", bridge_name]); run(["/usr/sbin/ip", "link", "set", tap, "up"])
                log = (directory / "qemu.log").open("w+b")
                command = [str(executable / "qemu-system-x86_64"), "-no-user-config", "-nodefaults", "-no-reboot", "-no-shutdown", "-machine", "pc,accel=kvm,smm=off", "-cpu", guest_cpu_model, "-smp", "2", "-m", str(node["memory"]), "-display", "none", "-L", str(tools / "usr/share/qemu"),
                    "-bios", str(tools / "usr/share/seabios/bios-256k.bin"), "-drive", "file=" + str(directory / "disk.qcow2") + ",if=virtio,format=qcow2", "-drive", "file=" + str(directory / "seed.iso") + ",if=virtio,format=raw,readonly=on",
                    "-drive", "file=" + str(root / "tools.iso") + ",if=virtio,format=raw,readonly=on", "-netdev", "tap,id=fixture,ifname=" + tap + ",script=no,downscript=no", "-device", "virtio-net-pci,netdev=fixture,mac=" + mac + ",romfile=",
                    "-device", "virtio-rng-pci", "-serial", "file:" + str(directory / "serial.log"), "-qmp", "unix:" + str(directory / "qmp.sock") + ",server=on,wait=off", "-d", "guest_errors,cpu_reset", "-D", str(directory / "cpu.log"), "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]
                if server_fixture_watchdog and node in nodes:
                    command.extend(("-device", "i6300esb", "-action", "watchdog=pause,reboot=reset"))
                if server_fixture_page_reporting:
                    command.extend(("-device", "virtio-balloon-pci,free-page-reporting=on"))
                process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--qemu-child", str(os.getpid()), *command], env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                processes.append((process, log)); node["directory"] = directory; node["qemu_pid"] = process.pid
            write("known-hosts", "\n".join(known_hosts) + "\n")
            def ssh(node, command, *, payload=None, check=True, seconds=30):
                return run(["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(root / "known-hosts"), "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1", "-i", root / "client-key", "fixture@" + node["address"], command], payload=payload, check=check, seconds=seconds)
            def upload(node, path, destination):
                if not re.fullmatch(r"/tmp/fixture-image-[0-9a-f]{64}\.tar", destination): raise ValueError("Require a fixed private guest image path")
                run(["scp", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(root / "known-hosts"), "-i", root / "client-key", path, "fixture@" + node["address"] + ":" + destination], seconds=180)
            def upload_server_artifact(node, path, digest):
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Require a hashed disposable server artifact")
                destination = "/var/tmp/fixture-image-" + digest + ".tar"
                run(["scp", "-q", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                     "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(root / "known-hosts"),
                     "-i", root / "client-key", path, "fixture@" + node["address"] + ":" + destination], seconds=240)
                return destination
            stage = "guest-boot"
            for node in all_guests:
                wait(lambda node=node: ssh(node, "test -f /run/fixture-artifacts-ready", check=False, seconds=8).returncode == 0, seconds=240)
                test.assertEqual(ssh(node, "hostname").stdout.strip(), node["name"])
                value = qmp(node["directory"] / "qmp.sock", "query-kvm")
                test.assertIs(value["enabled"], True); test.assertIs(value["present"], True)
            if lab_time:
                stage = "guest-clock-synchronization"
                lab_time.start_clients(all_guests, ssh, wait, guest_runtime_seconds)
            if server_fixture is not None:
                stage = "server-fixture"
                if server_fixture_page_reporting:
                    for node in all_guests:
                        wait(lambda node=node: ssh(
                            node, "test -n \"$(find /sys/bus/virtio/drivers/virtio_balloon"
                            " -maxdepth 1 -name 'virtio[0-9]*' -print -quit)\"",
                            check=False, seconds=10).returncode == 0, seconds=35)
                    report["guest_free_page_reporting_devices_bound"] = True
                native_edge_context = {"nodes": edge_nodes, "inputs": edge_inputs, "public": public / "native-edge"}
                report.update(server_fixture({"root": root, "nodes": nodes, "ssh": ssh, "upload": upload_server_artifact,
                                              "run": run, "wait": wait, "kubectl": local_bin / "kubectl",
                                              "sensitive": sensitive, "native_edge_hosts": native_edge_context}))
                test.assertEqual(kernel_state(), host_kernel)
                test.assertEqual(json.loads(run(["/usr/sbin/ip", "-j", "route", "show", "default"]).stdout), [])
                report["isolated_lab_has_no_upstream_route"] = True
            else:
                token = secrets.token_urlsafe(48); sensitive.append(token)
                ssh(nodes[0], "sudo install -m 600 /dev/null /etc/fixture/server-token && sudo tee /etc/fixture/server-token >/dev/null", payload=token + "\n")
                stage = "control-api"
                api_endpoint = "https://192.0.2.2:6443"
                if quorum:
                    import test_k3s_quorum_fixture as ha
                    proxy = ha.start_proxy(root, controls, processes, write, run, clean)
                    api_endpoint = ha.API
                    ha.prepare_control(ssh, nodes[0])
                    ssh(nodes[0], ha.server_command(nodes[0], first=True, external_cni=network_bootstrap is not None, runtime_seconds=guest_runtime_seconds))
                else:
                    policy_flag = "" if flannel_network_policy else " --disable-network-policy"
                    ssh(nodes[0], "sudo systemd-run --quiet --unit=fixture-k3s --property=Type=exec --property=RuntimeMaxSec=" + str(guest_runtime_seconds) + " /mnt/fixtures/k3s server --data-dir=/var/lib/fixture-k3s --disable-agent --node-name=fixture-control --bind-address=192.0.2.2 --advertise-address=192.0.2.2 --write-kubeconfig=/etc/fixture/admin.yaml --write-kubeconfig-mode=600 --token-file=/etc/fixture/server-token --egress-selector-mode=cluster --flannel-backend=host-gw --cluster-cidr=198.18.0.0/16 --service-cidr=198.19.0.0/16 --cluster-dns=198.19.0.10 --disable=traefik,local-storage,servicelb --disable-helm-controller --secrets-encryption" + policy_flag)
                def service_healthy(node):
                    if ssh(node, "sudo systemctl is-failed --quiet fixture-k3s", check=False).returncode == 0: raise AssertionError("The original guest K3s service failed")
                def api_config_ready():
                    service_healthy(nodes[0])
                    return ssh(nodes[0], "sudo test -s /etc/fixture/admin.yaml", check=False).returncode == 0
                wait(api_config_ready)
                config = yaml.safe_load(ssh(nodes[0], "sudo cat /etc/fixture/admin.yaml").stdout); config["clusters"][0]["cluster"]["server"] = api_endpoint
                kubeconfig = write("kubeconfig.json", json.dumps(config))
                target = {"kubeconfig_env": "DISPOSABLE_K3S_VM_KUBECONFIG", "context": config["current-context"], "cluster_uid": ""}
                with mock.patch.dict(os.environ, {target["kubeconfig_env"]: str(kubeconfig), "PATH": str(local_bin) + os.pathsep + clean["PATH"],
                        "HOME": str(root), "XDG_CACHE_HOME": str(root / "cache")}):
                    prefix = [local_bin / "kubectl", "--kubeconfig", kubeconfig, "--context", config["current-context"], "--request-timeout=10s"]
                    wait(lambda: "ok" in run([*prefix, "get", "--raw=/readyz"], seconds=15, check=False).stdout)
                    identity = json.loads(run([*prefix, "get", "namespace/kube-system", "-o", "json"], seconds=15).stdout)
                    target["cluster_uid"] = identity["metadata"]["uid"]
                    manager = Client(target)
                    verify_target(manager, target)
                    secure_token = ssh(nodes[0], "sudo cat /var/lib/fixture-k3s/server/node-token").stdout.strip(); sensitive.append(secure_token)
                    if not re.fullmatch(r"K10[0-9a-f]{64}::server:[A-Za-z0-9_-]+", secure_token): raise ValueError("Require the original CA-bound fixture join token")
                    if quorum:
                        stage = "embedded-etcd-peer-join"
                        ha.join_controls(manager, controls, ssh, wait, secure_token, external_cni=network_bootstrap is not None, runtime_seconds=guest_runtime_seconds)
                    stage = "worker-join"
                    for index, node in enumerate(workers, 1):
                        ssh(node, "sudo install -m 600 /dev/null /etc/fixture/join-token && sudo tee /etc/fixture/join-token >/dev/null", payload=secure_token + "\n")
                        ssh(node, "sudo mkdir -p /var/lib/fixture-k3s/agent/images && sudo cp /mnt/fixtures/k3s-airgap-images-amd64.tar.zst /var/lib/fixture-k3s/agent/images/", seconds=60)
                        ssh(node, "sudo systemd-run --quiet --unit=fixture-k3s --property=Type=exec --property=RuntimeMaxSec=" + str(guest_runtime_seconds) + " /mnt/fixtures/k3s agent --data-dir=/var/lib/fixture-k3s --server=" + api_endpoint + " --token-file=/etc/fixture/join-token --node-name=" + node["name"] + " --node-ip=" + node["address"] + " --flannel-iface=eth0 --node-label=platform.gitops/ci-worker=true --node-label=topology.kubernetes.io/zone=fixture-zone-" + str(index) + (" --node-label=platform.gitops/edge=true" if network_bootstrap is not None else ""))
                    if admission_workers:
                        report["lab_original_vm_pids"] = {node["name"]: node["qemu_pid"] for node in all_guests}
                        report["lab_guest_memory_mib"] = {node["name"]: node["memory"] for node in all_guests}
                        report["three_dedicated_admission_workers_verified"] = True
                    context = {"root": root, "client": manager, "target": target, "nodes": [nodes[0], *workers], "control_nodes": controls, "admission_workers": admission_workers, "guest_runtime_limit_seconds": guest_runtime_seconds, "ssh": ssh, "upload": upload, "run": run, "wait": wait}
                    if zuul_rollout: context["zuul_rollout"] = True
                    if lab_time: context["lab_time"] = lab_time
                    if edge_inputs is not None:
                        context["native_edge_hosts"] = {"nodes": edge_nodes, "inputs": edge_inputs, "public": public / "native-edge"}
                        report["native_edge_parent_memory_profile"] = resource_profile
                        report["lab_original_vm_pids"] = {node["name"]: node["qemu_pid"] for node in all_guests}
                        report["lab_guest_memory_mib"] = {node["name"]: node["memory"] for node in all_guests}
                    if network_bootstrap is not None:
                        stage = "external-cni-bootstrap"
                        wait(lambda: len(json.loads(manager.call(["get", "nodes", "-o", "json"]))["items"]) == len(nodes), seconds=240)
                        report.update(network_bootstrap(context))
                    def ready_workers():
                        for node in nodes: service_healthy(node)
                        rows = json.loads(manager.call(["get", "nodes", "-o", "json"]))["items"]
                        return len(rows) == (len(nodes) if quorum else len(workers)) and all(any(c.get("type") == "Ready" and c.get("status") == "True" for c in row.get("status", {}).get("conditions", [])) for row in rows)
                    wait(ready_workers, seconds=240)
                    stage = "worker-airgap-import"
                    for node in workers:
                        def cached_images_ready(node=node):
                            service_healthy(node)
                            result = ssh(node, "sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images list --quiet", check=False)
                            return result.returncode == 0 and {"docker.io/rancher/mirrored-library-busybox:1.37.0", "docker.io/rancher/mirrored-pause:3.6"} <= set(result.stdout.splitlines())
                        wait(cached_images_ready, seconds=180)
                        ssh(node, "sudo /mnt/fixtures/k3s ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images tag docker.io/rancher/mirrored-library-busybox:1.37.0 " + PROBE_IMAGE)
                    stage = "restricted-pod-execution"
                    report.update(worker_probe(test, context))
                    if quorum:
                        stage = "three-voter-quorum-and-api-outage"
                        report.update(ha.exercise(test, context, controls, proxy))
                    if workload is not None:
                        stage = "application-workload"
                        report.update(workload(context))
                    if quorum:
                        stage = "final-control-service-health"
                        report["final_control_services"] = ha.stable_controls(context, controls)
                        report["final_control_services_ready_verified"] = True
                    if lab_time:
                        stage = "final-guest-clock-health"
                        lab_time.synchronize(all_guests, ssh, wait)
                        report.update(lab_time.report())
                    memory = memory_snapshot()
                    if memory["events"].get("oom_kill", 0): raise ValueError("A process was OOM-killed in the original test cgroup")
                    report["parent_cgroup_memory"] = memory
                test.assertEqual(kernel_state(), host_kernel)
                test.assertEqual(json.loads(run(["/usr/sbin/ip", "-j", "route", "show", "default"]).stdout), [])
                report.update(k3s_version="v1.35.0+k3s1", guest_runtime_limit_seconds=guest_runtime_seconds, k3s_artifacts_sha256=ARTIFACTS, guest_image_sha512=IMAGE_SHA512, qemu_packages_sha256=PACKAGES_SHA,
                    disposable_disk_workspace_filesystem=filesystem, disk_backed_vm_storage_verified=True,
                    kvm_guest_isolation_verified=True, real_worker_pod_execution_verified=True, host_kubelet_kernel_tunables_unchanged=True, isolated_lab_has_no_upstream_route=True,
                    guest_cpu_model=guest_cpu_model, guest_machine="pc,smm=off", tekton_execution_verified=report.get("real_tekton_infrastructure_canary_verified") is True,
                    application_ci_verified=False, physical_node_ha_verified=False, cilium_integration_verified=False, full_stack_verified=False)
        except Exception:
            print(json.dumps({"parent_cgroup_memory_at_failure": memory_snapshot()}), flush=True)
            if quorum:
                print(json.dumps({"quorum_child_exit_codes": [p.poll() for p, _ in processes],
                    "api_proxy_startup_diagnostics": (root / "api-proxy.log").read_text(errors="replace")[-3500:].replace(str(root), "<private-scope>") if (root / "api-proxy.log").exists() else "not-started"}), flush=True)
            text = "\n".join(p.read_text(errors="replace")[-16000:] for p in root.glob("*/qemu.log"))
            print(json.dumps({"k3s_vm_failure_stage": stage, "qemu_signals": {s: s in text.lower() for s in ("permission denied", "not found", "kvm", "failed", "invalid", "could not")}}, sort_keys=True), flush=True)
            print(json.dumps({"guest_boot_signals": {p.parent.name: {"boot_attempts": p.read_text(errors="replace").count("Booting"), "cloud_init_observed": "cloud-init" in p.read_text(errors="replace").lower()} for p in root.glob("*/serial.log")},
                "guest_cpu_signals": {p.parent.name: {s: s in p.read_text(errors="replace").lower() for s in ("triple fault", "invalid", "unhandled")} for p in root.glob("*/cpu.log")}}, sort_keys=True), flush=True)
            monitors = {}
            for path in root.glob("*/qmp.sock"):
                try: monitors[path.parent.name] = qmp(path, "query-status")
                except (OSError, ValueError): monitors[path.parent.name] = {"monitor_unavailable": True}
            print(json.dumps({"guest_monitor_state": monitors}, sort_keys=True), flush=True)
            diagnostics = {}
            for path in root.glob("*/qemu.log"):
                value = path.read_text(errors="replace")[-3500:]
                value = value.replace(str(root), "<private-scope>")
                diagnostics[path.parent.name] = re.sub(r"[A-Za-z0-9_+/=-]{32,}", "<redacted>", value)
            print(json.dumps({"qemu_startup_diagnostics": diagnostics}), flush=True)
            if stage not in {"preparation", "private-network", "guest-boot"}:
                print(json.dumps({"guest_k3s_diagnostics": guest_failure_journals(nodes,ssh,sensitive)}), flush=True)
            raise
        finally:
            for process, log in reversed(processes):
                if process.poll() is None:
                    if os.getpgid(process.pid) != process.pid: raise ValueError("Original VM process group changed")
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=15)
                    except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
                log.close()
            if zuul_rollout:
                for tap in reversed(guest_taps):
                    run(["/usr/sbin/ip", "link", "delete", tap])
            test.assertEqual(kernel_state(), host_kernel)
    test.assertFalse(root.exists())
    current = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repository / "scripts").glob("*.py")}
    if current != sources: raise ValueError("Source files changed during VM acceptance")
    report.update(source_files_sha256=sources, temporary_guests_and_scope_removed=True)
    output = os.environ.get("K3S_VM_TEST_REPORT")
    if output:
        path = Path(output).resolve(); path.relative_to((repository / "private").resolve()); path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream: json.dump(report, stream, sort_keys=True)
    return report


def worker_probe(test, context):
    client = context["client"]
    def create(obj): return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(obj).encode()))
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "worker-probe", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
    create({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "probe", "namespace": "worker-probe"}, "automountServiceAccountToken": False})
    for node in context["nodes"][1:]:
        pod = create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": node["name"], "namespace": "worker-probe"}, "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "serviceAccountName": "probe",
            "nodeSelector": {"kubernetes.io/hostname": node["name"]}, "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "probe", "image": PROBE_IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["/bin/sh", "-ec", PROBE],
                "env": [{"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}, {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}, "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "64Mi"}}}]}})
        name = "pod/" + node["name"]; original = pod["metadata"]["uid"]
        def finished(name=name,original=original):
            observed = client.get(name, "worker-probe")
            if observed["metadata"]["uid"] != original: raise AssertionError("Original worker probe was replaced")
            if observed.get("status", {}).get("phase") == "Failed": raise AssertionError("Real worker probe failed")
            return observed if observed.get("status", {}).get("phase") == "Succeeded" else None
        observed = context["wait"](finished, seconds=120)
        test.assertEqual(observed["spec"]["nodeName"], node["name"])
        status = observed["status"]["containerStatuses"]; test.assertEqual(len(status), 1)
        test.assertEqual(status[0]["state"]["terminated"]["exitCode"], 0)
        test.assertRegex(status[0]["containerID"], r"^containerd://[0-9a-f]{64}$")
        test.assertTrue(any(v in status[0]["imageID"] for v in (PROBE_INDEX, PROBE_MANIFEST)))
        result = json.loads(client.call(["logs", name, "-n", "worker-probe"]))
        test.assertEqual(result, {"schema_version": 1, "pod_uid": original, "node": node["name"], "kernel_security_verified": True})
        worker = client.get("node/" + node["name"]); labels = worker["metadata"]["labels"]
        test.assertEqual(labels["platform.gitops/ci-worker"], "true")
        test.assertFalse(any(k in labels for k in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")))
        test.assertEqual(worker["status"]["nodeInfo"]["kubeletVersion"], "v1.35.0+k3s1")
    return {"dedicated_worker_nodes_observed": len(context["nodes"]) - 1, "restricted_probe_pods_completed": len(context["nodes"]) - 1, "actual_nonroot_readonly_seccomp_capability_and_memory_checks_passed": True}


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--qemu-child": child()
