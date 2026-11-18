"""Checks for review-only integrated static-provider fragments."""

import copy
from importlib.metadata import version
import unittest

import yaml

import render_zuul_integrated_static as renderer
from test_nodepool_static import plans, public_key

try:
    from zuul.configloader import ProviderParser, SectionParser
    from zuul.driver.static.staticprovider import StaticProviderSchema
except ImportError:
    ProviderParser = SectionParser = StaticProviderSchema = None


class IntegratedStaticTests(unittest.TestCase):
    def test_worker_pins_and_single_slot_are_preserved(self):
        supply, ci = plans()
        ci["zuul_version"] = "14.3.0"
        objects = yaml.safe_load(renderer.fragments(supply, ci)["platform"])
        provider = next(obj["provider"] for obj in objects if "provider" in obj)
        self.assertEqual(provider["labels"][0]["slots"], 1)
        self.assertTrue(provider["labels"][0]["host-key-checking"])
        self.assertTrue(all(node["host-key"] == public_key() for node in provider["nodes"]))
        self.assertEqual(len(provider["nodes"]), 2)

    def test_shared_trusted_project_is_rejected(self):
        supply, ci = plans()
        ci["zuul_version"] = "14.3.0"
        another = copy.deepcopy(ci["tenants"][0])
        another["name"] = "other"
        another["allowed_labels"] = ["other-linux"]
        ci["tenants"].append(another)
        other_pool = copy.deepcopy(supply["pools"][0])
        other_pool.update(name="other-linux", label="other-linux", tenant="other")
        for index, node in enumerate(other_pool["nodes"]):
            node["host"] = f"other-worker-{index}.example.com"
        supply["pools"].append(other_pool)
        with self.assertRaisesRegex(ValueError, "Trusted projects"):
            renderer.fragments(supply, ci)

    def test_invalid_inventory_is_rejected(self):
        supply, ci = plans()
        ci["zuul_version"] = "14.3.0"
        supply["pools"][0]["nodes"][0]["username"] = "root"
        with self.assertRaises(ValueError):
            renderer.fragments(supply, ci)

    def test_unreviewed_zuul_versions_are_rejected(self):
        supply, ci = plans()
        with self.assertRaisesRegex(ValueError, "14.3.0"):
            renderer.fragments(supply, ci)

    def test_all_runtime_bundles_are_review_only(self):
        supply, ci = plans()
        ci["zuul_version"] = "14.3.0"
        for runtime in ("rke2", "k3s", "k8s", "docker", "raw"):
            files = renderer.render(supply, ci, runtime)
            self.assertEqual(set(files), {"tenant-platform.yaml", "render-status.json"})
            import json
            status = json.loads(files["render-status.json"])
            self.assertFalse(status["deployed"])
            self.assertFalse(status["allocation_verified"])
            self.assertEqual(status["runtime"], runtime)

    @unittest.skipUnless(StaticProviderSchema, "Zuul SDK unavailable")
    def test_zuul_schema_accepts_synthetic_inherited_provider(self):
        self.assertEqual(version("zuul"), "14.3.0")
        supply, ci = plans()
        ci["zuul_version"] = "14.3.0"
        objects = yaml.safe_load(renderer.fragments(supply, ci)["platform"])
        get = lambda kind: next(item[kind] for item in objects if kind in item)
        provider, section = get("provider"), get("section")
        ProviderParser.schema(provider)
        SectionParser.schema(section)
        merged = dict(provider, connection=section["connection"])
        merged["images"] = [get("image")]
        merged["flavors"] = [get("flavor")]
        merged["labels"] = [dict(get("label"), **provider["labels"][0])]
        StaticProviderSchema().getProviderSchema()(merged)


if __name__ == "__main__":
    unittest.main()
