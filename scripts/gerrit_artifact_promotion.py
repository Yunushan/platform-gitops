#!/usr/bin/env python3
"""Explicit promotion of a merged Gerrit build's exact image to a reviewed Deployment."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import ci_gitops_handoff as common
import gerrit_gitops_handoff as legacy
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter
from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from kubernetes_management import Client, verify_target
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]
INPUTS = legacy.INPUTS
PROGRAMS = (*legacy.PROGRAMS, "gerrit_artifact_promotion.py")
CONTRACT = "gerrit-artifact-promotion-contract.json"


def legacy_plan(plan):
    return {**{k: v for k, v in plan.items() if k not in {"artifact_policy", "signature_policy"}}, "schema_version": 1}


def validate(plan, report, contract):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (2, 3) or "artifact_policy" not in plan:
        raise common.HandoffError("require-explicit-artifact-promotion-plan")
    if contract["plan"].get("schema_version") != 2:
        raise common.HandoffError("require-artifact-producing-ci-contract")
    if (plan["schema_version"] == 3) != ("signature_policy" in plan):
        raise common.HandoffError("signature-policy-requires-explicit-signed-promotion-version")
    if plan["schema_version"] == 3:
        import verify_image_signature as signatures
        selected = {"schema_version": 2, "image": contract["plan"]["publication"]["registry"]["origin"].removeprefix("https://") + "/"
            + contract["plan"]["publication"]["registry"]["repository"] + "@sha256:" + "0" * 64,
            "annotations": {"platform.gitops/validation": "pending"}, **signature_policy(plan)}
        signatures.validate(selected)
    legacy.validate_authorities(legacy_plan(plan), report, contract)
    validate_artifact_policy(plan)


def validate_artifact_policy(plan):
    policy = plan["artifact_policy"]
    fields = {"manifest", "namespace", "deployment", "deployment_uid", "container", "expected_image", "expected_deployment_revision", "commit_name", "commit_email", "destination_server"}
    if not isinstance(policy, dict) or set(policy) != fields:
        raise common.HandoffError("invalid-artifact-promotion-policy")
    for key in ("namespace", "deployment", "container"):
        if not isinstance(policy[key], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", policy[key]):
            raise common.HandoffError("invalid-artifact-workload-name")
    reporter.worker.uid(policy["deployment_uid"])
    server = policy["destination_server"]
    if not isinstance(server, str) or not server.startswith("https://") or any(ord(c) <= 32 for c in server):
        raise common.HandoffError("pin-verified-cd-destination-endpoint")
    parsed = urlsplit(server)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise common.HandoffError("pin-verified-cd-destination-endpoint")
    try: parsed.port
    except ValueError as error: raise common.HandoffError("pin-verified-cd-destination-endpoint") from error
    if (not isinstance(policy["commit_name"], str) or not 1 <= len(policy["commit_name"]) <= 120
            or policy["commit_name"] != policy["commit_name"].strip()
            or any(ord(c) < 32 or c in "<>" for c in policy["commit_name"])
            or not isinstance(policy["commit_email"], str) or len(policy["commit_email"]) > 254
            or not re.fullmatch(r"[A-Za-z0-9._+-]+@[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*[.][A-Za-z]{2,63}", policy["commit_email"])):
        raise common.HandoffError("pin-the-writers-registered-commit-identity")
    path = policy["manifest"]
    if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_/-]+[.]json", path)
            or common.manifest_path(path) != path or str(PurePosixPath(path).parent) != common.manifest_path(plan["cd"]["path"])):
        raise common.HandoffError("manifest-must-be-direct-json-child-of-reviewed-directory")
    if not isinstance(policy["expected_image"], str) or not bridge.common.IMAGE.fullmatch(policy["expected_image"]):
        raise common.HandoffError("pin-reviewed-source-manifest-image")
    before = policy["expected_deployment_revision"]
    if before is not None and (not isinstance(before, str) or not common.SHA.fullmatch(before)):
        raise common.HandoffError("pin-expected-deployment-branch-revision")
    if before is None and not plan["allow_create_branch"]:
        raise common.HandoffError("new-artifact-branch-creation-not-authorized")



def programs(plan):
    return (*PROGRAMS, "verify_image_signature.py") if plan["schema_version"] == 3 else PROGRAMS


def signature_policy(plan):
    import verify_image_signature as signatures
    policy = plan["signature_policy"]
    signatures.exact(policy, "cosign_binary registry_ca public_key trusted_root credential")
    return policy


def signature_annotations(plan, contract, data, evidence):
    """Bind the release authority to the same exact live CI evidence as promotion.

    Publicly logged values are digests/UIDs; project URLs and company names are
    not placed in annotations. Credential file hashes are deliberately excluded
    from the release policy identity, so token refresh does not change approval.
    """
    import verify_image_signature as signatures
    if "artifact" not in evidence or evidence.get("merged_revision") != data["revision"]:
        raise common.HandoffError("signature-requires-exact-merged-artifact-evidence")
    reporter.worker.uid(data["uid"])
    if not common.SHA.fullmatch(data["revision"]): raise common.HandoffError("invalid-signed-source-revision")
    scope = {k: v for k, v in plan.items() if k != "signature_policy"}
    source = contract["plan"]["source"]
    value = {
        "platform.gitops/source-identity-sha256": common.sha256({k: source[k] for k in ("project", "branch", "api_url", "clone_url")}),
        "platform.gitops/source-revision": data["revision"],
        "platform.gitops/pipeline-run-uid": data["uid"],
        "platform.gitops/ci-evidence-sha256": common.sha256(evidence),
        "platform.gitops/bridge-contract-sha256": plan["bridge_contract_sha256"],
        "platform.gitops/reporter-contract-sha256": plan["reporter_contract_sha256"],
        "platform.gitops/promotion-policy-sha256": common.sha256(scope),
        "platform.gitops/image-sha256": hashlib.sha256(evidence["artifact"]["image"].encode()).hexdigest(),
    }
    signatures.validate_annotations(value)
    return value


def verify_signature(plan, contract, data, evidence):
    if plan["schema_version"] == 2: return None
    selected = {"schema_version": 2, "image": evidence["artifact"]["image"],
        "annotations": signature_annotations(plan, contract, data, evidence), **signature_policy(plan)}
    return verify_release_signature(selected)


def verify_release_signature(selected):
    import verify_image_signature as signatures
    try:
        proof = signatures.operate(selected)
    except Exception:
        raise common.HandoffError("merged-ci-image-signature-not-verified") from None
    # Keep this check explicit even though the real verifier is in the pinned
    # helper package. A partial/local-inspection proof must never authorize Git.
    expected = {"plan_sha256": common.sha256(selected), "image": selected["image"],
        "cosign_version": signatures.COSIGN_VERSION, "cosign_sha256": signatures.COSIGN_SHA256,
        **{k + "_sha256": selected[k]["sha256"] for k in ("public_key", "trusted_root", "registry_ca")},
        "signed_annotations_sha256": common.sha256(selected["annotations"])}
    if (not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items())
            or any(proof.get(k) is not True for k in ("image_signature_verified", "transparency_inclusion_verified", "registry_tls_verified", "signed_annotations_verified"))
            or any(proof.get(k) is not False for k in ("admission_verified", "ci_evidence_verified", "release_authorization_verified", "ha_verified", "full_stack_verified"))):
        raise common.HandoffError("merged-ci-signature-proof-does-not-match")
    return proof

def manifest_image(document, policy):
    try:
        if (document["apiVersion"] != "apps/v1" or document["kind"] != "Deployment"
                or document["metadata"]["name"] != policy["deployment"] or document["metadata"]["namespace"] != policy["namespace"]):
            raise ValueError()
        containers = document["spec"]["template"]["spec"]["containers"]
        if not isinstance(containers, list) or not containers or any(not isinstance(c, dict) or not isinstance(c.get("name"), str) for c in containers):
            raise ValueError()
        if len({c["name"] for c in containers}) != len(containers):
            raise ValueError()
        selected = [c for c in containers if c["name"] == policy["container"]]
        if len(selected) != 1 or not isinstance(selected[0].get("image"), str):
            raise ValueError()
        return selected[0]
    except (KeyError, TypeError, ValueError) as error:
        raise common.HandoffError("reviewed-deployment-or-container-does-not-match") from error


class ArtifactGit(common.GitBranch):
    def object_call(self, arguments, payload="", extra_env=None):
        result = run_bounded([*self.prefix, *arguments], env={**self.env, **(extra_env or {})}, input=payload, text=True,
            timeout=180, output_max_bytes=512 * 1024, check=False)
        if result.returncode:
            raise common.HandoffError("artifact-git-object-operation-failed")
        return result.stdout

    def prepare_artifact(self, plan, data, evidence):
        policy = plan["artifact_policy"]; revision = data["revision"]; receipt = evidence["artifact"]
        before = policy["expected_deployment_revision"]
        current = self.ref(plan["deployment_branch"])
        self.call(["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", self.url, revision])
        if before is not None:
            self.call(["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--", self.url, before])
        self.check_path(revision, plan["cd"]["path"])
        entries = self.call(["ls-tree", "-r", "-z", "--full-tree", revision, "--", plan["cd"]["path"]]).stdout.split("\0")
        entries = [entry for entry in entries if entry]
        paths = {entry.partition("\t")[2] for entry in entries if entry.startswith("100644 blob ")}
        expected = {policy["manifest"]}
        manifest = PurePosixPath(policy["manifest"])
        build_file = str(manifest.with_name("kustomization.yaml"))
        if plan["cd"].get("kind") == "flux":
            if build_file not in paths:
                raise common.HandoffError("flux-artifact-directory-requires-kustomization")
            expected.add(build_file)
        if len(entries) != len(expected) or paths != expected:
            raise common.HandoffError("artifact-directory-must-contain-only-reviewed-regular-json-manifest")
        if build_file in expected:
            from strict_yaml import loads_strict_yaml_all
            build = self.call(["cat-file", "blob", revision + ":" + build_file]).stdout
            if loads_strict_yaml_all(build, yaml_12=True) != [{"apiVersion": "kustomize.config.k8s.io/v1beta1",
                    "kind": "Kustomization", "resources": [manifest.name]}]:
                raise common.HandoffError("flux-build-file-must-select-only-the-reviewed-deployment")
        raw = self.call(["cat-file", "blob", revision + ":" + policy["manifest"]]).stdout
        document = loads_strict_json(raw)
        selected = manifest_image(document, policy)
        if selected["image"] != policy["expected_image"]:
            raise common.HandoffError("reviewed-source-manifest-image-changed")
        selected["image"] = receipt["image"]
        # Build a new Git tree in a private index. No checkout, filters, hooks,
        # source commands, or source credentials execute here.
        index = str(self.repository.parent / "promotion.index")
        env = {"GIT_INDEX_FILE": index}
        self.object_call(["read-tree", revision], extra_env=env)
        content = bridge.common.canonical(document).decode() + "\n"
        blob = self.object_call(["hash-object", "-w", "--stdin"], content).strip()
        if not common.SHA.fullmatch(blob): raise common.HandoffError("invalid-promoted-manifest-object")
        self.object_call(["update-index", "--add", "--cacheinfo", "100644," + blob + "," + policy["manifest"]], extra_env=env)
        tree = self.object_call(["write-tree"], extra_env=env).strip()
        if not common.SHA.fullmatch(tree): raise common.HandoffError("invalid-promoted-tree-object")
        changed = self.call(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", revision, tree]).stdout
        if changed not in {"", policy["manifest"] + "\0"}:
            raise common.HandoffError("promotion-changed-files-outside-reviewed-manifest")
        timestamp = self.call(["show", "-s", "--format=%ct", revision]).stdout.strip()
        if not re.fullmatch(r"[0-9]{1,12}", timestamp): raise common.HandoffError("invalid-source-commit-time")
        identity = {"GIT_AUTHOR_NAME": policy["commit_name"], "GIT_AUTHOR_EMAIL": policy["commit_email"],
            "GIT_COMMITTER_NAME": policy["commit_name"], "GIT_COMMITTER_EMAIL": policy["commit_email"],
            "GIT_AUTHOR_DATE": "@" + timestamp + " +0000", "GIT_COMMITTER_DATE": "@" + timestamp + " +0000"}
        parents = [revision] + ([before] if before is not None and before != revision else [])
        message = {"schema_version": 1, "source_revision": revision, **build_identity(data),
            "ci_evidence_sha256": common.sha256(evidence), "plan_sha256": common.sha256(plan),
            "previous_deployment_revision": before, "artifact": receipt}
        arguments = ["commit-tree", tree]
        for parent in parents: arguments += ["-p", parent]
        target = self.object_call(arguments, "Platform artifact promotion\n\n" + bridge.common.canonical(message).decode() + "\n", identity).strip()
        if not common.SHA.fullmatch(target): raise common.HandoffError("invalid-promotion-commit")
        if current not in {before, target}:
            raise common.HandoffError("deployment-branch-differs-from-reviewed-before-or-exact-replay")
        if before is not None and self.call(["merge-base", "--is-ancestor", before, target], optional=True).returncode:
            raise common.HandoffError("artifact-promotion-is-not-a-fast-forward")
        return current, target

    def promote_artifact(self, branch, target, before):
        current = self.ref(branch)
        if current == target: return False
        if current != before: raise common.HandoffError("deployment-branch-changed-before-artifact-push")
        # The generated target includes the reviewed previous commit as a parent.
        # The lease is solely a compare-and-swap guard, never history rewriting.
        lease = "--force-with-lease=refs/heads/" + branch + ":" + (before or "")
        self.call(["push", lease, "--", self.url, target + ":refs/heads/" + branch], optional=True)
        if self.ref(branch) != target: raise common.HandoffError("artifact-branch-push-not-verified")
        return True


def cd_object(client, plan, contract):
    obj, _ = common.cd_objects(client, plan, contract)
    source = obj["spec"].get("source", {}); destination = obj["spec"].get("destination", {})
    if set(source) != {"repoURL", "targetRevision", "path"} or destination != {"server": plan["artifact_policy"]["destination_server"], "namespace": plan["artifact_policy"]["namespace"]}:
        raise common.HandoffError("artifact-promotion-requires-plain-directory-and-same-cluster-destination")
    return obj


def artifact_cd_object(client, plan, contract):
    if plan["cd"]["kind"] == "argocd":
        return cd_object(client, plan, contract)
    obj, repo = common.cd_objects(client, plan, contract)
    spec = obj["spec"]
    transformations = ("kubeConfig", "images", "patches", "postBuild", "components", "commonMetadata",
                       "namePrefix", "nameSuffix", "decryption")
    namespace = plan["artifact_policy"]["namespace"]
    if (any(spec.get(option) for option in transformations) or spec.get("targetNamespace") not in {None, "", namespace}
            or spec.get("deletionPolicy", "MirrorPrune") not in {"MirrorPrune", "Orphan"}
            or not isinstance(spec.get("serviceAccountName"), str)
            or repo["spec"].get("recurseSubmodules") or repo["spec"].get("ignore")):
        raise common.HandoffError("flux-artifact-promotion-requires-plain-directory-and-same-cluster-destination")
    return obj


def build_identity(data):
    if "zuul_build_uuid" in data:
        return {"zuul_build_uuid": data["zuul_build_uuid"]}
    if "gitlab_job_id" in data:
        return {"gitlab_pipeline_id": data["gitlab_pipeline_id"], "gitlab_job_id": data["gitlab_job_id"]}
    if "jenkins_build" in data:
        return {"jenkins_build": data["jenkins_build"], "jenkins_queue_id": data["jenkins_queue_id"]}
    if "woodpecker_step_id" in data:
        return {key: data[key] for key in ("woodpecker_pipeline_id", "woodpecker_workflow_id", "woodpecker_step_id")}
    if "task_uid" in data:
        return {"pipeline_run_uid": data["uid"], "task_run_uid": data["task_uid"]}
    return {"pipeline_run_uid": data["uid"]}


def verify_destination(client, plan):
    verify_target(client, plan["cd"]["management_target"])
    config = loads_strict_json(client.call(["config", "view", "--minify", "-o", "json"]))
    if config["clusters"][0]["cluster"]["server"] != plan["artifact_policy"]["destination_server"]:
        raise common.HandoffError("artifact-destination-is-not-the-verified-cd-endpoint")


def deployment_ready(client, plan, image):
    policy = plan["artifact_policy"]
    obj = client.get("deployment.apps/" + policy["deployment"], policy["namespace"])
    if not obj or obj.get("metadata", {}).get("uid") != policy["deployment_uid"] or obj["metadata"].get("deletionTimestamp"):
        raise common.HandoffError("original-artifact-deployment-changed")
    if manifest_image(obj, policy)["image"] != image: return False
    generation = obj["metadata"].get("generation"); desired = obj["spec"].get("replicas", 1); status = obj.get("status", {})
    return (type(generation) is int and generation > 0 and type(desired) is int and desired > 0
        and type(status.get("observedGeneration")) is int and status["observedGeneration"] >= generation
        and all(type(status.get(k)) is int and status[k] == desired for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
        and status.get("unavailableReplicas", 0) == 0
        and not any(c.get("type") == "ReplicaFailure" and c.get("status") == "True" for c in status.get("conditions", [])))


def execute(plan, report, contract, template, ci, cd, api, run_name, *, apply=False, record=None):
    validate(plan, report, contract)
    return execute_verified(plan, contract, cd,
        lambda: legacy.verified_ci(ci, api, report, contract, template, plan, run_name),
        apply=apply, record=record, signature_verifier=lambda data, evidence: verify_signature(plan, contract, data, evidence))


def execute_verified(plan, contract, cd, verify, *, apply=False, record=None, git_inputs=None,
                     inspect_cd=None, signature_verifier=None):
    """Promote using a provider-specific verifier and the reviewed controller scope."""
    record = record or (lambda **updates: None)
    inspect_cd = cd_object if inspect_cd is None else inspect_cd
    signature_verifier = signature_verifier or (lambda *_: None)
    data, evidence = verify()
    if "artifact" not in evidence: raise common.HandoffError("original-ci-artifact-evidence-is-missing")
    image = evidence["artifact"]["image"]
    record(phase="ci-verified", source_revision=data["revision"], **build_identity(data), ci_evidence_sha256=common.sha256(evidence), artifact=evidence["artifact"])
    signature = signature_verifier(data, evidence)
    signature_result = {"merged_ci_signature_verified": signature is not None}
    if signature is not None: record(phase="signature-verified", signature_proof=signature, merged_ci_signature_verified=True)
    verify_destination(cd, plan)
    app = inspect_cd(cd, plan, contract)
    # Pin the original live Deployment before preparing or pushing any branch.
    deployment_ready(cd, plan, image)
    with tempfile.TemporaryDirectory(prefix="gerrit-artifact-promotion-") as temporary:
        git = ArtifactGit(contract["plan"]["source"]["clone_url"], plan["git_username"], INPUTS if git_inputs is None else git_inputs, Path(temporary))
        if git.ref(contract["plan"]["source"]["branch"]) != data["revision"]: raise common.HandoffError("git-source-ref-no-longer-matches-tested-head")
        before, target = git.prepare_artifact(plan, data, evidence)
        def check_operation(app):
            if plan["cd"]["kind"] == "argocd" and before != target and (app.get("operation")
                    or app.get("status", {}).get("operationState", {}).get("phase") in {"Running", "Terminating"}):
                raise common.HandoffError("argocd-operation-is-already-active")
        check_operation(app)
        record(phase="git-preflight-verified", previous_deployment_revision=before, prepared_revision=target)
        if not apply:
            return {"read_only": True, "preflight_verified": True, "prepared_revision": target, "artifact_image": image,
                "branch_push_attempted": False, "controller_verified": False, "full_stack_verified": False, **signature_result}
        refreshed, proof = verify()
        verify_destination(cd, plan)
        check_operation(inspect_cd(cd, plan, contract))
        deployment_ready(cd, plan, image)
        if (data != refreshed or proof != evidence or git.ref(contract["plan"]["source"]["branch"]) != data["revision"]):
            raise common.HandoffError("ci-or-source-evidence-changed-before-push")
        refreshed_signature = signature_verifier(refreshed, proof)
        if signature is not None and refreshed_signature["signed_annotations_sha256"] != signature["signed_annotations_sha256"]:
            raise common.HandoffError("signed-ci-evidence-changed-before-push")
        if refreshed_signature is not None: record(phase="signature-rechecked", signature_proof=refreshed_signature, merged_ci_signature_verified=True)
        started = datetime.now(timezone.utc)
        record(phase="pre-push-verified")
        pushed = git.promote_artifact(plan["deployment_branch"], target, plan["artifact_policy"]["expected_deployment_revision"])
        record(phase="git-promoted", promoted_revision=target, branch_push_attempted=pushed)
        deadline = time.monotonic() + plan["timeout_seconds"]
        while True:
            if git.ref(plan["deployment_branch"]) != target: raise common.HandoffError("deployment-branch-was-superseded")
            def observed():
                inspect_cd(cd, plan, contract)
                return common.cd_observed(cd, plan, contract, target, since=started if before != target else None) and deployment_ready(cd, plan, image)
            ready = common.read_observation(observed, record)
            expired = time.monotonic() >= deadline
            if ready and not expired:
                record(phase="controller-verified", controller_verified=True, deployment_rollout_verified=True)
                return {"read_only": False, "preflight_verified": True, "branch_push_attempted": pushed,
                    "promoted_revision": target, "source_revision": data["revision"], "artifact_image": image,
                    "controller_verified": True, "deployment_rollout_verified": True, "signed_image_admission_verified": False, "full_stack_verified": False, **signature_result}
            if expired: raise common.HandoffError("artifact-controller-or-rollout-not-verified-before-timeout")
            time.sleep(5)


def load_bundle(root, digest):
    raw = read_bounded_bytes(root / CONTRACT, max_bytes=128 * 1024)
    if not isinstance(digest, str) or not common.DIGEST.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest:
        raise common.HandoffError("artifact-promotion-package-changed")
    selected = loads_strict_json(raw)
    if not isinstance(selected, dict) or set(selected) != {"schema_version", "plan", "files_sha256"} or type(selected["schema_version"]) is not int or selected["schema_version"] != 1:
        raise common.HandoffError("invalid-artifact-promotion-package")
    report, contract, template = reporter.load_bundle(root, selected["plan"]["reporter_contract_sha256"])
    expected = {*bridge.programs(contract["plan"]), *reporter.EXTRA_PROGRAMS, *programs(selected["plan"]),
        "run-template.json", "gerrit-event-contract.json", "gerrit-report-contract.json"}
    if set(selected["files_sha256"]) != expected: raise common.HandoffError("incomplete-artifact-promotion-package")
    for name, sha in selected["files_sha256"].items():
        if hashlib.sha256(read_bounded_bytes(root / name, max_bytes=bridge.LIMIT)).hexdigest() != sha:
            raise common.HandoffError("artifact-promotion-helper-changed")
    validate(selected["plan"], report, contract)
    return selected, report, contract, template


def operate(root, digest, run_name, *, apply=False, check_server=False, output=None):
    selected, report, contract, template = load_bundle(root, digest)
    bridge.common.verify_running_sources({"files_sha256": selected["files_sha256"]}, Path(__file__))
    plan = selected["plan"]
    if not isinstance(run_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", run_name): raise common.HandoffError("invalid-pipeline-run-name")
    if not apply and not check_server: return {"local_inputs_verified": True, "read_only": True, "controller_verified": False, "full_stack_verified": False}
    if os.name != "posix" or output is None: raise common.HandoffError("server-mode-requires-linux-and-private-proof-directory")
    record, state = common.new_journal(output, {"schema_version": plan["schema_version"], "plan_sha256": common.sha256(plan), "handoff_contract_sha256": digest,
        "runtime": plan["runtime"], "controller_verified": False, "full_stack_verified": False})
    try:
        api = legacy.ReadAPI(contract["plan"], plan)
        bridge.common.private_input("token", minimum=16, maximum=512, root=INPUTS)
        with bridge.common.frozen_target(contract["plan"], inputs=INPUTS, kubeconfig_name="ci-kubeconfig"):
            ci = Client(contract["plan"]["management_target"])
            with bridge.common.frozen_target({"management_target": plan["cd"]["management_target"]}, inputs=INPUTS, kubeconfig_name="cd-kubeconfig"):
                cd = Client(plan["cd"]["management_target"]); verify_target(cd, plan["cd"]["management_target"])
                return execute(plan, report, contract, template, ci, cd, api, run_name, apply=apply, record=record)
    except Exception as error:
        record(phase="failed", failed_at=state["phase"], error_code=str(error) if isinstance(error, common.HandoffError) else "artifact-promotion-validation-or-transport-failed", failure_diagnostic=legacy.failure_diagnostic(error))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--pipeline-run", required=True); parser.add_argument("--output-dir", type=Path)
    mode = parser.add_mutually_exclusive_group(); mode.add_argument("--apply", action="store_true"); mode.add_argument("--check-server", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.bundle_dir, args.contract_sha256, args.pipeline_run, apply=args.apply, check_server=args.check_server, output=args.output_dir), sort_keys=True)); return 0
    except Exception as error:
        code = str(error) if isinstance(error, common.HandoffError) else "artifact-promotion-validation-or-transport-failed"
        print("Gerrit artifact promotion stopped: " + code + ". Retain the private proof.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
