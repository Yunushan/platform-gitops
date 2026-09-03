"""Narrow Argo CD and EndpointSlice contracts for the shared Valkey service."""

from __future__ import annotations

import copy


STORAGE_RULES = (
    {"group": "apps", "kind": "StatefulSet", "jqPathExpressions": [".spec.volumeClaimTemplates[]?.spec.storageClassName"]},
    {"group": "", "kind": "PersistentVolumeClaim", "jsonPointers": ["/spec/storageClassName"]},
)


def storage_policy_patch(application: dict) -> dict:
    """Restore the repository policy without changing sources, pruning, or PVCs."""
    spec = application.get("spec") or {}
    if ((spec.get("destination") or {}).get("namespace") != "platform-cache"
            or (spec.get("source") or {}).get("path") != "gitops/clusters/rke2-main/premium-3node/apps/platform-valkey"):
        raise ValueError("Valkey Application does not match the managed platform profile.")
    rules = copy.deepcopy(spec.get("ignoreDifferences") or [])
    options = list((spec.get("syncPolicy") or {}).get("syncOptions") or [])
    if "RespectIgnoreDifferences=false" in options:
        raise ValueError("Valkey explicitly disables immutable-field preservation; reconcile its Application policy first.")
    if {"Force=true", "Replace=true"}.intersection(options):
        raise ValueError("Valkey enables a destructive sync mode; reconcile its Application policy before automatic repair.")
    for rule in STORAGE_RULES:
        field = "jqPathExpressions" if "jqPathExpressions" in rule else "jsonPointers"
        if not any(
            item.get("group", "") == rule["group"] and item.get("kind") == rule["kind"]
            and not item.get("name") and not item.get("namespace")
            and set(rule[field]).issubset(item.get(field) or [])
            for item in rules
        ):
            rules.append(copy.deepcopy(rule))
    if "RespectIgnoreDifferences=true" not in options:
        options.append("RespectIgnoreDifferences=true")
    if rules == spec.get("ignoreDifferences", []) and options == (spec.get("syncPolicy") or {}).get("syncOptions", []):
        return {}
    version = (application.get("metadata") or {}).get("resourceVersion")
    if not version:
        raise ValueError("Valkey Application has no resourceVersion for guarded repair.")
    return {"metadata": {"resourceVersion": version}, "spec": {
        "ignoreDifferences": rules, "syncPolicy": {"syncOptions": options},
    }}


def ready_primary_endpoints(slices: dict) -> int:
    count = 0
    for item in slices.get("items") or []:
        if (item.get("metadata") or {}).get("labels", {}).get("kubernetes.io/service-name") != "platform-valkey-primary":
            continue
        # Addresses alone are insufficient when the named target port is absent.
        if not any(port.get("port") == 6380 and port.get("protocol", "TCP") == "TCP" for port in item.get("ports") or []):
            continue
        for endpoint in item.get("endpoints") or []:
            conditions = endpoint.get("conditions") or {}
            if conditions.get("ready") is True and not conditions.get("terminating") and endpoint.get("addresses"):
                count += 1
    return count
