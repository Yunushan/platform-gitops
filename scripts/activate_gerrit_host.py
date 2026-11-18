#!/usr/bin/env python3
"""Activate one prepared native/Docker Gerrit member on its explicitly pinned host."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time
from uuid import uuid4

from atomic_file import atomic_write_text, _fsync_directory
from bounded_file import read_bounded_bytes
import activate_keycloak_host as host_tools
import bootstrap_keycloak_realm as source_tools
import gerrit_storage as storage
import render_gerrit_sites as sites
from render_gerrit_router import dns_name
import render_ceph_storage as values
from provision_ceph_storage import lock
from http_transport import read_bounded_response
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
LABEL = "platform.gitops/gerrit-"
PRIVATE_FILES = ("etc/secure.config", "etc/keystore", "etc/ssh_host_ed25519_key")
UNVERIFIED = {k: False for k in ("peer_forwarding_verified", "git_writes_verified", "failover_verified", "ha_verified", "full_stack_verified")}


def tools(): return source_tools.sources(Path(__file__).stem)
def name(plan): return "platform-" + plan["member"]
def selected(plan): return next(s for s in plan["site_plan"]["sites"] if s["name"] == plan["member"])
def labels(state): return {LABEL + k: state[v] for k, v in (("operation", "operation_id"), ("plan", "plan_sha256"), ("tools", "toolset_sha256"))}


def validate(plan):
    fields = {"schema_version", "runtime", "site_plan", "member", "machine_id_sha256", "boot_id", "service_uid", "service_gid", "private_files_sha256", "health", "docker", "native"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Use the finite Gerrit host activation plan")
    sites.validate(plan["site_plan"])
    if plan["site_plan"]["schema_version"] != 2 or plan["runtime"] not in {"raw", "docker"} or plan["member"] not in {s["name"] for s in plan["site_plan"]["sites"]}:
        raise ValueError("Select one guarded Docker/native primary")
    if not values.sha(plan["machine_id_sha256"]) or not values.uid(plan["boot_id"]): raise ValueError("Pin the original machine and boot")
    if any(not values.integer(plan[k], 1, 2 ** 31 - 2) for k in ("service_uid", "service_gid")): raise ValueError("Use a dedicated non-root service identity")
    if not sites.hashes(plan["private_files_sha256"], PRIVATE_FILES): raise ValueError("Pin the three prepared private files without publishing them")
    health = plan["health"]
    if not isinstance(health, dict) or set(health) != {"address", "hostname", "ca_file", "ca_sha256"}: raise ValueError("Pin the private health trust inputs")
    ip = ipaddress.IPv4Address(health["address"])
    if ip.is_unspecified or ip.is_multicast or str(ip) != health["address"] or not dns_name(health["hostname"]): raise ValueError("Select a direct member address and TLS DNS identity")
    if not host_tools.local_runtime_path(health["ca_file"]) or not values.sha(health["ca_sha256"]): raise ValueError("Pin the health trust file")
    if plan["runtime"] == "docker":
        spec = plan["docker"]
        if plan["native"] is not None or plan["service_uid"] != 1000 or plan["service_gid"] != 1000: raise ValueError("Docker requires the packaged UID/GID 1000")
        if (not isinstance(spec, dict) or set(spec) != {"binary", "socket", "binary_sha256", "daemon_id", "server_version", "image_id"}
                or not all(host_tools.local_runtime_path(spec[k]) for k in ("binary", "socket")) or spec["binary"] == spec["socket"]
                or not values.sha(spec["binary_sha256"]) or not isinstance(spec["daemon_id"], str) or not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", spec["daemon_id"])
                or not re.fullmatch(r"[2-9][0-9]\.[0-9]+\.[0-9]+", str(spec["server_version"])) or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(spec["image_id"]))):
            raise ValueError("Pin the local rootful Docker engine, CLI, socket and preloaded image")
    else:
        spec = plan["native"]
        if (plan["docker"] is not None or not isinstance(spec, dict) or set(spec) != {"user", "group", "java_home", "java_manifest", "java_manifest_sha256", "systemctl_sha256"}
                or any(not isinstance(spec[k], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", spec[k]) or spec[k] == "root" for k in ("user", "group"))
                or not all(host_tools.local_runtime_path(spec[k]) for k in ("java_home", "java_manifest"))
                or not values.sha(spec["java_manifest_sha256"]) or not values.sha(spec["systemctl_sha256"])):
            raise ValueError("Pin the dedicated native account, Java distribution and service manager")
        java = spec["java_home"]
        for directory in (selected(plan)["host_path"], plan["site_plan"]["shared_host_path"]):
            if java == directory or java.startswith(directory + "/") or directory.startswith(java + "/"): raise ValueError("Keep the immutable Java distribution separate from writable data")
    return plan


def package(bundle, plan):
    files = sites.render(plan["site_plan"], plan["runtime"])
    if bundle.is_symlink() or bundle.resolve(strict=True) != bundle.absolute(): raise ValueError("Require a directly staged package")
    if {p.name for p in bundle.iterdir()} != set(files): raise ValueError("The finite staged Gerrit package differs")
    for filename, expected in files.items():
        path = bundle / filename
        if path.is_symlink() or read_bounded_bytes(path, max_bytes=512 * 1024) != expected.encode(): raise ValueError("The reviewed startup package changed")
    return values.fingerprint(files)


def command(plan):
    site_plan = plan["site_plan"]
    return ["/var/gerrit", "/shared", "/opt/gerrit-startup/" + plan["member"] + ".sha256", *sites.guard_arguments(site_plan, "/opt/gerrit-startup")]


def mounts(bundle, plan):
    return [(selected(plan)["host_path"], "/var/gerrit", False), (plan["site_plan"]["shared_host_path"], "/shared", False),
        *[(str(bundle / filename), "/opt/gerrit-startup/" + filename, True) for filename in
            ("start.sh", plan["member"] + ".sha256", *sites.guard_files(plan["site_plan"], "/opt/gerrit-startup"))]]


def create_arguments(bundle, plan, state):
    args = ["container", "create", "--pull=never", "--name", name(plan), "--user=1000:1000", "--network=host", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--memory=3g", "--memory-swap=3g", "--cpus=4", "--pids-limit=512", "--ipc=none", "--cgroupns=private",
        "--userns=host", "--runtime=runc", "--restart=no", "--stop-timeout=120", "--workdir=/var/gerrit", "--hostname=" + plan["member"],
        "--no-healthcheck", "--log-driver=json-file", "--log-opt=max-size=10m", "--log-opt=max-file=3", "--entrypoint=/bin/sh"]
    for key, value in sorted(labels(state).items()): args += ["--label", key + "=" + value]
    for source, target, readonly in mounts(bundle, plan):
        args += ["--mount", "type=bind,source=" + source + ",destination=" + target + (",readonly" if readonly else "") + ",bind-propagation=rprivate"]
    return [*args, "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777,size=1g", plan["site_plan"]["java_image"], "/opt/gerrit-startup/start.sh", *command(plan)]


def unit_text(plan, state):
    body = sites.render(plan["site_plan"], "raw")[plan["member"] + ".service"]
    body = body.replace("User=gerrit\n", "User=" + plan["native"]["user"] + "\n").replace("Group=gerrit\n", "Group=" + plan["native"]["group"] + "\n")
    body = body.replace("Restart=on-failure\n", "Restart=no\n")
    return ("# gerrit-operation=" + state["operation_id"] + "\n# gerrit-plan=" + state["plan_sha256"] + "\n" + body
        + "\n[Service]\nEnvironment=JAVA_HOME=" + plan["native"]["java_home"] + "\nEnvironment=PATH=" + plan["native"]["java_home"] + "/bin:/usr/bin:/bin\nTasksMax=512\n")


def container_policy(row, bundle, plan, state, image):
    config, host = row.get("Config", {}), row.get("HostConfig", {})
    env = host_tools.environment_map(image.get("Config", {}).get("Env") or [])
    if (not host_tools.container_id(row.get("Id")) or row.get("Name") != "/" + name(plan) or row.get("Image") != plan["docker"]["image_id"]
            or config.get("Image") != plan["site_plan"]["java_image"] or config.get("User") != "1000:1000" or config.get("Entrypoint") != ["/bin/sh"]
            or config.get("Cmd") != ["/opt/gerrit-startup/start.sh", *command(plan)]
            or host_tools.environment_map([] if config.get("Env") is None else config["Env"]) != env
            or config.get("Labels") != {**(image.get("Config", {}).get("Labels") or {}), **labels(state)} or config.get("WorkingDir") != "/var/gerrit"
            or config.get("Hostname") != plan["member"] or config.get("StopTimeout") != 120 or config.get("Tty") is not False or config.get("OpenStdin") is not False
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]): raise ValueError("Original Gerrit container command or identity changed")
    expected = {"ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "host", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc",
        "PidsLimit": 512, "Memory": 3 * 1024 ** 3, "MemorySwap": 3 * 1024 ** 3, "NanoCpus": 4000000000, "AutoRemove": False,
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}}
    if (any(host.get(k) != v for k, v in expected.items()) or host.get("OomKillDisable") is not None and host["OomKillDisable"] is not False
            or host.get("CapDrop") != ["ALL"] or host.get("SecurityOpt") not in (["no-new-privileges:true"], ["no-new-privileges"])
            or any(host.get(k) for k in ("CapAdd", "Binds", "VolumesFrom", "Devices", "DeviceRequests", "DeviceCgroupRules", "GroupAdd", "Links", "PortBindings", "PublishAllPorts", "PidMode", "UTSMode", "ExtraHosts", "Sysctls", "Init"))
            or host.get("Tmpfs") != {"/tmp": "rw,nosuid,nodev,noexec,mode=1777,size=1g"}): raise ValueError("Original Gerrit Docker isolation changed")
    wanted = {target: (source, readonly) for source, target, readonly in mounts(bundle, plan)}
    actual = row.get("Mounts", [])
    if (len(actual) != len(wanted) or {m.get("Destination") for m in actual} != set(wanted) or len(host.get("Mounts", [])) != len(wanted)
            or any(m.get("Type") != "bind" or m.get("Source") != wanted[m["Destination"]][0] or m.get("RW") is not (not wanted[m["Destination"]][1])
                or m.get("Propagation") != "rprivate" for m in actual)): raise ValueError("Original Gerrit bind mounts changed")
    normalized = {**host, "OomKillDisable": False}
    return values.fingerprint({"Config": config, "HostConfig": normalized, "Mounts": sorted(actual, key=lambda m: m["Destination"]), "Image": row["Image"]})


def healthy_body(raw):
    if raw.startswith(b")]}'\n"): raw = raw[5:]
    body = loads_strict_json(raw)
    if not isinstance(body, dict) or not set(storage.REQUIRED_HEALTHCHECKS).issubset(body): return False
    checks = {k: v for k, v in body.items() if k not in {"ts", "elapsed"}}
    return bool(checks) and all(isinstance(v, dict) and v.get("result") == "passed" for v in checks.values())


class Host(host_tools.Host):
    def unit_path(self, plan): return Path("/etc/systemd/system") / (name(plan) + ".service")

    def prerequisites(self, bundle, plan):
        if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use an explicit Linux root activation session")
        if not host_tools.local_runtime_path(str(bundle)): raise ValueError("Stage the package at a canonical Linux path without mount-option separators")
        for module in (Path(sys.executable).resolve(strict=True), *Path(__file__).parent.glob("*.py")): host_tools.trusted(module)
        machine = read_bounded_bytes(Path("/etc/machine-id"), max_bytes=128).decode().strip()
        boot = read_bounded_bytes(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).decode().strip()
        if hashlib.sha256(machine.encode()).hexdigest() != plan["machine_id_sha256"] or boot != plan["boot_id"]: raise ValueError("Original Gerrit machine or boot changed")
        host_tools.trusted(bundle, directory=True)
        if plan["runtime"] == "raw" and bundle != Path("/etc/gerrit"): raise ValueError("Stage native startup files at their rendered /etc/gerrit paths")
        for path in bundle.iterdir(): host_tools.trusted(path); host_tools.readable(path, plan)
        if package(bundle, plan) is None: raise ValueError("Missing reviewed package")
        root = Path(selected(plan)["host_path"])
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or root.resolve(strict=True) != root or metadata.st_uid != plan["service_uid"] or metadata.st_mode & 0o022:
            raise ValueError("Prepared local site ownership or path changed")
        sites.verify_site(plan["site_plan"], plan["member"], root)
        for relative in (*sites.ARTIFACTS, *sites.site_locks(plan["site_plan"]), "etc/ssh_host_ed25519_key.pub", *PRIVATE_FILES):
            host_tools.readable(root / relative, plan, boundary=root if plan["runtime"] == "docker" else None)
        for relative, expected in plan["private_files_sha256"].items():
            path = root / relative; info = path.lstat()
            if (path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != plan["service_uid"]
                    or info.st_mode & 0o077 or hashlib.sha256(read_bounded_bytes(path, max_bytes=4 * 1024 ** 2)).hexdigest() != expected): raise ValueError("A prepared private site file changed")
        storage.check(plan["site_plan"]["shared_storage"], plan["site_plan"]["shared_host_path"])
        storage.check_health_configuration(root)
        if plan["runtime"] == "raw": storage.check_configuration(root, plan["site_plan"]["shared_host_path"])
        ca = Path(plan["health"]["ca_file"]); host_tools.trusted(ca)
        raw = read_bounded_bytes(ca, max_bytes=128 * 1024)
        if hashlib.sha256(raw).hexdigest() != plan["health"]["ca_sha256"]: raise ValueError("Health trust changed")
        ssl.create_default_context(cadata=raw.decode("ascii"))
        address = plan["health"]["address"]
        addresses = loads_strict_json(self.run(["/usr/sbin/ip", "-j", "-4", "address", "show"]))
        if sum(a.get("local") == address for row in addresses for a in row.get("addr_info", [])) != 1: raise ValueError("Direct Gerrit health address is not local")
        facts = {"machine_id_sha256": plan["machine_id_sha256"], "boot_id": boot}
        if plan["runtime"] == "docker":
            spec = plan["docker"]
            if (self.binary, self.socket) != host_tools.docker_paths(spec) or host_tools.file_hash(Path(self.binary)) != spec["binary_sha256"]: raise ValueError("The Docker transport changed")
            sock, info = host_tools.protected_socket(self.socket)
            engine = self.docker_facts(); version = loads_strict_json(self.docker("version", "--format", '{{json .Server.Version}}'))
            security = engine.get("security")
            if (engine.get("id") != spec["daemon_id"] or version != spec["server_version"] or engine.get("os") != "linux" or engine.get("runtime") != "runc"
                    or engine.get("cgroup") != "2" or any(engine.get(k) is not True for k in ("memory", "swap", "cpu_period", "cpu_quota", "pids"))
                    or not isinstance(security, list) or any("rootless" in s or "userns" in s for s in security)
                    or not any("name=seccomp" in s and "profile=builtin" in s for s in security)): raise ValueError("Docker engine identity or enforced isolation changed")
            images = loads_strict_json(self.docker("image", "inspect", plan["site_plan"]["java_image"]))
            if (len(images) != 1 or images[0].get("Id") != spec["image_id"] or plan["site_plan"]["java_image"] not in images[0].get("RepoDigests", [])
                    or images[0].get("Os") != "linux" or images[0].get("Architecture") != {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine)
                    or images[0].get("Config", {}).get("Volumes")): raise ValueError("Preload the exact reviewed runtime image without anonymous volumes")
            facts.update(socket_path=str(sock), socket_device=info.st_dev, socket_inode=info.st_ino, image={k: images[0][k] for k in ("Id", "Config")})
        else:
            import pwd
            import grp
            spec = plan["native"]; account = pwd.getpwnam(spec["user"])
            if account.pw_uid != plan["service_uid"] or account.pw_gid != plan["service_gid"] or grp.getgrnam(spec["group"]).gr_gid != plan["service_gid"]: raise ValueError("Dedicated service identity changed")
            host_tools.manifest(Path(spec["java_home"]), Path(spec["java_manifest"]), spec["java_manifest_sha256"], reader=plan)
            java = Path(spec["java_home"]) / "bin/java"; host_tools.trusted(java); host_tools.readable(java, plan, executable=True)
            if host_tools.file_hash(Path("/usr/bin/systemctl")) != spec["systemctl_sha256"]: raise ValueError("Service manager changed")
        return facts

    def existing(self, bundle, plan):
        if plan["runtime"] == "docker":
            ids = self.docker("container", "ls", "--all", "--no-trunc", "--filter", "name=^/" + name(plan) + "$", "--format", '{{.ID}}').split()
            if len(ids) > 1 or any(not host_tools.container_id(i) for i in ids): raise ValueError("Ambiguous existing Gerrit container")
            return ids[0] if ids else None
        if self.unit_path(plan).exists() or self.unit_path(plan).is_symlink() or self.properties(plan).get("LoadState") != "not-found": return "existing-unit"
        return None

    def properties(self, plan):
        raw = self.run(["/usr/bin/systemctl", "show", name(plan) + ".service", "--property=LoadState,FragmentPath,DropInPaths,ActiveState,SubState,MainPID,InvocationID,NRestarts,NeedDaemonReload,ControlGroup"])
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def create(self, bundle, plan, state):
        if plan["runtime"] == "docker":
            if not host_tools.container_id(self.docker(*create_arguments(bundle, plan, state)).strip()): raise ValueError("Unacknowledged Gerrit container")
            return
        path = self.unit_path(plan); host_tools.trusted(path.parent, directory=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o644); stream.write(unit_text(plan, state)); stream.flush(); os.fsync(stream.fileno())
        _fsync_directory(path.parent)

    def inspect(self, bundle, plan, state, facts):
        if plan["runtime"] == "docker":
            rows = loads_strict_json(self.docker("container", "inspect", name(plan)))
            if len(rows) != 1: raise ValueError("Original Gerrit container is missing")
            row = rows[0]; policy = container_policy(row, bundle, plan, state, facts["image"]); live = row.get("State", {})
            if (live.get("Status") not in {"created", "running"} or live.get("Running") is not (live.get("Status") == "running")
                    or live.get("Dead") or live.get("OOMKilled") or live.get("Error") or row.get("RestartCount") != 0): raise ValueError("Original Gerrit container failed or restarted")
            active = live["Running"]; pid = live.get("Pid") if active else None
            invocation = live.get("StartedAt") if active else None
            if active and (not isinstance(invocation, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", invocation)
                    or datetime.fromisoformat(invocation.replace("Z", "+00:00")).year < 2020): raise ValueError("Invalid original Gerrit invocation")
            cgroup = host_tools.process_cgroup(pid) if active else None
            if active and row["Id"] not in cgroup: raise ValueError("Gerrit container escaped its dedicated cgroup")
            return {"handle": row["Id"], "policy_sha256": policy, "active": active, "loaded": True, "invocation": invocation, "pid": pid, "cgroup": cgroup}
        path = self.unit_path(plan); info = host_tools.trusted(path)
        expected = unit_text(plan, state).encode()
        if info.st_mode & 0o777 != 0o644 or read_bounded_bytes(path, max_bytes=64 * 1024) != expected: raise ValueError("Original Gerrit unit changed")
        props = self.properties(plan)
        if (props.get("DropInPaths") or props.get("FragmentPath") not in {"", str(path)} or props.get("ActiveState") in {"failed", "deactivating"}
                or props.get("NRestarts", "0") != "0"): raise ValueError("Original Gerrit unit policy or invocation changed")
        active = props.get("ActiveState") == "active" and props.get("SubState") == "running" and str(props.get("MainPID", "")).isdigit() and int(props["MainPID"]) > 1
        invocation = props.get("InvocationID") or None
        if invocation is not None and not re.fullmatch(r"[0-9a-f]{32}", invocation): raise ValueError("Invalid original unit invocation")
        pid = int(props["MainPID"]) if active else None; cgroup = props.get("ControlGroup") or None
        if active and (not cgroup or not cgroup.endswith("/" + name(plan) + ".service") or host_tools.process_cgroup(pid) != cgroup): raise ValueError("Original Gerrit unit cgroup changed")
        return {"handle": values.fingerprint({"device": info.st_dev, "inode": info.st_ino}), "policy_sha256": hashlib.sha256(expected).hexdigest(),
            "active": active, "loaded": props.get("LoadState") == "loaded" and props.get("NeedDaemonReload") == "no" and props.get("FragmentPath") == str(path),
            "invocation": invocation, "pid": pid, "cgroup": cgroup}

    def start(self, bundle, plan, state):
        if plan["runtime"] == "docker": self.docker("container", "start", state["handle"])
        else: self.run(["/usr/bin/systemctl", "start", "--no-block", name(plan) + ".service"])

    def healthy(self, bundle, plan, original):
        address = plan["health"]["address"]
        if not all(host_tools.owns_listener(original, address, port) for port in (8443, 29418)): return False
        context = ssl.create_default_context(cadata=read_bounded_bytes(Path(plan["health"]["ca_file"]), max_bytes=128 * 1024).decode("ascii"))
        connection = host_tools.DirectHTTPS(address, plan["health"]["hostname"], 8443, context)
        try:
            connection.request("GET", "/config/server/healthcheck~status", headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200 or not healthy_body(read_bounded_response(response, max_bytes=64 * 1024)): return False
            scanner = Path("/usr/bin/ssh-keyscan"); host_tools.trusted(scanner)
            raw_keys = self.run([str(scanner), "-t", "ed25519", "-T", "5", "-p", "29418", address])
            rows = [line.split() for line in raw_keys.splitlines() if not line.startswith("#")]
            expected = read_bounded_bytes(Path(selected(plan)["host_path"]) / "etc/ssh_host_ed25519_key.pub", max_bytes=8192).decode().split()
            return (len(rows) == 1 and len(rows[0]) == 3 and rows[0][0] == "[" + address + "]:29418" and expected[:1] == ["ssh-ed25519"]
                and rows[0][1:3] == expected[:2] and bool(base64.b64decode(expected[1], validate=True)))
        except (OSError, ValueError, http.client.HTTPException): return False
        finally: connection.close()


def operate(bundle, plan, *, mode="local", output=None, host=None, wait_seconds=300):
    validate(plan); bundle = bundle.absolute(); digest = package(bundle, plan); source = tools()
    result = {"local_inputs_verified": True, "host_checked": False, "original_member_active": False, "tls_health_observed": False, "ssh_host_key_observed": False,
        "read_only": True, **UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-host", "activate", "resume"} or not values.integer(wait_seconds, 5, 900): raise ValueError("Select an explicit bounded Gerrit host operation")
    network = host or Host(plan["docker"]); facts = network.prerequisites(bundle, plan)
    if mode == "check-host":
        if network.existing(bundle, plan) is not None: raise ValueError("Existing Gerrit members require their original journal")
        return {**result, "host_checked": True}
    if output is None or output.is_symlink(): raise ValueError("Keep the private original operation directory")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts: raise ValueError("Use private operation storage")
    if mode == "activate": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume requires the original operation directory")
    receipt = directory / "gerrit-host-proof.json"
    with lock(directory):
        if mode == "resume":
            info, parent = receipt.lstat(), directory.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.name == "posix" and
                    (info.st_uid != os.geteuid() or info.st_mode & 0o077 or parent.st_uid != os.geteuid() or parent.st_mode & 0o077)): raise ValueError("Original private journal permissions changed")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or not values.uid(state.get("operation_id"))
                    or state.get("plan_sha256") != values.fingerprint(plan) or state.get("toolset_sha256") != source or state.get("package_sha256") != digest
                    or state.get("facts_sha256") != values.fingerprint(facts) or any(state.get(k) is not False for k in UNVERIFIED)
                    or any(type(state.get(k)) is not bool for k in ("create_intent", "reload_intent", "start_intent"))
                    or state["start_intent"] and not state["create_intent"] or state["reload_intent"] and not state["create_intent"]
                    or plan["runtime"] == "docker" and state["reload_intent"] or plan["runtime"] == "raw" and state["start_intent"] and not state["reload_intent"]
                    or any(state.get(k) is not None and not values.sha(state[k]) for k in ("handle", "policy_sha256"))): raise ValueError("Resume only the original matching Gerrit operation")
        else:
            if network.existing(bundle, plan) is not None: raise ValueError("Never adopt an existing Gerrit member")
            state = {"schema_version": 1, "operation_id": str(uuid4()), "plan_sha256": values.fingerprint(plan), "toolset_sha256": source, "package_sha256": digest,
                "facts_sha256": values.fingerprint(facts), "create_intent": False, "reload_intent": False, "start_intent": False,
                "handle": None, "policy_sha256": None, "invocation": None, **result}
        wrote = False
        def record(phase, **changes):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **changes); atomic_write_text(receipt, values.canonical(state))
        def unchanged():
            if tools() != source or package(bundle, plan) != digest or values.fingerprint(network.prerequisites(bundle, plan)) != state["facts_sha256"]:
                raise ValueError("Host, prepared inputs or source changed")
        def inspect():
            observed = network.inspect(bundle, plan, state, facts)
            if any(state[k] is not None and observed[k] != state[k] for k in ("handle", "policy_sha256", "invocation")): raise ValueError("Original Gerrit member was replaced or restarted")
            state.update(handle=observed["handle"], policy_sha256=observed["policy_sha256"])
            if observed["active"]:
                if observed["invocation"] is None: raise ValueError("Missing original invocation")
                state["invocation"] = observed["invocation"]
            return observed
        try:
            record("prepared", **result)
            if not state["create_intent"]:
                unchanged()
                if network.existing(bundle, plan) is not None: raise ValueError("An unrelated member appeared")
                state["create_intent"] = True; wrote = True; record("before-create", read_only=False); network.create(bundle, plan, state)
            original = inspect(); record("original-created")
            if original["active"] and not state["start_intent"]: raise ValueError("Member started outside the original operation")
            if plan["runtime"] == "raw" and not state["reload_intent"]:
                unchanged(); state["reload_intent"] = True; wrote = True; record("before-reload", read_only=False); network.reload()
            original = inspect()
            if not original["loaded"]: raise ValueError("Observe the original uncertain daemon reload; do not repeat it")
            if not state["start_intent"]:
                unchanged(); state["start_intent"] = True; wrote = True; record("before-start", read_only=False); network.start(bundle, plan, state)
            deadline = time.monotonic() + wait_seconds
            while True:
                original = inspect(); record("observing-original-member")
                if original["active"] and network.healthy(bundle, plan, original): break
                if time.monotonic() >= deadline: raise TimeoutError("Observe the original member again; do not repeat an uncertain start")
                time.sleep(1)
            unchanged(); original = inspect()
            if not original["active"] or not network.healthy(bundle, plan, original): raise ValueError("Original member lost readiness")
            result.update(host_checked=True, original_member_active=True, tls_health_observed=True, ssh_host_key_observed=True, read_only=not wrote)
            record("completed", **result); return result
        except BaseException:
            record("stopped-for-review", failed_at=state["phase"]); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--host-plan", type=Path, required=True)
    parser.add_argument("--mode", choices=("local", "check-host", "activate", "resume"), default="local")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=300); parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        if args.mode in {"activate", "resume"} and args.confirm != "ACTIVATE_PREPARED_GERRIT_MEMBER": raise ValueError("Confirm the selected host activation")
        plan = loads_strict_json(read_bounded_bytes(args.host_plan, max_bytes=256 * 1024))
        print(values.canonical(operate(args.bundle_dir, plan, mode=args.mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Gerrit host activation stopped; retain the private journal and original member. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
