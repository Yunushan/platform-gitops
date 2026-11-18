#!/usr/bin/env python3
"""Exercise private bounded snapshot capture, corruption refusal and offline use."""

import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import openbao_snapshot as backup


def fixture():
    return {"schema_version": 1, "source": {"url": "https://bao-a.example.com:8200", "cluster_id": "00000000-0000-0000-0000-000000000001", "version": "2.7.2",
        "raft": {"bao-a": "192.0.2.41:8201", "bao-b": "192.0.2.42:8201", "bao-c": "192.0.2.43:8201"},
        "ca_file": str(Path(tempfile.gettempdir()).resolve() / "fixture-ca.crt"), "ca_sha256": "a" * 64, "token_env": "OPENBAO_SNAPSHOT_TOKEN"},
        "limits": {"request_seconds": 15, "transfer_seconds": 60, "max_bytes": 1024 * 1024}}


def archive(plan, *, corrupt=False, duplicate=False, link=False, oversized=False):
    state = b"fixture encrypted state" * (100000 if oversized else 1)
    meta = backup.canonical({"Version": 1, "Index": 42, "Term": 2, "Size": len(state), "Configuration": {"Servers": [
        {"ID": name, "Address": address, "Suffrage": 0} for name, address in plan["source"]["raft"].items()]}}).encode()
    sums = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in (("meta.json", meta), ("state.bin", state))).encode()
    values = [("meta.json", meta), ("state.bin", state + b"changed" if corrupt else state), ("SHA256SUMS", sums), ("SHA256SUMS.sealed", b"fixture sealed checksum, not authenticated by this parser")]
    if duplicate: values.append(("meta.json", meta))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as package:
        for name, data in values:
            header = tarfile.TarInfo(name); header.size = len(data)
            if link and name == "state.bin": header.type = tarfile.SYMTYPE; header.linkname = "/outside"; header.size = 0
            package.addfile(header, io.BytesIO(data))
    return output.getvalue()


