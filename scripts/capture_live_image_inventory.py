#!/usr/bin/env python3
"""Capture a sanitized, digest-bound inventory from Kubernetes Pod JSON."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from atomic_file import atomic_write_text
from bounded_subprocess import BoundedSubprocessError, run_bounded
from subprocess_timeout import bounded_timeout_seconds


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_GROUPS = (
    ("init", "initContainers", "initContainerStatuses"),
    ("container", "containers", "containerStatuses"),
    ("ephemeral", "ephemeralContainers", "ephemeralContainerStatuses"),
)
KUBECTL_TIMEOUT_SECONDS = 120


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_from_image(image: str) -> str:
    value = image.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("@", 1)[0]
    slash = value.rfind("/")
    colon = value.rfind(":")
    if colon > slash:
        value = value[:colon]
    parts = value.split("/")
    first = parts[0]
    if "." not in first and ":" not in first and first != "localhost":
        if len(parts) == 1:
            parts = ["docker.io", "library", *parts]
        else:
            parts = ["docker.io", *parts]
    if parts[0] == "index.docker.io":
        parts[0] = "docker.io"
    return "/".join(parts).lower()


def digest_image(spec_image: str, image_id: str) -> str:
    value = image_id.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    if "@" in value:
        repository, digest = value.rsplit("@", 1)
        if not DIGEST_RE.fullmatch(digest.lower()):
            raise ValueError(f"runtime image ID is not SHA-256 bound: {image_id}")
        return f"{repository_from_image(repository)}@{digest.lower()}"
    if DIGEST_RE.fullmatch(value.lower()):
        return f"{repository_from_image(spec_image)}@{value.lower()}"
    raise ValueError(f"runtime image ID is not pullable by SHA-256 digest: {image_id}")


def status_map(pod: dict[str, Any], field: str) -> dict[str, dict[str, Any]]:
    statuses = pod.get("status", {}).get(field, []) or []
    return {
        str(item.get("name", "")): item
        for item in statuses
        if isinstance(item, dict) and item.get("name")
    }


def capture(document: Any, *, cluster_uid: str = "") -> dict[str, Any]:
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError("Pod input must be a Kubernetes List with an items array")

    containers: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for pod in document["items"]:
        if not isinstance(pod, dict):
            raise ValueError("Pod list contains a non-object item")
        metadata = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        namespace = str(metadata.get("namespace", "default"))
        pod_name = str(metadata.get("name", ""))
        node = str(spec.get("nodeName", ""))
        phase = str(status.get("phase", "Unknown"))
        if not pod_name:
            raise ValueError("Pod item is missing metadata.name")

        for container_type, spec_field, status_field in CONTAINER_GROUPS:
            statuses = status_map(pod, status_field)
            for container in spec.get(spec_field, []) or []:
                if not isinstance(container, dict):
                    continue
                name = str(container.get("name", ""))
                spec_image = str(container.get("image", "")).strip()
                runtime_status = statuses.get(name, {})
                image_id = str(runtime_status.get("imageID", "")).strip()
                base = {
                    "namespace": namespace,
                    "pod": pod_name,
                    "node": node,
                    "phase": phase,
                    "containerType": container_type,
                    "container": name,
                    "specImage": spec_image,
                }
                if not name or not spec_image or not image_id:
                    unresolved.append(
                        {
                            **base,
                            "reason": "missing-name-spec-image-or-runtime-image-id",
                        }
                    )
                    continue
                try:
                    resolved = digest_image(spec_image, image_id)
                except ValueError as exc:
                    unresolved.append({**base, "reason": str(exc)})
                    continue
                containers.append(
                    {
                        **base,
                        "imageID": image_id,
                        "digestImage": resolved,
                        "ready": runtime_status.get("ready") is True,
                    }
                )

    containers.sort(
        key=lambda item: (
            item["namespace"],
            item["pod"],
            item["containerType"],
            item["container"],
        )
    )
    unresolved.sort(
        key=lambda item: (
            item["namespace"],
            item["pod"],
            item["containerType"],
            item["container"],
        )
    )
    unique_images = sorted({item["digestImage"] for item in containers})
    return {
        "schemaVersion": 1,
        "capturedAt": utc_now(),
        "clusterUid": cluster_uid,
        "containers": containers,
        "unresolved": unresolved,
        "summary": {
            "pods": len(document["items"]),
            "containers": len(containers),
            "uniqueDigestImages": len(unique_images),
            "unresolved": len(unresolved),
        },
    }


def read_pods(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.pods_json:
        raw = args.pods_json.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    command = [args.kubectl, "--kubeconfig", args.kubeconfig, "get", "pods", "-A", "-o", "json"]
    timeout = bounded_timeout_seconds(
        KUBECTL_TIMEOUT_SECONDS,
        "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
    )
    try:
        result = run_bounded(
            command,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"kubectl Pod inventory query timed out after {timeout:g} seconds"
        ) from None
    except (BoundedSubprocessError, ValueError) as exc:
        raise RuntimeError(f"kubectl Pod inventory output rejected: {exc}") from None
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return json.loads(result.stdout), hashlib.sha256(result.stdout).hexdigest()


def cluster_uid(args: argparse.Namespace) -> str:
    if args.cluster_uid:
        return args.cluster_uid
    if not args.kubectl:
        return ""
    timeout = bounded_timeout_seconds(
        KUBECTL_TIMEOUT_SECONDS,
        "PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS",
    )
    try:
        result = run_bounded(
            [
                args.kubectl,
                "--kubeconfig",
                args.kubeconfig,
                "get",
                "namespace",
                "kube-system",
                "-o",
                "jsonpath={.metadata.uid}",
            ],
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"kubectl cluster UID query timed out after {timeout:g} seconds"
        ) from None
    except (BoundedSubprocessError, ValueError) as exc:
        raise RuntimeError(f"kubectl cluster UID output rejected: {exc}") from None
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "failed to read kube-system namespace UID")
    return result.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pods-json", type=Path)
    source.add_argument("--kubectl")
    parser.add_argument("--kubeconfig", default="/etc/rancher/rke2/rke2.yaml")
    parser.add_argument("--cluster-uid", default="")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-unresolved", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        pods, source_hash = read_pods(args)
        document = capture(pods, cluster_uid=cluster_uid(args))
        document["sourceSha256"] = source_hash
        atomic_write_text(args.output, json.dumps(document, indent=2) + "\n")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Live image inventory capture failed: {exc}", file=sys.stderr)
        return 1
    summary = document["summary"]
    print(
        "Live image inventory captured: "
        f"pods={summary['pods']} containers={summary['containers']} "
        f"images={summary['uniqueDigestImages']} unresolved={summary['unresolved']}"
    )
    if document["unresolved"] and not args.allow_unresolved:
        print("Live image inventory contains unresolved runtime image IDs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
