#!/usr/bin/env python3
"""Render separate, read-only CI and CD identities for a reviewed Gerrit handoff."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from bounded_file import read_bounded_bytes, read_bounded_text
import gerrit_gitops_handoff as handoff
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json


ROOT = Path(__file__).resolve().parents[1]
RBAC = "rbac.authorization.k8s.io"
RESERVED = {"default", "kube-system", "kube-public", "kube-node-lease"}


def canonical(value):
    return handoff.bridge.common.canonical(value).decode() + "\n"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def identity(value, *, workers=False):
    fields = {"account", "namespace"} | ({"workers"} if workers else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Declare exact dedicated promotion reader identities")
    for key in ("account", "namespace"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value[key]) or value[key] in RESERVED:
            raise ValueError("Use non-system, dedicated reader names")
    if workers:
        names = value["workers"]
        if (not isinstance(names, list) or not 1 <= len(names) <= 256
                or any(not isinstance(name, str) or len(name) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in name.split(".")) for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("Inventory distinct, exact CI worker node names")
    return value


def objects(target, selected, rules, cluster_rules, purpose):
    # Include namespace and scope in cluster-wide names: namespaced accounts
    # with the same short name must not overwrite another reader's bindings.
    name = "gerrit-promotion-" + purpose + "-" + digest(canonical([target, selected, rules, cluster_rules]))[:20]
    labels = {"platform.gitops/access-purpose": purpose}
    annotations = {"platform.gitops/access-target-cluster-uid": target["cluster_uid"]}
    def obj(kind, *, cluster=False, **values):
        return {"apiVersion": "v1" if kind == "ServiceAccount" else RBAC + "/v1", "kind": kind,
            "metadata": {"name": selected["account"] if kind == "ServiceAccount" else name,
                **({} if cluster else {"namespace": selected["namespace"]}), "labels": labels, "annotations": annotations}, **values}
    subject = {"kind": "ServiceAccount", "name": selected["account"], "namespace": selected["namespace"]}
    return [obj("ServiceAccount", automountServiceAccountToken=False), obj("Role", rules=rules),
        obj("RoleBinding", roleRef={"apiGroup": RBAC, "kind": "Role", "name": name}, subjects=[subject]),
        obj("ClusterRole", cluster=True, rules=cluster_rules),
        obj("ClusterRoleBinding", cluster=True, roleRef={"apiGroup": RBAC, "kind": "ClusterRole", "name": name}, subjects=[subject])]


def ci_objects(original, selected, reporter_account):
    identity(selected, workers=True)
    if selected["namespace"] != original["submitter_namespace"] or selected["account"] in {original["service_account"], original["submitter_account"], reporter_account}:
        raise ValueError("Keep the CI promotion reader separate from workers, submitter and reporter")
    result = objects(original["management_target"], selected, [
        {"apiGroups": ["tekton.dev"], "resources": ["pipelineruns", "taskruns"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods", "configmaps"], "verbs": ["get"]}], [
        {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system", original["namespace"]], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["nodes"], "resourceNames": selected["workers"], "verbs": ["get"]}], "ci")
    # The account lives in the control namespace; evidence lives only in the
    # separately pinned, restricted job namespace. The binding crosses neither cluster.
    for obj in result:
        if obj["kind"] in {"Role", "RoleBinding"}: obj["metadata"]["namespace"] = original["namespace"]
    return result


def cd_objects(cd, selected):
    identity(selected)
    if selected["namespace"] != cd["namespace"]:
        raise ValueError("Keep the CD observer in the reviewed Application namespace")
    return objects(cd["management_target"], selected,
        [{"apiGroups": ["argoproj.io"], "resources": ["applications"], "resourceNames": [cd["name"]], "verbs": ["get"]}],
        [{"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}], "cd")


def load_handoff(bundle, digest):
    from gerrit_artifact_promotion import CONTRACT, load_bundle as artifact_bundle
    if (bundle / CONTRACT).exists():
        if (bundle / "gerrit-handoff-contract.json").exists():
            raise ValueError("Refuse ambiguous legacy and artifact promotion packages")
        return artifact_bundle(bundle, digest)
    return handoff.load_bundle(bundle, digest)


def artifact_workload_objects(cd, selected, policy):
    name = "gerrit-artifact-rollout-" + digest(canonical([cd["management_target"], selected, policy]))[:20]
    metadata = {"name": name, "namespace": policy["namespace"]}
    return [{"apiVersion": RBAC + "/v1", "kind": "Role", "metadata": metadata,
        "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": [policy["deployment"]], "verbs": ["get"]}]},
        {"apiVersion": RBAC + "/v1", "kind": "RoleBinding", "metadata": metadata,
        "roleRef": {"apiGroup": RBAC, "kind": "Role", "name": name},
        "subjects": [{"kind": "ServiceAccount", "name": selected["account"], "namespace": selected["namespace"]}]}]


def render(bundle, handoff_digest, plan):
    chosen, report, contract, _ = load_handoff(bundle, handoff_digest)
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "ci", "cd"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("Declare the exact optional promotion access policy")
    original, cd = contract["plan"], chosen["plan"]["cd"]
    ci = ci_objects(original, plan["ci"], report["plan"]["account"])
    observer = cd_objects(cd, plan["cd"])
    if chosen["plan"]["schema_version"] in (2, 3):
        observer.extend(artifact_workload_objects(cd, plan["cd"], chosen["plan"]["artifact_policy"]))
    if original["management_target"]["cluster_uid"] == cd["management_target"]["cluster_uid"]:
        if plan["ci"]["namespace"] == plan["cd"]["namespace"] and plan["ci"]["account"] == plan["cd"]["account"]:
            raise ValueError("CI and CD must use separate service-account identities")
        if (plan["cd"]["namespace"] == original["submitter_namespace"] and plan["cd"]["account"] in {original["submitter_account"], report["plan"]["account"]}
                or plan["cd"]["namespace"] == original["namespace"] and plan["cd"]["account"] == original["service_account"]):
            raise ValueError("CD observation cannot reuse a CI writer or job identity")
    files = {"ci-reader-rbac.json": canonical({"apiVersion": "v1", "kind": "List", "items": ci}),
        "cd-reader-rbac.json": canonical({"apiVersion": "v1", "kind": "List", "items": observer})}
    selected = {"schema_version": 1, "handoff_contract_sha256": handoff_digest, "plan": plan,
        "targets": {"ci": original["management_target"], "cd": cd["management_target"]},
        "files_sha256": {name: digest(value) for name, value in files.items()}}
    files["promotion-access-contract.json"] = canonical(selected)
    files["access-status.json"] = canonical({"prepared": True, "contract_sha256": digest(files["promotion-access-contract.json"]),
        "runtime": contract["runtime"], "separate_target_manifests": True, "applied": False, "credentials_created": False,
        "authorizations_verified": False, "controller_verified": False, "full_stack_verified": False})
    return files


def load_bundle(root, expected_digest, handoff_bundle, handoff_digest):
    raw = read_bounded_bytes(
        root / "promotion-access-contract.json",
        max_bytes=128 * 1024,
    )
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("Promotion access contract changed")
    selected = loads_strict_json(raw)
    if not isinstance(selected, dict) or selected.get("handoff_contract_sha256") != handoff_digest:
        raise ValueError("Promotion access belongs to another handoff")
    files = render(handoff_bundle, handoff_digest, selected.get("plan"))
    # Exact regeneration also rejects unknown fields and a rehashed broad role.
    for name, expected in files.items():
        if read_bounded_text(root / name, max_bytes=1024 * 1024, encoding="utf-8") != expected:
            raise ValueError("Promotion access package no longer regenerates exactly")
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, max_bytes=128 * 1024, encoding="utf-8"))
        files = render(args.bundle_dir, args.contract_sha256, plan)
        write_bundle(files, args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"prepared": True, "contract_sha256": digest(files["promotion-access-contract.json"]), "applied": False, "full_stack_verified": False})); return 0
    except Exception:
        print("Promotion access preparation stopped; private identities and targets suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
