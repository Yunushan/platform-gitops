import json
from pathlib import Path
import unittest

import yaml

import render_onedev as renderer


def example(runtime):
    path = Path(__file__).resolve().parents[1] / "examples" / f"onedev-{runtime}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class OneDevRenderTest(unittest.TestCase):
    def test_kubernetes_single_writer_and_postgres_tls(self):
        plan = {**example("kubernetes"), "bootstrap": {"user": "admin", "email": "admin@example.com"}}
        for runtime in renderer.CLUSTER:
            with self.subTest(runtime=runtime):
                policy, service, deployment = yaml.safe_load_all(renderer.render(plan, runtime)["onedev.yaml"])
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["type"], "ClusterIP")
                self.assertEqual({port["name"]: port["port"] for port in service["spec"]["ports"]}, renderer.PORTS)
                spec = deployment["spec"]
                self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (1, "Recreate"))
                pod = spec["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], plan["state_claim"])
                secret = next(volume["secret"] for volume in pod["volumes"] if "secret" in volume)
                self.assertEqual(secret["secretName"], plan["credentials_secret"])
                self.assertEqual({item["key"] for item in secret["items"]}, {"ca.crt", "database-password", "initial-password"})
                container = pod["containers"][0]
                self.assertEqual(container["image"], renderer.IMAGE)
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertEqual(container["startupProbe"]["httpGet"], {"path": "/healthz", "port": "http"})
                self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/readyz", "port": "http"})
                env = {item["name"]: item["value"] for item in container["env"]}
                self.assertIn("sslmode=verify-full&sslrootcert=", env["hibernate_connection_url"])
                self.assertNotIn("hibernate_connection_password", env)
                self.assertEqual(env["initial_ssh_root_url"], "ssh://onedev.example.com:2222")

    def test_host_bundles_and_interactive_setup(self):
        files = renderer.render(example("docker"), "docker")
        service = yaml.safe_load(files["compose.yaml"])["services"]["onedev"]
        self.assertEqual(service["user"], "1000:1000")
        self.assertEqual(service["ports"], ["127.0.0.1:6610:6610", "127.0.0.1:6611:6611"])
        self.assertNotIn("docker.sock", files["compose.yaml"])
        self.assertIn('"$$source/bin/upgrade.sh" /opt/onedev', service["entrypoint"][2])
        self.assertNotIn("initial_password", service["environment"])
        unit = renderer.render(example("native"), "raw")["onedev.service"]
        self.assertIn('Environment="http_host=127.0.0.1"', unit)
        self.assertIn("ExecStart=/var/lib/onedev/bin/server.sh console", unit)
        self.assertIn("ReadWritePaths=/var/lib/onedev", unit)

    def test_rejects_untrusted_inputs_and_missing_credential_reference(self):
        plan = example("kubernetes")
        with self.assertRaises(ValueError):
            renderer.render({**plan, "credentials_secret": None}, "k8s")
        with self.assertRaises(ValueError):
            renderer.render({**plan, "https_url": "https://user@onedev.example.com"}, "k8s")
        with self.assertRaises(ValueError):
            renderer.render({**plan, "database": {**plan["database"], "password": "example"}}, "k8s")
        with self.assertRaises(ValueError):
            renderer.render({**example("native"), "user": "root"}, "raw")


if __name__ == "__main__":
    unittest.main()
