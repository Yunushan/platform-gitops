#!/usr/bin/env python3
"""Behavior-test exact rendered/live runtime image reconciliation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts/capture_live_image_inventory.py"
RECONCILE = ROOT / "scripts/reconcile_image_inventory.py"
VERIFY = ROOT / "scripts/verify_image_inventory_evidence.py"
PRIVATE_DIGEST = "registry.example.test/platform/app@sha256:" + "a" * 64
UPSTREAM_DIGEST = "docker.io/library/busybox@sha256:" + "b" * 64
COMMIT = "c" * 40


def write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def pod_list(private_image_id: str = PRIVATE_DIGEST) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "metadata": {"namespace": "platform", "name": "app-0"},
                "spec": {
                    "nodeName": "node-1",
                    "containers": [
                        {"name": "app", "image": "registry.example.test/platform/app:v1"},
                        {"name": "helper", "image": "busybox:1.36"},
                    ],
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {"name": "app", "ready": True, "imageID": private_image_id},
                        {"name": "helper", "ready": True, "imageID": UPSTREAM_DIGEST},
                    ],
                },
            }
        ],
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="platform-image-inventory-") as directory:
        root = Path(directory)
        manifest = write(
            root / "rendered/schema-validation/manifests/premium.json",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"namespace": "platform", "name": "app"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "app", "image": "registry.example.test/platform/app:v1"},
                                {"name": "helper", "image": "busybox:1.36"},
                            ]
                        }
                    }
                },
            },
        )
        summary = write(
            root / "rendered/schema-validation/summary.json",
            {
                "rendered": [
                    {
                        "profile": "premium-3node",
                        "application": "app",
                        "manifest": str(manifest.relative_to(root)),
                    }
                ],
                "skipped": [],
                "failures": [],
            },
        )
        pods = write(root / "pods.json", pod_list())
        live = root / "rendered/supply-chain/live.json"
        captured = run(
            CAPTURE,
            "--pods-json",
            str(pods),
            "--cluster-uid",
            "cluster-test",
            "--output",
            str(live),
        )
        if captured.returncode != 0:
            raise AssertionError(captured.stderr or captured.stdout)

        signatures = write(
            root / "rendered/supply-chain/cosign.json",
            {
                "schemaVersion": 1,
                "verifiedImages": [
                    {"image": PRIVATE_DIGEST, "key": "release.pub", "verified": True}
                ],
            },
        )
        trivy = write(
            root / "private/supply-chain/reports/busybox.json",
            {"SchemaVersion": 2, "Results": [{"Target": UPSTREAM_DIGEST, "Vulnerabilities": []}]},
        )
        today = date.today()
        exceptions = write(
            root / "private/supply-chain/exceptions.json",
            {
                "schemaVersion": 1,
                "exceptions": [
                    {
                        "id": "busybox-admission-gap",
                        "image": UPSTREAM_DIGEST,
                        "sourceImage": "busybox:1.36",
                        "owner": "platform-security",
                        "approvedBy": "risk-approver",
                        "ticket": "RISK-1",
                        "reason": "Temporary upstream admission scope exception.",
                        "createdAt": today.isoformat(),
                        "expiresAt": (today + timedelta(days=30)).isoformat(),
                        "vulnerabilityReport": str(trivy.relative_to(root)).replace("\\", "/"),
                        "vulnerabilityReportSha256": digest(trivy),
                    }
                ],
            },
        )
        evidence = root / "rendered/supply-chain/evidence.json"
        base = (
            "--rendered-summary",
            str(summary),
            "--live-inventory",
            str(live),
            "--signature-report",
            str(signatures),
            "--expected-registry",
            "registry.example.test",
            "--profile",
            "premium-3node",
            "--commit",
            COMMIT,
            "--output",
            str(evidence),
            "--root",
            str(root),
        )
        accepted = run(RECONCILE, *base, "--exceptions", str(exceptions))
        if accepted.returncode != 0:
            raise AssertionError(accepted.stderr or accepted.stdout)
        verified = run(
            VERIFY,
            str(evidence),
            "--expected-profile",
            "premium-3node",
            "--expected-commit",
            COMMIT,
        )
        if verified.returncode != 0:
            raise AssertionError(verified.stderr or verified.stdout)

        accepted_document = json.loads(evidence.read_text(encoding="utf-8"))
        exception_index = next(
            index
            for index, item in enumerate(accepted_document["images"])
            if item["exception"] is not None
        )

        stale_document = json.loads(json.dumps(accepted_document))
        stale_document["live"]["capturedAt"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
        stale_evidence = write(root / "rendered/supply-chain/stale-evidence.json", stale_document)
        stale = run(VERIFY, str(stale_evidence))
        if stale.returncode == 0 or "live.capturedAt is stale" not in stale.stderr:
            raise AssertionError("stale live image capture passed independent verification")

        expired_evidence_document = json.loads(json.dumps(accepted_document))
        expired_evidence_document["images"][exception_index]["exception"]["createdAt"] = (
            today - timedelta(days=30)
        ).isoformat()
        expired_evidence_document["images"][exception_index]["exception"]["expiresAt"] = (
            today - timedelta(days=1)
        ).isoformat()
        expired_evidence = write(
            root / "rendered/supply-chain/expired-evidence.json",
            expired_evidence_document,
        )
        expired_verified = run(VERIFY, str(expired_evidence))
        if expired_verified.returncode == 0 or "exception is expired" not in expired_verified.stderr:
            raise AssertionError("expired embedded exception passed independent verification")

        self_approved_document = json.loads(json.dumps(accepted_document))
        exception = self_approved_document["images"][exception_index]["exception"]
        exception["approvedBy"] = exception["owner"]
        self_approved = write(
            root / "rendered/supply-chain/self-approved-evidence.json",
            self_approved_document,
        )
        self_approved_result = run(VERIFY, str(self_approved))
        if (
            self_approved_result.returncode == 0
            or "owner and approver must differ" not in self_approved_result.stderr
        ):
            raise AssertionError("self-approved embedded exception passed verification")

        bad_summary_document = json.loads(json.dumps(accepted_document))
        bad_summary_document["summary"]["signatureVerifiedImages"] += 1
        bad_summary = write(
            root / "rendered/supply-chain/bad-summary-evidence.json",
            bad_summary_document,
        )
        bad_summary_result = run(VERIFY, str(bad_summary))
        if (
            bad_summary_result.returncode == 0
            or "summary.signatureVerifiedImages" not in bad_summary_result.stderr
        ):
            raise AssertionError("inconsistent signature summary passed independent verification")

        no_exception = run(RECONCILE, *base)
        if no_exception.returncode == 0 or "coverage is incomplete" not in no_exception.stderr:
            raise AssertionError("outside-registry image passed without an admission exception")

        unsigned_report = write(
            root / "rendered/supply-chain/unsigned.json",
            {
                "schemaVersion": 1,
                "verifiedImages": [
                    {"image": UPSTREAM_DIGEST, "key": "upstream.pub", "verified": True}
                ],
            },
        )
        unsigned_args = list(base)
        unsigned_args[unsigned_args.index(str(signatures))] = str(unsigned_report)
        unsigned_private = run(RECONCILE, *unsigned_args, "--exceptions", str(exceptions))
        if unsigned_private.returncode == 0 or "coverage is incomplete" not in unsigned_private.stderr:
            raise AssertionError("unsigned private-registry image passed reconciliation")

        original_manifest = json.loads(manifest.read_text(encoding="utf-8"))
        original_manifest["spec"]["template"]["spec"]["containers"].append(
            {"name": "dormant", "image": "quay.io/example/dormant:1.0"}
        )
        write(manifest, original_manifest)
        unresolved = run(RECONCILE, *base, "--exceptions", str(exceptions))
        if unresolved.returncode == 0 or "neither observed live nor resolved" not in unresolved.stderr:
            raise AssertionError("unresolved rendered image passed reconciliation")
        original_manifest["spec"]["template"]["spec"]["containers"].pop()
        write(manifest, original_manifest)

        exception_document = json.loads(exceptions.read_text(encoding="utf-8"))
        exception_document["exceptions"][0]["expiresAt"] = (today - timedelta(days=1)).isoformat()
        exception_document["exceptions"][0]["createdAt"] = (today - timedelta(days=30)).isoformat()
        write(exceptions, exception_document)
        expired = run(RECONCILE, *base, "--exceptions", str(exceptions))
        if expired.returncode == 0 or "is expired" not in expired.stderr:
            raise AssertionError("expired image exception passed reconciliation")

        invalid_pods = write(root / "invalid-pods.json", pod_list("registry.example.test/app:v1"))
        invalid_capture = run(
            CAPTURE,
            "--pods-json",
            str(invalid_pods),
            "--output",
            str(root / "invalid-live.json"),
        )
        if invalid_capture.returncode == 0 or "unresolved runtime image IDs" not in invalid_capture.stderr:
            raise AssertionError("tag-only runtime image ID passed capture")

    print("Rendered/live image inventory reconciliation self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
