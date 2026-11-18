#!/usr/bin/env python3
"""Keep registry credentials on original upload paths and reject artifact drift."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_harbor_registry_runtime_fixture as fixture


class RegistryContractTests(unittest.TestCase):
    def test_upload_state_cannot_redirect_credentials_or_change_the_repository(self):
        path = fixture.REPOSITORY + "/blobs/uploads/00000000-0000-0000-0000-000000000001"
        self.assertEqual(fixture.upload_location(path + "?_state=original"), path + "?_state=original")
        for value in ("https://unreviewed.example.com" + path + "?_state=x", "//unreviewed.example.com" + path + "?_state=x",
                path.replace("fixture/ha", "another/repo") + "?_state=x", path + "?_state=a&_state=b", path + "?_state=a&digest=sha256:x",
                path + "?_state=", path + "?_state=x#fragment", path + "?_state=x\r\nHeader:value", path.replace("uploads/", "uploads/%2e%2e/") + "?_state=x"):
            with self.subTest(value=value), self.assertRaises(ValueError): fixture.upload_location(value)

    def prepare(self, root):
        raw = b"synthetic registry artifact; not executed"; digest = hashlib.sha256(raw).hexdigest(); (root / "registry").write_bytes(raw)
        manifest = {"exit": {"exit_code": 0}, "sources_unchanged": True, "original_process_and_cgroup_removed": True, "seconds": 1,
            "log_sha256": "a" * 64, "built": True, "runtime_verified": False, "publisher_signatures_verified": False,
            "source_archive_sha256": fixture.SOURCE_SHA, "source_revision": fixture.REVISION, "go_archive_sha256": fixture.GO_SHA,
            "tags": ["include_oss", "include_gcs"], "binary_sha256": digest,
            "version_output": "fixture-registry github.com/docker/distribution 2.8.3-harbor.1-rc.1"}
        (root / "build-inputs.json").write_text(json.dumps(manifest)); return raw, digest, manifest

    def test_untyped_or_changed_provenance_and_executable_bytes_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw, digest, manifest = self.prepare(root)
            with mock.patch.object(fixture, "BINARY_SHA", digest):
                for value in ({**manifest, "built": 1}, {**manifest, "exit": {"exit_code": False}}, {**manifest, "tags": ["include_oss"]}, {**manifest, "source_revision": "0" * 40}, {**manifest, "extra": True}):
                    (root / "build-inputs.json").write_text(json.dumps(value))
                    with self.assertRaises(ValueError): fixture.stage_artifact(root, root / "staged")
                (root / "build-inputs.json").write_text(json.dumps(manifest)); (root / "registry").write_bytes(b"changed")
                with self.assertRaises(ValueError): fixture.stage_artifact(root, root / "staged")
                self.assertFalse((root / "staged").exists())
                (root / "registry").write_bytes(raw); target = fixture.stage_artifact(root, root / "staged"); self.assertEqual(target.read_bytes(), raw)
                with self.assertRaises(FileExistsError): fixture.stage_artifact(root, target)
                self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__": unittest.main()
