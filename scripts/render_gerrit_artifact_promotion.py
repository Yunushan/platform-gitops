#!/usr/bin/env python3
"""Render an opt-in image-bound merged-Gerrit promotion package."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import gerrit_artifact_promotion as promotion
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter
from bounded_file import read_bounded_text
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json

ROOT = Path(__file__).resolve().parents[1]


def render(bundle, plan):
    report, contract, _ = reporter.load_bundle(bundle, plan.get("reporter_contract_sha256"))
    promotion.validate(plan, report, contract)
    names = (*bridge.programs(contract["plan"]), *reporter.EXTRA_PROGRAMS,
        "run-template.json", "gerrit-event-contract.json", "gerrit-report-contract.json")
    files = {name: read_bounded_text(bundle / name, encoding="utf-8") for name in names}
    files.update({name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in promotion.programs(plan)})
    selected = {"schema_version": 1, "plan": plan,
        "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
    files[promotion.CONTRACT] = bridge.common.canonical(selected).decode() + "\n"
    files["artifact-promotion-status.json"] = json.dumps({"prepared": True, "runtime": contract["runtime"],
        "contract_sha256": hashlib.sha256(files[promotion.CONTRACT].encode()).hexdigest(),
        "merged_exact_head_required": True, "artifact_receipt_required": True, "signed_ci_binding_required": plan["schema_version"] == 3, "controller_verified": False,
        "deployment_rollout_verified": False, "full_stack_verified": False}, sort_keys=True, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(args.bundle_dir, plan), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"prepared": True, "controller_verified": False, "full_stack_verified": False})); return 0
    except Exception:
        print("Artifact promotion preparation stopped; private plans and targets suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
