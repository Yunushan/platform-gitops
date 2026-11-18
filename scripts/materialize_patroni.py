#!/usr/bin/env python3
"""Check a reviewed Patroni package or explicitly materialize private runtime credentials."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
import postgres_ha_policy as policy
from strict_json import loads_strict_json


PROGRAMS = ("materialize_patroni.py", "postgres_ha_policy.py", "bounded_file.py", "atomic_file.py", "strict_json.py")
STARTUP_PROGRAMS = ("start_patroni.py", "bounded_subprocess.py", "subprocess_timeout.py")
APPLICATION_PROGRAM = "provision_patroni_application.py"


def read_contract(bundle, digest):
    raw = read_bounded_bytes(bundle / "postgres-contract.json", max_bytes=128 * 1024)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest: raise ValueError("PostgreSQL package identity changed")
    contract = loads_strict_json(raw)
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "files_sha256"} or type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise ValueError("Invalid PostgreSQL package")
    policy.validate(contract["plan"], contract["runtime"])
    if contract["runtime"] not in {"docker", "raw"}: raise ValueError("Native materialization requires a Patroni package")
    return contract


def load(bundle, digest, member_name):
    contract = read_contract(bundle, digest)
    if yaml.__version__ != "6.0.3": raise ValueError("Use the reviewed YAML renderer version")
    expected = {"patroni-" + item["name"] + ".template.yaml": yaml.safe_dump(policy.patroni(contract["plan"], item), sort_keys=False).encode() for item in contract["plan"]["members"]}
    for router in contract["plan"].get("write_endpoint", {}).get("routers", []):
        expected[f"postgres-write-{router['name']}.cfg"] = policy.write_router_config(contract["plan"], router).encode()
    inventory = contract["files_sha256"]
    if not isinstance(inventory, dict): raise ValueError("PostgreSQL package inventory differs")
    programs = PROGRAMS + (STARTUP_PROGRAMS if "start_patroni.py" in inventory else ())
    if APPLICATION_PROGRAM in inventory:
        programs += (APPLICATION_PROGRAM,)
    if set(inventory) != set(expected) | set(programs): raise ValueError("PostgreSQL package inventory differs")
    for name in programs:
        executing = read_bounded_text(Path(__file__).parent / name, encoding="utf-8", max_bytes=2 * 1024 ** 2).encode()
        expected[name] = executing
    for name, value in expected.items():
        if hashlib.sha256(value).hexdigest() != contract["files_sha256"][name] or read_bounded_bytes(bundle / name, max_bytes=2 * 1024 ** 2) != value:
            raise ValueError("Runtime program or generated configuration differs")
    members = [item for item in contract["plan"]["members"] if item["name"] == member_name]
    if len(members) != 1: raise ValueError("Select one reviewed database member")
    return contract, policy.patroni(contract["plan"], members[0])


def regular(path, *, private=False):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & (0o077 if private else 0o022): raise ValueError("Untrusted runtime input file")
    if info.st_uid not in {0, os.geteuid()}: raise ValueError("Runtime input has an unexpected owner")


def directory(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022: raise ValueError("Use trusted runtime directories without symlink ancestors")


def tls_identity(inputs, anchors, hostname, *, server_plane, client_plane=None):
    """Verify chain, purpose and hostname with an in-memory TLS handshake."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(inputs / (server_plane + ".crt"), inputs / (server_plane + ".key"))
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.hostname_checks_common_name = False
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    client_context.load_verify_locations(cadata=anchors[server_plane].decode("ascii"))
    if client_plane:
        server_context.verify_mode = ssl.CERT_REQUIRED
        server_context.load_verify_locations(cadata=anchors[client_plane].decode("ascii"))
        client_context.load_cert_chain(inputs / (client_plane + ".crt"), inputs / (client_plane + ".key"))
    cin, cout, sin, sout = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(cin, cout, server_hostname=hostname)
    server = server_context.wrap_bio(sin, sout, server_side=True)
    done, transferred = set(), 0
    for _ in range(32):
        for name, connection in (("client", client), ("server", server)):
            if name in done: continue
            try: connection.do_handshake(); done.add(name)
            except ssl.SSLWantReadError: pass
        for source, destination in ((cout, sin), (sout, cin)):
            while source.pending:
                chunk = source.read(16384); transferred += len(chunk)
                if transferred > 2 * 1024 ** 2: raise ValueError("Excessive runtime certificate handshake")
                destination.write(chunk)
        if len(done) == 2: return server.getpeercert() if client_plane else {}
    raise ValueError("Runtime certificate identity was not verified")


