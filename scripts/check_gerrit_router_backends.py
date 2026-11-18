#!/usr/bin/env python3
"""Verify Gerrit backend health, router convergence, or public entry traffic."""

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import urlencode

from atomic_file import atomic_write_bytes
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from gerrit_storage import REQUIRED_HEALTHCHECKS
from http_transport import HttpTransportPolicyError, read_bounded_response
from render_gerrit_router import READINESS_PORT, ROUTE_PATH, route_config_id, validate
from strict_json import loads_strict_json


HEALTH_PATH = "/config/server/healthcheck~status"


def ssh_banner(address, port, family):
    deadline = time.monotonic() + 5
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        sock_address = (address, port) if family == socket.AF_INET else (address, port, 0, 0)
        connection.connect(sock_address)
        banner = bytearray()
        while len(banner) < 255 and b"\n" not in banner:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            connection.settimeout(remaining)
            chunk = connection.recv(255 - len(banner))
            if not chunk:
                break
            banner.extend(chunk)
    line, end, _ = banner.partition(b"\n")
    return bool(end and re.fullmatch(rb"SSH-2\.0-[!-~]+(?: [ -~]*)?\r?", line))


def parse_health(status, raw):
    result = {"http_status": status, "health_verified": False, "check_failures": []}
    try:
        if len(raw) > 65536:
            raise ValueError("Health response is too large")
        obj = loads_strict_json(raw.removeprefix(b")]}'\n"))
        if not isinstance(obj, dict):
            raise ValueError("Invalid health response")
    except ValueError:
        result["health_response_invalid"] = True
        return result
    checks = {name: value for name, value in obj.items() if name not in {"ts", "elapsed"}}
    failed = {name for name in set(REQUIRED_HEALTHCHECKS) | set(checks)
              if not isinstance(checks.get(name), dict) or checks[name].get("result") != "passed"}
    result["check_failures"] = sorted(failed.intersection(REQUIRED_HEALTHCHECKS))
    result["additional_checks_failed"] = bool(failed.difference(REQUIRED_HEALTHCHECKS))
    result["health_verified"] = status == 200 and not failed
    return result


def probe(backend, plan, context):
    hostname = plan["canonical_host"]
    result = {"tls_verified": False, "ssh_banner_verified": False, "health_verified": False}
    connection = http.client.HTTPSConnection(hostname, plan["https_port"], timeout=5, context=context)
    peer = None
    try:
        with socket.create_connection((backend, plan["https_port"]), timeout=5) as transport:
            with context.wrap_socket(transport, server_hostname=hostname) as session:
                peer = session.getpeername()[0]
                family = session.family
                result["tls_verified"] = True
                connection.sock = session
                connection.request("GET", HEALTH_PATH, headers={"Host": hostname, "Connection": "close"})
                response = connection.getresponse()
                try:
                    raw = read_bounded_response(response, max_bytes=65536)
                except HttpTransportPolicyError:
                    raw = b""
                result.update(parse_health(response.status, raw))
    except (OSError, http.client.HTTPException):
        return peer, result
    finally:
        connection.close()
    try:
        result["ssh_banner_verified"] = ssh_banner(peer, plan["ssh_port"], family)
    except OSError:
        pass
    return peer, result


def recovery_evidence(value):
    if not isinstance(value, dict) or set(value) != {"username", "changes"}:
        raise ValueError("Expected a reviewer identity and outage changes")
    username, changes = value["username"], value["changes"]
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", username):
        raise ValueError("Invalid Gerrit reader login")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        raise ValueError("Specify 1 to 50 outage change revisions")
    expected = {}
    for row in changes:
        if not isinstance(row, dict) or set(row) != {"number", "revision"}:
            raise ValueError("Invalid outage change record")
        number, revision = row["number"], row["revision"]
        if type(number) is not int or not 1 <= number <= 2147483647 or number in expected:
            raise ValueError("Invalid or duplicate Gerrit change number")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == "0" * 40:
            raise ValueError("Expected an exact nonzero Git revision")
        expected[number] = revision
    return username, expected


