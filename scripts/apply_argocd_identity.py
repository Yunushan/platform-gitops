#!/usr/bin/env python3
"""Apply a reviewed Keycloak identity handoff to original Argo CD objects."""

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
import bootstrap_keycloak_realm as bootstrap
import configure_keycloak_argocd as identity
from deploy_keycloak_ha import canonical, fingerprint, includes, lock, sha, uid
from http_transport import open_http_request, read_bounded_response
from kubernetes_management import Client, validate_target, verify_target
import render_keycloak_ha as renderer
from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
OPERATION = "platform.gitops/argocd-identity-operation"
CONTRACT = "platform.gitops/argocd-identity-contract"
RESTART = "platform.gitops/argocd-identity-restart"
UNVERIFIED = tuple(k for k in identity.UNVERIFIED if k != "argocd_configuration_applied")
NAMES = ("argocd-rbac-cm", "argocd-cm")


def tools(): return bootstrap.sources(Path(__file__).stem)


def pinned(path, digest):
    if not isinstance(path, str) or not Path(path).is_absolute() or not sha(digest): raise ValueError("Pin an absolute private input and SHA-256")
    raw = read_bounded_bytes(Path(path), max_bytes=256 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError("An original private input changed")
    return loads_strict_json(raw)


def prepared(bundle, plan):
    renderer.exact(plan, "schema_version identity_plan identity_proof target namespace_uid server configmaps projects_sha256 argo_ca_file argo_ca_sha256 admitted_sha256 rollout_id")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1: raise ValueError("Unsupported Argo CD identity operation")
    for item in (plan["identity_plan"], plan["identity_proof"]): renderer.exact(item, "path sha256")
    selected = pinned(plan["identity_plan"]["path"], plan["identity_plan"]["sha256"])
    contract, realm, realm_proof = identity.prepared(bundle, selected)
    proof = pinned(plan["identity_proof"]["path"], plan["identity_proof"]["sha256"])
    if (proof.get("schema_version") != 1 or type(proof.get("schema_version")) is not int or proof.get("action") != "apply" or proof.get("phase") != "completed" or proof.get("attempted") is not True
        or proof.get("client_policy_verified") is not True or proof.get("app_settings_rendered") is not True or proof.get("plan_sha256") != fingerprint(selected) or proof.get("toolset_sha256") != identity.tools()
        or any(not uid(proof.get(k)) for k in ("operation_id", "client_id", "mapper_id")) or any(not sha(proof.get(k)) for k in ("credential_sha256", "observed_sha256"))
        or any(proof.get(k) is not False for k in identity.UNVERIFIED)): raise ValueError("Use the completed original provider client proof")
    validate_target(plan["target"])
    if not uid(plan["namespace_uid"]) or not sha(plan["projects_sha256"]) or plan["admitted_sha256"] is not None and not sha(plan["admitted_sha256"]): raise ValueError("Pin the original namespace and reviewed authorization inventory")
    renderer.exact(plan["configmaps"], "argocd-cm argocd-rbac-cm")
    for item in plan["configmaps"].values():
        renderer.exact(item, "uid sha256")
        if not uid(item["uid"]) or not sha(item["sha256"]): raise ValueError("Pin the original ConfigMap identities and contents")
    server = plan["server"]; renderer.exact(server, "deployment uid pod_template_sha256 spec_sha256 image")
    if not renderer.label(server["deployment"]) or not uid(server["uid"]) or not sha(server["pod_template_sha256"]) or not sha(server["spec_sha256"]) or not uid(plan["rollout_id"]) or not isinstance(server["image"], str) or not renderer.IMAGE.fullmatch(server["image"]): raise ValueError("Pin the existing Argo CD server workload and a fresh reviewed rollout UUID")
    if not isinstance(plan["argo_ca_file"], str) or not Path(plan["argo_ca_file"]).is_absolute() or not sha(plan["argo_ca_sha256"]): raise ValueError("Pin the application's HTTPS trust anchor")
    return selected, contract, realm, realm_proof, proof


def settings(selected, realm):
    # Render from current reviewed policy, not an editable old output file.
    with tempfile.TemporaryDirectory(prefix="platform-argocd-settings-") as temporary:
        directory = Path(temporary); identity.write_app(directory, selected, realm)
        values, = loads_strict_yaml_all(read_bounded_bytes(directory / "argocd-values.yaml", max_bytes=1024 * 1024))
    return {"argocd-cm": values["configs"]["cm"], "argocd-rbac-cm": values["configs"]["rbac"]}


def material(obj):
    meta = obj["metadata"]
    if obj["kind"] == "Deployment":
        return {"apiVersion": obj["apiVersion"], "kind": obj["kind"], "uid": meta["uid"], "name": meta["name"], "namespace": meta["namespace"], "labels": meta.get("labels", {}),
            "annotations": {k: v for k, v in meta.get("annotations", {}).items() if k not in {OPERATION, CONTRACT}}, "ownerReferences": meta.get("ownerReferences", []), "spec": obj["spec"]}
    value = {"apiVersion": obj["apiVersion"], "kind": obj["kind"], "metadata": {"name": meta["name"], "namespace": meta["namespace"],
        "labels": meta.get("labels", {}), "annotations": {k: v for k, v in meta.get("annotations", {}).items() if k not in {OPERATION, CONTRACT}},
        "ownerReferences": meta.get("ownerReferences", [])}, "immutable": obj.get("immutable", False), "data": obj.get("data", {})}
    if obj["kind"] == "ConfigMap": value.update(uid=meta["uid"], binaryData=obj.get("binaryData", {}))
    else: value.update(type=obj["type"], data={k: hashlib.sha256(base64.b64decode(v, validate=True)).hexdigest() for k, v in obj.get("data", {}).items()})
    return value


def projects(client, namespace):
    rows = loads_strict_json(client.call(["get", "appprojects.argoproj.io", "--namespace", namespace, "-o", "json"]))
    items = rows.get("items")
    if not isinstance(items, list) or len(items) > 4096 or any(not uid(o.get("metadata", {}).get("uid")) or o["metadata"].get("deletionTimestamp") for o in items): raise ValueError("Invalid authorization project inventory")
    return fingerprint(sorted(({"name": o["metadata"]["name"], "uid": o["metadata"]["uid"], "roles": o.get("spec", {}).get("roles", [])} for o in items), key=lambda o: o["name"]))


def workload_ready(deployment):
    meta, spec, status = deployment.get("metadata", {}), deployment.get("spec", {}), deployment.get("status", {})
    replicas = spec.get("replicas", 1)
    return (type(meta.get("generation")) is int and status.get("observedGeneration") == meta["generation"] and type(replicas) is int and 1 <= replicas <= 32
        and all(status.get(k) == replicas for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")) and status.get("unavailableReplicas", 0) == 0)


def prerequisites(client, plan, selected, state=None, *, require_ready=True):
    verify_target(client, plan["target"]); namespace = selected["application"]["namespace"]
    ns = client.get("namespace/" + namespace) or {}
    if ns.get("metadata", {}).get("uid") != plan["namespace_uid"] or ns["metadata"].get("deletionTimestamp"): raise ValueError("The original application namespace changed")
    original = plan["server"]; deployment = client.get("deployment/" + original["deployment"], namespace) or {}
    if deployment.get("kind") != "Deployment": raise ValueError("The original server Deployment is missing")
    meta, spec, status = deployment.get("metadata", {}), deployment.get("spec", {}), deployment.get("status", {})
    replicas = spec.get("replicas", 1); template = spec.get("template", {}); entry = (state or {}).get("objects", {}).get("deployment/" + original["deployment"], {})
    is_after = bool(state and entry.get("attempted") and fingerprint(material(deployment)) == entry.get("after_sha256"))
    if (meta.get("uid") != original["uid"] or meta.get("deletionTimestamp") or type(meta.get("generation")) is not int or type(replicas) is not int or not 1 <= replicas <= 32
        or not is_after and (fingerprint(template) != original["pod_template_sha256"] or fingerprint(spec) != original["spec_sha256"] or entry.get("observed"))
        or is_after and (meta.get("annotations", {}).get(OPERATION) != state["operation_id"] or meta.get("annotations", {}).get(CONTRACT) != fingerprint(plan) or template.get("metadata", {}).get("annotations", {}).get(RESTART) != plan["rollout_id"])
        or require_ready and not workload_ready(deployment)
        or not any(c.get("name") == "server" and c.get("image") == original["image"] for c in template.get("spec", {}).get("containers", []))): raise ValueError("The original Argo CD server workload is unavailable or changed")
    strategy = spec.get("strategy", {}); rolling = strategy.get("rollingUpdate", {})
    def allowance(value, *, up=False):
        if type(value) is int and value >= 0: return value
        if isinstance(value, str) and re.fullmatch(r"(?:[0-9]|[1-9][0-9]|100)%", value): return (replicas * int(value[:-1]) + (99 if up else 0)) // 100
        raise ValueError("Review the existing rolling-update availability policy")
    unavailable = allowance(rolling.get("maxUnavailable", "25%")); surge = allowance(rolling.get("maxSurge", "25%"), up=True)
    if strategy.get("type", "RollingUpdate") != "RollingUpdate" or unavailable >= replicas or not unavailable and not surge: raise ValueError("Require a progressing rolling update that retains serving capacity")
    if projects(client, namespace) != plan["projects_sha256"]: raise ValueError("Existing AppProject authorization changed")


class Server:
    def __init__(self, plan, selected):
        raw = read_bounded_bytes(Path(plan["argo_ca_file"]), max_bytes=512 * 1024)
        if hashlib.sha256(raw).hexdigest() != plan["argo_ca_sha256"]: raise ValueError("Application HTTPS trust changed")
        self.origin = selected["application"]["origin"]
        self.context = ssl.create_default_context(cadata=raw.decode("ascii"))

    def get(self, path):
        if path not in {"/api/version", "/api/v1/settings"}: raise ValueError("Unexpected application observation endpoint")
        request = Request(self.origin + path, headers={"Accept": "application/json"})
        try:
            with open_http_request(request, timeout=15, use_environment_proxy=False, tls_context=self.context) as response:
                if response.status != 200: raise ValueError("Unexpected application response")
                raw = read_bounded_response(response, max_bytes=512 * 1024)
            return loads_strict_json(raw)
        except HTTPError as exc:
            exc.close()
            raise ValueError("Application HTTPS observation failed; private response details suppressed") from None
        except Exception: raise ValueError("Application HTTPS observation failed; private response details suppressed") from None


def server_version(server, selected):
    result = server.get("/api/version")
    if not isinstance(result, dict) or not isinstance(result.get("Version"), str) or result["Version"].split("+", 1)[0] != "v" + selected["application"]["version"]: raise ValueError("The application endpoint reports a different Argo CD version")


def loaded(server, selected, desired):
    result = server.get("/api/v1/settings")
    wanted, = loads_strict_yaml_all(desired["argocd-cm"]["oidc.config"])
    return (isinstance(result, dict) and result.get("url") == selected["application"]["origin"] and includes({"name": wanted["name"], "issuer": wanted["issuer"], "clientID": wanted["clientID"],
        "enablePKCEAuthentication": True, "scopes": wanted["requestedScopes"], "idTokenClaims": wanted["requestedIDTokenClaims"]}, result.get("oidcConfig")))


def provider(selected, contract, realm, realm_proof, proof, client, credential):
    flow = identity.realm_ready(client, selected, contract, realm, realm_proof)
    if identity.verify_client(client, identity.policy(selected, flow, proof), realm["realm"], credential) != proof["observed_sha256"]: raise ValueError("The original provider client changed")


def secret_metadata(client, namespace, name):
    template = '{{.metadata.uid}}{{"\\n"}}{{index .metadata.annotations "' + OPERATION + '"}}{{"\\n"}}{{index .metadata.annotations "' + CONTRACT + '"}}'
    return client.call(["get", "secret/" + name, "--namespace", namespace, "--ignore-not-found=true", "-o", "go-template=" + template]).strip()


def validate_response(expected, actual, credential=None):
    meta = actual.get("metadata", {})
    if actual.get("apiVersion") != expected["apiVersion"] or actual.get("kind") != expected["kind"] or meta.get("name") != expected["metadata"]["name"] or meta.get("namespace") != expected["metadata"]["namespace"] or meta.get("deletionTimestamp") or meta.get("finalizers"): raise ValueError("Admission changed identity or lifecycle")
    if expected["metadata"].get("annotations", {}) != meta.get("annotations", {}) or expected["metadata"].get("labels", {}) != meta.get("labels", {}) or expected["metadata"].get("ownerReferences", []) != meta.get("ownerReferences", []): raise ValueError("Admission changed reviewed ownership")
    if expected["kind"] == "Secret":
        if actual.get("type") != "Opaque" or actual.get("immutable") is not True or set(actual.get("data", {})) != set(expected["data"]): raise ValueError("Keep the dedicated immutable single-key client Secret")
        key = next(iter(expected["data"]))
        if not hmac.compare_digest(base64.b64decode(actual["data"][key], validate=True), credential.encode()): raise ValueError("The private client credential changed")
    elif expected["kind"] == "Deployment":
        if meta.get("uid") != expected["metadata"]["uid"] or actual.get("spec") != expected["spec"]: raise ValueError("Admission changed the reviewed original rollout")
    elif meta.get("uid") != expected["metadata"]["uid"] or actual.get("data", {}) != expected["data"] or actual.get("binaryData", {}) != expected.get("binaryData", {}) or actual.get("immutable", False) not in {None, False}: raise ValueError("Admission changed reviewed application settings")
    return fingerprint(material(actual))


def patch_request(client, namespace, name, current, expected, *, dry):
    change = {"op": "add", "path": "/spec/template/metadata/annotations", "value": expected["spec"]["template"]["metadata"]["annotations"]} if expected["kind"] == "Deployment" else {"op": "add", "path": "/data", "value": expected["data"]}
    patch = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]}, {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]}, change,
        {"op": "add", "path": "/metadata/annotations", "value": expected["metadata"]["annotations"]}]
    with tempfile.TemporaryDirectory(prefix="platform-argocd-patch-") as temporary:
        path = Path(temporary) / "patch.json"; descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream: stream.write(canonical(patch))
        args = ["patch", expected["kind"].lower() + "/" + name, "--namespace", namespace, "--type=json", "--patch-file", str(path), "-o", "json"]
        if dry: args.append("--dry-run=server")
        return loads_strict_json(client.call(args))


