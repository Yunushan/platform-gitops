#!/usr/bin/env python3
"""Self-test strict supply-chain evidence validation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts/verify_supply_chain_evidence.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="platform-supply-chain-") as directory:
        root = Path(directory)
        sbom = root / "sbom.json"
        scorecard = root / "scorecard.json"
        signatures = root / "signatures.json"
        write(
            sbom,
            {
                "spdxVersion": "SPDX-2.3",
                "dataLicense": "CC0-1.0",
                "SPDXID": "SPDXRef-DOCUMENT",
                "name": "platform-gitops",
                "documentNamespace": "https://example.test/spdx/platform-gitops",
                "creationInfo": {
                    "created": "2026-01-01T00:00:00Z",
                    "creators": ["Tool: syft-test"],
                },
                "packages": [{"SPDXID": "SPDXRef-Package-test", "name": "test"}],
            },
        )
        write(scorecard, {"score": 8.5, "checks": [{"name": "Pinned-Dependencies"}]})
        write(
            signatures,
            {
                "schemaVersion": 1,
                "verifiedImages": [
                    {
                        "image": "registry.example.test/platform/app@sha256:" + "a" * 64,
                        "key": "release.pub",
                        "verified": True,
                    }
                ],
            },
        )

        result = run(
            "--sbom",
            str(sbom),
            "--scorecard",
            str(scorecard),
            "--signature-report",
            str(signatures),
            "--strict",
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        if "verified_images=1" not in result.stdout:
            raise AssertionError("strict success output is missing signature evidence")

        missing = run("--sbom", str(sbom), "--strict")
        if missing.returncode == 0 or "requires an OpenSSF Scorecard" not in missing.stderr:
            raise AssertionError("strict mode accepted missing Scorecard evidence")

        weak = dict(json.loads(scorecard.read_text(encoding="utf-8")))
        weak["score"] = 6.9
        write(scorecard, weak)
        below = run("--sbom", str(sbom), "--scorecard", str(scorecard), "--minimum-score", "7")
        if below.returncode == 0 or "below required" not in below.stderr:
            raise AssertionError("validator accepted a below-threshold Scorecard report")

        write(scorecard, {"score": 8.5, "checks": [{"name": "Pinned-Dependencies"}]})
        invalid_signatures = json.loads(signatures.read_text(encoding="utf-8"))
        invalid_signatures["verifiedImages"][0]["image"] = "registry.example.test/platform/app:v1"
        write(signatures, invalid_signatures)
        mutable = run(
            "--sbom",
            str(sbom),
            "--signature-report",
            str(signatures),
        )
        if mutable.returncode == 0 or "pinned by sha256 digest" not in mutable.stderr:
            raise AssertionError("validator accepted a tag-only Cosign image")

        empty_sbom = json.loads(sbom.read_text(encoding="utf-8"))
        empty_sbom["packages"] = []
        write(sbom, empty_sbom)
        empty = run("--sbom", str(sbom))
        if empty.returncode == 0 or "at least one package" not in empty.stderr:
            raise AssertionError("validator accepted an empty SBOM")

    print("Supply-chain evidence validator self-test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