def matching_changes(peer, plan, context, auth, expected, *, port=None, certificate_sha256=None, project=None):
    # Require the index to match the commit as well as the change identity.
    # Gerrit may populate response details from shared NoteDb.
    indexed = " OR ".join(f"(change:{number} commit:{revision})" for number, revision in expected.items())
    query = urlencode({"q": indexed,
                       "n": len(expected) + 1, "o": "CURRENT_REVISION"})
    host = plan["canonical_host"]
    if port is None:
        port = plan["https_port"]
    connection = http.client.HTTPSConnection(host, port, timeout=5, context=context)
    try:
        with socket.create_connection((peer, port), timeout=5) as transport:
            with context.wrap_socket(transport, server_hostname=host) as session:
                if certificate_sha256 is not None and (
                    hashlib.sha256(session.getpeercert(binary_form=True)).hexdigest() != certificate_sha256
                ):
                    raise ValueError("The entry did not present the expected Gerrit peer certificate")
                connection.sock = session
                connection.request("GET", "/a/changes/?" + query,
                                   headers={"Host": host, "Authorization": auth, "Connection": "close",
                                            "Accept": "application/json"})
                response = connection.getresponse()
                if response.status != 200:
                    raise ValueError("Gerrit outage query unsuccessful")
                raw = read_bounded_response(response, max_bytes=2 * 1024 * 1024)
    finally:
        connection.close()
    if not raw.startswith(b")]}'\n"):
        raise ValueError("Invalid Gerrit JSON prefix")
    changes = loads_strict_json(raw[5:])
    if not isinstance(changes, list) or len(changes) > len(expected):
        raise ValueError("Unexpected Gerrit search response")
    observed = {}
    for row in changes:
        if not isinstance(row, dict) or row.get("_more_changes"):
            raise ValueError("Incomplete Gerrit search response")
        number = row.get("_number")
        if type(number) is not int or number not in expected or number in observed:
            raise ValueError("Unexpected Gerrit result identity")
        if project is not None and row.get("project") != project:
            raise ValueError("Gerrit returned a different project")
        observed[number] = row.get("current_revision")
    return sum(observed.get(number) == revision for number, revision in expected.items())


def reader_auth(username, token):
    if not isinstance(token, bytes) or not re.fullmatch(rb"[!-~]{16,512}", token):
        raise ValueError("Expected a private Gerrit HTTP credential")
    return "Basic " + base64.b64encode(username.encode("ascii") + b":" + token).decode("ascii")


def tls_context(ca):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=ca.decode("ascii"))
    return context


def check(plan, ca, *, evidence=None, token=None):
    validate(plan)
    if evidence is not None:
        if not plan.get("hold_primary"):
            raise ValueError("Use the hold plan for recovery checks")
        username, expected = recovery_evidence(evidence)
        auth = reader_auth(username, token)
    context = tls_context(ca)
    reports = {}
    peers = {}
    for role in ("primary", "backup"):
        peers[role], reports[role] = probe(plan[role + "_host"], plan, context)
    separated = None not in peers.values() and len(set(peers.values())) == 2
    ready = separated and all(all(row[key] for key in ("tls_verified", "health_verified", "ssh_banner_verified"))
                              for row in reports.values())
    result = {"backends_ready": ready, "distinct_backend_addresses": separated, "backends": reports}
    if evidence is not None:
        matched = {}
        if ready:
            for role, peer in peers.items():
                try:
                    matched[role] = matching_changes(peer, plan, context, auth, expected)
                except (OSError, ValueError, http.client.HTTPException):
                    matched[role] = None
        result.update({"outage_changes_expected": len(expected), "outage_changes_matched": matched,
                       "outage_changes_verified": len(matched) == 2
                       and all(value == len(expected) for value in matched.values())})
    return result


def entry_address(address):
    if not isinstance(address, str) or "%" in address:
        raise ValueError("Expected a direct unicast address")
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified or ip.is_multicast:
        raise ValueError("Expected a direct unicast address")
    return str(ip)


