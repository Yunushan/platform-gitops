#!/usr/bin/env python3
"""Verify detached, cryptographically signed production-acceptance approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bounded_file import read_bounded_bytes, read_bounded_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from strict_json import loads_strict_json
from subprocess_timeout import bounded_timeout_seconds
import verify_production_evidence as production_evidence


ROOT = Path(__file__).resolve().parents[1]
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
APPROVAL_STATEMENT = "production-acceptance-approved"
APPROVAL_FIELDS = {
    "schemaVersion",
    "approvedAt",
    "approver",
    "releaseId",
    "profile",
    "commit",
    "productionEvidenceSha256",
    "approvalKeySha256",
    "statement",
    "result",
}
COSIGN_TIMEOUT_SECONDS = 60


class ApprovalError(ValueError):
    """Raised when detached production approval is incomplete or untrusted."""


def artifact_sha256(path: Path) -> str:
    return hashlib.sha256(read_bounded_bytes(path)).hexdigest()


def nonempty(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError(f"{name} must be a non-empty string")
    return value.strip()


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError(f"{label} must be an ISO-8601 timestamp")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApprovalError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ApprovalError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def build_approval_document(
    production_document: dict[str, Any],
    *,
    production_sha256: str,
    approval_key_sha256: str,
    approver: str,
    approved_at: datetime | None = None,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(production_sha256):
        raise ApprovalError("production evidence SHA-256 is invalid")
    if not SHA256_RE.fullmatch(approval_key_sha256):
        raise ApprovalError("approval public-key SHA-256 is invalid")
    expected_approver = nonempty(production_document, "approver")
    operator = nonempty(production_document, "operator")
    normalized_approver = approver.strip()
    if not normalized_approver:
        raise ApprovalError("approver must be a non-empty string")
    if normalized_approver.casefold() != expected_approver.casefold():
        raise ApprovalError("approver does not match production evidence")
    if normalized_approver.casefold() == operator.casefold():
        raise ApprovalError("production operator cannot approve the same evidence")
    approved_at = approved_at or datetime.now(timezone.utc)
    return {
        "schemaVersion": 1,
        "approvedAt": approved_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "approver": expected_approver,
        "releaseId": nonempty(production_document, "releaseId"),
        "profile": nonempty(production_document, "profile"),
        "commit": nonempty(production_document, "commit").lower(),
        "productionEvidenceSha256": production_sha256,
        "approvalKeySha256": approval_key_sha256,
        "statement": APPROVAL_STATEMENT,
        "result": "approved",
    }


def validate_approval_document(
    approval_document: Any,
    *,
    production_document: dict[str, Any],
    production_sha256: str,
    expected_key_sha256: str,
    now: datetime,
    max_age_days: int,
) -> dict[str, str | datetime]:
    if not isinstance(approval_document, dict):
        raise ApprovalError("production approval must be a JSON object")
    actual_fields = set(approval_document)
    if actual_fields != APPROVAL_FIELDS:
        missing = sorted(APPROVAL_FIELDS - actual_fields)
        unknown = sorted(actual_fields - APPROVAL_FIELDS)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unsupported " + ", ".join(unknown))
        raise ApprovalError(
            "production approval fields do not match the contract: " + "; ".join(details)
        )
    if approval_document.get("schemaVersion") != 1:
        raise ApprovalError("schemaVersion must be 1")
    if nonempty(approval_document, "result").lower() != "approved":
        raise ApprovalError("result must be approved")
    if nonempty(approval_document, "statement") != APPROVAL_STATEMENT:
        raise ApprovalError(f"statement must be {APPROVAL_STATEMENT}")
    if not SHA256_RE.fullmatch(production_sha256):
        raise ApprovalError("production evidence SHA-256 is invalid")
    if not SHA256_RE.fullmatch(expected_key_sha256):
        raise ApprovalError("expected approval public-key SHA-256 is invalid")
    if nonempty(approval_document, "productionEvidenceSha256") != production_sha256:
        raise ApprovalError("approval does not bind the exact production evidence")
    if nonempty(approval_document, "approvalKeySha256") != expected_key_sha256:
        raise ApprovalError("approval public-key SHA-256 does not match the trust root")

    commit = nonempty(approval_document, "commit").lower()
    if not COMMIT_RE.fullmatch(commit):
        raise ApprovalError("commit must be a 40-character lowercase Git SHA")
    for field in ("releaseId", "profile", "commit"):
        if nonempty(approval_document, field) != nonempty(production_document, field):
            raise ApprovalError(f"approval {field} does not match production evidence")
    approver = nonempty(approval_document, "approver")
    expected_approver = nonempty(production_document, "approver")
    operator = nonempty(production_document, "operator")
    if approver.casefold() != expected_approver.casefold():
        raise ApprovalError("approval identity does not match production evidence")
    if approver.casefold() == operator.casefold():
        raise ApprovalError("production operator cannot approve the same evidence")

    approved_at = parse_timestamp(approval_document.get("approvedAt"), "approvedAt")
    completed_at = parse_timestamp(production_document.get("completedAt"), "completedAt")
    current = now.astimezone(timezone.utc)
    if approved_at > current + timedelta(minutes=5):
        raise ApprovalError("approvedAt is in the future")
    if approved_at < completed_at - timedelta(minutes=5):
        raise ApprovalError("approval predates production acceptance")
    age = current - approved_at
    if age > timedelta(days=max_age_days):
        raise ApprovalError(
            f"production approval is stale ({age.days} days old; maximum is {max_age_days})"
        )
    return {
        "approver": approver,
        "approved_at": approved_at,
        "release_id": nonempty(approval_document, "releaseId"),
        "profile": nonempty(approval_document, "profile"),
        "commit": commit,
    }


def verify_signature(
    *,
    approval_path: Path,
    bundle_path: Path,
    public_key_path: Path,
    expected_key_sha256: str,
    cosign_bin: str,
    runner: Any = run_bounded,
) -> dict[str, str]:
    if not approval_path.is_file():
        raise ApprovalError(f"production approval does not exist: {approval_path}")
    if not bundle_path.is_file():
        raise ApprovalError(f"production approval Sigstore bundle does not exist: {bundle_path}")
    if not public_key_path.is_file():
        raise ApprovalError(f"production approval public key does not exist: {public_key_path}")
    if not SHA256_RE.fullmatch(expected_key_sha256):
        raise ApprovalError("expected approval public-key SHA-256 is invalid")
    actual_key_sha256 = artifact_sha256(public_key_path)
    if actual_key_sha256 != expected_key_sha256:
        raise ApprovalError("production approval public key does not match its pinned SHA-256")

    cosign_path = Path(cosign_bin)
    executable = str(cosign_path.resolve()) if cosign_path.is_file() else shutil.which(cosign_bin)
    if not executable:
        raise ApprovalError("Cosign is required to verify production approval; set COSIGN_BIN")
    try:
        timeout = bounded_timeout_seconds(
            COSIGN_TIMEOUT_SECONDS,
            "PLATFORM_PRODUCTION_APPROVAL_COSIGN_TIMEOUT_SECONDS",
        )
        result = runner(
            [
                str(executable),
                "verify-blob",
                "--key",
                str(public_key_path),
                "--bundle",
                str(bundle_path),
                str(approval_path),
            ],
            text=True,
            timeout=timeout,
            check=False,
        )
    except ValueError as exc:
        raise ApprovalError(str(exc)) from None
    except subprocess.TimeoutExpired:
        raise ApprovalError(
            f"Cosign production approval verification timed out after {timeout:g} seconds"
        ) from None
    except BoundedSubprocessError as exc:
        raise ApprovalError(f"Cosign production approval output rejected: {exc}") from None
    except OSError as exc:
        raise ApprovalError(f"Cosign production approval verification could not run: {exc}") from exc
    if result.returncode != 0:
        raise ApprovalError("Cosign rejected the detached production approval signature")
    return {
        "productionApprovalBundle": artifact_sha256(bundle_path),
        "productionApprovalPublicKey": actual_key_sha256,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("approval_file", type=Path)
    parser.add_argument("--production-evidence", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument(
        "--public-key-sha256",
        default=os.environ.get("PLATFORM_PRODUCTION_APPROVAL_PUBLIC_KEY_SHA256", ""),
    )
    parser.add_argument("--profile", default=os.environ.get("PLATFORM_PROFILE", ""))
    parser.add_argument("--commit", default=os.environ.get("PLATFORM_EXPECTED_COMMIT", ""))
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument("--cosign-bin", default=os.environ.get("COSIGN_BIN", "cosign"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_days <= 0:
        print("--max-age-days must be greater than zero", file=sys.stderr)
        return 2
    try:
        production_document = loads_strict_json(
            read_bounded_text(args.production_evidence)
        )
        if not isinstance(production_document, dict):
            raise ApprovalError("production evidence must be a JSON object")
        production_evidence.validate_evidence(
            production_document,
            root=ROOT,
            now=datetime.now(timezone.utc),
            max_age_days=args.max_age_days,
            expected_profile=args.profile,
            expected_commit=args.commit,
        )
        approval_document = loads_strict_json(read_bounded_text(args.approval_file))
        summary = validate_approval_document(
            approval_document,
            production_document=production_document,
            production_sha256=artifact_sha256(args.production_evidence),
            expected_key_sha256=args.public_key_sha256,
            now=datetime.now(timezone.utc),
            max_age_days=args.max_age_days,
        )
        verify_signature(
            approval_path=args.approval_file,
            bundle_path=args.bundle,
            public_key_path=args.public_key,
            expected_key_sha256=args.public_key_sha256,
            cosign_bin=args.cosign_bin,
        )
    except (
        OSError,
        json.JSONDecodeError,
        ApprovalError,
        production_evidence.EvidenceError,
    ) as exc:
        print(f"Production approval validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Production approval accepted: "
        f"release={summary['release_id']} profile={summary['profile']} "
        f"commit={summary['commit']} approver={summary['approver']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
