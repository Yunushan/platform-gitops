#!/usr/bin/env python3
"""Independently validate rendered/live image coverage evidence."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


DIGEST_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class EvidenceError(ValueError):
    """Raised when image coverage evidence is incomplete or inconsistent."""


def nonempty(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{key} must be a non-empty string")
    return value.strip()


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be an RFC3339 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceError(f"{label} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def require_fresh_time(value: Any, label: str, *, now: datetime, max_age_hours: int) -> datetime:
    parsed = parse_time(value, label)
    current = now.astimezone(timezone.utc)
    if parsed > current + timedelta(minutes=5):
        raise EvidenceError(f"{label} is in the future")
    if current - parsed > timedelta(hours=max_age_hours):
        raise EvidenceError(f"{label} is stale")
    return parsed


def validate_evidence(
    document: Any,
    *,
    now: datetime,
    max_age_hours: int,
    expected_profile: str = "",
    expected_commit: str = "",
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise EvidenceError("image inventory evidence must be a JSON object")
    if document.get("schemaVersion") != 1:
        raise EvidenceError("schemaVersion must be 1")
    if nonempty(document, "result") != "passed":
        raise EvidenceError("result must be passed")
    profile = nonempty(document, "profile")
    commit = nonempty(document, "commit").lower()
    registry = nonempty(document, "expectedRegistry").lower()
    if expected_profile and profile != expected_profile:
        raise EvidenceError("profile does not match the expected production profile")
    if not COMMIT_RE.fullmatch(commit):
        raise EvidenceError("commit must be a lowercase 40-character Git SHA")
    if expected_commit and commit != expected_commit.lower():
        raise EvidenceError("commit does not match the expected production revision")
    if "/" in registry or "://" in registry or any(char.isspace() for char in registry):
        raise EvidenceError("expectedRegistry must be a registry host with optional port")

    generated = require_fresh_time(
        document.get("generatedAt"),
        "generatedAt",
        now=now,
        max_age_hours=max_age_hours,
    )

    inputs = document.get("inputs")
    if not isinstance(inputs, dict):
        raise EvidenceError("inputs must be an object")
    for key in ("renderedSummarySha256", "liveInventorySha256", "signatureReportSha256"):
        if not SHA256_RE.fullmatch(str(inputs.get(key, ""))):
            raise EvidenceError(f"inputs.{key} must be a lowercase SHA-256")
    exception_hash = inputs.get("exceptionsSha256")
    if exception_hash is not None and not SHA256_RE.fullmatch(str(exception_hash)):
        raise EvidenceError("inputs.exceptionsSha256 must be a lowercase SHA-256 when present")

    rendered = document.get("rendered")
    live = document.get("live")
    if not isinstance(rendered, dict) or not isinstance(live, dict):
        raise EvidenceError("rendered and live summaries must be objects")
    if rendered.get("unresolved") != 0:
        raise EvidenceError("rendered.unresolved must be zero")
    if live.get("unresolved") != 0:
        raise EvidenceError("live.unresolved must be zero")
    if not isinstance(rendered.get("uniqueImages"), int) or rendered["uniqueImages"] < 1:
        raise EvidenceError("rendered.uniqueImages must be positive")
    if not isinstance(live.get("uniqueImages"), int) or live["uniqueImages"] < 1:
        raise EvidenceError("live.uniqueImages must be positive")
    if not isinstance(rendered.get("references"), int) or rendered["references"] < 1:
        raise EvidenceError("rendered.references must be positive")
    if rendered["references"] < rendered["uniqueImages"]:
        raise EvidenceError("rendered.references cannot be smaller than rendered.uniqueImages")
    if not isinstance(live.get("containers"), int) or live["containers"] < 1:
        raise EvidenceError("live.containers must be positive")
    if live["containers"] < live["uniqueImages"]:
        raise EvidenceError("live.containers cannot be smaller than live.uniqueImages")
    cluster_uid = live.get("clusterUid")
    if not isinstance(cluster_uid, str) or not cluster_uid.strip():
        raise EvidenceError("live.clusterUid must be a non-empty string")
    require_fresh_time(
        live.get("capturedAt"),
        "live.capturedAt",
        now=now,
        max_age_hours=max_age_hours,
    )

    images = document.get("images")
    if not isinstance(images, list) or not images:
        raise EvidenceError("images must contain at least one covered digest")
    seen: set[str] = set()
    private_count = 0
    exception_count = 0
    rendered_count = 0
    live_count = 0
    signature_count = 0
    for index, item in enumerate(images):
        if not isinstance(item, dict):
            raise EvidenceError(f"images[{index}] must be an object")
        image = nonempty(item, "image").lower()
        if not DIGEST_IMAGE_RE.fullmatch(image):
            raise EvidenceError(f"images[{index}].image must be digest-pinned")
        if image in seen:
            raise EvidenceError(f"duplicate image coverage entry: {image}")
        seen.add(image)
        signature_verified = item.get("signatureVerified") is True
        admission_enforced = item.get("admissionEnforced") is True
        rendered_image = item.get("rendered")
        live_image = item.get("live")
        if not isinstance(rendered_image, bool) or not isinstance(live_image, bool):
            raise EvidenceError(f"images[{index}] rendered/live flags must be booleans")
        if not rendered_image and not live_image:
            raise EvidenceError(f"image is neither rendered nor live: {image}")
        rendered_count += int(rendered_image)
        live_count += int(live_image)
        signature_count += int(signature_verified)
        exception = item.get("exception")
        image_registry = image.split("/", 1)[0]
        if image_registry == registry:
            private_count += 1
            if not signature_verified or not admission_enforced:
                raise EvidenceError(
                    f"private-registry image lacks signature or admission coverage: {image}"
                )
            if exception is not None:
                raise EvidenceError(f"private-registry image cannot use an exception: {image}")
        elif exception is None:
            raise EvidenceError(f"outside-registry image lacks an admission-scope exception: {image}")
        else:
            exception_count += 1
            if not isinstance(exception, dict):
                raise EvidenceError(f"exception for {image} must be an object")
            for key in (
                "id",
                "owner",
                "approvedBy",
                "reason",
                "ticket",
                "createdAt",
                "expiresAt",
                "vulnerabilityReport",
                "vulnerabilityReportSha256",
            ):
                value = exception.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise EvidenceError(f"exception for {image} is missing {key}")
            if exception["owner"].casefold() == exception["approvedBy"].casefold():
                raise EvidenceError(f"exception owner and approver must differ: {image}")
            try:
                created = date.fromisoformat(exception["createdAt"])
                expires = date.fromisoformat(exception["expiresAt"])
            except ValueError as exc:
                raise EvidenceError(f"exception dates must use YYYY-MM-DD: {image}") from exc
            if created > generated.date():
                raise EvidenceError(f"exception creation date is after evidence generation: {image}")
            if expires < now.astimezone(timezone.utc).date():
                raise EvidenceError(f"exception is expired: {image}")
            if expires <= created or expires > created + timedelta(days=90):
                raise EvidenceError(f"exception must expire within 90 days: {image}")
            report_path = Path(exception["vulnerabilityReport"])
            if (
                report_path.is_absolute()
                or ".." in report_path.parts
                or report_path.parts[:2] != ("private", "supply-chain")
            ):
                raise EvidenceError(f"exception vulnerability report path is unsafe: {image}")
            if not SHA256_RE.fullmatch(exception["vulnerabilityReportSha256"]):
                raise EvidenceError(f"exception vulnerability report hash is invalid: {image}")
            for key in ("highVulnerabilities", "criticalVulnerabilities"):
                value = exception.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise EvidenceError(f"exception {key} must be a non-negative integer: {image}")
        if not signature_verified and exception is None:
            raise EvidenceError(f"image has neither signature nor exception coverage: {image}")

    summary = document.get("summary")
    if not isinstance(summary, dict):
        raise EvidenceError("summary must be an object")
    expected_counts = {
        "images": len(images),
        "privateRegistryImages": private_count,
        "signatureVerifiedImages": signature_count,
        "exceptions": exception_count,
        "uncovered": 0,
    }
    for key, value in expected_counts.items():
        if summary.get(key) != value:
            raise EvidenceError(f"summary.{key} does not match evidence entries")
    if rendered["uniqueImages"] != rendered_count:
        raise EvidenceError("rendered.uniqueImages does not match evidence entries")
    if live["uniqueImages"] != live_count:
        raise EvidenceError("live.uniqueImages does not match evidence entries")
    return {
        "profile": profile,
        "commit": commit,
        "registry": registry,
        "images": len(images),
        "exceptions": exception_count,
        "generated_at": generated,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=int(os.environ.get("PLATFORM_IMAGE_INVENTORY_MAX_AGE_HOURS", "24")),
    )
    parser.add_argument("--expected-profile", default=os.environ.get("PLATFORM_PROFILE", ""))
    parser.add_argument("--expected-commit", default=os.environ.get("PLATFORM_EXPECTED_COMMIT", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_hours <= 0:
        print("--max-age-hours must be positive", file=sys.stderr)
        return 2
    try:
        document = json.loads(args.evidence.read_text(encoding="utf-8"))
        summary = validate_evidence(
            document,
            now=datetime.now(timezone.utc),
            max_age_hours=args.max_age_hours,
            expected_profile=args.expected_profile,
            expected_commit=args.expected_commit,
        )
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"Image inventory evidence validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Image inventory evidence accepted: "
        f"profile={summary['profile']} commit={summary['commit']} "
        f"images={summary['images']} exceptions={summary['exceptions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
