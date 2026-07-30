#!/usr/bin/env python3
"""Require commit-bound live, governance, and signed-release proof for 100/100."""

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

import verify_production_evidence as production_evidence


ROOT = Path(__file__).resolve().parents[1]
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SEMVER_TAG_RE = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
CHECKSUM_RE = re.compile(r"^(?P<sha256>[0-9a-f]{64})\s+\*?(?P<name>\S.+|\S)$")
GITHUB_ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

PRODUCTION_WEIGHT = 80
GOVERNANCE_WEIGHT = 10
RELEASE_WEIGHT = 10
MAXIMUM_SCORE = PRODUCTION_WEIGHT + GOVERNANCE_WEIGHT + RELEASE_WEIGHT

GOVERNANCE_INPUTS = {
    "repository",
    "codeowners",
    "collaborators",
    "reviewerMembers",
    "privateVulnerabilityReporting",
    "codeqlDefaultSetup",
    "commit",
    "protection",
    "rulesets",
    "environment",
    "environmentPolicies",
    "workflowPermissions",
    "actionsPermissions",
}
GOVERNANCE_CONTROLS = {
    "branchProtection",
    "activeCodeowners",
    "independentCollaborators",
    "signedDefaultBranchTip",
    "releaseTagRuleset",
    "independentReleaseReviewConfigured",
    "releaseTagEnvironmentPolicy",
    "readOnlyWorkflowToken",
    "actionShaPinning",
    "securityScanning",
    "privateVulnerabilityReporting",
    "codeqlDefaultSetup",
}
RELEASE_INPUTS = {"tagRef", "annotatedTag", "commit"}
RELEASE_CONTROLS = {
    "stableSemanticVersion",
    "annotatedTag",
    "tagCommitBinding",
    "signedTag",
    "signedCommit",
}
RELEASE_APPROVAL_INPUTS = {
    "repository",
    "workflowRun",
    "environment",
    "reviewHistory",
    "collaborators",
    "rulesets",
    "teamMembers",
}
RELEASE_APPROVAL_CONTROLS = {
    "workflowRunBinding",
    "firstAttemptOnly",
    "requiredReviewerProtection",
    "recordedEnvironmentApproval",
    "authorizedReviewer",
    "independentReviewer",
    "releaseAuthoritySeparation",
}


class ReadinessError(ValueError):
    """Raised when a retained readiness artifact is incomplete or untrusted."""


