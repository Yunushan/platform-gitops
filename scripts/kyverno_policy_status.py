#!/usr/bin/env python3
"""Check the original ImageValidatingPolicy identity and its nested readiness."""


def original_policy_ready(current, original):
    """A ready flag belongs only to this unchanged, freshly observed policy."""
    for field in ("name", "uid", "generation"):
        if current.get("metadata", {}).get(field) != original["metadata"][field]:
            raise ValueError("Original image policy identity or generation changed")
    if current.get("spec") != original["spec"]:
        raise ValueError("Original image policy specification changed")
    # The v1 ImageValidatingPolicy CRD uses conditionStatus.ready, unlike
    # Deployment status.conditions. A top-level Ready condition is not proof.
    return current.get("status", {}).get("conditionStatus", {}).get("ready") is True
