#!/usr/bin/env python3
"""Activate one fresh Docker/native OpenBao member, leaving ceremony external."""

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
from uuid import uuid4

import activate_keycloak_host as mechanics
from atomic_file import atomic_write_text, _fsync_directory
from bootstrap_keycloak_realm import sources
from bounded_file import read_bounded_bytes
from deploy_keycloak_ha import canonical, fingerprint, lock, sha, uid
import render_openbao_ha as renderer
from http_transport import read_bounded_response
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
FILES = {"tls/tls.crt", "tls/tls.key", "tls/ca.crt"}
UNVERIFIED = ("initialized", "unsealed", "raft_membership_verified", "audit_delivery_verified", "restricted_identities_verified",
              "storage_encryption_verified", "physical_failure_domains_verified", "off_platform_restore_verified", "ha_verified", "full_stack_verified")
LABEL = "platform.gitops/openbao-"


def tools(): return sources(Path(__file__).stem)
def name(plan): return "platform-openbao-" + plan["member"]
def member(contract, plan): return next(m for m in contract["plan"]["members"] if m["name"] == plan["member"])
def labels(state): return {LABEL + "operation": state["operation_id"], LABEL + "plan": state["plan_sha256"], LABEL + "tools": state["toolset_sha256"]}


def package(bundle, digest):
    if not sha(digest): raise ValueError("Pin the exact host package")
    raw = read_bounded_bytes(bundle / "openbao-contract.json", max_bytes=256 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError("The OpenBao contract changed")
    contract = loads_strict_json(raw); renderer.exact(contract, "schema_version runtime plan files_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in {"raw", "docker"}: raise ValueError("Select a native or Docker package")
    files = renderer.render(contract["plan"], contract["runtime"])
    for path, value in files.items():
        if read_bounded_bytes(bundle / path, max_bytes=2 * 1024 ** 2) != value.encode(): raise ValueError("The package differs from current rendering policy")
    return contract


def validate(plan, contract):
    renderer.exact(plan, "schema_version contract_sha256 member machine_id_sha256 boot_id service_uid service_gid files_sha256 storage docker native")
    if (type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or not sha(plan["contract_sha256"]) or not sha(plan["machine_id_sha256"])
            or not uid(plan["boot_id"]) or plan["member"] not in {m["name"] for m in contract["plan"]["members"]}): raise ValueError("Pin the original host, boot and member")
    if any(type(plan[k]) is not int or not 1 <= plan[k] <= 2 ** 31 - 2 for k in ("service_uid", "service_gid")): raise ValueError("Select a non-root service identity")
    if not isinstance(plan["files_sha256"], dict) or set(plan["files_sha256"]) != FILES or not all(sha(v) for v in plan["files_sha256"].values()): raise ValueError("Pin each prepared TLS input")
    renderer.exact(plan["storage"], "data audit")
    for entry in plan["storage"].values():
        renderer.exact(entry, "device inode")
        if any(type(v) is not int or not 1 <= v < 2 ** 64 for v in entry.values()): raise ValueError("Pin original storage directory handles")
    if plan["storage"]["data"] == plan["storage"]["audit"]: raise ValueError("Data and audit directories must differ")
    if contract["runtime"] == "docker":
        pins = plan["docker"]; renderer.exact(pins, "binary socket binary_sha256 daemon_id server_version image_id")
        if plan["native"] is not None or plan["service_uid"] != 1000 or plan["service_gid"] != 1000: raise ValueError("Use the declared numeric Docker identity")
        if (not all(mechanics.local_runtime_path(pins[k]) for k in ("binary", "socket")) or pins["binary"] == pins["socket"] or not sha(pins["binary_sha256"])
                or not isinstance(pins["daemon_id"], str) or not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", pins["daemon_id"])
                or not isinstance(pins["server_version"], str) or not re.fullmatch(r"[2-9][0-9][.][0-9]+[.][0-9]+", pins["server_version"])
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(pins["image_id"]))): raise ValueError("Pin the explicit local Docker engine and image")
    else:
        if plan["docker"] is not None: raise ValueError("Native activation does not select Docker")
        pins = plan["native"]; renderer.exact(pins, "user group systemctl_sha256 max_runtime_seconds")
        if any(not isinstance(pins[k], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", pins[k]) or pins[k] == "root" for k in ("user", "group")) or not sha(pins["systemctl_sha256"]): raise ValueError("Pin the service account and service manager")
        limit = pins["max_runtime_seconds"]
        if limit is not None and (type(limit) is not int or not 60 <= limit <= 600): raise ValueError("Use a persistent member or an explicit finite canary lifetime")
    return plan


def unit_text(contract, plan, state):
    text = renderer.render(contract["plan"], "raw")[plan["member"] + ".service"]
    text = text.replace("User=openbao\n", "User=" + plan["native"]["user"] + "\n").replace("Group=openbao\n", "Group=" + plan["native"]["group"] + "\n")
    limit = plan["native"]["max_runtime_seconds"]
    return ("# openbao-operation=" + state["operation_id"] + "\n# openbao-plan=" + fingerprint(plan) + "\n" + text
        + ("\n[Service]\nRestart=no\nRuntimeMaxSec=" + str(limit) + "\n" if limit is not None else ""))


def mounts(contract, plan):
    selected = contract["plan"]; private = selected["private_dir"] + "/" + plan["member"]
    return [(private + "/config.json", "/etc/openbao/config.json", True), (private + "/tls", "/run/openbao/tls", True),
        (member(contract, plan)["data_dir"], "/var/lib/openbao", False), (selected["audit_dir"], "/var/log/openbao", False)]


def create_arguments(contract, plan, state):
    args = ["container", "create", "--pull=never", "--name", name(plan), "--user=1000:1000", "--network=host", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--memory=2g", "--memory-swap=2g", "--cpus=2", "--pids-limit=512", "--ipc=none", "--cgroupns=private",
        "--userns=host", "--runtime=runc", "--restart=unless-stopped", "--stop-timeout=60", "--workdir=/", "--hostname=" + plan["member"],
        "--no-healthcheck", "--log-driver=json-file", "--log-opt=max-size=10m", "--log-opt=max-file=3", "--entrypoint=/usr/bin/bao"]
    for k,v in sorted(labels(state).items()): args += ["--label", k + "=" + v]
    for source, target, readonly in mounts(contract, plan):
        args += ["--mount", "type=bind,source=" + source + ",destination=" + target + (",readonly" if readonly else "") + ",bind-propagation=rprivate"]
    return [*args, "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777,size=128m", contract["plan"]["image"], "server", "-config=/etc/openbao/config.json"]


def container_policy(row, contract, plan, state, image):
    config, host = row.get("Config", {}), row.get("HostConfig", {})
    if host.get("OomKillDisable") is not None and host.get("OomKillDisable") is not False: raise ValueError("OOM policy changed")
    host = {**host, "OomKillDisable": False}
    if (not mechanics.container_id(row.get("Id")) or state["handle"] is not None and row["Id"] != state["handle"] or row.get("Name") != "/" + name(plan)
            or row.get("Image") != plan["docker"]["image_id"] or config.get("Image") != contract["plan"]["image"] or config.get("User") != "1000:1000"
            or config.get("Entrypoint") != ["/usr/bin/bao"] or config.get("Cmd") != ["server", "-config=/etc/openbao/config.json"]
            or mechanics.environment_map(config.get("Env") or []) != mechanics.environment_map(image.get("Config", {}).get("Env") or [])
            or config.get("Labels") != {**(image.get("Config", {}).get("Labels") or {}), **labels(state)} or config.get("WorkingDir") != "/"
            or config.get("Hostname") != plan["member"] or config.get("StopTimeout") != 60 or config.get("Tty") is not False or config.get("OpenStdin") is not False
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]): raise ValueError("Original Docker command or identity changed")
    expected = {"ReadonlyRootfs": True, "Privileged": False, "OomKillDisable": False, "NetworkMode": "host", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host",
        "Runtime": "runc", "PidsLimit": 512, "Memory": 2 * 1024 ** 3, "MemorySwap": 2 * 1024 ** 3, "NanoCpus": 2000000000, "AutoRemove": False,
        "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}, "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}}
    if (any(host.get(k) != v for k,v in expected.items()) or host.get("CapDrop") != ["ALL"] or host.get("SecurityOpt") not in (["no-new-privileges:true"], ["no-new-privileges"])
            or any(host.get(k) for k in ("CapAdd", "Binds", "VolumesFrom", "Devices", "DeviceRequests", "DeviceCgroupRules", "GroupAdd", "Links", "PortBindings", "PublishAllPorts", "PidMode", "UTSMode", "ExtraHosts", "Sysctls", "Init"))
            or host.get("Tmpfs") != {"/tmp": "rw,nosuid,nodev,noexec,mode=1777,size=128m"}): raise ValueError("Original Docker isolation changed")
    expected_mounts = {target: (source, not readonly) for source,target,readonly in mounts(contract, plan)}
    actual = row.get("Mounts", [])
    if (len(actual) != 4 or {m.get("Destination") for m in actual} != set(expected_mounts) or len(host.get("Mounts", [])) != 4
            or any(m.get("Type") != "bind" or (m.get("Source"), m.get("RW")) != expected_mounts[m["Destination"]] or m.get("Propagation") != "rprivate" for m in actual)):
        raise ValueError("Original Docker prepared mounts changed")
    return fingerprint({"Config": config, "HostConfig": host, "Mounts": sorted(actual, key=lambda m: m["Destination"]), "Image": row["Image"]})


class Host(mechanics.Host):
    def unit_path(self, plan): return Path("/etc/systemd/system") / (name(plan) + ".service")
    def properties(self, plan):
        raw = self.run(["/usr/bin/systemctl", "show", name(plan) + ".service", "--property=LoadState,FragmentPath,DropInPaths,ActiveState,SubState,MainPID,InvocationID,NRestarts,NeedDaemonReload,ControlGroup"])
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def prerequisites(self, contract, plan, *, starting=False):
        if sys.platform != "linux" or os.geteuid() != 0: raise ValueError("Use the explicitly selected root Linux host")
        for module in (Path(sys.executable).resolve(strict=True), *Path(__file__).parent.glob("*.py")): mechanics.trusted(module)
        machine = read_bounded_bytes(Path("/etc/machine-id"), max_bytes=128).decode().strip()
        boot = read_bounded_bytes(Path("/proc/sys/kernel/random/boot_id"), max_bytes=128).decode().strip()
        if hashlib.sha256(machine.encode()).hexdigest() != plan["machine_id_sha256"] or boot != plan["boot_id"]: raise ValueError("The original machine or boot changed")
        selected = contract["plan"]; chosen = member(contract, plan); private = Path(selected["private_dir"]) / plan["member"]
        ip = Path("/usr/sbin/ip").resolve(strict=True); mechanics.trusted(ip)
        rows = loads_strict_json(self.run([str(ip), "-j", "-4", "address", "show"]))
        if sum(a.get("local") == chosen["address"] for row in rows for a in row.get("addr_info", [])) != 1: raise ValueError("The selected member IP is not uniquely local")
        if starting:
            for port in (8200, 8201):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation: reservation.bind((chosen["address"], port))
        mechanics.trusted(private, directory=True); mechanics.trusted(private / "tls", directory=True)
        if {p.name for p in (private / "tls").iterdir()} != {"tls.crt", "tls.key", "ca.crt"}: raise ValueError("The TLS mount inventory changed")
        for filename, digest in plan["files_sha256"].items():
            path = private / filename; info = mechanics.trusted(path)
            if filename == "tls/tls.key" and (info.st_gid != plan["service_gid"] or info.st_mode & 0o777 not in {0o440, 0o640}): raise ValueError("Use a private root-owned service-group-readable TLS key")
            if info.st_size > 128 * 1024 or mechanics.file_hash(path, maximum=128 * 1024) != digest: raise ValueError("A prepared TLS input changed")
            mechanics.readable(path, plan, boundary=private / "tls" if contract["runtime"] == "docker" else None)
        config = private / "config.json"; mechanics.trusted(config); mechanics.readable(config, plan, boundary=config if contract["runtime"] == "docker" else None)
        expected = (json.dumps(renderer.configuration(selected, chosen, contract["runtime"]), sort_keys=True, indent=2) + "\n").encode()
        if read_bounded_bytes(config, max_bytes=128 * 1024) != expected: raise ValueError("Prepared configuration differs from the original package")
        ssl.create_default_context(cadata=read_bounded_bytes(private / "tls/ca.crt", max_bytes=128 * 1024).decode("ascii"))
        for kind, path in (("data", Path(chosen["data_dir"])), ("audit", Path(selected["audit_dir"]))):
            info = mechanics.trusted(path, owner=plan["service_uid"], private=True, directory=True)
            if (info.st_gid != plan["service_gid"] or info.st_mode & 0o777 != 0o700 or {"device": info.st_dev, "inode": info.st_ino} != plan["storage"][kind]
                    or starting and any(path.iterdir())): raise ValueError("Storage identity, private access or fresh-state scope changed")
        result = {"machine_id_sha256": plan["machine_id_sha256"], "boot_id": boot, "storage": plan["storage"]}
        if contract["runtime"] == "docker":
            pins = plan["docker"]
            if (self.binary, self.socket) != mechanics.docker_paths(pins) or mechanics.file_hash(Path(self.binary)) != pins["binary_sha256"]: raise ValueError("The local Docker transport changed")
            sock, info = mechanics.protected_socket(self.socket); facts = self.docker_facts()
            version = loads_strict_json(self.docker("version", "--format", '{{json .Server.Version}}'))
            if (facts.get("id") != pins["daemon_id"] or version != pins["server_version"] or facts.get("os") != "linux" or facts.get("runtime") != "runc"
                    or not isinstance(facts.get("security"), list) or any("rootless" in s or "userns" in s for s in facts["security"])
                    or not any("name=seccomp" in s and "profile=builtin" in s for s in facts["security"]) or facts.get("cgroup") != "2"
                    or any(facts.get(k) is not True for k in ("memory", "swap", "cpu_period", "cpu_quota", "pids"))): raise ValueError("The reviewed Docker engine or resource enforcement changed")
            images = loads_strict_json(self.docker("image", "inspect", selected["image"]))
            if (len(images) != 1 or images[0].get("Id") != pins["image_id"] or selected["image"] not in images[0].get("RepoDigests", [])
                    or images[0].get("Os") != "linux" or images[0].get("Architecture") != {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine)
                    or images[0].get("Config", {}).get("Volumes")): raise ValueError("Preload the exact reviewed image without anonymous volumes")
            result.update(socket_path=str(sock), socket_device=info.st_dev, socket_inode=info.st_ino, image={k: images[0][k] for k in ("Id", "Config")})
        else:
            import pwd
            import grp
            pins = plan["native"]; account = pwd.getpwnam(pins["user"])
            if account.pw_uid != plan["service_uid"] or account.pw_gid != plan["service_gid"] or grp.getgrnam(pins["group"]).gr_gid != plan["service_gid"]: raise ValueError("Native account identity changed")
            binary = Path(selected["binary"]); mechanics.readable(binary, plan, executable=True)
            if mechanics.file_hash(binary) != selected["binary_sha256"] or mechanics.file_hash(Path("/usr/bin/systemctl")) != pins["systemctl_sha256"]: raise ValueError("A reviewed executable changed")
            if not re.fullmatch(r"OpenBao v" + re.escape(selected["version"]) + r"(?:\s|$).*", self.run([str(binary), "version"]).strip(), re.S): raise ValueError("The native release differs")
            checksum = private / "binary.sha256"; mechanics.trusted(checksum); mechanics.readable(checksum, plan)
            if read_bounded_bytes(checksum, max_bytes=4096).decode() != selected["binary_sha256"] + "  " + selected["binary"] + "\n": raise ValueError("The native executable commitment differs")
        return result

    def existing(self, contract, plan):
        if contract["runtime"] == "docker":
            ids = self.docker("container", "ls", "--all", "--no-trunc", "--filter", "name=^/" + name(plan) + "$", "--format", '{{.ID}}').split()
            if len(ids) > 1 or any(not mechanics.container_id(i) for i in ids): raise ValueError("Ambiguous original container")
            return ids[0] if ids else None
        path = self.unit_path(plan)
        return "existing-unit" if path.exists() or path.is_symlink() or self.properties(plan).get("LoadState") != "not-found" else None

    def create(self, contract, plan, state):
        if contract["runtime"] == "docker":
            identity = self.docker(*create_arguments(contract, plan, state)).strip()
            if not mechanics.container_id(identity): raise ValueError("The original container was not acknowledged")
            return identity
        path = self.unit_path(plan); mechanics.trusted(path.parent, directory=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644); os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(unit_text(contract, plan, state)); stream.flush(); os.fsync(stream.fileno())
        _fsync_directory(path.parent)

    def inspect(self, contract, plan, state, facts):
        if contract["runtime"] == "docker":
            rows = loads_strict_json(self.docker("container", "inspect", name(plan)))
            if len(rows) != 1: raise ValueError("The original container is absent")
            row = rows[0]; policy = container_policy(row, contract, plan, state, facts["image"]); live = row.get("State", {})
            if live.get("Dead") or live.get("OOMKilled") or live.get("Error") or live.get("Status") not in {"created", "running"} or live.get("Running") is not (live.get("Status") == "running"):
                raise ValueError("The original container failed")
            active = live.get("Running") is True; invocation = live.get("StartedAt") if active else None; pid = live.get("Pid") if active else None
            if active and (not isinstance(invocation, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", invocation) or datetime.fromisoformat(invocation.replace("Z", "+00:00")).year < 2020): raise ValueError("Invalid original container invocation")
            cgroup = mechanics.process_cgroup(pid) if active else None
            if cgroup is not None and row["Id"] not in cgroup: raise ValueError("The container escaped its original cgroup")
            return {"handle": row["Id"], "policy_sha256": policy, "active": active, "invocation": invocation, "loaded": True, "pid": pid, "cgroup": cgroup}
        path = self.unit_path(plan); info = mechanics.trusted(path); expected = unit_text(contract, plan, state).encode()
        if read_bounded_bytes(path, max_bytes=64 * 1024) != expected or info.st_mode & 0o777 != 0o644: raise ValueError("The original unit changed")
        props = self.properties(plan)
        if props.get("DropInPaths") or props.get("FragmentPath") not in {"", str(path)} or props.get("ActiveState") in {"failed", "deactivating"}: raise ValueError("Original unit ownership or state changed")
        active = props.get("ActiveState") == "active" and props.get("SubState") == "running" and str(props.get("MainPID", "")).isdigit() and int(props["MainPID"]) > 1
        invocation = props.get("InvocationID") or None; cgroup = props.get("ControlGroup") or None
        if invocation is not None and not re.fullmatch(r"[0-9a-f]{32}", invocation): raise ValueError("Invalid service invocation")
        if active and (not cgroup or not cgroup.endswith("/" + name(plan) + ".service") or mechanics.process_cgroup(int(props["MainPID"])) != cgroup): raise ValueError("The service escaped its original cgroup")
        return {"handle": fingerprint({"device": info.st_dev, "inode": info.st_ino}), "policy_sha256": hashlib.sha256(expected).hexdigest(), "active": active,
            "invocation": invocation, "loaded": props.get("LoadState") == "loaded" and props.get("FragmentPath") == str(path) and props.get("NeedDaemonReload") == "no",
            "pid": int(props["MainPID"]) if active else None, "cgroup": cgroup}

    def start(self, contract, plan, state):
        if contract["runtime"] == "docker": self.docker("container", "start", state["handle"])
        else: self.run(["/usr/bin/systemctl", "start", "--no-block", name(plan) + ".service"])

    def healthy(self, contract, plan, original):
        address = member(contract, plan)["address"]
        if not mechanics.owns_listener(original, address, 8200): return False
        ca = Path(contract["plan"]["private_dir"]) / plan["member"] / "tls/ca.crt"
        connection = http.client.HTTPSConnection(address, 8200, context=ssl.create_default_context(cadata=read_bounded_bytes(ca, max_bytes=128 * 1024).decode("ascii")), timeout=5)
        try:
            connection.request("GET", "/v1/sys/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 501: return False
            body = loads_strict_json(read_bounded_response(response, max_bytes=64 * 1024))
            return body.get("initialized") is False and body.get("sealed") is True and body.get("version") == contract["plan"]["version"]
        except (OSError, ValueError, http.client.HTTPException): return False
        finally: connection.close()


def operate(bundle, plan, *, mode="local", output=None, host=None, wait_seconds=120):
    contract = package(bundle, plan.get("contract_sha256")); validate(plan, contract)
    result = {"local_inputs_verified": True, "host_checked": False, "original_member_active": False, "tls_serving_uninitialized_verified": False,
        "requires_external_ceremony": True, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"check-host", "activate", "resume"} or type(wait_seconds) is not int or not 5 <= wait_seconds <= 600: raise ValueError("Select a bounded explicit host operation")
    backend = host or Host(plan["docker"]); facts = backend.prerequisites(contract, plan, starting=mode in {"check-host", "activate"}); source = tools()
    if mode == "check-host":
        if backend.existing(contract, plan) is not None: raise ValueError("Existing members require their original operation")
        return {**result, "host_checked": True}
    if output is None or output.is_symlink(): raise ValueError("Select the original private operation directory")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts: raise ValueError("Use a member-specific private directory")
    if mode == "activate": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume requires the original private journal")
    receipt = directory / "openbao-host-proof.json"
    with lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("The original journal cannot redirect")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("plan_sha256") != fingerprint(plan) or state.get("toolset_sha256") != source
                    or not uid(state.get("operation_id")) or state.get("facts_sha256") != fingerprint(facts) or any(state.get(k) is not False for k in UNVERIFIED)
                    or any(type(state.get(k)) is not bool for k in ("create_intent", "reload_intent", "start_intent")) or state["start_intent"] and not state["create_intent"]
                    or state["reload_intent"] and not state["create_intent"] or contract["runtime"] == "docker" and state["reload_intent"] or contract["runtime"] == "raw" and state["start_intent"] and not state["reload_intent"]
                    or state.get("handle") is not None and not sha(state["handle"]) or state.get("policy_sha256") is not None and not sha(state["policy_sha256"])): raise ValueError("Resume only the original invocation and inputs")
        else:
            if backend.existing(contract, plan) is not None: raise ValueError("Never adopt or replace an existing member")
            state = {"schema_version": 1, "operation_id": str(uuid4()), "plan_sha256": fingerprint(plan), "toolset_sha256": source, "facts_sha256": fingerprint(facts),
                "create_intent": False, "reload_intent": False, "start_intent": False, "handle": None, "policy_sha256": None, "invocation": None, **result}
        wrote = False
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        def unchanged():
            if tools() != source or fingerprint(backend.prerequisites(contract, plan, starting=not state["start_intent"])) != state["facts_sha256"]: raise ValueError("Prepared inputs changed")
        def inspect():
            observed = backend.inspect(contract, plan, state, facts)
            if (state["handle"] is not None and observed["handle"] != state["handle"] or state["policy_sha256"] is not None and observed["policy_sha256"] != state["policy_sha256"]
                    or state["invocation"] is not None and observed["invocation"] != state["invocation"]): raise ValueError("The original member was replaced or restarted")
            state.update(handle=observed["handle"], policy_sha256=observed["policy_sha256"])
            if observed["active"]:
                if observed["invocation"] is None: raise ValueError("The original active member lacks its invocation identity")
                state["invocation"] = observed["invocation"]
            return observed
        try:
            record("prepared", **result)
            if not state["create_intent"]:
                unchanged()
                if backend.existing(contract, plan) is not None: raise ValueError("An unrelated member appeared")
                state["create_intent"] = True; wrote = True; record("before-create", read_only=False); backend.create(contract, plan, state)
            original = inspect(); record("original-created")
            if original["active"] and not state["start_intent"]: raise ValueError("A member started outside its original operation")
            if contract["runtime"] == "raw" and not state["reload_intent"]:
                unchanged(); state["reload_intent"] = True; wrote = True; record("before-reload", read_only=False); backend.reload()
            original = inspect()
            if not original["loaded"]: raise ValueError("Observe the uncertain original reload; do not repeat it")
            if not state["start_intent"]:
                unchanged(); state["start_intent"] = True; wrote = True; record("before-start", read_only=False); backend.start(contract, plan, state)
            deadline = time.monotonic() + wait_seconds
            while True:
                original = inspect(); record("observing-original-member")
                if original["active"] and backend.healthy(contract, plan, original): break
                if time.monotonic() >= deadline: raise TimeoutError("Observe the same original member; do not repeat uncertain start")
                time.sleep(1)
            unchanged(); original = inspect()
            if not original["active"] or not backend.healthy(contract, plan, original): raise ValueError("The original member lost its uninitialized TLS state")
            result.update(host_checked=True, original_member_active=True, tls_serving_uninitialized_verified=True, read_only=not wrote)
            record("awaiting-external-ceremony", **result); return result
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="openbao-host-activation-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--host-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=120); group = parser.add_mutually_exclusive_group()
    for mode in ("check-host", "activate", "resume"): group.add_argument("--" + mode, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.host_plan, max_bytes=128 * 1024)); mode = next((m.replace("_", "-") for m in ("check_host", "activate", "resume") if getattr(args,m)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("OpenBao host activation stopped; preserve the private journal and original handles. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
