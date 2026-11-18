import json
from pathlib import Path
import unittest

import yaml

import render_allura as renderer


def example(runtime):
    path = Path(__file__).resolve().parents[1] / "examples" / f"allura-{runtime}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class AlluraRenderTest(unittest.TestCase):
    def test_cluster_colocates_web_and_worker_on_single_state_claim(self):
        plan = example("kubernetes")
        for runtime in renderer.CLUSTER:
            with self.subTest(runtime=runtime):
                policy, service, deployment = yaml.safe_load_all(renderer.render(plan, runtime)["allura.yaml"])
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")
                spec = deployment["spec"]
                self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (1, "Recreate"))
                pod = spec["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                volumes = {volume["name"]: volume for volume in pod["volumes"]}
                self.assertEqual(volumes["state"]["persistentVolumeClaim"]["claimName"], plan["state_claim"])
                self.assertEqual(volumes["config"]["secret"]["secretName"], plan["config_secret"])
                web, task = pod["containers"]
                self.assertEqual(web["args"][-1], f"allura_git_http:make_app('{renderer.CONFIG}')")
                self.assertIn("--forwarded-allow-ips=", web["args"])
                self.assertEqual(task["args"], ["--plugin=Allura", "taskd", renderer.CONFIG])
                self.assertEqual(web["readinessProbe"]["httpGet"]["path"], renderer.HEALTH_PATH)
                for container in (web, task):
                    self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
                    self.assertTrue(mounts["config"]["readOnly"])
                    self.assertEqual(mounts["state"]["mountPath"], renderer.STATE)

    def test_host_bundle_keeps_worker_private_and_web_on_loopback(self):
        services = yaml.safe_load(renderer.render(example("docker"), "docker")["compose.yaml"])["services"]
        self.assertEqual(services["web"]["ports"], ["127.0.0.1:8080:8080"])
        self.assertNotIn("ports", services["task"])
        self.assertEqual(services["web"]["volumes"], services["task"]["volumes"])
        units = renderer.render(example("native"), "raw")
        self.assertIn("--bind 127.0.0.1:8080", units["allura-web.service"])
        self.assertIn('paster --plugin=Allura taskd "/etc/allura/allura.ini"', units["allura-task.service"])

    def test_rejects_inline_private_configuration_and_mutable_images(self):
        plan = example("kubernetes")
        for invalid in (
            {**plan, "mongo_uri": "mongodb://example.com/database"},
            {**plan, "image": "registry.example.com/platform/allura:latest"},
            {**plan, "namespace": "default"},
            {**plan, "trusted_proxy_cidrs": ["*"]},
            {**plan, "trusted_proxy_cidrs": ["0.0.0.0/0"]},
        ):
            with self.subTest(fields=sorted(invalid)), self.assertRaises(ValueError):
                renderer.render(invalid, "k8s")

    def test_only_configured_proxies_are_trusted(self):
        plan = {**example("docker"), "trusted_proxy_cidrs": ["192.0.2.32/30", "::1"]}
        web = yaml.safe_load(renderer.render(plan, "docker")["compose.yaml"])["services"]["web"]
        self.assertIn("--forwarded-allow-ips=192.0.2.32/30,::1", web["command"])


if __name__ == "__main__":
    unittest.main()
