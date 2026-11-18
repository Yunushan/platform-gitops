"""Check Gogs runtime contracts and credential references."""

import json
import unittest

import yaml

import render_gogs as renderer


def example(runtime):
    name = {"raw": "native", "docker": "docker"}.get(runtime, "kubernetes")
    return json.loads((renderer.ROOT / "examples" / f"gogs-{name}.json").read_text(encoding="utf-8"))


class GogsRenderingTests(unittest.TestCase):
    def test_kubernetes_uses_one_writer_and_secret_refs(self):
        for runtime in ("rke2", "k3s", "k8s"):
            with self.subTest(runtime=runtime):
                files = renderer.render(example(runtime), runtime)
                policy, service, deployment = yaml.safe_load_all(files["gogs.yaml"])
                self.assertEqual(policy["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["type"], "ClusterIP")
                self.assertEqual(deployment["spec"]["replicas"], 1)
                self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                container = pod["containers"][0]
                self.assertEqual(container["image"], renderer.IMAGE)
                self.assertEqual(container["command"][-3:], ["/app/gogs/start.sh", "/app/gogs/gogs", "web"])
                env = {item["name"]: item for item in container["env"]}
                self.assertEqual(env[renderer.SECRET_ENV]["valueFrom"]["secretKeyRef"]["key"], "secret-key")
                self.assertEqual(env[renderer.DATABASE_ENV]["valueFrom"]["secretKeyRef"]["key"], "database-password")
                self.assertEqual(env["PGSSLROOTCERT"]["value"], renderer.CA_PATH)
                self.assertIn("SSL_MODE = verify-full", files["app.ini"])
                self.assertIn("INSTALL_LOCK = true", files["app.ini"])
                self.assertIn("FORCE_PRIVATE = true", files["app.ini"])

    def test_host_listeners_and_sqlite_configuration(self):
        compose = yaml.safe_load(renderer.render(example("docker"), "docker")["compose.yaml"])["services"]["gogs"]
        self.assertTrue(all(port.startswith("127.0.0.1:") for port in compose["ports"]))
        self.assertIn("$${GOGS_SECURITY_SECRET_KEY:-}", compose["entrypoint"][2])
        self.assertIn('test -s "$$GOGS_CUSTOM/conf/app.ini"', compose["entrypoint"][2])
        self.assertEqual(compose["env_file"][0]["format"], "raw")
        plan = dict(example("raw"), database={"type": "sqlite3"})
        native = renderer.render(plan, "raw")
        self.assertIn("HTTP_ADDR = 127.0.0.1", native["app.ini"])
        self.assertIn("SSH_LISTEN_HOST = 127.0.0.1", native["app.ini"])
        self.assertIn("PATH = /srv/gogs/state/data/gogs.db", native["app.ini"])
        self.assertNotIn(renderer.DATABASE_ENV, native["gogs.service"])
        self.assertIn(renderer.BINARY_SHA256, native["gogs.sha256"])

    def test_rejects_unsafe_or_cross_runtime_fields(self):
        for runtime, changes in (
            ("docker", {"image": "gogs/gogs:latest"}),
            ("raw", {"binary": "/srv/gogs/state/gogs"}),
            ("raw", {"environment_file": "/srv/gogs/state/gogs.env"}),
            ("raw", {"user": "root"}),
            ("rke2", {"namespace": "kube-system"}),
            ("k3s", {"https_url": "https://gogs.example.com/?test=1"}),
            ("k8s", {"database": {"type": "postgres", "password": "<UNEXPECTED>"}}),
        ):
            with self.subTest(runtime=runtime, fields=tuple(changes)), self.assertRaises(ValueError):
                renderer.render(dict(example(runtime), **changes), runtime)


if __name__ == "__main__":
    unittest.main()
