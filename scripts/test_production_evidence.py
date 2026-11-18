#!/usr/bin/env python3
"""Self-test the private, commit-bound production evidence validator."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_production_evidence as evidence  # noqa: E402
import verify_openbao_ceremony_evidence as ceremony_evidence  # noqa: E402
import test_data_protection_contract as protection_fixture  # noqa: E402
from verify_argocd_platform_revision import application_plan, verify_revision  # noqa: E402
from verify_restore_evidence import GITLAB_PROFILE  # noqa: E402


def fail(message: str) -> None:
    raise AssertionError(message)


def fixture(root: Path, now: datetime) -> dict[str, object]:
    directory = root / "private" / "production-evidence"
    directory.mkdir(parents=True)
    log = directory / "acceptance.log"
    log.write_text(
        "== platform production evidence ==\n"
        "source_branch=private-deploy\n"
        "source_expected_ref=seed/main\n"
        f"source_tree={'b' * 40}\n"
        f"source_remote_url_sha256={'c' * 64}\n"
        "== platform-production-check ==\n"
        "platform-production-check\n"
        "== rendered-live-image-reconciliation ==\n"
        "Image inventory evidence accepted:\n"
        f"argocd_revision=verified profile=premium-3node commit={'a' * 40} applications=23 repository_sha256={'d' * 64}\n",
        encoding="utf-8",
    )
    cluster_id_sha256 = hashlib.sha256(b"openbao-cluster-test").hexdigest()
    log.write_text(
        log.read_text(encoding="utf-8")
        + f"pod=openbao-0 cluster_id_sha256={cluster_id_sha256}\n"
        + f"pod=openbao-1 cluster_id_sha256={cluster_id_sha256}\n",
        encoding="utf-8",
    )
    inventory = directory / "acceptance-image-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAt": (now - timedelta(minutes=15)).isoformat(),
                "profile": "premium-3node",
                "commit": "a" * 40,
                "expectedRegistry": "registry.example.test",
                "result": "passed",
                "inputs": {
                    "renderedSummarySha256": "d" * 64,
                    "liveInventorySha256": "e" * 64,
                    "signatureReportSha256": "f" * 64,
                    "exceptionsSha256": None,
                },
                "rendered": {"references": 1, "uniqueImages": 1, "unresolved": 0},
                "live": {
                    "containers": 1,
                    "uniqueImages": 1,
                    "unresolved": 0,
                    "clusterUid": "cluster-test",
                    "capturedAt": (now - timedelta(minutes=16)).isoformat(),
                },
                "images": [
                    {
                        "image": "registry.example.test/platform/app@sha256:" + "1" * 64,
                        "rendered": True,
                        "live": True,
                        "signatureVerified": True,
                        "admissionEnforced": True,
                        "exception": None,
                    }
                ],
                "summary": {
                    "images": 1,
                    "privateRegistryImages": 1,
                    "signatureVerifiedImages": 1,
                    "exceptions": 0,
                    "uncovered": 0,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    openbao_configuration = (
        root
        / "gitops"
        / "clusters"
        / "rke2-main"
        / "premium-3node"
        / "apps"
        / "openbao"
    )
    openbao_configuration.mkdir(parents=True)
    (openbao_configuration / "values.yaml").write_text(
        "server:\n  ha:\n    replicas: 3\n", encoding="utf-8"
    )
    ceremony = directory / "acceptance-openbao-ceremony.json"
    ceremony.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "ceremonyId": "CHG-OPENBAO-TEST",
                "completedAt": (now - timedelta(days=30)).isoformat(),
                "recoveryTestedAt": (now - timedelta(days=1)).isoformat(),
                "operator": "openbao-operator@example.test",
                "approver": "openbao-approver@example.test",
                "profile": "premium-3node",
                "sourceCommit": "a" * 40,
                "configurationSha256": ceremony_evidence.configuration_sha256(
                    root, "premium-3node"
                ),
                "result": "passed",
                "cluster": {"clusterIdSha256": cluster_id_sha256},
                "seal": {
                    "mode": "shamir-pgp",
                    "shares": 5,
                    "threshold": 3,
                    "distinctCustodians": 5,
                    "custodianKeyFingerprintSha256": [
                        hashlib.sha256(f"custodian-{index}".encode()).hexdigest()
                        for index in range(5)
                    ],
                    "rootTokenRecipientFingerprintSha256": hashlib.sha256(
                        b"root-token-recipient"
                    ).hexdigest(),
                    "encryptedAtCreation": True,
                    "rootTokenEncryptedAtCreation": True,
                    "plaintextMaterialRetained": False,
                    "offlineEscrowCopies": 2,
                },
                "controls": {
                    name: True for name in ceremony_evidence.REQUIRED_CONTROLS
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    restore = directory / "acceptance-restore-evidence.json"
    restore.write_text(
        json.dumps(protection_fixture.valid_evidence(now), indent=2) + "\n",
        encoding="utf-8",
    )
    forgejo_recovery = directory / "acceptance-forgejo-recovery.json"
    forgejo_recovery.write_text(
        json.dumps(protection_fixture.valid_forgejo_evidence(now), indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "schemaVersion": 7,
        "releaseId": "release-test",
        "completedAt": (now - timedelta(minutes=15)).isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "commit": "a" * 40,
        "result": "passed",
        "logPath": "private/production-evidence/acceptance.log",
        "logSha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "imageInventory": {
            "path": "private/production-evidence/acceptance-image-inventory.json",
            "sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
        },
        "openbaoCeremony": {
            "path": "private/production-evidence/acceptance-openbao-ceremony.json",
            "sha256": hashlib.sha256(ceremony.read_bytes()).hexdigest(),
        },
        "restoreEvidence": {
            "path": "private/production-evidence/acceptance-restore-evidence.json",
            "sha256": hashlib.sha256(restore.read_bytes()).hexdigest(),
        },
        "forgejoRecovery": {
            "path": "private/production-evidence/acceptance-forgejo-recovery.json",
            "sha256": hashlib.sha256(forgejo_recovery.read_bytes()).hexdigest(),
        },
        "source": {
            "branch": "private-deploy",
            "expectedRef": "seed/main",
            "remote": "seed",
            "remoteUrlSha256": "c" * 64,
            "tree": "b" * 40,
            "clean": True,
        },
        "gates": {name: "passed" for name in evidence.REQUIRED_GATES},
    }


def test_gitlab_production_evidence(root: Path, now: datetime) -> None:
    document = fixture(root, now)
    directory = root / "private/production-evidence"
    inherited_openbao = "gitops/clusters/rke2-main/premium-3node/apps/openbao"
    (root / "profiles").mkdir()
    (root / "profiles" / f"{GITLAB_PROFILE}.yaml").write_text(
        f"profile: {GITLAB_PROFILE}\ninherits: premium-3node\n", encoding="utf-8",
    )
    platform_apps = (root / inherited_openbao).parents[1] / "platform-apps.yaml"
    platform_apps.write_text(f"spec:\n  source:\n    path: {inherited_openbao}\n", encoding="utf-8")
    if ceremony_evidence.configuration_sha256(root, GITLAB_PROFILE) != (
        ceremony_evidence.configuration_sha256(root, "premium-3node")
    ):
        fail("GitLab OpenBao ceremony must bind the actual inherited application tree")

    log = root / document["logPath"]
    log_prefix = log.read_text(encoding="utf-8").replace("profile=premium-3node ", f"profile={GITLAB_PROFILE} ")

    def rebind(key: str, proof: dict) -> None:
        path = root / document[key]["path"]
        path.write_text(json.dumps(proof), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        document[key]["sha256"] = digest
        if key == "gitlabRecovery":
            log.write_text(f"{log_prefix}GitLab recovery evidence accepted: sha256={digest}\n", encoding="utf-8")
            document["logSha256"] = hashlib.sha256(log.read_bytes()).hexdigest()

    for key in ("imageInventory", "openbaoCeremony", "restoreEvidence"):
        path = root / document[key]["path"]
        proof = json.loads(path.read_text(encoding="utf-8"))
        proof["profile"] = GITLAB_PROFILE
        if key == "restoreEvidence":
            proof["checks"]["gitlab"] = proof["checks"].pop("forgejo")
        rebind(key, proof)

    document["profile"] = GITLAB_PROFILE
    document["schemaVersion"] = 8
    document.pop("forgejoRecovery")
    document["gitlabRecovery"] = {"path": (directory / "acceptance-gitlab.json").relative_to(root).as_posix()}
    recovery = json.loads(
        (ROOT / "examples/gitlab-recovery-evidence.example.json").read_text(encoding="utf-8")
    )
    kickoff = now - timedelta(days=1)
    recovery["clusterUid"] = "cluster-test"
    recovery["outageStartedAt"] = (kickoff + timedelta(minutes=1)).isoformat()
    recovery["completedAt"] = (kickoff + timedelta(minutes=5)).isoformat()
    for phase, offset in (("before", 0), ("during", 2), ("after", 4)):
        recovery[phase]["observedAt"] = (kickoff + timedelta(minutes=offset)).isoformat()
    for check in recovery["checks"].values():
        check["evidence"]["verifiedAt"] = recovery["completedAt"]
    rebind("gitlabRecovery", recovery)

    def validate(candidate: dict) -> dict:
        return evidence.validate_evidence(
            candidate, root=root, now=now, max_age_days=7,
            expected_profile=GITLAB_PROFILE, expected_commit="a" * 40,
        )

    def rejects(candidate: dict, reason: str) -> None:
        try:
            validate(candidate)
        except evidence.EvidenceError as exc:
            if reason in str(exc):
                return
            fail(f"expected {reason!r} in rejection: {exc}")
        fail(f"expected GitLab acceptance rejection: {reason}")

    if validate(document)["gitlab_recovery_drill_id"] != recovery["drillId"]:
        fail("GitLab production acceptance did not retain the recovery drill identity")
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/verify_gitlab_recovery_evidence.py"),
            str(root / document["gitlabRecovery"]["path"]), "--expected-commit", "a" * 40,
        ],
        capture_output=True, text=True, check=False, timeout=30,
    )
    verified_marker = f"GitLab recovery evidence accepted: sha256={document['gitlabRecovery']['sha256']}"
    if result.returncode != 0 or result.stdout.strip() != verified_marker:
        fail(f"GitLab recovery CLI did not bind the verified bytes to the gate log: {result.stderr}")
    verified_log, verified_log_hash = log.read_bytes(), document["logSha256"]
    rebind("gitlabRecovery", dict(recovery, drillId="different-valid-drill"))
    log.write_bytes(verified_log)
    document["logSha256"] = verified_log_hash
    rejects(document, "does not record verification of the GitLab recovery hash")
    rebind("gitlabRecovery", recovery)
    rejects(dict(document, schemaVersion=7), "schemaVersion must be 8")
    rejects(dict(document, forgejoRecovery=document["gitlabRecovery"]), "forgejoRecovery cannot certify")
    rebind("gitlabRecovery", dict(recovery, clusterUid="other-cluster"))
    rejects(document, "another Kubernetes cluster")
    rebind("gitlabRecovery", dict(recovery, rtoSeconds=59))
    rejects(document, "exceeded rtoSeconds")
    stale_replica = deepcopy(recovery)
    stale_replica["after"]["replicas"]["gitaly-0"]["generation"] = 19
    rebind("gitlabRecovery", stale_replica)
    rejects(document, "stale replica generation")
    mismatched_job = deepcopy(recovery)
    mismatched_job["runnerPipeline"]["commit"] = "f" * 40
    rebind("gitlabRecovery", mismatched_job)
    rejects(document, "post-failure commit")


def test_live_application_revision() -> None:
    try:
        application_plan(ROOT, "premium-3node-lab")
    except ValueError as exc:
        if "lab MinIO" not in str(exc):
            raise
    else:
        fail("lab-only object storage passed production planning")
    plan = application_plan(ROOT, GITLAB_PROFILE)
    if {"gitlab-ce", "gitlab-runner", "platform-postgres"} - plan.keys() or "forgejo" in plan:
        fail("release revision plan must use the GitLab deployment profile")
    commit = "a" * 40
    repository = "https://git.example.test/platform.git"
    repo_hash = hashlib.sha256(repository.encode()).hexdigest()
    items = []
    for name, app in plan.items():
        source = dict(app["source"], repoURL=repository, targetRevision="main")
        items.append({
            "metadata": {"name": name},
            "spec": dict(app, source=source),
            "status": {
                "sync": {"status": "Synced", "revision": commit, "comparedTo": {
                    "source": dict(source), "destination": app["destination"],
                }},
                "health": {"status": "Healthy"},
            },
        })
    verify_revision({"items": items}, plan, commit, repo_hash)

    candidate = next(item for item in items if item["metadata"]["name"] == "gitlab-ce")
    sync = candidate["status"]["sync"]
    for source in (candidate["spec"]["source"], sync["comparedTo"]["source"]):
        source["repoURL"] = "https://mirror.example.test/platform.git"
    try:
        verify_revision({"items": items}, plan, commit, repo_hash)
    except ValueError as exc:
        if "repository differs" not in str(exc):
            raise
    else:
        fail("matching Git revisions in a different repository were accepted")
    for source in (candidate["spec"]["source"], sync["comparedTo"]["source"]):
        source["repoURL"] = repository
    for field, wrong in (("revision", "b" * 40), ("status", "OutOfSync"), ("revisions", [commit])):
        original = dict(sync)
        sync[field] = wrong
        try:
            verify_revision({"items": items}, plan, commit, repo_hash)
        except ValueError:
            pass
        else:
            fail(f"live evidence accepted stale or incompatible Argo CD {field}")
        sync.clear()
        sync.update(original)
    sync["comparedTo"]["source"]["path"] = "gitops/unreviewed"
    try:
        verify_revision({"items": items}, plan, commit, repo_hash)
    except ValueError as exc:
        if "different source" not in str(exc):
            raise
    else:
        fail("Argo CD evidence accepted stale status from a different deployment path")
    sync["comparedTo"]["source"]["path"] = plan["gitlab-ce"]["source"]["path"]
    candidate["operation"] = {"sync": {"revision": "b" * 40}}
    try:
        verify_revision({"items": items}, plan, commit, repo_hash)
    except ValueError as exc:
        if "active operation" not in str(exc):
            raise
    else:
        fail("Argo CD evidence accepted a pending sync operation")


def main() -> int:
    now = datetime.now(timezone.utc)
    test_live_application_revision()
    runner = (
        ROOT / "scripts" / "bootstrap" / "run-platform-production-evidence.sh"
    ).read_text(encoding="utf-8")
    for marker in (
        "require_value PLATFORM_RESTORE_EVIDENCE_FILE",
        "require_value PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE",
        "require_value PLATFORM_PRODUCTION_APPROVAL_APPROVER",
        "python_bin=\"${PLATFORM_PRODUCTION_EVIDENCE_PYTHON:-${PYTHON:-python3}}\"",
        'export PYTHON="${python_bin}"',
        '"${python_bin}" scripts/verify_production_evidence.py',
        "PLATFORM_EVIDENCE_APPROVER must match the configured PLATFORM_PRODUCTION_APPROVAL_APPROVER.",
        "atomic_write_bytes(destination, read_bounded_bytes(source))",
        '"restoreEvidence"',
        '"forgejoRecovery"',
        '"gitlabRecovery"',
        '"schemaVersion": 7',
    ):
        if marker not in runner:
            fail(f"production evidence runner is missing retained proof: {marker}")

    with tempfile.TemporaryDirectory(prefix="platform-production-evidence-") as directory:
        root = Path(directory)
        document = fixture(root, now)
        summary = evidence.validate_evidence(
            document,
            root=root,
            now=now,
            max_age_days=7,
            expected_profile="premium-3node",
            expected_commit="a" * 40,
        )
        if summary["release_id"] != "release-test":
            fail("valid production evidence did not preserve release id")
        if summary["expected_ref"] != "seed/main":
            fail("valid production evidence did not preserve source provenance")
        if summary["image_inventory_images"] != 1:
            fail("valid production evidence did not preserve image inventory proof")
        if summary["openbao_ceremony_id"] != "CHG-OPENBAO-TEST":
            fail("valid production evidence did not preserve OpenBao ceremony proof")
        if summary["restore_drill_id"] != "drill-test":
            fail("valid production evidence did not preserve restore drill proof")
        if summary["forgejo_recovery_drill_id"] != "forgejo-test":
            fail("valid production evidence did not preserve Forgejo recovery proof")

        stale = dict(document)
        stale["completedAt"] = (now - timedelta(days=8)).isoformat()
        try:
            evidence.validate_evidence(stale, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("stale production evidence was accepted")

        dirty = dict(document)
        dirty["source"] = dict(document["source"], clean=False)
        try:
            evidence.validate_evidence(dirty, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a dirty source checkout")

        older_packet = {**document, "completedAt": (now - timedelta(minutes=20)).isoformat()}
        try:
            evidence.validate_evidence(older_packet, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError as exc:
            if "postdates production acceptance" not in str(exc):
                raise
        else:
            fail("production evidence accepted an image inventory captured after the acceptance")
        outside_log = root / "outside.log"
        outside_log.write_bytes((root / document["logPath"]).read_bytes())
        linked_log = root / "private/production-evidence/linked.log"
        try:
            linked_log.symlink_to(outside_log)
        except OSError:
            if sys.platform != "win32":
                raise
        else:
            redirected = {**document, "logPath": linked_log.relative_to(root).as_posix()}
            try:
                evidence.validate_evidence(redirected, root=root, now=now, max_age_days=7)
            except evidence.EvidenceError as exc:
                if "resolves outside" not in str(exc):
                    raise
            else:
                fail("production evidence accepted a retained artifact outside the packet directory")

        wrong_remote = dict(document)
        wrong_remote["source"] = dict(
            document["source"], expectedRef="origin/main"
        )
        try:
            evidence.validate_evidence(wrong_remote, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a mismatched remote-tracking ref")

        changed_inventory = root / "private" / "production-evidence" / "acceptance-image-inventory.json"
        original_inventory = changed_inventory.read_text(encoding="utf-8")
        changed_inventory.write_text("{}\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a changed image inventory artifact")
        changed_inventory.write_text(original_inventory, encoding="utf-8")

        changed_ceremony = (
            root
            / "private"
            / "production-evidence"
            / "acceptance-openbao-ceremony.json"
        )
        original_ceremony = changed_ceremony.read_text(encoding="utf-8")
        wrong_source_ceremony = json.loads(original_ceremony)
        wrong_source_ceremony["sourceCommit"] = "9" * 40
        changed_ceremony.write_text(
            json.dumps(wrong_source_ceremony, indent=2) + "\n", encoding="utf-8"
        )
        wrong_ceremony_source = dict(document)
        wrong_ceremony_source["openbaoCeremony"] = dict(
            document["openbaoCeremony"],
            sha256=hashlib.sha256(changed_ceremony.read_bytes()).hexdigest(),
        )
        try:
            evidence.validate_evidence(
                wrong_ceremony_source, root=root, now=now, max_age_days=7
            )
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a ceremony from a different source revision")

        changed_ceremony.write_text("{}\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a changed OpenBao ceremony artifact")
        changed_ceremony.write_text(original_ceremony, encoding="utf-8")

        changed_restore = (
            root
            / "private"
            / "production-evidence"
            / "acceptance-restore-evidence.json"
        )
        original_restore = changed_restore.read_text(encoding="utf-8")
        changed_restore.write_text("{}\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a changed restore artifact")

        wrong_source_restore = json.loads(original_restore)
        wrong_source_restore["sourceCommit"] = "9" * 40
        changed_restore.write_text(
            json.dumps(wrong_source_restore, indent=2) + "\n", encoding="utf-8"
        )
        wrong_restore_source = dict(document)
        wrong_restore_source["restoreEvidence"] = dict(
            document["restoreEvidence"],
            sha256=hashlib.sha256(changed_restore.read_bytes()).hexdigest(),
        )
        try:
            evidence.validate_evidence(
                wrong_restore_source, root=root, now=now, max_age_days=7
            )
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a restore drill from another revision")
        changed_restore.write_text(original_restore, encoding="utf-8")

        changed_forgejo = (
            root
            / "private"
            / "production-evidence"
            / "acceptance-forgejo-recovery.json"
        )
        original_forgejo = changed_forgejo.read_text(encoding="utf-8")
        changed_forgejo.write_text("{}\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted a changed Forgejo recovery artifact")

        wrong_source_forgejo = json.loads(original_forgejo)
        wrong_source_forgejo["sourceCommit"] = "9" * 40
        wrong_source_forgejo["preRecovery"]["argocdRevision"] = "9" * 40
        wrong_source_forgejo["postRecovery"]["argocdRevision"] = "9" * 40
        changed_forgejo.write_text(
            json.dumps(wrong_source_forgejo, indent=2) + "\n", encoding="utf-8"
        )
        wrong_forgejo_source = dict(document)
        wrong_forgejo_source["forgejoRecovery"] = dict(
            document["forgejoRecovery"],
            sha256=hashlib.sha256(changed_forgejo.read_bytes()).hexdigest(),
        )
        try:
            evidence.validate_evidence(
                wrong_forgejo_source, root=root, now=now, max_age_days=7
            )
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence accepted Forgejo recovery from another revision")
        changed_forgejo.write_text(original_forgejo, encoding="utf-8")

        changed_log = root / "private" / "production-evidence" / "acceptance.log"
        original_log = changed_log.read_text(encoding="utf-8")
        changed_log.write_text(original_log.replace(f"commit={'a' * 40}", f"commit={'b' * 40}"), encoding="utf-8")
        wrong_live_revision = dict(document, logSha256=hashlib.sha256(changed_log.read_bytes()).hexdigest())
        try:
            evidence.validate_evidence(wrong_live_revision, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError as exc:
            if "verified Argo CD release revision" not in str(exc):
                raise
        else:
            fail("production evidence accepted an Argo CD check against another commit")
        changed_log.write_text("changed\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence with a changed log was accepted")

        test_gitlab_production_evidence(root / "gitlab", now)

    print("Production evidence validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
