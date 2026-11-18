#!/usr/bin/env python3
"""Exercise native CephFS mount identity, descriptor I/O, peer receipts and provider binding."""

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ceph_client_io as exchange
import ceph_native_io as agent
import render_ceph_native_check as renderer
from test_ceph_storage import fixture as storage_fixture, uid, image


def fixture(storage_plan):
    return {"schema_version": 1, "operation_id": uid(800), "storage_runtime": "rke2", "storage_plan_sha256": renderer.storage.fingerprint(storage_plan), "storage_proof_sha256": "d" * 64,
        "hosts": [{"name": "client-" + str(i), "machine_id_sha256": hashlib.sha256((str(i) * 32).encode()).hexdigest(), "boot_id": uid(900 + i), "failure_domain": "rack-" + str(i),
            "mount_path": "/srv/ceph-canary", "package_path": "/opt/platform/ceph-canary", "client": "canary-" + str(i)} for i in (1, 2)],
        "uid": 1000, "gid": 1000, "cephfs_path": "/platform-canaries", "image": image("e"), "payload_bytes": 65536, "timeout_seconds": 10}


def peer_receipts(plan, storage_plan, runtime="raw"):
    selected = renderer.scope(plan, storage_plan, runtime); now = datetime.now(timezone.utc)
    return [{"schema_version": 1, "operation_id": plan["operation_id"], "scope_sha256": agent.digest(selected), "role": role, "host": plan["hosts"][i]["name"], "runtime": runtime,
        "machine_id_sha256": plan["hosts"][i]["machine_id_sha256"], "boot_id": plan["hosts"][i]["boot_id"], "payload_bytes": plan["payload_bytes"],
        "agent_sha256": selected["agent_sha256"], "engine_sha256": selected["engine_sha256"], "mount": {"id": 20 + i, "device": "0:53", "mount_sha256": "a" * 64},
        "started_at": (now - timedelta(seconds=3)).isoformat(), "completed_at": (now - timedelta(seconds=1)).isoformat(), "cephfs_mount_observed": True,
        "payload_sha256": exchange.expected(agent.engine_scope(selected), role), "peer_sha256": exchange.expected(agent.engine_scope(selected), agent.ROLES[1 - i]),
        "peer_barrier_verified": True, **agent.UNVERIFIED} for i, role in enumerate(agent.ROLES)]


