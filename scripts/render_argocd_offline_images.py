#!/usr/bin/env python3
"""Pin cached Argo CD bootstrap workloads to images preloaded on the nodes."""

import argparse
import os
import re
import stat
import sys

import yaml

from strict_json import loads_strict_json
from strict_yaml import loads_strict_yaml_all


IMAGE_TAG = re.compile(r"[a-z0-9][a-z0-9.-]+/[a-z0-9_./-]+:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def read_file(path, limit):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError("Use a bounded regular file")
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("File exceeds input limit")
        return payload


def parse_lock(payload):
    images = loads_strict_json(payload)
    if (not isinstance(images, dict) or not images or any(
        not IMAGE_TAG.fullmatch(tag) or not isinstance(digest, str) or not SHA256.fullmatch(digest)
        for tag, digest in images.items()
    )):
        raise ValueError("Map exact upstream image tags to reviewed SHA-256 digests")
    return images


def workload_documents(payload):
    for raw in re.split(rb"(?m)^---[ \t]*\r?$", payload):
        obj, = loads_strict_yaml_all(raw) or [None]
        if obj is None:
            continue
        if not isinstance(obj, dict):
            raise ValueError("Expected a Kubernetes object")
        kind = obj["kind"]
        if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "ReplicaSet", "ReplicationController"}:
            yield raw, obj, None
            continue
        spec = obj["spec"]
        if kind == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        if kind != "Pod":
            spec = spec["template"]["spec"]
        yield raw, obj, spec


def pod_containers(spec):
    for key in ("containers", "initContainers", "ephemeralContainers"):
        yield from spec.get(key, [])


def render(payload, images):
    documents = []
    for raw, obj, spec in workload_documents(payload):
        if spec is None:
            documents.append(raw.strip(b"\r\n"))
            continue
        for container in pod_containers(spec):
            tag = container["image"]
            digest = images.get(tag)
            if not digest:
                raise ValueError("The image lock is missing a bootstrap container")
            container.update(image=tag.rsplit(":", 1)[0] + "@" + digest, imagePullPolicy="Never")
        documents.append(yaml.safe_dump(obj, sort_keys=False).encode().rstrip())
    if not documents:
        raise ValueError("The bootstrap manifest is empty")
    return b"---\n" + b"\n---\n".join(documents) + b"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("manifest", nargs="+")
    args = parser.parse_args()
    if not args.check and len(args.manifest) != 1:
        parser.error("render exactly one manifest")
    try:
        images = parse_lock(read_file(args.lock, 64 * 1024))
        for manifest in args.manifest:
            output = render(read_file(manifest, 4 * 1024 * 1024), images)
            if not args.check:
                sys.stdout.buffer.write(output)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        print("Offline Argo CD requires a valid image lock covering every bootstrap image.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
