#!/usr/bin/env python3
"""Request one planned CNPG switchover and observe its original handle; no forced promotion."""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text
import deploy_postgres_ha as deploy
from kubernetes_management import Client
from strict_json import loads_strict_json
import verify_postgres_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
PHASE = "Switchover in progress"
HEALTHY = "Cluster in healthy state"


def toolset():
    return deploy.fingerprint({"dependencies": deploy.toolset(), **{path.name: hashlib.sha256(read_bounded_text(path, encoding="utf-8", max_bytes=256 * 1024).encode()).hexdigest()
        for path in (Path(__file__), Path(runtime.__file__))}})


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40: raise ValueError("Require an exact timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None: raise ValueError("Require a timezone-aware timestamp")
    return parsed


def load_inputs(bundle, deployment, plan, installation_path, runtime_path):
    fields = {"schema_version", "contract_sha256", "deployment_plan_sha256", "installation_sha256", "runtime_sha256", "source_primary", "target_primary"}
    if (not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or any(not isinstance(plan[k], str) or not deploy.SHA.fullmatch(plan[k]) for k in fields - {"schema_version", "source_primary", "target_primary"})
            or plan["contract_sha256"] != deployment["contract_sha256"] or plan["deployment_plan_sha256"] != deploy.fingerprint(deployment)):
        raise ValueError("Pin the exact deployment scope and both original evidence files")
    for key in ("source_primary", "target_primary"):
        item = plan[key]
        if not isinstance(item, dict) or set(item) != {"name", "uid"} or not deploy.policy.label(item["name"]) or not deploy.uid(item["uid"]):
            raise ValueError("Choose exact source and target pod names and UIDs")
    if plan["source_primary"]["name"] == plan["target_primary"]["name"] or plan["source_primary"]["uid"] == plan["target_primary"]["uid"]:
        raise ValueError("A planned switchover requires a distinct reviewed standby")
    contract, expected, schema = deploy.load_package(bundle, deployment["contract_sha256"])
    deploy.validate(deployment, contract, deployment["contract_sha256"])
    installation = runtime.load_installation(installation_path, plan["installation_sha256"], deployment)
    raw = read_bounded_bytes(runtime_path, max_bytes=256 * 1024)
    if hashlib.sha256(raw).hexdigest() != plan["runtime_sha256"]: raise ValueError("Runtime observation differs from its reviewed pin")
    proof = loads_strict_json(raw)
    if (not isinstance(proof, dict) or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("command") != "observe-postgres-runtime" or proof.get("phase") != "completed"
            or proof.get("contract_sha256") != plan["contract_sha256"] or proof.get("deployment_plan_sha256") != plan["deployment_plan_sha256"]
            or proof.get("installation_sha256") != plan["installation_sha256"] or proof.get("cluster_uid") != installation["cluster_uid"]
            or proof.get("read_only") is not True or proof.get("runtime_configuration_observed") is not True or proof.get("streaming_quorum_observed") is not True
            or proof.get("query_sha256") != hashlib.sha256(runtime.SQL.encode()).hexdigest() or proof.get("toolset_sha256") != deploy.toolset()
            or proof.get("verifier_sha256") != hashlib.sha256(read_bounded_bytes(Path(runtime.__file__), max_bytes=256 * 1024)).hexdigest()
            or any(proof.get(k) is not False for k in runtime.UNVERIFIED)
            or not isinstance(proof.get("system_identifier"), str) or not re.fullmatch(r"[1-9][0-9]{0,19}", proof["system_identifier"])
            or int(proof["system_identifier"]) >= 2 ** 64 or not runtime.integer(proof.get("timeline_id"))):
        raise ValueError("Require a matching completed runtime observation from the current verifier")
    timestamp(proof.get("recorded_at"))
    view = proof.get("topology")
    if (not isinstance(view, dict) or set(view) != {"cluster_uid", "generation", "primary", "primary_since", "pods", "volumes"}
            or view["cluster_uid"] != installation["cluster_uid"] or view["primary"] != plan["source_primary"]["name"]
            or not runtime.integer(view["generation"]) or not isinstance(view["pods"], dict) or len(view["pods"]) != 3
            or not isinstance(view["volumes"], dict) or set(view["volumes"]) != set(view["pods"])
            or any(not deploy.policy.label(name) or not isinstance(pod, dict) or not deploy.uid(pod.get("uid")) for name, pod in view["pods"].items())
            or not isinstance(proof.get("postmaster_started_at"), dict) or set(proof["postmaster_started_at"]) != set(view["pods"])):
        raise ValueError("Runtime topology is incomplete")
    timestamp(view["primary_since"])
    for key in ("source_primary", "target_primary"):
        if view["pods"].get(plan[key]["name"], {}).get("uid") != plan[key]["uid"]: raise ValueError("The selected pod is outside the verified topology")
    expected["metadata"]["annotations"] = {deploy.OPERATION: installation["operation_id"], deploy.CONTRACT: plan["contract_sha256"]}
    return contract, expected, schema, installation, proof


def preflight(client, contract, expected, schema, deployment, installation, proof):
    view, evidence = runtime.snapshot(client, contract, expected, schema, deployment, installation)
    if view != proof["topology"] or any(evidence[k] != proof[k] for k in ("system_identifier", "timeline_id", "postgres_version", "postmaster_started_at")):
        raise ValueError("Source database topology, history or processes changed; take a new observation")
    current = client.get(deploy.CLUSTER + contract["plan"]["name"], contract["plan"]["namespace"])
    deploy.owned(current, expected, installation)
    status = current.get("status", {})
    if (current["metadata"].get("generation") != view["generation"] or status.get("phase") != HEALTHY
            or status.get("currentPrimary") != view["primary"] or status.get("targetPrimary") != view["primary"]
            or status.get("currentPrimaryTimestamp") != view["primary_since"]): raise ValueError("A cluster transition is already active")
    return current


def make_patch(current, plan, requested_at, operation):
    version = current.get("metadata", {}).get("resourceVersion")
    if not isinstance(version, str) or not version or len(version) > 256 or any(ord(ch) < 33 for ch in version): raise ValueError("Require an exact resource version for the status request")
    conditions = copy.deepcopy(current["status"].get("conditions", []))
    ready = [c for c in conditions if c.get("type") == "Ready"]
    if len(ready) != 1 or ready[0].get("status") != "True": raise ValueError("Require a single ready condition before switchover")
    ready[0].update(status="False", reason="ClusterIsNotReady", message="Cluster Is Not Ready", lastTransitionTime=requested_at)
    tests = {"/metadata/uid": current["metadata"]["uid"], "/metadata/resourceVersion": version, "/spec": current["spec"],
             "/status/currentPrimary": plan["source_primary"]["name"], "/status/targetPrimary": plan["source_primary"]["name"],
             "/status/currentPrimaryTimestamp": current["status"]["currentPrimaryTimestamp"]}
    changes = {"/status/targetPrimary": plan["target_primary"]["name"], "/status/targetPrimaryTimestamp": requested_at,
               "/status/phase": PHASE, "/status/phaseReason": "Switching over to " + plan["target_primary"]["name"] + "; platform operation " + operation,
               "/status/conditions": conditions}
    return [{"op": "test", "path": key, "value": value} for key, value in tests.items()] + [{"op": "add", "path": key, "value": value} for key, value in changes.items()]


def patch_request(client, selected, patch, *, dry):
    # kubectl patch does not accept '-' as stdin for --patch-file. Avoid
    # putting the private specification or condition messages in argv.
    with tempfile.TemporaryDirectory(prefix="platform-postgres-status-") as directory:
        path = Path(directory) / "request.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(deploy.canonical(patch))
        args = ["patch", deploy.CLUSTER + selected["name"], "--namespace", selected["namespace"], "--subresource=status", "--type=json", "--patch-file", str(path), "-o", "json"]
        if dry: args.append("--dry-run=server")
        return loads_strict_json(client.call(args, seconds=30))


def check_patch_response(response, expected, installation, plan, requested_at, operation):
    deploy.owned(response, expected, installation)
    status = response.get("status", {})
    ready = [c for c in status.get("conditions", []) if c.get("type") == "Ready"]
    if (status.get("targetPrimary") != plan["target_primary"]["name"] or status.get("targetPrimaryTimestamp") != requested_at
            or status.get("currentPrimary") != plan["source_primary"]["name"] or status.get("phase") != PHASE
            or status.get("phaseReason") != "Switching over to " + plan["target_primary"]["name"] + "; platform operation " + operation
            or len(ready) != 1 or ready[0].get("status") != "False" or ready[0].get("reason") != "ClusterIsNotReady"
            or ready[0].get("lastTransitionTime") != requested_at): raise ValueError("Server admission changed the planned status request")


def observed_request(current, plan, state):
    status = current.get("status", {}); source, target = plan["source_primary"]["name"], plan["target_primary"]["name"]
    if status.get("targetPrimary") == target:
        if status.get("targetPrimaryTimestamp") != state["requested_at"]: raise ValueError("Another switchover request superseded this operation")
        if status.get("currentPrimary") not in {source, target}: raise ValueError("An unexpected primary was selected")
        return True
    if state["request_observed"] or status.get("targetPrimary") != source or status.get("currentPrimary") != source:
        raise ValueError("A different transition superseded this operation")
    return False


def check_result(view, evidence, plan, proof):
    source, target = plan["source_primary"]["name"], plan["target_primary"]["name"]
    original = proof["topology"]
    if (view["cluster_uid"] != original["cluster_uid"] or view["generation"] != original["generation"] or view["primary"] != target
            or view["primary_since"] == original["primary_since"] or set(view["pods"]) != set(original["pods"])
            or view["volumes"] != original["volumes"] or evidence["system_identifier"] != proof["system_identifier"]
            or evidence["timeline_id"] <= proof["timeline_id"]): raise ValueError("The new primary does not preserve the original database and storage scope")
    for name, pod in view["pods"].items():
        if pod["node"] != original["pods"][name]["node"]: raise ValueError("Database placement changed during switchover")
        # The former primary may be restarted/recreated by CNPG. The chosen
        # primary and the other standby must keep their original processes.
        if name != source and (pod != original["pods"][name] or evidence["postmaster_started_at"][name] != proof["postmaster_started_at"][name]):
            raise ValueError("A surviving database identity or process changed during switchover")


def operate(bundle, deployment, plan, installation_path, runtime_path, *, mode="local", output=None, wait_seconds=180, client=None):
    contract, expected, schema, installation, proof = load_inputs(bundle, deployment, plan, installation_path, runtime_path)
    result = {"local_inputs_verified": True, "read_only": True, "server_checked": False, "request_observed": False,
              "planned_switchover_observed": False, **runtime.UNVERIFIED}
    if mode == "local": return result
    if mode not in {"check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 1800:
        raise ValueError("Choose an explicit bounded switchover operation and private journal")
    resolved = output.resolve(); relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use a private operation directory")
    if mode != "resume": resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = resolved / "postgres-switchover-proof.json"; tools = toolset()
    with deploy.lock(resolved):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("The journal cannot be a symlink")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply"
                    or state.get("plan_sha256") != deploy.fingerprint(plan) or state.get("toolset_sha256") != tools
                    or state.get("cluster_uid") != installation["cluster_uid"] or not isinstance(state.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"])
                    or type(state.get("request_intent")) is not bool or type(state.get("request_observed")) is not bool
                    or state["request_observed"] and not state["request_intent"] or any(state.get(k) is not False for k in runtime.UNVERIFIED)):
                raise ValueError("Resume only the exact original switchover journal and tooling")
            if state["request_intent"]:
                timestamp(state.get("requested_at"))
                if not isinstance(state.get("patch_sha256"), str) or not deploy.SHA.fullmatch(state["patch_sha256"]): raise ValueError("The original request scope is missing")
                raw = read_bounded_bytes(resolved / "status-request.json", max_bytes=256 * 1024)
                if deploy.fingerprint(loads_strict_json(raw)) != state["patch_sha256"]: raise ValueError("The original status request changed")
            elif state.get("requested_at") is not None or state.get("patch_sha256") is not None: raise ValueError("The request journal is inconsistent")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": uuid4().hex, "plan_sha256": deploy.fingerprint(plan), "toolset_sha256": tools,
                     "cluster_uid": installation["cluster_uid"], "request_intent": False, "requested_at": None, "patch_sha256": None, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values)
            atomic_write_text(receipt, deploy.canonical(state))
        # Retain acknowledgment evidence, but never carry forward an old
        # successful final observation into a new invocation.
        record("starting", planned_switchover_observed=False, read_only=True, server_checked=False, **runtime.UNVERIFIED)
        wrote = False
        try:
            network = client or Client(deployment["target"]); selected = contract["plan"]
            with network.frozen():
                if not state["request_intent"]:
                    current = preflight(network, contract, expected, schema, deployment, installation, proof)
                    requested_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    patch = make_patch(current, plan, requested_at, state["operation_id"])
                    response = patch_request(network, selected, patch, dry=True)
                    check_patch_response(response, expected, installation, plan, requested_at, state["operation_id"])
                    record("server-admission-reviewed", server_checked=True)
                    if mode == "check-server":
                        record("server-check-completed")
                        return {**result, "server_checked": True}
                    # Recheck live SQL, target identity and original scope after
                    # dry-run. The final resourceVersion/spec tests reject races.
                    current = preflight(network, contract, expected, schema, deployment, installation, proof)
                    requested_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    patch = make_patch(current, plan, requested_at, state["operation_id"])
                    atomic_write_text(resolved / "status-request.json", deploy.canonical(patch))
                    record("before-status-request", request_intent=True, requested_at=requested_at, patch_sha256=deploy.fingerprint(patch), read_only=False)
                    wrote = True
                    response = patch_request(network, selected, patch, dry=False)
                    check_patch_response(response, expected, installation, plan, requested_at, state["operation_id"])
                    record("status-request-acknowledged", request_observed=True)
                else:
                    deploy.prerequisites(network, deployment, contract, schema)
                deadline = time.monotonic() + wait_seconds
                while True:
                    current = network.get(deploy.CLUSTER + selected["name"], selected["namespace"])
                    deploy.owned(current, expected, installation)
                    if observed_request(current, plan, state):
                        record("request-observed", request_observed=True)
                        target = network.get("pod/" + plan["target_primary"]["name"], selected["namespace"]) or {}
                        if target.get("metadata", {}).get("uid") != plan["target_primary"]["uid"] or target.get("metadata", {}).get("deletionTimestamp"):
                            raise ValueError("The chosen new primary pod identity changed")
                        allowed = {selected["images"]["postgres"], deployment["operator"]["image"]}
                        if current["status"].get("currentPrimary") == plan["target_primary"]["name"] and deploy.ready(network, selected, current, allowed):
                            view, evidence = runtime.snapshot(network, contract, expected, schema, deployment, installation)
                            check_result(view, evidence, plan, proof)
                            final = network.get(deploy.CLUSTER + selected["name"], selected["namespace"])
                            deploy.owned(final, expected, installation)
                            if not observed_request(final, plan, state) or final["status"].get("currentPrimary") != view["primary"] or final["status"].get("currentPrimaryTimestamp") != view["primary_since"]:
                                raise ValueError("Another transition began during final verification")
                            if runtime.topology(network, selected, expected, installation, allowed) != view:
                                raise ValueError("Database readiness or storage changed during final verification")
                            result.update(server_checked=True, request_observed=True, planned_switchover_observed=True, read_only=not wrote)
                            record("completed", **result, topology=view, database=evidence)
                            return result
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the same switchover request again; never repeat an uncertain patch")
                    record("waiting-for-original-request"); time.sleep(2)
        except BaseException:
            record("failed", failed_at=state["phase"], planned_switchover_observed=False, error_code="postgres-switchover-unverified", **runtime.UNVERIFIED)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle-dir", "deployment-plan", "switchover-plan", "installation-proof", "runtime-proof"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=180)
    modes = parser.add_mutually_exclusive_group()
    for name in ("check-server", "apply", "resume"): modes.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    try:
        deployment = loads_strict_json(read_bounded_bytes(args.deployment_plan, max_bytes=128 * 1024))
        plan = loads_strict_json(read_bounded_bytes(args.switchover_plan, max_bytes=128 * 1024))
        mode = next((key.replace("_", "-") for key in ("check_server", "apply", "resume") if getattr(args, key)), "local")
        print(json.dumps(operate(args.bundle_dir, deployment, plan, args.installation_proof, args.runtime_proof, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds)))
        return 0
    except Exception:
        print("Planned PostgreSQL switchover refused; retain the original operation handle. Private details were suppressed.")
        return 1


if __name__ == "__main__": raise SystemExit(main())
