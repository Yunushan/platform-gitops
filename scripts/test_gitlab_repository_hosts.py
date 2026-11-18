import unittest

import yaml

import render_gitlab_repository_hosts as renderer


def plan():
    return {
        "schema_version": 1,
        "fresh_install": True,
        "gitaly": [{"host": f"gitaly-{i}.example.org", "bind_ipv4": f"192.0.2.{i + 10}"} for i in range(1, 4)],
        "praefect": [{"host": f"praefect-{i}.example.org", "bind_ipv4": f"192.0.2.{i + 20}"} for i in range(1, 4)],
        "praefect_endpoint": "repository.example.org",
        "gitlab_internal_url": "https://git.example.org:9443",
        "postgres": {"host": "db.example.org", "port": 5432, "database": "praefect", "user": "praefect"},
    }


def external_backends():
    return dict(plan(), schema_version=2, application_backends={
        "postgres": {"host": "db.example.org", "port": 5432, "database": "gitlabhq_production", "user": "gitlab"},
        "valkey": {"host": "cache.example.org", "port": 6380},
    })


def replicated_application():
    return dict(external_backends(), schema_version=3, application_hosts={
        "nodes": [{"host": f"rails-{i}.example.org", "bind_ipv4": f"192.0.2.{30 + i}"} for i in (1, 2)],
        "entry_ipv4": "192.0.2.40",
        "proxy_ipv4": ["192.0.2.41", "192.0.2.42"],
        "ssh_port": 2222,
        "object_storage": {
            "endpoint": "https://objects.example.org",
            "region": "us-east-1",
            "buckets": {kind: f"gitlab-{kind.replace('_', '-')}" for kind in (
                "artifacts", "external_diffs", "lfs", "uploads", "packages", "dependency_proxy",
                "terraform_state", "ci_secure_files", "ci_catalog_bundles", "pages",
            )},
        },
    })


