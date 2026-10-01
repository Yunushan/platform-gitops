#!/usr/bin/env python3
"""Read Argo CD application status; emit allowlisted signals, never raw private diagnostics."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from bounded_file import read_bounded_bytes
from bounded_subprocess import run_bounded
from strict_json import loads_strict_json


HEALTH = {"Healthy", "Degraded", "Progressing", "Missing", "Suspended", "Unknown"}
SYNC = {"Synced", "OutOfSync", "Unknown"}
PHASE = {"Running", "Succeeded", "Failed", "Error", "Terminating"}
RESOURCE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Pod", "Job", "Certificate", "PersistentVolumeClaim", "Ingress"}
CATEGORIES = {
    "unresolved_deployment_placeholder": r"<[A-Z0-9_]+>",
    "immutable_resource_change": r"updates to statefulset spec|field is immutable|immutable field|Forbidden: updates to",
    "rollout_deadline_exceeded": r"exceeded its progress deadline|ProgressDeadlineExceeded",
    "admission_webhook_unavailable": r"failed calling webhook|failed to call webhook",
    "scheduling_or_storage": r"FailedScheduling|FailedMount|FailedAttachVolume|Insufficient (?:cpu|memory)|unbound.*claim|diskpressure|no space left",
    "image_or_container_failure": r"ImagePullBackOff|ErrImagePull|CrashLoopBackOff|OOMKilled",
    "manifest_generation_or_comparison": r"ComparisonError|manifest generation|failed to generate|failed to load target state",
    "invalid_resource": r"is invalid|Invalid value|invalid patch",
}
GUIDANCE = {
    "unresolved_deployment_placeholder": "Render missing private settings in the deployment repository; run the strict profile check before publishing.",
    "immutable_resource_change": "Review the live/desired StatefulSet or PVC diff and verified backups; do not force-replace or prune storage.",
    "rollout_deadline_exceeded": "Inspect the affected pod's waiting reason and events locally; fix the cause before restarting.",
    "admission_webhook_unavailable": "Restore webhook/service connectivity; do not disable admission policies.",
    "scheduling_or_storage": "Inspect node capacity, scheduling constraints, PVCs and replica health before changing storage.",
    "image_or_container_failure": "Check pod events and private application logs; verify image pull access and application configuration.",
    "manifest_generation_or_comparison": "Check repository access and rendering errors locally without printing credentials.",
    "invalid_resource": "Review the rejected manifest locally; correct the validated field in Git.",
}


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def records(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def summarize_application(app: dict, expected_revision: str = "") -> dict:
    status = mapping(app.get("status"))
    health = mapping(status.get("health")).get("status")
    sync = mapping(status.get("sync")).get("status")
    health = health if isinstance(health, str) and health in HEALTH else "Unknown"
    sync = sync if isinstance(sync, str) and sync in SYNC else "Unknown"
    operation = mapping(status.get("operationState"))
    phase = operation.get("phase")
    phase = phase if isinstance(phase, str) and phase in PHASE else ("Unknown" if phase else "NotRecorded")
    conditions = records(status.get("conditions"))
    resources = records(status.get("resources"))
    messages = [item.get("message", "") for item in conditions]
    messages.extend((operation.get("message", ""), mapping(status.get("health")).get("message", "")))
    messages.extend(mapping(item.get("health")).get("message", "") for item in resources)
    messages.extend(item.get("message", "") for item in records(mapping(operation.get("syncResult")).get("resources")))
    signals = {
        key for key, pattern in CATEGORIES.items()
        if any(re.search(pattern, message, re.IGNORECASE) for message in messages if isinstance(message, str))
    }
    if any(item.get("type") == "ComparisonError" for item in conditions):
        signals.add("manifest_generation_or_comparison")
    name = mapping(app.get("metadata")).get("name", "")
    name = name if isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", name) else "redacted"
    unhealthy = Counter()
    for item in resources:
        resource_health = mapping(item.get("health")).get("status")
        if resource_health and resource_health != "Healthy":
            kind = item.get("kind")
            unhealthy[kind if isinstance(kind, str) and kind in RESOURCE_KINDS else "Other"] += 1
    errors = sum(isinstance(item.get("type"), str) and item["type"].endswith("Error") for item in conditions)
    revision = mapping(status.get("sync")).get("revision", "")
    return {
        "application": name,
        "health": health, "sync": sync, "last_operation": phase,
        "needs_attention": health != "Healthy" or sync != "Synced" or errors > 0,
        "error_conditions": errors,
        "orphan_warning": any(item.get("type") == "OrphanedResourceWarning" for item in conditions),
        "resources_requiring_prune": sum(item.get("requiresPruning") is True for item in resources),
        "unhealthy_resources_by_kind": dict(sorted(unhealthy.items())),
        "revision_matches_expected": (revision == expected_revision) if expected_revision else None,
        "failure_categories": sorted(signals),
        "next_checks": [GUIDANCE[key] for key in sorted(signals)],
    }


def diagnose(document: dict, applications: list[str], expected_revision: str = "") -> dict:
    if not isinstance(document, dict) or not isinstance(document.get("items"), list) or any(not isinstance(item, dict) for item in document["items"]):
        raise ValueError("Invalid application list")
    apps = records(document["items"])
    if applications:
        apps = [app for app in apps if mapping(app.get("metadata")).get("name") in applications]
        if {mapping(app.get("metadata")).get("name") for app in apps} != set(applications):
            raise ValueError("Requested application missing")
    if not apps:
        raise ValueError("Application list is empty; health cannot be established")
    summaries = sorted((summarize_application(app, expected_revision) for app in apps), key=lambda item: item["application"])
    attention = sum(item["needs_attention"] or item["revision_matches_expected"] is False for item in summaries)
    return {
        "read_only": True,
        "applications_checked": len(summaries), "applications_needing_attention": attention,
        "health_counts": dict(Counter(item["health"] for item in summaries)),
        "sync_counts": dict(Counter(item["sync"] for item in summaries)),
        "applications": summaries,
        "note": "Last-operation NotRecorded/Unknown is not Unknown health. OutOfSync needs diff review, not blanket sync/prune. Signals are candidates, not a complete root-cause diagnosis. Raw messages, endpoints and credentials were not printed.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Read an existing private kubectl JSON capture instead of contacting the cluster")
    parser.add_argument("--kubectl", default="/var/lib/rancher/rke2/bin/kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    parser.add_argument("--namespace", default="argocd")
    parser.add_argument("--application", action="append", default=[])
    parser.add_argument("--expected-revision", default="", help="Full reviewed Git commit ID to compare with live sync revisions")
    args = parser.parse_args(argv)
    if args.expected_revision and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.expected_revision):
        parser.error("--expected-revision must be a full Git commit ID")
    try:
        if args.input:
            raw = read_bounded_bytes(args.input, max_bytes=16 * 1024 * 1024)
        else:
            command = [args.kubectl, "--kubeconfig", args.kubeconfig, "--request-timeout=20s",
                       "-n", args.namespace, "get", "applications.argoproj.io", "-o", "json"]
            process = run_bounded(command, timeout=30, check=False, output_max_bytes=16 * 1024 * 1024)
            if process.returncode:
                raise ValueError("kubectl read failed")
            raw = process.stdout
        report = diagnose(loads_strict_json(raw), args.application, args.expected_revision)
        print(json.dumps(report, indent=2))
        return 2 if report["applications_needing_attention"] else 0
    except Exception:
        print("Application diagnostic could not establish live status; private details suppressed. No resources changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
