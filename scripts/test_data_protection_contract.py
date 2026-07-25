#!/usr/bin/env python3
"""Self-test the production data-protection and restore-evidence gates."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_platform_data_protection as protection  # noqa: E402
import verify_restore_evidence as evidence  # noqa: E402


def fail(message: str) -> None:
    raise AssertionError(message)


def valid_evidence(now: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "drillId": "drill-test",
        "completedAt": (now - timedelta(hours=2)).isoformat(),
        "operator": "operator@example.test",
        "approver": "approver@example.test",
        "profile": "premium-3node",
        "result": "passed",
        "rpoHours": 24,
        "rtoMinutes": 240,
        "elapsedMinutes": 60,
        "checks": {
            name: {"status": "passed", "evidence": f"ticket://test/{name}"}
            for name in evidence.REQUIRED_CHECKS
        },
    }


def test_restore_evidence() -> None:
    now = datetime.now(timezone.utc)
    document = valid_evidence(now)
    summary = evidence.validate_evidence(
        document,
        now=now,
        max_age_days=92,
        expected_profile="premium-3node",
    )
    if summary["drill_id"] != "drill-test":
        fail("valid restore evidence did not preserve the drill id")

    stale = valid_evidence(now)
    stale["completedAt"] = (now - timedelta(days=93)).isoformat()
    try:
        evidence.validate_evidence(stale, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("stale restore evidence was accepted")

    incomplete = valid_evidence(now)
    del incomplete["checks"]["harbor"]  # type: ignore[index]
    try:
        evidence.validate_evidence(incomplete, now=now, max_age_days=92)
    except evidence.EvidenceError:
        pass
    else:
        fail("incomplete restore evidence was accepted")


def test_endpoint_classification() -> None:
    for endpoint in (
        "http://minio.object-storage.svc.cluster.local:9000",
        "https://127.0.0.1:9000",
        "object-storage.svc",
    ):
        if not protection.is_cluster_local_endpoint(endpoint):
            fail(f"cluster-local endpoint was accepted: {endpoint}")
    for endpoint in (
        "https://s3.amazonaws.com",
        "https://backup.example.test:9443",
        "s3://platform-longhorn@us-east-1/",
    ):
        if protection.is_cluster_local_endpoint(endpoint):
            fail(f"external endpoint was rejected: {endpoint}")


def test_contract_wiring() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    wrapper = ROOT / "scripts" / "bootstrap" / "run-platform-data-protection.sh"
    playbook = ROOT / "ansible" / "playbooks" / "verify-platform-data-protection.yml"
    if "platform-data-protection:" not in makefile:
        fail("Makefile is missing platform-data-protection")
    if "run-platform-data-protection.sh" not in makefile:
        fail("production gate does not invoke the data-protection wrapper")
    if not wrapper.is_file() or not playbook.is_file():
        fail("data-protection wrapper or Ansible playbook is missing")
    with tempfile.TemporaryDirectory(prefix="platform-restore-evidence-") as directory:
        path = Path(directory) / "evidence.json"
        path.write_text(json.dumps(valid_evidence(datetime.now(timezone.utc))), encoding="utf-8")
        if not json.loads(path.read_text(encoding="utf-8")):
            fail("restore evidence fixture was not written")


def main() -> int:
    test_restore_evidence()
    test_endpoint_classification()
    test_contract_wiring()
    print("Production data-protection contract self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
