#!/usr/bin/env python3
"""Build a bounded static OCI artifact in a trusted post-review Zuul job."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import traceback

from atomic_file import atomic_write_bytes
from bounded_file import read_bootstrap_bytes, read_bounded_bytes, read_bounded_stream
import publish_harbor_static_image as publisher
import source_bound_static_image as source_binding
from strict_json import loads_strict_json


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
ARTIFACT = "platform-static-image"
PROGRAMS = (*source_binding.PROGRAMS, "atomic_file.py", "zuul_static_image.py")
REGISTRY_FIELDS = "origin ca_sha256 leaf_sha256 credential_sha256 project_id robot_id repository base_digest files"
EXIT_STAGE = {"contract": 20, "event": 21, "input": 22, "publish": 23, "private-inputs": 24,
              "checkout": 25, "registry-publication": 26, "receipt": 27,
              "source-witness": 28, "static-layer": 29, "registry-tls": 30, "registry-authentication": 31,
              "registry-fetch": 32, "registry-upload": 33, "registry-manifest": 34, "publication-receipt": 35}


def fingerprint(value):
    return hashlib.sha256(publisher.canonical(value)).hexdigest()


def registry_plan(plan, revision, inputs):
    return {"schema_version": 1, **plan["registry"], "revision": revision,
            "ca_file": str(inputs / "registry/ca.crt"), "credential_file": str(inputs / "registry/credential.json")}


def source_plan(plan, revision):
    return {"schema_version": 2, **plan["source"], "revision": revision, "branch_ref": "refs/heads/" + plan["branch"]}


def validate(plan):
    publisher.exact(plan, "schema_version tenant config_project project branch pipeline job secret source static_directory registry")
    if (type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or any(not isinstance(plan[key], str) or not NAME.fullmatch(plan[key]) for key in ("tenant", "pipeline", "job", "secret"))
            or not source_binding.tekton_checkout.branch_reference("refs/heads/" + plan["branch"] if isinstance(plan["branch"], str) else None)):
        raise ValueError("Select an exact trusted post-review publication scope")
    for key in ("config_project", "project"):
        value = plan[key]
        if (not isinstance(value, str) or not 1 <= len(value) <= 200
                or not all(NAME.fullmatch(part) and ".." not in part for part in value.split("/"))):
            raise ValueError("Select canonical Gerrit project paths")
    if plan["config_project"] == plan["project"]:
        raise ValueError("Keep trusted configuration separate from application sources")
    publisher.exact(plan["source"], "clone_url read_username ca_sha256 checkout_program_sha256")
    source_binding.validate_source(source_plan(plan, "1" * 40))
    if plan["source"]["checkout_program_sha256"] != source_binding.checkout_program_sha256():
        raise ValueError("Review the current trusted checkout program before rendering or publishing")
    if not plan["source"]["clone_url"].endswith("/a/" + plan["project"] + ".git"):
        raise ValueError("Source clone path must match the selected Gerrit project")
    directory = plan["static_directory"]
    if (not isinstance(directory, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,199}", directory)
            or any(part in {"", ".", ".."} for part in directory.split("/"))):
        raise ValueError("Select one regular static source subtree")
    publisher.exact(plan["registry"], REGISTRY_FIELDS)
    publisher.validate(registry_plan(plan, "1" * 40, PurePosixPath("/reviewed-inputs")))


def load_contract(path, digest):
    raw = read_bounded_bytes(path, max_bytes=128 * 1024)
    contract = loads_strict_json(raw)
    if not publisher.hex64(digest) or fingerprint(contract) != digest:
        raise ValueError("The trusted publication contract changed")
    publisher.exact(contract, "schema_version runtime plan programs_sha256")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in {"rke2", "k3s", "k8s", "raw", "docker"}:
        raise ValueError("Unsupported trusted publication package")
    validate(contract["plan"])
    hashes = contract["programs_sha256"]
    if not isinstance(hashes, dict) or set(hashes) != set(PROGRAMS):
        raise ValueError("Use the complete reviewed publisher program bundle")
    for name, selected in hashes.items():
        if not publisher.hex64(selected) or hashlib.sha256(read_bounded_bytes(path.parent / "programs" / name, max_bytes=256 * 1024)).hexdigest() != selected:
            raise ValueError("A trusted publisher program changed")
    # The entry point must belong to this exact bundle, not a nearby ambient copy.
    if Path(__file__).resolve() != (path.parent / "programs/zuul_static_image.py").resolve():
        raise ValueError("Execute the original reviewed publisher bundle")
    return contract


def validate_event(plan, event):
    publisher.exact(event, "build_uuid pipeline project branch revision")
    if (not isinstance(event["build_uuid"], str) or not re.fullmatch(r"[0-9a-f]{32}", event["build_uuid"])
            or event["pipeline"] != plan["pipeline"] or event["project"] != plan["project"] or event["branch"] != plan["branch"]
            or not isinstance(event["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", event["revision"])
            or event["revision"] == "0" * 40):
        raise ValueError("The Zuul branch event differs from its reviewed scope")


def verify_receipt(plan, revision, receipt):
    validate(plan)
    publisher.exact(receipt, "schema_version image manifest_digest config_digest output_layer_digest output_tree_sha256 base_digest source_revision artifact_publication_verified publisher_signature_verified deployment_verified ha_verified full_stack_verified source_checkout_witness_bound checkout_witness_sha256 source_plan_sha256")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ValueError("Unsupported static publication receipt")
    source = source_plan(plan, revision)
    source_binding.validate_source(source)
    for name in ("manifest_digest", "config_digest", "output_layer_digest"):
        if not isinstance(receipt[name], str) or not publisher.DIGEST.fullmatch(receipt[name]):
            raise ValueError("The published content digest is invalid")
    expected = {"image": plan["registry"]["origin"].removeprefix("https://") + "/" + plan["registry"]["repository"] + "@" + receipt["manifest_digest"],
        "base_digest": plan["registry"]["base_digest"], "source_revision": revision,
        "checkout_witness_sha256": hashlib.sha256(source_binding.witness_bytes(source)).hexdigest(),
        "source_plan_sha256": fingerprint(source)}
    if any(receipt[name] != value for name, value in expected.items()) or not publisher.hex64(receipt["output_tree_sha256"]):
        raise ValueError("The artifact receipt differs from the selected source or registry")
    if (any(receipt[name] is not True for name in ("artifact_publication_verified", "source_checkout_witness_bound"))
            or any(receipt[name] is not False for name in ("publisher_signature_verified", "deployment_verified", "ha_verified", "full_stack_verified"))):
        raise ValueError("The publication receipt overstates its verification scope")
    return receipt


def verify_artifact(contract, event, artifacts):
    plan = contract["plan"]
    validate(plan); validate_event(plan, event)
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 64 or any(not isinstance(value, dict) for value in artifacts):
        raise ValueError("Use a bounded artifact list from the selected Zuul build")
    matches = [value for value in artifacts if value.get("name") == ARTIFACT]
    if len(matches) != 1:
        raise ValueError("The selected build must have one unambiguous static artifact")
    artifact = matches[0]
    publisher.exact(artifact, "name url metadata")
    metadata = artifact["metadata"]
    publisher.exact(metadata, "schema_version producer build_uuid contract_sha256 receipt")
    if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
            or metadata["producer"] != "platform-zuul-static-image" or metadata["build_uuid"] != event["build_uuid"]
            or metadata["contract_sha256"] != fingerprint(contract)):
        raise ValueError("Artifact metadata belongs to another job contract or build")
    receipt = verify_receipt(plan, event["revision"], metadata["receipt"])
    expected_url = plan["registry"]["origin"] + "/v2/" + plan["registry"]["repository"] + "/manifests/" + receipt["manifest_digest"]
    if artifact["url"] != expected_url:
        raise ValueError("The artifact URL differs from its exact registry digest")
    return receipt


def private_inputs(directory):
    if directory.resolve() != directory or directory.is_symlink():
        raise ValueError("Use the original canonical private job inputs")
    for path in (directory, directory / "source", directory / "registry"):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Publisher inputs must remain owned and private")
    for relative in ("source/ca.crt", "source/token", "registry/ca.crt", "registry/credential.json"):
        path = directory / relative
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("Publisher input files must remain original private snapshots")


class PublicationError(ValueError):
    """A fixed public stage code with no API, Git or credential detail."""

    def __init__(self, stage):
        if stage not in EXIT_STAGE:
            raise ValueError("Unknown publication diagnostic stage")
        self.stage = stage
        super().__init__(stage)


def publication_failure_stage(error):
    functions = {"publish_bound": "source-witness", "output_layer": "static-layer", "__init__": "registry-tls",
                 "authenticate": "registry-authentication", "fetch": "registry-fetch", "upload": "registry-upload",
                 "assemble": "registry-manifest", "verify_receipt": "publication-receipt"}
    paths = {"publish_harbor_static_image.py", "source_bound_static_image.py", "zuul_static_image.py"}
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        if Path(frame.filename).name in paths and frame.name in functions:
            return functions[frame.name]
        if Path(frame.filename).name == "publish_harbor_static_image.py" and frame.name == "publish":
            return "registry-publication"
    return "registry-publication"


def operate(contract, event, inputs):
    validate(contract["plan"])
    plan = contract["plan"]
    validate_event(plan, event)
    if os.name != "posix" or os.geteuid() == 0:
        raise ValueError("Run the trusted publisher as a non-root Linux job")
    inputs = Path(inputs).absolute()
    source = source_plan(plan, event["revision"])
    publication = registry_plan(plan, event["revision"], inputs)
    try:
        private_inputs(inputs)
        # Validate trust/robot identity before cloning or uploading anything.
        publisher.private_inputs(publication)
    except Exception:
        raise PublicationError("private-inputs") from None
    with tempfile.TemporaryDirectory(prefix="platform-zuul-static-") as temporary:
        root = Path(temporary)
        workspace, witness = root / "source", root / "witness/checkout.json"
        witness.parent.mkdir(mode=0o700)
        try:
            source_binding.checkout_with_witness(source, workspace, witness, inputs / "source")
        except Exception:
            raise PublicationError("checkout") from None
        output = workspace / plan["static_directory"]
        if output.resolve() != output or not output.is_dir():
            raise ValueError("The reviewed static subtree is missing or redirects elsewhere")
        try:
            receipt = source_binding.publish_bound(publication, source, witness, output)
            verify_receipt(plan, event["revision"], receipt)
        except Exception as error:
            raise PublicationError(publication_failure_stage(error)) from None
    metadata = {"schema_version": 1, "producer": "platform-zuul-static-image", "build_uuid": event["build_uuid"],
                "contract_sha256": fingerprint(contract), "receipt": receipt}
    return {"artifact": {"name": ARTIFACT, "url": plan["registry"]["origin"] + "/v2/" + plan["registry"]["repository"] + "/manifests/" + receipt["manifest_digest"],
                         "metadata": metadata}, "artifact_publication_verified": True,
            "application_ci_verified": False, "deployment_verified": False, "ha_verified": False, "full_stack_verified": False}


def operate_private_payload(contract, event, payload):
    validate(contract["plan"])
    validate_event(contract["plan"], event)
    publisher.exact(payload, "source_ca source_token registry_ca registry_credential")
    if any(not isinstance(value, str) or not 1 <= len(value.encode()) <= (512 if key == "source_token" else 65536)
           for key, value in payload.items()):
        raise ValueError("Use bounded private job inputs")
    if os.name != "posix" or os.geteuid() == 0:
        raise ValueError("Use the original non-root Linux executor context")
    with tempfile.TemporaryDirectory(prefix="platform-zuul-inputs-") as temporary:
        root = Path(temporary)
        for name in ("source", "registry"):
            (root / name).mkdir(mode=0o700)
        for key, relative in {"source_ca": "source/ca.crt", "source_token": "source/token", "registry_ca": "registry/ca.crt", "registry_credential": "registry/credential.json"}.items():
            atomic_write_bytes(root / relative, payload[key].encode(), replace=False)
        return operate(contract, event, root)


def receipt_path(root, event):
    """Keep the public receipt in this executor's own build work directory."""
    if os.name != "posix" or os.geteuid() == 0 or not root.is_absolute() or root.resolve() != root:
        raise ValueError("Use the original non-root executor work directory")
    if root.name != "work" or root.parent.name != event["build_uuid"]:
        raise ValueError("Keep receipt storage scoped to this exact Zuul build")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("The original build work directory is not owned by the executor")
    return root / "platform-static-image-receipt.json"


