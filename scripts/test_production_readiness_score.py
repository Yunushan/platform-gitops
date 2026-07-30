#!/usr/bin/env python3
"""Behavior-test the fail-closed 100-point production readiness gate."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import test_github_release_ref as release_fixture  # noqa: E402
import test_production_evidence as production_fixture  # noqa: E402
import verify_github_release_ref as release_verifier  # noqa: E402
import verify_production_readiness_score as readiness  # noqa: E402


COMMIT = "a" * 40
REPOSITORY = "example/platform-gitops"
TAG = "v1.2.3"
PROFILE = "premium-3node"


def governance_evidence(now: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "generatedAt": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "repository": REPOSITORY,
        "defaultBranch": "main",
        "commit": COMMIT,
        "releaseEnvironment": "production-release",
        "tagPattern": "refs/tags/v*.*.*",
        "result": "passed",
        "inputSha256": {name: "1" * 64 for name in readiness.GOVERNANCE_INPUTS},
        "controls": {name: "passed" for name in readiness.GOVERNANCE_CONTROLS},
    }


def release_evidence(now: datetime) -> dict[str, object]:
    ref, tag, commit = release_fixture.fixtures()
    tag["object"]["sha"] = COMMIT
    commit["sha"] = COMMIT
    summary = release_verifier.validate_release_ref(
        repository=REPOSITORY,
        tag=TAG,
        expected_sha=COMMIT,
        ref_document=ref,
        tag_document=tag,
        commit_document=commit,
    )
    return release_verifier.build_release_evidence(
        summary,
        ref_document=ref,
        tag_document=tag,
        commit_document=commit,
        generated_at=now - timedelta(minutes=5),
    )


def evaluate(
    production: dict[str, object] | None,
    governance: dict[str, object] | None,
    release: dict[str, object] | None,
    *,
    root: Path,
    now: datetime,
) -> tuple[dict[str, object], list[str]]:
    return readiness.evaluate_readiness(
        production_document=production,
        governance_document=governance,
        release_document=release,
        root=root,
        now=now,
        expected_profile=PROFILE,
        expected_repository=REPOSITORY,
        expected_commit=COMMIT,
        expected_tag=TAG,
    )


def main() -> int:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="platform-readiness-score-") as directory:
        root = Path(directory)
        production = production_fixture.fixture(root, now)
        governance = governance_evidence(now)
        release = release_evidence(now)

        report, diagnostics = evaluate(production, governance, release, root=root, now=now)
        if report["score"] != 100 or report["result"] != "passed" or diagnostics:
            raise AssertionError(f"valid evidence did not earn 100/100: {report} {diagnostics}")
        if sum(category["weight"] for category in report["categories"]) != 100:
            raise AssertionError("readiness category weights do not total 100")

        failed_governance = deepcopy(governance)
        failed_governance["controls"]["securityScanning"] = "failed"
        report, diagnostics = evaluate(
            production,
            failed_governance,
            release,
            root=root,
            now=now,
        )
        if report["score"] != 90 or report["result"] != "failed":
            raise AssertionError("failed governance evidence did not remove its full score")
        if not any("securityScanning" in item for item in diagnostics):
            raise AssertionError("failed governance evidence did not explain the rejected control")

        wrong_release = deepcopy(release)
        wrong_release["commit"] = "b" * 40
        report, _ = evaluate(production, governance, wrong_release, root=root, now=now)
        if report["score"] != 90 or report["result"] != "failed":
            raise AssertionError("a release for another commit was accepted")

        stale_governance = deepcopy(governance)
        stale_governance["generatedAt"] = (now - timedelta(hours=25)).isoformat()
        report, _ = evaluate(production, stale_governance, release, root=root, now=now)
        if report["score"] != 90:
            raise AssertionError("stale governance evidence retained governance points")

        incomplete_release = deepcopy(release)
        del incomplete_release["inputSha256"]["annotatedTag"]
        report, _ = evaluate(production, governance, incomplete_release, root=root, now=now)
        if report["score"] != 90:
            raise AssertionError("release evidence with missing input provenance was accepted")

        stale_production = deepcopy(production)
        stale_production["completedAt"] = (now - timedelta(days=8)).isoformat()
        report, _ = evaluate(stale_production, governance, release, root=root, now=now)
        if report["score"] != 20:
            raise AssertionError("stale live acceptance evidence retained platform points")

        report, diagnostics = evaluate(None, None, None, root=root, now=now)
        if report["score"] != 0 or len(diagnostics) != 3:
            raise AssertionError("missing evidence did not fail every readiness category")

        evidence_path = root / "release.json"
        evidence_path.write_text(json.dumps(release) + "\n", encoding="utf-8")
        if not readiness.SHA256_RE.fullmatch(readiness.artifact_sha256(evidence_path)):
            raise AssertionError("readiness evidence hashing did not return a SHA-256")

        production_path = root / "production.json"
        governance_path = root / "governance.json"
        release_path = root / "release.json"
        checksums_path = root / "SHA256SUMS"
        bundle_path = root / "SHA256SUMS.sigstore.json"
        score_path = root / "score.json"
        production_path.write_text(json.dumps(production) + "\n", encoding="utf-8")
        governance_path.write_text(json.dumps(governance) + "\n", encoding="utf-8")
        release_path.write_text(json.dumps(release) + "\n", encoding="utf-8")
        artifact = f"platform-gitops-{TAG}"
        checksums_path.write_text(
            f"{'2' * 64}  {artifact}.tar.gz\n"
            f"{'3' * 64}  {artifact}.spdx.json\n"
            f"{'4' * 64}  {artifact}.cyclonedx.json\n"
            f"{readiness.artifact_sha256(governance_path)}  {artifact}.github-governance.json\n"
            f"{readiness.artifact_sha256(release_path)}  {artifact}.github-release.json\n",
            encoding="utf-8",
        )
        bundle_path.write_text("{}\n", encoding="utf-8")
        runner_calls: list[list[str]] = []

        def successful_cosign(command: list[str], **_kwargs):  # type: ignore[no-untyped-def]
            runner_calls.append(command)
            return type("Result", (), {"returncode": 0})()

        release_hashes = readiness.verify_release_bundle(
            checksums_path=checksums_path,
            bundle_path=bundle_path,
            governance_path=governance_path,
            release_path=release_path,
            repository=REPOSITORY,
            tag=TAG,
            cosign_bin=sys.executable,
            runner=successful_cosign,
        )
        if set(release_hashes) != {"checksums", "sigstoreBundle"}:
            raise AssertionError("release bundle verification did not retain artifact hashes")
        if not runner_calls or "--certificate-identity" not in runner_calls[0]:
            raise AssertionError("release bundle verification did not constrain keyless identity")
        identity_index = runner_calls[0].index("--certificate-identity") + 1
        issuer_index = runner_calls[0].index("--certificate-oidc-issuer") + 1
        if runner_calls[0][identity_index] != (
            f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/{TAG}"
        ):
            raise AssertionError("release bundle verification used the wrong workflow identity")
        if runner_calls[0][issuer_index] != readiness.GITHUB_ACTIONS_OIDC_ISSUER:
            raise AssertionError("release bundle verification used the wrong OIDC issuer")

        def rejected_cosign(_command: list[str], **_kwargs):  # type: ignore[no-untyped-def]
            return type("Result", (), {"returncode": 1})()

        try:
            readiness.verify_release_bundle(
                checksums_path=checksums_path,
                bundle_path=bundle_path,
                governance_path=governance_path,
                release_path=release_path,
                repository=REPOSITORY,
                tag=TAG,
                cosign_bin=sys.executable,
                runner=rejected_cosign,
            )
        except readiness.ReadinessError:
            pass
        else:
            raise AssertionError("a release bundle rejected by Cosign was accepted")

        mismatched_checksums = checksums_path.read_text(encoding="utf-8").replace(
            readiness.artifact_sha256(release_path), "5" * 64
        )
        checksums_path.write_text(mismatched_checksums, encoding="utf-8")
        try:
            readiness.verify_release_bundle(
                checksums_path=checksums_path,
                bundle_path=bundle_path,
                governance_path=governance_path,
                release_path=release_path,
                repository=REPOSITORY,
                tag=TAG,
                cosign_bin=sys.executable,
                runner=successful_cosign,
            )
        except readiness.ReadinessError:
            pass
        else:
            raise AssertionError("release evidence not present in signed checksums was accepted")
        checksums_path.write_text(
            mismatched_checksums.replace("5" * 64, readiness.artifact_sha256(release_path)),
            encoding="utf-8",
        )
        argv = [
            "verify_production_readiness_score.py",
            "--production-evidence",
            str(production_path),
            "--governance-evidence",
            str(governance_path),
            "--release-evidence",
            str(release_path),
            "--release-checksums",
            str(checksums_path),
            "--release-checksum-bundle",
            str(bundle_path),
            "--profile",
            PROFILE,
            "--repository",
            REPOSITORY,
            "--commit",
            COMMIT,
            "--tag",
            TAG,
            "--output",
            str(score_path),
        ]
        with (
            patch.object(readiness, "ROOT", root),
            patch.object(readiness, "verify_release_bundle", return_value=release_hashes),
            patch.object(sys, "argv", argv),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            if readiness.main() != 0:
                raise AssertionError("valid CLI evidence did not pass")
        score_document = json.loads(score_path.read_text(encoding="utf-8"))
        if score_document["score"] != 100 or set(score_document["evidenceSha256"]) != {
            "production",
            "governance",
            "release",
            "checksums",
            "sigstoreBundle",
        }:
            raise AssertionError("CLI score report is not complete and artifact-bound")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["verify_production_readiness_score.py"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            if readiness.main() != 2:
                raise AssertionError("CLI accepted missing evidence and identity arguments")

    print("Production readiness 100-point gate behavior passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
