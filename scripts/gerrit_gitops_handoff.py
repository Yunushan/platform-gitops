#!/usr/bin/env python3
"""Explicit promotion of an exact tested, reported and merged Gerrit head."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request

from bounded_file import read_bounded_bytes
import ci_gitops_handoff as handoff
from kubernetes_management import Client, KubernetesOperationError, verify_target
from strict_json import loads_strict_json
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter


ROOT = Path(__file__).resolve().parents[1]
INPUTS = Path("/run/platform/gerrit-promoter")
PROGRAMS = ("gerrit_gitops_handoff.py", "ci_gitops_handoff.py", "atomic_file.py")
EXTRA = {"reporter_contract_sha256", "source_read_username", "source_read_account_id"}


def failure_diagnostic(error):
    """Keep bounded code/type evidence without exception text, locals or URLs."""
    allowed_types = {"HandoffError", "KubernetesOperationError", "TimeoutError",
        "HTTPError", "URLError", "SSLCertVerificationError", "SSLError",
        "ConnectionRefusedError", "ConnectionResetError", "RemoteDisconnected",
        "PermissionError", "FileNotFoundError", "ValueError", "JSONDecodeError",
        "CalledProcessError", "TimeoutExpired", "OutputLimitExceeded"}
    diagnostic = {"exception_type": type(error).__name__ if type(error).__name__ in allowed_types else "other",
        "code_locations": []}
    if isinstance(error, KubernetesOperationError):
        diagnostic["kubernetes"] = error.diagnostic
    if isinstance(error, HTTPError) and type(error.code) is int and 400 <= error.code <= 599:
        diagnostic["http_status"] = error.code
    known_files = {*bridge.PROGRAMS, *reporter.EXTRA_PROGRAMS, *PROGRAMS}
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame.f_code
        filename = Path(frame.co_filename).name
        if filename in known_files and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", frame.co_name):
            diagnostic["code_locations"].append({"file": filename, "function": frame.co_name, "line": trace.tb_lineno})
            diagnostic["code_locations"] = diagnostic["code_locations"][-8:]
        trace = trace.tb_next
    return diagnostic


def validate(plan, selected, contract):
    if contract.get("plan", {}).get("schema_version") == 2:
        raise handoff.HandoffError("artifact-digest-promotion-requires-explicit-policy")
    validate_authorities(plan, selected, contract)


def validate_authorities(plan, selected, contract):
    """Shared reader/writer separation; callers enforce their own plan version."""
    if not isinstance(plan, dict) or not EXTRA <= set(plan): raise handoff.HandoffError("invalid-gerrit-handoff-plan")
    common = {k: v for k, v in plan.items() if k not in EXTRA}
    handoff.validate_policy(common, contract)
    if (plan["bridge_contract_sha256"] != selected["plan"]["source_contract_sha256"]
            or not isinstance(plan["reporter_contract_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["reporter_contract_sha256"])
            or plan["cd"]["kind"] != "argocd" or type(plan["source_read_account_id"]) is not int
            or not isinstance(plan["source_read_username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", plan["source_read_username"])
            or len({plan["source_read_username"].casefold(), plan["git_username"].casefold(), selected["plan"]["http_username"].casefold()}) != 3
            or plan["source_read_account_id"] == selected["plan"]["account_id"]): raise handoff.HandoffError("invalid-gerrit-handoff-authorities")
    bridge.number(plan["source_read_account_id"])


class ReadAPI(bridge.Gerrit):
    def __init__(self, original, plan, inputs=INPUTS):
        chosen = copy.deepcopy(original); chosen["source"]["read_username"] = plan["source_read_username"]
        super().__init__(chosen, inputs)
        self.original = original

    def request(self, path):
        project = quote(self.original["source"]["project"], safe="")
        branch = quote(self.original["source"]["branch"], safe="")
        if not (path in {"/a/accounts/self", "/a/projects/" + project + "/labels/?inherited", "/a/projects/" + project + "/branches/" + branch}
                or re.fullmatch(r"/a/changes/[1-9][0-9]{0,9}/detail\?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES", path)):
            raise handoff.HandoffError("source-read-outside-fixed-scope")
        request = Request(self.original["source"]["api_url"] + path, headers={"Authorization": self.authorization, "Accept": "application/json"})
        raw = self.response_bytes(request)
        if not raw.startswith(b")]}'\n"): raise handoff.HandoffError("invalid-gerrit-read-response")
        return loads_strict_json(raw[5:])


def verified_ci(client, api, selected, contract, template, plan, run_name):
    original = contract["plan"]; digest = plan["bridge_contract_sha256"]; report_digest = plan["reporter_contract_sha256"]
    verify_target(client, original["management_target"])
    namespace = client.get("namespace/" + original["namespace"])
    if not namespace or namespace.get("metadata", {}).get("uid") != original["namespace_uid"] or namespace["metadata"].get("deletionTimestamp") or namespace["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted": raise handoff.HandoffError("original-ci-namespace-changed")
    run = client.get("pipelinerun.tekton.dev/" + run_name, original["namespace"])
    if run is None: raise handoff.HandoffError("ci-run-missing")
    data, item, proof = reporter.evidence(client, contract, template, digest, run, selected["plan"])
    if proof["state"] != "success": raise handoff.HandoffError("ci-run-has-not-passed")
    account = api.request("/a/accounts/self")
    if not isinstance(account, dict) or type(account.get("_account_id")) is not int or account["_account_id"] != plan["source_read_account_id"] or account.get("username") != plan["source_read_username"] or account.get("inactive", False) is not False: raise handoff.HandoffError("original-source-reader-changed")
    reporter.label_policy(api, original, selected["plan"])
    obj = api.request("/a/changes/" + str(item["change"]) + "/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES")
    if bridge.current(original, obj, status="MERGED") != item: raise handoff.HandoffError("tested-patchset-is-not-the-merged-revision")
    payload = reporter.review_payload(data, item, report_digest, "success")
    record = {**data, "reporter_account_id": str(selected["plan"]["account_id"]), "reporter_contract_sha256": report_digest,
        "state": "success", "payload_sha256": reporter.fingerprint(payload), "evidence_sha256": reporter.fingerprint(proof)}
    name = data["run"] + "-vote"
    _, reservation_uid = bridge.receipt(client, original["namespace"], name + "-lock", report_digest, record, create=False)
    bridge.receipt(client, original["namespace"], name, report_digest, {**record, "reservation_uid": reservation_uid}, create=False)
    if not reporter.published(obj, selected["plan"], item, payload): raise handoff.HandoffError("original-successful-review-was-not-published")
    branch = api.request("/a/projects/" + quote(original["source"]["project"], safe="") + "/branches/" + quote(original["source"]["branch"], safe=""))
    if not isinstance(branch, dict) or branch.get("ref") != "refs/heads/" + original["source"]["branch"] or branch.get("revision") != item["revision"]: raise handoff.HandoffError("merged-commit-is-not-the-current-source-head")
    return data, {**proof, "merged_revision": item["revision"], "source_reader_account_id": plan["source_read_account_id"]}


def load_bundle(root, digest):
    raw = read_bounded_bytes(root / "gerrit-handoff-contract.json", max_bytes=128 * 1024)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest: raise handoff.HandoffError("handoff-package-changed")
    selected = loads_strict_json(raw)
    if not isinstance(selected, dict) or set(selected) != {"schema_version", "plan", "files_sha256"} or type(selected["schema_version"]) is not int or selected["schema_version"] != 1: raise handoff.HandoffError("invalid-handoff-package")
    expected = {*bridge.PROGRAMS, *reporter.EXTRA_PROGRAMS, *PROGRAMS, "run-template.json", "gerrit-event-contract.json", "gerrit-report-contract.json"}
    if set(selected["files_sha256"]) != expected: raise handoff.HandoffError("incomplete-handoff-package")
    for name, sha in selected["files_sha256"].items():
        if hashlib.sha256(read_bounded_bytes(root / name, max_bytes=bridge.LIMIT)).hexdigest() != sha: raise handoff.HandoffError("handoff-helper-changed")
    report, contract, template = reporter.load_bundle(root, selected["plan"]["reporter_contract_sha256"])
    validate(selected["plan"], report, contract)
    return selected, report, contract, template


def execute(plan, report, contract, template, ci, cd, api, run_name, *, apply=False, record=None):
    def verify(client, original, pipeline, source_digest, name):
        return verified_ci(client, api, report, original, pipeline, plan, name)
    return handoff.execute(plan, contract, template, ci, cd, run_name, apply=apply, record=record, verify_ci=verify, git_inputs=INPUTS)


def operate(root, digest, run_name, *, apply=False, check_server=False, output=None):
    selected, report, contract, template = load_bundle(root, digest)
    bridge.common.verify_running_sources({"files_sha256": selected["files_sha256"]}, Path(__file__))
    plan = selected["plan"]
    if not isinstance(run_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", run_name): raise handoff.HandoffError("invalid-pipeline-run-name")
    if not apply and not check_server: return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or output is None: raise handoff.HandoffError("server-mode-requires-linux-and-private-proof-directory")
    record, state = handoff.new_journal(output, {"schema_version": 1, "plan_sha256": handoff.sha256(plan), "handoff_contract_sha256": digest,
        "source_provider": "gerrit", "bridge_contract_sha256": plan["bridge_contract_sha256"], "reporter_contract_sha256": plan["reporter_contract_sha256"],
        "runtime": plan["runtime"], "controller_kind": "argocd", "controller_verified": False, "full_stack_verified": False})
    try:
        bridge.common.private_input("token", minimum=16, maximum=512, root=INPUTS)
        api = ReadAPI(contract["plan"], plan)
        with bridge.common.frozen_target(contract["plan"], inputs=INPUTS, kubeconfig_name="ci-kubeconfig"):
            ci = Client(contract["plan"]["management_target"])
            with bridge.common.frozen_target({"management_target": plan["cd"]["management_target"]}, inputs=INPUTS, kubeconfig_name="cd-kubeconfig"):
                cd = Client(plan["cd"]["management_target"]); verify_target(cd, plan["cd"]["management_target"])
                return execute(plan, report, contract, template, ci, cd, api, run_name, apply=apply, record=record)
    except Exception as error:
        record(phase="failed", failed_at=state["phase"], error_code=str(error) if isinstance(error, handoff.HandoffError) else "gerrit-handoff-validation-or-transport-failed",
            failure_diagnostic=failure_diagnostic(error))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--contract-sha256", required=True); parser.add_argument("--pipeline-run", required=True); parser.add_argument("--output-dir", type=Path)
    mode = parser.add_mutually_exclusive_group(); mode.add_argument("--check-server", action="store_true"); mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = operate(args.bundle_dir, args.contract_sha256, args.pipeline_run, apply=args.apply, check_server=args.check_server, output=args.output_dir)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception as error:
        code = str(error) if isinstance(error, handoff.HandoffError) else "gerrit-handoff-validation-or-transport-failed"
        print("Gerrit GitOps handoff stopped: " + code + ". Retain the private proof.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
