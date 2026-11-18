#!/usr/bin/env python3
"""Prepare an optional merged-Gerrit CI-to-Argo-CD branch handoff package."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from bounded_file import read_bounded_text
import gerrit_gitops_handoff as adapter
from render_gerrit_router import write_bundle
from strict_json import loads_strict_json
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter


ROOT = Path(__file__).resolve().parents[1]


def render(bundle, plan):
    report, contract, _ = reporter.load_bundle(bundle, plan.get("reporter_contract_sha256"))
    adapter.validate(plan, report, contract)
    names = (*bridge.PROGRAMS, *reporter.EXTRA_PROGRAMS, "run-template.json", "gerrit-event-contract.json", "gerrit-report-contract.json")
    files = {name: read_bounded_text(bundle / name, encoding="utf-8") for name in names}
    files.update({name: read_bounded_text(ROOT / "scripts" / name, encoding="utf-8") for name in adapter.PROGRAMS})
    chosen = {"schema_version": 1, "plan": plan, "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
    files["gerrit-handoff-contract.json"] = bridge.common.canonical(chosen).decode() + "\n"
    files["handoff-status.json"] = json.dumps({"prepared": True, "runtime": contract["runtime"], "contract_sha256": hashlib.sha256(files["gerrit-handoff-contract.json"].encode()).hexdigest(),
        "merged_exact_head_required": True, "controller_verified": False, "full_stack_verified": False}, sort_keys=True, indent=2) + "\n"
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True); parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = loads_strict_json(read_bounded_text(args.plan, encoding="utf-8"))
        write_bundle(render(args.bundle_dir, plan), args.output_dir, private_root=ROOT / "private")
        print(json.dumps({"prepared": True, "controller_verified": False, "full_stack_verified": False})); return 0
    except Exception:
        print("Gerrit handoff preparation stopped; private plan and API details suppressed.", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
