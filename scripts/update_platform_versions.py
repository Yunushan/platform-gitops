#!/usr/bin/env python3
"""Plan opt-in application image changes; --write edits local values, never the cluster."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys

import yaml

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes
from platform_release_policy import (
    CHANNELS, COMPONENTS, ReleasePolicyError, load_catalog, select_release, version_tuple,
)
from strict_yaml import loads_strict_yaml_all


ROOT = Path(__file__).resolve().parents[1]
PREMIUM_APPS = ROOT / "gitops/clusters/rke2-main/premium-3node/apps"
IMAGE_PATHS = {
    "forgejo": (("image", "tag"),),
    "woodpecker": (("server", "image", "tag"), ("agent", "image", "tag")),
    "argocd": (("global", "image", "tag"),),
}
ARGO_COMPONENTS = ("controller", "server", "repoServer", "applicationSet", "notifications", "dex")


def values_document(text: str) -> dict:
    try:
        docs = loads_strict_yaml_all(text)
        if len(docs) != 1 or not isinstance(docs[0], dict):
            raise ValueError()
        return docs[0]
    except (ValueError, yaml.YAMLError) as exc:
        raise ReleasePolicyError("Expected one unambiguous YAML values mapping; private contents suppressed") from exc


def mapping_at(document: dict, keys: tuple[str, ...]) -> dict:
    value = document
    for key in keys:
        value = value.get(key, {})
        if not isinstance(value, dict):
            raise ReleasePolicyError("Image values are not a mapping; no file changed")
    return value


def scalar_edit(text: str, keys: tuple[str, ...], tag: str) -> str:
    """Use validated YAML marks to preserve comments and every other scalar verbatim."""
    values_document(text)
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    for index, key in enumerate(keys):
        if not isinstance(node, yaml.MappingNode):
            raise ReleasePolicyError("Expected a block-style image mapping")
        pair = next(((k, v) for k, v in node.value if k.value == key), None)
        if pair is not None:
            if index == len(keys) - 1:
                scalar = pair[1]
                if not isinstance(scalar, yaml.ScalarNode) or scalar.style in {"|", ">"}:
                    raise ReleasePolicyError("Expected an image tag scalar")
                return text[:scalar.start_mark.index] + json.dumps(tag) + text[scalar.end_mark.index:]
            node = pair[1]
            continue
        # Only Argo's global.image/tag may be absent: chart appVersion is its current pin.
        if keys != ("global", "image", "tag") or index == 0 or node.flow_style or not node.value:
            raise ReleasePolicyError("Image tag is absent or uses an unsupported mapping layout")
        newline = "\r\n" if "\r\n" in text else "\n"
        remaining = keys[index:]
        block = ""
        for offset, field in enumerate(remaining):
            indent = "  " * (index + offset)
            block += indent + field + (": " + json.dumps(tag) if field == "tag" else ":") + newline
        position = node.end_mark.index
        prefix = newline if position and text[position - 1] != "\n" else ""
        return text[:position] + prefix + block + text[position:]
    raise ReleasePolicyError("Image tag could not be edited")


def current_tags(component: str, document: dict, catalog: dict) -> list[str]:
    result = []
    for path in IMAGE_PATHS[component]:
        image = mapping_at(document, path[:-1])
        tag = image.get("tag")
        if component == "argocd" and not tag:
            tag = catalog["components"][component]["pinned"]
        if not isinstance(tag, str) or not tag:
            raise ReleasePolicyError(f"{component} image tag is missing")
        version_tuple(component, tag)
        result.append(tag)
    return result


def update_values_text(component: str, text: str, target: str, *, current: list[str]) -> str:
    before = values_document(text)
    if all(tag == target for tag in current):
        return text
    for path in IMAGE_PATHS[component]:
        image = mapping_at(before, path[:-1])
        if any(image.get(key) for key in ("digest", "sha", "fullOverride")):
            raise ReleasePolicyError("Image has a digest pin or full override; review and update that image reference separately")
    if component == "forgejo" and target.endswith("-rootless") and mapping_at(before, ("image",)).get("rootless") is False:
        raise ReleasePolicyError("Keep the existing Forgejo image variant; a rootless conversion needs a separate migration")
    if component == "argocd":
        for section in ARGO_COMPONENTS:
            image = mapping_at(before, (section, "image"))
            if any(image.get(key) for key in ("tag", "digest", "sha")) and section != "dex":
                raise ReleasePolicyError("Argo CD component image overrides require review before a global image update")
    after = copy.deepcopy(before)
    rendered = text
    for path in IMAGE_PATHS[component]:
        container = after
        for key in path[:-1]:
            container = container.setdefault(key, {})
        container["tag"] = target
        rendered = scalar_edit(rendered, path, target)
    if values_document(rendered) != after:
        raise ReleasePolicyError("Version edit would alter unrelated values; no file changed")
    return rendered


def prepare_change(component: str, path: Path, channel: str, specific: str, catalog: dict) -> tuple[dict, str, str]:
    if path.is_symlink() or not path.is_file():
        raise ReleasePolicyError(f"{component} requires an existing regular values file")
    try:
        text = read_bounded_bytes(path, max_bytes=4 * 1024 * 1024).decode("utf-8")
    except (OSError, ValueError) as exc:
        raise ReleasePolicyError(f"Cannot read {component} values; private path/content suppressed") from exc
    document = values_document(text)
    tags = current_tags(component, document, catalog)
    target = select_release(component, channel=channel, specific=specific, current=tags[0], catalog=catalog)
    if any(version_tuple(component, target) < version_tuple(component, old) for old in tags):
        raise ReleasePolicyError("Application downgrades are refused; use the documented backup/restore procedure")
    rendered = update_values_text(component, text, target, current=tags)
    summary = {
        "component": component, "channel": channel, "current": tags, "target": target,
        "changed": rendered != text,
        "major_upgrade": any(version_tuple(component, target)[0] > version_tuple(component, old)[0] for old in tags),
        "unresolved_placeholders": bool(re.search(r"<[A-Z0-9_]+>", text)),
    }
    return summary, text, rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for component in COMPONENTS:
        name = "argocd-ha" if component == "argocd" else component
        parser.add_argument(f"--{component}", choices=CHANNELS, help="Omit to leave this component untouched")
        parser.add_argument(f"--{component}-version", default="", help="Exact stable X.Y.Z version for specific")
        parser.add_argument(f"--{component}-values", type=Path, default=PREMIUM_APPS / name / "values.yaml")
    parser.add_argument("--write", action="store_true", help="Write only selected image tags to local files; never sync/deploy")
    parser.add_argument("--allow-major-upgrade", action="store_true", help="Acknowledge a reviewed major upgrade (does not permit downgrades)")
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog()
        changes = []
        for component in COMPONENTS:
            channel = getattr(args, component)
            specific = getattr(args, f"{component}_version")
            if specific and not channel:
                raise ReleasePolicyError(f"--{component}-version requires --{component} specific")
            if channel:
                path = getattr(args, f"{component}_values")
                if any(path.resolve() == existing[0].resolve() for existing in changes):
                    raise ReleasePolicyError("Selected components must use distinct values files")
                summary, original, rendered = prepare_change(component, path, channel, specific, catalog)
                changes.append((path, original, rendered, summary))
        if args.write and not changes:
            raise ReleasePolicyError("Select at least one component before --write")
        if args.write:
            # Validate all components before writing any file. Never promote a public placeholder template.
            if any(item[3]["unresolved_placeholders"] for item in changes):
                raise ReleasePolicyError("Resolve private deployment placeholders before writing an update")
            if not args.allow_major_upgrade and any(item[3]["major_upgrade"] for item in changes):
                raise ReleasePolicyError("Review backup, restore and major-version migration notes; then use --allow-major-upgrade")
            if any(read_bounded_bytes(path).decode("utf-8") != original for path, original, _, _ in changes):
                raise ReleasePolicyError("A values file changed during planning; re-plan before writing")
            for path, original, rendered, _ in changes:
                if rendered != original:
                    atomic_write_text(path, rendered)
        print(json.dumps({
            "mode": "write-local-values" if args.write else "plan-only",
            "reviewed_on": catalog["reviewed_on"], "review_before": catalog["review_before"],
            "available": {component: {key: value for key, value in choices.items() if key in {"pinned", "lts", "stable"}}
                          for component, choices in catalog["components"].items()},
            "changes": [item[3] for item in changes],
            "cluster_changed": False,
        }, indent=2))
        return 0
    except (ReleasePolicyError, OSError, ValueError, yaml.YAMLError):
        # Error text is allowlisted in ReleasePolicyError; YAML/I/O diagnostics may expose private data.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, ReleasePolicyError) else "Version update failed; private details suppressed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
