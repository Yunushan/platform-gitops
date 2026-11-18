#!/usr/bin/env python3
"""Exercise explicit CephFS mount intent, private credentials and original-kernel-state observation."""

import base64
from contextlib import nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bounded_subprocess import run_bounded
import mount_cephfs_client as mount
from test_ceph_native import fixture as native_fixture
from test_ceph_storage import fixture as storage_fixture, uid


def fixture(native):
    return {"schema_version": 1, "operation_id": uid(880), "native_plan_sha256": mount.renderer.storage.fingerprint(native),
        "monitors": [{"address": "192.0.2." + str(i), "port": 3300} for i in (31, 32, 33)],
        "hosts": {h["name"]: {"helper_sha256": "a" * 64, "kernel_release": "6.12.1", "credential_file_env": "CEPH_CLIENT_" + str(i) + "_SECRET_FILE"} for i, h in enumerate(native["hosts"], 1)}}


class Host:
    def __init__(self):
        self.calls = 0; self.value = None; self.lost = False; self.reject = False; self.mutate = None
        self.current = {"namespace": {"device": 4, "inode": 30}, "credential": {"device": 1, "inode": 40, "mtime_ns": 100, "size": 41, "path_sha256": "b" * 64}}
        self.directory = {"device": 1, "inode": 50}
    def prerequisites(self, pins, selected): return copy.deepcopy(self.current)
    def mounted(self, scope, selected): return copy.deepcopy(self.value)
    def empty(self, selected): return copy.deepcopy(self.directory)
    def inventory_ids(self): return [1, 2, 3]
    def target_lock(self, selected): return nullcontext()
    def descriptor(self, scope, selected, mounted): return {"device": 53, "inode": 60}
    def mount(self, plan, scope, selected, expected):
        self.calls += 1
        if self.reject: raise ValueError("Fixture mount request uncertain")
        self.value = {"id": 500, "device": "0:53", "mount_sha256": "c" * 64}
        if self.mutate: self.mutate()
        if self.lost: self.lost = False; raise ValueError("Fixture mount acknowledgement lost")


