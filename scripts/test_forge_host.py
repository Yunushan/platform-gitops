"""Validate the opt-in Forgejo and Gitea host renderers."""

import json
from pathlib import Path
import tempfile
import unittest

import yaml

import render_forge_host as host
from test_postgres_ha import fixture as postgres_plan, write_endpoint_fixture, write_package


def example(runtime, provider="gitea"):
    name = f"{provider}-host-docker" if runtime == "docker" else "gitea-host-native"
    data = json.loads((host.ROOT / "examples" / (name + ".json")).read_text(encoding="utf-8"))
    if runtime == "raw":
        data["binary_sha256"] = "4" * 64
        data["provider"] = provider
    return data


class HostRendererTests(unittest.TestCase):
    def test_reviewed_patroni_binding_checks_endpoint_identity_and_trust_anchor(self):
        database_plan = postgres_plan()
        database_plan["write_endpoint"] = write_endpoint_fixture()
        database_plan["applications"] = [{"database": "forgejo", "owner": "forgejo_owner", "secret": "forgejo-owner"}]
        endpoint = database_plan["write_endpoint"]
        with tempfile.TemporaryDirectory() as temporary:
            bundle, digest = write_package(Path(temporary), database_plan, "raw")
            for runtime in host.RUNTIMES:
                with self.subTest(runtime=runtime):
                    app = example(runtime, "forgejo")
                    app["database"] = {"type": "postgres", "host": endpoint["host"], "port": endpoint["port"],
                                       "name": "forgejo", "user": "forgejo_owner"}
                    files = host.render(app, postgres_bundle=bundle, postgres_contract_sha256=digest)
                    self.assertIn(f"HOST = {endpoint['host']}:{endpoint['port']}", files["app.ini"])
                    ca = database_plan["tls"]["ca_sha256"]["postgres"]
                    if runtime == "docker":
                        service = yaml.safe_load(files["compose.yaml"])["services"][app["name"]]
                        self.assertIn(f"{ca} /run/platform/forge/postgres-ca.crt | sha256sum -c -", service["entrypoint"][2])
                    else:
                        self.assertEqual(files["postgres-ca.sha256"], f"{ca}  {app['inputs_path']}/postgres-ca.crt\n")
                        self.assertIn(f"--check --status {app['inputs_path']}/postgres-ca.sha256", files[app["name"] + ".service"])
                    with self.assertRaisesRegex(ValueError, "address differs"):
                        host.render(dict(app, database=dict(app["database"], port=6433)),
                                    postgres_bundle=bundle, postgres_contract_sha256=digest)
                    with self.assertRaisesRegex(ValueError, "identity is absent"):
                        host.render(dict(app, database=dict(app["database"], user="another_owner")),
                                    postgres_bundle=bundle, postgres_contract_sha256=digest)
                    with self.assertRaisesRegex(ValueError, "identity changed"):
                        host.render(app, postgres_bundle=bundle, postgres_contract_sha256="f" * 64)
                    with self.assertRaisesRegex(ValueError, "both"):
                        host.render(app, postgres_bundle=bundle)

    def test_single_active_files_and_provider_webhook_settings(self):
        for provider in host.PROVIDERS:
            for runtime in host.RUNTIMES:
                with self.subTest(provider=provider, runtime=runtime):
                    plan = dict(example(runtime, provider), custom_https_ca=True)
                    files = host.render(plan)
                    config = files["app.ini"]
                    self.assertIn("LOCAL_ROOT_URL = http://127.0.0.1:", config)
                    state, _ = host.runtime_paths(plan)
                    self.assertIn(f"SSH_SERVER_HOST_KEYS = {state}/data/ssh/gitea.ed25519", config)
                    self.assertIn("SSL_MODE = verify-full", config)
                    self.assertIn("SECRET_KEY_URI = file:", config)
                    self.assertIn("INTERNAL_TOKEN_URI = file:", config)
                    self.assertNotIn("PASSWD =", config)
                    self.assertIn("ALLOWED_HOST_LIST = ci.example.invalid", config)
                    if provider == "gitea":
                        self.assertIn("EGRESS_MODE = strict", config)
                        self.assertIn("PUBLIC_URL_DETECTION = never", config)
                    if runtime == "docker":
                        service = yaml.safe_load(files["compose.yaml"])["services"][plan["name"]]
                        self.assertEqual(service["image"], plan["image"])
                        self.assertEqual(service["user"], "1000:1000")
                        self.assertTrue(service["read_only"] and service["init"])
                        self.assertTrue(all(value.startswith("127.0.0.1:") for value in service["ports"]))
                        self.assertEqual(service["environment"]["PGPASSFILE"], "/run/platform/forge/pgpass")
                        self.assertEqual(service["environment"]["TMPDIR"], "/tmp")
                        self.assertEqual(service["environment"]["SSL_CERT_FILE"], "/run/platform/forge/https-ca-bundle.crt")
                        self.assertEqual(service["entrypoint"][-1], "web")
                        self.assertTrue(all(mount["target"] == host.CONTAINER_STATE or mount.get("read_only")
                                            for mount in service["volumes"]))
                    else:
                        unit = files[plan["name"] + ".service"]
                        self.assertIn("ProtectSystem=strict", unit)
                        self.assertIn("ReadWritePaths=" + plan["state_path"], unit)
                        self.assertIn("ExecStartPre=/usr/bin/sha256sum --check --status", unit)
                        self.assertIn("Environment=PGPASSFILE=" + plan["inputs_path"] + "/pgpass", unit)
                        self.assertIn("Environment=SSL_CERT_FILE=" + plan["inputs_path"], unit)

    def test_sqlite_does_not_require_postgres_credentials(self):
        prepared = dict(example("raw"), database={"type": "sqlite3"})
        files = host.render(prepared)
        self.assertIn("DB_TYPE = sqlite3", files["app.ini"])
        self.assertNotIn("PGPASSFILE", files[prepared["name"] + ".service"])

    def test_upgrade_bundles_only_replace_reviewed_runtime_files(self):
        for runtime in host.RUNTIMES:
            with self.subTest(runtime=runtime):
                current = example(runtime, "forgejo")
                if runtime == "raw":
                    target = dict(current, binary="/opt/forge/bin/forge-verified", binary_sha256="b" * 64)
                    expected = {current["name"] + ".service", "binary.sha256"}
                else:
                    target = dict(current, image="example.invalid/forge@sha256:" + "b" * 64)
                    expected = {"compose.yaml"}
                files = host.render(target, upgrade_from=current)
                self.assertEqual(set(files), expected)
                self.assertEqual(files, {k: v for k, v in host.render(target).items() if k != "app.ini"})
                for changes in ({"inputs_path": "/var/lib/other-inputs"}, {"state_path": "/srv/other-state"},
                                {"database": {"type": "sqlite3"}}, {"https_url": "https://other.example.invalid"},
                                {"provider": "gitea"}, {"http_port": 13449}):
                    with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "preserve"):
                        host.render(dict(target, **changes), upgrade_from=current)
                with self.assertRaises(ValueError):
                    host.render(current, upgrade_from=current)
                if runtime == "raw":
                    with self.assertRaisesRegex(ValueError, "separate path"):
                        host.render(dict(target, binary=current["binary"]), upgrade_from=current)
                else:
                    mirror = "example.invalid/other@" + current["image"].split("@", 1)[-1]
                    with self.assertRaisesRegex(ValueError, "new reviewed image digest"):
                        host.render(dict(target, image=mirror), upgrade_from=current)

    def test_gitea_webhook_can_use_one_explicit_port(self):
        for runtime in host.RUNTIMES:
            with self.subTest(runtime=runtime):
                files = host.render(dict(example(runtime), webhook_hosts=["192.0.2.1:8443"]))
                self.assertIn("ALLOWED_HOST_LIST = 192.0.2.1:8443\n", files["app.ini"])
        for entry in ("ci.example.invalid:0", "ci.example.invalid:65536",
                      "ci.example.invalid:*", "ci.example.invalid:80-443", "192.0.2.0/24:8443"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                host.render(dict(example("raw"), webhook_hosts=[entry]))
        with self.assertRaises(ValueError):
            host.render(dict(example("raw", "forgejo"), webhook_hosts=["ci.example.invalid:8443"]))

    def test_invalid_parameters_are_rejected(self):
        base = example("raw")
        for mutation in ({"https_url": "http://git.example.invalid"}, {"webhook_hosts": ["*.example.invalid"]},
                         {"state_path": "/"}, {"inputs_path": base["state_path"] + "/inputs"}, {"user": "root"},
                         {"binary_sha256": "unreviewed"}, {"binary": base["state_path"] + "/bin/gitea"},
                         {"database": dict(base["database"], password="example")}, {"ssh_port": True},
                         {"runtime": "k3s"}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                host.render({**base, **mutation})
        with self.assertRaises(ValueError):
            host.render(dict(example("docker"), image="example.invalid/forge:latest"))


if __name__ == "__main__":
    unittest.main()