def load_document(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReadinessError(f"{label} does not exist: {path}") from exc
    except OSError as exc:
        raise ReadinessError(f"{label} cannot be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReadinessError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReadinessError(f"{label} root must be a JSON object")
    return document


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReadinessError(f"{label} must be an ISO-8601 timestamp")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ReadinessError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReadinessError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def require_fresh(
    value: Any,
    *,
    label: str,
    now: datetime,
    max_age_hours: int,
) -> datetime:
    generated_at = parse_timestamp(value, label)
    current = now.astimezone(timezone.utc)
    if generated_at > current + timedelta(minutes=5):
        raise ReadinessError(f"{label} is in the future")
    age = current - generated_at
    if age > timedelta(hours=max_age_hours):
        raise ReadinessError(
            f"{label} is stale ({age.total_seconds() / 3600:.1f} hours; "
            f"maximum is {max_age_hours})"
        )
    return generated_at


def require_passed_map(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ReadinessError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unsupported " + ", ".join(unknown))
        raise ReadinessError(f"{label} entries do not match the contract: {'; '.join(details)}")
    failed = sorted(name for name in expected if str(value.get(name, "")).lower() != "passed")
    if failed:
        raise ReadinessError(f"{label} must all be passed: {', '.join(failed)}")


def require_hash_map(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ReadinessError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unsupported " + ", ".join(unknown))
        raise ReadinessError(f"{label} entries do not match the contract: {'; '.join(details)}")
    invalid = sorted(name for name in expected if not SHA256_RE.fullmatch(str(value.get(name, ""))))
    if invalid:
        raise ReadinessError(f"{label} contains invalid SHA-256 values: {', '.join(invalid)}")


def validate_governance_evidence(
    document: dict[str, Any],
    *,
    expected_repository: str,
    expected_commit: str,
    expected_default_branch: str,
    expected_environment: str,
    expected_tag_pattern: str,
    now: datetime,
    max_age_hours: int,
) -> None:
    if document.get("schemaVersion") != 3:
        raise ReadinessError("GitHub governance evidence schemaVersion must be 3")
    if document.get("result") != "passed":
        raise ReadinessError("GitHub governance evidence result must be passed")
    require_fresh(
        document.get("generatedAt"),
        label="GitHub governance generatedAt",
        now=now,
        max_age_hours=max_age_hours,
    )
    if document.get("repository") != expected_repository:
        raise ReadinessError("GitHub governance repository does not match")
    if document.get("defaultBranch") != expected_default_branch:
        raise ReadinessError("GitHub governance default branch does not match")
    if str(document.get("commit", "")).lower() != expected_commit:
        raise ReadinessError("GitHub governance commit does not match the released commit")
    if document.get("releaseEnvironment") != expected_environment:
        raise ReadinessError("GitHub governance release environment does not match")
    if document.get("tagPattern") != expected_tag_pattern:
        raise ReadinessError("GitHub governance tag pattern does not match")
    require_hash_map(document.get("inputSha256"), GOVERNANCE_INPUTS, "GitHub governance inputSha256")
    require_passed_map(document.get("controls"), GOVERNANCE_CONTROLS, "GitHub governance controls")


def validate_release_approval_evidence(
    document: dict[str, Any],
    *,
    expected_repository: str,
    expected_commit: str,
    expected_environment: str,
    now: datetime,
    max_age_hours: int,
) -> None:
    if document.get("schemaVersion") != 1:
        raise ReadinessError("GitHub release approval evidence schemaVersion must be 1")
    if document.get("result") != "passed":
        raise ReadinessError("GitHub release approval evidence result must be passed")
    require_fresh(
        document.get("generatedAt"),
        label="GitHub release approval generatedAt",
        now=now,
        max_age_hours=max_age_hours,
    )
    if document.get("repository") != expected_repository:
        raise ReadinessError("GitHub release approval repository does not match")
    if str(document.get("commit", "")).lower() != expected_commit:
        raise ReadinessError("GitHub release approval commit does not match the production commit")
    if document.get("releaseEnvironment") != expected_environment:
        raise ReadinessError("GitHub release approval environment does not match")
    if not isinstance(document.get("runId"), int) or document["runId"] <= 0:
        raise ReadinessError("GitHub release approval runId is invalid")
    if not isinstance(document.get("runAttempt"), int) or document["runAttempt"] <= 0:
        raise ReadinessError("GitHub release approval runAttempt is invalid")
    if not SHA256_RE.fullmatch(str(document.get("approvalBindingSha256", ""))):
        raise ReadinessError("GitHub release approval binding SHA-256 is invalid")
    require_hash_map(
        document.get("inputSha256"),
        RELEASE_APPROVAL_INPUTS,
        "GitHub release approval inputSha256",
    )
    require_passed_map(
        document.get("controls"),
        RELEASE_APPROVAL_CONTROLS,
        "GitHub release approval controls",
    )


def validate_release_evidence(
    document: dict[str, Any],
    *,
    expected_repository: str,
    expected_tag: str,
    expected_commit: str,
    now: datetime,
    max_age_hours: int,
) -> None:
    if document.get("schemaVersion") != 1:
        raise ReadinessError("GitHub release evidence schemaVersion must be 1")
    if document.get("result") != "passed":
        raise ReadinessError("GitHub release evidence result must be passed")
    require_fresh(
        document.get("generatedAt"),
        label="GitHub release generatedAt",
        now=now,
        max_age_hours=max_age_hours,
    )
    if document.get("repository") != expected_repository:
        raise ReadinessError("GitHub release repository does not match")
    if document.get("tag") != expected_tag:
        raise ReadinessError("GitHub release tag does not match")
    if str(document.get("commit", "")).lower() != expected_commit:
        raise ReadinessError("GitHub release commit does not match the production commit")
    if not COMMIT_RE.fullmatch(str(document.get("tagObjectSha", "")).lower()):
        raise ReadinessError("GitHub release tagObjectSha is invalid")
    require_hash_map(document.get("inputSha256"), RELEASE_INPUTS, "GitHub release inputSha256")
    require_passed_map(document.get("controls"), RELEASE_CONTROLS, "GitHub release controls")


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReadinessError(f"release checksum manifest cannot be read: {path}: {exc}") from exc
    entries: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        match = CHECKSUM_RE.fullmatch(line.strip())
        if not match:
            raise ReadinessError(f"release checksum manifest line {line_number} is invalid")
        name = match.group("name")
        if name in entries:
            raise ReadinessError(f"release checksum manifest contains duplicate entry: {name}")
        entries[name] = match.group("sha256")
    if not entries:
        raise ReadinessError("release checksum manifest is empty")
    return entries


def verify_release_bundle(
    *,
    checksums_path: Path,
    bundle_path: Path,
    governance_path: Path,
    release_path: Path,
    release_approval_path: Path,
    repository: str,
    tag: str,
    cosign_bin: str,
    runner: Any = subprocess.run,
) -> dict[str, str]:
    if not bundle_path.is_file():
        raise ReadinessError(f"release Sigstore bundle does not exist: {bundle_path}")
    entries = parse_checksum_manifest(checksums_path)
    artifact = f"platform-gitops-{tag}"
    required_names = {
        f"{artifact}.tar.gz",
        f"{artifact}.spdx.json",
        f"{artifact}.cyclonedx.json",
        f"{artifact}.github-governance.json",
        f"{artifact}.github-release.json",
        f"{artifact}.github-release-approval.json",
    }
    missing = sorted(required_names - set(entries))
    if missing:
        raise ReadinessError(
            "release checksum manifest is missing required artifacts: " + ", ".join(missing)
        )
    expected_hashes = {
        f"{artifact}.github-governance.json": artifact_sha256(governance_path),
        f"{artifact}.github-release.json": artifact_sha256(release_path),
        f"{artifact}.github-release-approval.json": artifact_sha256(release_approval_path),
    }
    mismatched = sorted(name for name, digest in expected_hashes.items() if entries[name] != digest)
    if mismatched:
        raise ReadinessError(
            "release evidence does not match signed checksums: " + ", ".join(mismatched)
        )

    cosign_path = Path(cosign_bin)
    executable = str(cosign_path.resolve()) if cosign_path.is_file() else shutil.which(cosign_bin)
    if not executable:
        raise ReadinessError(
            "Cosign is required to verify the release checksum bundle; set COSIGN_BIN"
        )
    identity = f"https://github.com/{repository}/.github/workflows/release.yml@refs/tags/{tag}"
    try:
        result = runner(
            [
                str(executable),
                "verify-blob",
                "--bundle",
                str(bundle_path),
                "--certificate-identity",
                identity,
                "--certificate-oidc-issuer",
                GITHUB_ACTIONS_OIDC_ISSUER,
                str(checksums_path),
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadinessError(f"Cosign release checksum verification could not run: {exc}") from exc
    if result.returncode != 0:
        raise ReadinessError("Cosign rejected the release checksum Sigstore bundle")
    return {
        "checksums": artifact_sha256(checksums_path),
        "sigstoreBundle": artifact_sha256(bundle_path),
    }


def evaluate_readiness(
    *,
    production_document: dict[str, Any] | None,
    governance_document: dict[str, Any] | None,
    release_document: dict[str, Any] | None,
    release_approval_document: dict[str, Any] | None,
    root: Path,
    now: datetime,
    expected_profile: str,
    expected_repository: str,
    expected_commit: str,
    expected_tag: str,
    expected_default_branch: str = "main",
    expected_environment: str = "production-release",
    expected_tag_pattern: str = "refs/tags/v*.*.*",
    max_production_age_days: int = 7,
    max_governance_age_hours: int = 24,
    max_release_age_hours: int = 24,
) -> tuple[dict[str, Any], list[str]]:
    categories = [
        {"name": "livePlatformAcceptance", "weight": PRODUCTION_WEIGHT, "earned": 0, "result": "failed"},
        {"name": "githubGovernance", "weight": GOVERNANCE_WEIGHT, "earned": 0, "result": "failed"},
        {"name": "signedReleaseProvenance", "weight": RELEASE_WEIGHT, "earned": 0, "result": "failed"},
    ]
    diagnostics: list[str] = []

    if production_document is None:
        diagnostics.append("live platform acceptance evidence is missing or unreadable")
    else:
        try:
            production_evidence.validate_evidence(
                production_document,
                root=root,
                now=now,
                max_age_days=max_production_age_days,
                expected_profile=expected_profile,
                expected_commit=expected_commit,
            )
        except (production_evidence.EvidenceError, OSError, json.JSONDecodeError) as exc:
            diagnostics.append(f"live platform acceptance evidence failed: {exc}")
        else:
            categories[0].update(earned=PRODUCTION_WEIGHT, result="passed")

    if governance_document is None:
        diagnostics.append("GitHub governance evidence is missing or unreadable")
    else:
        try:
            validate_governance_evidence(
                governance_document,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                expected_default_branch=expected_default_branch,
                expected_environment=expected_environment,
                expected_tag_pattern=expected_tag_pattern,
                now=now,
                max_age_hours=max_governance_age_hours,
            )
        except ReadinessError as exc:
            diagnostics.append(f"GitHub governance evidence failed: {exc}")
        else:
            categories[1].update(earned=GOVERNANCE_WEIGHT, result="passed")

    if release_document is None or release_approval_document is None:
        if release_document is None:
            diagnostics.append("signed release evidence is missing or unreadable")
        if release_approval_document is None:
            diagnostics.append("independent release approval evidence is missing or unreadable")
    else:
        try:
            validate_release_evidence(
                release_document,
                expected_repository=expected_repository,
                expected_tag=expected_tag,
                expected_commit=expected_commit,
                now=now,
                max_age_hours=max_release_age_hours,
            )
            validate_release_approval_evidence(
                release_approval_document,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                expected_environment=expected_environment,
                now=now,
                max_age_hours=max_release_age_hours,
            )
        except ReadinessError as exc:
            diagnostics.append(f"signed release and approval evidence failed: {exc}")
        else:
            categories[2].update(earned=RELEASE_WEIGHT, result="passed")

    score = sum(int(category["earned"]) for category in categories)
    report = {
        "schemaVersion": 1,
        "generatedAt": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "result": "passed" if score == MAXIMUM_SCORE and not diagnostics else "failed",
        "score": score,
        "maximumScore": MAXIMUM_SCORE,
        "expected": {
            "profile": expected_profile,
            "repository": expected_repository,
            "commit": expected_commit,
            "tag": expected_tag,
        },
        "categories": categories,
    }
    return report, diagnostics


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--production-evidence",
        type=Path,
        default=os.environ.get("PLATFORM_PRODUCTION_EVIDENCE_FILE") or None,
    )
    result.add_argument(
        "--governance-evidence",
        type=Path,
        default=os.environ.get("GITHUB_GOVERNANCE_EVIDENCE_FILE") or None,
    )
    result.add_argument(
        "--release-evidence",
        type=Path,
        default=os.environ.get("GITHUB_RELEASE_EVIDENCE_FILE") or None,
    )
    result.add_argument(
        "--release-approval-evidence",
        type=Path,
        default=os.environ.get("GITHUB_RELEASE_APPROVAL_EVIDENCE_FILE") or None,
    )
    result.add_argument(
        "--release-checksums",
        type=Path,
        default=os.environ.get("GITHUB_RELEASE_CHECKSUMS_FILE") or None,
    )
    result.add_argument(
        "--release-checksum-bundle",
        type=Path,
        default=os.environ.get("GITHUB_RELEASE_CHECKSUM_BUNDLE_FILE") or None,
    )
    result.add_argument("--profile", default=os.environ.get("PLATFORM_PROFILE", ""))
    result.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    result.add_argument(
        "--commit",
        default=os.environ.get("PLATFORM_EXPECTED_COMMIT") or os.environ.get("GITHUB_SHA", ""),
    )
    result.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""))
    result.add_argument("--default-branch", default=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"))
    result.add_argument("--release-environment", default="production-release")
    result.add_argument("--tag-ref-pattern", default="refs/tags/v*.*.*")
    result.add_argument("--max-production-age-days", type=int, default=7)
    result.add_argument("--max-governance-age-hours", type=int, default=24)
    result.add_argument("--max-release-age-hours", type=int, default=24)
    result.add_argument(
        "--output",
        type=Path,
        default=(
            Path(os.environ["PLATFORM_READINESS_SCORE_OUTPUT"])
            if os.environ.get("PLATFORM_READINESS_SCORE_OUTPUT")
            else None
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    argument_problems: list[str] = []
    if not args.production_evidence:
        argument_problems.append("--production-evidence or PLATFORM_PRODUCTION_EVIDENCE_FILE is required")
    if not args.governance_evidence:
        argument_problems.append("--governance-evidence or GITHUB_GOVERNANCE_EVIDENCE_FILE is required")
    if not args.release_evidence:
        argument_problems.append("--release-evidence or GITHUB_RELEASE_EVIDENCE_FILE is required")
    if not args.release_approval_evidence:
        argument_problems.append(
            "--release-approval-evidence or GITHUB_RELEASE_APPROVAL_EVIDENCE_FILE is required"
        )
    if not args.release_checksums:
        argument_problems.append("--release-checksums or GITHUB_RELEASE_CHECKSUMS_FILE is required")
    if not args.release_checksum_bundle:
        argument_problems.append(
            "--release-checksum-bundle or GITHUB_RELEASE_CHECKSUM_BUNDLE_FILE is required"
        )
    if not args.profile.strip():
        argument_problems.append("--profile or PLATFORM_PROFILE is required")
    if not REPOSITORY_RE.fullmatch(args.repository):
        argument_problems.append("--repository must use OWNER/REPOSITORY syntax")
    args.commit = args.commit.lower()
    if not COMMIT_RE.fullmatch(args.commit):
        argument_problems.append("--commit must be a 40-character lowercase Git commit SHA")
    if not SEMVER_TAG_RE.fullmatch(args.tag):
        argument_problems.append("--tag must be a stable semantic version such as v1.2.3")
    for name, value in (
        ("--max-production-age-days", args.max_production_age_days),
        ("--max-governance-age-hours", args.max_governance_age_hours),
        ("--max-release-age-hours", args.max_release_age_hours),
    ):
        if value <= 0:
            argument_problems.append(f"{name} must be greater than zero")
    if argument_problems:
        print("Production readiness score configuration failed:", file=sys.stderr)
        for problem in argument_problems:
            print(f" - {problem}", file=sys.stderr)
        return 2

    documents: dict[str, dict[str, Any] | None] = {}
    evidence_hashes: dict[str, str] = {}
    load_problems: list[str] = []
    for name, path, label in (
        ("production", args.production_evidence, "production evidence"),
        ("governance", args.governance_evidence, "GitHub governance evidence"),
        ("release", args.release_evidence, "GitHub release evidence"),
        (
            "releaseApproval",
            args.release_approval_evidence,
            "GitHub release approval evidence",
        ),
    ):
        try:
            documents[name] = load_document(path, label)
            evidence_hashes[name] = artifact_sha256(path)
        except (ReadinessError, OSError) as exc:
            documents[name] = None
            load_problems.append(str(exc))

    if (
        documents.get("governance") is not None
        and documents.get("release") is not None
        and documents.get("releaseApproval") is not None
    ):
        try:
            release_bundle_hashes = verify_release_bundle(
                checksums_path=args.release_checksums,
                bundle_path=args.release_checksum_bundle,
                governance_path=args.governance_evidence,
                release_path=args.release_evidence,
                release_approval_path=args.release_approval_evidence,
                repository=args.repository,
                tag=args.tag,
                cosign_bin=os.environ.get("COSIGN_BIN", "cosign"),
            )
        except (ReadinessError, OSError) as exc:
            documents["governance"] = None
            documents["release"] = None
            documents["releaseApproval"] = None
            load_problems.append(f"signed release bundle failed: {exc}")
        else:
            evidence_hashes.update(release_bundle_hashes)

    report, diagnostics = evaluate_readiness(
        production_document=documents["production"],
        governance_document=documents["governance"],
        release_document=documents["release"],
        release_approval_document=documents["releaseApproval"],
        root=ROOT,
        now=datetime.now(timezone.utc),
        expected_profile=args.profile.strip(),
        expected_repository=args.repository,
        expected_commit=args.commit,
        expected_tag=args.tag,
        expected_default_branch=args.default_branch,
        expected_environment=args.release_environment,
        expected_tag_pattern=args.tag_ref_pattern,
        max_production_age_days=args.max_production_age_days,
        max_governance_age_hours=args.max_governance_age_hours,
        max_release_age_hours=args.max_release_age_hours,
    )

    report["evidenceSha256"] = evidence_hashes

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(
        f"Production readiness score: {report['score']}/{report['maximumScore']} "
        f"result={report['result']}"
    )
    all_problems = load_problems + diagnostics
    if all_problems:
        for problem in all_problems:
            print(f" - {problem}", file=sys.stderr)
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
