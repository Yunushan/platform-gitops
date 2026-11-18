#!/usr/bin/env python3
"""Exercise original Docker handles, exact peer policy and interrupted create/start observation."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import manage_ceph_native_docker as runner
import render_ceph_native_check as renderer
from test_ceph_native import fixture as native_fixture, peer_receipts
from test_ceph_storage import fixture as storage_fixture


def fixture(storage):
    plan = native_fixture(storage)
    plan["docker_hosts"] = {h["name"]: {"binary_sha256": "a" * 64, "daemon_id": "engine-" + str(i) * 32, "server_version": "29.0.0", "image_id": "sha256:" + "b" * 64} for i, h in enumerate(plan["hosts"], 1)}
    return plan


class Host:
    def __init__(self, plan, storage):
        self.plan, self.storage = plan, storage; self.row = None; self.creates = 0; self.starts = 0; self.lost_create = False; self.lost_start = False; self.reject_create = False; self.reject_start = False
        self.inject = None; self.exit_code = 0; self.hold = False; self.bad_logs = False
        self.receipt = None
        self.current = {"image": {"Id": "sha256:" + "b" * 64, "Config": {"Env": ["PATH=/usr/local/bin:/usr/bin"], "Labels": {}}},
            "socket": {"device": 1, "inode": 20}, "mount": {"id": 30, "device": "0:53", "mount_sha256": "a" * 64}}

    def prerequisites(self, plan, storage, role): return copy.deepcopy(self.current)
    def find(self, name): return self.row["Id"] if self.row else None
    def inspect(self, cid):
        if self.row is None: raise ValueError("Fixture original container missing")
        return copy.deepcopy(self.row)

    def create(self, arguments):
        self.creates += 1
        if self.reject_create: raise ValueError("Fixture uncertain create")
        role = arguments[arguments.index("--role") + 1]; index = runner.agent.ROLES.index(role); selected = self.plan["hosts"][index]
        labels = dict(arg.split("=", 1) for i, arg in enumerate(arguments) if i and arguments[i - 1] == "--label")
        self.row = {"Id": str(index + 1) * 64, "Name": "/" + runner.name(self.plan, role), "Image": self.current["image"]["Id"], "RestartCount": 0, "AppArmorProfile": "docker-default",
            "Config": {"Image": self.plan["image"], "User": "1000:1000", "Entrypoint": ["python3"], "Cmd": arguments[arguments.index(self.plan["image"]) + 1:], "Labels": labels,
                "Env": self.current["image"]["Config"]["Env"], "WorkingDir": "/", "Hostname": "ceph-canary", "Tty": False, "OpenStdin": False, "Healthcheck": {"Test": ["NONE"]}},
            "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "none", "IpcMode": "none", "CgroupnsMode": "private", "UsernsMode": "host", "Runtime": "runc",
                "PidsLimit": 64, "Memory": 512 * 1024 ** 2, "MemorySwap": 512 * 1024 ** 2, "NanoCpus": 1000000000, "AutoRemove": False, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}}, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "Mounts": [{}, {}, {}, {}]},
            "Mounts": [{"Source": source, "Destination": target, "RW": rw, "Type": "bind", "Propagation": "rprivate"} for source, target, rw in (
                (selected["package_path"], "/program", False), (selected["mount_path"], "/volume", True), ("/etc/machine-id", "/identity/machine-id", False), ("/proc/sys/kernel/random/boot_id", "/identity/boot-id", False))],
            "NetworkSettings": {"Networks": {"none": {}}}, "State": {"Status": "created", "Running": False}}
        if self.inject: self.inject(self.row)
        if self.lost_create: self.lost_create = False; raise ValueError("Fixture create acknowledgement lost")
        return self.row["Id"]

    def start(self, cid):
        self.starts += 1
        if self.reject_start: raise ValueError("Fixture uncertain start")
        role = self.row["Config"]["Labels"][runner.LABEL + "role"]
        if self.receipt is None: self.receipt = peer_receipts(self.plan, self.storage, "docker")[runner.agent.ROLES.index(role)]
        before, after = (datetime.fromisoformat(self.receipt[k]) for k in ("started_at", "completed_at"))
        self.row["State"] = {"Status": "running" if self.hold else "exited", "Running": self.hold, "ExitCode": self.exit_code,
            "StartedAt": (before - timedelta(seconds=1)).isoformat(), "FinishedAt": (after + timedelta(seconds=1)).isoformat(), "OOMKilled": False, "Restarting": False, "Dead": False, "Error": ""}
        if self.lost_start: self.lost_start = False; raise ValueError("Fixture start acknowledgement lost")

    def logs(self, cid):
        return "truncated" if self.bad_logs else json.dumps(self.receipt) + "\n"


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.storage = storage_fixture(); self.plan = fixture(self.storage); self.host = Host(self.plan, self.storage)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name); (self.root / "private").mkdir()
        patch = mock.patch.object(runner, "ROOT", self.root); patch.start(); self.addCleanup(patch.stop)
        self.output = self.root / "private/docker-peer"; self.role = "cephfs-left"

    def run_peer(self, mode="create"):
        return runner.operate(self.plan, self.storage, self.role, mode=mode, output=self.output, host=self.host)

    def test_local_and_host_checks_have_no_create_or_start(self):
        self.assertFalse(self.run_peer("local")["docker_runtime_verified"])
        self.assertTrue(self.run_peer("check-host")["host_checked"])
        self.assertEqual((self.host.creates, self.host.starts), (0, 0)); self.assertFalse(self.output.exists())

    def test_original_container_receipt_and_resume_preserve_one_execution(self):
        result = self.run_peer(); self.assertTrue(result["docker_runtime_verified"])
        self.assertFalse(result["native_cephfs_data_roundtrip_verified"])
        self.assertTrue(self.run_peer("resume")["docker_runtime_verified"])
        self.assertEqual((self.host.creates, self.host.starts), (1, 1))
        proof = json.loads((self.output / "docker-peer-proof.json").read_text())
        runner.verify_proof(self.plan, self.storage, self.role, proof, proof["agent_receipt"])

    def test_create_or_start_acknowledgement_loss_is_observed_without_repetition(self):
        for index, attribute in enumerate(("lost_create", "lost_start")):
            self.output = self.root / "private" / str(index); self.host = Host(self.plan, self.storage); setattr(self.host, attribute, True)
            with self.assertRaises(ValueError): self.run_peer()
            self.assertTrue(self.run_peer("resume")["docker_runtime_verified"])
            self.assertEqual((self.host.creates, self.host.starts), (1, 1))

    def test_missing_uncertain_create_or_unstarted_uncertain_start_is_not_repeated(self):
        for index, attribute in enumerate(("reject_create", "reject_start")):
            self.output = self.root / "private" / str(index); self.host = Host(self.plan, self.storage); setattr(self.host, attribute, True)
            with self.assertRaises(ValueError): self.run_peer()
            setattr(self.host, attribute, False)
            with self.assertRaises(ValueError): self.run_peer("resume")
            self.assertEqual((self.host.creates, self.host.starts), (1, index))

    def test_different_image_binds_privileges_network_and_command_refuse_before_start(self):
        changes = [lambda r: r.update(Image="sha256:" + "c" * 64), lambda r: r["Config"].update(Entrypoint=["sh"]), lambda r: r["Config"].update(Env=["CREDENTIAL=dummy"]),
            lambda r: r["HostConfig"].update(Privileged=True), lambda r: r["HostConfig"].update(NetworkMode="host"), lambda r: r["HostConfig"].update(Devices=[{"PathOnHost": "/dev/sda"}]),
            lambda r: r["HostConfig"].update(SecurityOpt=["seccomp=unconfined"]), lambda r: r["HostConfig"].update(AutoRemove=True), lambda r: r["HostConfig"].update(RestartPolicy={"Name": "always", "MaximumRetryCount": 0}),
            lambda r: r["Mounts"][0].update(Source="/"), lambda r: r["Mounts"][0].update(RW=True), lambda r: r["Mounts"].append({"Type": "volume", "Destination": "/extra"})]
        for index, change in enumerate(changes):
            self.output = self.root / "private" / str(index); self.host = Host(self.plan, self.storage); self.host.inject = change
            with self.assertRaises(ValueError): self.run_peer()
            self.assertEqual(self.host.starts, 0)

    def test_changed_container_policy_or_handle_refuses_resume(self):
        self.run_peer(); self.host.row["HostConfig"]["NanoCpus"] = 2000000000
        with self.assertRaises(ValueError): self.run_peer("resume")
        self.assertEqual(self.host.starts, 1)
        self.host.row["HostConfig"]["NanoCpus"] = 1000000000; self.host.row["Id"] = "f" * 64
        with self.assertRaises(ValueError): self.run_peer("resume")

    def test_failed_exit_or_invalid_log_cannot_be_verified(self):
        for index, attribute in enumerate(("exit_code", "bad_logs")):
            self.output = self.root / "private" / str(index); self.host = Host(self.plan, self.storage); setattr(self.host, attribute, 1)
            with self.assertRaises(ValueError): self.run_peer()
            self.assertFalse((self.output / "docker-peer-proof.json").exists())

    def test_still_running_peer_is_polled_without_another_start(self):
        self.host.hold = True
        def complete(_): self.host.row["State"].update(Status="exited", Running=False)
        with mock.patch.object(runner.time, "sleep", side_effect=complete): self.assertTrue(self.run_peer()["docker_runtime_verified"])
        self.assertEqual((self.host.creates, self.host.starts), (1, 1))

    def test_source_change_stops_before_mutation(self):
        original = runner.toolset()
        with mock.patch.object(runner, "toolset", side_effect=[original, "0" * 64]):
            with self.assertRaises(ValueError): self.run_peer()
        self.assertEqual(self.host.creates, 0)

    def test_trusted_path_helper_is_included_in_source_identity(self):
        original = runner.toolset(); read = runner.read_bounded_bytes
        def changed(path, **kwargs):
            value = read(path, **kwargs)
            return value + b"\n# changed trust policy\n" if path.name == "start_gateway_edge.py" else value
        with mock.patch.object(runner, "read_bounded_bytes", side_effect=changed): self.assertNotEqual(runner.toolset(), original)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "Trusted local socket fixture requires root Linux")
    def test_real_socket_and_package_metadata_require_the_reviewed_engine_and_image(self):
        trusted_temp = tempfile.TemporaryDirectory(prefix="ceph-docker-host-", dir="/run"); self.addCleanup(trusted_temp.cleanup); trusted_root = Path(trusted_temp.name)
        binary = trusted_root / "docker"; binary.write_bytes(b"fixture reviewed binary\n"); binary.chmod(0o700)
        socket_path = trusted_root / "docker.sock"; server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.addCleanup(server.close)
        server.bind(str(socket_path)); socket_path.chmod(0o600)
        package = trusted_root / "package"; package.mkdir(mode=0o700)
        self.plan["hosts"][0]["package_path"] = str(package)
        pin = self.plan["docker_hosts"]["client-1"]; pin["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        for key, value in renderer.render(self.plan, self.storage, "docker").items():
            path = package / key; path.write_bytes(value.encode()); path.chmod(0o600)
        facts = {"id": pin["daemon_id"], "os": "linux", "security": ["name=seccomp,profile=builtin"], "runtime": "runc"}
        image = {"Id": pin["image_id"], "RepoDigests": [self.plan["image"]], "Os": "linux", "Architecture": {"x86_64": "amd64", "aarch64": "arm64"}[os.uname().machine], "Config": {}}
        actual = runner.Host(binary=binary, socket=socket_path)
        def run(args): return json.dumps(facts if args[0] == "info" else pin["server_version"] if args[0] == "version" else [image])
        def identity(path, limit): return ("1" * 32 if str(path).endswith("machine-id") else self.plan["hosts"][0]["boot_id"]).encode()
        with mock.patch.object(actual, "run", side_effect=run), mock.patch.object(runner.agent, "read", side_effect=identity), mock.patch.object(runner.agent, "mount_identity", return_value=self.host.current["mount"]):
            self.assertEqual(actual.prerequisites(self.plan, self.storage, self.role)["image"]["Id"], pin["image_id"])
            changes = [(facts, "id", "foreign-daemon-00000000000000"), (facts, "os", "windows"), (facts, "security", ["name=rootless"]),
                (facts, "runtime", "unreviewed"), (image, "Id", "sha256:" + "c" * 64), (image, "RepoDigests", []), (image, "Config", {"Volumes": {"/extra": {}}})]
            for value, key, invalid in changes:
                original = value[key]; value[key] = invalid
                with self.assertRaises(ValueError): actual.prerequisites(self.plan, self.storage, self.role)
                value[key] = original
            socket_path.chmod(0o666)
            with self.assertRaises(ValueError): actual.prerequisites(self.plan, self.storage, self.role)

    def test_pair_collector_requires_both_reviewed_docker_execution_proofs(self):
        from test_ceph_clients import VerificationTests
        setup = VerificationTests("test_local_and_server_check_do_not_create_or_write_volumes"); setup.setUp(); self.addCleanup(setup.doCleanups)
        self.storage = setup.storage; self.plan = fixture(self.storage); self.plan["storage_proof_sha256"] = hashlib.sha256(setup.proof.read_bytes()).hexdigest()
        proofs = []; original_receipts = peer_receipts(self.plan, self.storage, "docker")
        for index, role in enumerate(runner.agent.ROLES):
            self.role = role; self.output = self.root / "private" / str(index); self.host = Host(self.plan, self.storage)
            self.host.receipt = original_receipts[index]
            self.assertTrue(self.run_peer()["docker_runtime_verified"])
            proofs.append(json.loads((self.output / "docker-peer-proof.json").read_text()))
        receipts = [p["agent_receipt"] for p in proofs]
        result = renderer.verify(self.plan, self.storage, "docker", setup.proof, *receipts, client=setup.client, docker_proofs=proofs)
        self.assertTrue(result["docker_runtime_verified"]); self.assertTrue(result["native_cephfs_data_roundtrip_verified"])
        self.assertFalse(result["ha_verified"]); self.assertFalse(setup.client.attempts)
        with self.assertRaises(ValueError): renderer.verify(self.plan, self.storage, "docker", setup.proof, *receipts, client=setup.client)
        changed = copy.deepcopy(proofs); changed[0]["docker"]["daemon_id"] = "foreign-engine-0000000000000000"
        with self.assertRaises(ValueError): renderer.verify(self.plan, self.storage, "docker", setup.proof, *receipts, client=setup.client, docker_proofs=changed)


if __name__ == "__main__": unittest.main()
