#!/usr/bin/env python3
"""Publish a static image from a scoped Woodpecker plugin step."""

import argparse
import base64
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import traceback

from atomic_file import atomic_write_bytes
from bounded_file import read_bounded_bytes
import publish_harbor_static_image as harbor
from strict_json import loads_strict_json
import tekton_checkout


PREFIX = b"PLATFORM_WOODPECKER_IMAGE_RECEIPT_V1 "
RECEIPT_LIMIT = 8192
SCOPE = ("woodpecker_origin", "forge_origin", "provider", "woodpecker_repository_id", "forge_repository_id", "repository", "branch")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
STEP = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def origin(value):
    url = harbor.origin(value)
    hostname, port = url.hostname, url.port
    if (len(hostname) > 253 or any(not HOST_LABEL.fullmatch(label) for label in hostname.split("."))
            or value != "https://" + hostname + (f":{port}" if port is not None else "")):
        raise ValueError("invalid-https-origin")
    return value


def positive_id(value):
    return type(value) is int and 0 < value < 2**63


def match_fields(row, expected):
    return isinstance(row, dict) and all(
        type(row.get(key)) is type(value) and row[key] == value for key, value in expected.items()
    )


def fingerprint(contract):
    return hashlib.sha256(harbor.canonical(contract)).hexdigest()


def clone_url(contract):
    return contract["forge_origin"] + "/" + contract["repository"] + ".git"


def registry_plan(contract, revision, inputs):
    return {"schema_version": 1, **contract["registry"], "revision": revision,
            "ca_file": str(inputs / "registry/ca.crt"), "credential_file": str(inputs / "registry/credential.json")}


def validate(contract):
    harbor.exact(contract, "schema_version " + " ".join(SCOPE) + " workflow step static_directory source registry woodpecker_ca_sha256 receipt_key_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise ValueError("invalid-woodpecker-publisher-contract")
    origin(contract["woodpecker_origin"])
    origin(contract["forge_origin"])
    if (contract["provider"] not in {"forgejo", "gitea"}
            or not all(positive_id(contract[field]) for field in ("woodpecker_repository_id", "forge_repository_id"))
            or not isinstance(contract["repository"], str) or not REPOSITORY.fullmatch(contract["repository"])
            or not isinstance(contract["branch"], str) or not tekton_checkout.branch_reference("refs/heads/" + contract["branch"])
            or any(not isinstance(contract[key], str) or not STEP.fullmatch(contract[key]) for key in ("workflow", "step"))
            or any(not harbor.hex64(contract[key]) for key in ("woodpecker_ca_sha256", "receipt_key_sha256"))):
        raise ValueError("invalid-woodpecker-publisher-scope")
    harbor.exact(contract["source"], "read_username ca_sha256")
    username = contract["source"]["read_username"]
    if (not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", username)
            or not harbor.hex64(contract["source"]["ca_sha256"])):
        raise ValueError("invalid-woodpecker-source-credential-or-trust")
    directory = contract["static_directory"]
    if (not isinstance(directory, str) or not 1 <= len(directory) <= 200
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in directory.split("/"))):
        raise ValueError("invalid-woodpecker-static-directory")
    harbor.exact(contract["registry"], "origin ca_sha256 leaf_sha256 credential_sha256 project_id robot_id repository base_digest files")
    harbor.validate(registry_plan(contract, "1" * 40, PurePosixPath("/reviewed-inputs")))


def identity(contract, number, revision):
    if (not positive_id(number) or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == "0" * 40):
        raise ValueError("invalid-woodpecker-publication-run")
    return {"number": number, "revision": revision, "workflow": contract["workflow"], "step": contract["step"]}


def job_identity(contract, env):
    expected = {"CI": "woodpecker", "CI_SYSTEM_URL": contract["woodpecker_origin"], "CI_FORGE_URL": contract["forge_origin"],
                "CI_FORGE_TYPE": contract["provider"], "CI_REPO": contract["repository"],
                "CI_REPO_REMOTE_ID": str(contract["forge_repository_id"]), "CI_REPO_CLONE_URL": clone_url(contract),
                "CI_COMMIT_BRANCH": contract["branch"], "CI_COMMIT_REF": "refs/heads/" + contract["branch"],
                "CI_PIPELINE_EVENT": "push", "CI_WORKFLOW_NAME": contract["workflow"], "CI_STEP_NAME": contract["step"],
                "CI_STEP_TYPE": "plugin"}
    mismatched = [key for key, value in expected.items() if env.get(key) != value]
    if env.get("CI_PIPELINE_RERUNS", "") not in {"", "0"}:
        mismatched.append("CI_PIPELINE_RERUNS")
    if mismatched:
        raise ValueError("woodpecker-publisher-step-scope-mismatch:" + ",".join(mismatched))
    number = env.get("CI_PIPELINE_NUMBER", "")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", number):
        raise ValueError("invalid-woodpecker-pipeline-number")
    return identity(contract, int(number), env.get("CI_COMMIT_SHA"))


def check_receipt_key(contract, key):
    if len(key) != 32 or hashlib.sha256(key).hexdigest() != contract["receipt_key_sha256"]:
        raise ValueError("woodpecker-receipt-key-changed")


