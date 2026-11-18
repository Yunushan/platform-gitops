"""Safety checks for the disposable SSH worker artifact preparation."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import prepare_zuul_worker_fixture as worker


class WorkerPreparationTests(unittest.TestCase):
    def test_tree_identity_detects_file_changes_and_extra_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            path = root / "sub" / "file"
            path.write_bytes(b"reviewed")
            baseline = worker.tree_identity(root)
            path.write_bytes(b"changed")
            self.assertNotEqual(worker.tree_identity(root), baseline)
            path.write_bytes(b"reviewed")
            (root / "extra").write_bytes(b"extra")
            self.assertNotEqual(worker.tree_identity(root), baseline)

    def test_verifier_rejects_changed_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            target = private / "worker"
            target.mkdir()
            (target / "artifact").write_bytes(b"changed")

            def prepare_expected(_source, output):
                output.mkdir()
                (output / "artifact").write_bytes(b"reviewed")
                return {}

            with mock.patch.object(worker, "ROOT", root), mock.patch.object(worker, "prepare", side_effect=prepare_expected):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    worker.verify(root, target)
            self.assertEqual((target / "artifact").read_bytes(), b"changed")

    def test_changed_package_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "private").mkdir()
            source = root / "packages"
            source.mkdir()
            (source / "fixture.deb").write_bytes(b"changed")
            expected = hashlib.sha256(b"reviewed").hexdigest()
            target = root / "private" / "worker"
            with mock.patch.object(worker, "ROOT", root), mock.patch.object(worker.sys, "platform", "linux"), \
                    mock.patch.object(worker, "PACKAGES", {"fixture.deb": expected}), \
                    mock.patch.object(worker, "run_bounded") as extract:
                with self.assertRaises(ValueError):
                    worker.prepare(source, target)
                extract.assert_not_called()
                self.assertFalse(target.exists())

    def test_public_and_existing_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            target = private / "existing"
            target.mkdir()
            with mock.patch.object(worker, "ROOT", root), mock.patch.object(worker.sys, "platform", "linux"), \
                    mock.patch.object(worker, "run_bounded") as extract:
                for output in (target, root / "public"):
                    with self.assertRaises(ValueError):
                        worker.prepare(root, output)
                extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
