"""Build a private Kubeconform schema catalog from the profile's rendered CRDs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from strict_yaml import loads_strict_yaml_all


NATIVE_QUALIFIED_API_GROUPS = {
    "admission.k8s.io", "admissionregistration.k8s.io", "apidiscovery.k8s.io",
    "apiextensions.k8s.io", "apiregistration.k8s.io", "authentication.k8s.io",
    "authorization.k8s.io", "certificates.k8s.io", "coordination.k8s.io",
    "discovery.k8s.io", "events.k8s.io", "flowcontrol.apiserver.k8s.io",
    "internal.apiserver.k8s.io", "meta.k8s.io", "networking.k8s.io", "node.k8s.io",
    "rbac.authorization.k8s.io", "resource.k8s.io", "scheduling.k8s.io",
    "storage.k8s.io", "storagemigration.k8s.io",
}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
KIND = re.compile(r"[A-Za-z][A-Za-z0-9]*")
WINDOWS_DEVICE = re.compile(r"(?:con|aux|prn|nul|com[1-9]|lpt[1-9])", re.I)
OPENAPI_FIELDS = {
    "additionalProperties", "allOf", "anyOf", "default", "description", "enum", "example",
    "exclusiveMaximum", "exclusiveMinimum", "externalDocs", "format", "items", "maximum",
    "maxItems", "maxLength", "maxProperties", "minimum", "minItems", "minLength",
    "minProperties", "multipleOf", "not", "nullable", "oneOf", "pattern", "properties",
    "required", "title", "type", "uniqueItems", "x-kubernetes-embedded-resource",
    "x-kubernetes-int-or-string", "x-kubernetes-list-map-keys", "x-kubernetes-list-type",
    "x-kubernetes-map-type", "x-kubernetes-preserve-unknown-fields", "x-kubernetes-validations",
}


def is_native_api_group(group: str) -> bool:
    return "." not in group or group in NATIVE_QUALIFIED_API_GROUPS


def crd_schema_location(directory: Path) -> str:
    return str(directory.resolve() / "{{.Group}}" / "{{.ResourceAPIVersion}}" / "{{.ResourceKind}}.json")


def _validate_gvk(api_version: str, kind: str) -> tuple[str, str]:
    if not isinstance(api_version, str) or not isinstance(kind, str):
        raise ValueError("rendered Kubernetes object requires apiVersion and kind")
    group, slash, version = api_version.rpartition("/")
    if ((slash and not group) or len(group) > 253
            or (group and not all(DNS_LABEL.fullmatch(label) for label in group.split(".")))
            or not DNS_LABEL.fullmatch(version) or not KIND.fullmatch(kind) or len(kind) > 253
            or any(WINDOWS_DEVICE.fullmatch(part.split(".")[0]) for part in (group, version, kind))):
        raise ValueError("unsafe Kubernetes group, version or kind in rendered manifest")
    return group, version


def _json_schema(node: dict, *, resource: bool = False, structural: bool = True) -> dict:
    """Translate structural OpenAPI fields into local JSON Schema constraints."""
    if not isinstance(node, dict):
        raise ValueError("CRD schema node must be a mapping")
    # Kubernetes structural CRDs prohibit external references and schema dialects.
    # Check schema nodes only. Arbitrary user property names and example data are inert.
    if node.keys() - OPENAPI_FIELDS:
        raise ValueError("unsupported reference or schema keyword in CRD")
    result = dict(node)
    resource = resource or node.get("x-kubernetes-embedded-resource") is True
    properties = node.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("invalid CRD schema properties")
    if properties or resource:
        result["properties"] = {
            name: _json_schema(child, structural=structural and not (resource and name == "metadata"))
            for name, child in properties.items()
        }
    if resource:
        for name in ("apiVersion", "kind"):
            result["properties"].setdefault(name, {"type": "string"})
        result["properties"].setdefault("metadata", {"type": "object"})
    if "items" in node:
        result["items"] = _json_schema(node["items"], structural=structural)
    if isinstance(node.get("additionalProperties"), dict):
        result["additionalProperties"] = _json_schema(node["additionalProperties"], structural=structural)
    if (structural and node.get("type") == "object" and not node.get("x-kubernetes-preserve-unknown-fields")
            and "additionalProperties" not in node):
        result["additionalProperties"] = False
    # Composition clauses constrain the outer structure; closing their partial
    # properties would reject fields declared by the enclosing structural schema.
    for key in ("oneOf", "anyOf", "allOf"):
        if key in node:
            result[key] = [_json_schema(child, structural=False) for child in node[key]]
    if "not" in node:
        result["not"] = _json_schema(node["not"], structural=False)
    if node.get("x-kubernetes-list-type") == "set":
        result["uniqueItems"] = True
    if node.get("x-kubernetes-int-or-string") is True:
        result["type"] = ["integer", "string"]
    if node.get("nullable") is True:
        if "type" in result:
            types = result["type"]
            result["type"] = [*types, "null"] if isinstance(types, list) else [types, "null"]
    for bound in ("minimum", "maximum"):
        key = "exclusive" + bound.capitalize()
        if type(result.get(key)) is bool:
            exclusive = result.pop(key)
            if exclusive:
                if bound not in result:
                    raise ValueError("exclusive CRD schema bound has no value")
                result[key] = result.pop(bound)
    return result


def build_crd_schema_catalog(manifests: list[Path], directory: Path) -> dict[tuple[str, str], Path]:
    try:
        return _build_catalog(manifests, directory)
    except (KeyError, AttributeError, TypeError, RecursionError) as exc:
        raise ValueError("malformed rendered CRD or Kubernetes object") from exc


def _build_catalog(manifests: list[Path], directory: Path) -> dict[tuple[str, str], Path]:
    """Read the complete profile before resolving custom resource versions."""
    if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or any(directory.iterdir()))):
        raise ValueError("CRD schema catalog requires an empty scratch directory")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    catalog = {}
    identities = {}
    fingerprints = {}
    observed = set()
    for manifest in manifests:
        for doc in loads_strict_yaml_all(read_bounded_bytes(manifest), yaml_12=True):
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise ValueError("rendered Kubernetes document must be a mapping")
            api, kind = doc.get("apiVersion"), doc.get("kind")
            _validate_gvk(api, kind)
            observed.add((api, kind))
            if (api, kind) != ("apiextensions.k8s.io/v1", "CustomResourceDefinition"):
                continue
            spec = doc.get("spec") or {}
            crd_group = spec["group"]
            crd_kind = spec["names"]["kind"]
            if not isinstance(crd_group, str) or is_native_api_group(crd_group):
                raise ValueError("rendered CRD cannot provide a native Kubernetes schema")
            _validate_gvk(f"{crd_group}/v1", crd_kind)
            identity = (crd_group, crd_kind.lower())
            digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).digest()
            if identity in identities:
                if fingerprints[identity] != digest:
                    raise ValueError("conflicting rendered CRDs for the same group and kind")
                continue
            identities[identity] = crd_kind
            fingerprints[identity] = digest
            versions = set()
            for version in spec["versions"]:
                name = version["name"]
                crd_api = f"{crd_group}/{name}"
                _validate_gvk(crd_api, crd_kind)
                if name in versions or type(version.get("served")) is not bool:
                    raise ValueError("ambiguous version in rendered CRD")
                versions.add(name)
                if not version["served"]:
                    continue
                source = (version.get("schema") or {}).get("openAPIV3Schema")
                if not isinstance(source, dict) or source.get("type") != "object":
                    raise ValueError("served CRD version requires a structural object schema")
                schema = _json_schema(source, resource=True)
                # Kubeconform defaults to draft 4. Use its built-in draft 7
                # dialect for converted numeric exclusive bounds and const.
                schema["$schema"] = "http://json-schema.org/draft-07/schema#"
                schema["properties"]["apiVersion"]["const"] = crd_api
                schema["properties"]["kind"]["const"] = crd_kind
                path = directory / crd_group / name / (crd_kind.lower() + ".json")
                atomic_write_text(path, json.dumps(schema, separators=(",", ":")), replace=False)
                catalog[(crd_api, crd_kind)] = path
    for api, kind in observed:
        group = api.partition("/")[0]
        if (group, kind.lower()) in identities and (api, kind) not in catalog:
            raise ValueError(f"rendered object uses an unserved version or incorrect kind: {api}/{kind}")
    return catalog
