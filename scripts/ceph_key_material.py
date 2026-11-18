#!/usr/bin/env python3
"""Read monitor-held host-OSD recovery material without changing the provider."""

import base64
import re
import time

from kubernetes_management import Client, validate_target, verify_target
import render_ceph_storage as storage
from strict_json import loads_strict_json


MAX_BYTES = 4 * 1024 * 1024


def exact(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError("Missing or unsupported recovery fields")


def decode(value, maximum=64 * 1024):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum * 2:
        raise ValueError("Recovery data is not bounded base64")
    raw = base64.b64decode(value, validate=True)
    if len(raw) > maximum or base64.b64encode(raw).decode() != value:
        raise ValueError("Recovery data is not canonical base64")
    return raw


def validate_source(source):
    exact(source, "target namespace namespace_uid name cluster_uid spec_sha256 fsid ceph_version pod osds")
    validate_target(source["target"])
    if (any(not storage.uid(source[key]) for key in ("namespace_uid", "cluster_uid", "fsid"))
            or any(not storage.resource(source[key]) for key in ("namespace", "name"))
            or not storage.sha(source["spec_sha256"])
            or not isinstance(source["ceph_version"], str) or not re.fullmatch(r"20[.]2[.][0-9]+", source["ceph_version"])):
        raise ValueError("Pin the original namespace, cluster, FSID, spec and Ceph release")
    pod = source["pod"]
    exact(pod, "name uid node node_uid container spec_sha256 image image_id container_id started_at")
    if (any(not storage.resource(pod[key]) for key in ("name", "node", "container"))
            or any(not storage.uid(pod[key]) for key in ("uid", "node_uid")) or not storage.sha(pod["spec_sha256"])
            or not storage.image(pod["image"]) or not isinstance(pod["image_id"], str)
            or not (storage.image(pod["image_id"]) or re.fullmatch(r"(?:(?:containerd|cri-o|docker)://)?sha256:[0-9a-f]{64}", pod["image_id"]))
            or not isinstance(pod["container_id"], str)
            or not re.fullmatch(r"(?:containerd|cri-o|docker)://[0-9a-f]{64}", pod["container_id"])
            or not isinstance(pod["started_at"], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", pod["started_at"])):
        raise ValueError("Pin one original toolbox container, node, image and start identity")
    rows = source["osds"]
    if (not isinstance(rows, list) or not 4 <= len(rows) <= 256
            or any(not isinstance(row, dict) or set(row) != {"id", "uuid"}
                   or type(row["id"]) is not int or not 0 <= row["id"] <= 1000000
                   or not storage.uid(row["uuid"]) for row in rows)
            or len({row["id"] for row in rows}) != len(rows)
            or len({row["uuid"] for row in rows}) != len(rows)):
        raise ValueError("Pin the complete unique host-OSD ID and UUID inventory")


def validate_material(item, scope):
    exact(item, "fsid ceph_version osds osd_epoch auth_export_base64 config_keys monitor_secret monitor_endpoints")
    expected = sorted(scope["osds"], key=lambda row: row["id"])
    if (item["fsid"] != scope["fsid"] or item["ceph_version"] != scope["ceph_version"] or storage.canonical(item["osds"]) != storage.canonical(expected)
            or not storage.integer(item["osd_epoch"], 1, 2 ** 64 - 1) or not isinstance(item["config_keys"], dict)
            or set(item["config_keys"]) != {"dm-crypt/osd/" + row["uuid"] + "/luks" for row in expected}):
        raise ValueError("Archive is missing the original complete host-OSD key set")
    for key in item["config_keys"].values():
        if not 16 <= len(decode(key, 4096)) <= 4096:
            raise ValueError("Archived OSD key material is invalid")
    keyring = decode(item["auth_export_base64"], MAX_BYTES).decode("utf-8")
    required = ["client.admin", *["osd." + str(row["id"]) for row in expected], *["client.osd-lockbox." + row["uuid"] for row in expected]]
    if any(not re.search(r"(?m)^\[" + re.escape(entity) + r"\]\s*$", keyring) for entity in required):
        raise ValueError("Archived auth export is missing original recovery identities")
    for name in ("monitor_secret", "monitor_endpoints"):
        exact(item[name], "uid data")
        if not storage.uid(item[name]["uid"]) or not isinstance(item[name]["data"], dict):
            raise ValueError("Archived monitor bootstrap identities are invalid")
    data = item["monitor_secret"]["data"]
    if (not {"admin-secret", "mon-secret", "fsid", "cluster-name"}.issubset(data) or len(data) > 16
            or decode(data["fsid"]).decode("ascii") != item["fsid"] or any(not decode(value) for value in data.values())):
        raise ValueError("Archived monitor credentials are incomplete or have a different FSID")
    addresses = item["monitor_endpoints"]["data"]
    if (not {"data", "mapping", "maxMonId"}.issubset(addresses) or not addresses["data"]
            or any(not isinstance(value, str) or len(value.encode()) > 65536 for value in addresses.values())):
        raise ValueError("Archived monitor bootstrap configuration is incomplete")


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def seconds(self, maximum):
        remaining = int(self.deadline - time.monotonic())
        if remaining < 2:
            raise TimeoutError("Recovery operation deadline expired")
        return min(maximum, remaining)


class Source:
    def __init__(self, plan, budget, *, client=None):
        self.plan = plan
        self.scope = plan["source"]
        self.budget = budget
        self.client = client if client is not None else Client(self.scope["target"])

    def get(self, resource, namespace=None):
        self.budget.seconds(30)
        result = self.client.get(resource, namespace)
        self.budget.seconds(30)
        return result or {}

    def verify(self):
        verify_target(self.client, self.scope["target"])
        namespace = self.get("namespace/" + self.scope["namespace"])
        if namespace.get("metadata", {}).get("uid") != self.scope["namespace_uid"]:
            raise ValueError("The original provider namespace changed")
        cluster = self.get("cephcluster.ceph.rook.io/" + self.scope["name"], self.scope["namespace"])
        meta, spec, status = (cluster.get(key, {}) for key in ("metadata", "spec", "status"))
        if (meta.get("uid") != self.scope["cluster_uid"] or meta.get("deletionTimestamp")
                or storage.fingerprint(spec) != self.scope["spec_sha256"]
                or status.get("phase") != "Ready" or status.get("ceph", {}).get("health") != "HEALTH_OK"
                or status.get("ceph", {}).get("fsid") != self.scope["fsid"]):
            raise ValueError("The original healthy provider identity or policy changed")
        selected = spec.get("storage", {})
        if (selected.get("config", {}).get("encryptedDevice") != "true"
                or selected.get("useAllNodes") is not False or selected.get("useAllDevices") is not False
                or selected.get("storageClassDeviceSets") or not selected.get("nodes")
                or spec.get("external", {}).get("enable") or spec.get("security", {}).get("kms")):
            raise ValueError("This collector requires explicit encrypted host devices and monitor-held keys")
        pod_scope = self.scope["pod"]
        pod = self.get("pod/" + pod_scope["name"], self.scope["namespace"])
        meta, spec, status = (pod.get(key, {}) for key in ("metadata", "spec", "status"))
        containers = [row for row in spec.get("containers", []) if row.get("name") == pod_scope["container"]]
        statuses = [row for row in status.get("containerStatuses", []) if row.get("name") == pod_scope["container"]]
        if (meta.get("uid") != pod_scope["uid"] or meta.get("deletionTimestamp")
                or storage.fingerprint(spec) != pod_scope["spec_sha256"] or spec.get("nodeName") != pod_scope["node"]
                or status.get("phase") != "Running" or len(containers) != 1 or len(statuses) != 1
                or containers[0].get("image") != pod_scope["image"]):
            raise ValueError("The approved original toolbox pod changed")
        observed = statuses[0]
        if (observed.get("containerID") != pod_scope["container_id"] or observed.get("restartCount") != 0
                or observed.get("ready") is not True
                or observed.get("imageID") != pod_scope["image_id"]
                or observed.get("state") != {"running": {"startedAt": pod_scope["started_at"]}}):
            raise ValueError("The approved toolbox container was replaced or restarted")
        node = self.get("node/" + pod_scope["node"])
        if (node.get("metadata", {}).get("uid") != pod_scope["node_uid"]
                or not any(row.get("type") == "Ready" and row.get("status") == "True" for row in node.get("status", {}).get("conditions", []))):
            raise ValueError("The approved toolbox host changed or is not Ready")

    def ceph(self, arguments):
        seconds = self.budget.seconds(self.plan["limits"]["command_seconds"])
        if seconds < 5:
            raise TimeoutError("Not enough time for a bounded provider read")
        pod = self.scope["pod"]
        # The in-container timeout also bounds a read if kubectl disconnects.
        args = ["exec", "--namespace", self.scope["namespace"], pod["name"], "--container", pod["container"], "--",
                "/usr/bin/timeout", "--signal=TERM", "--kill-after=2", str(seconds), "/usr/bin/ceph", *arguments]
        value = self.client.call(args, seconds=seconds + 5)
        self.budget.seconds(30)
        if not isinstance(value, str) or not 0 < len(value.encode()) <= MAX_BYTES:
            raise ValueError("Provider output is absent or exceeds the capture bound")
        return value

    def owned(self, obj):
        meta = obj.get("metadata", {})
        if (not storage.uid(meta.get("uid")) or meta.get("deletionTimestamp")
                or not any(row.get("kind") == "CephCluster" and row.get("name") == self.scope["name"]
                           and row.get("uid") == self.scope["cluster_uid"] for row in meta.get("ownerReferences", []))):
            raise ValueError("Provider recovery material has foreign or missing ownership")

    def snapshot(self):
        self.verify()
        version = self.ceph(["version"]).strip()
        if not version.startswith("ceph version " + self.scope["ceph_version"] + " "):
            raise ValueError("Toolbox Ceph release differs from the reviewed provider")
        if self.ceph(["fsid"]).strip() != self.scope["fsid"]:
            raise ValueError("Toolbox is connected to another Ceph cluster")
        osdmap = loads_strict_json(self.ceph(["--format=json", "osd", "dump"]))
        rows = osdmap.get("osds")
        expected = sorted(self.scope["osds"], key=lambda row: row["id"])
        if (osdmap.get("fsid") != self.scope["fsid"] or type(osdmap.get("epoch")) is not int or osdmap["epoch"] <= 0
                or not isinstance(rows, list) or len(rows) != len(expected)
                or any(not isinstance(row, dict) or type(row.get("osd")) is not int
                       or type(row.get("up")) is not int or type(row.get("in")) is not int
                       or row["up"] != 1 or row["in"] != 1 for row in rows)
                or sorted(({"id": row["osd"], "uuid": row.get("uuid")} for row in rows), key=lambda row: row["id"]) != expected):
            raise ValueError("The complete healthy OSD inventory differs from the reviewed scope")
        keyring = self.ceph(["auth", "export"])
        required = ["client.admin", *["osd." + str(row["id"]) for row in expected], *["client.osd-lockbox." + row["uuid"] for row in expected]]
        if any(not re.search(r"(?m)^\[" + re.escape(entity) + r"\]\s*$", keyring) for entity in required):
            raise ValueError("Auth export is missing required host-OSD recovery identities")
        keys = {}
        for row in expected:
            name = "dm-crypt/osd/" + row["uuid"] + "/luks"
            value = self.ceph(["config-key", "get", name]).strip()
            if not 16 <= len(decode(value, 4096)) <= 4096:
                raise ValueError("An original OSD encryption key is invalid")
            keys[name] = value
        secret = self.get("secret/rook-ceph-mon", self.scope["namespace"])
        endpoints = self.get("configmap/rook-ceph-mon-endpoints", self.scope["namespace"])
        for obj in (secret, endpoints):
            self.owned(obj)
        data = secret.get("data")
        if (not isinstance(data, dict) or not {"admin-secret", "mon-secret", "fsid", "cluster-name"}.issubset(data)
                or len(data) > 16 or decode(data["fsid"]).decode() != self.scope["fsid"]):
            raise ValueError("The monitor bootstrap secret is incomplete or belongs to another cluster")
        for value in data.values():
            if not decode(value):
                raise ValueError("Monitor bootstrap material is empty")
        addresses = endpoints.get("data")
        if (not isinstance(addresses, dict) or not {"data", "mapping", "maxMonId"}.issubset(addresses)
                or any(not isinstance(value, str) or len(value.encode()) > 65536 for value in addresses.values())):
            raise ValueError("Monitor endpoint bootstrap configuration is incomplete")
        self.verify()
        return {"fsid": self.scope["fsid"], "ceph_version": self.scope["ceph_version"], "osds": expected, "osd_epoch": osdmap["epoch"],
                "auth_export_base64": base64.b64encode(keyring.encode()).decode(), "config_keys": keys,
                "monitor_secret": {"uid": secret["metadata"]["uid"], "data": data},
                "monitor_endpoints": {"uid": endpoints["metadata"]["uid"], "data": addresses}}

    def collect(self):
        with self.client.frozen():
            first, second = self.snapshot(), self.snapshot()
            if first != second:
                raise ValueError("Provider key material or identities changed during the repeated reads")
            if len(storage.canonical(first).encode()) > MAX_BYTES:
                raise ValueError("The complete provider recovery set exceeds the capture bound")
            return first
