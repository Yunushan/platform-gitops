#!/usr/bin/env python3
"""Validate GitBucket rendering contracts for every requested runtime."""

import json
import unittest

import yaml

import render_gitbucket as renderer


def example(runtime):
    name = {"raw": "native", "docker": "docker"}.get(runtime, "kubernetes")
    example_path = renderer.ROOT / "examples" / f"gitbucket-{name}.json"
    return json.loads(example_path.read_text(encoding="utf-8").replace("<DERIVED_IMAGE_DIGEST>", "a" * 64))


class GitBucketRenderingTests(unittest.TestCase):
    def test_kubernetes_uses_one_writer_and_private_bootstrap(self):
        for runtime in ("rke2", "k3s", "k8s"):
            with self.subTest(runtime=runtime):
                files = renderer.render(example(runtime), runtime)
                policy, service, deploy = yaml.safe_load_all(files["gitbucket.yaml"])
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["type"], "ClusterIP")
                self.assertEqual(deploy["spec"]["replicas"], 1)
                self.assertEqual(deploy["spec"]["strategy"]["type"], "Recreate")
                pod = deploy["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "gitbucket-state")
                self.assertIn("allow_anonymous_access=false", files["gitbucket.conf"])
                self.assertIn("ssh.publicAddress.host=gitbucket-ssh.example.com", files["gitbucket.conf"])

    def test_host_backends_bind_locally_and_preserve_existing_state(self):
        compose = yaml.safe_load(renderer.render(example("docker"), "docker")["compose.yaml"])["services"]["gitbucket"]
        self.assertTrue(all(port.startswith("127.0.0.1:") for port in compose["ports"]))
        self.assertFalse(compose["volumes"][0]["bind"]["create_host_path"])
        self.assertEqual(compose["user"], "1000:1000")
        self.assertIn(renderer.CONTAINER_WAR, compose["entrypoint"])
        self.assertIn("install -m 0444", renderer.containerfile())
        native = renderer.render(example("raw"), "raw")
        self.assertIn("--host=127.0.0.1", native["gitbucket.service"])
        self.assertIn("ssh.bindAddress.host=127.0.0.1", native["gitbucket.conf"])
        self.assertIn(renderer.WAR_SHA256, native["gitbucket.war.sha256"])

    def test_rejects_unsafe_or_cross_runtime_inputs(self):
        for runtime, changes in (
            ("docker", {"image": "ghcr.io/gitbucket/gitbucket:latest"}),
            ("docker", {"image": renderer.UPSTREAM_IMAGE}),
            ("docker", {"ssh_port": 8080}),
            ("raw", {"war": "/srv/gitbucket/state/modified.war"}),
            ("raw", {"user": "root"}),
            ("k3s", {"namespace": "kube-system"}),
            ("rke2", {"https_url": "https://gitbucket.example.com\nssh=false"}),
            ("k8s", {"state_path": "/srv/gitbucket/state"}),
        ):
            with self.subTest(runtime=runtime, fields=tuple(changes)), self.assertRaises(ValueError):
                renderer.render(dict(example(runtime), **changes), runtime)


if __name__ == "__main__":
    unittest.main()
