#!/usr/bin/env python3
"""Verify the reviewed Git revision and source for every selected platform application."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


def application_plan(root: Path, profile: str) -> dict:
    from render_deployable_gitops_apps import selected_application_documents
    from strict_yaml import loads_strict_yaml_all

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", profile):
        raise ValueError("invalid GitOps profile name")
    selection = "default" if profile == "base" else profile
    expected = {}
    for source in selected_application_documents(root, selection, "https://example.invalid/platform.git"):
        document, = loads_strict_yaml_all(source, yaml_12=True)
        name = document["metadata"]["name"]
        spec = document["spec"]
        if name in expected or spec.get("sources") or not spec["source"].get("path"):
            raise ValueError("profile must select distinct single-source Git applications")
        expected[name] = {
            "source": source_settings(spec["source"]),
            "destination": spec["destination"],
            "project": spec["project"],
        }
    if not expected:
        raise ValueError("profile does not select any Git applications")
    return expected


def source_settings(source: dict) -> dict:
    return {key: value for key, value in source.items() if key not in {"repoURL", "targetRevision"}}


def verify_revision(snapshot: dict, expected: dict, commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected revision must be a full lowercase Git commit")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("expected application plan is empty or invalid")
    live = {}
    for application in snapshot["items"]:
        name = application["metadata"]["name"]
        if name in live:
            raise ValueError("Argo CD snapshot contains duplicate application names")
        live[name] = application

    for name, plan in expected.items():
        application = live.get(name)
        if application is None:
            raise ValueError(f"{name}: required application is absent")
        spec = application["spec"]
        source = spec.get("source") or {}
        if (spec.get("sources") or source_settings(source) != plan["source"]
                or spec.get("destination") != plan["destination"] or spec.get("project") != plan["project"]):
            raise ValueError(f"{name}: source, destination, or project differs from the reviewed profile")

        status = application["status"]
        sync = status.get("sync") or {}
        comparison = sync.get("comparedTo") or {}
        compared_source = comparison.get("source") or {}
        if (comparison.get("sources") or source_settings(compared_source) != plan["source"]
                or not source.get("repoURL") or compared_source.get("repoURL") != source["repoURL"]
                or compared_source.get("targetRevision") not in {source.get("targetRevision"), commit}
                or comparison.get("destination") != plan["destination"]):
            raise ValueError(f"{name}: sync status belongs to a different source or destination")
        if sync.get("revisions") or sync.get("revision") != commit:
            raise ValueError(f"{name}: deployed revision differs from the reviewed Git commit")
        phase = (status.get("operationState") or {}).get("phase", "")
        conditions = status.get("conditions") or []
        if (sync.get("status") != "Synced" or status.get("health", {}).get("status") != "Healthy"
                or application.get("operation") or phase not in {"", "Succeeded", "Failed", "Error"}
                or any(condition.get("type") != "OrphanedResourceWarning" for condition in conditions)):
            raise ValueError(f"{name}: application is unhealthy, unsynced, or has an active operation or condition")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", metavar="PROFILE")
    args = parser.parse_args()
    if args.plan:
        try:
            print(json.dumps(application_plan(Path(__file__).resolve().parents[1], args.plan), sort_keys=True))
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            print(f"Cannot build release revision plan: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        profile = os.environ["PLATFORM_PROFILE"]
        commit = os.environ["PLATFORM_APP_HEALTH_EXPECTED_REVISION"]
        expected = json.loads(os.environ["PLATFORM_APP_HEALTH_REVISION_PLAN"])
        verify_revision(json.load(sys.stdin), expected, commit)
    except ValueError as exc:
        print(f"Argo CD release revision verification failed: {exc}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, AttributeError):
        print("Argo CD release revision verification received incomplete or malformed status", file=sys.stderr)
        return 1
    print(f"argocd_revision=verified profile={profile} commit={commit} applications={len(expected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
