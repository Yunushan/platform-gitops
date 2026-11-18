#!/usr/bin/env python3
"""Check update-center fixture metadata pinning."""
from pathlib import Path
import tempfile
import unittest

import prepare_jenkins_fixture_plugins as fixture


class JenkinsFixturePlugins(unittest.TestCase):
    def test_unreviewed_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text('{"plugins": {}}', encoding="utf-8")
            with self.assertRaises(ValueError): fixture.resolve(path)


if __name__ == "__main__": unittest.main()