def check_routers(plan, inventory):
    """Check each router's selected role and rendered configuration directly."""
    config_id = route_config_id(plan)
    if not isinstance(inventory, list) or not 2 <= len(inventory) <= 16:
        raise ValueError("Provide 2 to 16 individual router management endpoints")
    endpoints = []
    for row in inventory:
        if not isinstance(row, dict) or set(row) != {"address", "port"}:
            raise ValueError("Expected a direct router address and management port")
        address, port = row["address"], row["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid router endpoint")
        endpoints.append((entry_address(address), port))
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("Duplicate router endpoint")
    role = "backup" if plan.get("hold_primary", False) else "primary"
    expected = f"{config_id} {role}".encode("ascii")
    verified = []
    for address, port in endpoints:
        connection = http.client.HTTPConnection(address, port, timeout=3)
        try:
            connection.request("GET", ROUTE_PATH, headers={"Connection": "close"})
            with connection.getresponse() as response:
                matches = response.status == 200 and read_bounded_response(response, max_bytes=len(expected)) == expected
        except (OSError, http.client.HTTPException, HttpTransportPolicyError):
            matches = False
        finally:
            connection.close()
        verified.append(matches)
    return {"router_rollout_verified": all(verified), "expected_role": role,
            "router_count": len(verified), "routers_matching": sum(verified),
            "routers_not_matching": [index for index, matches in enumerate(verified, 1) if not matches]}


def ssh_patchset(host, address, port, project, username, ref, revision, identity, known_hosts):
    git, ssh = shutil.which("git"), shutil.which("ssh")
    if os.name != "posix" or git is None or ssh is None:
        raise ValueError("The entry check requires Git and OpenSSH on Linux")
    alias = host if port == 22 else f"[{host}]:{port}"
    # OpenSSH performs its own expansion of '%' and '~' in path options.
    # Stage the reviewed bytes under simple names in a private temporary cwd.
    info = identity.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("The private SSH identity must be readable only by its owner")
    key = read_bounded_bytes(identity, max_bytes=65536)
    hosts = read_bounded_bytes(known_hosts, max_bytes=131072)
    with tempfile.TemporaryDirectory(prefix="gerrit-entry-check-") as directory:
        key_path = Path(directory) / "identity"
        atomic_write_bytes(key_path, key, replace=False)
        atomic_write_bytes(Path(directory) / "known_hosts", hosts, replace=False)
        command = shlex.join([
            ssh, "-F", os.devnull, "-i", "./identity", "-o", f"HostName={address}",
            "-o", f"HostKeyAlias={alias}", "-o", "UserKnownHostsFile=./known_hosts",
            "-o", f"GlobalKnownHostsFile={os.devnull}", "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", "-o", "UpdateHostKeys=no",
        ])
        env = {"PATH": os.defpath, "HOME": directory, "XDG_CONFIG_HOME": directory, "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_VARIANT": "ssh",
               "GIT_SSH_COMMAND": command, "GIT_ALLOW_PROTOCOL": "ssh"}
        result = run_bounded(
            [git, "ls-remote", "--exit-code", "--refs", f"ssh://{username}@{host}:{port}/{project}", ref],
            cwd=directory, env=env, input=b"", timeout=15, output_max_bytes=65536,
        )
    return result.returncode == 0 and result.stdout.splitlines() == [f"{revision}\t{ref}".encode("ascii")]


