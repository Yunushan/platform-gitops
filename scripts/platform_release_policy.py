#!/usr/bin/env python3
"""Offline, reviewed release choices; never resolve floating tags at deployment time."""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import re

from bounded_file import read_bounded_text
from strict_json import loads_strict_json


CATALOG = Path(__file__).resolve().parents[1] / "config/platform-releases.json"
COMPONENTS = ("forgejo", "woodpecker", "argocd")
CHANNELS = ("pinned", "lts", "lts-stable", "stable", "specific")
RELEASE = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rootless)?")


class ReleasePolicyError(ValueError):
    pass


def version_tuple(component: str, value: str) -> tuple[int, int, int]:
    match = RELEASE.fullmatch(value)
    if not match or (component != "forgejo" and match[4]):
        raise ReleasePolicyError(
            f"{component.upper()}_IMAGE_TAG must be a stable release tag pinned to exact X.Y.Z"
            + (" (optionally -rootless)" if component == "forgejo" else "")
            + "; floating tags, prereleases, URLs and credentials are not accepted"
        )
    return tuple(int(match[index]) for index in (1, 2, 3))


def normalize_tag(component: str, value: str) -> str:
    version_tuple(component, value)
    bare = value.removeprefix("v")
    return bare if component == "forgejo" else f"v{bare}"


def load_catalog(path: Path = CATALOG) -> dict:
    try:
        catalog = loads_strict_json(read_bounded_text(path, max_bytes=32 * 1024))
        if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1 or set(catalog["components"]) != set(COMPONENTS):
            raise ValueError()
        reviewed = date.fromisoformat(catalog["reviewed_on"])
        deadline = date.fromisoformat(catalog["review_before"])
        if deadline <= reviewed:
            raise ValueError()
        for component, choices in catalog["components"].items():
            required = {"pinned", "stable", "source"} | ({"lts"} if component == "forgejo" else set())
            if not required <= choices.keys() or (component != "forgejo" and "lts" in choices):
                raise ValueError()
            for channel in ("pinned", "stable", "lts"):
                if channel in choices:
                    normalize_tag(component, choices[channel])
            if not choices["source"].startswith("https://"):
                raise ValueError()
        return catalog
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReleasePolicyError("Reviewed platform release catalog is invalid or unreadable") from exc


def select_release(
    component: str,
    *,
    channel: str = "pinned",
    specific: str = "",
    current: str = "",
    today: date | None = None,
    catalog: dict | None = None,
) -> str:
    if component not in COMPONENTS or channel not in CHANNELS:
        raise ReleasePolicyError("Unsupported platform component or update channel")
    channel = "lts" if channel == "lts-stable" else channel
    if channel == "lts" and component != "forgejo":
        raise ReleasePolicyError(f"{component} has no upstream LTS channel; use stable or specific")
    catalog = catalog if catalog is not None else load_catalog()
    choices = catalog["components"][component]
    if channel in {"lts", "stable"}:
        now = today or date.today()
        if not date.fromisoformat(catalog["reviewed_on"]) <= now < date.fromisoformat(catalog["review_before"]):
            raise ReleasePolicyError("Release catalog needs a fresh upstream review before selecting a named channel")
        selected = normalize_tag(component, choices[channel])
        if specific and version_tuple(component, specific) != version_tuple(component, selected):
            raise ReleasePolicyError(f"{component.upper()}_IMAGE_TAG conflicts with its selected update channel")
    elif channel == "specific":
        if not specific:
            raise ReleasePolicyError(f"specific requires {component.upper()}_IMAGE_TAG or --{component}-version")
        selected = normalize_tag(component, specific)
    else:
        selected = normalize_tag(component, specific or current or choices["pinned"])
    # The Helm chart also supports image.rootless=true with a plain release tag.
    # Preserve an explicit rootless suffix when editing an existing deployment.
    if component == "forgejo" and (current.endswith("-rootless") or specific.endswith("-rootless")):
        selected = selected.removesuffix("-rootless") + "-rootless"
    return selected


def release_from_environment(component: str, *, current: str = "") -> str:
    prefix = component.upper()
    return select_release(
        component,
        channel=os.environ.get(f"{prefix}_UPDATE_CHANNEL", "pinned").strip() or "pinned",
        specific=os.environ.get(f"{prefix}_IMAGE_TAG", "").strip(),
        current=current,
    )
