#!/usr/bin/env python3
"""Submit fixed Tekton jobs for current Gerrit patch sets, with retained receipts."""

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import Request

from bounded_file import read_bounded_bytes
from http_transport import open_http_request, read_bounded_response
from kubernetes_management import Client, validate_target, verify_target
from strict_json import loads_strict_json
import tekton_forge_bridge as common


OWNER = "platform.gitops/gerrit-tekton-contract"
INPUTS = Path("/run/platform/gerrit-tekton")
LIMIT = 1024 * 1024
PROGRAMS = ("tekton_gerrit_bridge.py", "tekton_forge_bridge.py", "tekton_checkout.py", "bounded_file.py",
            "bounded_subprocess.py", "subprocess_timeout.py", "strict_json.py", "http_transport.py", "kubernetes_management.py")
RUNTIMES = ("rke2", "k3s", "k8s", "docker", "raw")
FALSE_FLAGS = ("deployed", "gerrit_runtime_verified", "tekton_execution_verified", "review_vote_verified", "cd_handoff_verified", "ha_verified", "full_stack_verified")


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is not None


def number(value):
    if type(value) is int and 1 <= value <= 9999999999: return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value): return int(value)
    raise ValueError("Missing bounded change or patch-set number")


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("Require an exact nonzero commit")
    return value


def reference(change, patchset):
    return f"refs/changes/{number(change) % 100:02d}/{number(change)}/{number(patchset)}"


def programs(plan):
    if plan.get("schema_version") == 2:
        from gerrit_artifact_contract import EXTRA_PROGRAMS
        return (*PROGRAMS, *EXTRA_PROGRAMS)
    return PROGRAMS