def check_entry(plan, ca, evidence, token, identity, known_hosts):
    validate(plan)
    if not isinstance(evidence, dict) or set(evidence) != {
        "address", "https_port", "ssh_port", "peer_certificate_sha256", "username",
        "project", "number", "patchset", "revision",
    }:
        raise ValueError("Invalid entry evidence")
    address = entry_address(evidence["address"])
    https_port, ssh_port = evidence["https_port"], evidence["ssh_port"]
    if any(type(port) is not int or not 1 <= port <= 65535 for port in (https_port, ssh_port)) or https_port == ssh_port:
        raise ValueError("Expected distinct HTTPS and SSH entry ports")
    project = evidence["project"]
    if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", project) or any(
        not part or part.startswith(".") or part.endswith((".", ".lock")) or ".." in part for part in project.split("/")
    ):
        raise ValueError("Expected an exact Gerrit project name")
    number, patchset, revision = (evidence[key] for key in ("number", "patchset", "revision"))
    username, expected = recovery_evidence({"username": evidence["username"],
                                             "changes": [{"number": number, "revision": revision}]})
    if type(patchset) is not int or not 1 <= patchset <= 2147483647:
        raise ValueError("Expected a positive patchset number")
    pins = evidence["peer_certificate_sha256"]
    if (not isinstance(pins, dict) or set(pins) != {"primary", "backup"}
            or any(not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin) for pin in pins.values())
            or pins["primary"] == pins["backup"]):
        raise ValueError("Pin distinct SHA-256 TLS certificates for the two Gerrit peers")
    auth = reader_auth(username, token)
    context = tls_context(ca)
    role = "backup" if plan.get("hold_primary", False) else "primary"
    ref = f"refs/changes/{number % 100:02d}/{number}/{patchset}"
    try:
        https = matching_changes(address, plan, context, auth, expected, port=https_port,
                                 certificate_sha256=pins[role], project=project) == 1
    except (OSError, ValueError, http.client.HTTPException):
        https = False
    try:
        ssh = ssh_patchset(plan["canonical_host"], address, ssh_port, project, username, ref, revision,
                           identity.resolve(strict=True), known_hosts.resolve(strict=True))
    except (OSError, subprocess.SubprocessError):
        ssh = False
    return {"entry_verified": https and ssh, "expected_https_peer": role,
            "https_peer_and_current_revision_verified": https, "ssh_patchset_verified": ssh}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--backend-ca", type=Path)
    selection.add_argument("--router-inventory", type=Path,
                           help=f"Private list of direct router management endpoints (router port {READINESS_PORT})")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--recovery-evidence", type=Path,
                          help="Private JSON with username and exact outage change revisions")
    recovery.add_argument("--entry-evidence", type=Path, help="Private entry IP, peer pins and patchset evidence")
    parser.add_argument("--reader-token-file", type=Path, help="Private Gerrit HTTP token for the reader")
    parser.add_argument("--ssh-identity", type=Path, help="Private OpenSSH identity for the entry check")
    parser.add_argument("--known-hosts", type=Path, help="Reviewed SSH host keys for the canonical entry hostname")
    args = parser.parse_args(argv)
    evidence_path = args.recovery_evidence or args.entry_evidence
    if bool(evidence_path) != bool(args.reader_token_file) or (evidence_path and args.backend_ca is None):
        parser.error("Outage and entry evidence require a CA and private reader token file")
    if args.entry_evidence:
        if not (args.ssh_identity and args.known_hosts):
            parser.error("The entry check requires both an SSH identity and known-hosts file")
    elif args.ssh_identity or args.known_hosts:
        parser.error("SSH inputs require entry evidence")
    try:
        plan = loads_strict_json(read_bounded_bytes(args.plan, max_bytes=65536))
        if args.router_inventory is not None:
            inventory = loads_strict_json(read_bounded_bytes(args.router_inventory, max_bytes=16384))
            result = check_routers(plan, inventory)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["router_rollout_verified"] else 1
        evidence = None
        token = None
        if evidence_path is not None:
            evidence = loads_strict_json(read_bounded_bytes(evidence_path, max_bytes=16384))
            if not isinstance(evidence, dict):
                raise ValueError("Invalid evidence document")
            token = read_bounded_bytes(args.reader_token_file, max_bytes=514).rstrip(b"\r\n")
        ca = read_bounded_bytes(args.backend_ca, max_bytes=128 * 1024)
        if args.entry_evidence is not None:
            result = check_entry(plan, ca, evidence, token, args.ssh_identity, args.known_hosts)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["entry_verified"] else 1
        result = check(plan, ca, evidence=evidence, token=token)
    except (ValueError, OSError):
        parser.exit(1, "Gerrit check failed. Check the private plan and selected check inputs.\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["backends_ready"] and result.get("outage_changes_verified", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
