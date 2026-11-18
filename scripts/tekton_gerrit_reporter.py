#!/usr/bin/env python3
"""Opt-in Verified votes for original, evidenced Gerrit/Tekton patch-set runs."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import Request

from bounded_file import read_bounded_bytes
from kubernetes_management import Client, KubernetesOperationError, verify_target
from strict_json import loads_strict_json
import tekton_forge_reporter as worker
import tekton_gerrit_bridge as bridge


INPUTS = Path("/run/platform/gerrit-tekton-reporter")
EXTRA_PROGRAMS = ("tekton_gerrit_reporter.py", "tekton_forge_reporter.py")


def fingerprint(value): return hashlib.sha256(bridge.common.canonical(value)).hexdigest()


def validate(plan, contract):
    fields = {"schema_version", "source_contract_sha256", "account", "secret", "http_username", "account_id", "label_definition_sha256", "helper_images", "poll_seconds", "max_runs_per_poll"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Declare the separate reviewed Gerrit reporter")
    original = contract["plan"]
    for key in ("source_contract_sha256", "label_definition_sha256"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]): raise ValueError("Pin the original submission contract and effective label definition")
    for key in ("account", "secret"):
        if not isinstance(plan[key], str) or not bridge.common.NAME.fullmatch(plan[key]) or len(plan[key]) > 63: raise ValueError("Use dedicated reporter references")
    if plan["account"] in {original["service_account"], original["submitter_account"]} or plan["secret"] in {original["bridge_secret"], original["read_secret"]}:
        raise ValueError("Keep reporter credentials and submitter/worker identities separate")
    if original.get("schema_version") == 2 and plan["secret"] in {original["publication"]["robot_secret"], original["publication"]["trust_secret"]}:
        raise ValueError("Keep reporter and publication credentials separate")
    if not isinstance(plan["http_username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", plan["http_username"]) or plan["http_username"] == original["source"]["read_username"] or type(plan["account_id"]) is not int:
        raise ValueError("Pin the separate Gerrit bot account and username")
    bridge.number(plan["account_id"])
    if not isinstance(plan["helper_images"], list) or not 1 <= len(plan["helper_images"]) <= 16 or any(not isinstance(i, str) or not bridge.common.IMAGE.fullmatch(i) for i in plan["helper_images"]):
        raise ValueError("Pin the controller's allowed worker helper images")
    for key, low, high in (("poll_seconds", 10, 300), ("max_runs_per_poll", 1, 100)):
        if type(plan[key]) is not int or not low <= plan[key] <= high: raise ValueError("Bound reporter polling")


class GerritReview(bridge.Gerrit):
    def __init__(self, original, plan, inputs=INPUTS):
        chosen = copy.deepcopy(original); chosen["source"]["read_username"] = plan["http_username"]
        super().__init__(chosen, inputs)
        self.original, self.settings = original, plan

    def request(self, path, payload=None):
        project = quote(self.original["source"]["project"], safe="")
        read = path == "/a/accounts/self" or path == "/a/projects/" + project + "/labels/?inherited" or re.fullmatch(r"/a/changes/[1-9][0-9]{0,9}/detail\?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES", path)
        write = re.fullmatch(r"/a/changes/[1-9][0-9]{0,9}/revisions/[0-9a-f]{40}/review", path)
        if payload is None and not read or payload is not None and not write: raise ValueError("Reporter API resource is outside its fixed scope")
        if payload is not None:
            if (set(payload) != {"message", "tag", "labels", "notify", "drafts", "omit_duplicate_comments", "ignore_automatic_attention_set_rules"}
                    or payload["notify"] != "NONE" or payload["drafts"] != "KEEP" or payload["omit_duplicate_comments"] is not True
                    or payload["ignore_automatic_attention_set_rules"] is not True or set(payload["labels"]) != {"Verified"}
                    or type(payload["labels"]["Verified"]) is not int or payload["labels"]["Verified"] not in {-1, 1}):
                raise ValueError("Only the fixed Verified result write is allowed")
        request = Request(self.original["source"]["api_url"] + path, data=bridge.common.canonical(payload) if payload is not None else None,
            headers={"Authorization": self.authorization, "Accept": "application/json", "Content-Type": "application/json"})
        raw = self.response_bytes(request)
        if not raw.startswith(b")]}'\n"): raise ValueError("Missing protected Gerrit response prefix")
        return loads_strict_json(raw[5:])


def source_policy(api, original, plan):
    account = api.request("/a/accounts/self")
    if not isinstance(account, dict) or type(account.get("_account_id")) is not int or account["_account_id"] != plan["account_id"] or account.get("username") != plan["http_username"] or account.get("inactive", False) is not False:
        raise ValueError("Reporter token does not identify the original active bot account")
    label_policy(api, original, plan)


def label_policy(api, original, plan):
    labels = api.request("/a/projects/" + quote(original["source"]["project"], safe="") + "/labels/?inherited")
    if not isinstance(labels, list) or len(labels) > 256: raise ValueError("Invalid effective label inventory")
    choices = [label for label in labels if isinstance(label, dict) and label.get("name") == "Verified"]
    if len(choices) != 1: raise ValueError("Require one unambiguous effective Verified label")
    label = choices[0]
    if (fingerprint(label) != plan["label_definition_sha256"] or label.get("copy_condition", "") != ""
            or label.get("allow_post_submit", False) is not False or type(label.get("default_value")) is not int or label["default_value"] != 0
            or any(value is not False for key, value in label.items() if key.startswith("copy_") and key != "copy_condition")):
        raise ValueError("Original label definition changed or can copy approvals to an untested revision")
    values = label.get("values")
    if not isinstance(values, dict) or any(not re.fullmatch(r" ?[+-]?[0-9]+", k) for k in values) or not {-1, 0, 1} <= {int(k) for k in values}:
        raise ValueError("Verified label does not support the reviewed failure/neutral/success values")


def identity(client, contract, template, digest, run):
    params = run.get("spec", {}).get("params", [])
    if not isinstance(params, list) or len(params) != 2 or [p.get("name") for p in params] != ["revision", "patchset-ref"]:
        raise ValueError("Run has no sole reviewed revision/ref pair")
    ref = params[1].get("value", "")
    match = re.fullmatch(r"refs/changes/([0-9]{2})/([1-9][0-9]{0,9})/([1-9][0-9]{0,9})", ref) if isinstance(ref, str) else None
    if match is None: raise ValueError("Run has no canonical Gerrit patch set")
    item = {"change": int(match[2]), "patchset": int(match[3]), "revision": bridge.revision(params[0].get("value")), "ref": ref}
    expected, data = bridge.run_for(contract, template, digest, item)
    observed = run
    if run.get("spec", {}).get("status") in {"Cancelled", "CancelledRunFinally", "StoppedRunFinally"} and worker.terminal(run) == "failure":
        # Cancellation is an explicit controller intent, not another pipeline.
        # Admit only these known terminal-negative intents; they can never
        # satisfy a successful-build vote or alter any reviewed job field.
        observed = copy.deepcopy(run); observed["spec"].pop("status")
    bridge.check_run(expected, observed)
    run_uid = worker.uid(run.get("metadata", {}).get("uid"))
    _, lock_uid = bridge.receipt(client, contract["plan"]["namespace"], data["run"] + "-lock", digest, data, create=False)
    bridge.receipt(client, contract["plan"]["namespace"], data["run"] + "-done", digest, {**data, "reservation_uid": lock_uid, "uid": run_uid}, create=False)
    return expected, {**data, "uid": run_uid}, item


def evidence(client, contract, template, digest, run, plan):
    expected, data, item = identity(client, contract, template, digest, run)
    state = worker.terminal(run)
    proof = {**data, "state": state, "completion_time": run.get("status", {}).get("completionTime")}
    if state == "success":
        if run["spec"].get("status"): raise ValueError("Cancellation cannot establish success")
        effective = {**contract["plan"], "reporting": {"helper_images": plan["helper_images"]}}
        proof.update(worker.success_evidence(client, effective, expected, run, data, publication=contract["plan"]["schema_version"] == 2))
        if contract["plan"]["schema_version"] == 2:
            from gerrit_artifact_contract import verify_receipt
            child = run["status"]["childReferences"][0]["name"]
            proof["artifact"] = verify_receipt(contract["plan"], data, client.get("taskrun.tekton.dev/" + child, contract["plan"]["namespace"]), run)
    return data, item, proof


def review(api, original, item):
    obj = api.request("/a/changes/" + str(item["change"]) + "/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES")
    if bridge.current(original, obj) != item: raise ValueError("Review result is stale, closed or for another patch set")
    return obj


def published(obj, plan, item, payload):
    messages = obj.get("messages", [])
    if not isinstance(messages, list) or len(messages) > 4096: raise ValueError("Review messages exceed the inspected bound")
    tagged = [m for m in messages if isinstance(m, dict) and m.get("tag") == payload["tag"]]
    if not tagged:
        votes = obj.get("labels", {}).get("Verified", {}).get("all", [])
        if not isinstance(votes, list): raise ValueError("Invalid detailed label votes")
        own = [v for v in votes if v.get("_account_id") == plan["account_id"]]
        if len(own) > 1 or any(type(v.get("value")) is not int or v["value"] != 0 for v in own):
            raise ValueError("Bot already has an unrelated verification vote; do not overwrite it")
        return False
    if len(tagged) != 1: raise ValueError("Ambiguous duplicate result messages")
    message = tagged[0]
    # Gerrit adds a Patch Set header and label summary to a review message.
    # The private deterministic body must still be present as an exact suffix.
    if (message.get("author", {}).get("_account_id") != plan["account_id"] or message.get("real_author", {}).get("_account_id", plan["account_id"]) != plan["account_id"]
            or message.get("_revision_number") != item["patchset"] or not isinstance(message.get("id"), str) or not message["id"]
            or not isinstance(message.get("message"), str) or not (message["message"] == payload["message"] or message["message"].endswith("\n\n" + payload["message"]))):
        raise ValueError("Tagged result has another author, revision or body")
    rows = obj.get("labels", {}).get("Verified", {}).get("all", [])
    if not isinstance(rows, list): raise ValueError("Missing detailed verification votes")
    votes = [v for v in rows if v.get("_account_id") == plan["account_id"]]
    if len(votes) != 1 or type(votes[0].get("value")) is not int or votes[0]["value"] != payload["labels"]["Verified"] or votes[0].get("copied", False) is not False:
        raise ValueError("Tagged result's bot vote drifted or was copied")
    return True


def report_run(client, api, contract, template, digest, plan, report_digest, run):
    original = contract["plan"]
    verify_target(client, original["management_target"])
    ns = client.get("namespace/" + original["namespace"])
    if not ns or ns.get("metadata", {}).get("uid") != original["namespace_uid"] or ns["metadata"].get("deletionTimestamp") or ns["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted": raise ValueError("Original restricted job namespace changed")
    expected, data, item = identity(client, contract, template, digest, run)
    state = worker.terminal(run)
    if state is None: return "pending"
    payload = review_payload(data, item, report_digest, state)
    source_policy(api, original, plan)
    obj = review(api, original, item)
    base = {**data, "reporter_account_id": str(plan["account_id"]), "reporter_contract_sha256": report_digest, "state": state, "payload_sha256": fingerprint(payload)}
    name = data["run"] + "-vote"
    recorded = client.get("configmap/" + name, original["namespace"], optional=True)
    if recorded is not None:
        record = recorded.get("data", {})
        if set(record) != {*base, "evidence_sha256", "reservation_uid"} or any(record.get(k) != v for k, v in base.items()) or not isinstance(record.get("evidence_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["evidence_sha256"]): raise ValueError("Original review receipt differs from this run")
        _, reservation_uid = bridge.receipt(client, original["namespace"], name + "-lock", report_digest, {k: v for k, v in record.items() if k != "reservation_uid"}, create=False)
        if record["reservation_uid"] != reservation_uid: raise ValueError("Review reservation was replaced")
        bridge.receipt(client, original["namespace"], name, report_digest, record, create=False)
        if not published(obj, plan, item, payload): raise ValueError("Published review was removed; it will not be overwritten")
        return "already-reported"
    _, _, proof = evidence(client, contract, template, digest, run, plan)
    record = {**base, "evidence_sha256": fingerprint(proof)}
    # Never adopt an unreserved matching message, even if the same bot wrote it.
    reservation = client.get("configmap/" + name + "-lock", original["namespace"], optional=True)
    if published(obj, plan, item, payload):
        if reservation is None: raise ValueError("Review has no original publication reservation")
        _, reservation_uid = bridge.receipt(client, original["namespace"], name + "-lock", report_digest, record, create=False)
    else:
        current = client.get("pipelinerun.tekton.dev/" + data["run"], original["namespace"])
        _, _, refreshed = evidence(client, contract, template, digest, current, plan)
        if refreshed != proof: raise ValueError("Worker/controller evidence changed before publication")
        source_policy(api, original, plan)
        obj = review(api, original, item)
        if published(obj, plan, item, payload) and client.get("configmap/" + name + "-lock", original["namespace"], optional=True) is None:
            raise ValueError("Review appeared without an original reservation")
        created, reservation_uid = bridge.receipt(client, original["namespace"], name + "-lock", report_digest, record, create=True)
        if created:
            try: api.request("/a/changes/" + str(item["change"]) + "/revisions/" + item["revision"] + "/review", payload)
            except Exception: pass
        if not published(review(api, original, item), plan, item, payload): raise ValueError("Uncertain review write was not repeated; retain the original reservation")
    bridge.receipt(client, original["namespace"], name, report_digest, {**record, "reservation_uid": reservation_uid}, create=True)
    return "reported"


def review_payload(data, item, report_digest, state):
    if state not in {"success", "failure"}: raise ValueError("Require a terminal review result")
    return {"tag": "autogenerated:platform-tekton:" + report_digest[:16] + ":" + data["uid"],
        "message": "Tekton " + state + "; run " + data["uid"] + "; revision " + item["revision"],
        "labels": {"Verified": 1 if state == "success" else -1}, "notify": "NONE", "drafts": "KEEP", "omit_duplicate_comments": True, "ignore_automatic_attention_set_rules": True}


def load_bundle(root, digest):
    raw = read_bounded_bytes(root / "gerrit-report-contract.json", max_bytes=128 * 1024)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest: raise ValueError("Original reporter contract changed")
    selected = loads_strict_json(raw)
    if not isinstance(selected, dict) or set(selected) != {"schema_version", "plan", "files_sha256"} or type(selected["schema_version"]) is not int or selected["schema_version"] != 1: raise ValueError("Invalid reporter contract")
    contract, template = bridge.load_bundle(root, selected["plan"].get("source_contract_sha256"))
    if set(selected["files_sha256"]) != {*bridge.programs(contract["plan"]), *EXTRA_PROGRAMS, "run-template.json", "gerrit-event-contract.json"}: raise ValueError("Incomplete reporter helper inventory")
    for name, expected in selected["files_sha256"].items():
        if hashlib.sha256(read_bounded_bytes(root / name, max_bytes=bridge.LIMIT)).hexdigest() != expected: raise ValueError("Original reporter/package program changed")
    validate(selected["plan"], contract)
    return selected, contract, template


def poll(client, api, contract, template, digest, plan, report_digest, cursor=""):
    if not isinstance(cursor, str) or len(cursor) > 4096: raise ValueError("Invalid bounded continuation token")
    query = urlencode({"limit": plan["max_runs_per_poll"], "labelSelector": bridge.OWNER + "=" + digest[:32], "continue": cursor})
    result = loads_strict_json(client.call(["get", "--raw", "/apis/tekton.dev/v1/namespaces/" + contract["plan"]["namespace"] + "/pipelineruns?" + query]))
    if not isinstance(result, dict) or result.get("apiVersion") != "tekton.dev/v1" or result.get("kind") != "PipelineRunList" or not isinstance(result.get("items"), list) or len(result["items"]) > plan["max_runs_per_poll"]: raise ValueError("Invalid bounded PipelineRun inventory")
    summary = {"pending": 0, "reported": 0, "already-reported": 0, "rejected": 0}
    for run in result["items"]:
        try: summary[report_run(client, api, contract, template, digest, plan, report_digest, run)] += 1
        except Exception: summary["rejected"] += 1
    cursor = result.get("metadata", {}).get("continue", "")
    if not isinstance(cursor, str) or len(cursor) > 4096: raise ValueError("Invalid server continuation token")
    return summary, cursor


def operate(root, digest, *, once=False):
    if os.name != "posix" or os.geteuid() == 0: raise ValueError("Use a separate non-root Linux reporter identity")
    selected, contract, template = load_bundle(root, digest)
    bridge.common.verify_running_sources({"files_sha256": selected["files_sha256"]}, Path(__file__))
    plan = selected["plan"]; original = contract["plan"]
    api = GerritReview(original, plan)
    client = Client(original["management_target"])
    with client.frozen():
        cursor = ""
        while True:
            summary, cursor = poll(client, api, contract, template, plan["source_contract_sha256"], plan, digest, cursor)
            print(json.dumps({**summary, **{k: False for k in bridge.FALSE_FLAGS}}, sort_keys=True), flush=True)
            if once: return summary
            time.sleep(plan["poll_seconds"])


def failure_diagnostic(error):
    locations = []
    frame = error.__traceback__
    while frame is not None:
        code = frame.tb_frame.f_code
        name = Path(code.co_filename).name
        if re.fullmatch(r"[a-z_]+[.]py", name) and re.fullmatch(r"[A-Za-z_<>]+", code.co_name):
            locations.append(name + ":" + str(frame.tb_lineno) + " " + code.co_name)
        frame = frame.tb_next
    result = {"exception_type": type(error).__name__, "code_locations": locations[-8:]}
    if isinstance(error, KubernetesOperationError): result["kubernetes"] = error.diagnostic
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--contract-sha256", required=True)
    mode = parser.add_mutually_exclusive_group(required=True); mode.add_argument("--once", action="store_true"); mode.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    try: operate(args.bundle_dir, args.contract_sha256, once=args.once); return 0
    except Exception as error:
        print(json.dumps({"gerrit_reporter_failure": failure_diagnostic(error)}, sort_keys=True), file=sys.stderr)
        print("Gerrit result reporting stopped; private identity, source and API details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
