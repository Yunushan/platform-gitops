#!/usr/bin/env python3
"""Explicit disposable rootful Docker engine and kernel-isolation acceptance.

Uses a private Unix socket, no bridge/firewall writes or borrowed images. This
is an engine prerequisite check, not Keycloak or full-stack acceptance.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from uuid import uuid4

import activate_keycloak_host as activation
from bounded_subprocess import run_bounded


ARCHIVE = "docker-29.9.3.tgz"
SHA256 = "3fb5d1f13014c18cc54945bc177c58a9056a482dada031e17d12cf2e6922c973"
BINARIES = {"ctr", "dockerd", "docker-init", "docker", "containerd", "docker-proxy", "runc", "containerd-shim-runc-v2"}
PROBE = r'''#include <stdio.h>
#include <unistd.h>
#include <sys/prctl.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <linux/capability.h>
int main(void) {
  struct statvfs fs;
  struct __user_cap_header_struct h = {_LINUX_CAPABILITY_VERSION_3, 0};
  struct __user_cap_data_struct d[2] = {{0}, {0}};
  if (statvfs("/", &fs) || syscall(SYS_capget, &h, d)) return 2;
  printf("{\"uid\":%u,\"gid\":%u,\"no_new_privileges\":%d,\"seccomp_mode\":%d,\"effective_capabilities\":%u,\"readonly_root\":%s}\n",
    getuid(), getgid(), prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0), prctl(PR_GET_SECCOMP,0,0,0,0),
    d[0].effective | d[1].effective, fs.f_flag & ST_RDONLY ? "true" : "false");
  fflush(stdout); sleep(20); return 0;
}
'''


def exercise(test, *, workload=None):
    if workload is not None and not callable(workload): raise ValueError("Select one explicit disposable workload callback")
    if sys.platform != "linux" or os.geteuid() != 0 or os.uname().machine != "x86_64" or not Path("/run/systemd/system").is_dir():
        raise ValueError("Use an explicit disposable root Linux amd64 systemd runner")
    archive = Path(os.environ["DOCKER_ENGINE_TEST_ARTIFACTS"]).resolve(strict=True) / ARCHIVE
    if archive.is_symlink() or archive.stat().st_size > 128 * 1024 ** 2: raise ValueError("Use the reviewed bounded engine archive")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256: raise ValueError("Engine artifact fingerprint differs")
    cc = Path(shutil.which("cc") or "/missing-compiler").resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix="platform-docker-engine-", dir="/run")); root.chmod(0o700)
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    storage = Path(tempfile.mkdtemp(prefix="platform-docker-data-", dir="/var/lib")); storage.chmod(0o700)
    storage_identity = (storage.stat().st_dev, storage.stat().st_ino)
    unit = "platform-docker-engine-fixture-" + uuid4().hex[:12] + ".service"
    fixture_id = uuid4().hex; container = None; original_invocation = None; engine_id = None
    sources = activation.tools(); backend = activation.Host({"binary": str(root / "docker/docker"), "socket": str(root / "docker.sock")})
    source_paths = [Path(__file__), Path(__file__).with_name("test_keycloak_docker_runtime.py")]
    fixture_sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}; stage = "preparation"
    report = {"docker_engine_version": "29.9.3", "docker_engine_archive_sha256": SHA256,
        "docker_engine_transport_verified": False, "docker_kernel_isolation_verified": False,
        "docker_engine_cleanup_verified": False, "docker_socket_alias_refusal_verified": False, "docker_resource_controllers_verified": False, "publisher_signature_verified": False,
        "keycloak_runtime_verified": False, "ha_verified": False, "full_stack_verified": False}

    def run(arguments, *, seconds=45):
        result = run_bounded(list(map(str, arguments)), input="", text=True, env=activation.CLEAN, timeout=seconds, output_max_bytes=2 * 1024 ** 2, check=False)
        if result.returncode: raise ValueError("Disposable engine command failed; private details retained only by the test runner")
        return result.stdout

    def properties():
        raw = run(["/usr/bin/systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,MainPID,InvocationID,ControlGroup"])
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def engine():
        info = json.loads(backend.docker("info", "--format", "{{json .}}"))
        if engine_id is not None and info.get("ID") != engine_id: raise ValueError("Original fixture engine was replaced")
        return info

    def owned_container():
        row = json.loads(backend.docker("container", "inspect", container))[0]
        if row.get("Id") != container or row.get("Config", {}).get("Labels", {}).get("platform.fixture") != fixture_id: raise ValueError("Original fixture container changed")
        return row

    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if len(members) != len(BINARIES) + 1 or {m.name for m in members} != {"docker", *("docker/" + n for n in BINARIES)}:
                raise ValueError("The reviewed engine archive inventory changed")
            if any(m.name != "docker" and (not m.isfile() or not 1 <= m.size <= 128 * 1024 ** 2) for m in members): raise ValueError("Only regular engine binaries may be staged")
            tar.extractall(root, filter="data")
        for path in (root / "docker").iterdir(): path.chmod(0o755); os.chown(path, 0, 0)
        (root / "docker").chmod(0o755)
        report["docker_tools_sha256"] = {p.name: activation.file_hash(p) for p in (root / "docker").iterdir()}
        config = {"hosts": ["unix://" + str(root / "docker.sock")], "data-root": str(storage), "exec-root": str(root / "exec"), "pidfile": str(root / "engine.pid"),
            "bridge": "none", "iptables": False, "ip6tables": False, "ip-forward": False, "ip-masq": False, "storage-driver": "vfs",
            "features": {"containerd-snapshotter": False}, "exec-opts": ["native.cgroupdriver=cgroupfs"],
            "cgroup-parent": "/system.slice/" + unit + "/containers", "group": "0", "shutdown-timeout": 20,
            "log-driver": "json-file", "log-opts": {"max-size": "1m", "max-file": "1"}}
        (root / "daemon.json").write_text(json.dumps(config)); (root / "daemon.json").chmod(0o600)
        launcher = root / "engine-launcher.py"
        launcher.write_text('''import os,sys
from pathlib import Path
expected=sys.argv[1]
rows=[line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")]
if rows != [expected+"/supervisor"]: raise SystemExit("Unexpected delegated supervisor")
group=Path("/sys/fs/cgroup")/expected.lstrip("/")
if (group/"cgroup.procs").read_text().strip(): raise SystemExit("Delegation root has internal processes")
controllers={"cpu","memory","pids"}
if not controllers <= set((group/"cgroup.controllers").read_text().split()): raise SystemExit("Required resource controllers unavailable")
(group/"cgroup.subtree_control").write_text("+cpu +memory +pids")
if not (group/"supervisor/memory.swap.max").is_file(): raise SystemExit("Delegated swap controller unavailable")
os.execv(sys.argv[2],sys.argv[2:])
''', encoding="utf-8"); launcher.chmod(0o600)
        # A finite delegated cgroup contains the daemon AND its containers.
        # The test never enables a boot service or uses the default engine.
        stage = "engine-startup"
        run(["/usr/bin/systemd-run", "--quiet", "--unit=" + unit, "--property=Type=exec", "--property=RuntimeMaxSec=600", "--property=Restart=no", "--property=Delegate=yes", "--property=DelegateSubgroup=supervisor",
             "--property=KillMode=mixed", "--property=TimeoutStopSec=40", "--property=MemoryMax=4G", "--property=TasksMax=4096", "--property=UMask=0077", "--property=PrivateMounts=yes",
             "--setenv=PATH=" + str(root / "docker") + ":/usr/sbin:/usr/bin:/sbin:/bin", "--setenv=HOME=/nonexistent", "--setenv=LC_ALL=C",
             "/usr/bin/python3", "-B", launcher, "/system.slice/" + unit, root / "docker/dockerd", "--config-file=" + str(root / "daemon.json")])
        deadline = time.monotonic() + 60
        while True:
            props = properties()
            if props.get("ActiveState") == "failed": raise ValueError("The disposable engine service failed")
            if props.get("ActiveState") == "active" and props.get("SubState") == "running":
                if not original_invocation: original_invocation = props["InvocationID"]
                elif props.get("InvocationID") != original_invocation: raise ValueError("The original engine invocation changed")
                try: info = engine(); break
                except (ValueError, FileNotFoundError): pass
            if time.monotonic() >= deadline: raise TimeoutError("Disposable engine startup deadline")
            time.sleep(0.25)
        engine_id = info["ID"]
        production_facts = backend.docker_facts()
        test.assertEqual(production_facts["id"], engine_id); test.assertEqual(production_facts["cgroup"], "2")
        for key in ("memory", "swap", "cpu_period", "cpu_quota", "pids"): test.assertIs(production_facts[key], True)
        if (info.get("OSType") != "linux" or info.get("ServerVersion") != "29.9.3" or info.get("DefaultRuntime") != "runc" or info.get("DockerRootDir") != str(storage)
            or info.get("Driver") != "vfs" or info.get("CgroupDriver") != "cgroupfs" or not any("name=seccomp" in v and "profile=builtin" in v for v in info.get("SecurityOptions", []))
            or info.get("CgroupVersion") != "2" or any(info.get(k) is not True for k in ("MemoryLimit", "SwapLimit", "CpuCfsPeriod", "CpuCfsQuota", "PidsLimit"))
            or any("rootless" in v or "userns" in v for v in info.get("SecurityOptions", []))): raise ValueError("Unexpected disposable engine policy")
        protected, socket_info = activation.protected_socket(root / "docker.sock")
        test.assertEqual(protected, root / "docker.sock")
        conventional, alias_info = activation.protected_socket(Path("/var/run") / root.name / "docker.sock")
        test.assertEqual(conventional, protected); test.assertEqual((alias_info.st_dev, alias_info.st_ino), (socket_info.st_dev, socket_info.st_ino))
        (root / "socket-link").symlink_to(root / "docker.sock"); (root / "parent-link").symlink_to(root, target_is_directory=True)
        untrusted = root / "untrusted"; untrusted.mkdir(); untrusted.chmod(0o777); (untrusted / "socket-link").symlink_to(root / "docker.sock")
        for path in (root / "socket-link", root / "parent-link/docker.sock", untrusted / "socket-link"):
            with test.subTest(alias=path.name), test.assertRaises(ValueError): activation.protected_socket(path)
        try:
            (root / "docker.sock").chmod(0o666)
            with test.assertRaises(ValueError): activation.protected_socket(root / "docker.sock")
        finally: (root / "docker.sock").chmod(stat.S_IMODE(socket_info.st_mode))
        report["docker_socket_alias_refusal_verified"] = True
        report["docker_engine_transport_verified"] = True
        stage = "probe-compilation"
        report["compiler_sha256"] = activation.file_hash(cc)
        (root / "probe.c").write_text(PROBE)
        run([cc, "-static", "-O2", "-o", root / "probe", root / "probe.c"])
        report["probe_source_sha256"] = hashlib.sha256(PROBE.encode()).hexdigest()
        report["probe_binary_sha256"] = activation.file_hash(root / "probe")
        with tarfile.open(root / "rootfs.tar", "w") as tar:
            data = (root / "probe").read_bytes(); member = tarfile.TarInfo("probe"); member.size = len(data); member.mode = 0o755; tar.addfile(member, io.BytesIO(data))
        stage = "image-import"
        image = backend.docker("image", "import", str(root / "rootfs.tar")).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image): raise ValueError("Image import did not return an immutable image ID")
        stage = "container-create"
        container = backend.docker("container", "create", "--name=probe-" + fixture_id, "--label=platform.fixture=" + fixture_id, "--pull=never", "--user=1000:1000", "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=64m", "--memory-swap=64m", "--cpus=0.5", "--pids-limit=32", "--ipc=none", "--cgroupns=private",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=1m", "--entrypoint=/probe", image).strip()
        if not activation.container_id(container): raise ValueError("Container creation did not return its original ID")
        stage = "container-start"
        owned_container(); backend.docker("container", "start", container)
        stage = "kernel-observation"
        row = owned_container(); pid = row.get("State", {}).get("Pid")
        if row.get("State", {}).get("Running") is not True or not activation.process_cgroup(pid).startswith("/system.slice/" + unit + "/containers/" + container): raise ValueError("The running probe escaped its original delegated cgroup")
        cgroup = Path("/sys/fs/cgroup") / activation.process_cgroup(pid).lstrip("/")
        test.assertEqual((cgroup / "memory.max").read_text().strip(), str(64 * 1024 ** 2))
        test.assertEqual((cgroup / "memory.swap.max").read_text().strip(), "0")
        test.assertEqual((cgroup / "pids.max").read_text().strip(), "32")
        quota, period = map(int, (cgroup / "cpu.max").read_text().split()); test.assertEqual(2 * quota, period)
        report["docker_resource_controllers_verified"] = True
        deadline = time.monotonic() + 10
        while True:
            output = backend.docker("container", "logs", container).strip()
            if output: break
            if time.monotonic() >= deadline: raise TimeoutError("Container probe did not report its kernel state")
            time.sleep(0.1)
        test.assertEqual(json.loads(output), {"uid": 1000, "gid": 1000, "no_new_privileges": 1, "seccomp_mode": 2, "effective_capabilities": 0, "readonly_root": True})
        engine(); test.assertEqual(properties()["InvocationID"], original_invocation)
        if workload is not None:
            stage = "prepared-workload"
            report["workload"] = workload({"root": root, "storage": storage, "backend": backend, "engine_id": engine_id, "engine_info": info, "run": run})
            if not isinstance(report["workload"], dict): raise ValueError("Workload callback must return aggregate evidence")
            engine(); test.assertEqual(properties()["InvocationID"], original_invocation)
        if activation.tools() != sources or {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths} != fixture_sources: raise ValueError("First-party activation tools changed during engine acceptance")
        report["docker_kernel_isolation_verified"] = True; report["host_toolset_sha256"] = sources; report["fixture_sources_sha256"] = fixture_sources
    except BaseException as error:
        logs = run(["/usr/bin/journalctl", "-u", unit, "--no-pager", "-o", "cat", "--lines=500"]).casefold()
        print(json.dumps({"docker_fixture_safe_failure": {"stage": stage, "exception_type": type(error).__name__, "signals": {
            "permission_denied": "permission denied" in logs or "operation not permitted" in logs,
            "apparmor": "apparmor" in logs, "cgroup": "cgroup" in logs, "mount_failure": "failed to mount" in logs,
            "unknown_option": "unknown flag" in logs or "unrecognized" in logs, "out_of_memory": "out of memory" in logs}}}), flush=True)
        raise
    finally:
        props = properties()
        if original_invocation and props.get("InvocationID") != original_invocation: raise ValueError("Preserve a replaced engine's handles instead of cleaning it")
        if container and props.get("ActiveState") == "active":
            engine(); owned_container(); backend.docker("container", "rm", "--force", container)
        if props.get("LoadState") != "not-found":
            run(["/usr/bin/systemctl", "stop", unit])
            if properties().get("LoadState") != "not-found": run(["/usr/bin/systemctl", "reset-failed", unit])
        if properties().get("ActiveState") in {"active", "activating", "deactivating"}: raise ValueError("Preserve a still-live disposable engine")
        for process in Path("/proc").glob("[0-9]*"):
            for part in ("exe", "cwd"):
                try: target = os.readlink(process / part)
                except OSError: continue
                if target.startswith((str(root) + "/", str(storage) + "/")): raise ValueError("Preserve remaining original engine processes")
        if any(line.split()[4] in {str(root), str(storage)} or line.split()[4].startswith((str(root) + "/", str(storage) + "/")) for line in Path("/proc/self/mountinfo").read_text().splitlines()): raise ValueError("Preserve remaining original engine mounts")
        if root.is_symlink() or (root.stat().st_dev, root.stat().st_ino) != root_identity or root.parent != Path("/run") or not root.name.startswith("platform-docker-engine-"): raise ValueError("Fixture cleanup scope changed")
        if storage.is_symlink() or (storage.stat().st_dev, storage.stat().st_ino) != storage_identity or storage.parent != Path("/var/lib") or not storage.name.startswith("platform-docker-data-"): raise ValueError("Fixture storage cleanup scope changed")
        shutil.rmtree(storage)
        shutil.rmtree(root)
    report["docker_engine_cleanup_verified"] = True
    return report
