#!/usr/bin/env python3
"""Inspect locally, or explicitly promote verified CI to a GitOps deployment branch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import subprocess
import tempfile
import time
from urllib.request import Request

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from bounded_subprocess import run_bounded
from http_transport import open_http_request, read_bounded_response
from kubernetes_management import KubernetesOperationError
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds
from tekton_checkout import git_environment
import tekton_forge_bridge as bridge
import tekton_forge_reporter as reporter


ROOT = Path(__file__).resolve().parents[1]
INPUTS = Path("/run/platform/promoter")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


class HandoffError(ValueError):
    """A static, public diagnostic code, never raw server/Git output."""


def sha256(value) -> str:
    return hashlib.sha256(bridge.canonical(value)).hexdigest()


def git_branch(value) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value) is not None
            and ".." not in value and all(part and not part.startswith(".") and not part.endswith((".", ".lock")) for part in value.split("/")))


def manifest_path(value) -> str:
    if not isinstance(value, str) or len(value) > 240 or not value or "\\" in value or any(ord(ch) < 32 for ch in value):
        raise HandoffError("invalid-manifest-path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise HandoffError("invalid-manifest-path")
    return str(path)


def validate_policy(plan: dict, contract: dict) -> None:
    fields = {"schema_version", "runtime", "bridge_contract_sha256", "deployment_branch", "allow_create_branch", "allow_prune",
              "git_username", "timeout_seconds", "cd"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise HandoffError("invalid-handoff-plan")
    if (not isinstance(plan["bridge_contract_sha256"], str) or not DIGEST.fullmatch(plan["bridge_contract_sha256"])
            or plan["runtime"] != contract["runtime"] or not git_branch(plan["deployment_branch"])
            or plan["deployment_branch"] == contract["plan"]["source"]["branch"]
            or any(type(plan[k]) is not bool for k in ("allow_create_branch", "allow_prune"))
            or not isinstance(plan["git_username"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", plan["git_username"])
            or type(plan["timeout_seconds"]) is not int or not 30 <= plan["timeout_seconds"] <= 900):
        raise HandoffError("invalid-handoff-policy")
    cd = plan["cd"]
    base = {"kind", "management_target", "namespace", "name", "uid", "spec_sha256", "path"}
    extra = {"source_namespace", "source_name", "source_uid", "source_spec_sha256"}
    if not isinstance(cd, dict) or cd.get("kind") not in {"argocd", "flux"} or set(cd) != base | (extra if cd["kind"] == "flux" else set()):
        raise HandoffError("invalid-cd-target")
    bridge.validate_target(cd["management_target"])
    reporter.uid(cd["uid"])
    manifest_path(cd["path"])
    for key in ("namespace", "name") + (("source_namespace", "source_name") if cd["kind"] == "flux" else ()):
        if not isinstance(cd[key], str) or not bridge.NAME.fullmatch(cd[key]) or len(cd[key]) > 63:
            raise HandoffError("invalid-cd-object-name")
    for key in ("spec_sha256",) + (("source_spec_sha256",) if cd["kind"] == "flux" else ()):
        if not isinstance(cd[key], str) or not DIGEST.fullmatch(cd[key]):
            raise HandoffError("missing-reviewed-spec-digest")
    if cd["kind"] == "flux":
        reporter.uid(cd["source_uid"])


def validate(plan: dict, contract: dict) -> None:
    validate_policy(plan, contract)
    if contract["plan"]["schema_version"] == 2:
        raise HandoffError("artifact-producing-run-requires-signed-promotion")
    if not bridge.reporting_settings(contract["plan"])["enabled"]:
        raise HandoffError("ci-reporting-must-be-enabled")


def read_api(plan: dict, suffix: str, *, inputs=None) -> tuple[dict, dict]:
    inputs = INPUTS if inputs is None else inputs
    token = bridge.private_input("api-token", minimum=16, maximum=512, root=inputs).decode("ascii")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in token):
        raise HandoffError("invalid-private-read-token")
    request = Request(plan["source"]["api_url"] + suffix, headers={"Authorization": "token " + token, "Accept": "application/json"})
    with open_http_request(request, timeout=10, use_environment_proxy=False, tls_context=bridge.source_tls(plan, inputs)) as response:
        payload = loads_strict_json(read_bounded_response(response, max_bytes=256 * 1024))
        headers = {k.casefold(): v for k, v in response.headers.items()}
    if not isinstance(payload, dict):
        raise HandoffError("invalid-source-api-response")
    return payload, headers


def verified_ci(client, contract, template, digest, run_name, *, requester=None) -> tuple[dict, dict]:
    requester = read_api if requester is None else requester
    run = client.get("pipelinerun.tekton.dev/" + run_name, contract["plan"]["namespace"])
    if not run:
        raise HandoffError("ci-run-missing")
    data, evidence = reporter.evidence(client, contract, template, digest, run)
    if evidence["state"] != "success":
        raise HandoffError("ci-run-has-not-passed")
    context = bridge.reporting_settings(contract["plan"])["context"] + "/" + digest[:12]
    payload = {"context": context, "state": "success", "description": "Tekton success; run " + data["uid"], "target_url": ""}
    record = {**data, "state": "success", "context": context, "payload_sha256": sha256(payload), "evidence_sha256": sha256(evidence)}
    bridge.receipt(client, contract["plan"]["namespace"], data["run"] + "-report", digest, record, create=False)
    source = contract["plan"]["source"]
    repository, _ = requester(contract["plan"], reporter.repo_base(contract["plan"]))
    if type(repository.get("id")) is not int or repository["id"] != source["repository_id"] or repository.get("full_name") != source["repository"]:
        raise HandoffError("source-repository-identity-changed")
    actual = reporter.latest_status(contract["plan"], data["revision"], context, requester=requester)
    if not reporter.same_status(actual, payload):
        raise HandoffError("verified-ci-status-missing-or-drifted")
    branch, _ = requester(contract["plan"], reporter.repo_base(contract["plan"]) + "/branches/" + bridge.quote(source["branch"], safe=""))
    if branch.get("commit", {}).get("id") != data["revision"]:
        raise HandoffError("ci-run-is-not-the-current-source-head")
    if contract["plan"]["schema_version"] == 2 and (repository.get("clone_url") != source["clone_url"]
            or branch.get("name") != source["branch"] or branch.get("protected") is not True):
        raise HandoffError("artifact-source-branch-or-repository-changed")
    return data, evidence


def reviewed_object(client, resource, namespace, name, expected_uid, digest):
    obj = client.get(resource + "/" + name, namespace)
    if (not obj or obj.get("metadata", {}).get("uid") != expected_uid
            or sha256(obj.get("spec")) != digest or obj.get("metadata", {}).get("deletionTimestamp")):
        raise HandoffError("gitops-object-identity-or-spec-changed")
    return obj


def cd_objects(client, plan, contract) -> tuple[dict, dict | None]:
    cd, url = plan["cd"], contract["plan"]["source"]["clone_url"]
    resource = "application.argoproj.io" if cd["kind"] == "argocd" else "kustomization.kustomize.toolkit.fluxcd.io"
    obj = reviewed_object(client, resource, cd["namespace"], cd["name"], cd["uid"], cd["spec_sha256"])
    spec = obj["spec"]
    if cd["kind"] == "argocd":
        source = spec.get("source", {})
        automated = spec.get("syncPolicy", {}).get("automated")
        if (spec.get("sources") or source.get("chart") or source.get("repoURL") != url or source.get("targetRevision") != plan["deployment_branch"]
                or manifest_path(source.get("path")) != manifest_path(cd["path"])
                or not isinstance(automated, dict) or automated.get("enabled") is False
                or automated.get("prune") and not plan["allow_prune"]):
            raise HandoffError("argocd-source-or-automatic-sync-policy-mismatch")
        return obj, None
    source = reviewed_object(client, "gitrepository.source.toolkit.fluxcd.io", cd["source_namespace"], cd["source_name"], cd["source_uid"], cd["source_spec_sha256"])
    ref = spec.get("sourceRef", {})
    if (ref.get("kind") != "GitRepository" or ref.get("name") != cd["source_name"] or ref.get("namespace", cd["namespace"]) != cd["source_namespace"]
            or source["spec"].get("url") != url or source["spec"].get("ref") != {"branch": plan["deployment_branch"]}
            or source["spec"].get("suspend") or source["spec"].get("include") or spec.get("suspend") or spec.get("force")
            or manifest_path(spec.get("path")) != manifest_path(cd["path"]) or spec.get("wait") is not True
            or not spec.get("serviceAccountName") or spec.get("prune") and not plan["allow_prune"]):
        raise HandoffError("flux-source-or-reconciliation-policy-mismatch")
    return obj, source


def current_ready(obj):
    generation = obj.get("metadata", {}).get("generation")
    status = obj.get("status", {})
    ready = [c for c in status.get("conditions", []) if c.get("type") == "Ready"]
    return (type(generation) is int and generation > 0 and status.get("observedGeneration") == generation
            and len(ready) == 1 and ready[0].get("status") == "True" and ready[0].get("observedGeneration") == generation
            and not any(c.get("type") in {"Stalled", "Reconciling"} and c.get("status") == "True" for c in status.get("conditions", [])))


def read_observation(observe, record=None) -> bool:
    """Retry only a read-only controller observation, within its caller's deadline."""
    try:
        return observe()
    except (subprocess.TimeoutExpired, KubernetesOperationError) as error:
        signals = getattr(error, "diagnostic", {}).get("signals", {})
        transport={"timeout", "connection_failure", "unavailable"}
        if signals.get("tls_handshake_timeout") is True and signals.get("tls_non_timeout_failure") is False:
            transport.update({"tls_failure","tls_handshake_timeout"})
        transient = isinstance(error, subprocess.TimeoutExpired) or (
            any(signals.get(name) for name in ("timeout", "connection_failure", "unavailable"))
            and not any(value for name, value in signals.items() if name not in transport))
        if not transient:
            raise
        if record is not None:
            record(controller_observation_transport_retry={"exception_type": type(error).__name__, "signals": signals})
        return False


