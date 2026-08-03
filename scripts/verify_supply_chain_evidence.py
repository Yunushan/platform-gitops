#!/usr/bin/env python3
"""Validate SBOM, Scorecard, and Cosign evidence produced by release gates."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

from bounded_file import read_bounded_text
from strict_json import loads_strict_json


DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def load_json(path: Path, label: str) -> Any:
    try:
        return loads_strict_json(read_bounded_text(path))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def validate_sbom(path: Path) -> dict[str, Any]:
    document = load_json(path, "SBOM")
    if not isinstance(document, dict):
        raise ValueError("SBOM root must be a JSON object")
    version = require_string(document.get("spdxVersion"), "SBOM spdxVersion")
    if not version.startswith("SPDX-"):
        raise ValueError(f"SBOM spdxVersion is unsupported: {version}")
    if document.get("dataLicense") != "CC0-1.0":
        raise ValueError("SBOM dataLicense must be CC0-1.0")
    if document.get("SPDXID") != "SPDXRef-DOCUMENT":
        raise ValueError("SBOM SPDXID must be SPDXRef-DOCUMENT")
    require_string(document.get("name"), "SBOM name")
    require_string(document.get("documentNamespace"), "SBOM documentNamespace")

    creation = document.get("creationInfo")
    if not isinstance(creation, dict):
        raise ValueError("SBOM creationInfo must be an object")
    require_string(creation.get("created"), "SBOM creationInfo.created")
    creators = creation.get("creators")
    if not isinstance(creators, list) or not any(
        isinstance(item, str) and item.strip() for item in creators
    ):
        raise ValueError("SBOM creationInfo.creators must contain at least one creator")

    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ValueError("SBOM must contain at least one package")
    package_ids: set[str] = set()
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise ValueError(f"SBOM package {index} must be an object")
        package_id = require_string(package.get("SPDXID"), f"SBOM package {index} SPDXID")
        if package_id in package_ids:
            raise ValueError(f"SBOM contains duplicate package SPDXID: {package_id}")
        package_ids.add(package_id)
        require_string(package.get("name"), f"SBOM package {index} name")

    return {"spdx_version": version, "packages": len(packages)}


def validate_scorecard(path: Path, minimum_score: float) -> dict[str, Any]:
    report = load_json(path, "OpenSSF Scorecard report")
    if not isinstance(report, dict):
        raise ValueError("OpenSSF Scorecard report root must be a JSON object")
    score = report.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        raise ValueError("OpenSSF Scorecard report score must be numeric")
    if score < 0 or score > 10:
        raise ValueError("OpenSSF Scorecard report score must be between 0 and 10")
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("OpenSSF Scorecard report must contain checks")
    if score < minimum_score:
        raise ValueError(
            f"OpenSSF Scorecard score {score:.1f} is below required {minimum_score:.1f}"
        )
    return {"score": score, "checks": len(checks)}


def validate_signature_report(path: Path) -> dict[str, Any]:
    report = load_json(path, "Cosign signature report")
    if not isinstance(report, dict):
        raise ValueError("Cosign signature report root must be a JSON object")
    if report.get("schemaVersion") != 1:
        raise ValueError("Cosign signature report schemaVersion must be 1")
    images = report.get("verifiedImages")
    if not isinstance(images, list) or not images:
        raise ValueError("Cosign signature report must contain verifiedImages")
    seen: set[str] = set()
    for index, item in enumerate(images):
        if not isinstance(item, dict):
            raise ValueError(f"Cosign verified image {index} must be an object")
        image = require_string(item.get("image"), f"Cosign verified image {index} image")
        if not DIGEST_RE.search(image):
            raise ValueError(f"Cosign image must be pinned by sha256 digest: {image}")
        if image in seen:
            raise ValueError(f"Cosign signature report contains duplicate image: {image}")
        seen.add(image)
        require_string(item.get("key"), f"Cosign verified image {index} key")
        if item.get("verified") is not True:
            raise ValueError(f"Cosign image is not marked verified: {image}")
    return {"verified_images": len(images)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--sbom", required=True, type=Path)
    result.add_argument("--scorecard", type=Path)
    result.add_argument("--signature-report", type=Path)
    result.add_argument("--minimum-score", type=float, default=7.0)
    result.add_argument("--strict", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if not 0 <= args.minimum_score <= 10:
        print("Supply-chain evidence validation failed: minimum score must be 0-10", file=sys.stderr)
        return 2
    try:
        sbom = validate_sbom(args.sbom)
        scorecard = None
        signatures = None
        if args.scorecard:
            scorecard = validate_scorecard(args.scorecard, args.minimum_score)
        elif args.strict:
            raise ValueError("strict evidence requires an OpenSSF Scorecard report")
        if args.signature_report:
            signatures = validate_signature_report(args.signature_report)
        elif args.strict:
            raise ValueError("strict evidence requires a Cosign signature report")
    except ValueError as exc:
        print(f"Supply-chain evidence validation failed: {exc}", file=sys.stderr)
        return 1

    fields = [
        f"sbom={args.sbom}",
        f"spdx={sbom['spdx_version']}",
        f"packages={sbom['packages']}",
    ]
    if scorecard:
        fields.extend((f"scorecard={scorecard['score']:.1f}", f"checks={scorecard['checks']}"))
    if signatures:
        fields.append(f"verified_images={signatures['verified_images']}")
    print("Supply-chain evidence validation passed: " + " ".join(fields))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
