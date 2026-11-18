"""Opt-in regression for the actual Zuul 14.3 Ansible validation behavior."""

import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import start_zuul


@unittest.skipUnless(os.environ.get("ZUUL_EXECUTOR_MISSING_ANSIBLE_TEST") == "1",
                     "explicit disposable SDK with missing Ansible required")
class RealExecutorPreflightTests(unittest.TestCase):
    def test_missing_ansible_is_refused_despite_upstream_cli_exit_zero(self):
        self.assertEqual(importlib.metadata.version("zuul"), "14.3.0")
        self.assertNotEqual(os.geteuid(), 0)
        bindir = Path(sys.executable).parent
        manager = bindir / "zuul-manage-ansible"
        daemon = bindir / "zuul-executor"
        result = subprocess.run([str(manager), "--validate"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False, timeout=120)
        self.assertEqual(result.returncode, 0)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"PATH": str(bindir) + ":/usr/bin:/bin"}):
            with self.assertRaisesRegex(start_zuul.ZuulLaunchError, "Ansible validation failed"):
                start_zuul.executor_checks(Path(directory) / "zuul.conf", Path(directory), str(daemon))
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
