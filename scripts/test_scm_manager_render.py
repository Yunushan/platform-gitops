import json
from pathlib import Path
import unittest

import yaml

import render_scm_manager as renderer


def example(runtime):
    path = Path(__file__).resolve().parents[1] / "examples" / f"scm-manager-{runtime}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class SCMManagerRenderTest(unittest.TestCase):
    def test_cluster_packages_share_single_writer_and_secret_reference(self):
        plan = {**example("kubernetes"), "bootstrap_secret": "private-bootstrap"}
        for runtime in renderer.CLUSTER_RUNTIMES:
            with self.subTest(runtime=runtime):
                policy, service, deployment = yaml.safe_load_all(renderer.render(plan, runtime)["scm-manager.yaml"])
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["type"], "ClusterIP")
                spec = deployment["spec"]
                self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (1, "Recreate"))
                pod = spec["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], plan["state_claim"])
                container = pod["containers"][0]
                self.assertEqual(container["image"], renderer.IMAGE)
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                secret = next(env for env in container["env"] if env["name"] == renderer.PASSWORD_ENV)
                self.assertEqual(secret["valueFrom"]["secretKeyRef"], {"name": "private-bootstrap", "key": "initial-password"})

    def test_host_packages_listen_locally_and_leave_wizard_available(self):
        docker = yaml.safe_load(renderer.render(example("docker"), "docker")["compose.yaml"])["services"]["scm-manager"]
        self.assertEqual(docker["ports"], ["127.0.0.1:8080:8080"])
        self.assertEqual(docker["user"], "1000:1000")
        self.assertIn(f"-Dorg.sqlite.tmpdir={renderer.WORK}", docker["environment"]["JAVA_OPTS"])
        self.assertTrue(any(mount.startswith(f"{renderer.WORK}:rw,exec,") for mount in docker["tmpfs"]))
        self.assertNotIn("env_file", docker)
        files = renderer.render(example("native"), "raw")
        self.assertIn("SCM_ADDRESS_BINDING=127.0.0.1", files["scm-manager.service"])
        self.assertIn('Environment="JAVA_OPTS=-Duser.home=/var/lib/scm-manager '
                      '-Dorg.sqlite.tmpdir=/var/lib/scm-manager/work"', files["scm-manager.service"])
        self.assertIn("ExecStart=/opt/scm-manager/scm-server/bin/scm-server", files["scm-manager.service"])
        self.assertNotIn("EnvironmentFile", files["scm-manager.service"])
        self.assertIn(renderer.ARCHIVE_SHA256, files["scm-manager.sha256"])

    def test_rejects_invalid_reference_and_inline_secret(self):
        with self.assertRaises(ValueError):
            renderer.render({**example("kubernetes"), "namespace": "default"}, "rke2")
        with self.assertRaises(ValueError):
            renderer.render({**example("docker"), "bootstrap_env_file": "/srv/state/private.env"}, "docker")
        with self.assertRaises(ValueError):
            renderer.render({**example("native"), "password": "example"}, "raw")


if __name__ == "__main__":
    unittest.main()