def argocd_observed(obj, revision, *, since: datetime | None = None) -> bool:
    status = obj.get("status", {})
    operation = status.get("operationState", {})
    if any(c.get("type", "").endswith("Error") for c in status.get("conditions", [])):
        return False
    if (status.get("sync", {}).get("status") != "Synced" or status["sync"].get("revision") != revision
            or status.get("health", {}).get("status") != "Healthy" or operation.get("phase") != "Succeeded"
            or operation.get("syncResult", {}).get("revision") != revision):
        return False
    reconciled = reporter.instant(status.get("reconciledAt"))
    if reconciled > datetime.now(timezone.utc) + timedelta(seconds=30):
        return False
    return reconciled >= since if since is not None else True


def cd_observed(client, plan, contract, revision, *, since: datetime | None = None) -> bool:
    obj, source = cd_objects(client, plan, contract)
    status = obj.get("status", {})
    if plan["cd"]["kind"] == "argocd":
        return argocd_observed(obj, revision, since=since)
    expected = plan["deployment_branch"] + "@sha1:" + revision
    return current_ready(obj) and current_ready(source) and source.get("status", {}).get("artifact", {}).get("revision") == expected and status.get("lastAppliedRevision") == expected


class GitBranch:
    def __init__(self, url: str, username: str, inputs: Path, directory: Path):
        self.url = url
        self.env, empty = git_environment(username, inputs, directory)
        self.repository = directory / "objects.git"
        self.prefix = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull, "-c", "http.followRedirects=false",
                       "-c", "http.sslVerify=true", "-c", "protocol.ext.allow=never", "-C", str(self.repository)]
        self.repository.mkdir(mode=0o700)
        self.call(["init", "--bare", "--template=" + str(empty)])

    def call(self, args, *, optional=False):
        result = run_bounded([*self.prefix, *args], env=self.env, input="", text=True,
                             timeout=bounded_timeout_seconds(180, "PLATFORM_HANDOFF_GIT_TIMEOUT_SECONDS"),
                             output_max_bytes=256 * 1024, check=False)
        if result.returncode and not optional:
            raise HandoffError("git-operation-failed")
        return result

    def ref(self, branch):
        output = self.call(["ls-remote", "--heads", self.url, "refs/heads/" + branch]).stdout
        rows = output.splitlines()
        if not rows:
            return None
        parts = rows[0].split()
        if len(rows) != 1 or len(parts) != 2 or not SHA.fullmatch(parts[0]) or parts[1] != "refs/heads/" + branch:
            raise HandoffError("ambiguous-remote-branch")
        return parts[0]

    def prepare(self, branch, revision, allow_create):
        before = self.ref(branch)
        if before is None and not allow_create:
            raise HandoffError("deployment-branch-creation-not-authorized")
        self.call(["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", self.url, revision])
        self.call(["cat-file", "-e", revision + "^{commit}"])
        if before is not None:
            self.call(["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", self.url, before])
            if self.call(["merge-base", "--is-ancestor", before, revision], optional=True).returncode != 0:
                raise HandoffError("deployment-would-not-fast-forward")
        return before

    def check_path(self, revision, path):
        path = manifest_path(path)
        expression = revision + ("^{tree}" if path == "." else ":" + path)
        result = self.call(["cat-file", "-t", expression], optional=True)
        if result.returncode or result.stdout.strip() != "tree":
            raise HandoffError("tested-commit-has-no-selected-manifest-directory")

    def promote(self, branch, revision, before):
        current = self.ref(branch)
        if current != before and current != revision:
            raise HandoffError("deployment-branch-changed-after-preflight")
        attempted = current != revision
        if attempted:
            ref = "refs/heads/" + branch
            # The fast-forward relation was checked in prepare(). The explicit
            # lease also rejects deletion or replacement after that check.
            self.call(["push", "--force-with-lease=" + ref + ":" + (before or ""), self.url, revision + ":" + ref], optional=True)
        if self.ref(branch) != revision:
            raise HandoffError("deployment-branch-push-not-verified")
        return attempted


