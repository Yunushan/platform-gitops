#!/usr/bin/env python3
"""Explicit read-only Cosign verification with pinned tool, trust and pull token."""

import argparse
import base64
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit

from atomic_file import atomic_binary_output, atomic_write_bytes
from bounded_file import read_bootstrap_bytes
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
COSIGN_VERSION = "v3.1.4"
COSIGN_SHA256 = "424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa"
LIMIT = 1024 * 1024
PREDICATE = "https://sigstore.dev/cosign/sign/v1"
HEX = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"([a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?)/([a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)@sha256:([0-9a-f]{64})")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("Unexpected signature verification fields")


def https_origin(value):
    if not isinstance(value, str) or len(value) > 512: raise ValueError("Bound the trust endpoint")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/") or any(ord(c) <= 32 or ord(c) > 126 for c in value)
            or parsed.port is not None and not 1 <= parsed.port <= 65535):
        raise ValueError("Use an explicit HTTPS trust origin")
    return parsed



def validate_annotations(value):
    # Cosign's string-slice flag also parses CSV: reject commas/controls rather
    # than allowing one approved value to introduce another flag entry.
    if not isinstance(value, dict) or not 1 <= len(value) <= 16:
        raise ValueError("Require bounded explicit signed annotations")
    for key, item in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", key)
                or not isinstance(item, str) or not 1 <= len(item) <= 2048
                or any(ord(c) < 33 or ord(c) > 126 or c in ",=\\\"" for c in item)):
            raise ValueError("Use unambiguous signed annotation values")
    return value

def validate(plan):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (1, 2):
        raise ValueError("Unsupported signature verification plan")
    exact(plan, "schema_version image cosign_binary registry_ca public_key trusted_root credential" + (" annotations" if plan["schema_version"] == 2 else ""))
    if plan["schema_version"] == 2: validate_annotations(plan["annotations"])
    image = plan["image"]
    match = IMAGE.fullmatch(image) if isinstance(image, str) and len(image) <= 1024 else None
    if match is None: raise ValueError("Pin one canonical registry image digest")
    https_origin("https://" + match[1])
    paths = set()
    for name in ("cosign_binary", "registry_ca", "public_key", "trusted_root", "credential"):
        ref = plan[name]; exact(ref, "path sha256")
        if not isinstance(ref["path"], str) or not Path(ref["path"]).is_absolute() or ".." in Path(ref["path"]).parts:
            raise ValueError("Use explicit absolute input paths")
        if ref["path"] in paths: raise ValueError("Separate tool, trust and credential inputs")
        paths.add(ref["path"])
        if not isinstance(ref["sha256"], str) or not HEX.fullmatch(ref["sha256"]): raise ValueError("Pin every input digest")
    if plan["cosign_binary"]["sha256"] != COSIGN_SHA256: raise ValueError("Use the reviewed Cosign release")
    return match.groups()


def small_input(ref, limit=LIMIT):
    path = Path(ref["path"])
    if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError("Refuse redirected verification inputs")
    raw = read_bootstrap_bytes(path, limit, follow_symlinks=False)
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]: raise ValueError("Original verification input changed")
    return raw


