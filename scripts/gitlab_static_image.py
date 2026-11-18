#!/usr/bin/env python3
"""Publish a digest-addressed static image from an exact GitLab Runner job."""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

from atomic_file import atomic_write_bytes
from bounded_file import read_bounded_bytes
import publish_harbor_static_image as harbor
from strict_json import loads_strict_json
import tekton_checkout
import verify_gitlab_pipeline as gitlab


ARTIFACT = "platform-image-receipt.json"
LIMIT = 16 * 1024
REGISTRY_FIELDS = "origin ca_sha256 leaf_sha256 credential_sha256 project_id robot_id repository base_digest files"


def fingerprint(contract):
    return hashlib.sha256(harbor.canonical(contract)).hexdigest()


def clone_url(contract):
    return contract["api_url"].removesuffix("/api/v4") + "/" + contract["project_path"] + ".git"


def image_reference(contract, digest):
    registry = contract["registry"]
    return registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + digest


def registry_plan(contract, revision, inputs):
    return {"schema_version": 1, **contract["registry"], "revision": revision,
            "ca_file": str(inputs / "registry/ca.crt"), "credential_file": str(inputs / "registry/credential.json")}


def validate(contract):
    harbor.exact(contract, "schema_version api_url project_id project_path branch job source static_directory registry")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise ValueError("unsupported-gitlab-publisher-contract")
    source = {key: contract[key] for key in ("api_url", "project_id", "project_path", "branch")}
    gitlab.validate({"schema_version": 1, **source, "pipeline_id": 1, "revision": "1" * 40, "required_jobs": [contract["job"]]})
    harbor.exact(contract["source"], "read_username ca_sha256")
    if (not isinstance(contract["source"]["read_username"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", contract["source"]["read_username"])
            or not harbor.hex64(contract["source"]["ca_sha256"])):
        raise ValueError("invalid-gitlab-source-credential-or-trust")
    directory = contract["static_directory"]
    if (not isinstance(directory, str) or not 1 <= len(directory) <= 200
            or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in directory.split("/"))):
        raise ValueError("invalid-static-source-directory")
    harbor.exact(contract["registry"], REGISTRY_FIELDS)
    harbor.validate(registry_plan(contract, "1" * 40, PurePosixPath("/reviewed-inputs")))


def job_identity(contract, env):
    expected = {"CI_SERVER_URL": contract["api_url"].removesuffix("/api/v4"),
                "CI_PROJECT_ID": str(contract["project_id"]), "CI_PROJECT_PATH": contract["project_path"],
                "CI_COMMIT_BRANCH": contract["branch"], "CI_JOB_NAME": contract["job"],
                "CI_COMMIT_REF_PROTECTED": "true", "CI_PIPELINE_SOURCE": "push"}
    if any(env.get(name) != value for name, value in expected.items()):
        raise ValueError("gitlab-publisher-job-scope-mismatch")
    ids = {}
    for name in ("pipeline_id", "job_id"):
        value = env.get("CI_" + name.upper(), "")
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
            raise ValueError("invalid-gitlab-publisher-job-id")
        ids[name] = int(value)
    revision = env.get("CI_COMMIT_SHA", "")
    if not gitlab.SHA.fullmatch(revision) or revision == "0" * 40:
        raise ValueError("invalid-gitlab-publisher-revision")
    return {"project_id": contract["project_id"], **ids, "revision": revision}


def verify_receipt(contract, identity, document):
    harbor.exact(document, "schema_version producer publisher_contract_sha256 job artifact")
    if (type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["producer"] != "platform-gitlab-static-image"
            or document["publisher_contract_sha256"] != fingerprint(contract)):
        raise ValueError("gitlab-artifact-publisher-mismatch")
    job = document["job"]
    if not isinstance(job, dict) or job != identity or any(type(job[name]) is not int for name in ("project_id", "pipeline_id", "job_id")):
        raise ValueError("gitlab-artifact-job-mismatch")
    return harbor.verify_static_receipt(contract["registry"], identity["revision"], document["artifact"])


def publish(contract, inputs, env):
    validate(contract)
    identity = job_identity(contract, env)
    if os.name != "posix" or os.geteuid() == 0:
        raise ValueError("use-non-root-linux-gitlab-runner")
    inputs = inputs.resolve(strict=True)
    source = inputs / "source"
    ca = read_bounded_bytes(source / "ca.crt", max_bytes=65536)
    if hashlib.sha256(ca).hexdigest() != contract["source"]["ca_sha256"]:
        raise ValueError("gitlab-publisher-source-trust-changed")
    plan = registry_plan(contract, identity["revision"], inputs)
    # Validate the publisher's credentials before Git checkout or publication.
    harbor.private_inputs(plan)
    with tempfile.TemporaryDirectory(prefix="platform-gitlab-static-") as temporary:
        workspace = Path(temporary) / "source"
        tekton_checkout.checkout(clone_url(contract), identity["revision"], workspace, contract["source"]["read_username"],
                                 credentials=source, branch_ref="refs/heads/" + contract["branch"])
        output = workspace / contract["static_directory"]
        if not output.is_dir() or output.resolve() != output:
            raise ValueError("gitlab-publisher-static-directory-not-regular")
        artifact = harbor.publish(plan, output)
    document = {"schema_version": 1, "producer": "platform-gitlab-static-image", "publisher_contract_sha256": fingerprint(contract),
                "job": identity, "artifact": artifact}
    verify_receipt(contract, identity, document)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(ARTIFACT))
    args = parser.parse_args(argv)
    try:
        contract = loads_strict_json(read_bounded_bytes(args.contract, max_bytes=128 * 1024))
        if fingerprint(contract) != args.contract_sha256:
            raise ValueError("gitlab-publisher-contract-changed")
        output = args.output.absolute()
        if output.name != ARTIFACT or output.parent.resolve(strict=True) != output.parent or output.exists() or output.is_symlink():
            raise ValueError("use-a-new-canonical-gitlab-artifact-path")
        receipt = publish(contract, args.input_dir, os.environ)
        atomic_write_bytes(output, harbor.canonical(receipt) + b"\n", replace=False)
    except Exception:
        print("GitLab image publication stopped. Check the reviewed job scope and private Runner inputs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