def new_journal(path: Path, initial: dict):
    resolved = path.resolve()
    relative = resolved.relative_to((ROOT / "private").resolve())
    if not relative.parts or path.exists() or path.is_symlink():
        raise HandoffError("proof-directory-must-be-new-and-private")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = dict(initial)
    def record(**updates):
        state.update(updates, recorded_at=datetime.now(timezone.utc).isoformat())
        atomic_write_text(resolved / "handoff-proof.json", json.dumps(state, sort_keys=True, indent=2) + "\n")
    record(phase="local-validated")
    return record, state


def execute(plan, contract, template, ci_client, cd_client, run_name, *, apply=False, record=None, verify_ci=None, git_inputs=None):
    digest = plan["bridge_contract_sha256"]
    record = record or (lambda **kwargs: None)
    verify_ci = verified_ci if verify_ci is None else verify_ci
    data, evidence = verify_ci(ci_client, contract, template, digest, run_name)
    record(phase="ci-verified", source_revision=data["revision"], pipeline_run_uid=data["uid"], ci_evidence_sha256=sha256(evidence))
    app, _ = cd_objects(cd_client, plan, contract)
    active = plan["cd"]["kind"] == "argocd" and (app.get("operation") or app.get("status", {}).get("operationState", {}).get("phase") in {"Running", "Terminating"})
    record(phase="cd-config-verified")
    with tempfile.TemporaryDirectory(prefix="ci-gitops-promotion-") as directory:
        git = GitBranch(contract["plan"]["source"]["clone_url"], plan["git_username"], INPUTS if git_inputs is None else git_inputs, Path(directory))
        if git.ref(contract["plan"]["source"]["branch"]) != data["revision"]:
            raise HandoffError("git-source-ref-no-longer-matches-tested-head")
        before = git.prepare(plan["deployment_branch"], data["revision"], plan["allow_create_branch"])
        git.check_path(data["revision"], plan["cd"]["path"])
        if active and before != data["revision"]:
            raise HandoffError("argocd-operation-is-already-active")
        record(phase="git-preflight-verified", previous_deployment_revision=before)
        if not apply:
            return {"read_only": True, "preflight_verified": True, "branch_push_attempted": False, "controller_verified": False}
        refreshed, proof = verify_ci(ci_client, contract, template, digest, run_name)
        refreshed_app, _ = cd_objects(cd_client, plan, contract)
        if (plan["cd"]["kind"] == "argocd" and before != data["revision"]
                and (refreshed_app.get("operation") or refreshed_app.get("status", {}).get("operationState", {}).get("phase") in {"Running", "Terminating"})):
            raise HandoffError("argocd-operation-is-already-active")
        if refreshed != data or proof != evidence or git.ref(contract["plan"]["source"]["branch"]) != data["revision"]:
            raise HandoffError("ci-or-source-evidence-changed-before-push")
        started = datetime.now(timezone.utc)
        record(phase="pre-push-verified")
        pushed = git.promote(plan["deployment_branch"], data["revision"], before)
        record(phase="git-promoted", branch_push_attempted=pushed, promoted_revision=data["revision"])
        deadline = time.monotonic() + plan["timeout_seconds"]
        while True:
            try:
                deployment_revision = git.ref(plan["deployment_branch"])
            except (HandoffError, subprocess.TimeoutExpired) as error:
                if isinstance(error, HandoffError) and str(error) != "git-operation-failed":
                    raise
                record(phase="git-promoted", deployment_ref_transport_retry=True)
                if time.monotonic() >= deadline:
                    raise HandoffError("controller-reconciliation-not-verified-before-timeout") from error
                time.sleep(5)
                continue
            if deployment_revision != data["revision"]:
                raise HandoffError("deployment-branch-was-superseded")
            observed = read_observation(lambda: cd_observed(cd_client, plan, contract, data["revision"], since=started if before != data["revision"] else None), record)
            expired = time.monotonic() >= deadline
            if observed and not expired:
                record(phase="controller-verified", controller_verified=True)
                return {"read_only": False, "preflight_verified": True, "branch_push_attempted": pushed, "controller_verified": True,
                        "kubernetes_workload_scope": True, "full_stack_verified": False}
            if expired:
                raise HandoffError("controller-reconciliation-not-verified-before-timeout")
            time.sleep(5)