class FakeClient:
    def __init__(self, plan): self.plan = copy.deepcopy(plan); self.calls = []; self.payload = archive(plan); self.downloads = 0; self.root_scope = False; self.changed_after = False
    def json(self, path, *, method="GET", data=None):
        self.calls.append((method, path))
        if path == "sys/health":
            return {"cluster_id": "00000000-0000-0000-0000-000000000002" if self.changed_after and self.downloads else self.plan["source"]["cluster_id"],
                "version": "2.7.2", "initialized": True, "sealed": False, "standby": False}
        if path == backup.READ_PATHS[0]:
            return {"data": {"config": {"servers": [{"node_id": n, "address": a, "voter": True, "leader": i == 0} for i, (n, a) in enumerate(self.plan["source"]["raft"].items())]}}}
        if path == "sys/capabilities-self": return {p: ["root"] if self.root_scope else ["read"] if p in backup.READ_PATHS else ["deny"] for p in data["paths"]}
        raise AssertionError("Unexpected backup request")
    def snapshot(self, output, limits):
        self.downloads += 1; self.calls.append(("GET", backup.READ_PATHS[1])); output.write(self.payload)
        return len(self.payload), hashlib.sha256(self.payload).hexdigest()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root = Path(tmp.name); self.plan = fixture(); self.client = FakeClient(self.plan)
    def save(self, data):
        path = self.root / "input.tar.gz"; path.write_bytes(data); path.chmod(0o600); return path
    def test_local_validation_has_no_network_or_credential_access(self):
        with mock.patch.object(backup, "Client", side_effect=AssertionError("No network")):
            self.assertTrue(backup.operate(self.plan)["local_inputs_verified"])
        self.assertFalse(self.client.calls)
    def test_capture_and_offline_verification_preserve_private_integrity_without_restore_claims(self):
        output = self.root / "capture"
        result = backup.operate(self.plan, mode="capture", output=output, client=self.client)
        self.assertTrue(result["captured"]); self.assertFalse((output / "snapshot.part").exists())
        with mock.patch.object(backup, "Client", side_effect=AssertionError("Offline verifier cannot contact source")):
            result = backup.operate(self.plan, mode="verify", output=output)
        self.assertTrue(result["verified"]); self.assertTrue(result["internal_checksums_verified"])
        for name in backup.FALSE_FLAGS: self.assertIs(result[name], False)
        self.assertTrue(all(method == "GET" or method == "POST" and path == "sys/capabilities-self" for method, path in self.client.calls))
        if os.name == "posix": self.assertEqual((output / "snapshot.tar.gz").stat().st_mode & 0o777, 0o600)
    def test_existing_output_is_not_replaced(self):
        output = self.root / "capture"; output.mkdir(); (output / "retained").write_text("retain")
        with self.assertRaises(FileExistsError): backup.operate(self.plan, mode="capture", output=output, client=self.client)
        self.assertEqual((output / "retained").read_text(), "retain"); self.assertFalse(self.client.calls)
    def test_root_scope_and_wrong_cluster_refuse_before_download(self):
        for root_scope in (True, False):
            client = FakeClient(self.plan); client.root_scope = root_scope
            if not root_scope: client.plan["source"]["cluster_id"] = "00000000-0000-0000-0000-000000000003"
            with self.assertRaises(ValueError): backup.operate(self.plan, mode="capture", output=self.root / str(root_scope), client=client)
            self.assertEqual(client.downloads, 0)
    def test_changed_source_keeps_partial_without_marking_it_complete(self):
        self.client.changed_after = True; output = self.root / "capture"
        with self.assertRaises(ValueError): backup.operate(self.plan, mode="capture", output=output, client=self.client)
        self.assertTrue((output / "snapshot.part").exists()); self.assertFalse((output / "snapshot.tar.gz").exists())
        self.assertFalse(json.loads((output / "openbao-snapshot-proof.json").read_text())["captured"])
    def test_internal_corruption_duplicates_links_and_expansion_are_refused(self):
        for choice in ("corrupt", "duplicate", "link", "oversized"):
            path = self.save(archive(self.plan, **{choice: True}))
            with self.subTest(case=choice), self.assertRaises(ValueError): backup.verify_archive(path, self.plan)
    def test_gzip_trailer_is_checked_even_after_tar_end_marker(self):
        path = self.save(archive(self.plan)[:-5])
        with self.assertRaises((EOFError, gzip.BadGzipFile)): backup.verify_archive(path, self.plan)
    def test_wrong_metadata_voters_or_changed_saved_proof_refuse(self):
        path = self.save(archive(self.plan)); changed = copy.deepcopy(self.plan); changed["source"]["raft"]["bao-a"] = "192.0.2.51:8201"
        with self.assertRaises(ValueError): backup.verify_archive(path, changed)
        output = self.root / "capture"; backup.operate(self.plan, mode="capture", output=output, client=self.client)
        proof = output / "openbao-snapshot-proof.json"; data = json.loads(proof.read_text()); data["snapshot_bytes"] += 1; proof.write_text(json.dumps(data))
        with self.assertRaises(ValueError): backup.operate(self.plan, mode="verify", output=output)
    def test_client_rejects_redirects_and_oversized_headers_without_forwarding_credentials(self):
        response = mock.Mock(status=302); connection = mock.Mock(); connection.getresponse.return_value = response
        source = self.plan["source"]
        with mock.patch.object(backup, "read_bootstrap_bytes", return_value=b"fixture-ca"), mock.patch.object(backup.ssl, "create_default_context"), mock.patch.dict(os.environ, {source["token_env"]: "fixture-only-bearer"}):
            source["ca_sha256"] = hashlib.sha256(b"fixture-ca").hexdigest(); client = backup.Client(self.plan)
        with mock.patch.object(client, "connect", return_value=connection):
            with self.assertRaises(ValueError): client.snapshot(io.BytesIO(), self.plan["limits"])
            self.assertEqual(connection.request.call_count, 1)
            response.status = 200; response.getheader.return_value = str(self.plan["limits"]["max_bytes"] + 1)
            with self.assertRaises(ValueError): client.snapshot(io.BytesIO(), self.plan["limits"])
            response.read.assert_not_called()
            response.read1.assert_not_called()
    def test_plan_rejects_credentials_redirect_shapes_and_unbounded_limits(self):
        for field, value in (("url", "http://bao.example.com"), ("url", "https://user:password@bao.example.com"), ("url", "https://bao.example.com/path"),
                             ("url", "https://bao.example.com:0"), ("token_env", "literal credential"), ("version", "latest")):
            plan = copy.deepcopy(self.plan); plan["source"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): backup.validate(plan)
        plan = copy.deepcopy(self.plan); plan["limits"]["max_bytes"] = True
        with self.assertRaises(ValueError): backup.validate(plan)


if __name__ == "__main__": unittest.main()
