#!/usr/bin/env python3
"""Validate private evidence from an opt-in Forgejo singleton recovery drill."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bounded_file import read_bounded_text
from strict_json import loads_strict_json


COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
LONGHORN_DRIVER = "driver.longhorn.io"
LONGHORN_SECRET_REFS = (
    "nodePublishSecretRef",
    "nodeStageSecretRef",
    "nodeExpandSecretRef",
)


class EvidenceError(ValueError):
    """Raised when Forgejo recovery evidence is incomplete or stale."""


def nonempty(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value.strip()


def positive_number(document: dict[str, Any], name: str) -> float:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise EvidenceError(f"{name} must be a positive number")
    return float(value)


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError("completedAt must be a non-empty RFC3339 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceError("completedAt is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("completedAt must include a timezone")
    return parsed.astimezone(timezone.utc)


def recovery_state(document: dict[str, Any], name: str) -> dict[str, Any]:
    state = document.get(name)
    if not isinstance(state, dict):
        raise EvidenceError(f"{name} must be an object")
    for key in (
        "deploymentUid",
        "argocdApplicationUid",
        "argocdRevision",
        "podName",
        "podUid",
        "podIP",
        "node",
        "serviceUid",
        "serviceClusterIP",
    ):
        nonempty(state, key)
    if state.get("httpCode") != 200:
        raise EvidenceError(f"{name}.httpCode must be 200")
    if not isinstance(state.get("deploymentGeneration"), int) or state["deploymentGeneration"] <= 0:
        raise EvidenceError(f"{name}.deploymentGeneration must be a positive integer")
    if not isinstance(state.get("servicePort"), int) or not 1 <= state["servicePort"] <= 65535:
        raise EvidenceError(f"{name}.servicePort is invalid")
    images = state.get("imageIDs")
    if not isinstance(images, list) or not images or any(not str(value).strip() for value in images):
        raise EvidenceError(f"{name}.imageIDs must contain immutable runtime image IDs")
    endpoints = state.get("endpointAddresses")
    if not isinstance(endpoints, list) or nonempty(state, "podIP") not in endpoints:
        raise EvidenceError(f"{name}.endpointAddresses must contain the Ready pod IP")
    claims = state.get("storageClaims")
    if not isinstance(claims, list) or not claims:
        raise EvidenceError(f"{name}.storageClaims must contain at least one persistent claim")
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise EvidenceError(f"{name}.storageClaims[{index}] must be an object")
        for key in ("name", "uid", "volumeName", "storageClass"):
            nonempty(claim, key)
        storage_class = str(claim.get("storageClass", ""))
        if not storage_class.startswith("longhorn") or not storage_class.endswith("-encrypted"):
            raise EvidenceError(
                f"{name}.storageClaims[{index}] must use an encrypted Longhorn storage class"
            )
        if claim.get("csiDriver") != LONGHORN_DRIVER:
            raise EvidenceError(
                f"{name}.storageClaims[{index}] is not backed by the Longhorn CSI driver"
            )
        if claim.get("encrypted") is not True:
            raise EvidenceError(
                f"{name}.storageClaims[{index}] encryption proof is missing"
            )
        encryption_secret = nonempty(claim, "encryptionSecretName")
        refs = claim.get("encryptionSecretRefs")
        if not isinstance(refs, dict):
            raise EvidenceError(
                f"{name}.storageClaims[{index}].encryptionSecretRefs must be an object"
            )
        expected_ref = f"longhorn-system/{encryption_secret}"
        for ref_name in LONGHORN_SECRET_REFS:
            if refs.get(ref_name) != expected_ref:
                raise EvidenceError(
                    f"{name}.storageClaims[{index}].{ref_name} must be {expected_ref}"
                )
        if claim.get("longhornState") != "attached":
            raise EvidenceError(f"{name}.storageClaims[{index}] Longhorn state is not attached")
        if claim.get("longhornRobustness") != "healthy":
            raise EvidenceError(f"{name}.storageClaims[{index}] Longhorn volume is not healthy")
    return state


def claim_identity(state: dict[str, Any]) -> list[tuple[str, ...]]:
    return sorted(
        (
            str(claim["name"]),
            str(claim["uid"]),
            str(claim["volumeName"]),
            str(claim["storageClass"]),
            str(claim["csiDriver"]),
            str(claim["encryptionSecretName"]),
            json.dumps(claim["encryptionSecretRefs"], sort_keys=True),
        )
        for claim in state["storageClaims"]
    )


def validate_evidence(
    document: Any,
    *,
    now: datetime,
    max_age_days: int,
    expected_profile: str = "",
    expected_commit: str = "",
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise EvidenceError("Forgejo recovery evidence must be a JSON object")
    if document.get("schemaVersion") != 2:
        raise EvidenceError("schemaVersion must be 2")
    drill_id = nonempty(document, "drillId")
    operator = nonempty(document, "operator")
    approver = nonempty(document, "approver")
    if operator.casefold() == approver.casefold():
        raise EvidenceError("operator and approver must be different people")
    profile = nonempty(document, "profile")
    if expected_profile and profile != expected_profile:
        raise EvidenceError(
            f"profile {profile!r} does not match expected profile {expected_profile!r}"
        )
    commit = nonempty(document, "sourceCommit").lower()
    if not COMMIT_RE.fullmatch(commit):
        raise EvidenceError("sourceCommit must be a 40-character lowercase Git SHA")
    if expected_commit and commit != expected_commit.lower():
        raise EvidenceError("sourceCommit does not match the expected Git revision")
    if nonempty(document, "result").lower() != "passed":
        raise EvidenceError("result must be passed")
    if nonempty(document, "namespace") != "forgejo":
        raise EvidenceError("namespace must be forgejo")
    if nonempty(document, "deployment") != "forgejo":
        raise EvidenceError("deployment must be forgejo")
    if nonempty(document, "service") != "forgejo-http":
        raise EvidenceError("service must be forgejo-http")
    if nonempty(document, "recoveryMode") != "node-failover":
        raise EvidenceError("recoveryMode must be node-failover")
    source_node = nonempty(document, "sourceNode")
    target_node = nonempty(document, "targetNode")
    if source_node == target_node:
        raise EvidenceError("sourceNode and targetNode must differ")
    eligible_nodes = document.get("eligibleRecoveryNodes")
    if (
        not isinstance(eligible_nodes, list)
        or not eligible_nodes
        or any(not isinstance(node, str) or not node.strip() for node in eligible_nodes)
    ):
        raise EvidenceError("eligibleRecoveryNodes must contain Ready node names")
    normalized_eligible = [node.strip() for node in eligible_nodes]
    if len(normalized_eligible) != len(set(normalized_eligible)):
        raise EvidenceError("eligibleRecoveryNodes must not contain duplicates")
    if source_node in normalized_eligible:
        raise EvidenceError("eligibleRecoveryNodes must exclude the source node")
    if target_node not in normalized_eligible:
        raise EvidenceError("targetNode was not an eligible recovery node")
    if document.get("sourceNodeWasSchedulable") is not True:
        raise EvidenceError("sourceNodeWasSchedulable must be true")
    if document.get("sourceNodeRestoredSchedulable") is not True:
        raise EvidenceError("sourceNodeRestoredSchedulable must be true")

    completed_at = parse_timestamp(document.get("completedAt"))
    now_utc = now.astimezone(timezone.utc)
    if completed_at > now_utc + timedelta(minutes=5):
        raise EvidenceError("completedAt is in the future")
    age = now_utc - completed_at
    if age > timedelta(days=max_age_days):
        raise EvidenceError(
            f"Forgejo recovery drill is stale ({age.days} days old; maximum is {max_age_days})"
        )
    rto_seconds = positive_number(document, "rtoSeconds")
    elapsed_seconds = positive_number(document, "elapsedSeconds")
    if elapsed_seconds > rto_seconds:
        raise EvidenceError(
            f"elapsedSeconds {elapsed_seconds:g} exceeds rtoSeconds {rto_seconds:g}"
        )

    before = recovery_state(document, "preRecovery")
    after = recovery_state(document, "postRecovery")
    if before["node"] != source_node:
        raise EvidenceError("preRecovery.node does not match sourceNode")
    if after["node"] != target_node:
        raise EvidenceError("postRecovery.node does not match targetNode")
    if before["node"] == after["node"]:
        raise EvidenceError("Forgejo must recover on a different node")
    if before["argocdRevision"] != commit or after["argocdRevision"] != commit:
        raise EvidenceError("Argo CD Forgejo revision does not match sourceCommit")
    if before["podUid"] == after["podUid"]:
        raise EvidenceError("preRecovery and postRecovery pod UIDs must differ")
    for key in (
        "deploymentUid",
        "deploymentGeneration",
        "argocdApplicationUid",
        "argocdRevision",
        "serviceUid",
        "serviceClusterIP",
        "servicePort",
        "imageIDs",
    ):
        if before[key] != after[key]:
            raise EvidenceError(f"{key} changed during the Forgejo recovery drill")
    if claim_identity(before) != claim_identity(after):
        raise EvidenceError("Forgejo PVC or PV identity changed during the recovery drill")

    return {
        "drill_id": drill_id,
        "profile": profile,
        "commit": commit,
        "completed_at": completed_at,
        "age_days": age.total_seconds() / 86400,
        "rto_seconds": rto_seconds,
        "elapsed_seconds": elapsed_seconds,
        "source_node": source_node,
        "target_node": target_node,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_FORGEJO_RECOVERY_MAX_AGE_DAYS", "92")),
    )
    parser.add_argument("--expected-profile", default=os.environ.get("PLATFORM_PROFILE", ""))
    parser.add_argument(
        "--expected-commit",
        default=os.environ.get("PLATFORM_EXPECTED_COMMIT", ""),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_days <= 0:
        print("--max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if not args.evidence_file.is_file():
        print(f"Forgejo recovery evidence does not exist: {args.evidence_file}", file=sys.stderr)
        return 1
    try:
        document = loads_strict_json(read_bounded_text(args.evidence_file))
        summary = validate_evidence(
            document,
            now=datetime.now(timezone.utc),
            max_age_days=args.max_age_days,
            expected_profile=args.expected_profile,
            expected_commit=args.expected_commit,
        )
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"Forgejo recovery evidence validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Forgejo recovery evidence accepted: "
        f"drill={summary['drill_id']} profile={summary['profile']} "
        f"commit={summary['commit']} elapsed={summary['elapsed_seconds']:g}s/"
        f"{summary['rto_seconds']:g}s node={summary['source_node']}->"
        f"{summary['target_node']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
