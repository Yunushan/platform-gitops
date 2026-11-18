#!/usr/bin/env python3
"""Connect live Jenkins/Gerrit evidence to the guarded Argo CD handoff.

This module is a programmatic adapter; its caller must authenticate and verify
the Kubernetes management target and use separate private Git credentials.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from bounded_file import read_bounded_text
import ci_gitops_handoff as handoff
from kubernetes_management import Client, KubernetesOperationError, verify_target
import report_jenkins_gerrit as reporter
from strict_json import loads_strict_json
import tekton_forge_bridge as bridge
import verify_jenkins_gerrit as verifier


def validate(policy):
    if not isinstance(policy, dict) or set(policy) != {"schema_version", "runtime", "report", "promotion"} or type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise handoff.HandoffError("invalid-jenkins-handoff-policy")
    report = reporter.validate(policy["report"])
    if policy["runtime"] not in {"rke2", "k3s", "k8s", "docker", "raw"}:
        raise handoff.HandoffError("invalid-runtime")
    trigger = report["trigger"]
    promotion = policy["promotion"]
    digest = reporter.reporter.fingerprint(report)
    clone = trigger["gerrit_origin"] + "/a/" + trigger["project"] + ".git"
    contract = {"runtime": policy["runtime"], "plan": {"source": {"branch": trigger["branch"], "clone_url": clone}}}
    handoff.validate_policy(promotion, contract)
    if (promotion["bridge_contract_sha256"] != digest or promotion["cd"]["kind"] != "argocd"
            or promotion["git_username"].casefold() == report["reviewer_username"].casefold()):
        raise handoff.HandoffError("jenkins-handoff-authority-or-policy-mismatch")
    return report, contract


def execute(policy, queue_journal, review_journal_dir, jenkins_get, gerrit_get,
            reviewer_api, cd_client, *, git_inputs, apply=False, record=None):
    report, contract = validate(policy)
    promotion = policy["promotion"]
    if type(apply) is not bool: raise handoff.HandoffError("invalid-handoff-mode")
    verify_target(cd_client, promotion["cd"]["management_target"])
    def verify(_client, _contract, _template, _digest, _name):
        evidence = reporter.verify_merged(report, queue_journal, review_journal_dir,
                                          jenkins_get, gerrit_get, reviewer_api)
        return {"revision": evidence["revision"], "uid": "jenkins-queue-" + str(evidence["queue_id"])}, evidence
    return handoff.execute(promotion, contract, None, None, cd_client, "jenkins", apply=apply,
                           record=record, verify_ci=verify, git_inputs=git_inputs)


def private_readers(report, git_username, inputs, *, jenkins_tls=None, gerrit_tls=None):
    trigger = report["trigger"]
    users = [bridge.private_input(name + "-user", minimum=1, maximum=100, root=inputs).decode("ascii")
             for name in ("jenkins", "gerrit", "reviewer")]
    if (any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", user) for user in users)
            or users[2] != report["reviewer_username"]
            or len({users[1].casefold(), users[2].casefold(), git_username.casefold()}) != 3):
        raise ValueError("separate-identities-required")
    tokens = [bridge.private_input(name + "-token", minimum=16, maximum=512, root=inputs)
              for name in ("jenkins", "gerrit", "reviewer")]
    if any(any(c < 33 or c > 126 for c in token) for token in tokens):
        raise ValueError("invalid-token")
    return (verifier.requester(trigger["jenkins_origin"], users[0], tokens[0], tls_context=jenkins_tls),
            verifier.requester(trigger["gerrit_origin"], users[1], tokens[1], prefix=True, tls_context=gerrit_tls),
            reporter.ReviewAPI(report, tokens[2], tls_context=gerrit_tls))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--queue-journal", type=Path, required=True)
    parser.add_argument("--review-journal-dir", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--git-input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    record = None; state = {}
    try:
        if os.name != "posix": raise ValueError("linux-required")
        policy = loads_strict_json(read_bounded_text(args.policy, max_bytes=32768))
        report, _ = validate(policy)
        readers = private_readers(report, policy["promotion"]["git_username"], args.input_dir)
        token = bridge.private_input("token", minimum=16, maximum=512, root=args.git_input_dir)
        if any(c < 33 or c > 126 for c in token): raise ValueError("invalid-token")
        bridge.private_input("ca.crt", maximum=256 * 1024, root=args.git_input_dir)
        record, state = handoff.new_journal(args.output_dir, {"schema_version": 1, "policy_sha256": handoff.sha256(policy),
                                            "source_provider": "gerrit", "controller_kind": "argocd", "controller_verified": False, "full_stack_verified": False})
        target = policy["promotion"]["cd"]["management_target"]
        bridge.validate_target(target)
        with bridge.frozen_target({"management_target": target}, inputs=args.input_dir, kubeconfig_name="cd-kubeconfig"):
            result = execute(policy, args.queue_journal, args.review_journal_dir, *readers, Client(target), git_inputs=args.git_input_dir,
                apply=args.apply, record=record)
    except Exception as error:
        code = str(error) if isinstance(error, handoff.HandoffError) and re.fullmatch(r"[a-z0-9-]{1,100}", str(error)) else "jenkins-handoff-validation-or-transport-failed"
        diagnostic = {"exception_type": type(error).__name__ if type(error).__name__ in {
            "HandoffError", "KubernetesOperationError", "TimeoutError", "HTTPError", "URLError",
            "SSLError", "SSLCertVerificationError", "PermissionError", "FileNotFoundError", "ValueError"} else "other"}
        if isinstance(error, KubernetesOperationError): diagnostic["kubernetes"] = error.diagnostic
        if record is not None:
            try: record(phase="failed", failed_at=state.get("phase"), error_code=code, failure_diagnostic=diagnostic)
            except Exception: pass
        print("Jenkins handoff stopped: " + code + "; retain the private proof and inspect the outcome before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": sys.exit(main())
