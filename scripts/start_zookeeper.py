#!/usr/bin/env python3
"""Check a locked member by default; initialize/start or TLS-probe only explicitly."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json


TLS_FILES = ("client-server.pem", "client-ca.pem", "quorum-server.pem", "quorum-ca.pem",
             "health-client.pem", "health-client.key")
MARKER = ".platform-zookeeper.json"


def read_regular(path: Path, limit: int, *, projected: bool = False) -> bytes:
    if path.is_symlink() and not projected:
        raise ValueError("Locked files must not be symbolic links")
    return read_bounded_bytes(path, max_bytes=limit)


def local_inputs(member: str, expected_hash: str, *, root: Path = Path("/etc/zookeeper"),
                 home: Path = Path("/opt/zookeeper"), verify_artifacts: bool = True) -> dict:
    if (not isinstance(member, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", member)
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ValueError("Declare a member and its reviewed contract digest")
    payload = read_regular(root / (member + "-contract.json"), 65536, projected=True)
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("Member contract differs from the rendered lock")
    contract = loads_strict_json(payload.decode("utf-8"))
    required = {"schema_version", "ensemble_id", "member_id", "members", "version", "data_dir", "host", "client_port", "probe_address", "artifacts", "files_sha256"}
    if (not isinstance(contract, dict) or set(contract) != required or type(contract["schema_version"]) is not int
            or contract["schema_version"] != 1 or type(contract["member_id"]) is not int or contract["member_id"] not in {1, 2, 3}
            or not isinstance(contract["ensemble_id"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", contract["ensemble_id"])
            or not isinstance(contract["version"], str) or not re.fullmatch(r"3[.]9[.][0-9]+", contract["version"])
            or int(contract["version"].split(".")[2]) < 4):
        raise ValueError("Invalid member identity or supported release contract")
    if (not isinstance(contract["data_dir"], str) or not re.fullmatch(r"/(?:srv|var/lib)/[A-Za-z0-9_/-]+", contract["data_dir"])
            or "//" in contract["data_dir"] or contract["data_dir"].endswith("/")
            or not isinstance(contract["host"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", contract["host"])
            or type(contract["client_port"]) is not int or not 1024 <= contract["client_port"] <= 65535):
        raise ValueError("Invalid member address or data path")
    if not isinstance(contract["probe_address"], str):
        raise ValueError("Invalid local probe address")
    address = ipaddress.IPv4Address(contract["probe_address"])
    if not address.is_private or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError("Invalid local probe address")
    peers = contract["members"]
    if (not isinstance(peers, list) or len(peers) != 3
            or any(not isinstance(peer, dict) or set(peer) != {"id", "host", "quorum_port", "election_port"}
                   or type(peer["id"]) is not int or peer["id"] not in {1, 2, 3}
                   or not isinstance(peer["host"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", peer["host"])
                   or any(type(peer[key]) is not int or not 1024 <= peer[key] <= 65535 for key in ("quorum_port", "election_port"))
                   for peer in peers)
            or len({peer["id"] for peer in peers}) != 3 or len({peer["host"] for peer in peers}) != 3
            or not any(peer["id"] == contract["member_id"] and peer["host"] == contract["host"] for peer in peers)):
        raise ValueError("Invalid locked voting membership")
    files = contract["files_sha256"]
    if not isinstance(files, dict) or set(files) != {member + ".cfg", "logback.xml"}:
        raise ValueError("Invalid member configuration lock")
    for name, digest in files.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(read_regular(root / name, 65536, projected=True)).hexdigest() != digest:
            raise ValueError("Member configuration changed")
    artifacts = contract["artifacts"]
    if (not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 256
            or f"zookeeper-{contract['version']}.jar" not in artifacts):
        raise ValueError("Lock the complete reviewed ZooKeeper classpath")
    library = home / "lib"
    if not library.is_dir() or library.is_symlink():
        raise ValueError("Provision the reviewed ZooKeeper library directory")
    actual = {path.name for path in library.iterdir() if path.suffix == ".jar"}
    if actual != set(artifacts):
        raise ValueError("Classpath contains missing or unreviewed jars")
    total = 0
    for name, digest in artifacts.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}[.]jar", name)
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid classpath lock entry")
        if not verify_artifacts:
            continue
        blob = read_regular(library / name, 64 * 1024 ** 2)
        total += len(blob)
        if not blob or total > 512 * 1024 ** 2 or hashlib.sha256(blob).hexdigest() != digest:
            raise ValueError("Classpath content differs from the approved artifacts")
    # Projected Kubernetes Secret keys are symlinks by design. Validate their
    # resolved targets as bounded regular inputs rather than rejecting the mount.
    for name in TLS_FILES:
        metadata = (root / "private" / name).stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4 * 1024 ** 2:
            raise ValueError("Provision bounded regular private TLS input files")
        if os.name == "posix" and name in {"client-server.pem", "quorum-server.pem", "health-client.key"} and metadata.st_mode & 0o007:
            raise ValueError("Private TLS key inputs must not be readable by other users")
    return contract


def identity(contract: dict) -> dict:
    return {key: contract[key] for key in ("ensemble_id", "member_id", "members")}


def state_inputs(contract: dict, *, state: Path, initialize: bool = False) -> bool:
    if not state.is_dir() or state.is_symlink():
        raise ValueError("Provision the dedicated member state directory")
    marker, myid = state / MARKER, state / "myid"
    expected = identity(contract)
    if marker.exists() or marker.is_symlink() or myid.exists() or myid.is_symlink():
        recorded = loads_strict_json(read_regular(marker, 16384).decode("utf-8"))
        if recorded != expected or read_regular(myid, 32) != f"{contract['member_id']}\n".encode():
            raise ValueError("Existing state belongs to another member or ensemble")
        return False
    entries = list(state.iterdir())
    if entries:
        # An otherwise empty newly formatted filesystem can contain lost+found.
        if (len(entries) != 1 or entries[0].name != "lost+found" or entries[0].is_symlink()
                or not entries[0].is_dir() or next(entries[0].iterdir(), None) is not None):
            raise ValueError("Refuse to initialize nonempty or unmanaged state")
    if not initialize:
        return True
    for path, content in ((marker, json.dumps(expected, sort_keys=True) + "\n"), (myid, f"{contract['member_id']}\n")):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    # Interrupted/partial initialization is retained and refused on the next
    # attempt. There is no automatic delete, repair, adoption or member rename.
    return False


def launch(member: str, digest: str, *, initialize: bool = False, root: Path = Path("/etc/zookeeper"),
           home: Path = Path("/opt/zookeeper"), state: Path | None = None) -> None:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("ZooKeeper requires a non-root Linux service identity")
    contract = local_inputs(member, digest, root=root, home=home)
    state = Path(contract["data_dir"]) if state is None else state
    pending = state_inputs(contract, state=state)
    if pending and not initialize:
        raise ValueError("New state needs explicit initialize-if-empty authorization")
    java = shutil.which("java")
    if not java:
        raise ValueError("The reviewed Java runtime is required")
    classpath = os.pathsep.join(str(home / "lib" / name) for name in sorted(contract["artifacts"]))
    env = {key: value for key, value in os.environ.items()
           if key not in {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}}
    # This main class is the same version entry point used by upstream
    # zkServer.sh. No shell startup file or image entrypoint is sourced.
    version = run_bounded([java, "-cp", classpath, "org.apache.zookeeper.version.VersionInfoMain"],
                          timeout=20, text=True, input="", env=env, output_max_bytes=8192, check=False)
    if version.returncode or not re.search(r"(?<![\w.])" + re.escape(contract["version"]) + r"(?:-[0-9a-f]+)?(?:,|\s|$)", version.stdout):
        raise ValueError("Installed ZooKeeper release differs from the locked contract")
    local_inputs(member, digest, root=root, home=home)
    if state_inputs(contract, state=state) and not initialize:
        raise ValueError("State changed before startup")
    state_inputs(contract, state=state, initialize=initialize)
    env["HOME"] = str(state)
    args = [java, "-Xms256m", "-Xmx1024m", "-XX:+ExitOnOutOfMemoryError", "-XX:-UsePerfData",
            "-Dzookeeper.readonlymode.enabled=false", "-Dlogback.configurationFile=" + str(root / "logback.xml"),
            "-cp", classpath, "org.apache.zookeeper.server.quorum.QuorumPeerMain", str(root / (member + ".cfg"))]
    os.execve(java, args, env)


def probe(contract: dict, *, root: Path = Path("/etc/zookeeper")) -> dict:
    # An explicit client context keeps hostname/chain verification required
    # without opting into ambient SSLKEYLOGFILE key logging.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(root / "private" / "client-ca.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Supplying an empty password prevents an interactive OpenSSL prompt when
    # an encrypted key is mistakenly provisioned for this noninteractive probe.
    context.load_cert_chain(str(root / "private" / "health-client.pem"), str(root / "private" / "health-client.key"), password="")
    deadline = time.monotonic() + 5
    # Probe this member, not a Service which might select another member.
    with socket.create_connection((contract["probe_address"], contract["client_port"]), timeout=2) as raw:
        with context.wrap_socket(raw, server_hostname=contract["host"]) as connection:
            connection.sendall(b"srvr")
            chunks = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Member status deadline exceeded")
                connection.settimeout(min(2, remaining))
                data = connection.recv(min(4096, 16385 - size))
                if not data:
                    break
                chunks.append(data)
                size += len(data)
                if size > 16384:
                    raise ValueError("Member status exceeded the capture limit")
    modes = re.findall(r"(?m)^Mode: (leader|follower)\s*$", b"".join(chunks).decode("ascii"))
    if len(modes) != 1:
        raise ValueError("Member is not serving as a quorum leader or follower")
    return {"read_only": True, "tls_status_probe": True, "member_serving": True, "mode": modes[0], "ha_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--member", required=True)
    parser.add_argument("--contract-sha256", required=True)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--start", action="store_true")
    operation.add_argument("--probe", action="store_true")
    parser.add_argument("--initialize-if-empty", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.initialize_if_empty and not args.start:
            raise ValueError("State initialization is allowed only during explicit startup")
        if args.start:
            launch(args.member, args.contract_sha256, initialize=args.initialize_if_empty)
            raise ValueError("ZooKeeper exec returned unexpectedly")
        contract = local_inputs(args.member, args.contract_sha256, verify_artifacts=not args.probe)
        if args.probe:
            report = probe(contract)
        else:
            report = {"read_only": True, "local_inputs_verified": True,
                      "state_initialization_pending": state_inputs(contract, state=Path(contract["data_dir"])),
                      "connections_verified": False, "ha_verified": False}
        print(json.dumps(report))
        return 0
    except (ValueError, OSError, RuntimeError, UnicodeError, subprocess.SubprocessError):
        print("ZooKeeper member check/start stopped; inspect private locks, state identity, TLS inputs and runtime logs.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
