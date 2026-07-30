#!/usr/bin/env python3
"""Render and schema-validate a complete non-secret premium deployment profile."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile

from atomic_file import atomic_write_text
from bounded_file import read_bounded_text
from synthetic_private_profile import prepare_synthetic_private_profile
from validate_rendered_manifests import ROOT, application_sources, validate


OUTPUT_ROOT = ROOT / "rendered/synthetic-private-schema"
REPORT_SUFFIXES = (
    ".kubeconform.json",
    ".kubeconform.stderr.log",
    ".render.log",
)


def retained_summary(summary: dict[str, object]) -> dict[str, object]:
    retained = deepcopy(summary)
    for collection in ("rendered", "failures"):
        records = retained.get(collection)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            for field in ("log", "report"):
                value = record.get(field)
                if isinstance(value, str) and value:
                    record[field] = f"reports/{Path(value).name}"
    retained["artifactPolicy"] = "sanitized-reports-only"
    return retained


def retain_sanitized_artifacts(
    schema_output: Path,
    output_root: Path,
    summary: dict[str, object],
) -> None:
    """Copy only known-safe reports out of the temporary rendered checkout."""

    if output_root.exists():
        shutil.rmtree(output_root)
    reports_source = schema_output / "reports"
    reports_destination = output_root / "schema-validation/reports"
    reports_destination.mkdir(parents=True)
    if reports_source.is_dir():
        for report in reports_source.rglob("*"):
            if not report.is_file():
                continue
            relative = report.relative_to(reports_source)
            if len(relative.parts) != 1 or not report.name.endswith(REPORT_SUFFIXES):
                raise RuntimeError(f"unexpected rendered-schema report artifact: {relative}")
            shutil.copy2(report, reports_destination / report.name)
    atomic_write_text(
        output_root / "schema-validation/summary.json",
        json.dumps(retained_summary(summary), indent=2, sort_keys=True) + "\n",
    )


def main() -> int:
    output_root = OUTPUT_ROOT.resolve()
    rendered_root = (ROOT / "rendered").resolve()
    if rendered_root not in output_root.parents:
        print("synthetic profile output escaped rendered/", file=sys.stderr)
        return 2
    if output_root.exists():
        shutil.rmtree(output_root)

    with tempfile.TemporaryDirectory(prefix="platform-synthetic-private-schema-") as temporary:
        repo_root = Path(temporary) / "repo"
        try:
            prepare_synthetic_private_profile(repo_root, source_root=ROOT)
        except (OSError, RuntimeError, ValueError) as exc:
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
        schema_output = repo_root / "rendered/schema-validation"
        summary_path = schema_output / "summary.json"
        summary = (
            json.loads(read_bounded_text(summary_path))
            if summary_path.is_file()
            else {}
        )
        if summary:
            try:
                retain_sanitized_artifacts(schema_output, output_root, summary)
            except (OSError, RuntimeError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
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
