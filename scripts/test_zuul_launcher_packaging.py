"""Exercise the opt-in integrated launcher packaging contract."""

import configparser
import copy
from importlib.metadata import version
import unittest
from unittest.mock import Mock

import yaml

import render_zuul_config as config
import render_zuul_services as services
from test_zuul_config import fixture

try:
    from zuul.configloader import TenantParser
except ImportError:
    TenantParser = None


def selected():
    plan = fixture()
    plan["zuul_version"] = "14.3.0"
    plan["tenants"][0]["allowed_labels"] = ["platform-linux"]
    plan["integrated_launcher"] = {"enabled": True, "nodepool_fallback": False, "provider_projects": {"platform": "platform/ci-config"}}
    return plan


class LauncherPackagingTests(unittest.TestCase):
    def test_requires_reviewed_version_and_tenant_restrictions(self):
        for mutate in (lambda p: p.update(zuul_version="14.2.0"),
                       lambda p: p["tenants"][0].pop("allowed_labels"),
                       lambda p: p.update(integrated_launcher={"enabled": "true"}),
                       lambda p: p.update(integrated_launcher={"enabled": True}),
                       lambda p: p["integrated_launcher"].update(nodepool_fallback="false"),
                       lambda p: p["integrated_launcher"].update(provider_projects={"platform": "applications/example"})):
            plan = selected()
            mutate(plan)
            with self.assertRaises(config.ZuulConfigError):
                config.validate(plan)

    def test_default_configuration_is_unchanged(self):
        plan = fixture()
        before = services.render(plan, "docker")
        disabled = copy.deepcopy(plan)
        disabled["integrated_launcher"] = {"enabled": False}
        self.assertEqual(before, services.render(disabled, "docker"))
        self.assertNotIn("zuul-launcher", yaml.safe_load(before["zuul-compose.yaml"])["services"])

    def test_opt_in_config_and_runtime_resources(self):
        plan = selected()
        parser = configparser.RawConfigParser()
        parser.read_string(config.configuration(plan))
        self.assertEqual(parser['connection "platform-static"']['driver'], 'static')
        self.assertEqual(parser['launcher']['command_socket'], '/var/lib/zuul/launcher.socket')
        self.assertFalse(yaml.safe_load(config.tenant_configuration(plan))[0]['tenant']['use-nodepool'])
        trusted = yaml.safe_load(config.tenant_configuration(plan))[0]['tenant']['source']['gerrit']['config-projects']
        self.assertTrue(trusted[0]['platform/ci-config']['include-provider-config'])
        plan["integrated_launcher"]["nodepool_fallback"] = True
        self.assertTrue(yaml.safe_load(config.tenant_configuration(plan))[0]['tenant']['use-nodepool'])
        for runtime in ("rke2", "k3s", "k8s"):
            objects = list(yaml.safe_load_all(services.render(plan, runtime)["zuul-services.yaml"]))
            deployment = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "zuul-launcher")
            self.assertEqual(deployment["spec"]["replicas"], 2)
            self.assertIn("launcher", deployment["spec"]["template"]["spec"]["containers"][0]["command"])
        compose = yaml.safe_load(services.render(plan, "docker")["zuul-compose.yaml"])
        self.assertIn("zuul-launcher", compose["services"])
        self.assertIn("zuul-launcher.service", services.render(plan, "raw"))

    @unittest.skipUnless(TenantParser, "Zuul SDK unavailable")
    def test_real_tenant_parser_loads_provider_classes_only_when_selected(self):
        self.assertEqual(version("zuul"), "14.3.0")
        source = Mock()
        current = frozenset(TenantParser.job_classes)
        plan = selected()
        conf = yaml.safe_load(config.tenant_configuration(plan))[0]['tenant']['source']['gerrit']['config-projects'][0]
        loaded = TenantParser._getProject(source, conf, current)
        self.assertTrue(set(TenantParser.provider_classes) <= loaded.load_classes)
        plan["integrated_launcher"] = {"enabled": False}
        conf = yaml.safe_load(config.tenant_configuration(plan))[0]['tenant']['source']['gerrit']['config-projects'][0]
        loaded = TenantParser._getProject(source, conf, current)
        self.assertTrue(set(TenantParser.provider_classes).isdisjoint(loaded.load_classes))


if __name__ == "__main__":
    unittest.main()
