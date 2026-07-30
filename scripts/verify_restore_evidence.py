#!/usr/bin/env python3
"""Validate private restore and continuity evidence used by the production gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bounded_file import read_bounded_text


REQUIRED_CHECKS = (
    "etcd",
    "velero",
    "cloudnativepg",
    "longhorn",
    "longhornEncryptionKey",
    "objectStorage",
    "forgejo",
    "harbor",
    "secrets",
    "argocd",
    "ingress",
    "certificates",
)
EVIDENCE_SCHEMES = {"evidence", "https", "s3", "ticket"}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")


class EvidenceError(ValueError):
    """Raised when restore or continuity evidence is incomplete or stale."""


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be a non-empty RFC3339 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceError(f"{label} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def positive_number(document: dict[str, Any], name: str) -> float:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise EvidenceError(f"{name} must be a positive number")
    return float(value)


def nonempty_string(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value.strip()


def require_true(document: dict[str, Any], name: str, label: str) -> None:
    if document.get(name) is not True:
        raise EvidenceError(f"{label}.{name} must be true")


def validate_proof(
    value: Any,
    *,
    label: str,
    recovery_started_at: datetime,
    completed_at: datetime,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    if nonempty_string(value, "status").lower() != "passed":
        raise EvidenceError(f"{label}.status must be passed")
    proof = value.get("evidence")
    if not isinstance(proof, dict):
        raise EvidenceError(f"{label}.evidence must be an object")
    uri = nonempty_string(proof, "uri")
    parsed = urlparse(uri)
    if parsed.scheme.lower() not in EVIDENCE_SCHEMES or not (parsed.netloc or parsed.path):
        allowed = ", ".join(sorted(EVIDENCE_SCHEMES))
        raise EvidenceError(f"{label}.evidence.uri must use an approved scheme: {allowed}")
    digest = nonempty_string(proof, "sha256").lower()
    if not SHA256_RE.fullmatch(digest):
        raise EvidenceError(f"{label}.evidence.sha256 must be a lowercase SHA-256")
    verified_at = parse_timestamp(proof.get("verifiedAt"), f"{label}.evidence.verifiedAt")
    if verified_at < recovery_started_at - timedelta(minutes=5):
        raise EvidenceError(f"{label}.evidence.verifiedAt predates the recovery exercise")
    if verified_at > completed_at + timedelta(minutes=5):
        raise EvidenceError(f"{label}.evidence.verifiedAt is after drill completion")
    return {"uri": uri, "sha256": digest, "verified_at": verified_at}


def validate_evidence(
    document: Any,
    *,
    now: datetime,
    max_age_days: int,
    expected_profile: str = "",
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise EvidenceError("restore evidence must be a JSON object")
    if document.get("schemaVersion") != 2:
        raise EvidenceError("schemaVersion must be 2")

    drill_id = nonempty_string(document, "drillId")
    operator = nonempty_string(document, "operator")
    approver = nonempty_string(document, "approver")
    profile = nonempty_string(document, "profile")
    source_commit = nonempty_string(document, "sourceCommit").lower()
    if not COMMIT_RE.fullmatch(source_commit):
        raise EvidenceError("sourceCommit must be a 40-character lowercase Git SHA")
    if nonempty_string(document, "result").lower() != "passed":
        raise EvidenceError("result must be passed")
    if operator.casefold() == approver.casefold():
        raise EvidenceError("operator and approver must be different people")
    if expected_profile and profile != expected_profile:
        raise EvidenceError(
            f"profile {profile!r} does not match expected profile {expected_profile!r}"
        )

    backup_completed_at = parse_timestamp(document.get("backupCompletedAt"), "backupCompletedAt")
    recovery_started_at = parse_timestamp(
        document.get("recoveryStartedAt"), "recoveryStartedAt"
    )
    completed_at = parse_timestamp(document.get("completedAt"), "completedAt")
    now_utc = now.astimezone(timezone.utc)
    if completed_at > now_utc + timedelta(minutes=5):
        raise EvidenceError("completedAt is in the future")
    if recovery_started_at > completed_at:
        raise EvidenceError("recoveryStartedAt must not be after completedAt")
    if backup_completed_at > recovery_started_at + timedelta(minutes=5):
        raise EvidenceError("backupCompletedAt must not be after recoveryStartedAt")
    age = now_utc - completed_at
    if age > timedelta(days=max_age_days):
        raise EvidenceError(
            f"restore drill is stale ({age.days} days old; maximum is {max_age_days})"
        )

    rpo_hours = positive_number(document, "rpoHours")
    rto_minutes = positive_number(document, "rtoMinutes")
    elapsed_minutes = positive_number(document, "elapsedMinutes")
    measured_rpo_hours = max(
        0.0,
        (recovery_started_at - backup_completed_at).total_seconds() / 3600,
    )
    measured_elapsed_minutes = (completed_at - recovery_started_at).total_seconds() / 60
    if measured_rpo_hours > rpo_hours:
        raise EvidenceError(
            f"measured backup age {measured_rpo_hours:g}h exceeds rpoHours {rpo_hours:g}"
        )
    if measured_elapsed_minutes > rto_minutes:
        raise EvidenceError(
            f"measured recovery time {measured_elapsed_minutes:g}m exceeds rtoMinutes {rto_minutes:g}"
        )
    if abs(measured_elapsed_minutes - elapsed_minutes) > 1:
        raise EvidenceError(
            "elapsedMinutes does not match recoveryStartedAt/completedAt within one minute"
        )

    target = document.get("recoveryTarget")
    if not isinstance(target, dict):
        raise EvidenceError("recoveryTarget must be an object")
    target_type = nonempty_string(target, "type")
    if target_type not in {"isolated-cluster", "disposable-lab"}:
        raise EvidenceError(
            "recoveryTarget.type must be isolated-cluster or disposable-lab"
        )
    nonempty_string(target, "identifier")
    if target.get("isProduction") is not False:
        raise EvidenceError("recoveryTarget.isProduction must be false")
    require_true(target, "failureDomainSeparated", "recoveryTarget")

    checks = document.get("checks")
    if not isinstance(checks, dict):
        raise EvidenceError("checks must be an object")
    unknown = sorted(set(checks) - set(REQUIRED_CHECKS))
    if unknown:
        raise EvidenceError(f"checks contains unsupported entries: {', '.join(unknown)}")
    for name in REQUIRED_CHECKS:
        validate_proof(
            checks.get(name),
            label=f"checks.{name}",
            recovery_started_at=recovery_started_at,
            completed_at=completed_at,
        )

    continuity = document.get("continuity")
    if not isinstance(continuity, dict):
        raise EvidenceError("continuity must be an object")
    unknown_continuity = sorted(set(continuity) - {"failover", "failback"})
    if unknown_continuity:
        raise EvidenceError(
            f"continuity contains unsupported entries: {', '.join(unknown_continuity)}"
        )
    failover = continuity.get("failover")
    validate_proof(
        failover,
        label="continuity.failover",
        recovery_started_at=recovery_started_at,
        completed_at=completed_at,
    )
    if not isinstance(failover, dict):
        raise EvidenceError("continuity.failover must be an object")
    require_true(failover, "dnsVipTlsValidated", "continuity.failover")
    require_true(failover, "dataConsistencyValidated", "continuity.failover")

    failback = continuity.get("failback")
    validate_proof(
        failback,
        label="continuity.failback",
        recovery_started_at=recovery_started_at,
        completed_at=completed_at,
    )
    if not isinstance(failback, dict):
        raise EvidenceError("continuity.failback must be an object")
    require_true(failback, "currentBackupValidated", "continuity.failback")
    require_true(failback, "dataReconciled", "continuity.failback")

    return {
        "drill_id": drill_id,
        "profile": profile,
        "source_commit": source_commit,
        "completed_at": completed_at,
        "age_days": age.total_seconds() / 86400,
        "rpo_hours": rpo_hours,
        "measured_rpo_hours": measured_rpo_hours,
        "rto_minutes": rto_minutes,
        "elapsed_minutes": measured_elapsed_minutes,
        "recovery_target": target_type,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate recent, independently approved restore and continuity evidence."
    )
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_RESTORE_DRILL_MAX_AGE_DAYS", "92")),
    )
    parser.add_argument(
        "--expected-profile",
        default=os.environ.get("PLATFORM_PROFILE", ""),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_days <= 0:
        print("--max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if not args.evidence_file.is_file():
        print(f"Restore evidence file does not exist: {args.evidence_file}", file=sys.stderr)
        return 1
    try:
        document = json.loads(read_bounded_text(args.evidence_file))
        summary = validate_evidence(
            document,
            now=datetime.now(timezone.utc),
            max_age_days=args.max_age_days,
            expected_profile=args.expected_profile,
        )
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"Restore evidence validation failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Restore and continuity evidence accepted: "
        f"drill={summary['drill_id']} profile={summary['profile']} "
        f"commit={summary['source_commit']} target={summary['recovery_target']} "
        f"elapsed={summary['elapsed_minutes']:g}m/{summary['rto_minutes']:g}m "
        f"backup_age={summary['measured_rpo_hours']:g}h/{summary['rpo_hours']:g}h"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