class PackageTests(unittest.TestCase):
    def test_raw_and_docker_launchers_validate_by_default_and_need_no_keys(self):
        storage_plan = storage_fixture(); plan = fixture(storage_plan)
        for runtime in ("raw", "docker"):
            files = renderer.render(plan, storage_plan, runtime)
            self.assertEqual(len(files), 6)
            self.assertNotIn("--execute", files["run-cephfs-left.sh"])
            self.assertTrue(all(json.loads(files["render-status.json"])[k] is False for k in renderer.UNVERIFIED))
            selected = json.loads(files["native-scope.json"])
            for name, key in (("ceph_native_io.py", "agent_sha256"), ("ceph_client_io.py", "engine_sha256")):
                self.assertEqual(hashlib.sha256(files[name].encode()).hexdigest(), selected[key])
        command = renderer.command(plan, "docker", 0)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pull=never", "--security-opt=no-new-privileges:true"):
            self.assertIn(flag, command)
        self.assertFalse(any("privileged" in arg or "secretfile" in arg or "docker.sock,dst" in arg for arg in command))
        self.assertIn(plan["image"], command)

    def test_invalid_hosts_root_paths_mutable_images_and_unsupported_runtimes_refuse(self):
        storage_plan = storage_fixture()
        changes = [lambda p: p["hosts"][1].update(machine_id_sha256=p["hosts"][0]["machine_id_sha256"]), lambda p: p["hosts"][1].update(failure_domain=p["hosts"][0]["failure_domain"]),
            lambda p: p["hosts"][0].update(client="admin"), lambda p: p.update(uid=0), lambda p: p.update(gid=True), lambda p: p.update(cephfs_path="/"),
            lambda p: p["hosts"][0].update(mount_path="/srv/data/../data"), lambda p: p["hosts"][0].update(package_path=p["hosts"][0]["mount_path"]),
            lambda p: p.update(image="python:latest"), lambda p: p.update(storage_plan_sha256="0" * 64), lambda p: p.update(password="dummy")]
        for change in changes:
            plan = fixture(storage_plan); change(plan)
            with self.assertRaises(ValueError): renderer.render(plan, storage_plan, "raw")
        with self.assertRaises(ValueError): renderer.render(fixture(storage_plan), storage_plan, "k8s")

    def test_local_agent_validation_does_not_open_host_identity_or_exchange(self):
        storage_plan = storage_fixture(); plan = fixture(storage_plan)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scope.json"; path.write_text(json.dumps(renderer.scope(plan, storage_plan, "raw")))
            with mock.patch.object(agent, "execute", side_effect=AssertionError("live write")), mock.patch("builtins.print"):
                self.assertEqual(agent.main(["--scope", str(path), "--role", "cephfs-left"]), 0)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.scope = renderer.scope(fixture(storage_fixture()), storage_fixture(), "raw"); self.host = self.scope["hosts"][0]

    def mount_line(self, *, target=None):
        selected = self.scope
        source = self.host["client"] + "@" + selected["fsid"] + "." + selected["filesystem"] + "=" + selected["cephfs_path"]
        return f'101 20 0:53 / {target or self.host["mount_path"]} rw,nosuid,nodev,noexec - ceph {source} rw,ms_mode=secure\n'

    def test_exact_mount_and_docker_bind_are_checked(self):
        for target in (self.host["mount_path"], "/volume"):
            with mock.patch.object(agent, "read", return_value=self.mount_line(target=target).encode()):
                self.assertEqual(agent.mount_identity(self.scope, self.host, target)["id"], 101)

    def test_wrong_fsid_filesystem_path_client_security_and_stack_refuse(self):
        line = self.mount_line()
        changes = [lambda s: s.replace(self.scope["fsid"], uid(999)), lambda s: s.replace(self.scope["filesystem"], "foreign-fs"),
            lambda s: s.replace("=/platform-canaries", "=/other"), lambda s: s.replace(self.host["client"] + "@", "admin@"), lambda s: s.replace("ms_mode=secure", "ms_mode=prefer-secure"),
            lambda s: s.replace("nosuid,", ""), lambda s: s.replace("rw,nosuid", "ro,nosuid"), lambda s: s.replace(" - ceph ", " - ext4 "),
            lambda s: s.replace(" 0:53 / ", " 0:53 /child "), lambda s: s.replace("ms_mode=secure", "ms_mode=secure,recover_session=clean"), lambda s: s + s,
            lambda s: s + '102 101 0:54 / /srv/ceph-canary/nested rw - ext4 fixture rw\n']
        for change in changes:
            with mock.patch.object(agent, "read", return_value=change(line).encode()), self.assertRaises(ValueError): agent.mount_identity(self.scope, self.host, self.host["mount_path"])

    def test_nonroot_identity_and_container_witnesses_are_required(self):
        def values(path, limit): return ("1" * 32 if str(path).endswith("machine-id") else self.host["boot_id"]).encode()
        with mock.patch.object(agent.sys, "platform", "linux"), mock.patch.object(agent.os, "geteuid", return_value=1000, create=True), mock.patch.object(agent.os, "getegid", return_value=1000, create=True), mock.patch.object(agent, "read", side_effect=values):
            agent.identity(self.scope, self.host)
            with self.assertRaises(ValueError): agent.identity(self.scope, self.host, container=True)
            docker = {**self.scope, "runtime": "docker"}; agent.identity(docker, self.host, container=True)
            changed = {**self.host, "boot_id": uid(9999)}
            with self.assertRaises(ValueError): agent.identity(self.scope, changed)
        with mock.patch.object(agent.sys, "platform", "linux"), mock.patch.object(agent.os, "geteuid", return_value=0, create=True), self.assertRaises(ValueError): agent.identity(self.scope, self.host)

    @unittest.skipUnless(os.name == "posix", "Descriptor identity requires Linux procfs")
    def test_real_directory_descriptor_cannot_be_replaced_by_another_mount_id(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                value = os.fstat(descriptor); fdinfo = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
                mount_id = int(next(s.split(":", 1)[1] for s in fdinfo.splitlines() if s.startswith("mnt_id:")))
                mounted = {"id": mount_id, "device": f"{os.major(value.st_dev)}:{os.minor(value.st_dev)}"}
                self.assertEqual(agent.descriptor_identity(descriptor, mounted)["inode"], value.st_ino)
                with self.assertRaises(ValueError): agent.descriptor_identity(descriptor, {**mounted, "id": mount_id + 1})
            finally: os.close(descriptor)


class ExchangeTests(unittest.TestCase):
    def packaged_agent(self, plan, storage_plan, directory):
        root = Path(directory)
        files = renderer.render(plan, storage_plan, "raw")
        for filename in ("ceph_native_io.py", "ceph_client_io.py"):
            (root / filename).write_bytes(files[filename].encode())
        spec = importlib.util.spec_from_file_location("packaged_agent", root / "ceph_native_io.py")
        packaged = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packaged)
        return packaged, json.loads(files["native-scope.json"])

    def test_changed_exchange_source_is_refused_before_loading(self):
        selected = renderer.scope(fixture(storage_fixture()), storage_fixture(), "raw"); selected["engine_sha256"] = "0" * 64
        with self.assertRaises(ValueError): agent.engine(selected)

    @unittest.skipUnless(os.name == "posix", "Post-exchange mount check requires POSIX descriptors")
    def test_mount_change_during_exchange_cannot_emit_success(self):
        storage_plan = storage_fixture(); plan = fixture(storage_plan)
        with tempfile.TemporaryDirectory() as directory:
            plan["hosts"][0]["mount_path"] = directory
            packaged, selected = self.packaged_agent(plan, storage_plan, directory)
            mounted = {"id": 101, "device": "0:53", "mount_sha256": "a" * 64}
            fake_engine = mock.Mock(); fake_engine.exchange.return_value = {}
            with mock.patch.object(packaged, "identity"), mock.patch.object(packaged, "engine", return_value=fake_engine), mock.patch.object(packaged, "descriptor_identity", return_value={"device": 53, "inode": 12}), mock.patch.object(packaged, "mount_identity", side_effect=[mounted, {**mounted, "id": 102}]):
                with self.assertRaises(ValueError): packaged.execute(selected, "cephfs-left")
            self.assertEqual(fake_engine.exchange.call_count, 1)

    @unittest.skipUnless(os.name == "posix", "Peer file operations require POSIX descriptors")
    def test_actual_peer_bytes_and_barrier_use_the_open_root_not_its_path(self):
        selected = renderer.scope(fixture(storage_fixture()), storage_fixture(), "raw")
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as executor:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                jobs = {role: executor.submit(exchange.exchange, agent.engine_scope(selected), role, Path("/unreviewed/absent"), root_descriptor=descriptor) for role in agent.ROLES}
                results = {role: future.result(timeout=15) for role, future in jobs.items()}
                self.assertEqual(results["cephfs-left"]["peer_sha256"], results["cephfs-right"]["payload_sha256"])
                with self.assertRaises(FileExistsError): exchange.exchange(agent.engine_scope(selected), "cephfs-left", None, root_descriptor=descriptor)
                os.fstat(descriptor)  # The exchange closes its duplicate, not the caller's handle.
            finally: os.close(descriptor)

    @unittest.skipUnless(os.name == "posix", "Native agent fixture requires POSIX descriptors")
    def test_agent_rechecks_mount_identity_after_real_local_peer_exchange(self):
        storage_plan = storage_fixture(); plan = fixture(storage_plan)
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as executor:
            for host in plan["hosts"]: host["mount_path"] = directory
            packaged, selected = self.packaged_agent(plan, storage_plan, directory)
            # Identity/mount are simulated explicitly; the descriptor and all data I/O are real.
            with mock.patch.object(packaged, "identity"), mock.patch.object(packaged, "mount_identity", return_value={"id": 101, "device": "0:53", "mount_sha256": "a" * 64}), mock.patch.object(packaged, "descriptor_identity", return_value={"device": 53, "inode": 12}):
                jobs = [executor.submit(packaged.execute, selected, role) for role in packaged.ROLES]
                values = [job.result(timeout=15) for job in jobs]
                self.assertTrue(renderer.receipts(plan, storage_plan, "raw", *values)["receipts_sha256"])
            blocked_engine = mock.Mock()
            blocked_engine.exchange.side_effect = AssertionError("must not write")
            with mock.patch.object(packaged, "identity"), mock.patch.object(packaged, "mount_identity", side_effect=ValueError("unmounted")), mock.patch.object(packaged, "engine", return_value=blocked_engine):
                with self.assertRaises(ValueError): packaged.execute(selected, "cephfs-left")
            blocked_engine.exchange.assert_not_called()


