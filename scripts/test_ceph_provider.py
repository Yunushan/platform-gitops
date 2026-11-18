#!/usr/bin/env python3
"""Test finite fresh-provider scope, device refusal and whole-device scans."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import ceph_device_probe as probe
import render_ceph_provider as provider
from test_ceph_storage import uid, image


def fixture():
    return {"schema_version": 1, "cluster_id": uid(100), "namespace": "rook-provider", "namespace_uid": uid(1), "name": "rook-provider",
        "target": {"kubeconfig_env": "STORAGE_KUBECONFIG", "context": "storage", "cluster_uid": uid(2)},
        "operator": {"namespace": "rook-system", "namespace_uid": uid(3), "deployment": "rook-ceph-operator", "uid": uid(4), "image": image("a"), "cluster_crd_uid": uid(5),
            "configmaps": {name: {"uid": uid(6 + i), "data_sha256": "0" * 64} for i, name in enumerate(("rook-ceph-operator-config", "rook-csi-operator-image-set-configmap"))}},
        "images": {"ceph": image("f"), "probe": image("b")}, "ceph_version": "20.2.4", "domain_label": "topology.kubernetes.io/zone",
        "nodes": [{"name": "storage-" + str(i), "uid": uid(10 + i), "failure_domain": "rack-" + str(i), "machine_id_sha256": str(i) * 64, "boot_id": uid(20 + i),
            "devices": [{"by_id": "/dev/disk/by-id/wwn-fixture-" + str(i), "major": 8, "minor": 16, "bytes": 64 * 1024 ** 3, "identity_sha256": str(i + 4) * 64}]} for i in range(1, 5)],
        "resources": {role: {"requests": {"cpu": "1", "memory": "4Gi" if role == "osd" else "1Gi"}, "limits": {"cpu": "4", "memory": "8Gi" if role == "osd" else "2Gi"}} for role in ("mon", "mgr", "osd")},
        "probe_timeout_seconds": 3600, "admitted_sha256": None}


class ProviderTests(unittest.TestCase):
    def test_all_consumer_runtimes_prepare_explicit_devices_and_safe_cluster_policy(self):
        plan = fixture()
        for runtime in provider.storage.RUNTIMES:
            files = provider.render(plan, runtime)
            status = json.loads(files["render-status.json"])
            for key in provider.UNVERIFIED: self.assertFalse(status[key])
            cluster = provider.cluster(plan); spec = cluster["spec"]
            self.assertFalse(spec["storage"]["useAllNodes"]); self.assertFalse(spec["storage"]["useAllDevices"])
            self.assertEqual(spec["storage"]["config"]["encryptedDevice"], "true"); self.assertEqual(spec["mon"]["count"], 3); self.assertEqual(spec["mgr"]["count"], 2)
            self.assertEqual(len(spec["storage"]["nodes"]), 4)

    def test_device_scope_credentials_aliases_and_unsupported_topology_refuse(self):
        changes = [lambda p: p["nodes"].pop(), lambda p: p["nodes"][0]["devices"][0].update(by_id="/dev/sda"),
            lambda p: p["nodes"][0]["devices"][0].update(by_id="/dev/disk/by-id/wwn-fixture-part1"), lambda p: p["nodes"][1].update(boot_id=p["nodes"][0]["boot_id"]),
            lambda p: p["nodes"][0]["devices"][0].update(bytes=True), lambda p: p["resources"]["osd"]["requests"].update(memory="1Gi"),
            lambda p: p["images"].update(probe="python:latest"), lambda p: p.update(password="dummy")]
        for change in changes:
            plan = fixture(); change(plan)
            with self.assertRaises(ValueError): provider.validate(plan, "rke2")

    def test_probe_jobs_bind_only_readonly_host_inputs_and_no_service_credentials(self):
        plan = fixture(); objects = provider.probe_objects(plan, "a" * 32)
        self.assertTrue(objects[0]["immutable"]); self.assertEqual(len(objects), 5)
        for i, job in enumerate(objects[1:]):
            pod = job["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeName"], plan["nodes"][i]["name"]); self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(job["spec"]["backoffLimit"], 0)
            self.assertTrue(all(m["readOnly"] for m in pod["containers"][0]["volumeMounts"]))
            self.assertTrue(pod["containers"][0]["securityContext"]["privileged"])
            self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(len([v for v in pod["volumes"] if v.get("hostPath", {}).get("type") == "BlockDevice"]), 1)

    def test_optional_actual_rook_cluster_schema(self):
        if os.environ.get("CEPH_STORAGE_TEST_SCHEMA") != "1": self.skipTest("Opt in to pinned upstream cluster-schema validation")
        import jsonschema
        plan = fixture()
        for runtime in provider.storage.RUNTIMES:
            provider.validate(plan, runtime); jsonschema.Draft4Validator(provider.schema()).validate(provider.cluster(plan))


class DeviceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "The sysfs page limit is Linux-specific")
    def test_sysfs_stat_size_is_distinct_from_attribute_content_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "serial"
            path.write_bytes(b"fixture-drive\n")
            info = SimpleNamespace(st_mode=path.stat().st_mode, st_size=os.sysconf("SC_PAGE_SIZE"))
            with mock.patch.object(probe.os, "fstat", return_value=info):
                self.assertEqual(probe.sysfs_attribute(path, 512), b"fixture-drive")
                with self.assertRaises(ValueError):
                    probe.read(path, 512)
                path.write_bytes(b"x" * 513)
                with self.assertRaises(ValueError):
                    probe.sysfs_attribute(path, 512)

    def test_default_scope_check_does_not_read_host_or_open_devices(self):
        plan = fixture(); scope = provider.scope(plan, plan["nodes"][0])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scope.json"; path.write_text(json.dumps(scope))
            original_open = probe.os.open
            def scope_only(name, *args, **kwargs):
                if Path(name) != path: raise AssertionError("host/device open")
                return original_open(name, *args, **kwargs)
            with mock.patch.object(probe, "probe", side_effect=AssertionError("live probe")), mock.patch.object(probe.os, "open", side_effect=scope_only) as opened, mock.patch("builtins.print"):
                self.assertEqual(probe.main(["--scope", str(path)]), 0)
                self.assertEqual(opened.call_count, 1)

    def test_whole_stream_rejects_nonzero_bytes_at_start_middle_and_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture"; size = 8 * 1024 ** 2 + 13
            with path.open("wb") as stream: stream.truncate(size)
            with path.open("rb") as stream:
                self.assertEqual(probe.scan_zero(stream.fileno(), size, time.monotonic() + 10), hashlib.sha256(bytes(size)).hexdigest())
            for offset in (0, size // 2, size - 1):
                with path.open("r+b") as stream: stream.seek(offset); stream.write(b"x")
                with path.open("rb") as stream, self.assertRaises(ValueError): probe.scan_zero(stream.fileno(), size, time.monotonic() + 10)
                with path.open("r+b") as stream: stream.seek(offset); stream.write(b"\0")
            with path.open("rb") as stream, self.assertRaises(TimeoutError): probe.scan_zero(stream.fileno(), size, time.monotonic() - 1)

    def test_truncated_or_larger_device_stream_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture"; path.write_bytes(bytes(4096))
            for size in (4095, 4097):
                with path.open("rb") as stream, self.assertRaises(ValueError): probe.scan_zero(stream.fileno(), size, time.monotonic() + 10)

    def test_duplicate_scope_keys_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scope.json"; path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError): probe.load(path)

    def test_host_mount_inventory_refuses_mounted_device_and_ambiguous_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mountinfo"; disk = {"major": 8, "minor": 16}
            path.write_text("24 1 8:16 / / rw,relatime - ext4 /dev/fixture rw\n")
            with self.assertRaises(ValueError): probe.mounted(disk, mountinfo=path)
            path.write_text("not a mount table\n")
            with self.assertRaises(ValueError): probe.mounted(disk, mountinfo=path)

    @unittest.skipUnless(os.name == "posix", "Synthetic sysfs requires POSIX symlinks")
    def test_hardware_identity_partition_and_holder_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); node = root / "devices/pci0000/storage"; node.mkdir(parents=True)
            (node / "holders").mkdir(); (node / "slaves").mkdir(); (node / "dev").write_text("8:16\n"); (node / "wwid").write_text("fixture-wwid\n")
            (root / "dev/block").mkdir(parents=True); (root / "dev/block/8:16").symlink_to(node)
            disk = {"major": 8, "minor": 16, "identity_sha256": probe.fingerprint({"wwid": "fixture-wwid", "serial": ""})}
            self.assertEqual(probe.hardware(disk, sysfs=root), disk["identity_sha256"])
            (node / "partition").write_text("1")
            with self.assertRaises(ValueError): probe.hardware(disk, sysfs=root)
            (node / "partition").unlink(); (node / "holders/fixture").mkdir()
            with self.assertRaises(ValueError): probe.hardware(disk, sysfs=root)

    def test_optional_real_linux_loop_device(self):
        if os.environ.get("CEPH_DEVICE_TEST_LOOP") != "1": self.skipTest("Opt in to a disposable Linux loop-device fixture")
        from test_ceph_probe_fixture import exercise
        exercise(self)


if __name__ == "__main__": unittest.main()
