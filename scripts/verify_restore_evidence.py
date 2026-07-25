#!/usr/bin/env python3
"""Validate private restore-drill evidence used by the production gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REQUIRED_CHECKS = (
    "etcd",
    "velero",
    "cloudnativepg",
    "longhorn",
    "forgejo",
    "harbor",
    "secrets",
    "argocd",
    "ingress",
    "certificates",
)


class EvidenceError(ValueError):
    """Raised when restore-drill evidence is incomplete or stale."""


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


def validate_evidence(
    document: Any,
    *,
    now: datetime,
    max_age_days: int,
    expected_profile: str = "",
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise EvidenceError("restore evidence must be a JSON object")
    if document.get("schemaVersion") != 1:
        raise EvidenceError("schemaVersion must be 1")

    drill_id = nonempty_string(document, "drillId")
    operator = nonempty_string(document, "operator")
    approver = nonempty_string(document, "approver")
    profile = nonempty_string(document, "profile")
    result = nonempty_string(document, "result").lower()
    if result != "passed":
        raise EvidenceError("result must be passed")
    if operator.casefold() == approver.casefold():
        raise EvidenceError("operator and approver must be different people")
    if expected_profile and profile != expected_profile:
        raise EvidenceError(
            f"profile {profile!r} does not match expected profile {expected_profile!r}"
        )

    completed_at = parse_timestamp(document.get("completedAt"), "completedAt")
    now_utc = now.astimezone(timezone.utc)
    if completed_at > now_utc + timedelta(minutes=5):
        raise EvidenceError("completedAt is in the future")
    age = now_utc - completed_at
    if age > timedelta(days=max_age_days):
        raise EvidenceError(
            f"restore drill is stale ({age.days} days old; maximum is {max_age_days})"
        )

    rpo_hours = positive_number(document, "rpoHours")
    rto_minutes = positive_number(document, "rtoMinutes")
    elapsed_minutes = positive_number(document, "elapsedMinutes")
    if elapsed_minutes > rto_minutes:
        raise EvidenceError(
            f"elapsedMinutes {elapsed_minutes:g} exceeds rtoMinutes {rto_minutes:g}"
        )

    checks = document.get("checks")
    if not isinstance(checks, dict):
        raise EvidenceError("checks must be an object")
    unknown = sorted(set(checks) - set(REQUIRED_CHECKS))
    if unknown:
        raise EvidenceError(f"checks contains unsupported entries: {', '.join(unknown)}")
    for name in REQUIRED_CHECKS:
        check = checks.get(name)
        if not isinstance(check, dict):
            raise EvidenceError(f"checks.{name} must be an object")
        status = str(check.get("status", "")).strip().lower()
        evidence = str(check.get("evidence", "")).strip()
        if status != "passed":
            raise EvidenceError(f"checks.{name}.status must be passed")
        if not evidence:
            raise EvidenceError(f"checks.{name}.evidence must identify retained proof")

    return {
        "drill_id": drill_id,
        "profile": profile,
        "completed_at": completed_at,
        "age_days": age.total_seconds() / 86400,
        "rpo_hours": rpo_hours,
        "rto_minutes": rto_minutes,
        "elapsed_minutes": elapsed_minutes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate recent, independently approved restore-drill evidence."
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
        document = json.loads(args.evidence_file.read_text(encoding="utf-8"))
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
        "Restore evidence accepted: "
        f"drill={summary['drill_id']} profile={summary['profile']} "
        f"completed={summary['completed_at'].isoformat()} "
        f"elapsed={summary['elapsed_minutes']:g}m/{summary['rto_minutes']:g}m "
        f"rpo={summary['rpo_hours']:g}h"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
