#!/usr/bin/env python3
import json
import unittest

import yaml

import render_sourcehut as renderer


def plan(kind):
    return json.loads((renderer.ROOT / "examples" / f"sourcehut-{kind}.json").read_text(encoding="utf-8"))


class SourceHutRenderTest(unittest.TestCase):
    def test_compose_restricts_public_ports_and_repository_mounts(self):
        bundle = renderer.render(plan("docker"), "docker")
        services = yaml.safe_load(bundle["compose.yaml"])["services"]
        self.assertEqual(set(services), set(renderer.ROLES))
        self.assertEqual(services["proxy"]["ports"], ["127.0.0.1:8080:8080"])
        for role, service in services.items():
            self.assertTrue(service["read_only"])
            self.assertNotEqual(service["user"], "0:0")
            if role != "proxy":
                self.assertNotIn("ports", service)
                self.assertEqual(service["network_mode"], "service:proxy")
        self.assertTrue(services["git-http"]["volumes"][-1]["read_only"])
        self.assertNotIn("/etc/sr.ht", [mount["target"] for mount in services["proxy"]["volumes"]])

    def test_kubernetes_exposes_only_proxy_and_admits_nonroot(self):
        for runtime in sorted(renderer.CLUSTER):
            with self.subTest(runtime=runtime):
                proxy, network, service, deployment = list(yaml.safe_load_all(
                    renderer.render(plan("kubernetes"), runtime)["sourcehut.yaml"]))
                self.assertEqual(proxy["kind"], "ConfigMap")
                self.assertIn("/usr/bin/git.sr.ht-http-clone", proxy["data"]["nginx.conf"])
                self.assertEqual(network["spec"]["ingress"], [])
                self.assertEqual(service["spec"]["ports"], [{"name": "http", "port": 8080, "targetPort": "http"}])
                self.assertEqual(deployment["spec"]["replicas"], 1)
                self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                self.assertTrue(all(c["securityContext"]["runAsUser"] >= 100 for c in pod["containers"]))

    def test_native_uses_loopback_and_blocks_internal_api_at_the_proxy(self):
        bundle = renderer.render(plan("native"), "raw")
        self.assertEqual(bundle["meta.sr.ht-api.conf"], "META_BIND=127.0.0.1:5100\nMETA_ARGS='-m 127.0.0.1:0'\n")
        self.assertEqual(bundle["git.sr.ht-api.conf"], "GIT_BIND=127.0.0.1:5101\nGIT_ARGS='-m 127.0.0.1:0'\n")
        self.assertIn("command_user=git:git", bundle["sourcehut-fcgiwrap"])
        nginx = bundle["nginx.conf"]
        self.assertIn("listen 127.0.0.1:8080 default_server;", nginx)
        self.assertEqual(nginx.count('"^internal([[:space:]]|$)") { return 403; }'), 2)
        self.assertIn("if ($arg_service = git-receive-pack) { return 405; }", nginx)

    def test_opt_in_ssh_uses_dedicated_listener_and_supplied_host_key(self):
        path = "/etc/sourcehut-ssh/ssh_host_ed25519_key"
        docker = renderer.render({**plan("docker"), "ssh_port": 2200, "ssh_host_key_file": path}, "docker")
        services = yaml.safe_load(docker["compose.yaml"])["services"]
        ssh = services["git-ssh"]
        self.assertIn("127.0.0.1:2200:2222", services["proxy"]["ports"])
        self.assertEqual(ssh["network_mode"], "service:proxy")
        self.assertEqual(ssh["cap_drop"], ["ALL"])
        self.assertEqual(set(ssh["cap_add"]), {"SETUID", "SETGID", "SYS_CHROOT"})
        self.assertEqual([(m["target"], m["read_only"]) for m in ssh["volumes"] if m["source"] == path],
                         [(renderer.SSH_KEY, True)])
        for directive in ("AuthenticationMethods publickey", "PermitRootLogin no", "AuthorizedKeysFile none",
                          "AuthorizedKeysCommand /usr/bin/sourcehut-ssh %u /var/empty %t %k",
                          "AuthorizedKeysCommandUser git", "DisableForwarding yes", "PermitTTY no"):
            self.assertIn(directive + "\n", docker["sshd_config"])
        native = renderer.render({**plan("native"), "ssh_port": 2200}, "raw")
        self.assertIn("ListenAddress 127.0.0.1\n", native["sshd_config"])
        self.assertIn("Port 2200\n", native["sshd_config"])
        self.assertIn(renderer.SSH_CONFIG, native["sourcehut-sshd"])

    def test_cluster_ssh_keeps_host_key_and_capabilities_in_one_container(self):
        for runtime in sorted(renderer.CLUSTER):
            with self.subTest(runtime=runtime):
                plan_data = {**plan("kubernetes"), "ssh_host_key_secret": "dedicated-host-key"}
                config, _, service, deployment = list(yaml.safe_load_all(
                    renderer.render(plan_data, runtime)["sourcehut.yaml"]))
                self.assertIn("HostKey /tmp/ssh-host/ed25519\n", config["data"]["sshd_config"])
                self.assertIn({"name": "ssh", "port": 2222, "targetPort": "ssh"}, service["spec"]["ports"])
                pod = deployment["spec"]["template"]["spec"]
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
                self.assertEqual([c["name"] for c in pod["containers"] if any(
                    m["name"] == "host-key" for m in c["volumeMounts"])], ["git-ssh"])
                ssh = pod["containers"][-1]["securityContext"]
                self.assertFalse(ssh["allowPrivilegeEscalation"])
                self.assertEqual(ssh["capabilities"]["drop"], ["ALL"])
                self.assertEqual(set(ssh["capabilities"]["add"]), {"SETUID", "SETGID", "SYS_CHROOT"})

    def test_rejects_unpinned_images_invalid_listeners_and_hostname_injection(self):
        docker = plan("docker")
        for change in ({"image": "sourcehut:latest"}, {"http_port": 5100}, {"meta_host": docker["git_host"]},
                       {"git_host": "git.example.com; return 200;"}, {"config_dir": "/etc/../tmp/config"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                renderer.render({**docker, **change}, "docker")
        with self.assertRaises(ValueError):
            renderer.render({**docker, "ssh_port": docker["http_port"], "ssh_host_key_file": "/etc/ssh/key"}, "docker")
        with self.assertRaises(ValueError):
            renderer.render({**docker, "ssh_port": 2200}, "docker")
        cluster = plan("kubernetes")
        with self.assertRaises(ValueError):
            renderer.render({**cluster, "ssh_host_key_secret": cluster["config_secret"]}, "k8s")


if __name__ == "__main__":
    unittest.main()
