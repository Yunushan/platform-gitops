#!/usr/bin/env python3
"""Activate one prepared Docker/native Keycloak member; resume original handles."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import itertools
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import time
from uuid import uuid4

from atomic_file import atomic_write_text, _fsync_directory
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
import bootstrap_keycloak_realm as bootstrap
from deploy_keycloak_ha import canonical, fingerprint, lock, sha, uid
import render_keycloak_ha as renderer
from http_transport import read_bounded_response
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
CLEAN = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent", "LC_ALL": "C"}
LABEL = "platform.gitops/keycloak-"
FILES = {"database.env", "tls/tls.crt", "tls/tls.key", "db-ca/ca.crt", "health-ca.crt"}
UNVERIFIED = (*bootstrap.UNVERIFIED, "cache_membership_verified", "fleet_deployment_verified")


def tools(): return bootstrap.sources(Path(__file__).stem)
def container_id(value): return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def docker_paths(spec):
    return (spec.get("binary", "/usr/bin/docker"), spec.get("socket", "/var/run/docker.sock")) if spec is not None else ("/usr/bin/docker", "/var/run/docker.sock")


def local_runtime_path(value):
    return isinstance(value, str) and bool(re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value)) and not any(part in {".", ".."} for part in value.split("/"))


def validate(plan, contract):
    renderer.exact(plan, "schema_version contract_sha256 member machine_id_sha256 boot_id service_uid service_gid bind_addresses files_sha256 docker native")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not sha(plan["contract_sha256"]) or not sha(plan["machine_id_sha256"]) or not uid(plan["boot_id"]): raise ValueError("Pin the original host and package")
    if contract["runtime"] not in {"docker", "raw"} or plan["member"] not in {m["name"] for m in contract["plan"]["members"]}: raise ValueError("Select exactly one Docker/native member")
    if any(type(plan[k]) is not int or not 1 <= plan[k] <= 2 ** 31 - 2 for k in ("service_uid", "service_gid")): raise ValueError("Pin the dedicated non-root service identity")
    renderer.exact(plan["bind_addresses"], "https management")
    member = next(m for m in contract["plan"]["members"] if m["name"] == plan["member"])
    if any(value not in {"127.0.0.1", member["address"]} for value in plan["bind_addresses"].values()): raise ValueError("Bind HTTPS/management explicitly to loopback or the reviewed member address")
    if not isinstance(plan["files_sha256"], dict) or set(plan["files_sha256"]) != FILES or not all(sha(v) for v in plan["files_sha256"].values()): raise ValueError("Pin each prepared private input")
    if contract["runtime"] == "docker":
        if plan["native"] is not None or plan["service_uid"] != 1000 or plan["service_gid"] != 1000: raise ValueError("Docker uses the reviewed UID/GID 1000")
        spec = plan["docker"]
        fields = "binary_sha256 daemon_id server_version image_id"
        renderer.exact(spec, fields + (" binary socket" if isinstance(spec, dict) and ({"binary", "socket"} & set(spec)) else ""))
        if not all(local_runtime_path(v) for v in docker_paths(spec)) or docker_paths(spec)[0] == docker_paths(spec)[1]: raise ValueError("Select separate canonical Linux CLI and local Unix socket paths")
        if not sha(spec["binary_sha256"]) or not isinstance(spec["daemon_id"], str) or not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", spec["daemon_id"]) or not isinstance(spec["server_version"], str) or not re.fullmatch(r"[2-9][0-9][.][0-9]+[.][0-9]+", spec["server_version"]) or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(spec["image_id"])): raise ValueError("Pin the local Docker engine and preloaded image")
    else:
        if plan["docker"] is not None: raise ValueError("Native activation cannot select Docker")
        spec = plan["native"]; renderer.exact(spec, "user group java_home java_manifest_sha256 systemctl_sha256 max_runtime_seconds")
        if spec["max_runtime_seconds"] is not None and (type(spec["max_runtime_seconds"]) is not int or not 60 <= spec["max_runtime_seconds"] <= 600): raise ValueError("Use null for a persistent member or an explicit 60–600 second canary lifetime")
        if any(not isinstance(spec[k], str) or not renderer.IDENTIFIER.fullmatch(spec[k]) or len(spec[k]) > 31 or spec[k] == "root" for k in ("user", "group")): raise ValueError("Select a named non-root native service account and group")
        if not isinstance(spec["java_home"], str) or not renderer.PATH.fullmatch(spec["java_home"]) or not sha(spec["java_manifest_sha256"]) or not sha(spec["systemctl_sha256"]): raise ValueError("Pin the reviewed Java release and service manager")
        homes = [spec["java_home"], contract["plan"]["native_home"], contract["plan"]["private_dir"]]
        if any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for i, a in enumerate(homes) for b in homes[i + 1:]): raise ValueError("Keep runtime, Java and private inputs separate")


def trusted(path, *, owner=0, private=False, directory=False):
    path = Path(path)
    if not path.is_absolute(): raise ValueError("Use absolute staged host paths")
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022: raise ValueError("Untrusted host path ancestor")
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != owner or info.st_mode & (0o077 if private else 0o022)
        or directory and not stat.S_ISDIR(info.st_mode) or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)): raise ValueError("Untrusted host file or directory")
    return info


def protected_socket(value):
    if not local_runtime_path(str(value)): raise ValueError("Use a canonical absolute local socket path")
    path = Path(value)
    # /var/run is a conventional root-controlled alias on supported Linux
    # hosts. Do not resolve arbitrary aliases before checking their ancestors:
    # a writable alias directory could otherwise redirect later CLI requests.
    if path.parts[:3] == ("/", "var", "run"):
        alias = Path("/var/run"); trusted(alias.parent, directory=True); info = alias.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != 0 or alias.resolve(strict=True) != Path("/run"): raise ValueError("The conventional runtime alias changed")
            path = Path("/run", *path.parts[3:])
    trusted(path.parent, directory=True); info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o007: raise ValueError("Require a protected root-owned local Docker socket")
    return path, info


def file_hash(path, *, maximum=256 * 1024 ** 2):
    info = trusted(path)
    if info.st_size > maximum: raise ValueError("Host artifact exceeds the bounded inventory")
    digest = hashlib.sha256(); size = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino): raise ValueError("The original artifact handle changed")
        while True:
            chunk = stream.read(min(1024 * 1024, maximum - size + 1))
            if not chunk: break
            size += len(chunk)
            if size > maximum: raise ValueError("A host artifact grew beyond its read bound")
            digest.update(chunk)
    value = digest.hexdigest()
    after = path.stat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns): raise ValueError("A host artifact changed while reading")
    return value


def readable(path, plan, *, executable=False, boundary=None):
    if boundary is not None and not path.is_relative_to(boundary): raise ValueError("The selected file is outside its read-only mount")
    for item in [*reversed(path.parents), path]:
        if boundary is not None and not item.is_relative_to(boundary): continue
        info = item.stat(); shift = 6 if info.st_uid == plan["service_uid"] else 3 if info.st_gid == plan["service_gid"] else 0
        required = 1 if item.is_dir() or executable and item == path else 4
        if not (info.st_mode >> shift) & required: raise ValueError("The dedicated service identity cannot access a prepared input")


def manifest(home, path, expected, *, mutable_data=False, reader=None):
    trusted(home, directory=True); trusted(path)
    raw = read_bounded_bytes(path, max_bytes=1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected: raise ValueError("The reviewed distribution manifest changed")
    entries = {}
    for line in raw.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)", line)
        if not match: raise ValueError("Use a relative, complete SHA-256 distribution inventory")
        digest, name = match.groups()
        if any(p in {".", ".."} for p in name.split("/")) or name in entries or mutable_data and name.split("/")[0] == "data": raise ValueError("Unsafe or repeated manifest entry")
        entries[name] = digest
    if not 1 <= len(entries) <= 5000: raise ValueError("Bound the immutable distribution inventory")
    actual = set(); total = 0; count = 0
    def inventory():
        for parent, directories, filenames in os.walk(home, followlinks=False):
            if mutable_data and Path(parent) == home: directories[:] = [d for d in directories if d != "data"]
            for name in [*directories, *filenames]: yield Path(parent) / name
    for item in inventory():
        relative = item.relative_to(home).as_posix()
        count += 1
        if count > 10000: raise ValueError("Excessive distribution inventory")
        if item.is_dir() and not item.is_symlink():
            trusted(item, directory=True)
            if reader is not None: readable(item, reader)
            continue
        if relative not in entries: raise ValueError("An unreviewed file or symlink entered the distribution")
        total += item.lstat().st_size
        if total > 2 * 1024 ** 3 or file_hash(item) != entries[relative]: raise ValueError("A reviewed distribution file changed")
        if reader is not None: readable(item, reader)
        actual.add(relative)
    if actual != set(entries): raise ValueError("A reviewed distribution file is missing")


def password(raw, runtime):
    text = raw.decode("utf-8")
    if text.endswith("\n"): text = text[:-1]
    if not text.startswith("KC_DB_PASSWORD="): raise ValueError("The credential file must contain only KC_DB_PASSWORD")
    value = text[len("KC_DB_PASSWORD="):]
    if runtime == "raw":
        if len(value) >= 2 and value[0] == value[-1] == "'" and "'" not in value[1:-1]: value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            source, value, index = value[1:-1], "", 0
            while index < len(source):
                char = source[index]; index += 1
                if char == "\\":
                    if index == len(source) or source[index] not in '\\"$`': raise ValueError("Use the supported systemd environment quoting")
                    char = source[index]; index += 1
                elif char == '"': raise ValueError("Unescaped environment quote")
                value += char
        elif re.search(r"[\s'\"\\]", value): raise ValueError("Quote the native environment value explicitly")
    if not 1 <= len(value) <= 1024 or any(ord(c) < 32 or ord(c) > 126 for c in value) or "${" in value: raise ValueError("Use a single supported credential assignment without expression expansion")
    return value


def unit_text(contract, plan, state):
    body = renderer.render(contract["plan"], "raw")[plan["member"] + ".service"]
    body = body.replace("User=keycloak\n", "User=" + plan["native"]["user"] + "\n").replace("Group=keycloak\n", "Group=" + plan["native"]["group"] + "\n")
    java = plan["native"]["java_home"]
    return ("# keycloak-operation=" + state["operation_id"] + "\n# keycloak-plan=" + fingerprint(plan) + "\n" + body
        + "\n[Service]\nEnvironment=JAVA_HOME=" + java + "\nEnvironment=PATH=" + java + "/bin:/usr/bin:/bin\n"
        + "".join("Environment=" + key + "=" + value + "\n" for key, value in bind_environment(plan).items())
        + ("Restart=no\nRuntimeMaxSec=" + str(plan["native"]["max_runtime_seconds"]) + "\n" if plan["native"]["max_runtime_seconds"] is not None else ""))


def bind_environment(plan): return {"KC_HTTP_HOST": plan["bind_addresses"]["https"], "KC_HTTP_MANAGEMENT_HOST": plan["bind_addresses"]["management"]}


def labels(state): return {LABEL + "operation": state["operation_id"], LABEL + "plan": state["plan_sha256"], LABEL + "tools": state["toolset_sha256"]}
def name(plan): return "platform-" + plan["member"]


def create_arguments(contract, plan, state):
    selected = contract["plan"]; private = selected["private_dir"]
    args = ["container", "create", "--pull=never", "--name", name(plan), "--user=1000:1000", "--network=host", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--memory=2g", "--memory-swap=2g", "--cpus=2", "--pids-limit=512", "--ipc=none", "--cgroupns=private", "--userns=host", "--runtime=runc", "--restart=unless-stopped",
        "--stop-timeout=120", "--workdir=/opt/keycloak", "--hostname=" + plan["member"], "--no-healthcheck", "--log-driver=json-file", "--log-opt=max-size=10m", "--log-opt=max-file=3",
        "--entrypoint=/opt/keycloak/bin/kc.sh", "--env-file", private + "/database.env"]
    for key, value in sorted(labels(state).items()): args += ["--label", key + "=" + value]
    for key, value in bind_environment(plan).items(): args += ["--env", key + "=" + value]
    for directory in ("tls", "db-ca"):
        args += ["--mount", "type=bind,source=" + private + "/" + directory + ",destination=/run/platform-keycloak/" + directory + ",readonly,bind-propagation=rprivate"]
    args += ["--tmpfs", "/opt/keycloak/data:rw,nosuid,nodev,noexec,uid=1000,gid=1000,mode=0700,size=1g", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777,size=256m"]
    member = next(m for m in selected["members"] if m["name"] == plan["member"])
    return [*args, selected["image"], *renderer.arguments(selected, member=member)]


def environment_map(values):
    if not isinstance(values, list) or len(values) > 512: raise ValueError("Invalid container environment inventory")
    result = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value or "\0" in value: raise ValueError("Invalid container environment entry")
        key, item = value.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in result: raise ValueError("Ambiguous container environment")
        result[key] = item
    return result


def container_policy(row, contract, plan, state, image, credential):
    config, host = row.get("Config", {}), row.get("HostConfig", {})
    # Docker materializes this nullable default after the first start. Pin its
    # meaning, including before start, rather than treating null -> false as a
    # replacement. Disabling OOM termination or a false-looking type is refused.
    if host.get("OomKillDisable") is not None and host.get("OomKillDisable") is not False: raise ValueError("Original Docker OOM policy changed")
    host = {**host, "OomKillDisable": False}
    overrides = {"KC_DB_PASSWORD": credential, **bind_environment(plan)}
    env = {**environment_map(image.get("Config", {}).get("Env") or []), **overrides}
    member = next(m for m in contract["plan"]["members"] if m["name"] == plan["member"])
    if (not container_id(row.get("Id")) or state["handle"] is not None and row["Id"] != state["handle"] or row.get("Name") != "/" + name(plan)
        or row.get("Image") != plan["docker"]["image_id"] or config.get("Image") != contract["plan"]["image"] or config.get("User") != "1000:1000"
        or config.get("Entrypoint") != ["/opt/keycloak/bin/kc.sh"] or config.get("Cmd") != renderer.arguments(contract["plan"], member=member)
        or environment_map(config.get("Env")) != env or config.get("Labels") != {**(image.get("Config", {}).get("Labels") or {}), **labels(state)} or config.get("WorkingDir") != "/opt/keycloak"
        or config.get("Hostname") != plan["member"] or config.get("StopTimeout") != 120 or config.get("Tty") is not False or config.get("OpenStdin") is not False or config.get("Healthcheck", {}).get("Test") != ["NONE"]): raise ValueError("Original Docker command or identity changed")
    required = {"ReadonlyRootfs": True, "Privileged": False, "OomKillDisable": False, "NetworkMode": "host", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc", "PidsLimit": 512,
        "Memory": 2 * 1024 ** 3, "MemorySwap": 2 * 1024 ** 3, "NanoCpus": 2000000000, "AutoRemove": False, "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}}
    if (any(host.get(k) != v for k, v in required.items()) or host.get("CapDrop") != ["ALL"] or host.get("SecurityOpt") not in (["no-new-privileges:true"], ["no-new-privileges"])
        or any(host.get(k) for k in ("CapAdd", "Binds", "VolumesFrom", "Devices", "DeviceRequests", "DeviceCgroupRules", "GroupAdd", "Links", "PortBindings", "PublishAllPorts", "PidMode", "UTSMode", "ExtraHosts", "Sysctls", "Init"))
        or host.get("Tmpfs") != {"/opt/keycloak/data": "rw,nosuid,nodev,noexec,uid=1000,gid=1000,mode=0700,size=1g", "/tmp": "rw,nosuid,nodev,noexec,mode=1777,size=256m"}): raise ValueError("Original Docker isolation changed")
    expected = {"/run/platform-keycloak/" + part: contract["plan"]["private_dir"] + "/" + part for part in ("tls", "db-ca")}
    mounts = row.get("Mounts", [])
    if len(mounts) != 2 or {m.get("Destination") for m in mounts} != set(expected) or any(m.get("Type") != "bind" or m.get("Source") != expected[m["Destination"]] or m.get("RW") is not False or m.get("Propagation") != "rprivate" for m in mounts) or len(host.get("Mounts", [])) != 2: raise ValueError("Original Docker private mounts changed")
    return fingerprint({"Config": {**config, "Env": [hashlib.sha256((key + "=" + value).encode()).hexdigest() for key, value in sorted(env.items())]}, "HostConfig": host, "Mounts": sorted(mounts, key=lambda m: m["Destination"]), "Image": row["Image"]})


class DirectHTTPS(http.client.HTTPSConnection):
    def __init__(self, address, hostname, port, context):
        super().__init__(hostname, port, timeout=5, context=context); self.address = address

    def connect(self): self.sock = self._context.wrap_socket(socket.create_connection((self.address, self.port), self.timeout), server_hostname=self.host)


class Host:
    def __init__(self, docker=None): self.binary, self.socket = docker_paths(docker)

    def run(self, args, *, environment=None):
        result = run_bounded(args, input="", env=environment or CLEAN, text=True, timeout=45, output_max_bytes=2 * 1024 ** 2, check=False)
        if result.returncode: raise ValueError("Host operation failed; private diagnostics were not printed")
        return result.stdout

    def docker(self, *args): return self.run([self.binary, "--host=unix://" + self.socket, *args])
    def docker_facts(self):
        return loads_strict_json(self.docker("info", "--format", '{"id":{{json .ID}},"os":{{json .OSType}},"security":{{json .SecurityOptions}},"runtime":{{json .DefaultRuntime}},"cgroup":{{json .CgroupVersion}},"memory":{{json .MemoryLimit}},"swap":{{json .SwapLimit}},"cpu_period":{{json .CPUCfsPeriod}},"cpu_quota":{{json .CPUCfsQuota}},"pids":{{json .PidsLimit}}}'))
    def unit_path(self, plan): return Path("/etc/systemd/system") / (name(plan) + ".service")

    def prerequisites(self, contract, plan, *, starting=False):
        if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use an explicit root Linux host session")
        for module in (Path(sys.executable).resolve(strict=True), *Path(__file__).parent.glob("*.py")): trusted(module)
        machine = read_bounded_bytes(Path("/etc/machine-id"), max_bytes=128).decode().strip()
        boot = read_bounded_bytes(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).decode().strip()
        if hashlib.sha256(machine.encode()).hexdigest() != plan["machine_id_sha256"] or boot != plan["boot_id"]: raise ValueError("Original host or boot changed")
        selected = contract["plan"]; member = next(m for m in selected["members"] if m["name"] == plan["member"])
        ip = Path("/usr/sbin/ip").resolve(strict=True); trusted(ip)
        addresses = loads_strict_json(self.run([str(ip), "-j", "-4", "address", "show"]))
        if sum(a.get("local") == member["address"] for row in addresses for a in row.get("addr_info", [])) != 1: raise ValueError("The reviewed member address is not local")
        private = Path(selected["private_dir"]); trusted(private, directory=True)
        for part in ("tls", "db-ca"): trusted(private / part, directory=True)
        for filename, digest in plan["files_sha256"].items():
            path = private / filename
            info = trusted(path, private=filename == "database.env")
            if filename == "tls/tls.key" and (info.st_gid != plan["service_gid"] or info.st_mode & 0o777 not in {0o440, 0o640}): raise ValueError("The private TLS key must be root-owned and readable only by its service group")
            if info.st_size > 128 * 1024 or hashlib.sha256(read_bounded_bytes(path, max_bytes=128 * 1024)).hexdigest() != digest: raise ValueError("A prepared private input changed")
            if filename not in {"database.env", "health-ca.crt"}:
                readable(path, plan, boundary=private / filename.split("/")[0] if contract["runtime"] == "docker" else None)
        if {p.name for p in (private / "tls").iterdir()} != {"tls.crt", "tls.key"} or {p.name for p in (private / "db-ca").iterdir()} != {"ca.crt"}: raise ValueError("Keep only the reviewed TLS/CA mount contents")
        password(read_bounded_bytes(private / "database.env", max_bytes=4096), contract["runtime"])
        ssl.create_default_context(cadata=read_bounded_bytes(private / "health-ca.crt", max_bytes=128 * 1024).decode("ascii"))
        result = {"machine_id_sha256": plan["machine_id_sha256"], "boot_id": boot}
        if contract["runtime"] == "docker":
            pins = plan["docker"]
            if (self.binary, self.socket) != docker_paths(pins): raise ValueError("The selected local Docker transport changed")
            if file_hash(Path(self.binary)) != pins["binary_sha256"]: raise ValueError("The Docker executable changed")
            sock, info = protected_socket(self.socket)
            facts = self.docker_facts()
            version = loads_strict_json(self.docker("version", "--format", '{{json .Server.Version}}'))
            if facts.get("id") != pins["daemon_id"] or version != pins["server_version"] or facts.get("os") != "linux" or facts.get("runtime") != "runc" or not isinstance(facts.get("security"), list) or any("rootless" in s or "userns" in s for s in facts["security"]) or not any("name=seccomp" in s and "profile=builtin" in s for s in facts["security"]): raise ValueError("The reviewed rootful Docker engine changed")
            if facts.get("cgroup") != "2" or any(facts.get(k) is not True for k in ("memory", "swap", "cpu_period", "cpu_quota", "pids")): raise ValueError("The Docker engine cannot enforce the reviewed resource limits")
            rows = loads_strict_json(self.docker("image", "inspect", selected["image"]))
            if len(rows) != 1 or rows[0].get("Id") != pins["image_id"] or selected["image"] not in rows[0].get("RepoDigests", []) or rows[0].get("Os") != "linux" or rows[0].get("Architecture") != {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine) or rows[0].get("Config", {}).get("Volumes"): raise ValueError("Preload the reviewed image without anonymous volumes")
            result.update(socket_path=str(sock), socket_device=info.st_dev, socket_inode=info.st_ino, image={k: rows[0][k] for k in ("Id", "Config")})
        else:
            import pwd
            import grp
            user = pwd.getpwnam(plan["native"]["user"])
            if user.pw_uid != plan["service_uid"] or user.pw_gid != plan["service_gid"] or grp.getgrnam(plan["native"]["group"]).gr_gid != plan["service_gid"]: raise ValueError("The native service identity changed")
            java = Path(plan["native"]["java_home"]); home = Path(selected["native_home"])
            manifest(home, private / "native-files.sha256", selected["native_manifest_sha256"], mutable_data=True, reader=plan)
            manifest(java, private / "java-files.sha256", plan["native"]["java_manifest_sha256"], reader=plan)
            if file_hash(Path("/usr/bin/systemctl")) != plan["native"]["systemctl_sha256"]: raise ValueError("The service manager changed")
            version = self.run([str(home / "bin/kc.sh"), "--version"], environment={**CLEAN, "JAVA_HOME": str(java), "PATH": str(java / "bin") + ":/usr/bin:/bin"})
            if not re.fullmatch(r"Keycloak " + re.escape(selected["version"]) + r"(?:\s|$).*", version.strip(), re.S): raise ValueError("The prepared Keycloak version differs")
            for binary in (home / "bin/kc.sh", java / "bin/java"): trusted(binary); readable(binary, plan, executable=True)
            trusted(home / "data", directory=True)
            if starting:
                data = Path("/var/lib") / ("platform-keycloak-" + plan["member"])
                if data.exists() or data.is_symlink() or any((home / "data").iterdir()): raise ValueError("Fresh activation requires unused local member state")
            trusted(private / "native-manifest.sha256")
            for item in (private / "native-manifest.sha256", private / "native-files.sha256"): readable(item, plan)
            if read_bounded_bytes(private / "native-manifest.sha256", max_bytes=4096).decode() != selected["native_manifest_sha256"] + "  " + selected["private_dir"] + "/native-files.sha256\n": raise ValueError("The generated native manifest commitment changed")
        return result

    def existing(self, contract, plan):
        if contract["runtime"] == "docker":
            ids = self.docker("container", "ls", "--all", "--no-trunc", "--filter", "name=^/" + name(plan) + "$", "--format", '{{.ID}}').split()
            if len(ids) > 1 or any(not container_id(i) for i in ids): raise ValueError("Ambiguous original container")
            return ids[0] if ids else None
        path = self.unit_path(plan)
        props = self.properties(plan)
        if path.exists() or path.is_symlink() or props.get("LoadState") != "not-found": return "existing-unit"
        return None

    def properties(self, plan):
        raw = self.run(["/usr/bin/systemctl", "show", name(plan) + ".service", "--property=LoadState,FragmentPath,DropInPaths,ActiveState,SubState,MainPID,InvocationID,NRestarts,NeedDaemonReload,ControlGroup"])
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def create(self, contract, plan, state):
        if contract["runtime"] == "docker":
            identity = self.docker(*create_arguments(contract, plan, state)).strip()
            if not container_id(identity): raise ValueError("Docker did not acknowledge an original container")
            return identity
        path = self.unit_path(plan); trusted(path.parent, directory=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(unit_text(contract, plan, state)); stream.flush(); os.fsync(stream.fileno())
        _fsync_directory(path.parent)
        return "original-unit"

    def inspect(self, contract, plan, state, facts):
        if contract["runtime"] == "docker":
            rows = loads_strict_json(self.docker("container", "inspect", name(plan)))
            if len(rows) != 1: raise ValueError("Original container is missing")
            row = rows[0]; credential = password(read_bounded_bytes(Path(contract["plan"]["private_dir"]) / "database.env", max_bytes=4096), "docker")
            policy = container_policy(row, contract, plan, state, facts["image"], credential)
            live = row.get("State", {})
            if live.get("Dead") or live.get("OOMKilled") or live.get("Error") or live.get("Status") not in {"created", "running"} or live.get("Running") is not (live.get("Status") == "running"): raise ValueError("The original container failed")
            invocation = live.get("StartedAt") if live.get("Running") is True else None
            if invocation is not None:
                if not isinstance(invocation, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", invocation) or datetime.fromisoformat(invocation.replace("Z", "+00:00")).year < 2020: raise ValueError("Invalid original container start identity")
            pid = live.get("Pid") if live.get("Running") is True else None
            cgroup = process_cgroup(pid) if pid is not None else None
            if cgroup is not None and row["Id"] not in cgroup: raise ValueError("The original container has no dedicated host cgroup")
            return {"handle": row["Id"], "policy_sha256": policy, "active": live.get("Running") is True, "invocation": invocation, "loaded": True, "pid": pid, "cgroup": cgroup}
        path = self.unit_path(plan); info = trusted(path)
        expected = unit_text(contract, plan, state).encode()
        if read_bounded_bytes(path, max_bytes=64 * 1024) != expected or info.st_mode & 0o777 != 0o644: raise ValueError("The original unit content changed")
        props = self.properties(plan)
        if props.get("DropInPaths") or props.get("FragmentPath") not in {"", str(path)} or props.get("ActiveState") in {"failed", "deactivating"}: raise ValueError("Original service ownership or state changed")
        active = props.get("ActiveState") == "active" and props.get("SubState") == "running" and str(props.get("MainPID", "")).isdigit() and int(props["MainPID"]) > 1
        invocation = props.get("InvocationID") or None
        if invocation is not None and not re.fullmatch(r"[0-9a-f]{32}", invocation): raise ValueError("Invalid original service invocation")
        cgroup = props.get("ControlGroup") or None
        if active and (not cgroup or not cgroup.endswith("/" + name(plan) + ".service") or process_cgroup(int(props["MainPID"])) != cgroup): raise ValueError("The original service has no dedicated host cgroup")
        return {"handle": fingerprint({"device": info.st_dev, "inode": info.st_ino}), "policy_sha256": hashlib.sha256(expected).hexdigest(), "active": active, "invocation": invocation,
            "loaded": props.get("LoadState") == "loaded" and props.get("FragmentPath") == str(path) and props.get("NeedDaemonReload") == "no", "pid": int(props["MainPID"]) if active else None, "cgroup": cgroup}

    def reload(self): self.run(["/usr/bin/systemctl", "daemon-reload"])
    def start(self, contract, plan, state):
        if contract["runtime"] == "docker": self.docker("container", "start", state["handle"])
        else: self.run(["/usr/bin/systemctl", "start", "--no-block", name(plan) + ".service"])

    def healthy(self, contract, plan, original):
        selected = contract["plan"]; member = next(m for m in selected["members"] if m["name"] == plan["member"])
        address = plan["bind_addresses"]["management"]
        if not owns_listener(original, address, 9000) or not owns_listener(original, plan["bind_addresses"]["https"], 8443): return False
        ca = read_bounded_bytes(Path(selected["private_dir"]) / "health-ca.crt", max_bytes=128 * 1024).decode("ascii")
        connection = DirectHTTPS(address, selected["hostname"], 9000, ssl.create_default_context(cadata=ca))
        try:
            connection.request("GET", "/health/ready", headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200: return False
            body = loads_strict_json(read_bounded_response(response, max_bytes=64 * 1024))
            return isinstance(body, dict) and body.get("status") == "UP" and isinstance(body.get("checks"), list) and bool(body["checks"]) and all(c.get("status") == "UP" for c in body["checks"])
        except (OSError, ValueError, http.client.HTTPException): return False
        finally: connection.close()


def process_cgroup(pid):
    if type(pid) is not int or pid <= 1: raise ValueError("Invalid original process identity")
    raw = read_bounded_bytes(Path("/proc") / str(pid) / "cgroup", max_bytes=8192).decode()
    rows = [line[3:] for line in raw.splitlines() if line.startswith("0::")]
    if len(rows) != 1 or not rows[0].startswith("/") or rows[0] == "/" or ".." in rows[0].split("/"): raise ValueError("Require a dedicated unified cgroup")
    return rows[0]


def owns_listener(original, address, port):
    pid, group = original.get("pid"), original.get("cgroup")
    if not group or process_cgroup(pid) != group: raise ValueError("The original member process changed cgroup")
    directory = Path("/sys/fs/cgroup") / group.lstrip("/")
    processes = read_bounded_bytes(directory / "cgroup.procs", max_bytes=32 * 1024).decode().split()
    if not processes or len(processes) > 512 or any(not p.isdigit() for p in processes): raise ValueError("Invalid original cgroup process inventory")
    sockets = set(); count = 0
    for process in processes:
        if process_cgroup(int(process)) != group: raise ValueError("An original member process moved cgroup")
        entries = list(itertools.islice((Path("/proc") / process / "fd").iterdir(), 8193)); count += len(entries)
        if len(entries) > 8192 or count > 16384: raise ValueError("Excessive original process descriptors")
        for entry in entries:
            try: target = os.readlink(entry)
            except FileNotFoundError: continue
            match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
            if match: sockets.add(match[1])
    ipv4 = socket.inet_aton(address)[::-1].hex().upper()
    mapped = socket.inet_pton(socket.AF_INET6, "::ffff:" + address)
    mapped = b"".join(mapped[i:i + 4][::-1] for i in range(0, 16, 4)).hex().upper()
    for filename, hosts in (("tcp", {ipv4}), ("tcp6", {mapped})):
        rows = read_bounded_bytes(Path("/proc/net") / filename, max_bytes=4 * 1024 ** 2).decode().splitlines()[1:]
        for row in rows:
            values = row.split(); host, number = values[1].split(":")
            if values[3] == "0A" and host in hosts and int(number, 16) == port and values[9] in sockets: return True
    return False


def operate(bundle, plan, *, mode="local", output=None, host=None, wait_seconds=180):
    contract, _ = bootstrap.package(bundle, plan.get("contract_sha256")); validate(plan, contract)
    result = {"local_inputs_verified": True, "host_checked": False, "original_member_active": False, "tls_readiness_observed": False, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"check-host", "activate", "resume"} or type(wait_seconds) is not int or not 5 <= wait_seconds <= 600: raise ValueError("Use one explicit bounded host activation")
    network = host or Host(plan["docker"]); facts = network.prerequisites(contract, plan, starting=mode in {"check-host", "activate"}); source = tools()
    if mode == "check-host":
        if network.existing(contract, plan) is not None: raise ValueError("Existing members require their original activation journal")
        return {**result, "host_checked": True}
    if output is None or output.is_symlink(): raise ValueError("Keep an original private operation directory")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts: raise ValueError("Use a private member-specific operation directory")
    if mode == "activate": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume requires its original journal")
    receipt = directory / "keycloak-host-proof.json"
    with lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("The journal cannot redirect")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (state.get("schema_version") != 1 or type(state.get("schema_version")) is not int or state.get("plan_sha256") != fingerprint(plan) or state.get("toolset_sha256") != source
                or not uid(state.get("operation_id")) or state.get("facts_sha256") != fingerprint(facts) or any(state.get(k) is not False for k in UNVERIFIED)
                or any(type(state.get(k)) is not bool for k in ("create_intent", "reload_intent", "start_intent")) or state["start_intent"] and not state["create_intent"]
                or state["reload_intent"] and not state["create_intent"] or contract["runtime"] == "docker" and state["reload_intent"] or contract["runtime"] == "raw" and state["start_intent"] and not state["reload_intent"]
                or state.get("handle") is not None and not sha(state["handle"]) or state.get("policy_sha256") is not None and not sha(state["policy_sha256"])): raise ValueError("Resume only the original member and source")
        else:
            if network.existing(contract, plan) is not None: raise ValueError("Never replace or adopt an existing member")
            state = {"schema_version": 1, "operation_id": str(uuid4()), "plan_sha256": fingerprint(plan), "toolset_sha256": source, "facts_sha256": fingerprint(facts),
                "create_intent": False, "reload_intent": False, "start_intent": False, "handle": None, "policy_sha256": None, "invocation": None, **result}
        wrote = False
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        def unchanged():
            if tools() != source or fingerprint(network.prerequisites(contract, plan)) != state["facts_sha256"]: raise ValueError("Prepared inputs changed before activation")
        def inspect():
            observed = network.inspect(contract, plan, state, facts)
            if (state["handle"] is not None and observed["handle"] != state["handle"] or state["policy_sha256"] is not None and observed["policy_sha256"] != state["policy_sha256"]
                or state["invocation"] is not None and observed["invocation"] != state["invocation"]): raise ValueError("The original member was replaced or restarted")
            state.update(handle=observed["handle"], policy_sha256=observed["policy_sha256"])
            if observed["active"]:
                if observed["invocation"] is None: raise ValueError("The running original service has no invocation identity")
                state["invocation"] = observed["invocation"]
            return observed
        try:
            record("prepared", **result)
            if not state["create_intent"]:
                unchanged()
                if network.existing(contract, plan) is not None: raise ValueError("An unrelated member appeared")
                state["create_intent"] = True; wrote = True; record("before-create", read_only=False)
                network.create(contract, plan, state)
            # A timed-out create is only observed. Absence or a partial unit is
            # never permission to repeat the original write.
            original = inspect(); record("original-created")
            if original["active"] and not state["start_intent"]: raise ValueError("A member started outside its original operation")
            if contract["runtime"] == "raw" and not state["reload_intent"]:
                unchanged(); state["reload_intent"] = True; wrote = True; record("before-reload", read_only=False); network.reload()
            original = inspect()
            if not original["loaded"]: raise ValueError("Observe the original uncertain service-manager reload; do not repeat it")
            if not state["start_intent"]:
                unchanged(); state["start_intent"] = True; wrote = True; record("before-start", read_only=False); network.start(contract, plan, state)
            deadline = time.monotonic() + wait_seconds
            while True:
                original = inspect(); record("observing-original-member")
                if original["active"] and network.healthy(contract, plan, original): break
                if time.monotonic() >= deadline: raise TimeoutError("Observe the same original member again; never repeat an uncertain start")
                time.sleep(1)
            unchanged(); original = inspect()
            if not original["active"] or not network.healthy(contract, plan, original): raise ValueError("The original member lost readiness")
            result.update(host_checked=True, original_member_active=True, tls_readiness_observed=True, read_only=not wrote)
            record("completed", **result); return result
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="keycloak-host-activation-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--host-plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=180)
    group = parser.add_mutually_exclusive_group()
    for value in ("check-host", "activate", "resume"): group.add_argument("--" + value, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.host_plan, max_bytes=128 * 1024)); mode = next((v.replace("_", "-") for v in ("check_host", "activate", "resume") if getattr(args, v)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Keycloak host activation stopped; preserve its private journal and original handles. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
