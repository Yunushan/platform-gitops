"""Offline coverage for profile-local, version-exact CRD schemas."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from rendered_crd_schemas import build_crd_schema_catalog, crd_schema_location


def definition():
    return {
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "metadata": {"name": "examples.unit.example"},
        "spec": {
            "group": "unit.example", "names": {"kind": "Example", "plural": "examples"},
            "versions": [
                {
                    "name": "v1", "served": True, "storage": True,
                    "schema": {"openAPIV3Schema": {"type": "object", "properties": {
                        "metadata": {"type": "object"},
                        "spec": {"type": "object", "required": ["count"], "properties": {
                            "count": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
                            "target": {"x-kubernetes-int-or-string": True},
                            "caption": {"type": "string", "nullable": True},
                            "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                            "options": {"type": "array", "x-kubernetes-list-type": "set",
                                        "items": {"type": "string"}},
                            "opaque": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
                            "nested": {"type": "object", "x-kubernetes-embedded-resource": True,
                                       "x-kubernetes-preserve-unknown-fields": True},
                        }},
                    }}},
                },
                {"name": "v2", "served": False, "storage": False},
            ],
        },
    }


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog"

    def build(self, *resources):
        files = []
        for index, resource in enumerate(resources):
            path = self.root / f"{index}.yaml"
            path.write_text(json.dumps(resource), encoding="utf-8")
            files.append(path)
        return build_crd_schema_catalog(files, self.catalog)

    def test_order_duplicate_and_version(self):
        instance = {"apiVersion": "unit.example/v1", "kind": "Example"}
        catalog = self.build(instance, definition(), definition())
        self.assertEqual(set(catalog), {("unit.example/v1", "Example")})
        path = catalog[("unit.example/v1", "Example")]
        self.assertEqual(path.relative_to(self.catalog).as_posix(), "unit.example/v1/example.json")
        schema = json.loads(path.read_text())
        self.assertEqual(schema["properties"]["apiVersion"]["const"], "unit.example/v1")
        self.assertFalse(schema["properties"]["spec"]["additionalProperties"])
        self.assertEqual(schema["properties"]["spec"]["properties"]["count"]["exclusiveMinimum"], 0)
        self.assertNotIn("additionalProperties", schema["properties"]["metadata"])

    def test_unserved_version(self):
        with self.assertRaisesRegex(ValueError, "unserved"):
            self.build({"apiVersion": "unit.example/v2", "kind": "Example"}, definition())

    def test_kind_case_cannot_reuse_schema(self):
        with self.assertRaisesRegex(ValueError, "incorrect kind"):
            self.build({"apiVersion": "unit.example/v1", "kind": "EXAMPLE"}, definition())

    def test_conflict(self):
        changed = definition()
        changed["spec"]["versions"][0]["served"] = False
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.build(definition(), changed)

    def test_unsafe_inputs(self):
        for api, kind in (("../v1", "Example"), ("unit.example/v1", "../Example"),
                          ("/v1", "Pod"), ("con.test/v1", "Example")):
            with self.subTest(api=api, kind=kind), self.assertRaisesRegex(ValueError, "unsafe"):
                self.build({"apiVersion": api, "kind": kind})
        obj = definition()
        obj["spec"]["group"] = "rbac.authorization.k8s.io"
        with self.assertRaisesRegex(ValueError, "native"):
            self.build(obj)

    def test_schema_references_are_never_followed(self):
        for dangerous in ({"$ref": "https://example.invalid/a"},
                          {"if": {"$ref": "file:///unavailable"}},
                          {"properties": {"field": {"$dynamicRef": "external"}}}):
            obj = definition()
            obj["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["bad"] = dangerous
            with self.subTest(dangerous=dangerous), self.assertRaisesRegex(ValueError, "unsupported"):
                self.build(obj)

    def test_yaml12_enum_and_malformed_input(self):
        path = self.root / "manifest.yaml"
        path.write_text("apiVersion: v1\nkind: ConfigMap\ndata:\n  symbol: =\n")
        self.assertEqual(build_crd_schema_catalog([path], self.catalog), {})
        path.write_text("apiVersion: v1\nkind: ConfigMap\nkind: Secret\n")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_crd_schema_catalog([path], self.catalog)

    def test_real_kubeconform(self):
        binary = shutil.which(os.environ.get("KUBECONFORM_BIN", "kubeconform"))
        if not binary:
            self.skipTest("Kubeconform binary unavailable")
        self.build(definition())
        base = {
            "apiVersion": "unit.example/v1", "kind": "Example",
            "metadata": {"name": "test", "labels": {"team": "ci"}},
            "spec": {"count": 1, "target": 8080, "caption": None, "labels": {"owner": "test"},
                     "opaque": {"unmodeled": [True]}, "nested": {"apiVersion": "v1", "kind": "Pod",
                                                                  "metadata": {"name": "nested"}}},
        }
        for accepted, change in ((True, {}), (True, {"target": "https"}),
                                 (True, {"options": ["a", "b"]}), (False, {"options": ["a", "a"]}),
                                 (False, {"typo": True}), (False, {"count": 0}),
                                 (False, {"target": False}), (False, {"labels": {"wrong": 7}})):
            item = deepcopy(base)
            item["spec"].update(change)
            with self.subTest(accepted=accepted, change=change):
                run = subprocess.run(
                    [binary, "-strict", "-summary", "-output", "json",
                     "-schema-location", crd_schema_location(self.catalog)],
                    input=json.dumps(item), text=True, capture_output=True, timeout=20,
                )
                summary = json.loads(run.stdout)["summary"]
                self.assertEqual(run.returncode == 0, accepted, run.stdout)
                self.assertEqual(summary["valid" if accepted else "invalid"], 1, run.stdout)


if __name__ == "__main__":
    unittest.main()