def operate(plan_path: Path, bundle: Path, run_name: str, *, apply=False, check_server=False, output: Path | None = None):
    plan = loads_strict_json(read_bounded_text(plan_path, encoding="utf-8"))
    contract, template = bridge.load_bundle(bundle, plan.get("bridge_contract_sha256", ""))
    bridge.verify_running_sources(contract, Path(__file__))
    validate(plan, contract)
    if not isinstance(run_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", run_name):
        raise HandoffError("invalid-pipeline-run-name")
    if not apply and not check_server:
        return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or output is None:
        raise HandoffError("server-mode-requires-linux-and-new-private-proof-directory")
    record, state = new_journal(output, {"schema_version": 1, "plan_sha256": sha256(plan), "bridge_contract_sha256": plan["bridge_contract_sha256"],
                                       "runtime": plan["runtime"], "controller_kind": plan["cd"]["kind"], "controller_verified": False, "full_stack_verified": False})
    try:
        bridge.private_input("token", minimum=16, maximum=512, root=INPUTS)
        bridge.private_input("api-token", minimum=16, maximum=512, root=INPUTS)
        with bridge.frozen_target(contract["plan"], inputs=INPUTS, kubeconfig_name="ci-kubeconfig"):
            ci_client = bridge.Client(contract["plan"]["management_target"])
            bridge.verify_target(ci_client, contract["plan"]["management_target"])
            with bridge.frozen_target({"management_target": plan["cd"]["management_target"]}, inputs=INPUTS, kubeconfig_name="cd-kubeconfig"):
                cd_client = bridge.Client(plan["cd"]["management_target"])
                bridge.verify_target(cd_client, plan["cd"]["management_target"])
                return execute(plan, contract, template, ci_client, cd_client, run_name, apply=apply, record=record)
    except Exception as error:
        record(phase="failed", failed_at=state["phase"], error_code=str(error) if isinstance(error, HandoffError) else "handoff-validation-or-transport-failed")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--pipeline-run", required=True)
    parser.add_argument("--output-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-server", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = operate(args.plan, args.bundle_dir, args.pipeline_run, apply=args.apply, check_server=args.check_server, output=args.output_dir)
        print(json.dumps(result))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, HandoffError) else "handoff-validation-or-transport-failed"
        print("CI GitOps handoff stopped: " + code + ". Retain the private proof; no automatic rollback was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
