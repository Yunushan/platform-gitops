"""Verify fixture fingerprints detect content, name and symlink changes."""

from pathlib import Path
import tempfile
import unittest

from zuul_fixture_integrity import fingerprint


class FixtureIntegrityTests(unittest.TestCase):
    def test_content_and_unexpected_file_change_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = root / "file"
            item.write_bytes(b"fixture")
            before = fingerprint(root)
            self.assertEqual(before, fingerprint(root))
            item.write_bytes(b"changed")
            self.assertNotEqual(before['digest'], fingerprint(root)['digest'])
            item.write_bytes(b"fixture")
            (root / "added").write_bytes(b"extra")
            self.assertNotEqual(before['digest'], fingerprint(root)['digest'])

    def test_escaping_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.write_bytes(b"outside")
            try:
                (root / "link").symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "escapes"):
                fingerprint(root)


if __name__ == "__main__":
    unittest.main()
