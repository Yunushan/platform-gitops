#!/usr/bin/env python3
"""Read explicitly reviewed upstream CloudNativePG release manifests offline."""

import hashlib
import json

from strict_yaml import loads_strict_yaml_all


RELEASES = {
    "1.30.1": {
        "manifest_sha256": "37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb",
        "image": "ghcr.io/cloudnative-pg/cloudnative-pg@sha256:b0f9805bd5f98592e7320b5ea624dd10a7a6ef8e477df5650655c94f0b242497",
        "source": "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.1/cnpg-1.30.1.yaml",
    },
}


def objects(version, payload):
    if (not isinstance(version, str) or version not in RELEASES or not isinstance(payload, bytes) or not 0 < len(payload) <= 2 * 1024 * 1024
            or hashlib.sha256(payload).hexdigest() != RELEASES[version]["manifest_sha256"]):
        raise ValueError("Supply the exact reviewed CloudNativePG patch release manifest")
    values = loads_strict_yaml_all(payload.decode("utf-8"))
    if (not 1 <= len(values) <= 128 or any(not isinstance(value, dict) or not isinstance(value.get("metadata"), dict) for value in values)
            or len({(value.get("apiVersion"), value.get("kind"), value["metadata"].get("namespace"), value["metadata"].get("name")) for value in values}) != len(values)):
        raise ValueError("The reviewed operator inventory is incomplete or duplicated")
    return values


def cluster_schema(version, payload):
    matches = [value for value in objects(version, payload)
               if value.get("kind") == "CustomResourceDefinition" and value["metadata"].get("name") == "clusters.postgresql.cnpg.io"]
    if len(matches) != 1:
        raise ValueError("The reviewed release must define exactly one Cluster API")
    versions = [value for value in matches[0]["spec"]["versions"]
                if value.get("name") == "v1" and value.get("served") is True and value.get("storage") is True]
    if len(versions) != 1 or not isinstance(versions[0].get("schema", {}).get("openAPIV3Schema"), dict):
        raise ValueError("The reviewed release must serve and store the expected Cluster schema")
    return json.dumps(versions[0]["schema"]["openAPIV3Schema"], sort_keys=True, separators=(",", ":")) + "\n"
