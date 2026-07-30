#!/usr/bin/env python3
"""Reconcile rendered and live images with signatures and admission scope."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable

from capture_live_image_inventory import repository_from_image
from verify_image_inventory_evidence import validate_evidence
from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes, read_bounded_text


ROOT = Path(__file__).resolve().parents[1]
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
EXCEPTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
IMAGE_KEYS = {"image", "imagename", "defaultimage", "sidecarimage"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(read_bounded_bytes(path)).hexdigest()


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(read_bounded_text(path, encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {path}: {exc}") from exc


def normalized_source(image: str) -> str:
    value = image.strip()
    repository = repository_from_image(value)
    if "@" in value:
        return f"{repository}@{value.rsplit('@', 1)[1].lower()}"
    slash = value.rfind("/")
    colon = value.rfind(":")
    tag = value[colon + 1 :] if colon > slash else "latest"
    return f"{repository}:{tag}"


def registry_of(image: str) -> str:
    return repository_from_image(image).split("/", 1)[0]


def manifest_documents(path: Path) -> Iterable[Any]:
    text = read_bounded_text(path, encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        loaded = json.loads(stripped)
        return loaded if isinstance(loaded, list) else [loaded]
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError(
            "PyYAML is required to inspect rendered YAML; install the Ansible/PyYAML runtime"
        ) from exc
    return list(yaml.safe_load_all(text))


def image_values(value: Any, *, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}"
            key_normalized = str(key).lower()
            if (
                isinstance(child, str)
                and (key_normalized in IMAGE_KEYS or key_normalized.endswith("imagename"))
                and child.strip()
            ):
                yield child_path, child.strip()
            yield from image_values(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from image_values(child, path=f"{path}/{index}")


def rendered_images(summary_path: Path, profile: str, root: Path) -> list[dict[str, str]]:
    summary = load_json(summary_path, "rendered schema summary")
    if not isinstance(summary, dict):
        raise ValueError("rendered schema summary must be an object")
    if summary.get("failures"):
        raise ValueError("rendered schema summary contains failures")
    if summary.get("skipped"):
        raise ValueError("rendered schema summary contains skipped applications")
    records = summary.get("rendered")
    if not isinstance(records, list) or not records:
        raise ValueError("rendered schema summary contains no applications")

    results: list[dict[str, str]] = []
    profile_records = 0
    for record in records:
        if not isinstance(record, dict) or record.get("profile") != profile:
            continue
        profile_records += 1
        manifest_value = record.get("manifest")
        application = str(record.get("application", ""))
        if not isinstance(manifest_value, str) or not manifest_value:
            raise ValueError("rendered application record is missing manifest")
        manifest = Path(manifest_value)
        if not manifest.is_absolute():
            manifest = root / manifest
        if not manifest.is_file():
            raise ValueError(f"rendered manifest is missing: {manifest}")
        for document_index, document in enumerate(manifest_documents(manifest)):
            if not isinstance(document, dict):
                continue
            metadata = document.get("metadata", {}) if isinstance(document.get("metadata"), dict) else {}
            object_ref = "/".join(
                part
                for part in (
                    str(document.get("kind", "Unknown")),
                    str(metadata.get("namespace", "default")),
                    str(metadata.get("name", "unnamed")),
                )
            )
            for field, image in image_values(document):
                results.append(
                    {
                        "application": application,
                        "object": object_ref,
                        "document": str(document_index),
                        "field": field,
                        "sourceImage": normalized_source(image),
                    }
                )
    if profile_records == 0:
        raise ValueError(f"rendered schema summary has no records for profile {profile}")
    if not results:
        raise ValueError(f"rendered profile {profile} contains no discoverable image fields")
    results.sort(key=lambda item: tuple(item.values()))
    return results


def signature_images(path: Path) -> set[str]:
    report = load_json(path, "Cosign signature report")
    if not isinstance(report, dict) or report.get("schemaVersion") != 1:
        raise ValueError("Cosign signature report must use schemaVersion 1")
    entries = report.get("verifiedImages")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Cosign signature report contains no verified images")
    images: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("verified") is not True:
            raise ValueError("Cosign signature report contains an unverified entry")
        image = normalized_source(str(entry.get("image", "")))
        if not DIGEST_RE.search(image):
            raise ValueError(f"Cosign signature entry is not digest-pinned: {image}")
        images.add(image)
    return images


def validate_trivy_report(path: Path) -> dict[str, int]:
    report = load_json(path, "exception vulnerability report")
    if not isinstance(report, dict) or not isinstance(report.get("Results"), list):
        raise ValueError(f"vulnerability report is not a Trivy JSON report: {path}")
    counts = {"HIGH": 0, "CRITICAL": 0}
    for result in report["Results"]:
        if not isinstance(result, dict):
            continue
        for vulnerability in result.get("Vulnerabilities", []) or []:
            if not isinstance(vulnerability, dict):
                continue
            severity = str(vulnerability.get("Severity", "")).upper()
            if severity in counts:
                counts[severity] += 1
    return counts


def exception_records(path: Path | None, root: Path, now: datetime) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    document = load_json(path, "image inventory exceptions")
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise ValueError("image inventory exceptions must use schemaVersion 1")
    entries = document.get("exceptions")
    if not isinstance(entries, list):
        raise ValueError("image inventory exceptions must contain an exceptions array")
    result: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"exception {index} must be an object")
        exception_id = str(entry.get("id", "")).strip()
        image = normalized_source(str(entry.get("image", "")))
        owner = str(entry.get("owner", "")).strip()
        approver = str(entry.get("approvedBy", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        ticket = str(entry.get("ticket", "")).strip()
        if not EXCEPTION_ID_RE.fullmatch(exception_id):
            raise ValueError(f"exception {index} has an invalid id")
        if not DIGEST_RE.search(image):
            raise ValueError(f"exception {exception_id} image must be digest-pinned")
        if image in result:
            raise ValueError(f"duplicate exception for image: {image}")
        if not owner or not approver or not reason or not ticket:
            raise ValueError(f"exception {exception_id} lacks owner, approver, reason, or ticket")
        if owner.casefold() == approver.casefold():
            raise ValueError(f"exception {exception_id} owner and approver must differ")
        try:
            created = date.fromisoformat(str(entry.get("createdAt", "")))
            expires = date.fromisoformat(str(entry.get("expiresAt", "")))
        except ValueError as exc:
            raise ValueError(f"exception {exception_id} dates must use YYYY-MM-DD") from exc
        if expires < now.date():
            raise ValueError(f"exception {exception_id} is expired")
        if expires <= created or expires > created + timedelta(days=90):
            raise ValueError(f"exception {exception_id} must expire within 90 days of creation")
        report_value = str(entry.get("vulnerabilityReport", "")).strip()
        report_hash = str(entry.get("vulnerabilityReportSha256", "")).strip().lower()
        report_path = Path(report_value)
        if report_path.is_absolute() or ".." in report_path.parts:
            raise ValueError(f"exception {exception_id} vulnerability report path is unsafe")
        if report_path.parts[:2] != ("private", "supply-chain"):
            raise ValueError(
                f"exception {exception_id} vulnerability report must be below private/supply-chain"
            )
        absolute_report = root / report_path
        if not absolute_report.is_file() or sha256_file(absolute_report) != report_hash:
            raise ValueError(f"exception {exception_id} vulnerability report hash does not match")
        vulnerability_counts = validate_trivy_report(absolute_report)
        source_image = str(entry.get("sourceImage", "")).strip()
        result[image] = {
            "id": exception_id,
            "owner": owner,
            "approvedBy": approver,
            "reason": reason,
            "ticket": ticket,
            "createdAt": created.isoformat(),
            "expiresAt": expires.isoformat(),
            "sourceImage": normalized_source(source_image) if source_image else None,
            "vulnerabilityReport": report_value,
            "vulnerabilityReportSha256": report_hash,
            "highVulnerabilities": vulnerability_counts["HIGH"],
            "criticalVulnerabilities": vulnerability_counts["CRITICAL"],
        }
    return result


def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    now = datetime.now(timezone.utc)
    rendered = rendered_images(args.rendered_summary, args.profile, root)
    live = load_json(args.live_inventory, "live image inventory")
    if not isinstance(live, dict) or live.get("schemaVersion") != 1:
        raise ValueError("live image inventory must use schemaVersion 1")
    if live.get("unresolved"):
        raise ValueError("live image inventory contains unresolved containers")
    containers = live.get("containers")
    if not isinstance(containers, list) or not containers:
        raise ValueError("live image inventory contains no containers")

    live_resolutions: dict[str, set[str]] = {}
    for container in containers:
        if not isinstance(container, dict):
            raise ValueError("live image inventory contains an invalid container entry")
        source = normalized_source(str(container.get("specImage", "")))
        digest = normalized_source(str(container.get("digestImage", "")))
        if not DIGEST_RE.search(digest):
            raise ValueError(f"live image is not digest-pinned: {digest}")
        live_resolutions.setdefault(source, set()).add(digest)
    ambiguous = sorted(source for source, digests in live_resolutions.items() if len(digests) != 1)
    if ambiguous:
        raise ValueError("live tags resolved to multiple digests: " + ", ".join(ambiguous))

    signatures = signature_images(args.signature_report)
    exceptions = exception_records(args.exceptions, root, now)
    exception_by_source = {
        item["sourceImage"]: digest
        for digest, item in exceptions.items()
        if item.get("sourceImage")
    }
    unresolved_rendered: list[str] = []
    rendered_digests: set[str] = set()
    for item in rendered:
        source = item["sourceImage"]
        if DIGEST_RE.search(source):
            rendered_digests.add(source)
        elif source in live_resolutions:
            rendered_digests.update(live_resolutions[source])
        elif source in exception_by_source:
            rendered_digests.add(exception_by_source[source])
        else:
            unresolved_rendered.append(source)
    if unresolved_rendered:
        raise ValueError(
            "rendered images were neither observed live nor resolved by exception: "
            + ", ".join(sorted(set(unresolved_rendered)))
        )

    live_digests = {next(iter(values)) for values in live_resolutions.values()}
    required = sorted(rendered_digests | live_digests)
    expected_registry = args.expected_registry.strip().lower()
    if not expected_registry or "/" in expected_registry or "://" in expected_registry:
        raise ValueError("expected registry must be a registry host with optional port")

    images: list[dict[str, Any]] = []
    uncovered: list[str] = []
    for image in required:
        private = registry_of(image) == expected_registry
        signed = image in signatures
        exception = exceptions.get(image)
        if private and (not signed or exception is not None):
            uncovered.append(image)
        elif not private and exception is None:
            uncovered.append(image)
        images.append(
            {
                "image": image,
                "rendered": image in rendered_digests,
                "live": image in live_digests,
                "signatureVerified": signed,
                "admissionEnforced": private,
                "exception": exception,
            }
        )
    if uncovered:
        raise ValueError("image coverage is incomplete: " + ", ".join(uncovered))

    commit = args.commit.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit must be a lowercase 40-character Git SHA")
    document: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "profile": args.profile,
        "commit": commit,
        "expectedRegistry": expected_registry,
        "result": "passed",
        "inputs": {
            "renderedSummarySha256": sha256_file(args.rendered_summary),
            "liveInventorySha256": sha256_file(args.live_inventory),
            "signatureReportSha256": sha256_file(args.signature_report),
            "exceptionsSha256": sha256_file(args.exceptions) if args.exceptions else None,
        },
        "rendered": {
            "references": len(rendered),
            "uniqueImages": len(rendered_digests),
            "unresolved": 0,
        },
        "live": {
            "containers": len(containers),
            "uniqueImages": len(live_digests),
            "unresolved": 0,
            "clusterUid": str(live.get("clusterUid", "")),
            "capturedAt": str(live.get("capturedAt", "")),
        },
        "images": images,
        "summary": {
            "images": len(images),
            "privateRegistryImages": sum(1 for item in images if item["admissionEnforced"]),
            "signatureVerifiedImages": sum(1 for item in images if item["signatureVerified"]),
            "exceptions": sum(1 for item in images if item["exception"] is not None),
            "uncovered": 0,
        },
    }
    validate_evidence(
        document,
        now=now,
        max_age_hours=1,
        expected_profile=args.profile,
        expected_commit=commit,
    )
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered-summary", required=True, type=Path)
    parser.add_argument("--live-inventory", required=True, type=Path)
    parser.add_argument("--signature-report", required=True, type=Path)
    parser.add_argument("--exceptions", type=Path)
    parser.add_argument("--expected-registry", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        document = reconcile(args)
        atomic_write_text(args.output, json.dumps(document, indent=2) + "\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Image inventory reconciliation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Image inventory reconciliation passed: "
        f"images={document['summary']['images']} "
        f"signed={document['summary']['signatureVerifiedImages']} "
        f"exceptions={document['summary']['exceptions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