class ReceiptTests(unittest.TestCase):
    def setUp(self): self.storage = storage_fixture(); self.plan = fixture(self.storage); self.values = peer_receipts(self.plan, self.storage)

    def test_both_receipts_bind_hosts_operation_program_and_scope(self):
        self.assertEqual(set(renderer.receipts(self.plan, self.storage, "raw", *self.values)["receipts_sha256"]), set(agent.ROLES))

    def test_foreign_old_corrupt_nonoverlapping_or_overclaimed_receipts_refuse(self):
        changes = [lambda v: v[0].update(role="cephfs-right"), lambda v: v[0].update(host="foreign"), lambda v: v[0].update(scope_sha256="0" * 64),
            lambda v: v[0].update(engine_sha256="0" * 64), lambda v: v[0].update(peer_sha256="0" * 64), lambda v: v[0].update(boot_id=uid(999)),
            lambda v: v[0].update(peer_barrier_verified=False), lambda v: v[0].update(ha_verified=True), lambda v: v[0]["mount"].update(id=True),
            lambda v: v[0].update(started_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()),
            lambda v: v[0].update(completed_at=datetime.now().isoformat()),
            lambda v: v[1].update(started_at=datetime.now(timezone.utc).isoformat(), completed_at=datetime.now(timezone.utc).isoformat())]
        for change in changes:
            values = copy.deepcopy(self.values); change(values)
            with self.assertRaises(ValueError): renderer.receipts(self.plan, self.storage, "raw", *values)

    def test_live_verifier_requires_the_original_storage_chain(self):
        from test_ceph_clients import VerificationTests
        setup = VerificationTests("test_local_and_server_check_do_not_create_or_write_volumes"); setup.setUp(); self.addCleanup(setup.doCleanups)
        plan = fixture(setup.storage); plan["storage_proof_sha256"] = hashlib.sha256(setup.proof.read_bytes()).hexdigest()
        values = peer_receipts(plan, setup.storage)
        result = renderer.verify(plan, setup.storage, "raw", setup.proof, *values, client=setup.client)
        self.assertTrue(result["native_cephfs_data_roundtrip_verified"]); self.assertTrue(result["native_cephfs_mounts_verified"])
        self.assertTrue(all(result[k] is False for k in agent.UNVERIFIED)); self.assertFalse(setup.client.attempts)
        self.assertEqual(result["storage_runtime"], "rke2")
        wrong_runtime = {**plan, "storage_runtime": "raw"}
        with self.assertRaises(ValueError): renderer.verify(wrong_runtime, setup.storage, "raw", setup.proof, *peer_receipts(wrong_runtime, setup.storage), client=setup.client)
        setup.client.data[(None, "csidriver/" + setup.storage["csi"]["rbd"]["driver"])]["metadata"]["uid"] = uid(999)
        with self.assertRaises(ValueError): renderer.verify(plan, setup.storage, "raw", setup.proof, *values, client=setup.client)
        self.assertFalse(setup.client.attempts)


if __name__ == "__main__": unittest.main()
