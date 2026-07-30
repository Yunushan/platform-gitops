#!/usr/bin/env python3
"""Require a GitHub-verified commit and annotated tag for a release ref."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from atomic_file import atomic_write_text
from http_transport import (
    HttpTransportPolicyError,
    http_timeout_seconds,
    read_bounded_response,
)


SEMVER_TAG_RE = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ReleaseRefError(ValueError):
    """Raised when a release ref is not immutable and independently verified."""


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseRefError(f"{label} must be a JSON object")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseRefError(f"{label} must be a non-empty string")
    return value.strip()


def require_verified_signature(document: dict[str, Any], label: str) -> None:
    verification = require_object(document.get("verification"), f"{label}.verification")
    if verification.get("verified") is not True:
        reason = str(verification.get("reason") or "unknown")
        raise ReleaseRefError(f"{label} signature is not GitHub-verified: {reason}")
    if verification.get("reason") != "valid":
        raise ReleaseRefError(f"{label} signature verification reason must be valid")
    for field in ("signature", "payload", "verified_at"):
        require_string(verification.get(field), f"{label}.verification.{field}")


def validate_release_ref(
    *,
    repository: str,
    tag: str,
    expected_sha: str,
    ref_document: Any,
    tag_document: Any,
    commit_document: Any,
) -> dict[str, str]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ReleaseRefError("repository must use OWNER/REPOSITORY syntax")
    if not SEMVER_TAG_RE.fullmatch(tag):
        raise ReleaseRefError("tag must be a stable semantic version such as v1.2.3")
    expected_sha = expected_sha.lower()
    if not SHA_RE.fullmatch(expected_sha):
        raise ReleaseRefError("expected SHA must be a 40-character lowercase Git commit SHA")

    ref = require_object(ref_document, "tag ref")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise ReleaseRefError("GitHub tag ref does not match the requested release tag")
    ref_target = require_object(ref.get("object"), "tag ref object")
    if ref_target.get("type") != "tag":
        raise ReleaseRefError("release tag must be annotated; lightweight tags are rejected")
    tag_object_sha = require_string(ref_target.get("sha"), "tag object SHA").lower()
    if not SHA_RE.fullmatch(tag_object_sha):
        raise ReleaseRefError("tag object SHA is invalid")

    tag_object = require_object(tag_document, "annotated tag")
    if tag_object.get("sha") != tag_object_sha:
        raise ReleaseRefError("annotated tag object does not match the tag ref")
    if tag_object.get("tag") != tag:
        raise ReleaseRefError("annotated tag name does not match the release tag")
    tagged_object = require_object(tag_object.get("object"), "annotated tag target")
    if tagged_object.get("type") != "commit":
        raise ReleaseRefError("annotated release tag must point directly to a commit")
    if str(tagged_object.get("sha") or "").lower() != expected_sha:
        raise ReleaseRefError("annotated release tag does not point to GITHUB_SHA")
    require_verified_signature(tag_object, "annotated tag")

    commit = require_object(commit_document, "release commit")
    if str(commit.get("sha") or "").lower() != expected_sha:
        raise ReleaseRefError("GitHub commit object does not match GITHUB_SHA")
    require_verified_signature(commit, "release commit")

    return {
        "repository": repository,
        "tag": tag,
        "tag_object_sha": tag_object_sha,
        "commit_sha": expected_sha,
    }


def build_release_evidence(
    summary: dict[str, str],
    *,
    ref_document: Any,
    tag_document: Any,
    commit_document: Any,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": summary["repository"],
        "tag": summary["tag"],
        "commit": summary["commit_sha"],
        "tagObjectSha": summary["tag_object_sha"],
        "result": "passed",
        "inputSha256": {
            "tagRef": canonical_sha256(ref_document),
            "annotatedTag": canonical_sha256(tag_document),
            "commit": canonical_sha256(commit_document),
        },
        "controls": {
            "stableSemanticVersion": "passed",
            "annotatedTag": "passed",
            "tagCommitBinding": "passed",
            "signedTag": "passed",
            "signedCommit": "passed",
        },
    }


def api_get(api_url: str, path: str, token: str) -> dict[str, Any]:
    request = Request(
        f"{api_url.rstrip('/')}/{path.lstrip('/')}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "platform-gitops-release-verifier",
        },
    )
    try:
        timeout = http_timeout_seconds()
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(read_bounded_response(response))
    except HTTPError as exc:
        raise ReleaseRefError(f"GitHub API request failed with HTTP {exc.code}: {path}") from exc
    except URLError as exc:
        raise ReleaseRefError(f"GitHub API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseRefError(f"GitHub API returned invalid JSON: {path}") from exc
    except HttpTransportPolicyError as exc:
        raise ReleaseRefError(f"GitHub API response rejected for {path}: {exc}") from exc
    return require_object(payload, f"GitHub API response for {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            os.environ.get(
                "GITHUB_RELEASE_EVIDENCE_OUTPUT",
                "rendered/governance/github-release-evidence.json",
            )
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GitHub release verification requires GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        repository_path = quote(args.repository, safe="/")
        tag_path = quote(args.tag, safe="")
        ref_document = api_get(
            args.api_url,
            f"repos/{repository_path}/git/ref/tags/{tag_path}",
            token,
        )
        ref_target = require_object(ref_document.get("object"), "tag ref object")
        tag_object_sha = require_string(ref_target.get("sha"), "tag object SHA")
        tag_document = api_get(
            args.api_url,
            f"repos/{repository_path}/git/tags/{quote(tag_object_sha, safe='')}",
            token,
        )
        commit_document = api_get(
            args.api_url,
            f"repos/{repository_path}/git/commits/{quote(args.sha, safe='')}",
            token,
        )
        summary = validate_release_ref(
            repository=args.repository,
            tag=args.tag,
            expected_sha=args.sha,
            ref_document=ref_document,
            tag_document=tag_document,
            commit_document=commit_document,
        )
        evidence = build_release_evidence(
            summary,
            ref_document=ref_document,
            tag_document=tag_document,
            commit_document=commit_document,
        )
    except ReleaseRefError as exc:
        print(f"GitHub release ref verification failed: {exc}", file=sys.stderr)
        return 1
    atomic_write_text(args.output, json.dumps(evidence, indent=2) + "\n")
    print(
        "GitHub release ref verified: "
        f"repository={summary['repository']} tag={summary['tag']} "
        f"commit={summary['commit_sha']} tag_object={summary['tag_object_sha']} "
        f"evidence={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
