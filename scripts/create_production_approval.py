#!/usr/bin/env python3
"""Create the exact production-approval document an independent approver signs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from strict_json import loads_strict_json
import verify_production_approval as approval
import verify_production_evidence as production_evidence


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-evidence", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--public-key-sha256", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-age-days", type=int, default=7)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_age_days <= 0:
        print("--max-age-days must be greater than zero", file=sys.stderr)
        return 2
    try:
        document = loads_strict_json(read_bounded_text(args.production_evidence))
        if not isinstance(document, dict):
            raise approval.ApprovalError("production evidence must be a JSON object")
        production_evidence.validate_evidence(
            document,
            root=ROOT,
            now=datetime.now(timezone.utc),
            max_age_days=args.max_age_days,
        )
        key_sha256 = approval.artifact_sha256(args.public_key)
        if key_sha256 != args.public_key_sha256:
            raise approval.ApprovalError(
                "production approval public key does not match its pinned SHA-256"
            )
        retained = approval.build_approval_document(
            document,
            production_sha256=approval.artifact_sha256(args.production_evidence),
            approval_key_sha256=key_sha256,
            approver=args.approver,
        )
        atomic_write_text(args.output, json.dumps(retained, indent=2) + "\n")
    except (
        OSError,
        json.JSONDecodeError,
        approval.ApprovalError,
        production_evidence.EvidenceError,
    ) as exc:
        print(f"Production approval creation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Production approval document created for signing: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