def operate(bundle, plan, *, mode="local", output=None, wait_seconds=60, client=None, provider_client=None, server=None):
    selected, contract, realm, realm_proof, proof = prepared(bundle, plan); source_hash = tools(); app = selected["application"]; namespace = app["namespace"]
    result = {"local_inputs_verified": True, "server_checked": False, "argocd_configuration_applied": False, "server_settings_observed": False, "original_rollout_observed": False, "read_only": True, **{k: False for k in UNVERIFIED}}
    if mode == "local": return result
    if mode not in {"check-server", "apply", "resume"} or output is None or type(wait_seconds) is not int or not 5 <= wait_seconds <= 600: raise ValueError("Use one explicit bounded application operation")
    if mode != "check-server" and not sha(plan["admitted_sha256"]): raise ValueError("Review and pin server admission before applying identity settings")
    directory = output.resolve(); relative = directory.relative_to((ROOT / "private").resolve())
    if not relative.parts or output.is_symlink(): raise ValueError("Use an original private journal directory")
    if mode != "resume": directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not directory.is_dir(): raise ValueError("Resume requires its original private directory")
    names = ["secret/" + app["secret_name"], *("configmap/" + name for name in NAMES), "deployment/" + plan["server"]["deployment"]]; receipt = directory / "argocd-identity-proof.json"
    with lock(directory):
        if mode == "resume":
            if receipt.is_symlink(): raise ValueError("The original journal cannot redirect")
            state = loads_strict_json(read_bounded_bytes(receipt, max_bytes=256 * 1024))
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("action") != "apply" or state.get("plan_sha256") != fingerprint(plan) or state.get("toolset_sha256") != source_hash or not uid(state.get("operation_id"))
                or not isinstance(state.get("objects"), dict) or set(state["objects"]) != set(names) or not sha(state.get("credential_sha256")) or any(state.get(k) is not False for k in UNVERIFIED)): raise ValueError("Resume only the original application identity operation")
            for entry in state["objects"].values():
                renderer.exact(entry, "attempted observed uid before_sha256 after_sha256")
                if type(entry["attempted"]) is not bool or type(entry["observed"]) is not bool or entry["observed"] and not entry["attempted"] or entry["uid"] is not None and not uid(entry["uid"]) or entry["before_sha256"] is not None and not sha(entry["before_sha256"]) or not sha(entry["after_sha256"]): raise ValueError("Invalid original object handle")
        else:
            state = {"schema_version": 1, "action": mode, "operation_id": str(uuid4()), "plan_sha256": fingerprint(plan), "toolset_sha256": source_hash, "credential_sha256": proof["credential_sha256"],
                "objects": {name: {"attempted": False, "observed": False, "uid": None, "before_sha256": None, "after_sha256": None} for name in names}, **result}
        def record(phase, **values):
            state.update(phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **values); atomic_write_text(receipt, canonical(state))
        record("local-validated", **result); wrote = False
        try:
            credential = os.environ.get(app["client_secret_env"], "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credential) or not hmac.compare_digest(hashlib.sha256(credential.encode()).hexdigest(), state["credential_sha256"]): raise ValueError("Preserve the original provider client credential")
            desired = settings(selected, realm); identity.write_app(directory, selected, realm)
            public = server or Server(plan, selected); server_version(public, selected)
            upstream = provider_client or identity.Client(selected["realm_plan"]["server"], realm["realm"], app["client_id"])
            provider(selected, contract, realm, realm_proof, proof, upstream, credential)
            network = client or Client(plan["target"])
            with network.frozen():
                prerequisites(network, plan, selected, state, require_ready=not state["objects"][names[-1]]["attempted"])
                secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": app["secret_name"], "namespace": namespace, "labels": {"app.kubernetes.io/part-of": "argocd"},
                    "annotations": {OPERATION: state["operation_id"], CONTRACT: fingerprint(plan)}}, "immutable": True, "type": "Opaque", "data": {app["secret_key"]: base64.b64encode(credential.encode()).decode()}}
                expected = {names[0]: secret}
                for name in NAMES:
                    current = network.get("configmap/" + name, namespace)
                    if not current or current.get("metadata", {}).get("uid") != plan["configmaps"][name]["uid"] or current["metadata"].get("deletionTimestamp") or current["metadata"].get("finalizers") or current.get("immutable") or current.get("kind") != "ConfigMap": raise ValueError("An original mutable application ConfigMap changed identity")
                    entry = state["objects"]["configmap/" + name]
                    if not entry["attempted"] and fingerprint(material(current)) != plan["configmaps"][name]["sha256"]: raise ValueError("Review the original complete ConfigMap before changing identity settings")
                    if name == "argocd-rbac-cm" and any(k.startswith("policy.") and k.endswith(".csv") and k != "policy.csv" and v.strip() for k, v in current.get("data", {}).items()): raise ValueError("Review and consolidate additional RBAC fragments separately")
                    value = copy.deepcopy(current); value["data"] = {**current.get("data", {}), **desired[name]}; value["metadata"]["annotations"] = {**current["metadata"].get("annotations", {}), OPERATION: state["operation_id"], CONTRACT: fingerprint(plan)}
                    expected["configmap/" + name] = value
                current = network.get(names[-1], namespace); entry = state["objects"][names[-1]]
                if not entry["attempted"] and current["spec"]["template"].get("metadata", {}).get("annotations", {}).get(RESTART) == plan["rollout_id"]: raise ValueError("Use a fresh reviewed rollout UUID")
                value = copy.deepcopy(current); value["spec"]["template"].setdefault("metadata", {})["annotations"] = {**current["spec"]["template"].get("metadata", {}).get("annotations", {}), RESTART: plan["rollout_id"]}
                value["metadata"]["annotations"] = {**current["metadata"].get("annotations", {}), OPERATION: state["operation_id"], CONTRACT: fingerprint(plan)}
                expected[names[-1]] = value
                if not any(entry["attempted"] for entry in state["objects"].values()):
                    if secret_metadata(network, namespace, app["secret_name"]): raise ValueError("Never adopt an existing client Secret")
                    for name, obj in expected.items():
                        if obj["kind"] == "Secret": response = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=canonical(obj).encode()))
                        else: response = patch_request(network, namespace, obj["metadata"]["name"], network.get(name, namespace), obj, dry=True)
                        entry = state["objects"][name]; entry["after_sha256"] = validate_response(obj, response, credential)
                        if obj["kind"] != "Secret": entry.update(uid=obj["metadata"]["uid"], before_sha256=fingerprint(material(network.get(name, namespace))))
                    admitted = fingerprint({name: entry["after_sha256"] for name, entry in state["objects"].items()}); record("admission-reviewed", admitted_sha256=admitted, server_checked=True)
                    if plan["admitted_sha256"] is not None and admitted != plan["admitted_sha256"]: raise ValueError("Admission differs from the reviewed policy")
                    if mode == "check-server": return {**result, "server_checked": True, "admitted_sha256": admitted}
                if fingerprint({name: entry["after_sha256"] for name, entry in state["objects"].items()}) != plan["admitted_sha256"]: raise ValueError("The original admitted settings changed")
                for name, obj in expected.items():
                    entry = state["objects"][name]
                    if not entry["attempted"]:
                        prerequisites(network, plan, selected, state); provider(selected, contract, realm, realm_proof, proof, upstream, credential)
                        if tools() != source_hash: raise ValueError("Application tooling changed")
                        current = None
                        if obj["kind"] == "Secret":
                            if secret_metadata(network, namespace, app["secret_name"]): raise ValueError("A foreign Secret appeared")
                            dry = loads_strict_json(network.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=canonical(obj).encode()))
                        else:
                            current = network.get(name, namespace)
                            if not current or fingerprint(material(current)) != entry["before_sha256"]: raise ValueError("A ConfigMap changed before its guarded update")
                            dry = patch_request(network, namespace, obj["metadata"]["name"], current, obj, dry=True)
                        if validate_response(obj, dry, credential) != entry["after_sha256"]: raise ValueError("Admission changed before the write")
                        entry["attempted"] = True; record("before-" + name, read_only=False); wrote = True
                        if obj["kind"] == "Secret": response = loads_strict_json(network.call(["create", "-f", "-", "-o", "json"], payload=canonical(obj).encode()))
                        else: response = patch_request(network, namespace, obj["metadata"]["name"], current, obj, dry=False)
                        if obj["kind"] == "Secret" and uid(response.get("metadata", {}).get("uid")): entry["uid"] = response["metadata"]["uid"]; record("write-response-observed")
                    deadline = time.monotonic() + wait_seconds
                    while True:
                        if obj["kind"] == "Secret":
                            metadata = secret_metadata(network, namespace, app["secret_name"])
                            if metadata and (len(metadata.splitlines()) != 3 or metadata.splitlines()[1:] != [state["operation_id"], fingerprint(plan)]): raise ValueError("A foreign Secret occupies the original name")
                            current = network.get(name, namespace, optional=True) if metadata else None
                        else: current = network.get(name, namespace, optional=True)
                        if current and entry["uid"] is not None and current.get("metadata", {}).get("uid") != entry["uid"]: raise ValueError("An original resource was replaced")
                        if current and fingerprint(material(current)) == entry["after_sha256"]:
                            if not uid(current.get("metadata", {}).get("uid")): raise ValueError("An original resource has no valid identity")
                            if current["metadata"].get("annotations", {}).get(OPERATION) != state["operation_id"] or current["metadata"].get("annotations", {}).get(CONTRACT) != fingerprint(plan): raise ValueError("Original write markers changed")
                            validate_response(obj, current, credential); entry.update(uid=current["metadata"]["uid"], observed=True); record("observed-" + name); break
                        if entry["observed"] or current and obj["kind"] != "Secret" and fingerprint(material(current)) != entry["before_sha256"]: raise ValueError("An original object was deleted or changed")
                        if time.monotonic() >= deadline: raise TimeoutError("Unknown original write outcome; never automatically repeat it")
                        time.sleep(1)
                deadline = time.monotonic() + wait_seconds
                while True:
                    prerequisites(network, plan, selected, state, require_ready=False)
                    try: observed = workload_ready(network.get(names[-1], namespace)) and loaded(public, selected, desired)
                    except ValueError: observed = False  # Read-only observation may span the server's normal listener reload.
                    if observed: break
                    if time.monotonic() >= deadline: raise TimeoutError("Observe the original application settings again")
                    time.sleep(1)
                prerequisites(network, plan, selected, state); provider(selected, contract, realm, realm_proof, proof, upstream, credential); server_version(public, selected)
                for name, obj in expected.items():
                    current = network.get(name, namespace)
                    if not current or current.get("metadata", {}).get("uid") != state["objects"][name]["uid"] or validate_response(obj, current, credential) != state["objects"][name]["after_sha256"]: raise ValueError("Application configuration changed during final observation")
                if not loaded(public, selected, desired) or tools() != source_hash: raise ValueError("Settings or source identity changed during final observation")
                result.update(server_checked=True, argocd_configuration_applied=True, server_settings_observed=True, original_rollout_observed=True, read_only=not wrote); record("completed", **result); return result
        except BaseException:
            record("failed", failed_at=state["phase"], error_code="argocd-identity-unverified"); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--application-plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path); parser.add_argument("--wait-seconds", type=int, default=60)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("check-server", "apply", "resume"): modes.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_bytes(args.application_plan, max_bytes=128 * 1024)); mode = next((name.replace("_", "-") for name in ("check_server", "apply", "resume") if getattr(args, name)), "local")
        print(canonical(operate(args.bundle_dir, plan, mode=mode, output=args.output_dir, wait_seconds=args.wait_seconds))); return 0
    except Exception:
        print("Argo CD identity application stopped; preserve the private journal and original object handles. Details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