def operate(bundle, digest, member, *, materialize=False, inputs=None, output=None, managed_state=False):
    if type(materialize) is not bool: raise ValueError("Credential materialization must be an explicit boolean choice")
    contract, configuration = load(bundle, digest, member)
    result = {"local_policy_verified": True, "credentials_materialized": False, "runtime_tls_material_verified": False, "deployed": False, "ha_verified": False, "full_stack_verified": False}
    if not materialize: return result
    plan = contract["plan"]
    if sys.platform != "linux" or os.geteuid() != plan["native"]["uid"] or os.getegid() != plan["native"]["gid"]: raise ValueError("Materialize only as the reviewed non-root database identity")
    if (inputs is None or output is None or not inputs.is_absolute() or not output.is_absolute() or inputs != inputs.resolve() or output != output.resolve()
            or output.name != "patroni.yaml" or not str(output).startswith("/run/")): raise ValueError("Select canonical private inputs and a volatile patroni.yaml output")
    directory(bundle); directory(inputs); directory(output.parent)
    if output.parent.stat().st_uid != os.geteuid() or output.parent.stat().st_mode & 0o077: raise ValueError("Runtime credential output needs an owned private directory")
    for name in PROGRAMS:
        regular(bundle / name)
    anchors = {}
    for plane in ("postgres", "rest", "zk"):
        ca, cert, key = (inputs / (plane + suffix) for suffix in ("-ca.crt", ".crt", ".key"))
        regular(ca); regular(cert); regular(key, private=True)
        raw = read_bounded_bytes(ca, max_bytes=256 * 1024)
        if hashlib.sha256(raw).hexdigest() != plan["tls"]["ca_sha256"][plane]: raise ValueError("Runtime trust anchor differs")
        read_bounded_bytes(cert, max_bytes=256 * 1024)
        key_bytes = read_bounded_bytes(
            key,
            max_bytes=64 * 1024,
        )
        if b"ENCRYPTED" in key_bytes: raise ValueError("Provide an approved noninteractive runtime key")
        anchors[plane] = raw
    selected = next(item for item in plan["members"] if item["name"] == member)
    tls_identity(inputs, anchors, selected["host"], server_plane="postgres")
    if "write_endpoint" in plan:
        tls_identity(inputs, anchors, plan["write_endpoint"]["host"], server_plane="postgres")
    tls_identity(inputs, anchors, selected["host"], server_plane="rest", client_plane="rest")
    zk_peer = tls_identity(inputs, anchors, selected["host"], server_plane="rest", client_plane="zk")
    expected_principal = plan["zookeeper"]["principals"][plan["members"].index(selected)]
    if zk_peer.get("subject") != ((("commonName", expected_principal.removeprefix("CN=")),),):
        raise ValueError("DCS certificate identity differs from the reviewed ACL principal")
    passwords = {}
    for role, key in {**policy.PASSWORDS, "rest": policy.REST_PASSWORD}.items():
        path = inputs / key; regular(path, private=True)
        value = read_bounded_text(path, encoding="utf-8", max_bytes=1024).removesuffix("\n")
        if len(value) < 16 or any(char in value for char in ("\r", "\n", "\x00")): raise ValueError("Provision private high-entropy database credentials")
        passwords[role] = value
        if role == "rest":
            configuration["restapi"]["authentication"]["password"] = value
            configuration["ctl"]["authentication"]["password"] = value
        else: configuration["postgresql"]["authentication"][role]["password"] = value
    if len(set(passwords.values())) != len(passwords): raise ValueError("Use distinct privileged, replication and rewind credentials")
    # Point to the exact checked input files, not unverified fixed-path aliases.
    for section in (configuration["restapi"], configuration["ctl"], configuration["zookeeper"], configuration["postgresql"]["parameters"], configuration["bootstrap"]["dcs"]["postgresql"]["parameters"]):
        for key, value in list(section.items()):
            if isinstance(value, str) and value.startswith("/etc/platform-postgres/inputs/"): section[key] = str(inputs / Path(value).name)
    for auth in configuration["postgresql"]["authentication"].values(): auth["sslrootcert"] = str(inputs / "postgres-ca.crt")
    configuration["postgresql"]["pgpass"] = str(output.parent / "pgpass")
    if managed_state:
        configuration["postgresql"]["data_dir"] = str(Path(selected["state_path"]) / "pgdata")
        configuration["postgresql"]["parameters"]["unix_socket_directories"] = str(output.parent)
    payload = yaml.safe_dump(configuration, sort_keys=False)
    if output.exists() or output.is_symlink():
        regular(output, private=True)
        if read_bounded_text(output, encoding="utf-8", max_bytes=256 * 1024) != payload: raise ValueError("Existing runtime credentials differ; review a separate rotation")
    else: atomic_write_text(output, payload)
    return {**result, "credentials_materialized": True, "runtime_tls_material_verified": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--member", required=True); parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--inputs-dir", type=Path); parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, args.member, materialize=args.materialize, inputs=args.inputs_dir, output=args.output_file), sort_keys=True)); return 0
    except Exception:
        print("Patroni materialization refused; credentials and private API details were not printed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