def receipt_body(contract, job, artifact):
    return {"schema_version": 1, "producer": "platform-woodpecker-static-image",
            "publisher_contract_sha256": fingerprint(contract), "job": job, "artifact": artifact}


def authenticate_receipt(body, key):
    return hmac.digest(key, PREFIX + harbor.canonical(body), "sha256").hex()


def verify_receipt(contract, selected, envelope, key):
    check_receipt_key(contract, key)
    harbor.exact(envelope, "receipt mac_sha256")
    body, mac = envelope["receipt"], envelope["mac_sha256"]
    harbor.exact(body, "schema_version producer publisher_contract_sha256 job artifact")
    if not harbor.hex64(mac) or not hmac.compare_digest(mac, authenticate_receipt(body, key)):
        raise ValueError("woodpecker-publication-receipt-authentication-failed")
    if (type(body["schema_version"]) is not int or body["schema_version"] != 1
            or body["producer"] != "platform-woodpecker-static-image" or body["publisher_contract_sha256"] != fingerprint(contract)
            or not isinstance(body["job"], dict) or set(body["job"]) != set(selected)
            or not match_fields(body["job"], selected)):
        raise ValueError("woodpecker-publication-receipt-scope-mismatch")
    return harbor.verify_static_receipt(contract["registry"], selected["revision"], body["artifact"])


def publish(contract, inputs, env):
    validate(contract)
    job = job_identity(contract, env)
    if os.name != "posix" or os.geteuid() == 0:
        raise ValueError("use-non-root-linux-woodpecker-plugin")
    key = read_bounded_bytes(inputs / "receipt-key", max_bytes=32)
    check_receipt_key(contract, key)
    source_inputs = inputs / "source"
    ca = read_bounded_bytes(source_inputs / "ca.crt", max_bytes=65536)
    if hashlib.sha256(ca).hexdigest() != contract["source"]["ca_sha256"]:
        raise ValueError("woodpecker-source-trust-changed")
    plan = registry_plan(contract, job["revision"], inputs)
    harbor.private_inputs(plan)
    with tempfile.TemporaryDirectory(prefix="platform-woodpecker-static-") as temporary:
        workspace = Path(temporary) / "source"
        tekton_checkout.checkout(clone_url(contract), job["revision"], workspace, contract["source"]["read_username"],
                                 credentials=source_inputs, branch_ref="refs/heads/" + contract["branch"])
        output = workspace / contract["static_directory"]
        if not output.is_dir() or output.resolve() != output:
            raise ValueError("woodpecker-publisher-static-directory-not-regular")
        artifact = harbor.publish(plan, output)
    body = receipt_body(contract, job, artifact)
    envelope = {"receipt": body, "mac_sha256": authenticate_receipt(body, key)}
    verify_receipt(contract, job, envelope, key)
    return PREFIX + harbor.canonical(envelope)


def read_base64(value, maximum):
    if not isinstance(value, str) or len(value) > (maximum + 2) // 3 * 4:
        raise ValueError("woodpecker-plugin-input-exceeds-bound")
    raw = base64.b64decode(value, validate=True)
    if not raw or len(raw) > maximum:
        raise ValueError("invalid-woodpecker-plugin-input-size")
    return raw


def plugin(env):
    contract = loads_strict_json(read_base64(env.pop("PLUGIN_CONTRACT_B64"), 24 * 1024))
    if fingerprint(contract) != env.pop("PLUGIN_CONTRACT_SHA256"):
        raise ValueError("woodpecker-publisher-contract-changed")
    validate(contract)
    payload = loads_strict_json(read_base64(env.pop("PLUGIN_PRIVATE_INPUTS_B64"), 64 * 1024))
    harbor.exact(payload, "source_ca source_token registry_ca registry_credential receipt_key")
    with tempfile.TemporaryDirectory(prefix="platform-woodpecker-inputs-") as temporary:
        inputs = Path(temporary)
        for directory in ("registry", "source"):
            (inputs / directory).mkdir(mode=0o700)
        for name, path, limit in (("source_ca", "source/ca.crt", 32768), ("source_token", "source/token", 512),
                                  ("registry_ca", "registry/ca.crt", 32768), ("registry_credential", "registry/credential.json", 16384),
                                  ("receipt_key", "receipt-key", 32)):
            atomic_write_bytes(inputs / path, read_base64(payload[name], limit), replace=False)
        return publish(contract, inputs, env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        receipt = plugin(os.environ)
        if len(receipt) > RECEIPT_LIMIT:
            raise ValueError("woodpecker-publication-receipt-exceeds-bound")
        sys.stdout.buffer.write(receipt + b"\n")
        return 0
    except Exception as error:
        scope = re.fullmatch(r"woodpecker-publisher-step-scope-mismatch:((?:CI[A-Z_]*(?:,|$))+)", str(error))
        files = {"woodpecker_static_image.py", "publish_harbor_static_image.py", "tekton_checkout.py", "bounded_file.py"}
        locations = [Path(frame.filename).name + ":" + str(frame.lineno)
                     for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).name in files][-6:]
        diagnostic = {"code_locations": locations, "scope_fields": scope[1].split(",") if scope else []}
        print("Woodpecker publication stopped. " + harbor.canonical(diagnostic).decode("ascii"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
