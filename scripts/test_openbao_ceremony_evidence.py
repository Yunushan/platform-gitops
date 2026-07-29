#!/usr/bin/env python3
"""Behavior-test the secret-free OpenBao ceremony evidence validator."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_openbao_ceremony_evidence as evidence  # noqa: E402


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def fixture(root: Path, now: datetime) -> dict[str, object]:
    configuration = (
        root
        / "gitops"
        / "clusters"
        / "rke2-main"
        / "premium-3node"
        / "apps"
        / "openbao"
    )
    configuration.mkdir(parents=True)
    (configuration / "kustomization.yaml").write_text(
        "resources:\n  - namespace.yaml\n", encoding="utf-8"
    )
    (configuration / "namespace.yaml").write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: openbao\n", encoding="utf-8"
    )
    return {
        "schemaVersion": 1,
        "ceremonyId": "CHG-2026-OPENBAO-001",
        "completedAt": (now - timedelta(days=30)).isoformat(),
        "recoveryTestedAt": (now - timedelta(days=1)).isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "sourceCommit": "a" * 40,
        "configurationSha256": evidence.configuration_sha256(root, "premium-3node"),
        "result": "passed",
        "cluster": {"clusterIdSha256": digest("openbao-cluster")},
        "seal": {
            "mode": "shamir-pgp",
            "shares": 5,
            "threshold": 3,
            "distinctCustodians": 5,
            "custodianKeyFingerprintSha256": [
                digest(f"custodian-{index}") for index in range(1, 6)
            ],
            "rootTokenRecipientFingerprintSha256": digest("root-token-recipient"),
            "encryptedAtCreation": True,
            "rootTokenEncryptedAtCreation": True,
            "plaintextMaterialRetained": False,
            "offlineEscrowCopies": 2,
        },
        "controls": {name: True for name in evidence.REQUIRED_CONTROLS},
    }


def must_reject(document: dict[str, object], root: Path, now: datetime, label: str) -> None:
    try:
        evidence.validate_evidence(
            document,
            root=root,
            now=now,
            max_recovery_age_days=180,
            expected_profile="premium-3node",
        )
    except evidence.EvidenceError:
        return
    raise AssertionError(f"OpenBao ceremony evidence accepted {label}")


def main() -> int:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="openbao-ceremony-evidence-") as directory:
        root = Path(directory)
        document = fixture(root, now)
        summary = evidence.validate_evidence(
            document,
            root=root,
            now=now,
            max_recovery_age_days=180,
            expected_profile="premium-3node",
            expected_source_commit="a" * 40,
            expected_cluster_id_sha256=digest("openbao-cluster"),
        )
        if summary["shares"] != 5 or summary["threshold"] != 3:
            raise AssertionError("valid ceremony evidence lost the custody quorum")

        wrong_source = deepcopy(document)
        try:
            evidence.validate_evidence(
                wrong_source,
                root=root,
                now=now,
                max_recovery_age_days=180,
                expected_profile="premium-3node",
                expected_source_commit="b" * 40,
            )
        except evidence.EvidenceError:
            pass
        else:
            raise AssertionError("ceremony evidence accepted a different source revision")

        same_approver = deepcopy(document)
        same_approver["approver"] = same_approver["operator"]
        must_reject(same_approver, root, now, "self-approval")

        stale = deepcopy(document)
        stale["recoveryTestedAt"] = (now - timedelta(days=181)).isoformat()
        must_reject(stale, root, now, "stale recovery testing")

        duplicate_custodian = deepcopy(document)
        duplicate_custodian["seal"]["custodianKeyFingerprintSha256"][1] = (
            duplicate_custodian["seal"]["custodianKeyFingerprintSha256"][0]
        )
        must_reject(duplicate_custodian, root, now, "duplicate key custodians")

        shared_root_recipient = deepcopy(document)
        shared_root_recipient["seal"]["rootTokenRecipientFingerprintSha256"] = (
            shared_root_recipient["seal"]["custodianKeyFingerprintSha256"][0]
        )
        must_reject(shared_root_recipient, root, now, "a non-independent root recipient")

        plaintext = deepcopy(document)
        plaintext["seal"]["plaintextMaterialRetained"] = True
        must_reject(plaintext, root, now, "retained plaintext material")

        weak_quorum = deepcopy(document)
        weak_quorum["seal"]["threshold"] = 2
        must_reject(weak_quorum, root, now, "a weak recovery quorum")

        missing_control = deepcopy(document)
        missing_control["controls"]["initialRootTokenRevoked"] = False
        must_reject(missing_control, root, now, "an active initial root token")

        wrong_cluster = deepcopy(document)
        try:
            evidence.validate_evidence(
                wrong_cluster,
                root=root,
                now=now,
                max_recovery_age_days=180,
                expected_profile="premium-3node",
                expected_cluster_id_sha256=digest("different-cluster"),
            )
        except evidence.EvidenceError:
            pass
        else:
            raise AssertionError("ceremony evidence accepted a different live cluster")

        configuration = (
            root
            / "gitops"
            / "clusters"
            / "rke2-main"
            / "premium-3node"
            / "apps"
            / "openbao"
            / "namespace.yaml"
        )
        configuration.write_text(configuration.read_text(encoding="utf-8") + "# changed\n")
        must_reject(document, root, now, "a changed OpenBao configuration")

    with tempfile.TemporaryDirectory(prefix="openbao-auto-unseal-evidence-") as directory:
        root = Path(directory)
        auto_unseal = fixture(root, now)
        auto_unseal["seal"]["mode"] = "hsm-auto-unseal"
        must_reject(auto_unseal, root, now, "auto-unseal without a provider binding")
        auto_unseal["seal"]["providerKeySha256"] = digest("hsm-provider-key")
        evidence.validate_evidence(
            auto_unseal,
            root=root,
            now=now,
            max_recovery_age_days=180,
            expected_profile="premium-3node",
        )

    print("OpenBao ceremony evidence validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
