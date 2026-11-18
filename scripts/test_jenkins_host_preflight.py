#!/usr/bin/env python3
"""Optional isolated root-owned staging check; does not start Jenkins."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import check_jenkins_host as preflight


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0 and os.environ.get("JENKINS_HOST_PREFLIGHT_FIXTURE") == "1",
                     "isolated native staging fixture not requested")
class JenkinsHostPreflight(unittest.TestCase):
    def test_root_controlled_staging_and_artifact_drift(self):
        import grp
        import pwd
        uid, gid = pwd.getpwnam("nobody").pw_uid, grp.getgrnam("nogroup").gr_gid
        with tempfile.TemporaryDirectory(prefix="jenkins-preflight-fixture-", dir="/run") as directory:
            root = Path(directory); root.chmod(0o755)
            home = root / "home"; home.mkdir(mode=0o700); os.chown(home, uid, gid)
            java = root / "java"; shutil.copyfile("/usr/bin/true", java); java.chmod(0o755)
            war = root / "jenkins.war"; war.write_bytes(b"synthetic-fixture-war"); war.chmod(0o644)
            digest = hashlib.sha256(war.read_bytes()).hexdigest()
            checksum = root / "jenkins.sha256"; checksum.write_text(digest + "  " + str(war) + "\n", encoding="utf-8"); checksum.chmod(0o644)
            plan = {"schema_version": 1, "runtime": "raw", "name": "jenkins", "home": str(home), "port": 8080,
                    "image": None, "java": str(java), "war": str(war), "war_sha256": digest,
                    "checksum_file": str(checksum), "user": "nobody", "group": "nogroup"}
            result = preflight.check(plan)
            self.assertTrue(result["war_sha256_verified"])
            self.assertTrue(result["home_permissions_verified"])
            self.assertFalse(result["jenkins_started"])
            war.write_bytes(b"changed")
            with self.assertRaises(ValueError): preflight.check(plan)
            war.write_bytes(b"synthetic-fixture-war"); checksum.write_text("bad checksum\n", encoding="utf-8")
            with self.assertRaises(ValueError): preflight.check(plan)


if __name__ == "__main__": unittest.main()
