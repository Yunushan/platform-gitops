#!/usr/bin/env python3
"""Exercise real bounded input publication and recovery without services or a VIP."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import struct
import tempfile
import unittest
from unittest import mock

import gateway_edge_pair_staging as staging
import manage_gateway_edge_pair as pair
from test_gateway_edge_pair import fixture, Remote


class StageRemote(Remote):
    def __init__(self, plan):
        super().__init__(plan)
        self.fail_stage = False
    def stage(self, peer, header, inputs, source):
        self.calls.append((peer["host"], "stage", None))
        if self.fail_stage:
            self.fail_stage = False
            raise TimeoutError("Staging acknowledgment lost")
        return {"staged": True, "manifest_sha256": staging.fingerprint(header), "machine_sha256": peer["machine_sha256"]}


class DistributionTests(unittest.TestCase):
    def run_pair(self, root, plan, bundle, gateway, remote, *, mode="apply", distribute=True):
        sources = pair.tool_files()
        ca = bundle.parent / "ca.crt"
        with mock.patch.object(pair, "ROOT", root), mock.patch.object(pair, "tool_files", return_value=sources):
            return pair.operate(plan, bundle, gateway, mode=mode, distribute_inputs=distribute, ca_file=ca,
                transport=remote, observer=lambda: {"vip_reachable": True}, output=root / "private/operation")

    def test_both_distributions_complete_before_any_reservation_or_host_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = StageRemote(plan)
            self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote)["pair_services_ready"])
            self.assertEqual(remote.calls[:4], [("edge-b", "stage", None), ("edge-a", "stage", None), ("edge-b", "reserve", None), ("edge-a", "reserve", None)])
            receipt = json.loads((root / "private/operation/pair-proof.json").read_text())
            self.assertEqual(set(receipt["staging"]), {"edge-a", "edge-b"})

    def test_staging_timeout_keeps_same_journal_and_never_starts_or_reserves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = StageRemote(plan); remote.fail_stage = True
            with self.assertRaises(TimeoutError): self.run_pair(root, plan, bundle, gateway, remote)
            receipt = root / "private/operation/pair-proof.json"
            before = json.loads(receipt.read_text())
            self.assertEqual(remote.calls, [("edge-b", "stage", None)])
            self.assertFalse(remote.jobs); self.assertFalse(remote.leases)
            with self.assertRaises(ValueError): self.run_pair(root, plan, bundle, gateway, remote, mode="resume", distribute=False)
            self.run_pair(root, plan, bundle, gateway, remote, mode="resume")
            self.assertEqual(json.loads(receipt.read_text())["operation_id"], before["operation_id"])

    def test_failed_distribution_can_cancel_without_requiring_more_uploads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, *_ = fixture(root); remote = StageRemote(plan); remote.fail_stage = True
            with self.assertRaises(TimeoutError): self.run_pair(root, plan, bundle, gateway, remote)
            self.assertTrue(self.run_pair(root, plan, bundle, gateway, remote, mode="cancel", distribute=False)["cancelled"])

    def test_scope_rejects_overlaps_system_paths_excessive_files_and_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, bundle, gateway, ca, artifacts, _ = fixture(Path(temporary))
            contract, files = pair.host_manager.capture(bundle, plan["edge_contract_sha256"], gateway)
            inputs = pair.staging_inputs(contract, files, gateway, pair.tool_files(), ca, artifacts)
            header = pair.staging_request(plan["peers"][0], inputs)
            for change in (lambda h: h["roots"]["ca"].update(path="/etc/passwd"),
                           lambda h: h["roots"]["tools"].update(path=h["roots"]["bundle"]["path"]),
                           lambda h: h["roots"]["ca"].update(path=h["roots"]["bundle"]["path"] + "/ca.crt"),
                           lambda h: h["roots"]["bundle"]["files"].update({"../escape": {"size": 0, "sha256": "0" * 64, "mode": 0o644}}),
                           lambda h: h["roots"]["bundle"]["files"].update({"sub/./alias": {"size": 0, "sha256": "0" * 64, "mode": 0o644}}),
                           lambda h: h["roots"]["ca"].update(path="/opt/platform/./alias"),
                           lambda h: h["roots"]["ca"]["files"]["payload"].update(size=staging.FILE_LIMIT + 1),
                           lambda h: h["roots"]["ca"].update(path="/var/lib/platform-gateway-edge/state.json")):
                bad = copy.deepcopy(header); change(bad)
                with self.assertRaises(ValueError): staging.validate(bad)
            self.assertEqual(set(inputs), {"tools", "bundle", "gateway", "ca"})
            self.assertNotIn("private", " ".join(inputs["tools"]))

    def test_native_artifacts_are_pinned_and_nothing_else_is_uploaded(self):
        from test_gateway_edge_host import package
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); bundle, digest, gateway, ca, artifacts = package(root)
            contract, files = pair.host_manager.capture(bundle, digest, gateway)
            (artifacts / "not-approved.key").write_text("must not be captured")
            inputs = pair.staging_inputs(contract, files, gateway, pair.tool_files(), ca, artifacts)
            self.assertEqual(set(inputs["artifacts"]), {"haproxy", "keepalived"})
            (artifacts / "haproxy").write_bytes(b"changed")
            with self.assertRaises(ValueError): pair.staging_inputs(contract, files, gateway, pair.tool_files(), ca, artifacts)

    def test_modified_staging_source_is_refused_before_ssh_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, bundle, gateway, ca, artifacts, tools = fixture(root)
            binary = root / "ssh"; binary.write_bytes(b"fixture SSH executable")
            plan["ssh_binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
            transport = pair.SSH(plan, tools, binary)
            contract, files = pair.host_manager.capture(bundle, plan["edge_contract_sha256"], gateway)
            inputs = pair.staging_inputs(contract, files, gateway, pair.tool_files(), ca, artifacts)
            header = pair.staging_request(plan["peers"][0], inputs)
            with mock.patch.object(pair, "run_bounded") as run, self.assertRaises(ValueError):
                transport.stage(plan["peers"][0], header, inputs, "raise RuntimeError('unreviewed')")
            run.assert_not_called()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0, "Root Linux /run transfer fixtures")
class RealStagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="platform-entry-stage-", dir="/run")
        self.root = Path(self.temporary.name)
        self.inputs = {"tools": {"scripts/tool.py": b"print('fixture only')\n"}, "bundle": {"entry.json": b"fixture bundle"},
                       "gateway": {"gateway.yaml": b"fixture gateway"}, "ca": {"payload": b"fixture public CA"},
                       "artifacts": {"haproxy": b"fixture executable"}}
        self.header = {"schema_version": 1, "machine_sha256": hashlib.sha256(Path("/etc/machine-id").read_bytes().strip()).hexdigest(),
            "roots": {label: {"path": str(self.root / label), "kind": "file" if label == "ca" else "directory",
                              "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest(), "mode": 0o555 if label == "artifacts" else 0o644} for name, value in files.items()}}
                      for label, files in self.inputs.items()}}

    def tearDown(self): self.temporary.cleanup()

    def run_stage(self, wire=None):
        return staging.stage(io.BytesIO(pair.staging_wire(self.header, self.inputs) if wire is None else wire), staging.fingerprint(self.header))

    def assert_unpublished(self):
        self.assertFalse(any(Path(root["path"]).exists() for root in self.header["roots"].values()))

    def test_real_isolated_shell_bootstrap_publish_and_identical_retry(self):
        source = Path(staging.__file__).read_text()
        command = ["/bin/sh", "-c", shlex.join(["/usr/bin/python3", "-I", "-B", "-c", source, staging.fingerprint(self.header)])]
        for _ in range(2):
            result = pair.run_bounded(command, input=pair.staging_wire(self.header, self.inputs), timeout=15, output_max_bytes=65536)
            self.assertEqual(result.returncode, 0, "Local staging bootstrap failed; details suppressed")
            self.assertTrue(json.loads(result.stdout)["staged"])
        for label, root in self.header["roots"].items():
            for name, item in root["files"].items():
                path = Path(root["path"]) if label == "ca" else Path(root["path"]) / name
                staging.verify_file(path, item)
        self.assertEqual((self.root / "artifacts/haproxy").stat().st_mode & 0o777, 0o555)

    def test_transport_binary_framing_then_pinned_agent_identity_in_isolated_shell(self):
        plan, bundle, gateway, ca, artifacts, tools = fixture(self.root)
        contract, files = pair.host_manager.capture(bundle, plan["edge_contract_sha256"], gateway)
        sources = pair.tool_files()
        inputs = pair.staging_inputs(contract, files, gateway, sources, ca, artifacts)
        peer = plan["peers"][0]
        peer.update(machine_sha256=self.header["machine_sha256"], remote_root=str(self.root / "tools"))
        peer["paths"] = {"bundle": str(self.root / "bundle"), "gateway": str(self.root / "gateway"), "ca": str(self.root / "ca"), "artifacts": None}
        shell = Path("/bin/sh")
        plan["ssh_binary_sha256"] = hashlib.sha256(shell.read_bytes()).hexdigest()
        transport = pair.SSH(plan, tools, shell)
        # Exercise the production bounded transport methods through a local shell;
        # SSH authentication/network transport remains a separate live-fleet gate.
        transport.peers[peer["host"]] = [str(shell), "-c"]
        header = pair.staging_request(peer, inputs)
        self.assertTrue(transport.stage(peer, header, inputs, sources["gateway_edge_pair_staging.py"])["staged"])
        request = {"verb": "identity", "operation_id": "1" * 32, "plan_sha256": "2" * 64, "target": plan["edge_contract_sha256"],
            "host": peer["host"], "machine_sha256": peer["machine_sha256"], "paths": {}, "expected_current": "absent", "rollback": False, "tools": tools, "job_id": None}
        self.assertEqual(transport.call(peer, request), {"machine_sha256": peer["machine_sha256"], "toolset_sha256": plan["toolset_sha256"]})
        self.assertFalse((self.root / "tools/scripts/__pycache__").exists())

    def test_truncated_transfer_retries_without_publishing_incomplete_roots(self):
        wire = pair.staging_wire(self.header, self.inputs)
        # Cut within the last file after earlier complete cache files exist.
        with self.assertRaises(ValueError): self.run_stage(wire[:-3])
        self.assert_unpublished()
        self.assertTrue(self.run_stage()["staged"])

    def test_corruption_and_trailing_data_never_publish_any_input(self):
        wire = pair.staging_wire(self.header, self.inputs)
        for bad in (wire[:-1] + bytes([wire[-1] ^ 1]), wire + b"extra"):
            with self.assertRaises(ValueError): self.run_stage(bad)
            self.assert_unpublished()
        self.assertTrue(self.run_stage()["staged"])

    def test_identity_mismatch_cannot_create_transfer_state(self):
        self.header["machine_sha256"] = "f" * 64
        with self.assertRaises(ValueError): self.run_stage()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_foreign_data_and_symlinks_are_never_adopted_or_overwritten(self):
        directory = self.root / "bundle"; directory.mkdir()
        foreign = directory / "foreign.txt"; foreign.write_bytes(b"retain")
        with self.assertRaises(ValueError): self.run_stage()
        self.assertEqual(foreign.read_bytes(), b"retain")
        foreign.unlink(); directory.rmdir()
        outside = self.root / "outside"; outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.run_stage()
        self.assertEqual(list(outside.iterdir()), [])

    def test_published_modified_hardlinked_and_wrong_mode_inputs_are_refused(self):
        self.run_stage()
        path = self.root / "bundle/entry.json"; original = path.read_bytes()
        path.write_bytes(b"modified")
        with self.assertRaises(ValueError): self.run_stage()
        path.write_bytes(original); path.chmod(0o600)
        with self.assertRaises(ValueError): self.run_stage()
        path.chmod(0o644)
        os.link(path, self.root / "unapproved-hardlink")
        with self.assertRaises(ValueError): self.run_stage()

    def test_lost_publication_ack_reuses_published_roots_and_finishes_remaining(self):
        rename = os.rename
        def interrupt(source, target):
            if Path(target) == self.root / "gateway": raise OSError("Fixture publication interruption")
            return rename(source, target)
        with mock.patch.object(staging.os, "rename", side_effect=interrupt), self.assertRaises(OSError): self.run_stage()
        self.assertTrue((self.root / "bundle").exists())
        self.assertFalse((self.root / "gateway").exists())
        self.assertTrue(self.run_stage()["staged"])

    def test_partial_cache_extra_files_and_changed_claim_are_refused(self):
        wire = pair.staging_wire(self.header, self.inputs)
        with self.assertRaises(ValueError): self.run_stage(wire[:-3])
        cache = next(self.root.glob(".platform-entry-inputs-*"))
        extra = cache / "files/unexpected"; extra.write_bytes(b"foreign")
        with self.assertRaises(ValueError): self.run_stage()
        extra.unlink()
        claim = cache / "manifest.json"; claim.write_bytes(b"{}")
        with self.assertRaises(ValueError): self.run_stage()
        self.assert_unpublished()

    def test_a_live_staging_lock_rejects_a_second_writer(self):
        import fcntl
        target = Path(self.header["roots"]["bundle"]["path"])
        key = hashlib.sha256(str(target).encode()).hexdigest()
        lock = target.parent / (".platform-entry-stage-" + key + ".lock")
        with lock.open("wb") as stream:
            lock.chmod(0o600); fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): self.run_stage()
            self.assert_unpublished()
        self.assertTrue(self.run_stage()["staged"])


if __name__ == "__main__": unittest.main()
