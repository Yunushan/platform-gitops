import copy
import os
from pathlib import Path
import unittest

import yaml
import render_gitlab_traefik_entry as renderer


def plan():
    return {"schema_version": 1, "ssh_host": "git.example.test", "public_port": 2222,
            "listener_port": 2223, "entrypoint": "gitlab-shell", "ingress_class": "traefik",
            "gitlab_namespace": "gitlab", "gitlab_release": "gitlab", "traefik_namespace": "traefik",
            "traefik_instance": "traefik-traefik"}


class GitLabTraefikEntryTest(unittest.TestCase):
    def test_paired_overlays_use_same_entry_and_public_port(self):
        result = renderer.render(plan())
        gitlab = yaml.safe_load(result["gitlab-ssh-values.yaml"])
        traefik = yaml.safe_load(result["traefik-ssh-values.yaml"])
        self.assertEqual(gitlab["global"]["shell"]["port"], traefik["ports"]["gitlab-shell"]["exposedPort"])
        self.assertEqual(gitlab["gitlab"]["gitlab-shell"]["traefik"]["entrypoint"], "gitlab-shell")
        self.assertEqual(gitlab["gitlab"]["gitlab-shell"]["traefik"]["apiVersion"], "traefik.io/v1alpha1")
        policy = yaml.safe_load(result["gitlab-ssh-network-policy.yaml"])
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], {"app": "gitlab-shell", "release": "gitlab"})
        self.assertEqual(policy["spec"]["ingress"][0]["ports"], [{"protocol": "TCP", "port": 2222}])
        self.assertEqual(policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"], "traefik-traefik")

    def test_invalid_or_conflicting_listeners_are_rejected(self):
        for key, value in (("entrypoint", "web"), ("entrypoint", "123"), ("entrypoint", "git--ssh"), ("entrypoint", "long-entrypoint-name"),
                           ("public_port", 443), ("listener_port", 9000), ("public_port", True),
                           ("ssh_host", "https://example.test"), ("traefik_namespace", "gitlab")):
            candidate = copy.deepcopy(plan())
            candidate[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                renderer.render(candidate)

    @unittest.skipUnless(os.environ.get("GITLAB_SSH_RENDER") and os.environ.get("TRAEFIK_SSH_RENDER"), "chart renders not supplied")
    def test_vendored_chart_outputs_route_to_exposed_entrypoint(self):
        def docs(env):
            return [d for d in yaml.safe_load_all(Path(os.environ[env]).read_text(encoding="utf-8")) if isinstance(d, dict)]
        gitlab, traefik = docs("GITLAB_SSH_RENDER"), docs("TRAEFIK_SSH_RENDER")
        policy = yaml.safe_load(renderer.render(plan())["gitlab-ssh-network-policy.yaml"])
        shell = next(d for d in gitlab if d.get("kind") == "Deployment" and d["metadata"]["name"] == "gitlab-gitlab-shell")
        shell_labels = shell["spec"]["template"]["metadata"]["labels"]
        self.assertTrue(policy["spec"]["podSelector"]["matchLabels"].items() <= shell_labels.items())
        controller = next(d for d in traefik if d.get("kind") == "Deployment" and d["metadata"]["name"] == "traefik")
        ingress_labels = controller["spec"]["template"]["metadata"]["labels"]
        selector = policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
        self.assertTrue(selector.items() <= ingress_labels.items())
        service = next(d for d in gitlab if d.get("kind") == "Service" and d["metadata"]["name"] == "gitlab-gitlab-shell")
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], 2222)
        route = next(d for d in gitlab if d.get("kind") == "IngressRouteTCP" and d["metadata"]["name"] == "gitlab-gitlab-shell")
        self.assertEqual(route["apiVersion"], "traefik.io/v1alpha1")
        self.assertEqual(route["spec"]["entryPoints"], ["gitlab-shell"])
        self.assertEqual(route["spec"]["ingressClassName"], "traefik")
        self.assertEqual(route["spec"]["routes"][0]["services"][0]["port"], 2222)
        services = [d for d in traefik if d.get("kind") == "Service"]
        self.assertTrue(any(any(port.get("name") == "gitlab-shell" and port.get("port") == 2222
                                    for port in service["spec"].get("ports", [])) for service in services))
        self.assertTrue(any("--entryPoints.gitlab-shell.address=:2223/tcp" in arg
                            for d in traefik if d.get("kind") in {"Deployment", "DaemonSet"}
                            for c in d["spec"]["template"]["spec"]["containers"] for arg in c.get("args", [])))


if __name__ == "__main__":
    unittest.main()
