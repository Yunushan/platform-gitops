"""Shared checks for GitHub release tag rules, environment policies and identities."""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatchcase
import re
from typing import Any, Literal


class GitHubPolicyError(ValueError):
    """GitHub returned an incomplete or ambiguous policy listing."""


def fetch_github_objects(path: str, get: Callable[[str], Any]) -> list[dict[str, Any]]:
    """Collect complete identity or ruleset lists, rejecting repeated IDs and oversized pages."""
    items: list[dict[str, Any]] = []
    ids: set[int] = set()
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        batch = get(f"{path}{separator}per_page=100&page={page}")
        if not isinstance(batch, list) or len(batch) > 100:
            raise GitHubPolicyError(f"invalid paginated GitHub response: {path}")
        for item in batch:
            identifier = item.get("id") if isinstance(item, dict) else None
            if type(identifier) is not int or identifier <= 0 or identifier in ids:
                raise GitHubPolicyError(f"invalid or repeated ID in paginated GitHub response: {path}")
            ids.add(identifier)
            items.append(item)
        if len(batch) < 100:
            return items
    raise GitHubPolicyError(f"GitHub pagination exceeded 100 pages: {path}")


def covers_release_tags(ruleset: Any, tag_ref_pattern: str) -> bool:
    if not isinstance(ruleset, dict):
        return False
    if ruleset.get("target") != "tag" or ruleset.get("enforcement") != "active":
        return False
    conditions = ruleset.get("conditions")
    refs = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(refs, dict):
        return False
    includes, excludes = refs.get("include"), refs.get("exclude", [])
    if not isinstance(includes, list) or tag_ref_pattern not in includes:
        return False
    if not isinstance(excludes, list):
        return False

    # For ordinary literals, * and ?, test exact exclusions directly. For glob
    # exclusions, disjoint literal prefixes prove they cannot remove a release tag.
    simple_pattern = re.fullmatch(r"[a-zA-Z0-9._/*?-]+", tag_ref_pattern)
    prefix = re.split(r"[^a-zA-Z0-9._/-]", tag_ref_pattern, maxsplit=1)[0]
    for exclusion in excludes:
        if not isinstance(exclusion, str) or not exclusion.startswith("refs/"):
            return False
        excluded_prefix = re.split(r"[^a-zA-Z0-9._/-]", exclusion, maxsplit=1)[0]
        if simple_pattern and exclusion == excluded_prefix and not fnmatchcase(exclusion, tag_ref_pattern):
            continue
        if prefix.startswith(excluded_prefix) or excluded_prefix.startswith(prefix):
            return False
    return True


def deployment_tag_policy(document: Any, pattern: str) -> Literal["current", "missing", "conflict"]:
    if not isinstance(document, dict) or type(document.get("total_count")) is not int:
        return "conflict"
    items = document.get("branch_policies")
    if not isinstance(items, list) or document["total_count"] != len(items):
        return "conflict"
    if not items:
        return "missing"
    if all(isinstance(item, dict) and item.get("type") == "tag" and item.get("name") == pattern
           for item in items):
        return "current"
    return "conflict"