class RepositoryHostsTest(unittest.TestCase):
    def test_replicated_application_renders_private_listeners_and_shared_backends(self):
        sample = replicated_application()
        for runtime in ("raw", "docker"):
            with self.subTest(runtime=runtime):
                files = renderer.render(sample, runtime)
                common = files["gitlab-application.rb"]
                for index, node in enumerate(sample["application_hosts"]["nodes"], 1):
                    role = f"rails-{index}"
                    config = files[f"{role}.gitlab.rb"]
                    self.assertTrue(config.startswith(common))
                    self.assertIn("external_url 'https://git.example.org:9443'", config)
                    self.assertIn("File.size?('/etc/gitlab/gitlab-secrets.json')", config)
                    self.assertIn("gitlab_sshd['generate_host_keys'] = false", config)
                    self.assertIn("gitlab_sshd['proxy_policy'] = 'require'", config)
                    self.assertIn("nginx['real_ip_trusted_addresses'] = [\"192.0.2.41/32\", \"192.0.2.42/32\"]", config)
                    for kind, bucket in sample["application_hosts"]["object_storage"]["buckets"].items():
                        self.assertIn(f"['{kind}']['bucket'] = '{bucket}'", config)
                    self.assertIn("File.read('/etc/gitlab/repository-cluster/rails-s3-secret-key')", config)
                    compose_file = f"{role}.compose.yaml"
                    if runtime == "raw":
                        self.assertNotIn(compose_file, files)
                        self.assertIn(f"nginx['listen_addresses'] = ['{node['bind_ipv4']}']", config)
                    else:
                        service = yaml.safe_load(files[compose_file])["services"][role]
                        self.assertEqual(service["ports"], [
                            f"{node['bind_ipv4']}:8443:8443/tcp", f"{node['bind_ipv4']}:2222:2222/tcp",
                        ])
                proxy = files["gitlab-application-haproxy.cfg"]
                self.assertIn("bind 192.0.2.40:9443 ssl", proxy)
                self.assertIn("bind 192.0.2.40:2222", proxy)
                self.assertIn("http-check send meth GET uri /-/readiness", proxy)
                self.assertIn("verify required ca-file", proxy)
                self.assertIn("verifyhost git.example.org", proxy)
                self.assertIn("http-request set-header X-Forwarded-For %[src]", proxy)
                self.assertIn("default-server send-proxy-v2 check", proxy)

    def test_replicated_application_rejects_ambiguous_endpoints_and_buckets(self):
        mutations = (
            lambda app: app["nodes"].pop(),
            lambda app: app.update(entry_ipv4=app["nodes"][0]["bind_ipv4"]),
            lambda app: app.update(proxy_ipv4=["192.0.2.41", "192.0.2.41"]),
            lambda app: app.update(ssh_port=9443),
            lambda app: app["object_storage"].update(endpoint="https://objects.example.org/embedded-path"),
            lambda app: app["object_storage"]["buckets"].update(artifacts="gitlab-lfs"),
            lambda app: app["object_storage"].update(password="inline"),
        )
        for mutate in mutations:
            sample = replicated_application()
            mutate(sample["application_hosts"])
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                renderer.render(sample, "docker")

    def test_external_backends_are_explicit_and_application_only(self):
        for runtime in ("raw", "docker"):
            base = renderer.render(plan(), runtime)
            candidate = renderer.render(external_backends(), runtime)
            config = candidate.pop("gitlab-application.rb")
            self.assertTrue(config.startswith(base.pop("gitlab-application.rb")))
            self.assertEqual(candidate, base)
            for option in (
                "postgresql['enable'] = false",
                "gitlab_rails['auto_migrate'] = false",
                "gitlab_rails['db_database'] = 'gitlabhq_production'",
                "gitlab_rails['db_sslmode'] = 'verify-full'",
                "gitlab_rails['db_sslrootcert'] = '/etc/gitlab/trusted-certs/rails-postgres-ca.crt'",
                "redis['enable'] = false",
                "gitlab_rails['redis_port'] = 6380",
                "gitlab_rails['redis_ssl'] = true",
                "gitlab_rails['redis_tls_ca_cert_file'] = '/etc/gitlab/trusted-certs/rails-valkey-ca.crt'",
                "File.read('/etc/gitlab/repository-cluster/rails-postgres-password')",
                "File.read('/etc/gitlab/repository-cluster/rails-valkey-password')",
            ):
                self.assertIn(option, config)
            self.assertNotIn("redis_tls_client_key_file", config)

        mutual_tls = external_backends()
        mutual_tls["application_backends"]["valkey"]["mutual_tls"] = True
        config = renderer.render(mutual_tls, "docker")["gitlab-application.rb"]
        self.assertIn("redis_tls_client_cert_file'] = '/etc/gitlab/ssl/rails-valkey-client.crt'", config)
        self.assertIn("redis_tls_client_key_file'] = '/etc/gitlab/ssl/rails-valkey-client.key'", config)

    def test_rejects_shared_application_role_and_inline_cache_password(self):
        cases = [external_backends(), external_backends(), external_backends(), external_backends()]
        cases[0]["application_backends"]["postgres"]["user"] = "praefect"
        cases[1]["application_backends"]["postgres"]["database"] = "praefect"
        cases[2]["application_backends"]["valkey"]["password"] = "not-a-file"
        cases[3]["application_backends"]["valkey"]["port"] = True
        for candidate in cases:
            with self.assertRaises(ValueError):
                renderer.render(candidate, "docker")

    def test_raw_and_docker_role_configs(self):
        for runtime in ("raw", "docker"):
            with self.subTest(runtime=runtime):
                rendered = renderer.render(plan(), runtime)
                for role, port in (("gitaly", 9999), ("praefect", 3305)):
                    for i, node in enumerate(plan()[role], 1):
                        name = f"{role}-{i}"
                        config = rendered[name + ".gitlab.rb"]
                        bind = "0.0.0.0" if runtime == "docker" else node["bind_ipv4"]
                        self.assertIn(f"tls_listen_addr: '{bind}:{port}'", config)
                        self.assertIn("listen_addr: nil", config)
                        self.assertIn(f"{role}['enable'] = true", config)
                        if runtime == "docker":
                            service = yaml.safe_load(rendered[name + ".compose.yaml"])["services"][name]
                            self.assertEqual(service["ports"], [f"{node['bind_ipv4']}:{port}:{port}/tcp"])
                            self.assertTrue(all(mount["bind"]["create_host_path"] is False for mount in service["volumes"]))
                        else:
                            self.assertNotIn(name + ".compose.yaml", rendered)
                config = rendered["praefect-1.gitlab.rb"]
                self.assertIn("sslmode: 'verify-full'", config)
                self.assertIn("default_replication_factor: 3", config)
                for node in plan()["gitaly"]:
                    self.assertIn(f"tls://{node['host']}:9999", config)
                self.assertIn("tls://repository.example.org:3305", rendered["gitlab-application.rb"])

    def test_rejects_existing_or_ambiguous_topologies(self):
        invalid = [dict(plan(), fresh_install=False), dict(plan(), praefect=plan()["gitaly"]),
                   dict(plan(), praefect_endpoint=plan()["gitaly"][0]["host"]),
                   dict(plan(), gitlab_internal_url="https://git.example.org/unsafe-path"),
                   dict(plan(), gitlab_internal_url="https://git.example.org:65536")]
        shared_address = plan()
        shared_address["gitaly"][0]["bind_ipv4"] = shared_address["praefect"][0]["bind_ipv4"]
        invalid.append(shared_address)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                renderer.render(candidate, "docker")


if __name__ == "__main__":
    unittest.main()
