#!/usr/bin/env python3
"""Publish a static OCI image from the exact Gerrit patch set of a Jenkins build."""

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
import source_bound_static_image as checkout
from strict_json import loads_strict_json
import verify_jenkins_gerrit as jenkins


ARTIFACT = "platform-image-receipt.json"
LIMIT = 16 * 1024
SCOPE = ("jenkins_origin", "job", "gerrit_origin", "project", "branch")
IDENTITY = ("build", "change", "patchset", "revision")


def fingerprint(value):
    return hashlib.sha256(harbor.canonical(value)).hexdigest()


def registry_plan(contract, revision, inputs):
    return {"schema_version": 1, **contract["registry"], "revision": revision,
            "ca_file": str(inputs / "registry/ca.crt"), "credential_file": str(inputs / "registry/credential.json")}


def source_plan(contract, identity):
    return {"schema_version": 1, **contract["source"], "revision": identity["revision"],
            "clone_url": f"{contract['gerrit_origin']}/a/{contract['project']}.git",
            "patchset_ref": jenkins.gerrit.reference(identity["change"], identity["patchset"])}


def image_reference(contract, digest):
    registry = contract["registry"]
    return registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + digest


def validate_identity(contract, identity):
    harbor.exact(identity, " ".join(IDENTITY))
    jenkins.validate({"schema_version": 1, **{key: contract[key] for key in SCOPE},
                      "job_config_sha256": "0" * 64, **identity})
    checkout.validate_source(source_plan(contract, identity))


def validate(contract):
    harbor.exact(contract, "schema_version jenkins_origin job gerrit_origin project branch jenkins_ca_sha256 source static_directory registry")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or not harbor.hex64(contract["jenkins_ca_sha256"]):
        raise ValueError("invalid-jenkins-publisher-contract")
    harbor.exact(contract["source"], "read_username ca_sha256 checkout_program_sha256")
    if contract["source"]["checkout_program_sha256"] != checkout.checkout_program_sha256():
        raise ValueError("jenkins-publisher-checkout-program-changed")
    validate_identity(contract, {"build": 1, "change": 1, "patchset": 1, "revision": "1" * 40})
    directory = contract["static_directory"]
    if (not isinstance(directory, str) or not 1 <= len(directory) <= 200
            or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in directory.split("/"))):
        raise ValueError("invalid-static-source-directory")
    harbor.exact(contract["registry"], "origin ca_sha256 leaf_sha256 credential_sha256 project_id robot_id repository base_digest files")
    harbor.validate(registry_plan(contract, "1" * 40, PurePosixPath("/reviewed-inputs")))


def build_identity(contract, env):
    if env.get("JOB_NAME") != "/".join(contract["job"]):
        raise ValueError("jenkins-publisher-job-mismatch")
    identity = {}
    for key, variable in (("build", "BUILD_NUMBER"), ("change", "GERRIT_CHANGE_NUMBER"), ("patchset", "GERRIT_PATCHSET_NUMBER")):
        value = env.get(variable, "")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
            raise ValueError("invalid-jenkins-publisher-build-identity")
        identity[key] = int(value)
    identity["revision"] = env.get("GERRIT_PATCHSET_REVISION")
    validate_identity(contract, identity)
    if (env.get("GIT_COMMIT") != identity["revision"]
            or env.get("GERRIT_REFSPEC") != source_plan(contract, identity)["patchset_ref"]):
        raise ValueError("jenkins-publisher-checkout-mismatch")
    return identity


def verify_receipt(contract, identity, receipt):
    validate_identity(contract, identity)
    harbor.exact(receipt, "schema_version producer publisher_contract_sha256 build artifact")
    if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["producer"] != "platform-jenkins-static-image"
            or receipt["publisher_contract_sha256"] != fingerprint(contract)):
        raise ValueError("jenkins-artifact-publisher-mismatch")
    validate_identity(contract, receipt["build"])
    if receipt["build"] != identity:
        raise ValueError("jenkins-artifact-build-mismatch")
    artifact = receipt["artifact"]
    harbor.exact(artifact, "schema_version image manifest_digest config_digest output_layer_digest output_tree_sha256 base_digest source_revision artifact_publication_verified publisher_signature_verified deployment_verified ha_verified full_stack_verified source_checkout_witness_bound checkout_witness_sha256 source_plan_sha256")
    source = source_plan(contract, identity)
    if (type(artifact["schema_version"]) is not int or artifact["schema_version"] != 1
            or any(not isinstance(artifact[key], str) or not harbor.DIGEST.fullmatch(artifact[key])
                   for key in ("manifest_digest", "config_digest", "output_layer_digest"))
            or not harbor.hex64(artifact["output_tree_sha256"])):
        raise ValueError("invalid-jenkins-artifact-digest")
    expected = {"image": image_reference(contract, artifact["manifest_digest"]), "base_digest": contract["registry"]["base_digest"],
                "source_revision": source["revision"], "source_plan_sha256": fingerprint(source),
                "checkout_witness_sha256": hashlib.sha256(checkout.witness_bytes(source)).hexdigest()}
    if (any(artifact[key] != value for key, value in expected.items())
            or any(artifact[key] is not True for key in ("artifact_publication_verified", "source_checkout_witness_bound"))
            or any(artifact[key] is not False for key in ("publisher_signature_verified", "deployment_verified", "ha_verified", "full_stack_verified"))):
        raise ValueError("jenkins-artifact-scope-mismatch")
    return artifact


def publish(contract, inputs, env):
    validate(contract)
    identity = build_identity(contract, env)
    if os.name != "posix" or os.geteuid() == 0:
        raise ValueError("use-non-root-linux-jenkins-worker")
    inputs = inputs.resolve(strict=True)
    plan = registry_plan(contract, identity["revision"], inputs)
    harbor.private_inputs(plan)
    source = source_plan(contract, identity)
    with tempfile.TemporaryDirectory(prefix="platform-jenkins-static-") as temporary:
        workspace = Path(temporary) / "source"
        witness = Path(temporary) / "proof/checkout.json"
        witness.parent.mkdir(mode=0o700)
        checkout.checkout_with_witness(source, workspace, witness, inputs / "source")
        output = workspace / contract["static_directory"]
        if not output.is_dir() or output.resolve() != output:
            raise ValueError("jenkins-publisher-static-directory-not-regular")
        artifact = checkout.publish_bound(plan, source, witness, output)
    receipt = {"schema_version": 1, "producer": "platform-jenkins-static-image", "publisher_contract_sha256": fingerprint(contract),
               "build": identity, "artifact": artifact}
    verify_receipt(contract, identity, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        contract = loads_strict_json(read_bounded_bytes(args.contract, max_bytes=128 * 1024))
        if fingerprint(contract) != args.contract_sha256:
            raise ValueError("jenkins-publisher-contract-changed")
        workspace = Path(os.environ["WORKSPACE"])
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or workspace != Path.cwd():
            raise ValueError("use-the-original-jenkins-workspace")
        output = workspace / ARTIFACT
        if output.exists() or output.is_symlink():
            raise ValueError("use-a-new-artifact-path")
        receipt = publish(contract, args.input_dir, os.environ)
        atomic_write_bytes(output, harbor.canonical(receipt) + b"\n", replace=False)
    except Exception:
        print("Jenkins image publication stopped. Check the reviewed job, workspace and private worker inputs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