def pull_token(raw, registry, repository):
    item = loads_strict_json(raw); exact(item, "registry repository token")
    if item["registry"] != registry or item["repository"] != repository: raise ValueError("Pull credential scope differs")
    value = item["token"]
    if not isinstance(value, str) or len(value) > 32768 or not re.fullmatch(r"[A-Za-z0-9_-]+[.][A-Za-z0-9_-]+[.][A-Za-z0-9_-]+", value):
        raise ValueError("Use a bounded Harbor pull token")
    header, claims = [loads_strict_json(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))) for s in value.split(".")[:2]]
    if (not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256" or claims.get("aud") not in ("harbor-registry", ["harbor-registry"])
            or claims.get("access") != [{"type": "repository", "name": repository, "actions": ["pull"]}]
            or type(claims.get("exp")) is not int or not time.time() < claims["exp"] <= time.time() + 86400
            or "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > time.time() + 5)):
        raise ValueError("Require a current repository pull-only token")
    return value


def inspect(plan):
    registry, repository, _ = validate(plan)
    copy_tool(plan["cosign_binary"])
    inputs = {name: small_input(plan[name]) for name in ("registry_ca", "public_key", "trusted_root", "credential")}
    key = inputs["public_key"]
    if not key.startswith(b"-----BEGIN PUBLIC KEY-----\n") or b"PRIVATE KEY" in key or len(key) > 65536:
        raise ValueError("Use an explicit public release key")
    if not inputs["registry_ca"].startswith(b"-----BEGIN CERTIFICATE-----"): raise ValueError("Use an explicit registry CA")
    trust = loads_strict_json(inputs["trusted_root"])
    if not isinstance(trust, dict) or trust.get("mediaType") != "application/vnd.dev.sigstore.trustedroot+json;version=0.1":
        raise ValueError("Use the reviewed Sigstore trusted root")
    logs = trust.get("tlogs")
    if not isinstance(logs, list) or not 1 <= len(logs) <= 8: raise ValueError("Require bounded transparency-log trust")
    for log in logs:
        if not isinstance(log, dict) or not isinstance(log.get("publicKey"), dict): raise ValueError("Require explicit log keys")
        https_origin(log.get("baseUrl"))
    pull_token(inputs["credential"], registry, repository)
    return inputs


def copy_tool(ref, target=None):
    source = Path(ref["path"])
    if any(p.is_symlink() for p in (source, *source.parents)): raise ValueError("Refuse redirected tool input")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256(); total = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 240 * LIMIT: raise ValueError("Bound the pinned tool")
        staged_output = atomic_binary_output(target, mode=0o500, replace=False) if target is not None else nullcontext()
        with os.fdopen(descriptor, "rb", closefd=False) as incoming, staged_output as output:
            while block := incoming.read(LIMIT):
                total += len(block)
                if total > 240 * LIMIT: raise ValueError("Tool exceeded its bound")
                digest.update(block)
                if output is not None: output.write(block)
            if total != info.st_size or digest.hexdigest() != COSIGN_SHA256:
                raise ValueError("Pinned Cosign executable changed")
    finally:
        os.close(descriptor)


def checked_output(raw, image, annotations=None):
    if annotations is not None: validate_annotations(annotations)
    rows = loads_strict_json(raw)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32: raise ValueError("No bounded verified image payload")
    for row in rows:
        if not isinstance(row, dict): raise ValueError("Invalid verified payload")
        critical = row.get("critical")
        if (not isinstance(critical, dict) or critical.get("type") != PREDICATE
                or critical.get("identity") != {"docker-reference": image}
                or critical.get("image") != {"docker-manifest-digest": image.rsplit("@", 1)[1]}):
            raise ValueError("Verified payload differs from the selected image")
        if annotations is not None:
            optional = row.get("optional")
            if not isinstance(optional, dict) or any(optional.get(k) != v for k, v in annotations.items()):
                raise ValueError("Verified signature differs from the required signed annotations")
    return rows


def require_nonroot():
    if os.name != "posix" or os.geteuid() == 0: raise ValueError("Use a separate non-root Linux verification identity")


def operate(plan):
    require_nonroot()
    inputs = inspect(plan)
    registry, repository, _ = validate(plan)
    with tempfile.TemporaryDirectory(prefix="platform-signature-verify-") as directory:
        root = Path(directory); root.chmod(0o700)
        for name in ("registry_ca", "public_key", "trusted_root"):
            atomic_write_bytes(root / name, inputs[name], mode=0o400, replace=False)
        auth = {"auths": {registry: {"registrytoken": pull_token(inputs["credential"], registry, repository)}}}
        atomic_write_bytes(root / "config.json", canonical(auth), mode=0o400, replace=False)
        binary = root / "cosign"; copy_tool(plan["cosign_binary"], binary)
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root), "DOCKER_CONFIG": str(root), "LANG": "C", "LC_ALL": "C"}
        arguments = [str(binary), "verify", "--key", str(root / "public_key"), "--trusted-root", str(root / "trusted_root"),
            "--registry-cacert", str(root / "registry_ca"), "--insecure-ignore-tlog=false", "--insecure-ignore-sct=false",
            "--output=json"]
        for key, value in sorted(plan.get("annotations", {}).items()): arguments += ["--annotations", key + "=" + value]
        arguments.append(plan["image"])
        try:
            result = run_bounded(arguments, cwd=root, env=environment, text=True, timeout=90, output_max_bytes=LIMIT, check=False)
        except Exception:
            raise ValueError("Bounded image signature verification failed") from None
        if result.returncode: raise ValueError("Image signature verification refused")
        rows = checked_output(result.stdout, plan["image"], plan.get("annotations"))
        return {"schema_version": 1, "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(), "image": plan["image"],
            "cosign_version": COSIGN_VERSION, "cosign_sha256": COSIGN_SHA256, "public_key_sha256": plan["public_key"]["sha256"],
            "trusted_root_sha256": plan["trusted_root"]["sha256"], "registry_ca_sha256": plan["registry_ca"]["sha256"],
            "verified_payload_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
            "image_signature_verified": True, "transparency_inclusion_verified": True, "registry_tls_verified": True,
            "signed_annotations_verified": plan["schema_version"] == 2,
            "signed_annotations_sha256": hashlib.sha256(canonical(plan["annotations"])).hexdigest() if plan["schema_version"] == 2 else None,
            "admission_verified": False, "ci_evidence_verified": False, "release_authorization_verified": False,
            "ha_verified": False, "full_stack_verified": False, "verified_at": datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bootstrap_bytes(args.plan, LIMIT, follow_symlinks=False))
        if not args.verify:
            inspect(plan)
            print(json.dumps({"local_inputs_verified": True, "image_signature_verified": False, "admission_verified": False}))
            return 0
        if args.output is None: raise ValueError("Use a new private proof path")
        target = args.output
        if target.is_symlink() or not target.is_absolute() or not target.resolve().is_relative_to((ROOT / "private").resolve()) or target.exists():
            raise ValueError("Use a new ignored private proof")
        proof = operate(plan)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream: stream.write(canonical(proof) + b"\n")
        print(json.dumps({"image_signature_verified": True, "admission_verified": False, "full_stack_verified": False}))
        return 0
    except Exception:
        print("Image signature verification stopped; private inputs and tool output suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
