#!/usr/bin/env python3
"""Bind static OCI publication to an isolated, exact Git checkout witness."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from bounded_file import read_bootstrap_bytes, read_bounded_bytes, read_bounded_text
import publish_harbor_static_image as publisher
from strict_json import loads_strict_json
import tekton_checkout


PROGRAMS = ("source_bound_static_image.py", "tekton_checkout.py", "publish_harbor_static_image.py",
            "bounded_file.py", "http_transport.py", "strict_json.py")


def checkout_program_sha256():
    # ConfigMap data and the embedded checkout program use UTF-8 with LF lines.
    return hashlib.sha256(read_bounded_text(tekton_checkout.__file__, max_bytes=256 * 1024).encode()).hexdigest()


def validate_source(source):
    if not isinstance(source, dict) or type(source.get("schema_version")) is not int or source["schema_version"] not in (1, 2, 3):
        raise ValueError("Unsupported source binding")
    ref_key = "patchset_ref" if source["schema_version"] == 1 else "branch_ref"
    publisher.exact(source, "schema_version clone_url read_username revision ca_sha256 checkout_program_sha256 " + ref_key)
    value = source["clone_url"]
    if not isinstance(value, str) or len(value) > 1024 or not value.startswith("https://"): raise ValueError("Use an explicit canonical HTTPS Git URL")
    parsed = urlsplit(value); publisher.origin("https://" + parsed.netloc)
    if parsed.query or parsed.fragment or not parsed.path.endswith(".git"):
        raise ValueError("Select an exact HTTPS repository URL")
    if source["schema_version"] == 3:
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}[.]git", parsed.path):
            raise ValueError("Select an exact forge owner and repository")
    else:
        project = parsed.path[3:-4]
        if (not parsed.path.startswith("/a/") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", project)
                or ".." in project or any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in project.split("/"))):
            raise ValueError("Select an exact Gerrit project")
    if not isinstance(source["read_username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", source["read_username"]): raise ValueError("Select the scoped Git reader")
    if not isinstance(source["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["revision"]) or source["revision"] == "0" * 40: raise ValueError("Pin the exact nonzero commit")
    if source["schema_version"] == 1:
        match = re.fullmatch(r"refs/changes/([0-9]{2})/([1-9][0-9]{0,9})/([1-9][0-9]{0,9})", source["patchset_ref"]) if isinstance(source["patchset_ref"], str) else None
        if not match or match[1] != f"{int(match[2]) % 100:02d}": raise ValueError("Pin the canonical Gerrit patch-set ref")
    elif not tekton_checkout.branch_reference(source["branch_ref"]):
        raise ValueError("Pin the canonical reviewed branch ref")
    if not all(publisher.hex64(source[key]) for key in ("ca_sha256", "checkout_program_sha256")): raise ValueError("Pin source trust and the trusted checkout helper")
    return source


def witness_document(source):
    validate_source(source)
    ref_key = "patchset_ref" if source["schema_version"] == 1 else "branch_ref"
    return {"schema_version": source["schema_version"], "source_revision": source["revision"], ref_key: source[ref_key],
        "clone_url_sha256": hashlib.sha256(source["clone_url"].encode()).hexdigest(), "source_ca_sha256": source["ca_sha256"],
        "checkout_program_sha256": source["checkout_program_sha256"], "source_plan_sha256": hashlib.sha256(publisher.canonical(source)).hexdigest()}


def witness_bytes(source): return publisher.canonical(witness_document(source)) + b"\n"


def verify_witness(source, raw):
    validate_source(source)
    if source["checkout_program_sha256"] != checkout_program_sha256(): raise ValueError("The reviewed checkout program changed")
    if not isinstance(raw, bytes) or raw != witness_bytes(source): raise ValueError("Original checkout witness or source binding changed")
    return hashlib.sha256(raw).hexdigest()


def checkout_with_witness(source, workspace, witness, credentials):
    validate_source(source)
    workspace, witness, credentials = Path(workspace), Path(witness), Path(credentials)
    if source["checkout_program_sha256"] != checkout_program_sha256(): raise ValueError("The reviewed checkout program changed")
    if hashlib.sha256(read_bounded_bytes(credentials / "ca.crt", max_bytes=65536)).hexdigest() != source["ca_sha256"]: raise ValueError("The source CA changed")
    if (not workspace.is_absolute() or not witness.is_absolute() or witness.parent.is_symlink() or not witness.parent.is_dir()
            or any(witness.parent.iterdir()) or witness.resolve().is_relative_to(workspace.resolve())): raise ValueError("Use a fresh checkout-only witness volume outside the source tree")
    ref_key = "patchset_ref" if source["schema_version"] == 1 else "branch_ref"
    tekton_checkout.checkout(source["clone_url"], source["revision"], workspace, source["read_username"], credentials=credentials, **{ref_key: source[ref_key]})
    raw = witness_bytes(source)
    descriptor = os.open(witness, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream: stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return {"checkout_witness_written": True, "source_revision": source["revision"], "checkout_witness_sha256": hashlib.sha256(raw).hexdigest()}


def publish_bound(plan, source, witness, output):
    publisher.validate(plan); validate_source(source)
    witness, output = Path(witness), Path(output)
    if plan["revision"] != source["revision"] or witness.resolve().is_relative_to(output.resolve()): raise ValueError("Publication revision or witness isolation changed")
    raw = read_bootstrap_bytes(witness, 16384, follow_symlinks=False)
    witness_sha = verify_witness(source, raw)
    result = publisher.publish(plan, output)
    result.update(source_checkout_witness_bound=True, checkout_witness_sha256=witness_sha,
        source_plan_sha256=hashlib.sha256(publisher.canonical(source)).hexdigest())
    # This binds a file, not a signed attestation. The controller/operator must
    # verify actual step isolation and task identity before trusting provenance.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--mode", choices=("checkout", "publish"), required=True)
    parser.add_argument("--source-plan", type=Path, required=True); parser.add_argument("--witness", type=Path, required=True)
    parser.add_argument("--workspace", type=Path); parser.add_argument("--credentials", type=Path); parser.add_argument("--plan", type=Path); parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if os.name != "posix" or os.geteuid() == 0: raise ValueError("Use a non-root Linux CI step")
        source = loads_strict_json(read_bounded_bytes(args.source_plan, max_bytes=16384))
        if args.mode == "checkout":
            if args.workspace is None or args.credentials is None or args.plan is not None or args.output is not None: raise ValueError("Select checkout-only arguments")
            result = checkout_with_witness(source, args.workspace, args.witness, args.credentials)
        else:
            if args.plan is None or args.output is None or args.workspace is not None or args.credentials is not None: raise ValueError("Select publish-only arguments")
            if args.source_plan.resolve().is_relative_to(args.output.resolve()): raise ValueError("Keep trusted source input outside build output")
            result = publish_bound(loads_strict_json(read_bounded_bytes(args.plan, max_bytes=65536)), source, args.witness, args.output)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception:
        print(json.dumps({"source_bound_failure": {"stage": args.mode, "details_suppressed": True}}), file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