def validate(plan):
    fields = {"schema_version", "name", "source", "management_target", "namespace", "namespace_uid", "service_account",
              "submitter_account", "submitter_namespace", "node_selector", "controller_selector", "checkout_image", "build_image",
              "bridge_image", "build_argv", "read_secret", "bridge_secret", "timeout_minutes", "poll_seconds", "max_changes"}
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in {1, 2}:
        raise ValueError("Unsupported Gerrit review job plan")
    if set(plan) != fields | ({"publication"} if plan["schema_version"] == 2 else set()): raise ValueError("Unexpected Gerrit review job fields")
    for key in ("name", "namespace", "service_account", "submitter_account", "submitter_namespace", "read_secret", "bridge_secret"):
        if not isinstance(plan[key], str) or not common.NAME.fullmatch(plan[key]) or len(plan[key]) > (20 if key == "name" else 63):
            raise ValueError("Invalid dedicated review job identity")
    reserved = {"default", "kube-system", "kube-public", "kube-node-lease", "tekton-pipelines", "tekton-pipelines-resolvers"}
    if plan["namespace"] in reserved or plan["submitter_namespace"] in reserved or plan["namespace"] == plan["submitter_namespace"] or not identifier(plan["namespace_uid"]):
        raise ValueError("Pin the original restricted job namespace and separate control namespace")
    validate_target(plan["management_target"])
    for key in ("node_selector", "controller_selector"):
        if not common.valid_selector(plan[key]): raise ValueError("Require explicit Linux worker/control selectors")
    if any(k in plan["node_selector"] for k in ("node-role.kubernetes.io/master", "node-role.kubernetes.io/control-plane")):
        raise ValueError("Jobs need separate worker capacity")
    for key in ("checkout_image", "build_image", "bridge_image"):
        if not isinstance(plan[key], str) or not common.IMAGE.fullmatch(plan[key]): raise ValueError("Pin every job/control image")
    args = plan["build_argv"]
    if not isinstance(args, list) or not 1 <= len(args) <= 32 or any(not isinstance(a, str) or not a or len(a) > 1000 or "$(" in a or any(ord(c) < 32 for c in a) for a in args):
        raise ValueError("Require fixed reviewed build arguments")
    for key, low, high in (("timeout_minutes", 5, 60), ("poll_seconds", 10, 300), ("max_changes", 1, 1000)):
        if type(plan[key]) is not int or not low <= plan[key] <= high: raise ValueError("Bound job and discovery work")
    source = plan["source"]
    if not isinstance(source, dict) or set(source) != {"api_url", "project", "branch", "clone_url", "read_username", "ca_sha256"}:
        raise ValueError("Bind one Gerrit project, branch, origin and trust anchor")
    for key in ("project", "branch"):
        value = source[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) or ".." in value or any(not p or p.startswith(".") or p.endswith((".", ".lock")) for p in value.split("/")):
            raise ValueError("Invalid exact project or branch")
    origin = source["api_url"]
    if not isinstance(origin, str) or len(origin) > 2048 or any(c.isspace() or ord(c) < 33 for c in origin): raise ValueError("Invalid Gerrit origin")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or origin.endswith("/"):
        raise ValueError("Use an explicit verified HTTPS Gerrit origin without credentials or a path")
    if source["clone_url"] != origin + "/a/" + source["project"] + ".git": raise ValueError("Git and review API project origins must agree")
    if not isinstance(source["read_username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", source["read_username"]) or not isinstance(source["ca_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["ca_sha256"]):
        raise ValueError("Pin explicit scoped login and HTTPS CA bytes")
    if plan["schema_version"] == 2:
        from gerrit_artifact_contract import validate as validate_publication
        validate_publication(plan)
    return plan


class Gerrit:
    def __init__(self, plan, inputs=INPUTS):
        validate(plan)
        if os.name == "posix" and (inputs / "ca.crt").stat().st_mode & 0o007: raise ValueError("Private source trust input is world-accessible")
        ca = read_bounded_bytes(inputs / "ca.crt", max_bytes=256 * 1024)
        if hashlib.sha256(ca).hexdigest() != plan["source"]["ca_sha256"]: raise ValueError("Source trust anchor changed")
        token = common.private_input("api-token", minimum=16, maximum=512, root=inputs)
        if any(v < 33 or v > 126 for v in token): raise ValueError("Invalid private Gerrit HTTP credential")
        self.plan = plan
        self.context = ssl.create_default_context(cadata=ca.decode("ascii"))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.authorization = "Basic " + base64.b64encode(plan["source"]["read_username"].encode() + b":" + token).decode("ascii")

    def response_bytes(self, request):
        try:
            with open_http_request(request, timeout=10, use_environment_proxy=False, tls_context=self.context) as response:
                return read_bounded_response(response, max_bytes=LIMIT)
        except HTTPError as exc:
            exc.close()
            raise

    def get(self, path):
        if not path.startswith("/a/changes/") or "#" in path: raise ValueError("Read only Gerrit change resources")
        request = Request(self.plan["source"]["api_url"] + path, headers={"Authorization": self.authorization, "Accept": "application/json"})
        raw = self.response_bytes(request)
        if not raw.startswith(b")]}'\n"): raise ValueError("Missing Gerrit JSON protection prefix")
        return loads_strict_json(raw[5:])


def current(plan, change, *, status="NEW"):
    if status not in {"NEW", "MERGED"}: raise ValueError("Select an explicit supported review state")
    if not isinstance(change, dict) or change.get("project") != plan["source"]["project"] or change.get("branch") != plan["source"]["branch"] or change.get("status") != status or change.get("work_in_progress", False) is not False:
        raise ValueError("Change is outside this reviewed open/non-WIP project and branch")
    change_number = number(change.get("_number"))
    commit = revision(change.get("current_revision"))
    info = change.get("revisions", {}).get(commit)
    if not isinstance(info, dict) or type(info.get("_number")) is not int: raise ValueError("Current revision lacks an original patch-set number")
    patchset = number(info["_number"])
    if info.get("ref") != reference(change_number, patchset): raise ValueError("Current patch-set ref differs from its change identity")
    return {"change": change_number, "patchset": patchset, "revision": commit, "ref": info["ref"]}


def from_event(plan, event):
    if not isinstance(event, dict) or event.get("type") != "patchset-created": raise ValueError("Only patch-set creation events are accepted")
    change, patchset = event.get("change"), event.get("patchSet")
    if not isinstance(change, dict) or not isinstance(patchset, dict) or change.get("project") != plan["source"]["project"] or change.get("branch") != plan["source"]["branch"]:
        raise ValueError("Event is outside the exact project and branch")
    n, p = number(change.get("number")), number(patchset.get("number"))
    if patchset.get("ref") != reference(n, p): raise ValueError("Event patch-set ref is not canonical")
    return {"change": n, "patchset": p, "revision": revision(patchset.get("revision")), "ref": patchset["ref"]}


def verify_current(source, plan, item):
    actual = current(plan, source.get("/a/changes/" + str(number(item["change"])) + "?o=CURRENT_REVISION"))
    if actual != item: raise ValueError("Patch-set event is stale or source identity changed")


def run_for(contract, template, digest, item):
    if (not isinstance(item, dict) or set(item) != {"change", "patchset", "revision", "ref"}
            or type(item["change"]) is not int or type(item["patchset"]) is not int
            or item["ref"] != reference(item["change"], item["patchset"])): raise ValueError("Invalid reviewed patch-set identity")
    revision(item["revision"])
    key = hashlib.sha256(common.canonical({"contract": digest, **item})).hexdigest()
    run = copy.deepcopy(template)
    name = contract["plan"]["name"] + "-" + key[:32]
    run["metadata"].update(name=name, annotations={OWNER: digest}, labels={OWNER: digest[:32], common.PIPELINE_LABEL: name})
    run["spec"]["params"] = [{"name": "revision", "value": item["revision"]}, {"name": "patchset-ref", "value": item["ref"]}]
    data = {"run": run["metadata"]["name"], "change": str(item["change"]), "patchset": str(item["patchset"]), "revision": item["revision"],
            "ref": item["ref"], "spec_sha256": hashlib.sha256(common.canonical(run["spec"])).hexdigest()}
    return run, data


def check_run(expected, actual):
    if not isinstance(actual, dict) or actual.get("apiVersion") != "tekton.dev/v1" or actual.get("kind") != "PipelineRun" or actual.get("spec") != expected["spec"]:
        raise ValueError("Admission or retained run differs from the exact fixed pipeline")
    meta = actual.get("metadata", {})
    if any(meta.get(k) != expected["metadata"][k] for k in ("name", "namespace", "annotations", "labels")) or meta.get("deletionTimestamp") or meta.get("ownerReferences"):
        raise ValueError("Run identity or admission metadata changed")


def receipt(client, namespace, name, digest, data, *, create):
    expected = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                "metadata": {"name": name, "namespace": namespace, "annotations": {OWNER: digest}, "labels": {OWNER: digest[:32]}}, "data": data}
    value = client.get("configmap/" + name, namespace, optional=True)
    created = False; acknowledged_uid = None
    if value is None and create:
        try:
            acknowledged = loads_strict_json(client.call(["create", "-f", "-", "-o", "json"], payload=common.canonical(expected)))
            acknowledged_uid = acknowledged.get("metadata", {}).get("uid")
            if not identifier(acknowledged_uid): raise ValueError("Reservation/receipt acknowledgment has no original UID")
            created = True
        except ValueError:
            pass
        value = client.get("configmap/" + name, namespace, optional=True)
    if value is None or value.get("immutable") is not True or value.get("data") != data or any(value.get("metadata", {}).get(k) != expected["metadata"][k] for k in ("name", "namespace", "annotations", "labels")) or value["metadata"].get("deletionTimestamp") or value["metadata"].get("ownerReferences") or not identifier(value["metadata"].get("uid")):
        raise ValueError("Original immutable reservation or receipt is missing or changed")
    if acknowledged_uid is not None and value["metadata"]["uid"] != acknowledged_uid:
        raise ValueError("Acknowledged reservation/receipt was replaced")
    return created, value["metadata"]["uid"]


def submit(client, source, contract, template, digest, item):
    plan = contract["plan"]; namespace = plan["namespace"]
    verify_target(client, plan["management_target"])
    ns = client.get("namespace/" + namespace)
    if not ns or ns.get("metadata", {}).get("uid") != plan["namespace_uid"] or ns["metadata"].get("deletionTimestamp") or ns["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
        raise ValueError("Original restricted job namespace is unavailable")
    run, data = run_for(contract, template, digest, item)
    name = data["run"]
    existing = client.get("pipelinerun.tekton.dev/" + name, namespace, optional=True)
    done = client.get("configmap/" + name + "-done", namespace, optional=True)
    if existing is not None or done is not None:
        _, lock_uid = receipt(client, namespace, name + "-lock", digest, data, create=False)
    else:
        lock_uid = None
    if done is not None:
        expected_done = {**data, "reservation_uid": lock_uid, "uid": done.get("data", {}).get("uid", "")}
        if not identifier(expected_done["uid"]): raise ValueError("Completion receipt has no original run UID")
        receipt(client, namespace, name + "-done", digest, expected_done, create=False)
        if existing is None: return {"accepted": True, "duplicate": True, "run_retained": False}
    acknowledged_run_uid = None
    if existing is None:
        verify_current(source, plan, item)
        admitted = loads_strict_json(client.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=common.canonical(run)))
        check_run(run, admitted)
        verify_current(source, plan, item)
        created, lock_uid = receipt(client, namespace, name + "-lock", digest, data, create=True)
        if created:
            try:
                acknowledged = loads_strict_json(client.call(["create", "-f", "-", "-o", "json"], payload=common.canonical(run)))
                acknowledged_run_uid = acknowledged.get("metadata", {}).get("uid")
            except ValueError: pass
        existing = client.get("pipelinerun.tekton.dev/" + name, namespace, optional=True)
        if existing is None: raise ValueError("Uncertain reservation/run write requires recovery; it was not repeated")
    check_run(run, existing)
    run_uid = existing["metadata"].get("uid")
    if not identifier(run_uid) or acknowledged_run_uid is not None and acknowledged_run_uid != run_uid or done is not None and done["data"]["uid"] != run_uid: raise ValueError("Original run was replaced")
    _, current_lock_uid = receipt(client, namespace, name + "-lock", digest, data, create=False)
    if current_lock_uid != lock_uid: raise ValueError("Original reservation was replaced")
    created, _ = receipt(client, namespace, name + "-done", digest, {**data, "reservation_uid": lock_uid, "uid": run_uid}, create=True)
    return {"accepted": True, "duplicate": not created, "run_retained": True}


def discover(source, plan):
    # A bounded whole inventory is read before any submission. A full last page
    # marked _more_changes never silently becomes a complete inventory.
    result = []; seen = set(); offset = 0
    while True:
        size = min(100, plan["max_changes"] - len(result) + 1)
        query = "project:" + plan["source"]["project"] + " branch:" + plan["source"]["branch"] + " status:open -is:wip"
        rows = source.get("/a/changes/?" + urlencode({"q": query, "n": size, "S": offset, "o": "CURRENT_REVISION"}))
        if not isinstance(rows, list) or len(rows) > size: raise ValueError("Invalid bounded Gerrit change inventory")
        for row in rows:
            item = current(plan, row)
            if item["change"] in seen: raise ValueError("Source inventory changed during pagination; retry read-only discovery")
            seen.add(item["change"]); result.append(item)
        if len(result) > plan["max_changes"]: raise ValueError("Source inventory exceeds the reviewed cycle bound")
        if not rows or rows[-1].get("_more_changes") is not True: return result
        offset += len(rows)


def load_bundle(root, digest):
    raw = read_bounded_bytes(root / "gerrit-event-contract.json", max_bytes=128 * 1024)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest: raise ValueError("Original review contract changed")
    contract = loads_strict_json(raw)
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "runtime", "plan", "files_sha256"} or type(contract["schema_version"]) is not int or contract["schema_version"] != 1 or contract["runtime"] not in RUNTIMES:
        raise ValueError("Invalid review package")
    validate(contract["plan"])
    if set(contract["files_sha256"]) != {*programs(contract["plan"]), "run-template.json"}: raise ValueError("Incomplete review program inventory")
    files = {}
    for name, digest in contract["files_sha256"].items():
        raw = read_bounded_bytes(root / name, max_bytes=LIMIT)
        if hashlib.sha256(raw).hexdigest() != digest: raise ValueError("Original review helper changed")
        files[name] = raw
    return contract, loads_strict_json(files["run-template.json"])


def operate(root, digest, *, event_file=None, once=False):
    if os.name != "posix" or os.geteuid() == 0: raise ValueError("Use a scoped non-root Linux bridge identity")
    contract, template = load_bundle(root, digest)
    common.verify_running_sources(contract, Path(__file__))
    plan = contract["plan"]; source = Gerrit(plan)
    client = Client(plan["management_target"])
    with client.frozen():
        while True:
            items = [from_event(plan, loads_strict_json(read_bounded_bytes(event_file, max_bytes=LIMIT)))] if event_file else discover(source, plan)
            results = [submit(client, source, contract, template, digest, item) for item in items]
            report = {"checked": len(items), "accepted": sum(r["accepted"] for r in results), "duplicates": sum(r["duplicate"] for r in results), **{k: False for k in FALSE_FLAGS}}
            print(json.dumps(report, sort_keys=True), flush=True)
            if once or event_file: return report
            time.sleep(plan["poll_seconds"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--contract-sha256", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--event-file", type=Path); mode.add_argument("--poll-once", action="store_true"); mode.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    try:
        operate(args.bundle_dir, args.contract_sha256, event_file=args.event_file, once=args.poll_once); return 0
    except Exception:
        print("Gerrit review submission stopped; private source, credentials and API details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
