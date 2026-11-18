#!/usr/bin/env python3
"""Refuse changed executable bytes and untyped/ambiguous client build receipts."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_harbor_cache_runtime_fixture as fixture


class HarborCacheArtifactTests(unittest.TestCase):
    def prepare(self, root):
        material = b"synthetic fixture executable bytes; never executed"
        digest = hashlib.sha256(material).hexdigest()
        (root / "harbor-cache-client").write_bytes(material)
        manifest = {"harbor_version": "2.15.1", "harbor_revision": fixture.REVISION, "go_version": "go1.25.9", "go_archive_sha256": fixture.GO_SHA,
            "harbor_archive_sha256": fixture.SOURCE_SHA, "fixture_sha256": hashlib.sha256((Path(fixture.__file__).parent / "fixtures/harbor-cache-client/main.go").read_bytes()).hexdigest(),
            "harbor_sources_sha256": fixture.SOURCES, "binary_sha256": digest, "build_exit": {"exit_code": 0}, "original_build_process_and_cgroup_removed": True,
            "application_compatibility_verified": False, "publisher_signatures_verified": False}
        (root / "build-inputs.json").write_text(json.dumps(manifest))
        return material, digest, manifest

    def test_validated_bytes_stage_once_and_never_overwrite_an_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); material, digest, _ = self.prepare(root); target = root / "staged"
            with mock.patch.object(fixture, "BINARY_SHA", digest):
                self.assertEqual(fixture.stage_artifact(root, target), target)
                self.assertEqual(target.read_bytes(), material)
                with self.assertRaises(FileExistsError): fixture.stage_artifact(root, target)
                self.assertEqual(target.read_bytes(), material)

    def test_numeric_flags_changed_source_extra_fields_and_duplicate_keys_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, digest, manifest = self.prepare(root)
            mutations = [{**manifest, "original_build_process_and_cgroup_removed": 1}, {**manifest, "publisher_signatures_verified": 0},
                {**manifest, "fixture_sha256": "0" * 64}, {**manifest, "additional": True}]
            with mock.patch.object(fixture, "BINARY_SHA", digest):
                for value in mutations:
                    (root / "build-inputs.json").write_text(json.dumps(value))
                    with self.assertRaises(ValueError): fixture.artifact(root)
                (root / "build-inputs.json").write_text(json.dumps(manifest)[:-1] + ',"binary_sha256":"' + digest + '"}')
                with self.assertRaises(ValueError): fixture.artifact(root)

    def test_changed_second_read_refuses_before_creating_an_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, digest, _ = self.prepare(root); original = fixture.read_bounded_bytes; target = root / "staged"
            def swapped(path, **kwargs):
                return b"changed after initial validation" if path.name == "harbor-cache-client" else original(path, **kwargs)
            with mock.patch.object(fixture, "BINARY_SHA", digest), mock.patch.object(fixture, "read_bounded_bytes", side_effect=swapped):
                with self.assertRaises(ValueError): fixture.stage_artifact(root, target)
            self.assertFalse(target.exists())


if __name__ == "__main__": unittest.main()
