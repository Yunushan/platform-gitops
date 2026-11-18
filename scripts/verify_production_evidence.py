#!/usr/bin/env python3
"""Validate a private, commit-bound platform production-acceptance record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bounded_file import read_bounded_bytes, read_bounded_text
from strict_json import loads_strict_json
from verify_image_inventory_evidence import (
    EvidenceError as ImageInventoryEvidenceError,
    validate_evidence as validate_image_inventory,
)
from verify_openbao_ceremony_evidence import (
    EvidenceError as OpenBaoCeremonyEvidenceError,
    validate_evidence as validate_openbao_ceremony,
)
from verify_restore_evidence import (
    EvidenceError as RestoreEvidenceError,
    GITLAB_PROFILE,
    validate_evidence as validate_restore_evidence,
)
from verify_forgejo_recovery_evidence import validate_evidence as validate_forgejo_recovery
from verify_gitlab_recovery_evidence import validate_evidence as validate_gitlab_recovery


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_GATES = (
    "sourceProvenance",
    "repository",
    "profile",
    "renderedSchema",
    "supplyChain",
    "runtimeImageInventory",
    "rke2",
    "platformStatus",
    "tls",
    "policyReadiness",
    "networkIsolation",
    "internalTls",
    "openbaoReadiness",
    "openbaoCeremony",
    "observability",
    "capacity",
    "applicationHealth",
    "dataProtection",
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
OPENBAO_CLUSTER_ID_RE = re.compile(r"cluster_id_sha256=([a-f0-9]{64})")


class EvidenceError(ValueError):
    """Raised when a production-acceptance record is incomplete or untrusted."""


def nonempty(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value.strip()


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


def retained_path(value: str, root: Path, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceError(f"{label} must be a relative path below private/production-evidence")
    if relative.parts[:2] != ("private", "production-evidence"):
        raise EvidenceError(f"{label} must be below private/production-evidence")
    return root / relative


def validate_evidence(
    document: Any,
    *,
    root: Path,
    now: datetime,
    max_age_days: int,
    openbao_recovery_max_age_days: int = 180,
    restore_max_age_days: int = 92,
    forgejo_recovery_max_age_days: int = 92,
    gitlab_recovery_max_age_days: int = 92,
    expected_profile: str = "",
    expected_commit: str = "",
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise EvidenceError("production evidence must be a JSON object")
    release_id = nonempty(document, "releaseId")
    profile = nonempty(document, "profile")
    gitlab = profile == GITLAB_PROFILE
    version = 8 if gitlab else 7
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != version:
        raise EvidenceError(f"schemaVersion must be {version} for {profile}")
    commit = nonempty(document, "commit").lower()
    if not COMMIT_RE.fullmatch(commit):
        raise EvidenceError("commit must be a 40-character lowercase Git SHA")
    if expected_profile and profile != expected_profile:
        raise EvidenceError(f"profile {profile!r} does not match expected profile {expected_profile!r}")
    if expected_commit and commit != expected_commit.lower():
        raise EvidenceError("commit does not match the expected current Git revision")

    operator = nonempty(document, "operator")
    approver = nonempty(document, "approver")
    if operator.casefold() == approver.casefold():
        raise EvidenceError("operator and approver must be different people")
    if nonempty(document, "result").lower() != "passed":
        raise EvidenceError("result must be passed")

    source = document.get("source")
    if not isinstance(source, dict):
        raise EvidenceError("source must be an object")
    branch = nonempty(source, "branch")
    expected_ref = nonempty(source, "expectedRef")
    remote = nonempty(source, "remote")
    tree = nonempty(source, "tree").lower()
    remote_url_sha256 = nonempty(source, "remoteUrlSha256").lower()
    if source.get("clean") is not True:
        raise EvidenceError("source.clean must be true")
    if not REMOTE_RE.fullmatch(remote):
        raise EvidenceError("source.remote is invalid")
    if (
        not REF_RE.fullmatch(expected_ref)
        or ".." in expected_ref
        or "//" in expected_ref
        or expected_ref.endswith("/")
    ):
        raise EvidenceError("source.expectedRef is invalid")
    if not expected_ref.startswith(f"{remote}/"):
        raise EvidenceError("source.expectedRef must belong to source.remote")
    if not COMMIT_RE.fullmatch(tree):
        raise EvidenceError("source.tree must be a 40-character lowercase Git tree SHA")
    if not SHA256_RE.fullmatch(remote_url_sha256):
        raise EvidenceError("source.remoteUrlSha256 must be a lowercase SHA-256")

    completed_at = parse_timestamp(document.get("completedAt"))
    age = now.astimezone(timezone.utc) - completed_at
    if completed_at > now.astimezone(timezone.utc) + timedelta(minutes=5):
        raise EvidenceError("completedAt is in the future")
    if age > timedelta(days=max_age_days):
        raise EvidenceError(
            f"production evidence is stale ({age.days} days old; maximum is {max_age_days})"
        )

    gates = document.get("gates")
    if not isinstance(gates, dict):
        raise EvidenceError("gates must be an object")
    unknown = sorted(set(gates) - set(REQUIRED_GATES))
    if unknown:
        raise EvidenceError(f"gates contains unsupported entries: {', '.join(unknown)}")
    for name in REQUIRED_GATES:
        if str(gates.get(name, "")).strip().lower() != "passed":
            raise EvidenceError(f"gates.{name} must be passed")

    path = retained_path(nonempty(document, "logPath"), root, "logPath")
    expected_hash = nonempty(document, "logSha256").lower()
    if not SHA256_RE.fullmatch(expected_hash):
        raise EvidenceError("logSha256 must be a 64-character lowercase SHA-256")
    if not path.is_file():
        raise EvidenceError(f"retained production log is missing: {path}")
    log_bytes = read_bounded_bytes(path)
    if hashlib.sha256(log_bytes).hexdigest() != expected_hash:
        raise EvidenceError("retained production log hash does not match logSha256")
    text = log_bytes.decode("utf-8", errors="replace")
    for marker in (
        "== platform production evidence ==",
        "== platform-production-check ==",
        "platform-production-check",
        "== rendered-live-image-reconciliation ==",
        "Image inventory evidence accepted:",
        f"source_branch={branch}",
        f"source_expected_ref={expected_ref}",
        f"source_tree={tree}",
    ):
        if marker not in text:
            raise EvidenceError(f"retained production log is missing marker: {marker}")
    revision_marker = rf"\bargocd_revision=verified profile={re.escape(profile)} commit={commit} applications=[1-9][0-9]*\b"
    if gitlab and not re.search(revision_marker, text):
        raise EvidenceError("retained production log is missing the verified Argo CD release revision")
    openbao_cluster_ids = sorted(set(OPENBAO_CLUSTER_ID_RE.findall(text)))
    if len(openbao_cluster_ids) != 1:
        raise EvidenceError(
            "retained production log must contain exactly one sanitized OpenBao cluster identity"
        )

    inventory_reference = document.get("imageInventory")
    if not isinstance(inventory_reference, dict):
        raise EvidenceError("imageInventory must be an object")
    inventory_path_value = inventory_reference.get("path")
    inventory_hash = str(inventory_reference.get("sha256", "")).lower()
    if not isinstance(inventory_path_value, str) or not inventory_path_value.strip():
        raise EvidenceError("imageInventory.path must be a non-empty string")
    if not SHA256_RE.fullmatch(inventory_hash):
        raise EvidenceError("imageInventory.sha256 must be a lowercase SHA-256")
    inventory_path = retained_path(inventory_path_value.strip(), root, "imageInventory.path")
    if not inventory_path.is_file():
        raise EvidenceError(f"retained image inventory evidence is missing: {inventory_path}")
    inventory_bytes = read_bounded_bytes(inventory_path)
    if hashlib.sha256(inventory_bytes).hexdigest() != inventory_hash:
        raise EvidenceError("retained image inventory hash does not match imageInventory.sha256")
    try:
        inventory_document = loads_strict_json(inventory_bytes.decode("utf-8"))
        inventory_summary = validate_image_inventory(
            inventory_document,
            now=now,
            max_age_hours=max_age_days * 24,
            expected_profile=profile,
            expected_commit=commit,
        )
    except (OSError, json.JSONDecodeError, ImageInventoryEvidenceError) as exc:
        raise EvidenceError(f"retained image inventory evidence is invalid: {exc}") from exc

    ceremony_reference = document.get("openbaoCeremony")
    if not isinstance(ceremony_reference, dict):
        raise EvidenceError("openbaoCeremony must be an object")
    ceremony_path_value = ceremony_reference.get("path")
    ceremony_hash = str(ceremony_reference.get("sha256", "")).lower()
    if not isinstance(ceremony_path_value, str) or not ceremony_path_value.strip():
        raise EvidenceError("openbaoCeremony.path must be a non-empty string")
    if not SHA256_RE.fullmatch(ceremony_hash):
        raise EvidenceError("openbaoCeremony.sha256 must be a lowercase SHA-256")
    ceremony_path = retained_path(
        ceremony_path_value.strip(), root, "openbaoCeremony.path"
    )
    if not ceremony_path.is_file():
        raise EvidenceError(f"retained OpenBao ceremony evidence is missing: {ceremony_path}")
    ceremony_bytes = read_bounded_bytes(ceremony_path)
    if hashlib.sha256(ceremony_bytes).hexdigest() != ceremony_hash:
        raise EvidenceError("retained OpenBao ceremony hash does not match openbaoCeremony.sha256")
    try:
        ceremony_document = loads_strict_json(ceremony_bytes.decode("utf-8"))
        ceremony_summary = validate_openbao_ceremony(
            ceremony_document,
            root=root,
            now=now,
            max_recovery_age_days=openbao_recovery_max_age_days,
            expected_profile=profile,
            expected_source_commit=commit,
            expected_cluster_id_sha256=openbao_cluster_ids[0],
        )
    except (OSError, json.JSONDecodeError, OpenBaoCeremonyEvidenceError) as exc:
        raise EvidenceError(f"retained OpenBao ceremony evidence is invalid: {exc}") from exc

    restore_reference = document.get("restoreEvidence")
    if not isinstance(restore_reference, dict):
        raise EvidenceError("restoreEvidence must be an object")
    restore_path_value = restore_reference.get("path")
    restore_hash = str(restore_reference.get("sha256", "")).lower()
    if not isinstance(restore_path_value, str) or not restore_path_value.strip():
        raise EvidenceError("restoreEvidence.path must be a non-empty string")
    if not SHA256_RE.fullmatch(restore_hash):
        raise EvidenceError("restoreEvidence.sha256 must be a lowercase SHA-256")
    restore_path = retained_path(
        restore_path_value.strip(), root, "restoreEvidence.path"
    )
    if not restore_path.is_file():
        raise EvidenceError(f"retained restore evidence is missing: {restore_path}")
    restore_bytes = read_bounded_bytes(restore_path)
    if hashlib.sha256(restore_bytes).hexdigest() != restore_hash:
        raise EvidenceError("retained restore evidence hash does not match restoreEvidence.sha256")
    try:
        restore_document = loads_strict_json(restore_bytes.decode("utf-8"))
        restore_summary = validate_restore_evidence(
            restore_document,
            now=now,
            max_age_days=restore_max_age_days,
            expected_profile=profile,
            expected_commit=commit,
        )
    except (OSError, json.JSONDecodeError, RestoreEvidenceError) as exc:
        raise EvidenceError(f"retained restore evidence is invalid: {exc}") from exc

    recovery_key, other_key = (
        ("gitlabRecovery", "forgejoRecovery") if gitlab else ("forgejoRecovery", "gitlabRecovery")
    )
    if other_key in document:
        raise EvidenceError(f"{other_key} cannot certify {profile}")
    recovery_reference = document.get(recovery_key)
    if not isinstance(recovery_reference, dict):
        raise EvidenceError(f"{recovery_key} must be an object")
    recovery_hash = nonempty(recovery_reference, "sha256")
    if not SHA256_RE.fullmatch(recovery_hash):
        raise EvidenceError(f"{recovery_key}.sha256 must be a lowercase SHA-256")
    if gitlab and f"GitLab recovery evidence accepted: sha256={recovery_hash}" not in text.splitlines():
        raise EvidenceError("retained production log does not record verification of the GitLab recovery hash")
    recovery_path = retained_path(nonempty(recovery_reference, "path"), root, f"{recovery_key}.path")
    try:
        recovery_bytes = read_bounded_bytes(recovery_path)
        if hashlib.sha256(recovery_bytes).hexdigest() != recovery_hash:
            raise EvidenceError(f"retained {recovery_key} hash does not match {recovery_key}.sha256")
        recovery_document = loads_strict_json(recovery_bytes.decode("utf-8"))
        if gitlab:
            recovery_summary = validate_gitlab_recovery(
                recovery_document, now=now, max_age_days=gitlab_recovery_max_age_days,
                expected_commit=commit, expected_cluster_uid=inventory_document["live"]["clusterUid"],
            )
            if recovery_summary["completed_at"] > completed_at:
                raise EvidenceError("GitLab recovery evidence postdates production acceptance")
        else:
            recovery_summary = validate_forgejo_recovery(
                recovery_document, now=now, max_age_days=forgejo_recovery_max_age_days,
                expected_profile=profile, expected_commit=commit,
            )
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"retained {recovery_key} evidence is invalid: {exc}") from exc

    forge = "gitlab" if gitlab else "forgejo"
    return {
        "release_id": release_id,
        "profile": profile,
        "commit": commit,
        "branch": branch,
        "expected_ref": expected_ref,
        "tree": tree,
        "completed_at": completed_at,
        "age_days": age.total_seconds() / 86400,
        "log_path": path,
        "image_inventory_path": inventory_path,
        "image_inventory_images": inventory_summary["images"],
        "openbao_ceremony_path": ceremony_path,
        "openbao_ceremony_id": ceremony_summary["ceremony_id"],
        "openbao_seal_mode": ceremony_summary["seal_mode"],
        "restore_evidence_path": restore_path,
        "restore_drill_id": restore_summary["drill_id"],
        f"{forge}_recovery_path": recovery_path,
        f"{forge}_recovery_drill_id": recovery_summary["drill_id"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_PRODUCTION_EVIDENCE_MAX_AGE_DAYS", "7")),
    )
    parser.add_argument(
        "--openbao-recovery-max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_OPENBAO_RECOVERY_MAX_AGE_DAYS", "180")),
    )
    parser.add_argument(
        "--restore-max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_RESTORE_DRILL_MAX_AGE_DAYS", "92")),
    )
    parser.add_argument(
        "--forgejo-recovery-max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_FORGEJO_RECOVERY_MAX_AGE_DAYS", "92")),
    )
    parser.add_argument(
        "--gitlab-recovery-max-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_GITLAB_RECOVERY_MAX_AGE_DAYS", "92")),
    )
    parser.add_argument("--expected-profile", default=os.environ.get("PLATFORM_PROFILE", ""))
    parser.add_argument("--expected-commit", default=os.environ.get("PLATFORM_EXPECTED_COMMIT", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_days <= 0:
        print("--max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if args.openbao_recovery_max_age_days <= 0:
        print("--openbao-recovery-max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if args.restore_max_age_days <= 0:
        print("--restore-max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if args.forgejo_recovery_max_age_days <= 0:
        print("--forgejo-recovery-max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if args.gitlab_recovery_max_age_days <= 0:
        print("--gitlab-recovery-max-age-days must be greater than zero", file=sys.stderr)
        return 2
    if not args.evidence_file.is_file():
        print(f"Production evidence file does not exist: {args.evidence_file}", file=sys.stderr)
        return 1
    try:
        document = loads_strict_json(read_bounded_text(args.evidence_file))
        summary = validate_evidence(
            document,
            root=ROOT,
            now=datetime.now(timezone.utc),
            max_age_days=args.max_age_days,
            openbao_recovery_max_age_days=args.openbao_recovery_max_age_days,
            restore_max_age_days=args.restore_max_age_days,
            forgejo_recovery_max_age_days=args.forgejo_recovery_max_age_days,
            gitlab_recovery_max_age_days=args.gitlab_recovery_max_age_days,
            expected_profile=args.expected_profile,
            expected_commit=args.expected_commit,
        )
    except (OSError, ValueError) as exc:
        print(f"Production evidence validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Production evidence accepted: "
        f"release={summary['release_id']} profile={summary['profile']} "
        f"commit={summary['commit']} completed={summary['completed_at'].isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
