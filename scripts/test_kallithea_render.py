import json
from pathlib import Path
import unittest

import yaml

import render_kallithea as renderer


def example(runtime):
    path = Path(__file__).resolve().parents[1] / "examples" / f"kallithea-{runtime}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class KallitheaRenderTest(unittest.TestCase):
    def test_cluster_uses_private_read_only_configuration_and_single_writer(self):
        plan = example("kubernetes")
        for runtime in renderer.CLUSTER:
            with self.subTest(runtime=runtime):
                policy, service, deployment = yaml.safe_load_all(renderer.render(plan, runtime)["kallithea.yaml"])
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["type"], "ClusterIP")
                spec = deployment["spec"]
                self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (1, "Recreate"))
                pod = spec["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                config = next(volume["secret"] for volume in pod["volumes"] if volume["name"] == "config")
                self.assertEqual(config["secretName"], plan["config_secret"])
                container = pod["containers"][0]
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertEqual(container["args"][-2:], [renderer.CONFIG, "0.0.0.0:5000"])
                self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/_admin/login")

    def test_host_bundles_bind_to_loopback(self):
        docker = yaml.safe_load(renderer.render(example("docker"), "docker")["compose.yaml"])["services"]["kallithea"]
        self.assertEqual(docker["ports"], ["127.0.0.1:5000:5000"])
        self.assertEqual(docker["user"], "1000:1000")
        unit = renderer.render(example("native"), "raw")["kallithea.service"]
        self.assertIn("/etc/kallithea/kallithea.ini 127.0.0.1:5000", unit)
        self.assertIn("ReadWritePaths=/var/lib/kallithea", unit)

    def test_rejects_mutable_image_and_inline_configuration(self):
        plan = example("kubernetes")
        with self.assertRaises(ValueError):
            renderer.render({**plan, "image": "registry.example.com/platform/kallithea:latest"}, "k8s")
        with self.assertRaises(ValueError):
            renderer.render({**plan, "password": "example"}, "k8s")


if __name__ == "__main__":
    unittest.main()
