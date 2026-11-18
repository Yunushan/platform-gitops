#!/usr/bin/env python3
"""Create a new private Sigstore TUF bootstrap repository from approved offline keys."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from atomic_file import atomic_write_bytes
from bounded_file import read_bootstrap_bytes
from bounded_subprocess import run_bounded
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
KEY_HEADER = b"-----BEGIN " + b"PRIVATE KEY-----\n"
LIMIT = 1024 * 1024
ROLES = {"root": (3, 2, 366), "targets": (3, 2, 31), "snapshot": (1, 1, 8), "timestamp": (1, 1, 2)}
TARGETS = {"trusted_root.json": None, "rekor.pub": "Rekor", "ctfe.pub": "CTFE", "fulcio.crt.pem": "Fulcio"}


def canonical(value):
    # This constrained bootstrap emits ASCII metadata without floats; compact
    # sorted JSON is the TUF canonical encoding for these values.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def exact(value, names):
    if not isinstance(value, dict) or set(value) != set(names): raise ValueError("Unexpected bootstrap fields")


def read_ref(ref, *, maximum=LIMIT):
    exact(ref, ("path", "sha256"))
    if not isinstance(ref["path"], str): raise ValueError("Use absolute reviewed input paths")
    path = Path(ref["path"])
    if (not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents))
            or not isinstance(ref["sha256"], str) or len(ref["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in ref["sha256"])):
        raise ValueError("Pin unredirected bootstrap inputs")
    raw = read_bootstrap_bytes(path, maximum, follow_symlinks=False)
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]: raise ValueError("Original bootstrap input changed")
    return raw


def validate(plan, *, now=None):
    exact(plan, ("schema_version", "keys", "expires", "targets"))
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported TUF bootstrap plan")
    exact(plan["keys"], ROLES); exact(plan["expires"], ROLES); exact(plan["targets"], TARGETS)
    now = now or datetime.now(timezone.utc)
    for name, (number, _, days) in ROLES.items():
        values = plan["keys"][name]
        if not isinstance(values, list) or len(values) != number: raise ValueError("Require separate reviewed threshold role keys")
        expiration = plan["expires"][name]
        if not isinstance(expiration, str) or len(expiration) != 20 or not expiration.endswith("Z"):
            raise ValueError("Use explicit UTC expiration dates")
        end = datetime.strptime(expiration, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if not 0 < (end - now).total_seconds() <= days * 86400: raise ValueError("Require bounded unexpired role metadata")
    ordered = [plan["expires"][name] for name in ("timestamp", "snapshot", "targets", "root")]
    if ordered != sorted(ordered): raise ValueError("Role expiry must follow the delegated trust chain")
    paths = [ref["path"] for refs in plan["keys"].values() for ref in refs] + [ref["path"] for ref in plan["targets"].values()]
    if len(paths) != len(set(paths)): raise ValueError("Separate every role key and target file")


def command(arguments, directory):
    result = run_bounded(["/usr/bin/openssl", *map(str, arguments)], cwd=directory,
        env={"PATH": "/usr/bin:/bin", "HOME": str(directory), "TMPDIR": str(directory), "LANG": "C", "LC_ALL": "C"},
        input=b"", text=False, timeout=10, output_max_bytes=65536, check=False)
    if result.returncode: raise ValueError("Bounded offline signing operation refused")
    return result.stdout


def require_signer():
    if os.name != "posix" or os.geteuid() == 0: raise ValueError("Use a separate non-root offline signing identity")


def generate(plan):
    require_signer()
    validate(plan)
    target_bytes = {name: read_ref(ref) for name, ref in plan["targets"].items()}
    if any(b"PRIVATE KEY" in raw for raw in target_bytes.values()): raise ValueError("Only public trust targets may be published")
    trust = loads_strict_json(target_bytes["trusted_root.json"])
    if not isinstance(trust, dict) or trust.get("mediaType") != "application/vnd.dev.sigstore.trustedroot+json;version=0.1" or not trust.get("tlogs"):
        raise ValueError("Require an approved Sigstore trusted root")
    for name in ("rekor.pub", "ctfe.pub"):
        if not target_bytes[name].startswith(b"-----BEGIN PUBLIC KEY-----\n"): raise ValueError("Use explicit public log keys")
    if not target_bytes["fulcio.crt.pem"].startswith(b"-----BEGIN CERTIFICATE-----"): raise ValueError("Use an explicit CA trust target")
    with tempfile.TemporaryDirectory(prefix="platform-tuf-offline-") as directory:
        work = Path(directory); work.chmod(0o700)
        public = {}; roles = {}; frozen = {}; used = set()
        for role, (number, threshold, _) in ROLES.items():
            identifiers = []
            for index, ref in enumerate(plan["keys"][role]):
                info = Path(ref["path"]).stat()
                if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                    raise ValueError("Restrict offline signing keys to their signing identity")
                raw = read_ref(ref, maximum=65536)
                if not raw.startswith(KEY_HEADER): raise ValueError("Use approved offline PKCS8 Ed25519 keys")
                key = work / (role + "-" + str(index) + ".pem")
                atomic_write_bytes(key, raw, mode=0o400, replace=False)
                der = command(["pkey", "-in", key, "-pubout", "-outform", "DER"], work)
                if len(der) != 44 or not der.startswith(bytes.fromhex("302a300506032b6570032100")): raise ValueError("Use Ed25519 TUF signing keys")
                value = {"keytype": "ed25519", "scheme": "ed25519", "keyval": {"public": der[-32:].hex()}}
                keyid = hashlib.sha256(canonical(value)).hexdigest()
                if keyid in used: raise ValueError("Role keys must not overlap or repeat")
                used.add(keyid); identifiers.append(keyid); public[keyid] = value; frozen[keyid] = key
            if len(identifiers) != number: raise ValueError("Missing original role key")
            roles[role] = {"keyids": identifiers, "threshold": threshold}
        def signed(role, fields):
            data = {"_type": role, "spec_version": "1.0.31", "version": 1, "expires": plan["expires"][role], **fields}
            path = work / (role + ".signed")
            atomic_write_bytes(path, canonical(data), mode=0o400, replace=False)
            signatures = []
            # Sign with the configured threshold; additional offline keys are
            # retained by their operator rather than copied into the repository.
            for keyid in roles[role]["keyids"][:roles[role]["threshold"]]:
                signature = command(["pkeyutl", "-sign", "-rawin", "-inkey", frozen[keyid], "-in", path], work)
                if len(signature) != 64: raise ValueError("Unexpected metadata signature size")
                signatures.append({"keyid": keyid, "sig": signature.hex()})
            return canonical({"signed": data, "signatures": signatures}) + b"\n"
        root = signed("root", {"consistent_snapshot": False, "keys": public, "roles": roles})
        targets = {}
        for name, raw in target_bytes.items():
            value = {"length": len(raw), "hashes": {"sha256": hashlib.sha256(raw).hexdigest()}}
            if TARGETS[name]: value["custom"] = {"sigstore": {"usage": TARGETS[name], "status": "Active"}}
            targets[name] = value
        target_meta = signed("targets", {"targets": targets})
        def meta(raw): return {"version": 1, "length": len(raw), "hashes": {"sha256": hashlib.sha256(raw).hexdigest()}}
        snapshot = signed("snapshot", {"meta": {"targets.json": meta(target_meta)}})
        timestamp = signed("timestamp", {"meta": {"snapshot.json": meta(snapshot)}})
        # Flat files intentionally: an HTTPS mirror maps only approved metadata
        # names and /targets/<name> to these exact bytes, with no directory walk.
        files = {"root.json": root, "1.root.json": root, "targets.json": target_meta, "snapshot.json": snapshot, "timestamp.json": timestamp, **target_bytes}
        status = {"schema_version": 1, "bootstrap_only": True, "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
            "metadata_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
            "role_keyids": roles, "private_keys_published": False, "tuf_client_acceptance_verified": False,
            "admission_verified": False, "ha_verified": False, "full_stack_verified": False}
        files["tuf-bootstrap-status.json"] = canonical(status) + b"\n"
        return {name: raw.decode() for name, raw in files.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bootstrap_bytes(args.plan, LIMIT, follow_symlinks=False)); validate(plan)
        if not args.create:
            for refs in plan["keys"].values():
                for ref in refs: read_ref(ref, maximum=65536)
            for ref in plan["targets"].values(): read_ref(ref)
            print(json.dumps({"local_inputs_verified": True, "repository_created": False})); return 0
        files = generate(plan)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"repository_created": True, "bootstrap_only": True, "admission_verified": False, "full_stack_verified": False})); return 0
    except Exception:
        print("Private TUF bootstrap stopped; key paths, credentials and signing details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
