#!/usr/bin/env python3
import json
import unittest

import yaml

import render_rhodecode as renderer


def plan(kind):
    return json.loads((renderer.ROOT / "examples" / f"rhodecode-{kind}.json").read_text(encoding="utf-8"))


class RhodeCodeRenderTest(unittest.TestCase):
    def test_cluster_shares_storage_and_exposes_only_web(self):
        for runtime in sorted(renderer.CLUSTER):
            with self.subTest(runtime=runtime):
                policy, service, deployment = list(yaml.safe_load_all(renderer.render(plan("kubernetes"), runtime)["rhodecode.yaml"]))
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["ports"], [{"name": "http", "port": 10020, "targetPort": "http"}])
                self.assertEqual(deployment["spec"]["replicas"], 1)
                self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["securityContext"]["runAsUser"], 999)
                web, vcs, worker = pod["containers"]
                self.assertIn("--bind=0.0.0.0:10020", web["args"])
                self.assertIn("--bind=127.0.0.1:10010", vcs["args"])
                self.assertNotIn("ports", vcs)
                self.assertNotIn("ports", worker)
                self.assertIn("worker", worker["args"])
                self.assertEqual(web["volumeMounts"], vcs["volumeMounts"])
                self.assertEqual(web["volumeMounts"], worker["volumeMounts"])
                self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"], {"claimName": "rhodecode-state"})

    def test_docker_shares_network_namespace_and_keeps_state_external(self):
        services = yaml.safe_load(renderer.render(plan("docker"), "docker")["compose.yaml"])["services"]
        self.assertEqual(services["web"]["ports"], ["127.0.0.1:10020:10020"])
        self.assertEqual(services["vcs"]["network_mode"], "service:web")
        self.assertEqual(services["worker"]["network_mode"], "service:web")
        self.assertIn("--bind=127.0.0.1:10010", services["vcs"]["command"])
        self.assertNotIn("ports", services["vcs"])
        for service in services.values():
            self.assertTrue(service["read_only"])
            self.assertTrue(all(not mount["bind"]["create_host_path"] for mount in service["volumes"]))

    def test_native_services_use_prepared_artifacts_and_loopback(self):
        output = renderer.render(plan("native"), "raw")
        web, vcs = output["rhodecode-web.service"], output["rhodecode-vcs.service"]
        self.assertIn("Requires=rhodecode-vcs.service rhodecode-worker.service\n", web)
        self.assertIn("ExecStart=/opt/rhodecode/bin/celery ", output["rhodecode-worker.service"])
        self.assertIn("ExecStart=/opt/rhodecode/bin/gunicorn ", web)
        self.assertIn("ExecStart=/opt/rhodecode/vcs_bin/gunicorn ", vcs)
        self.assertIn("--bind=127.0.0.1:10020", web)
        self.assertIn("--bind=127.0.0.1:10010", vcs)
        self.assertIn("User=rhodecode\n", web)

    def test_unsafe_inputs_are_rejected(self):
        docker = plan("docker")
        for bad in ({"image": "rhodecode:latest"}, {"http_port": 10010}, {"trusted_proxy_ips": ["*"]},
                    {"trusted_proxy_ips": ["192.0.2.0/24"]}, {"config_dir": "/etc/x\nExecStart=evil"},
                    {"password": "unexpected"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                renderer.render({**docker, **bad}, "docker")


if __name__ == "__main__":
    unittest.main()
