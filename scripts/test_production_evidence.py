#!/usr/bin/env python3
"""Self-test the private, commit-bound production evidence validator."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_production_evidence as evidence  # noqa: E402
import verify_openbao_ceremony_evidence as ceremony_evidence  # noqa: E402
import test_data_protection_contract as protection_fixture  # noqa: E402


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
        "== platform-production-check ==\n"
        "platform-production-check\n"
        "== rendered-live-image-reconciliation ==\n"
        "Image inventory evidence accepted:\n",
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


def main() -> int:
    now = datetime.now(timezone.utc)
    runner = (
        ROOT / "scripts" / "bootstrap" / "run-platform-production-evidence.sh"
    ).read_text(encoding="utf-8")
    for marker in (
        "require_value PLATFORM_RESTORE_EVIDENCE_FILE",
        "require_value PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE",
        "require_value PLATFORM_PRODUCTION_APPROVAL_APPROVER",
        "PLATFORM_EVIDENCE_APPROVER must match the configured PLATFORM_PRODUCTION_APPROVAL_APPROVER.",
        "atomic_write_text(destination, read_bounded_text(source))",
        '"restoreEvidence"',
        '"forgejoRecovery"',
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
        changed_log.write_text("changed\n", encoding="utf-8")
        try:
            evidence.validate_evidence(document, root=root, now=now, max_age_days=7)
        except evidence.EvidenceError:
            pass
        else:
            fail("production evidence with a changed log was accepted")

    print("Production evidence validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
