#!/usr/bin/env python3
"""Validate the operator-reviewed GitLab CE repository recovery record."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sys
from typing import Any

from bounded_file import read_bounded_bytes
from strict_json import loads_strict_json
from verify_restore_evidence import (
    COMMIT_RE,
    EvidenceError,
    GITLAB_PROFILE,
    nonempty_string,
    parse_timestamp,
    positive_number,
    require_true,
    validate_proof,
)


def mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def commit_sha(document: dict[str, Any], name: str) -> str:
    value = nonempty_string(document, name)
    if not COMMIT_RE.fullmatch(value):
        raise EvidenceError(f"{name} must be a lowercase Git commit SHA")
    return value


def integer(document: dict[str, Any], name: str, minimum: int) -> int:
    value = document.get(name)
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"{name} must be an integer of at least {minimum}")
    return value


def observed_replicas(
    phase: dict[str, Any], label: str, expected_count: int, expected_ref: str
) -> tuple[dict[str, tuple[str, str]], str, int]:
    generation = integer(phase, "generation", 0)
    replicas = mapping(phase.get("replicas"), f"{label}.replicas")
    if len(replicas) != expected_count:
        raise EvidenceError(f"{label} must have {expected_count} healthy replicas")
    identities: dict[str, tuple[str, str]] = {}
    for storage, value in replicas.items():
        sample = mapping(value, f"{label}.replicas.{storage}")
        node = nonempty_string(sample, "node")
        pod_uid = nonempty_string(sample, "podUid")
        if not storage.strip() or sample.get("ref") != expected_ref:
            raise EvidenceError(f"{label} includes a replica without the expected Git ref")
        if integer(sample, "generation", 0) != generation:
            raise EvidenceError(f"{label} includes a stale replica generation")
        for field in ("assigned", "healthy", "validPrimary"):
            require_true(sample, field, f"{label}.replicas.{storage}")
        identities[storage] = node, pod_uid
    nodes = {node for node, _ in identities.values()}
    pods = {uid for _, uid in identities.values()}
    if len(nodes) != expected_count or len(pods) != expected_count:
        raise EvidenceError(f"{label} requires distinct node and pod identities")
    primary = nonempty_string(phase, "primaryStorage")
    if primary not in identities:
        raise EvidenceError(f"{label}.primaryStorage must be a healthy observed replica")
    if any(phase.get(field) != expected_ref for field in ("httpsRef", "sshRef")):
        raise EvidenceError(f"{label} must return the expected ref over HTTPS and SSH")
    return identities, primary, generation


def validate_evidence(
    document: Any,
    *,
    now: datetime,
    max_age_days: int,
    expected_commit: str = "",
    expected_cluster_uid: str = "",
) -> dict[str, Any]:
    document = mapping(document, "GitLab recovery evidence")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1:
        raise EvidenceError("GitLab recovery evidence schemaVersion must be 1")
    if document.get("profile") != GITLAB_PROFILE or document.get("result") != "passed":
        raise EvidenceError(f"GitLab recovery evidence must pass for profile {GITLAB_PROFILE}")
    drill_id = nonempty_string(document, "drillId")
    operator = nonempty_string(document, "operator")
    approver = nonempty_string(document, "approver")
    if operator.casefold() == approver.casefold():
        raise EvidenceError("GitLab recovery operator and approver must differ")
    source = commit_sha(document, "sourceCommit")
    if expected_commit and source != expected_commit:
        raise EvidenceError("GitLab recovery sourceCommit does not match the release")
    cluster = nonempty_string(document, "clusterUid")
    if expected_cluster_uid and cluster != expected_cluster_uid:
        raise EvidenceError("GitLab recovery evidence is from another Kubernetes cluster")

    before = mapping(document.get("before"), "before")
    during = mapping(document.get("during"), "during")
    after = mapping(document.get("after"), "after")
    baseline_at = parse_timestamp(before.get("observedAt"), "before.observedAt")
    failure_at = parse_timestamp(document.get("outageStartedAt"), "outageStartedAt")
    service_at = parse_timestamp(during.get("observedAt"), "during.observedAt")
    recovered_at = parse_timestamp(after.get("observedAt"), "after.observedAt")
    completed_at = parse_timestamp(document.get("completedAt"), "completedAt")
    if not baseline_at < failure_at < service_at < recovered_at <= completed_at:
        raise EvidenceError("GitLab recovery observations are out of order")
    age = now.astimezone(timezone.utc) - completed_at
    if max_age_days <= 0 or age > timedelta(days=max_age_days) or age < -timedelta(minutes=5):
        raise EvidenceError("GitLab recovery evidence is stale or dated in the future")
    rto = positive_number(document, "rtoSeconds")
    elapsed = (service_at - failure_at).total_seconds()
    if elapsed > rto:
        raise EvidenceError("observed GitLab repository recovery exceeded rtoSeconds")

    baseline = commit_sha(before, "httpsRef")
    ssh_write = commit_sha(during, "sshPushedCommit")
    https_write = commit_sha(during, "httpsPushedCommit")
    if len({baseline, ssh_write, https_write}) != 3:
        raise EvidenceError("GitLab recovery must prove new writes over SSH and HTTPS")
    if any(during.get(field) != baseline for field in ("httpsRefBeforeWrite", "sshRefBeforeWrite")):
        raise EvidenceError("GitLab must preserve the acknowledged ref during primary loss")
    initial, old_primary, initial_gen = observed_replicas(before, "before", 3, baseline)
    surviving, _, serving_gen = observed_replicas(during, "during", 2, https_write)
    restored, _, restored_gen = observed_replicas(after, "after", 3, https_write)
    expected_survivors = {name: identity for name, identity in initial.items() if name != old_primary}
    if surviving != expected_survivors:
        raise EvidenceError("the two original secondary replicas must serve while the primary is absent")
    if (
        restored.keys() != initial.keys()
        or any(restored[name] != identity for name, identity in surviving.items())
        or restored[old_primary][1] in {uid for _, uid in initial.values()}
        or not initial_gen < serving_gen <= restored_gen
    ):
        raise EvidenceError("the original primary storage must rejoin with a replacement pod and current data")

    target = mapping(document.get("target"), "target")
    project_id = integer(target, "projectId", 1)
    integer(target, "praefectRepositoryId", 1)
    ref = nonempty_string(target, "ref")
    if not ref.startswith("refs/heads/") or ref == "refs/heads/":
        raise EvidenceError("target.ref must identify the drill branch")
    for key in ("testProject", "httpsTlsVerified", "sshHostKeyVerified", "sourceNodeRestored"):
        require_true(target, key, "target")
    apps_before = mapping(before.get("argocdApplications"), "before.argocdApplications")
    apps_after = mapping(after.get("argocdApplications"), "after.argocdApplications")
    for application in ("gitlab-ce", "gitlab-runner"):
        prior = mapping(apps_before.get(application), f"before.{application}")
        final = mapping(apps_after.get(application), f"after.{application}")
        if (
            nonempty_string(prior, "uid") != nonempty_string(final, "uid")
            or any(app.get("revision") != source or app.get("sync") != "Synced"
                   or app.get("health") != "Healthy" for app in (prior, final))
        ):
            raise EvidenceError(f"{application} must remain bound to the healthy reviewed Argo CD revision")

    pipeline = mapping(document.get("runnerPipeline"), "runnerPipeline")
    if (integer(pipeline, "projectId", 1) != project_id or pipeline.get("commit") != https_write
            or pipeline.get("source") != "push" or pipeline.get("status") != "success"):
        raise EvidenceError("GitLab Runner must execute the recovered project's post-failure commit")
    for key in ("pipelineId", "runnerId", "jobId"):
        integer(pipeline, key, 1)
    artifacts = mapping(document.get("checks"), "checks")
    for key, earliest in (
        ("replicaChecksums", recovered_at),
        ("repositoryFailover", service_at),
        ("runnerArtifact", service_at),
    ):
        proof = validate_proof(
            artifacts.get(key), label=f"checks.{key}",
            recovery_started_at=baseline_at, completed_at=completed_at,
        )
        if not earliest <= proof["verified_at"] <= completed_at:
            raise EvidenceError(f"checks.{key} verification predates its observation or follows completion")

    return {
        "drill_id": drill_id, "cluster_uid": cluster,
        "completed_at": completed_at, "elapsed_seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument("--max-age-days", type=int, default=92)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    try:
        evidence = read_bounded_bytes(args.evidence_file)
        validate_evidence(
            loads_strict_json(evidence.decode("utf-8")),
            now=datetime.now(timezone.utc), max_age_days=args.max_age_days,
            expected_commit=args.expected_commit,
        )
    except (OSError, ValueError) as exc:
        print(f"GitLab recovery evidence failed: {exc}", file=sys.stderr)
        return 1
    print(f"GitLab recovery evidence accepted: sha256={hashlib.sha256(evidence).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