def read_receipt(path, contract, event):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError("Use the original private receipt file")
    value = loads_strict_json(read_bootstrap_bytes(path, 16384, follow_symlinks=False))
    verify_artifact(contract, event, [value])
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--event-json")
    parser.add_argument("--receipt-work-root", type=Path)
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--input-dir", type=Path)
    selected.add_argument("--input-stdin", action="store_true")
    selected.add_argument("--check-contract", action="store_true")
    selected.add_argument("--read-receipt", action="store_true")
    args = parser.parse_args(argv)
    stage = "contract"
    try:
        contract = load_contract(args.contract.absolute(), args.contract_sha256)
        if args.check_contract:
            if args.event_json is not None or args.receipt_work_root is not None:
                raise ValueError("Contract inspection accepts no event or receipt")
            print(json.dumps({"contract_verified": True, "artifact_publication_verified": False, "full_stack_verified": False}))
            return 0
        if not isinstance(args.event_json, str) or len(args.event_json.encode()) > 16384:
            raise ValueError("Zuul event identity exceeds its bound")
        stage = "event"
        event = loads_strict_json(args.event_json)
        validate_event(contract["plan"], event)
        stage = "receipt"
        destination = receipt_path(args.receipt_work_root, event) if args.receipt_work_root is not None else None
        if args.read_receipt:
            if destination is None: raise ValueError("Select the original receipt work root")
            print(json.dumps(read_receipt(destination, contract, event), sort_keys=True))
            return 0
        if destination is not None and (not args.input_stdin or destination.exists() or destination.is_symlink()):
            raise ValueError("Only an original stdin job may write a fresh receipt")
        if args.input_stdin:
            stage = "input"
            payload = loads_strict_json(read_bounded_stream(sys.stdin.buffer, max_bytes=256 * 1024))
            stage = "publish"
            result = operate_private_payload(contract, event, payload)
        else:
            result = operate(contract, event, args.input_dir)
        if destination is None:
            print(json.dumps(result, sort_keys=True))
        else:
            stage = "receipt"
            artifact = result["artifact"]
            verify_artifact(contract, event, [artifact])
            atomic_write_bytes(destination, publisher.canonical(artifact) + b"\n", replace=False)
        return 0
    except Exception as error:
        if isinstance(error, PublicationError): stage = error.stage
        print("Trusted Zuul publication stopped; stage=" + stage + "; details suppressed.", file=sys.stderr)
        return EXIT_STAGE[stage]


if __name__ == "__main__":
    raise SystemExit(main())
