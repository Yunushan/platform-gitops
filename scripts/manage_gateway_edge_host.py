#!/usr/bin/env python3
"""Install, verify, resume or explicitly roll back one owned Linux Gateway entry host."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from uuid import uuid4

from atomic_file import atomic_write_bytes, atomic_write_text, _fsync_directory
from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import run_bounded
from deploy_gateway_edge import load_package
import gateway_edge_health as health
import render_gateway_edge as renderer
import start_gateway_edge as starter
from strict_json import loads_strict_json


ROLES = ("haproxy", "keepalived")
STATE = Path("/var/lib/platform-gateway-edge")
ACTIVE = Path(renderer.BASE)
UNITS = Path("/etc/systemd/system")
CLEAN = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "HOME": "/"}


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def capture(bundle, digest, gateway_bundle):
    contract, _, gateway = load_package(bundle, digest, gateway_bundle)
    if contract["runtime"] not in {"docker", "raw"}:
        raise ValueError("Host installation requires Docker or native Linux artifacts")
    generated = renderer.render(contract["plan"], gateway, contract["runtime"])
    files = {name: content.encode() for name, content in generated.items()}
    for name, expected in files.items():
        if read_bounded_bytes(bundle / name, max_bytes=2 * 1024 ** 2) != expected:
            raise ValueError("Host artifact differs from its reviewed generated content")
    return contract, files


def payloads(contract, files, ca_file, artifacts):
    result = dict(files)
    ca = read_bounded_bytes(ca_file, max_bytes=256 * 1024)
    if sha(ca) != contract["plan"]["ca_sha256"]:
        raise ValueError("Host CA differs from the reviewed fingerprint")
    result["ca.crt"] = ca
    if contract["runtime"] == "raw":
        if artifacts is None: raise ValueError("Supply the reviewed native daemon artifacts")
        for role in ROLES:
            value = read_bounded_bytes(artifacts / role, max_bytes=64 * 1024 ** 2)
            if sha(value) != contract["plan"]["binaries"][role]["sha256"]:
                raise ValueError("Native daemon artifact fingerprint differs")
            result["bin/" + role] = value
        support = contract["plan"]["binaries"]["keepalived"].get("config_opts_sha256")
        if support:
            value = read_bounded_bytes(artifacts / "keepalived.config-opts", max_bytes=256 * 1024)
            if sha(value) != support: raise ValueError("Keepalived build metadata differs from its reviewed fingerprint")
            result["bin/keepalived.config-opts"] = value
    return result


def transition(current, target, *, rollback=False):
    if current is None: return
    mutable = {"gateway_contract_sha256", "images", "binaries", "ca_sha256", "ca_secret", "routes",
               "minimum_ready_routes", *renderer.SSH_SERVICES}
    old, new = current["plan"], target["plan"]
    if (current["runtime"] != target["runtime"] or {k: v for k, v in old.items() if k not in mutable} != {k: v for k, v in new.items() if k not in mutable}
            or {r["hostname"] for r in old["routes"]} != {r["hostname"] for r in new["routes"]}):
        raise ValueError("Host lifecycle cannot change entry ownership, VIP topology or hostname scope")
    for service in renderer.SSH_SERVICES:
        if service in old and service in new and old[service] != new[service]:
            raise ValueError("Preserve published SSH endpoints during an edge release transition")
    old_ssh = {old[key]["port"]: key for key in renderer.SSH_SERVICES if key in old}
    new_ssh = {new[key]["port"]: key for key in renderer.SSH_SERVICES if key in new}
    if any(old_ssh[port] != new_ssh[port] for port in old_ssh.keys() & new_ssh.keys()):
        raise ValueError("A published SSH port cannot change forge during an edge release transition")
    for role in ROLES:
        a, b = (tuple(int(p) for p in plan["binaries"][role]["version"].split(".")) for plan in (old, new))
        if b < a and not rollback: raise ValueError("Daemon downgrade requires explicit rollback to the retained previous release")


def secure_directory(path):
    if path.is_symlink(): raise ValueError("Managed directories cannot be symlinks")
    if not path.exists():
        secure_directory(path.parent)
        path.mkdir(mode=0o755)
        # Pair jobs intentionally use UMask=0077. These new managed paths
        # contain public release files and must be traversable by HAProxy's
        # separate service identity; private receipts keep their own 0600 mode.
        path.chmod(0o755)
        _fsync_directory(path)
        _fsync_directory(path.parent)
    starter.trusted_paths([path])


@contextmanager
def host_lock(path, *, blocking=False):
    import fcntl
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        state = os.fstat(descriptor)
        if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1 or state.st_uid != 0 or state.st_mode & 0o077: raise ValueError("Untrusted host-operation lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def install_files(directory, files):
    secure_directory(directory)
    for name, value in files.items():
        target = directory / name
        secure_directory(target.parent)
        mode = file_mode(name)
        if target.exists() or target.is_symlink():
            starter.trusted_paths([target])
            if target.is_symlink() or not stat.S_ISREG(target.stat().st_mode) or read_bounded_bytes(target, max_bytes=64 * 1024 ** 2) != value:
                raise ValueError("Immutable installed release changed")
        else:
            write_binary(target, value, mode=mode)
        if target.stat().st_mode & 0o777 != mode:
            raise ValueError("Installed release permissions differ")
    entries = list(directory.rglob("*"))
    actual = {p.relative_to(directory).as_posix() for p in entries if not p.is_dir()}
    expected_dirs = {str(Path(name).parent) for name in files if Path(name).parent != Path(".")}
    if actual != set(files) or any(p.is_symlink() or p.is_dir() and p.relative_to(directory).as_posix() not in expected_dirs for p in entries):
        raise ValueError("Unexpected files or directories in immutable release")


def file_mode(name):
    return 0o555 if name in {"bin/" + role for role in ROLES} else 0o644


def write_binary(path, value, *, mode):
    descriptor, name = tempfile.mkstemp(prefix=".edge-binary-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value); stream.flush(); os.fchmod(stream.fileno(), mode); os.fsync(stream.fileno())
        if path.exists() or path.is_symlink(): raise ValueError("Refuse binary replacement in an immutable release")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class Host:
    def __init__(self, state=STATE, active=ACTIVE, units=UNITS):
        self.state, self.active, self.units = state, active, units

    def run(self, arguments, *, environment=None):
        result = run_bounded(arguments, env=environment or CLEAN, input="", text=True, timeout=45, output_max_bytes=1024 ** 2, check=False)
        if result.returncode: raise ValueError("Host service operation failed; inspect local diagnostics")
        return result.stdout

    def prerequisites(self, contract, host):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ValueError("Host operations require an explicit root Linux session")
        selected = next(h for h in contract["plan"]["entry_hosts"] if h["name"] == host)
        if starter.interface_address(selected["interface"]) != selected["address"]:
            raise ValueError("The local host does not match the declared interface/address")
        starter.trusted_paths([Path("/usr/bin/python3"), Path(sys.executable)])
        if contract["runtime"] == "raw":
            starter.trusted_paths([Path("/usr/bin/systemctl"), Path("/usr/bin/systemd-analyze")])
        else:
            socket_path = Path("/var/run/docker.sock").resolve(strict=True)
            starter.trusted_paths([Path("/usr/bin/docker"), socket_path.parent])
            socket_stat = socket_path.stat()
            if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != 0 or socket_stat.st_mode & 0o007:
                raise ValueError("Docker must use its local root-owned, non-public Unix socket")
            info = loads_strict_json(self.run(self.docker("info", "--format", '{{json .OSType}}')))  # No credentials or full engine inventory.
            options = loads_strict_json(self.run(self.docker("info", "--format", '{{json .SecurityOptions}}')))
            if info != "linux" or any(any(mode in str(item) for mode in ("rootless", "userns")) for item in options):
                raise ValueError("A local rootful Linux Docker engine is required")
            for image in contract["plan"]["images"].values(): self.image_id(image)

    def docker(self, *arguments):
        return ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *arguments]

    def image_id(self, image):
        rows = loads_strict_json(self.run(self.docker("image", "inspect", image)))
        if len(rows) != 1 or image not in rows[0].get("RepoDigests", []): raise ValueError("Preload the exact reviewed image digest")
        return rows[0]["Id"]

    def compose(self, directory, contract, host, *arguments):
        project = contract["plan"]["name"] + "-" + host
        environment = {**CLEAN, "PLATFORM_EDGE_BUNDLE": str(directory), "PLATFORM_EDGE_INPUTS": str(directory)}
        return self.run(self.docker("compose", "--project-name", project, "-f", str(directory / ("compose-" + host + ".yaml")), *arguments), environment=environment)

    def unit(self, host, role):
        return "edge-" + host + "-" + role + ".service"

    def ownership(self, directory, files, contract, host, alternate=None):
        candidates = [(contract, files)] if directory is not None else []
        if alternate is not None: candidates.append(alternate)
        if contract["runtime"] == "raw":
            for role in ROLES:
                unit = self.unit(host, role)
                path = self.units / unit
                detail = self.run(["/usr/bin/systemctl", "show", unit, "--property=FragmentPath,DropInPaths"])
                properties = dict(line.split("=", 1) for line in detail.splitlines() if "=" in line)
                fragment = properties.get("FragmentPath", "")
                if (path.exists() or path.is_symlink() or fragment) and (not candidates or path.is_symlink() or not path.is_file()
                        or properties.get("DropInPaths") or fragment not in {"", str(path)} or read_bounded_bytes(path) not in [values[unit] for _, values in candidates]):
                    raise ValueError("Refuse an unowned or changed systemd unit")
        else:
            project = contract["plan"]["name"] + "-" + host
            ids = self.run(self.docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project)).split()
            if ids and not candidates: raise ValueError("Refuse existing unowned Compose containers")
            if candidates:
                seen = set()
                for cid in ids:
                    row = loads_strict_json(self.run(self.docker("inspect", cid)))[0]
                    labels = row.get("Config", {}).get("Labels", {})
                    if (labels.get(renderer.OWNER) != contract["plan"]["name"] or labels.get("platform.gitops/edge-host") != host
                            or labels.get("platform.gitops/edge-contract") not in {sha(values["edge-contract.json"]) for _, values in candidates}
                            or labels.get("com.docker.compose.service") not in ROLES or labels.get("com.docker.compose.service") in seen):
                        raise ValueError("Compose container ownership changed")
                    seen.add(labels["com.docker.compose.service"])
        selected = next(h for h in contract["plan"]["entry_hosts"] if h["name"] == host)
        starter.trusted_paths([Path("/usr/sbin/ip")])
        interfaces = loads_strict_json(self.run(["/usr/sbin/ip", "-j", "address", "show", "dev", selected["interface"]]))
        vip = contract["plan"]["vip"].split("/")[0]
        if any(item.get("local") == vip for row in interfaces for item in row.get("addr_info", [])):
            recognized = False
            for candidate, values in candidates:
                try:
                    release = self.state / "releases" / sha(values["edge-contract.json"])
                    self.verify_role(release, values, candidate, host, "keepalived")
                    recognized = True
                    break
                except (ValueError, OSError): pass
            if not recognized: raise ValueError("An assigned local VIP requires a verified owned daemon; do not adopt stale or unrelated addresses")

    def parse(self, directory, contract, host):
        if contract["runtime"] == "docker":
            self.compose(directory, contract, host, "config", "--quiet")
            return
        for role in ROLES:
            binary = directory / "bin" / role
            starter.verify_daemon_version(binary, role, contract["plan"]["binaries"][role]["version"], CLEAN)
            source = directory / ("haproxy.cfg" if role == "haproxy" else "keepalived-" + host + ".conf")
            # Parser-only copy points at the staged CA, not a previous active CA.
            with tempfile.TemporaryDirectory(prefix="platform-edge-parser-") as temporary:
                check = Path(temporary) / source.name
                config = read_bounded_text(source).replace(renderer.native_ca(contract["plan"]), str(directory / "ca.crt"))
                atomic_write_text(check, config.replace(renderer.BASE, str(directory)))
                self.run([str(binary), *(["-c", "-f", str(check)] if role == "haproxy" else ["--config-test", "--use-file", str(check)])])
        self.run(["/usr/bin/systemd-analyze", "verify", *[str(directory / self.unit(host, role)) for role in ROLES]])

    def service_identity(self):
        import pwd
        try: account = pwd.getpwnam("platform-edge")
        except KeyError:
            starter.trusted_paths([Path("/usr/sbin/useradd")])
            self.run(["/usr/sbin/useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "platform-edge"])
            account = pwd.getpwnam("platform-edge")
        if account.pw_uid == 0 or account.pw_gid == 0: raise ValueError("HAProxy service account must be non-root")

    def stop_vip(self, directory, contract, host):
        if directory is None: return
        if contract["runtime"] == "raw": self.run(["/usr/bin/systemctl", "stop", self.unit(host, "keepalived")])
        else: self.compose(directory, contract, host, "stop", "--timeout", "10", "keepalived")

    def select(self, directory):
        secure_directory(self.active.parent)
        temporary = self.active.with_name(".gateway-edge-" + uuid4().hex)
        temporary.symlink_to(directory, target_is_directory=True)
        os.replace(temporary, self.active)
        _fsync_directory(self.active.parent)

    def activate_role(self, directory, files, contract, host, role):
        if contract["runtime"] == "raw":
            secure_directory(self.units)
            atomic_write_text(self.units / self.unit(host, role), files[self.unit(host, role)].decode(), mode=0o644)
            self.run(["/usr/bin/systemctl", "daemon-reload"])
            self.run(["/usr/bin/systemctl", "enable", self.unit(host, role)])
            self.run(["/usr/bin/systemctl", "restart", self.unit(host, role)])
        else:
            self.compose(directory, contract, host, "up", "-d", "--no-deps", "--pull", "never", "--force-recreate", role)

    def verify_role(self, directory, files, contract, host, role):
        binary = contract["plan"]["binaries"][role]
        if contract["runtime"] == "raw":
            unit = self.unit(host, role)
            if read_bounded_bytes(self.units / unit) != files[unit]: raise ValueError("Installed service unit changed")
            rows = self.run(["/usr/bin/systemctl", "show", unit, "--property=ActiveState,SubState,MainPID,FragmentPath,DropInPaths,NeedDaemonReload"])
            props = dict(line.split("=", 1) for line in rows.splitlines() if "=" in line)
            if (props.get("ActiveState") != "active" or props.get("SubState") != "running" or props.get("DropInPaths")
                    or props.get("FragmentPath") != str(self.units / unit) or props.get("NeedDaemonReload") != "no"):
                raise ValueError("Service is not the current running reviewed unit")
            pid = int(props.get("MainPID", "0"))
        else:
            ids = self.compose(directory, contract, host, "ps", "--all", "--quiet", role).split()
            if len(ids) != 1: raise ValueError("Expected exactly one owned service container")
            row = loads_strict_json(self.run(self.docker("inspect", ids[0])))[0]
            config, hc, status = row.get("Config", {}), row.get("HostConfig", {}), row.get("State", {})
            labels = config.get("Labels", {})
            caps = {str(c).removeprefix("CAP_") for c in hc.get("CapAdd", [])}
            expected_caps = {"NET_BIND_SERVICE"} if role == "haproxy" else set(renderer.KEEPALIVED_CAPABILITIES)
            mounts = {m.get("Destination"): m for m in row.get("Mounts", [])}
            allowed_mounts = {renderer.BASE, "/run/platform/gateway-edge", "/tmp"} | ({"/run/platform-gateway-edge"} if role == "keepalived" else set())
            if (config.get("Image") != contract["plan"]["images"][role] or row.get("Image") != self.image_id(config["Image"])
                    or config.get("User") != ("1000:1000" if role == "haproxy" else "0:0") or config.get("Entrypoint") not in (None, [])
                    or config.get("Cmd") != renderer.command({"name": host}, role, sha(files["edge-contract.json"]))
                    or labels.get("platform.gitops/edge-contract") != sha(files["edge-contract.json"])
                    or labels.get("com.docker.compose.project") != contract["plan"]["name"] + "-" + host or labels.get("com.docker.compose.service") != role
                    or hc.get("NetworkMode") != "host" or hc.get("Privileged") is not False or hc.get("ReadonlyRootfs") is not True
                    or caps != expected_caps or {str(c).removeprefix("CAP_") for c in hc.get("CapDrop", [])} != {"ALL"}
                    or set(hc.get("SecurityOpt", [])) != {"no-new-privileges:true"} or hc.get("PidMode", "") or hc.get("IpcMode") != "private" or hc.get("Devices")
                    or hc.get("RestartPolicy", {}).get("Name") != "unless-stopped" or status.get("Running") is not True
                    or any(status.get(flag) is not False for flag in ("Paused", "Restarting", "Dead"))
                    or status.get("Health", {}).get("Status") != "healthy"
                    or set(mounts) - allowed_mounts or any(m.get("Type") not in {"bind", "tmpfs"} for m in mounts.values())
                    or any(mounts.get(target, {}).get("Type") != "bind" or mounts.get(target, {}).get("Source") != str(directory) or mounts.get(target, {}).get("RW") is not False
                           for target in (renderer.BASE, "/run/platform/gateway-edge"))
                    or any(m.get("Type") != "tmpfs" for target, m in mounts.items() if target not in {renderer.BASE, "/run/platform/gateway-edge"})):
                raise ValueError("Container identity, artifact, readiness or isolation differs")
            pid = int(status.get("Pid", 0))
        if pid <= 1 or sha(read_bounded_bytes(Path("/proc") / str(pid) / "exe", max_bytes=64 * 1024 ** 2)) != binary["sha256"]:
            raise ValueError("Running process does not execute the reviewed daemon")
        if contract["runtime"] == "raw":
            config = directory / ("haproxy.cfg" if role == "haproxy" else "keepalived-" + host + ".conf")
            expected = starter.daemon_arguments(directory / "bin" / role, config, role, contract["plan"])
            command = read_bounded_bytes(Path("/proc") / str(pid) / "cmdline", max_bytes=64 * 1024).rstrip(b"\0").split(b"\0")
            if command != [part.encode() for part in expected]: raise ValueError("Running service still uses a different immutable release")

    def wait_role(self, directory, files, contract, host, role):
        deadline = time.monotonic() + 40
        while True:
            try:
                self.verify_role(directory, files, contract, host, role)
                if not health.healthy(contract["plan"]["health_port"], renderer.edge_identity(contract["plan"])):
                    raise ValueError("Local route health differs from the selected release")
                return
            except (ValueError, OSError):
                if time.monotonic() >= deadline: raise ValueError("Entry service did not reach its reviewed local readiness") from None
                time.sleep(0.5)


def release_files(directory, contract, expected_manifest):
    starter.trusted_paths([directory])
    if any(not (directory / name).is_file() or (directory / name).is_symlink() for name in ("edge-contract.json", "host-files.json")):
        raise ValueError("Retained contract and receipt must be regular files")
    digest = sha(read_bounded_bytes(directory / "edge-contract.json"))
    if digest != directory.name: raise ValueError("Retained contract differs from its recorded release digest")
    starter.load_bundle(directory, digest, contract["host"], check_executing=False)
    document = read_bounded_bytes(directory / "host-files.json")
    if sha(document) != expected_manifest: raise ValueError("Retained release inventory differs from the host receipt")
    manifest = loads_strict_json(document)
    if not isinstance(manifest, dict) or any(not re.fullmatch(r"(?:bin/)?[a-z0-9][a-z0-9_.-]*", k) or not renderer.digest(v) for k, v in manifest.items()):
        raise ValueError("Invalid installed file inventory")
    entries = list(directory.rglob("*"))
    paths = [p for p in entries if not p.is_dir()]
    allowed_dirs = {str(Path(name).parent) for name in manifest if Path(name).parent != Path(".")}
    if (any(p.is_symlink() or p.is_dir() and p.relative_to(directory).as_posix() not in allowed_dirs for p in entries)
            or any(not stat.S_ISREG(p.stat().st_mode) for p in paths) or {p.relative_to(directory).as_posix() for p in paths} != set(manifest) | {"host-files.json"}
            or any(p.stat().st_mode & 0o777 != file_mode(p.relative_to(directory).as_posix()) for p in paths)):
        raise ValueError("Retained file inventory or permissions changed")
    starter.trusted_paths(paths)
    result = {name: read_bounded_bytes(directory / name, max_bytes=64 * 1024 ** 2) for name in manifest}
    if any(sha(value) != manifest[name] for name, value in result.items()): raise ValueError("Retained release changed")
    return result


def validate_state(state, owner):
    fields = {"schema_version", "owner", "stable", "previous", "pending", "releases"}
    if isinstance(state, dict) and "coordination" in state: fields.add("coordination")
    if (not isinstance(state, dict) or set(state) != fields
            or type(state["schema_version"]) is not int or state["schema_version"] != 1 or state["owner"] != owner
            or not isinstance(state["releases"], dict) or any(not renderer.digest(k) or not renderer.digest(v) for k, v in state["releases"].items())
            or any(value is not None and (not renderer.digest(value) or value not in state["releases"]) for value in (state["stable"], state["previous"]))):
        raise ValueError("Host state is invalid or belongs to a different entry")
    pending = state["pending"]
    if pending is not None and (not isinstance(pending, dict) or set(pending) != {"target", "manifest_sha256", "rollback", "attempt"}
            or not renderer.digest(pending["target"]) or not renderer.digest(pending["manifest_sha256"]) or type(pending["rollback"]) is not bool
            or not isinstance(pending["attempt"], str) or not re.fullmatch(r"[0-9a-f]{32}", pending["attempt"])):
        raise ValueError("Invalid pending host operation")
    if "coordination" in state:
        marker = state["coordination"]
        if (not isinstance(marker, dict) or set(marker) != {"operation_id", "plan_sha256", "target"}
                or not isinstance(marker["operation_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", marker["operation_id"])
                or not renderer.digest(marker["plan_sha256"]) or not renderer.digest(marker["target"])):
            raise ValueError("Invalid durable coordination marker")


def pair_reservation(state_directory, owner):
    path = state_directory / "pair-reservation.json"
    if not path.exists() and not path.is_symlink(): return None
    starter.trusted_paths([path])
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("Pair reservation must be a private regular file")
    lease = loads_strict_json(read_bounded_bytes(path, max_bytes=16 * 1024))
    if (not isinstance(lease, dict) or set(lease) != {"schema_version", "operation_id", "plan_sha256", "target", "owner"}
            or type(lease["schema_version"]) is not int or lease["schema_version"] != 1 or lease["owner"] != owner
            or not isinstance(lease["operation_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", lease["operation_id"])
            or not renderer.digest(lease["plan_sha256"]) or not renderer.digest(lease["target"])):
        raise ValueError("Invalid or unrelated pair reservation")
    return lease


def operate(bundle, digest, gateway_bundle, host, *, mode="local", ca_file=None, artifacts=None, expected_current="absent", machine=None, pair_token=None):
    contract, files = capture(bundle, digest, gateway_bundle)
    if host not in {h["name"] for h in contract["plan"]["entry_hosts"]}: raise ValueError("Unknown entry host")
    result = {"local_inputs_verified": True, "read_only": mode in {"local", "check", "verify"}, "activated": False,
              "vip_failover_verified": False, "ha_verified": False, "full_stack_verified": False}
    if mode == "local": return result
    if mode not in {"check", "apply", "resume", "rollback", "verify"}: raise ValueError("Unknown host lifecycle mode")
    machine = machine or Host()
    machine.prerequisites(contract, host)
    if mode == "check":
        values = payloads(contract, files, ca_file, artifacts)
        with tempfile.TemporaryDirectory(prefix="platform-edge-check-") as temporary:
            # Root-owned temporary parent is private, despite /tmp being sticky.
            directory = Path(temporary) / "release"
            directory.mkdir(mode=0o755)
            for name, value in values.items():
                atomic_write_bytes(directory / name, value, mode=file_mode(name), replace=False)
            machine.parse(directory, contract, host)
        return {**result, "host_checked": True}
    if mode == "verify" and not (machine.state / "state.json").is_file(): raise ValueError("No owned host installation to verify")
    secure_directory(machine.state)
    with host_lock(machine.state / "operation.lock"):
        state_path = machine.state / "state.json"
        if state_path.is_symlink(): raise ValueError("Host state cannot be a symlink")
        if state_path.exists():
            starter.trusted_paths([state_path])
            if not state_path.is_file() or state_path.stat().st_mode & 0o777 != 0o600: raise ValueError("Host state must remain a private regular file")
        state = loads_strict_json(read_bounded_bytes(state_path)) if state_path.exists() else None
        owner = {"name": contract["plan"]["name"], "runtime": contract["runtime"], "host": host}
        reservation = pair_reservation(machine.state, owner)
        marker = state.get("coordination") if state else None
        expected_marker = {key: reservation[key] for key in ("operation_id", "plan_sha256", "target")} if reservation else None
        if marker != expected_marker:
            raise ValueError("Interrupted pair reservation metadata requires coordinator recovery")
        if mode in {"apply", "resume", "rollback"} and (reservation is not None or pair_token is not None):
            if reservation is None or pair_token != reservation["operation_id"] or digest != reservation["target"]:
                raise ValueError("Host mutation is reserved by a pair operation")
        if state is not None: validate_state(state, owner)
        current = state.get("stable") if state else None
        pending = state.get("pending") if state else None
        if expected_current != (current or "absent"): raise ValueError("Expected-current digest differs; inspect host state before retry")
        if mode == "resume":
            if not pending or pending.get("target") != digest: raise ValueError("Resume only the exact recorded pending release")
        elif pending: raise ValueError("A partial host operation requires explicit same-release resume")
        if mode == "rollback" and (not state or state.get("previous") != digest): raise ValueError("Rollback requires the retained previous release")
        old_directory = machine.state / "releases" / current if current else None
        old_contract, old_files = None, {}
        if old_directory:
            old_files = release_files(old_directory, owner, state.get("releases", {}).get(current))
            old_contract = loads_strict_json(old_files["edge-contract.json"])
        transition(old_contract, contract, rollback=mode == "rollback" or bool(pending and pending.get("rollback")))
        expected_links = {machine.state / "releases" / d for d in (current, pending.get("target") if pending else None) if d}
        if machine.active.is_symlink():
            if machine.active.readlink() not in expected_links: raise ValueError("Active selector is not owned by the recorded operation")
        elif machine.active.exists() or current: raise ValueError("Active installation is missing or unowned")
        if mode == "verify" or current == digest and mode != "resume":
            if current != digest: raise ValueError("Requested release is not active")
            if contract["runtime"] == "raw":
                trust = machine.state / "trust" / contract["plan"]["ca_sha256"] / "ca.crt"
                starter.trusted_paths([trust])
                if trust.is_symlink() or sha(read_bounded_bytes(trust, max_bytes=256 * 1024)) != contract["plan"]["ca_sha256"]:
                    raise ValueError("Persistent native CA trust differs from the active release")
            machine.ownership(old_directory, old_files, contract, host)
            for role in ROLES: machine.verify_role(old_directory, old_files, contract, host, role)
            if not health.healthy(contract["plan"]["health_port"], renderer.edge_identity(contract["plan"])): raise ValueError("Entry route health failed")
            return {**result, "host_ready": True}
        directory = machine.state / "releases" / digest
        values = payloads(contract, files, ca_file or directory / "ca.crt", artifacts or (directory / "bin" if directory.exists() else None))
        manifest_document = json.dumps({name: sha(value) for name, value in values.items()}, sort_keys=True).encode()
        if mode == "resume" and pending.get("manifest_sha256") != sha(manifest_document): raise ValueError("Resume payload differs from the reserved release")
        machine.ownership(old_directory, old_files, old_contract or contract, host, alternate=(contract, values) if mode == "resume" else None)
        attempt = uuid4().hex
        proof = machine.state / "attempts" / attempt / "proof.json"
        secure_directory(proof.parent); proof.parent.chmod(0o700)
        state = state or {"schema_version": 1, "owner": owner, "stable": None, "previous": None, "releases": {}}
        state["pending"] = {"target": digest, "manifest_sha256": sha(manifest_document), "rollback": mode == "rollback" or bool(pending and pending.get("rollback")), "attempt": attempt}
        atomic_write_text(state_path, json.dumps(state, sort_keys=True))
        journal = {"attempt": attempt, "target": digest, "previous": current, **result}
        def record(phase, **extra):
            journal.update(phase=phase, **extra)
            atomic_write_text(proof, json.dumps(journal, sort_keys=True))
        try:
            record("before-release-staging", write_outcome="unverified")
            install_files(directory, {**values, "host-files.json": manifest_document})
            if contract["runtime"] == "raw":
                install_files(machine.state / "trust" / contract["plan"]["ca_sha256"], {"ca.crt": values["ca.crt"]})
            machine.parse(directory, contract, host)
            # A lost acknowledgment can already have left the desired services
            # running. Verify first on explicit resume instead of restarting them.
            complete = False
            if mode == "resume" and machine.active.is_symlink() and machine.active.readlink() == directory:
                try:
                    machine.ownership(directory, values, contract, host)
                    for role in ROLES: machine.verify_role(directory, values, contract, host, role)
                    complete = health.healthy(contract["plan"]["health_port"], renderer.edge_identity(contract["plan"]))
                except (ValueError, OSError): pass
            if not complete:
                if contract["runtime"] == "raw":
                    record("before-service-identity", write_outcome="unverified"); machine.service_identity()
                record("before-vip-stop", write_outcome="unverified")
                # Compose project identity is fixed across releases; stopping the
                # candidate also handles a partial initial activation on resume.
                machine.stop_vip(directory if mode == "resume" else old_directory, contract if mode == "resume" else old_contract or contract, host)
                record("before-active-selector", write_outcome="unverified"); machine.select(directory)
                for role in ROLES:
                    record("before-service-activation", role=role, write_outcome="unverified")
                    machine.activate_role(directory, values, contract, host, role)
                    machine.wait_role(directory, values, contract, host, role)
                    record("service-ready", role=role, write_outcome="acknowledged")
            state.update(stable=digest, previous=current, pending=None)
            state["releases"][digest] = sha(manifest_document)
            record("before-state-commit", write_outcome="unverified")
            atomic_write_text(state_path, json.dumps(state, sort_keys=True))
            record("completed", activated=True, host_ready=True, write_outcome="acknowledged")
            return {**result, "activated": True, "host_ready": True}
        except BaseException:
            record("failed", failed_at=journal.get("phase"), write_outcome="unverified")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle-dir", "gateway-bundle-dir"): parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True); parser.add_argument("--host", required=True)
    parser.add_argument("--ca-file", type=Path); parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--expected-current", default="absent")
    modes = parser.add_mutually_exclusive_group()
    for flag in ("check-host", "apply", "resume", "rollback", "verify"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    mode = next((name for name in ("apply", "resume", "rollback", "verify") if getattr(args, name)), "check" if args.check_host else "local")
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, args.gateway_bundle_dir, args.host, mode=mode,
            ca_file=args.ca_file, artifacts=args.artifacts_dir, expected_current=args.expected_current)))
        return 0
    except Exception:
        print("Entry host lifecycle stopped. Retain installed releases and private attempt proofs; inspect state before explicit resume or rollback.", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