class MountTests(unittest.TestCase):
    def setUp(self):
        self.storage = storage_fixture(); self.native = native_fixture(self.storage); self.plan = fixture(self.native); self.host = Host()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name); (self.root / "private").mkdir()
        patch = mock.patch.object(mount, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop); self.output = self.root / "private/mount"

    def operate(self, mode="create", runtime="raw"):
        return mount.operate(self.plan, self.native, self.storage, runtime, "client-1", mode=mode, output=self.output, host=self.host)

    def test_local_and_host_check_do_not_mount(self):
        self.assertFalse(self.operate("local")["cephfs_mount_prepared"])
        self.assertTrue(self.operate("check-host")["host_checked"]); self.assertEqual(self.host.calls, 0); self.assertFalse(self.output.exists())

    def test_success_and_resume_keep_the_original_mount_without_repeating(self):
        result = self.operate(); self.assertTrue(result["cephfs_mount_prepared"])
        self.assertTrue(all(result[k] is False for k in mount.UNVERIFIED))
        self.assertTrue(self.operate("resume")["cephfs_mount_prepared"]); self.assertEqual(self.host.calls, 1)
        proof = json.loads((self.output / "cephfs-mount-proof.json").read_text())
        self.assertEqual(proof["mount"]["id"], 500); self.assertEqual(proof["native_plan_sha256"], self.plan["native_plan_sha256"])
        self.assertNotIn("credential", proof)

    def test_lost_acknowledgement_observes_matching_new_mount_once(self):
        self.host.lost = True
        with self.assertRaises(ValueError): self.operate()
        self.assertTrue(self.operate("resume")["cephfs_mount_prepared"]); self.assertEqual(self.host.calls, 1)

    def test_absent_uncertain_mount_or_removed_original_is_never_requested_again(self):
        self.host.reject = True
        with self.assertRaises(ValueError): self.operate()
        self.host.reject = False
        with self.assertRaises(ValueError): self.operate("resume")
        self.assertEqual(self.host.calls, 1)
        self.output = self.root / "private/other"; self.host = Host(); self.operate(); self.host.value = None
        with self.assertRaises(ValueError): self.operate("resume")
        self.assertEqual(self.host.calls, 1)

    def test_existing_mount_is_not_adopted_by_a_new_create(self):
        self.host.value = {"id": 500, "device": "0:53", "mount_sha256": "c" * 64}
        self.assertTrue(self.operate("check-host")["existing_matching_mount"])
        with self.assertRaises(ValueError): self.operate()
        self.assertEqual(self.host.calls, 0)

    def test_target_is_reobserved_after_taking_the_shared_host_lock(self):
        def concurrent(_):
            self.host.value = {"id": 500, "device": "0:53", "mount_sha256": "c" * 64}
            return nullcontext()
        self.host.target_lock = concurrent
        with self.assertRaises(ValueError): self.operate()
        self.assertEqual(self.host.calls, 0)

    def test_changed_mount_namespace_key_inode_kernel_plan_or_mount_refuses(self):
        self.operate()
        for change in (lambda: self.host.current["namespace"].update(inode=31), lambda: self.host.current["credential"].update(inode=41), lambda: self.host.value.update(id=501)):
            current, value = copy.deepcopy(self.host.current), copy.deepcopy(self.host.value); change()
            with self.assertRaises(ValueError): self.operate("resume")
            self.host.current, self.host.value = current, value
        self.plan["hosts"]["client-1"]["kernel_release"] = "6.12.2"
        with self.assertRaises(ValueError): self.operate("resume")
        self.assertEqual(self.host.calls, 1)

    def test_changed_prerequisites_after_mount_cannot_produce_a_proof(self):
        self.host.mutate = lambda: self.host.current["credential"].update(inode=999)
        with self.assertRaises(ValueError): self.operate()
        self.assertFalse((self.output / "cephfs-mount-proof.json").exists()); self.assertEqual(self.host.calls, 1)

    def test_docker_consumers_use_the_same_host_mount_scope(self):
        self.assertTrue(self.operate(runtime="docker")["cephfs_mount_prepared"])
        self.assertFalse(self.operate("resume", runtime="docker")["docker_runtime_verified"])

    def test_source_change_refuses_before_any_mount_request(self):
        original = mount.toolset()
        with mock.patch.object(mount, "toolset", side_effect=[original, "0" * 64]):
            with self.assertRaises(ValueError): self.operate()
        self.assertEqual(self.host.calls, 0)

    def test_invalid_mount_scope_and_unreviewed_monitors_refuse_before_host_calls(self):
        changes = [lambda p: p.update(native_plan_sha256="0" * 64), lambda p: p.update(operation_id=self.native["operation_id"]), lambda p: p["monitors"].pop(),
            lambda p: p["monitors"][0].update(address="127.0.0.1"), lambda p: p["monitors"][0].update(address="monitor.example.com"), lambda p: p["monitors"][0].update(port=True),
            lambda p: p["monitors"][0].update(address=p["monitors"][1]["address"]), lambda p: p["hosts"]["client-1"].update(credential_file_env="PATH"), lambda p: p.update(password="dummy")]
        for change in changes:
            plan = copy.deepcopy(self.plan); change(plan)
            with self.assertRaises(ValueError): mount.operate(plan, self.native, self.storage, "raw", "client-1", host=self.host)
        self.assertEqual(self.host.calls, 0)

    def test_mount_argv_contains_only_a_secret_descriptor_reference_and_fixed_options(self):
        scope = mount.renderer.scope(self.native, self.storage, "raw")
        command = mount.arguments(self.plan, scope, self.native["hosts"][0], Path("/usr/sbin/mount.ceph"), 17)
        self.assertEqual(command[1], "canary-1@" + self.storage["provider"]["fsid"] + "." + self.storage["names"]["filesystem"] + "=/platform-canaries")
        self.assertIn("-n", command)
        for option in ("ms_mode=secure", "nofallback", "noshare", "conf=/dev/null", "recover_session=no", "secretfile=/proc/"):
            self.assertIn(option, command[-1])
        self.assertNotIn(",secret=", command[-1]); self.assertNotIn("remount", command[-1])

    def test_ipv6_monitor_endpoints_are_bracketed_without_dns(self):
        for i, row in enumerate(self.plan["monitors"], 1): row["address"] = "2001:db8::" + str(i)
        mount.validate(self.plan, self.native, self.storage, "raw", "client-1")
        command = mount.arguments(self.plan, mount.renderer.scope(self.native, self.storage, "raw"), self.native["hosts"][0], Path("/usr/sbin/mount.ceph"), 17)
        self.assertIn("mon_addr=[2001:db8::1]:3300/[2001:db8::2]:3300/[2001:db8::3]:3300", command[-1])


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "Real private credential fixture requires root Linux")
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ceph-mount-host-", dir="/run"); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.storage = storage_fixture(); self.native = native_fixture(self.storage); self.plan = fixture(self.native); self.selected = self.native["hosts"][0]
        self.file = self.root / "client.secret"; self.value = base64.b64encode(b"x" * 28); self.file.write_bytes(self.value + b"\n"); self.file.chmod(0o600)
        env = mock.patch.dict(os.environ, {self.plan["hosts"]["client-1"]["credential_file_env"]: str(self.file)}); env.start(); self.addCleanup(env.stop)
        self.host = mount.Host(lock_root=self.root / "locks")

    def test_real_per_target_lock_excludes_another_attempt_and_releases(self):
        with self.host.target_lock(self.selected):
            with self.assertRaises(BlockingIOError), self.host.target_lock(self.selected): pass
        with self.host.target_lock(self.selected): pass
        self.assertEqual(len(list((self.root / "locks").iterdir())), 1)

    def test_child_reads_the_original_private_descriptor_without_a_key_in_argv(self):
        with self.host.credential(self.plan["hosts"]["client-1"], self.selected) as (descriptor, metadata):
            reference = f"/proc/{os.getpid()}/fd/{descriptor}"
            code = "import pathlib,sys; print(len(pathlib.Path(sys.argv[1]).read_bytes().strip()) == 40)"
            result = run_bounded(["/usr/bin/python3", "-I", "-B", "-c", code, reference], input="", text=True, env=mount.CLEAN, timeout=10, output_max_bytes=1024)
            self.assertEqual(result.returncode, 0); self.assertEqual(result.stdout.strip(), "True")
            self.assertFalse(any("secret" in key for key in metadata)); self.assertEqual(metadata["inode"], self.file.stat().st_ino)

    def test_public_symlink_or_hardlinked_credentials_refuse(self):
        directory = self.root / "real"; directory.mkdir(mode=0o700); copied = directory / "client.secret"; copied.write_bytes(self.value); copied.chmod(0o600)
        alias = self.root / "alias"; alias.symlink_to(directory, target_is_directory=True)
        with mock.patch.dict(os.environ, {self.plan["hosts"]["client-1"]["credential_file_env"]: str(alias / "client.secret")}):
            with self.assertRaises(ValueError), self.host.credential(self.plan["hosts"]["client-1"], self.selected): pass
        self.file.chmod(0o644)
        with self.assertRaises(ValueError), self.host.credential(self.plan["hosts"]["client-1"], self.selected): pass
        self.file.chmod(0o600); link = self.root / "link"; os.link(self.file, link)
        with self.assertRaises(ValueError), self.host.credential(self.plan["hosts"]["client-1"], self.selected): pass
        link.unlink(); self.file.unlink(); self.file.symlink_to(link)
        with self.assertRaises(OSError), self.host.credential(self.plan["hosts"]["client-1"], self.selected): pass

    def test_credential_rotation_while_held_is_detected(self):
        with self.assertRaises(ValueError), self.host.credential(self.plan["hosts"]["client-1"], self.selected): self.file.write_bytes(base64.b64encode(b"y" * 28))

    def test_real_empty_directory_and_mount_inventory_are_read_without_mounting(self):
        target = self.root / "empty"; target.mkdir(mode=0o700); selected = {**self.selected, "mount_path": str(target)}
        self.assertEqual(self.host.empty(selected)["inode"], target.stat().st_ino)
        self.assertTrue(self.host.inventory_ids()); self.assertIsNone(self.host.mounted(mount.renderer.scope(self.native, self.storage, "raw"), selected))
        (target / "data").write_text("fixture")
        with self.assertRaises(ValueError): self.host.empty(selected)


if __name__ == "__main__": unittest.main()
