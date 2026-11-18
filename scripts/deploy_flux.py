#!/usr/bin/env python3
"""Check a reviewed Flux package locally; deploy only with explicit --apply."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from bounded_file import read_bounded_bytes
from kubernetes_management import Client, resource_name, verify_owned, verify_target
from render_flux import OWNER_LABEL, RUNTIMES, validate
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


def load_package(root: Path, digest: str) -> tuple[dict, dict[str, bytes]]:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Supply the reviewed deployment contract digest")
    payload = read_bounded_bytes(root / "deployment-contract.json", max_bytes=256 * 1024)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Deployment contract changed")
    contract = loads_strict_json(payload.decode("utf-8"))
    if (not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "crds", "inventory", "files_sha256"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in RUNTIMES):
        raise ValueError("Invalid deployment contract")
    validate(contract["plan"])
    if set(contract["files_sha256"]) != {"flux-components.yaml", "flux-sync.yaml"}:
        raise ValueError("Unexpected deployment files")
    files = {}
    objects = []
    for name, expected in contract["files_sha256"].items():
        payload = read_bounded_bytes(root / name, max_bytes=16 * 1024 ** 2)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("Deployment manifest changed")
        files[name] = payload
        objects.extend(loads_strict_yaml_all(payload.decode("utf-8")))
    inventory = [{"apiVersion": obj["apiVersion"], "kind": obj["kind"], "name": obj["metadata"]["name"], "namespace": obj["metadata"].get("namespace")}
                 for obj in objects]
    crds = sorted(obj["metadata"]["name"] for obj in objects if obj["kind"] == "CustomResourceDefinition")
    if inventory != contract["inventory"] or len(inventory) > 256 or contract["crds"] != crds:
        raise ValueError("Deployment inventory differs from reviewed content")
    return contract, files


def require_owned(client: Client, item: dict, namespace: str) -> None:
    # Namespace/storage are preprovisioned prerequisites. Selected Flux objects
    # are never adopted from an installation managed by another operator/tool.
    verify_owned(client, item, OWNER_LABEL, namespace)


def server_preflight(client: Client, contract: dict) -> None:
    plan = contract["plan"]
    verify_target(client, plan["management_target"])
    namespace, sync = plan["namespace"], plan["sync"]
    claim = client.get("persistentvolumeclaim/" + plan["artifact_claim"], namespace)
    if (not claim or claim.get("status", {}).get("phase") != "Bound" or "ReadWriteMany" not in claim.get("spec", {}).get("accessModes", [])
            or claim.get("spec", {}).get("volumeMode", "Filesystem") != "Filesystem"):
        raise ValueError("Source artifacts require an existing bound ReadWriteMany filesystem claim")
    if not client.get("serviceaccount/" + sync["service_account"], sync["namespace"]):
        raise ValueError("Preprovision the scoped workload reconciliation service account")
    # Query only the name output, never serialize the credential contents.
    client.call(["get", "secret/" + sync["read_secret"], "--namespace", sync["namespace"], "-o", "name"])
    for item in contract["inventory"]:
        if item["kind"] not in {"GitRepository", "Kustomization"}:
            require_owned(client, item, namespace)


def require_current_ready(current: dict | None) -> dict:
    status = (current or {}).get("status", {})
    generation = (current or {}).get("metadata", {}).get("generation")
    if (type(generation) is not int or status.get("observedGeneration") != generation
            or not any(c.get("type") == "Ready" and c.get("status") == "True" and c.get("observedGeneration") == generation
                       for c in status.get("conditions", []))):
        raise ValueError("Flux Ready condition is stale or does not describe the current generation")
    return status


def operate(root: Path, digest: str, *, apply: bool = False, check_server: bool = False) -> dict:
    contract, files = load_package(root, digest)
    report = {"read_only": not apply, "local_inputs_verified": True, "server_checked": False,
              "deployed": False, "bootstrap_ready": False, "reconciliation_verified": False, "ha_verified": False}
    if not apply and not check_server:
        return report
    plan = contract["plan"]
    client = Client(plan["management_target"])
    server_preflight(client, contract)
    # Submit the in-memory verified bytes, not paths that can change while
    # network checks run. Never force ownership conflicts or delete/prune here.
    apply_args = ["apply", "--server-side", "--field-manager=platform-flux", "-f", "-"]
    client.call([*apply_args, "--dry-run=server"], payload=files["flux-components.yaml"], seconds=60)
    report["server_checked"] = True
    if not apply:
        return report
    client.call(apply_args, payload=files["flux-components.yaml"], seconds=60)
    for crd in contract["crds"]:
        client.call(["wait", "--for=condition=Established", "customresourcedefinition/" + crd, "--timeout=120s"], seconds=120)
    for controller in sorted(plan["images"]):
        client.call(["rollout", "status", "deployment/" + controller, "--namespace", plan["namespace"], "--timeout=180s"], seconds=180)
    for item in contract["inventory"]:
        if item["kind"] in {"GitRepository", "Kustomization"}:
            require_owned(client, item, plan["namespace"])
    client.call([*apply_args, "--dry-run=server"], payload=files["flux-sync.yaml"], seconds=60)
    client.call(apply_args, payload=files["flux-sync.yaml"], seconds=60)
    sync = plan["sync"]
    captured_source = None
    for resource in ("gitrepository.source.toolkit.fluxcd.io", "kustomization.kustomize.toolkit.fluxcd.io"):
        if captured_source:
            revision = captured_source["status"]["artifact"]["revision"]
            client.call(["wait", "--for=jsonpath={.status.lastAppliedRevision}=" + revision,
                         resource + "/" + sync["name"], "--namespace", sync["namespace"], "--timeout=180s"], seconds=180)
        client.call(["wait", "--for=condition=Ready", resource + "/" + sync["name"], "--namespace", sync["namespace"], "--timeout=180s"], seconds=180)
        current = client.get(resource + "/" + sync["name"], sync["namespace"])
        status = require_current_ready(current)
        if captured_source is None:
            revision = status.get("artifact", {}).get("revision", "")
            branch = re.escape(sync["branch"]) + "@"
            if "commit" in sync:
                expected = f"(?:{branch})?sha1:{sync['commit']}"
            else:
                expected = branch + r"sha(?:1:[0-9a-f]{40}|256:[0-9a-f]{64})"
            if not isinstance(revision, str) or not re.fullmatch(expected, revision):
                if "commit" in sync:
                    raise ValueError("Git source did not produce the pinned commit")
                raise ValueError("Git source has no complete current branch revision")
            captured_source = current
        elif status.get("lastAppliedRevision") != captured_source["status"]["artifact"]["revision"]:
            raise ValueError("Reconciliation has not applied the captured source revision")
    latest = client.get("gitrepository.source.toolkit.fluxcd.io/" + sync["name"], sync["namespace"])
    require_current_ready(latest)
    if (not latest or latest.get("status", {}).get("artifact", {}).get("revision") != captured_source["status"]["artifact"]["revision"]
            or latest.get("metadata", {}).get("generation") != captured_source["metadata"]["generation"]):
        raise ValueError("Git source changed during bootstrap checks; retry current reconciliation")
    report.update(deployed=True, bootstrap_ready=True)
    # A current bootstrap Ready condition is not a drift, rollout, artifact
    # failover or restore test for the selected application stack.
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, apply=args.apply, check_server=args.check_server)))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        print("Flux deployment stopped; inspect private package, explicit target, prerequisites, ownership and controller readiness. Partial changes are retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
