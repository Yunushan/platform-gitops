#!/usr/bin/env python3
"""Behavior-test the fail-closed 100-point production readiness gate."""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
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
import create_production_approval as approval_creator  # noqa: E402
import verify_github_release_ref as release_verifier  # noqa: E402
import verify_production_approval as approval_verifier  # noqa: E402
import verify_production_readiness_score as readiness  # noqa: E402


COMMIT = "a" * 40
REPOSITORY = "example/platform-gitops"
TAG = "v1.2.3"
PROFILE = "premium-3node"
APPROVER = "approver@example.test"
PRODUCTION_EVIDENCE_SHA256 = "5" * 64
APPROVAL_KEY_SHA256 = "6" * 64


def bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix().split(":", 1)[1].lstrip("/")
    return f"/mnt/{drive}/{rest}"


def repo_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def run_bash(root: Path, script: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        raise AssertionError("bash is required for production score runner validation")
    script_path = root / "score-runner-test.sh"
    exports = "\n".join(
        f"export {name}={shlex.quote(value)}" for name, value in environment.items()
    )
    script_path.write_text(exports + "\n" + script, encoding="utf-8", newline="\n")
    command = "bash " + shlex.quote(bash_path(script_path))
    env = os.environ.copy()
    env.pop("PYTHON", None)
    env.pop("PLATFORM_PRODUCTION_SCORE_PYTHON", None)
    env.update(environment)
    return subprocess.run(
        [bash, "-lc", command],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def assert_runner_success(result: subprocess.CompletedProcess[str], description: str) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"expected {description} to succeed, got {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def read_capture(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            raise AssertionError(f"score runner capture contains an invalid line: {line!r}")
        values[key] = value
    return values


def test_score_runner_configuration(root: Path) -> None:
    runner = (ROOT / "scripts/bootstrap/run-platform-production-score.sh").read_text(
        encoding="utf-8"
    )
    if "umask 077" not in runner:
        raise AssertionError("production score runner must protect its output by default")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    if "platform-production-score:\n\t@bash scripts/bootstrap/run-platform-production-score.sh" not in makefile:
        raise AssertionError("Makefile score target must delegate interpreter selection to the wrapper")
    if 'PLATFORM_PRODUCTION_SCORE_PYTHON="$(PYTHON)"' in makefile:
        raise AssertionError("Makefile score target must not override private-file PYTHON")

    private_python = root / "private-python"
    explicit_python = root / "explicit-python"
    capture_from_file = root / "capture-from-file.txt"
    capture_from_explicit = root / "capture-from-explicit.txt"
    for path, label in ((private_python, "private"), (explicit_python, "explicit")):
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "{\n"
            "  printf 'interpreter=%s\\n' '" + label + "'\n"
            "  printf 'script=%s\\n' \"$1\"\n"
            "  printf 'arg1=%s\\n' \"$2\"\n"
            "  printf 'arg2=%s\\n' \"$3\"\n"
            "  printf 'profile=%s\\n' \"${PLATFORM_PROFILE:-}\"\n"
            "  printf 'from_file=%s\\n' \"${SCORE_FROM_FILE:-}\"\n"
            "} > \"${SCORE_CAPTURE}\"\n",
            encoding="utf-8",
            newline="\n",
        )
        path.chmod(0o700)

    env_file = root / "score.env"
    env_file.write_text(
        f"PYTHON={repo_path(private_python)}\n"
        "PLATFORM_PROFILE=private-profile\n"
        "SCORE_FROM_FILE=loaded\n",
        encoding="utf-8",
        newline="\n",
    )
    private_result = run_bash(
        root,
        """
set -euo pipefail
bash scripts/bootstrap/run-platform-production-score.sh --fixture-arg "value with spaces"
""",
        {
            "PLATFORM_PRODUCTION_SCORE_ENV_FILE": repo_path(env_file),
            "PLATFORM_PROFILE": "explicit-profile",
            "SCORE_CAPTURE": repo_path(capture_from_file),
        },
    )
    assert_runner_success(private_result, "loading the private score interpreter")
    private_capture = read_capture(capture_from_file)
    if private_capture != {
        "interpreter": "private",
        "script": "scripts/verify_production_readiness_score.py",
        "arg1": "--fixture-arg",
        "arg2": "value with spaces",
        "profile": "explicit-profile",
        "from_file": "loaded",
    }:
        raise AssertionError(f"private score runner configuration was not preserved: {private_capture}")

    explicit_result = run_bash(
        root,
        """
set -euo pipefail
bash scripts/bootstrap/run-platform-production-score.sh --fixture-arg "value with spaces"
""",
        {
            "PLATFORM_PRODUCTION_SCORE_ENV_FILE": repo_path(env_file),
            "PLATFORM_PRODUCTION_SCORE_PYTHON": repo_path(explicit_python),
            "PLATFORM_PROFILE": "explicit-profile",
            "SCORE_CAPTURE": repo_path(capture_from_explicit),
        },
    )
    assert_runner_success(explicit_result, "preserving an explicit score interpreter")
    explicit_capture = read_capture(capture_from_explicit)
    if explicit_capture["interpreter"] != "explicit" or explicit_capture["from_file"] != "loaded":
        raise AssertionError(
            f"explicit score interpreter was not preferred over the private file: {explicit_capture}"
        )


def governance_evidence(now: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 3,
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


def release_approval_evidence(now: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "generatedAt": (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        "repository": REPOSITORY,
        "runId": 1234,
        "runAttempt": 1,
        "commit": COMMIT,
        "releaseEnvironment": "production-release",
        "result": "passed",
        "approvalBindingSha256": "2" * 64,
        "inputSha256": {name: "3" * 64 for name in readiness.RELEASE_APPROVAL_INPUTS},
        "controls": {name: "passed" for name in readiness.RELEASE_APPROVAL_CONTROLS},
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


def production_approval_evidence(
    production: dict[str, object], now: datetime
) -> dict[str, object]:
    return approval_verifier.build_approval_document(
        production,
        production_sha256=PRODUCTION_EVIDENCE_SHA256,
        approval_key_sha256=APPROVAL_KEY_SHA256,
        approver=str(production["approver"]),
        approved_at=now - timedelta(minutes=1),
    )


def evaluate(
    production: dict[str, object] | None,
    production_approval: dict[str, object] | None,
    governance: dict[str, object] | None,
    release: dict[str, object] | None,
    release_approval: dict[str, object] | None,
    *,
    root: Path,
    now: datetime,
    approval_approver: str = APPROVER,
) -> tuple[dict[str, object], list[str]]:
    return readiness.evaluate_readiness(
        production_document=production,
        production_approval_document=production_approval,
        governance_document=governance,
        release_document=release,
        release_approval_document=release_approval,
        root=root,
        now=now,
        expected_profile=PROFILE,
        expected_repository=REPOSITORY,
        expected_commit=COMMIT,
        expected_tag=TAG,
        production_evidence_sha256=PRODUCTION_EVIDENCE_SHA256,
        production_approval_key_sha256=APPROVAL_KEY_SHA256,
        expected_approval_approver=approval_approver,
    )


def main() -> int:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="platform-readiness-score-", dir=ROOT) as directory:
        root = Path(directory)
        test_score_runner_configuration(root)
        production = production_fixture.fixture(root, now)
        production_approval = production_approval_evidence(production, now)
        governance = governance_evidence(now)
        release = release_evidence(now)
        release_approval = release_approval_evidence(now)

        report, diagnostics = evaluate(
            production,
            production_approval,
            governance,
            release,
            release_approval,
            root=root,
            now=now,
        )
        if report["score"] != 100 or report["result"] != "passed" or diagnostics:
            raise AssertionError(f"valid evidence did not earn 100/100: {report} {diagnostics}")
        if sum(category["weight"] for category in report["categories"]) != 100:
            raise AssertionError("readiness category weights do not total 100")

        report, diagnostics = evaluate(
            production,
            production_approval,
            governance,
            release,
            release_approval,
            root=root,
            now=now,
            approval_approver="unconfigured-approver@example.test",
        )
        if report["score"] != 20 or report["result"] != "failed":
            raise AssertionError("an approval by an unauthorized identity was accepted")
        if not any("configured authorized approver" in item for item in diagnostics):
            raise AssertionError("unauthorized approval did not explain the rejected identity")

        failed_governance = deepcopy(governance)
        failed_governance["controls"]["securityScanning"] = "failed"
        report, diagnostics = evaluate(
            production,
            production_approval,
            failed_governance,
            release,
            release_approval,
            root=root,
            now=now,
        )
        if report["score"] != 90 or report["result"] != "failed":
            raise AssertionError("failed governance evidence did not remove its full score")
        if not any("securityScanning" in item for item in diagnostics):
            raise AssertionError("failed governance evidence did not explain the rejected control")

        wrong_release = deepcopy(release)
        wrong_release["commit"] = "b" * 40
        report, _ = evaluate(
            production,
            production_approval,
            governance,
            wrong_release,
            release_approval,
            root=root,
            now=now,
        )
        if report["score"] != 90 or report["result"] != "failed":
            raise AssertionError("a release for another commit was accepted")

        stale_governance = deepcopy(governance)
        stale_governance["generatedAt"] = (now - timedelta(hours=25)).isoformat()
        report, _ = evaluate(
            production,
            production_approval,
            stale_governance,
            release,
            release_approval,
            root=root,
            now=now,
        )
        if report["score"] != 90:
            raise AssertionError("stale governance evidence retained governance points")

        incomplete_release = deepcopy(release)
        del incomplete_release["inputSha256"]["annotatedTag"]
        report, _ = evaluate(
            production,
            production_approval,
            governance,
            incomplete_release,
            release_approval,
            root=root,
            now=now,
        )
        if report["score"] != 90:
            raise AssertionError("release evidence with missing input provenance was accepted")

        stale_production = deepcopy(production)
        stale_production["completedAt"] = (now - timedelta(days=8)).isoformat()
        report, _ = evaluate(
            stale_production,
            production_approval,
            governance,
            release,
            release_approval,
            root=root,
            now=now,
        )
        if report["score"] != 20:
            raise AssertionError("stale live acceptance evidence retained platform points")

        wrong_approval = deepcopy(release_approval)
        wrong_approval["commit"] = "b" * 40
        report, _ = evaluate(
            production,
            production_approval,
            governance,
            release,
            wrong_approval,
            root=root,
            now=now,
        )
        if report["score"] != 90 or report["result"] != "failed":
            raise AssertionError("a release approval for another commit was accepted")

        report, diagnostics = evaluate(None, None, None, None, None, root=root, now=now)
        if report["score"] != 0 or len(diagnostics) != 5:
            raise AssertionError("missing evidence did not fail every readiness category")

        evidence_path = root / "release.json"
        evidence_path.write_text(json.dumps(release) + "\n", encoding="utf-8")
        if not readiness.SHA256_RE.fullmatch(readiness.artifact_sha256(evidence_path)):
            raise AssertionError("readiness evidence hashing did not return a SHA-256")

        production_path = root / "production.json"
        production_approval_path = root / "production-approval.json"
        production_approval_bundle_path = root / "production-approval.sigstore.json"
        production_approval_key_path = root / "production-approver.pub"
        governance_path = root / "governance.json"
        release_path = root / "release.json"
        release_approval_path = root / "release-approval.json"
        checksums_path = root / "SHA256SUMS"
        bundle_path = root / "SHA256SUMS.sigstore.json"
        score_path = root / "score.json"
        production_path.write_text(json.dumps(production) + "\n", encoding="utf-8")
        production_approval_key_path.write_text(
            "-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----\n",
            encoding="utf-8",
        )
        actual_approval_key_sha256 = approval_verifier.artifact_sha256(
            production_approval_key_path
        )
        creator_argv = [
            "create_production_approval.py",
            "--production-evidence",
            str(production_path),
            "--public-key",
            str(production_approval_key_path),
            "--public-key-sha256",
            actual_approval_key_sha256,
            "--approver",
            str(production["approver"]),
            "--output",
            str(production_approval_path),
        ]
        with (
            patch.object(approval_creator, "ROOT", root),
            patch.object(sys, "argv", creator_argv),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            if approval_creator.main() != 0:
                raise AssertionError("valid production approval could not be created")
        production_approval_bundle_path.write_text("{}\n", encoding="utf-8")
        governance_path.write_text(json.dumps(governance) + "\n", encoding="utf-8")
        release_path.write_text(json.dumps(release) + "\n", encoding="utf-8")
        release_approval_path.write_text(json.dumps(release_approval) + "\n", encoding="utf-8")
        artifact = f"platform-gitops-{TAG}"
        checksums_path.write_text(
            f"{'2' * 64}  {artifact}.tar.gz\n"
            f"{'3' * 64}  {artifact}.spdx.json\n"
            f"{'4' * 64}  {artifact}.cyclonedx.json\n"
            f"{readiness.artifact_sha256(governance_path)}  {artifact}.github-governance.json\n"
            f"{readiness.artifact_sha256(release_path)}  {artifact}.github-release.json\n"
            f"{readiness.artifact_sha256(release_approval_path)}  "
            f"{artifact}.github-release-approval.json\n",
            encoding="utf-8",
        )
        bundle_path.write_text("{}\n", encoding="utf-8")
        runner_calls: list[list[str]] = []

        def successful_cosign(command: list[str], **_kwargs):  # type: ignore[no-untyped-def]
            runner_calls.append(command)
            return type("Result", (), {"returncode": 0})()

        approval_runner_calls: list[list[str]] = []

        def successful_approval_cosign(
            command: list[str], **_kwargs
        ):  # type: ignore[no-untyped-def]
            approval_runner_calls.append(command)
            return type("Result", (), {"returncode": 0})()

        approval_hashes = approval_verifier.verify_signature(
            approval_path=production_approval_path,
            bundle_path=production_approval_bundle_path,
            public_key_path=production_approval_key_path,
            expected_key_sha256=actual_approval_key_sha256,
            cosign_bin=sys.executable,
            runner=successful_approval_cosign,
        )
        if set(approval_hashes) != {
            "productionApprovalBundle",
            "productionApprovalPublicKey",
        }:
            raise AssertionError("production approval did not retain signature trust hashes")
        if not approval_runner_calls or "--key" not in approval_runner_calls[0]:
            raise AssertionError("production approval did not use its pinned public key")

        try:
            approval_verifier.verify_signature(
                approval_path=production_approval_path,
                bundle_path=production_approval_bundle_path,
                public_key_path=production_approval_key_path,
                expected_key_sha256="0" * 64,
                cosign_bin=sys.executable,
                runner=successful_approval_cosign,
            )
        except approval_verifier.ApprovalError:
            pass
        else:
            raise AssertionError("production approval accepted an unpinned public key")

        def rejected_approval_cosign(
            _command: list[str], **_kwargs
        ):  # type: ignore[no-untyped-def]
            return type("Result", (), {"returncode": 1})()

        try:
            approval_verifier.verify_signature(
                approval_path=production_approval_path,
                bundle_path=production_approval_bundle_path,
                public_key_path=production_approval_key_path,
                expected_key_sha256=actual_approval_key_sha256,
                cosign_bin=sys.executable,
                runner=rejected_approval_cosign,
            )
        except approval_verifier.ApprovalError:
            pass
        else:
            raise AssertionError("production approval accepted a rejected signature")

        release_hashes = readiness.verify_release_bundle(
            checksums_path=checksums_path,
            bundle_path=bundle_path,
            governance_path=governance_path,
            release_path=release_path,
            release_approval_path=release_approval_path,
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
                release_approval_path=release_approval_path,
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
                release_approval_path=release_approval_path,
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
            "--production-approval",
            str(production_approval_path),
            "--production-approval-bundle",
            str(production_approval_bundle_path),
            "--production-approval-public-key",
            str(production_approval_key_path),
            "--production-approval-public-key-sha256",
            actual_approval_key_sha256,
            "--production-approval-approver",
            APPROVER,
            "--governance-evidence",
            str(governance_path),
            "--release-evidence",
            str(release_path),
            "--release-approval-evidence",
            str(release_approval_path),
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
            patch.object(
                approval_verifier,
                "verify_signature",
                return_value=approval_hashes,
            ),
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
            "productionApproval",
            "productionApprovalBundle",
            "productionApprovalPublicKey",
            "governance",
            "release",
            "releaseApproval",
            "checksums",
            "sigstoreBundle",
        }:
            raise AssertionError("CLI score report is not complete and artifact-bound")
        if (
            score_document["expected"]["productionApprovalApprover"] != APPROVER
            or
            score_document["expected"]["productionApprovalPublicKeySha256"]
            != actual_approval_key_sha256
        ):
            raise AssertionError("CLI score report did not retain the approval trust root")

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
