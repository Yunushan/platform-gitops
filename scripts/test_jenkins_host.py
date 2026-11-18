#!/usr/bin/env python3
"""Check single-active Jenkins raw-host and Docker renderers."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

from render_jenkins_host import render


class JenkinsHostTest(unittest.TestCase):
    def setUp(self):
        self.raw = {"schema_version": 1, "runtime": "raw", "name": "jenkins", "home": "/var/lib/platform-jenkins",
                    "port": 8080, "image": None, "java": "/opt/java/bin/java", "war": "/opt/jenkins/jenkins.war",
                    "war_sha256": "a" * 64, "checksum_file": "/etc/platform-jenkins/jenkins.war.sha256",
                    "user": "jenkins", "group": "jenkins"}
        self.docker = dict(self.raw, runtime="docker", image="jenkins/jenkins@sha256:" + "b" * 64,
                           java=None, war=None, war_sha256=None, checksum_file=None, user=None, group=None)

    def test_native_is_nonroot_local_and_checks_war(self):
        files = render(self.raw)
        unit = files["jenkins.service"]
        self.assertIn("User=jenkins\n", unit)
        self.assertIn("--httpListenAddress=127.0.0.1", unit)
        self.assertIn("ExecStartPre=/usr/bin/sha256sum --check --status", unit)
        self.assertEqual(files["jenkins.war.sha256"], "a" * 64 + "  /opt/jenkins/jenkins.war\n")

    def test_docker_binds_local_and_does_not_mount_socket(self):
        service = yaml.safe_load(render(self.docker)["compose.yaml"])["services"]["jenkins"]
        self.assertEqual(service["ports"], ["127.0.0.1:8080:8080"])
        self.assertEqual(service["user"], "1000:1000")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["volumes"][0]["target"], "/var/jenkins_home")
        self.assertFalse(service["volumes"][0]["bind"]["create_host_path"])

    def test_invalid_host_values_rejected(self):
        for key, value in [("home", "/home/jenkins"), ("home", "/tmp/foo/../bar"), ("port", True),
                           ("user", "root"), ("war", "/var/lib/platform-jenkins/jenkins.war"),
                           ("checksum_file", "/etc/unsafe%value"), ("war_sha256", "bad")]:
            with self.subTest(key=key):
                plan = copy.deepcopy(self.raw); plan[key] = value
                with self.assertRaises(ValueError): render(plan)
        with self.assertRaises(ValueError): render(dict(self.docker, image="jenkins/jenkins:lts"))

    @unittest.skipUnless(os.name == "posix" and os.environ.get("JENKINS_SYSTEMD_VERIFY") == "1" and shutil.which("systemd-analyze"),
                         "isolated systemd unit validation not requested")
    def test_systemd_unit_parses_with_local_verifier(self):
        plan = dict(self.raw, java="/usr/bin/true", user="nobody", group="nogroup")
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / "jenkins-fixture.service"
            unit.write_text(render(plan)["jenkins.service"], encoding="utf-8")
            subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True, timeout=30, check=True)


if __name__ == "__main__": unittest.main()
