#!/usr/bin/env python3
"""Render and schema-validate a complete non-secret premium deployment profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from synthetic_private_profile import prepare_synthetic_private_profile
from validate_rendered_manifests import ROOT, application_sources, validate


OUTPUT_ROOT = ROOT / "rendered/synthetic-private-schema"


def remove_generated_sources(repo_root: Path) -> None:
    for generated_source in (
        repo_root / "gitops",
        repo_root / "inventory",
        repo_root / "private",
    ):
        if generated_source.exists():
            shutil.rmtree(generated_source)


def main() -> int:
    output_root = OUTPUT_ROOT.resolve()
    rendered_root = (ROOT / "rendered").resolve()
    if rendered_root not in output_root.parents:
        print("synthetic profile output escaped rendered/", file=sys.stderr)
        return 2
    if output_root.exists():
        shutil.rmtree(output_root)

    repo_root = output_root / "repo"
    try:
        prepare_synthetic_private_profile(repo_root, source_root=ROOT)
    except (OSError, RuntimeError, ValueError) as exc:
        remove_generated_sources(repo_root)
        print(str(exc), file=sys.stderr)
        return 1

    args = argparse.Namespace(
        profile=["premium-3node"],
        allow_incomplete=False,
        require_complete=True,
        kubernetes_version=None,
        output_dir="rendered/schema-validation",
    )
    expected = {name for name, _ in application_sources("premium-3node", repo_root)}
    result = validate(args, root=repo_root)
    summary_path = repo_root / "rendered/schema-validation/summary.json"
    summary = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.is_file()
        else {}
    )
    remove_generated_sources(repo_root)
    if result != 0 or not summary:
        return result or 1

    rendered = {
        str(record.get("application"))
        for record in summary.get("rendered", [])
        if isinstance(record, dict)
    }
    if summary.get("skipped") or summary.get("failures") or rendered != expected:
        print(
            "synthetic premium validation did not prove every application: "
            f"expected={len(expected)} rendered={len(rendered)} "
            f"skipped={len(summary.get('skipped', []))} "
            f"failures={len(summary.get('failures', []))}",
            file=sys.stderr,
        )
        return 1

    print(
        "synthetic_private_profile=passed "
        f"profile=premium-3node applications={len(rendered)} placeholders=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
