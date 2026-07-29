#!/usr/bin/env python3
"""Validate secret-free OpenBao initialization and recovery ceremony evidence."""

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


ROOT = Path(__file__).resolve().parents[1]
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SEAL_MODES = {"shamir-pgp", "kms-auto-unseal", "hsm-auto-unseal"}
REQUIRED_CONTROLS = (
    "initialRootTokenRevoked",
    "leastPrivilegeAdminEstablished",
    "kubernetesAuthConfigured",
    "auditDeviceEnabled",
    "restartRecoveryTested",
    "quorumRecoveryTested",
)


class EvidenceError(ValueError):
    """Raised when OpenBao ceremony evidence is incomplete or unsafe."""


def nonempty(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value.strip()


def sha256_value(document: dict[str, Any], name: str) -> str:
    value = nonempty(document, name).lower()
    if not SHA256_RE.fullmatch(value):
        raise EvidenceError(f"{name} must be a 64-character lowercase SHA-256")
    return value


def positive_integer(document: dict[str, Any], name: str) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceError(f"{name} must be a positive integer")
    return value


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


def configuration_directory(root: Path, profile: str) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise EvidenceError("profile is invalid")
    directory = root / "gitops" / "clusters" / "rke2-main" / profile / "apps" / "openbao"
    if not directory.is_dir():
        raise EvidenceError(f"OpenBao configuration directory is missing: {directory}")
    return directory


def configuration_sha256(root: Path, profile: str) -> str:
    """Hash paths and contents for the profile-specific OpenBao application tree."""

    directory = configuration_directory(root, profile)
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        raise EvidenceError(f"OpenBao configuration directory is empty: {directory}")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_evidence(
    document: Any,
    *,
    root: Path,
    now: datetime,
    max_recovery_age_days: int,
    expected_profile: str = "",
    expected_source_commit: str = "",
    expected_configuration_sha256: str = "",
    expected_cluster_id_sha256: str = "",
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise EvidenceError("OpenBao ceremony evidence must be a JSON object")
    if document.get("schemaVersion") != 1:
        raise EvidenceError("schemaVersion must be 1")

    ceremony_id = nonempty(document, "ceremonyId")
    if not IDENTIFIER_RE.fullmatch(ceremony_id):
        raise EvidenceError("ceremonyId contains unsupported characters")
    profile = nonempty(document, "profile")
    if not PROFILE_RE.fullmatch(profile):
        raise EvidenceError("profile is invalid")
    if expected_profile and profile != expected_profile:
        raise EvidenceError(f"profile {profile!r} does not match expected profile {expected_profile!r}")
    source_commit = nonempty(document, "sourceCommit").lower()
    if not COMMIT_RE.fullmatch(source_commit):
        raise EvidenceError("sourceCommit must be a 40-character lowercase Git SHA")
    if expected_source_commit:
        expected_commit = expected_source_commit.lower()
        if not COMMIT_RE.fullmatch(expected_commit):
            raise EvidenceError("expected source commit is invalid")
        if source_commit != expected_commit:
            raise EvidenceError("sourceCommit does not match the accepted production revision")
    if nonempty(document, "result").lower() != "passed":
        raise EvidenceError("result must be passed")

    operator = nonempty(document, "operator")
    approver = nonempty(document, "approver")
    if operator.casefold() == approver.casefold():
        raise EvidenceError("operator and approver must be different people")

    completed_at = parse_timestamp(document.get("completedAt"), "completedAt")
    recovery_tested_at = parse_timestamp(document.get("recoveryTestedAt"), "recoveryTestedAt")
    current_time = now.astimezone(timezone.utc)
    if completed_at > current_time + timedelta(minutes=5):
        raise EvidenceError("completedAt is in the future")
    if recovery_tested_at > current_time + timedelta(minutes=5):
        raise EvidenceError("recoveryTestedAt is in the future")
    if recovery_tested_at < completed_at:
        raise EvidenceError("recoveryTestedAt must not precede completedAt")
    recovery_age = current_time - recovery_tested_at
    if recovery_age > timedelta(days=max_recovery_age_days):
        raise EvidenceError(
            "OpenBao recovery evidence is stale "
            f"({recovery_age.days} days old; maximum is {max_recovery_age_days})"
        )

    configuration_hash = sha256_value(document, "configurationSha256")
    expected_configuration = expected_configuration_sha256.lower()
    if expected_configuration:
        if not SHA256_RE.fullmatch(expected_configuration):
            raise EvidenceError("expected configuration SHA-256 is invalid")
    else:
        expected_configuration = configuration_sha256(root, profile)
    if configuration_hash != expected_configuration:
        raise EvidenceError("configurationSha256 does not match the current OpenBao input tree")

    cluster = document.get("cluster")
    if not isinstance(cluster, dict):
        raise EvidenceError("cluster must be an object")
    cluster_id_hash = sha256_value(cluster, "clusterIdSha256")
    if expected_cluster_id_sha256:
        expected_cluster = expected_cluster_id_sha256.lower()
        if not SHA256_RE.fullmatch(expected_cluster):
            raise EvidenceError("expected cluster ID SHA-256 is invalid")
        if cluster_id_hash != expected_cluster:
            raise EvidenceError("cluster.clusterIdSha256 does not match the live OpenBao cluster")

    seal = document.get("seal")
    if not isinstance(seal, dict):
        raise EvidenceError("seal must be an object")
    seal_mode = nonempty(seal, "mode")
    if seal_mode not in SEAL_MODES:
        raise EvidenceError("seal.mode must be shamir-pgp, kms-auto-unseal, or hsm-auto-unseal")
    shares = positive_integer(seal, "shares")
    threshold = positive_integer(seal, "threshold")
    custodians = positive_integer(seal, "distinctCustodians")
    if shares < 5:
        raise EvidenceError("seal.shares must be at least 5")
    if threshold < 3 or threshold >= shares:
        raise EvidenceError("seal.threshold must be at least 3 and lower than seal.shares")
    if custodians != shares:
        raise EvidenceError("seal.distinctCustodians must equal seal.shares")

    fingerprints = seal.get("custodianKeyFingerprintSha256")
    if not isinstance(fingerprints, list) or len(fingerprints) != shares:
        raise EvidenceError("seal.custodianKeyFingerprintSha256 must contain one entry per share")
    normalized_fingerprints: list[str] = []
    for fingerprint in fingerprints:
        normalized = str(fingerprint).lower()
        if not SHA256_RE.fullmatch(normalized):
            raise EvidenceError("every custodian key fingerprint hash must be a lowercase SHA-256")
        normalized_fingerprints.append(normalized)
    if len(set(normalized_fingerprints)) != shares:
        raise EvidenceError("custodian key fingerprint hashes must be unique")

    root_recipient = sha256_value(seal, "rootTokenRecipientFingerprintSha256")
    if root_recipient in normalized_fingerprints:
        raise EvidenceError("root token recipient must be separate from every key-share custodian")
    if seal.get("encryptedAtCreation") is not True:
        raise EvidenceError("seal.encryptedAtCreation must be true")
    if seal.get("rootTokenEncryptedAtCreation") is not True:
        raise EvidenceError("seal.rootTokenEncryptedAtCreation must be true")
    if seal.get("plaintextMaterialRetained") is not False:
        raise EvidenceError("seal.plaintextMaterialRetained must be false")
    if positive_integer(seal, "offlineEscrowCopies") < 2:
        raise EvidenceError("seal.offlineEscrowCopies must be at least 2")

    provider_hash = str(seal.get("providerKeySha256") or "").lower()
    if seal_mode == "shamir-pgp":
        if provider_hash:
            raise EvidenceError("seal.providerKeySha256 is only valid for auto-unseal modes")
    elif not SHA256_RE.fullmatch(provider_hash):
        raise EvidenceError("auto-unseal evidence requires seal.providerKeySha256")

    controls = document.get("controls")
    if not isinstance(controls, dict):
        raise EvidenceError("controls must be an object")
    unknown_controls = sorted(set(controls) - set(REQUIRED_CONTROLS))
    if unknown_controls:
        raise EvidenceError(f"controls contains unsupported entries: {', '.join(unknown_controls)}")
    for name in REQUIRED_CONTROLS:
        if controls.get(name) is not True:
            raise EvidenceError(f"controls.{name} must be true")

    return {
        "ceremony_id": ceremony_id,
        "profile": profile,
        "source_commit": source_commit,
        "completed_at": completed_at,
        "recovery_tested_at": recovery_tested_at,
        "recovery_age_days": recovery_age.total_seconds() / 86400,
        "configuration_sha256": configuration_hash,
        "cluster_id_sha256": cluster_id_hash,
        "seal_mode": seal_mode,
        "shares": shares,
        "threshold": threshold,
        "custodians": custodians,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", nargs="?", type=Path)
    parser.add_argument(
        "--max-recovery-age-days",
        type=int,
        default=int(os.environ.get("PLATFORM_OPENBAO_RECOVERY_MAX_AGE_DAYS", "180")),
    )
    parser.add_argument("--expected-profile", default=os.environ.get("PLATFORM_PROFILE", ""))
    parser.add_argument(
        "--expected-source-commit",
        default=os.environ.get("PLATFORM_OPENBAO_SOURCE_COMMIT", ""),
    )
    parser.add_argument(
        "--expected-configuration-sha256",
        default=os.environ.get("PLATFORM_OPENBAO_CONFIGURATION_SHA256", ""),
    )
    parser.add_argument(
        "--expected-cluster-id-sha256",
        default=os.environ.get("PLATFORM_OPENBAO_CLUSTER_ID_SHA256", ""),
    )
    parser.add_argument("--print-configuration-sha256", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_recovery_age_days <= 0:
        print("--max-recovery-age-days must be greater than zero", file=sys.stderr)
        return 2
    if args.print_configuration_sha256:
        if not args.expected_profile:
            print("--expected-profile is required with --print-configuration-sha256", file=sys.stderr)
            return 2
        try:
            print(configuration_sha256(ROOT, args.expected_profile))
        except (EvidenceError, OSError) as exc:
            print(f"OpenBao configuration digest failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.evidence_file is None or not args.evidence_file.is_file():
        print(f"OpenBao ceremony evidence file does not exist: {args.evidence_file}", file=sys.stderr)
        return 1
    try:
        document = json.loads(args.evidence_file.read_text(encoding="utf-8"))
        summary = validate_evidence(
            document,
            root=ROOT,
            now=datetime.now(timezone.utc),
            max_recovery_age_days=args.max_recovery_age_days,
            expected_profile=args.expected_profile,
            expected_source_commit=args.expected_source_commit,
            expected_configuration_sha256=args.expected_configuration_sha256,
            expected_cluster_id_sha256=args.expected_cluster_id_sha256,
        )
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"OpenBao ceremony evidence validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "OpenBao ceremony evidence accepted: "
        f"ceremony={summary['ceremony_id']} profile={summary['profile']} "
        f"mode={summary['seal_mode']} recovery={summary['recovery_tested_at'].isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
