#!/usr/bin/env python3
"""Test static supply and tenant label restrictions without SSH or registration."""

import base64
import contextlib
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import yaml

import render_nodepool_static as nodepool
import render_zuul_config as zuul
from test_zuul_config import fixture


def public_key():
    parts = [b"ssh-ed25519", bytes(range(32))]
    wire = b"".join(struct.pack(">I", len(part)) + part for part in parts)
    return "ssh-ed25519 " + base64.b64encode(wire).decode()


def plans():
    ci = fixture()
    ci["tenants"][0]["allowed_labels"] = ["platform-linux"]
    supply = {"schema_version": 1, "nodepool_version": "11.0.0", "namespace": "nodepool",
              "image": "registry.example.com/platform/nodepool@sha256:" + "b" * 64,
              "pools": [{"name": "platform-linux", "tenant": "platform", "label": "platform-linux", "nodes": [
                  {"host": f"worker-{index}.example.com", "port": 22, "username": "zuul", "host_key": public_key(),
                   "failure_domain": f"rack-{index}"} for index in range(2)]}]}
    return supply, ci


class StaticWorkerTests(unittest.TestCase):
    def test_strict_host_pins_tls_and_single_job_capacity(self):
        supply, ci = plans()
        rendered = yaml.safe_load(nodepool.configuration(supply, ci))
        self.assertEqual(rendered["zookeeper-servers"], ci["zookeeper"])
        self.assertEqual(set(rendered["zookeeper-tls"]), {"cert", "key", "ca"})
        provider = rendered["providers"][0]
        self.assertEqual(provider["driver"], "static")
        for node in provider["pools"][0]["nodes"]:
            self.assertTrue(node["host-key-checking"])
            self.assertEqual(node["host-key"], public_key())
            self.assertEqual(node["max-parallel-jobs"], 1)
            self.assertEqual(node["python-path"], "/usr/bin/python3")
        self.assertNotIn("diskimages", rendered)

    def test_worker_key_wire_structure_rejects_mismatches(self):
        self.assertTrue(nodepool.host_key(public_key()))
        for invalid in ("", "fake-key", "ssh-rsa " + public_key().split()[1], public_key() + " comment", "ssh-ed25519 !!!",
                        public_key().replace(" ", "\n"), " " + public_key()):
            self.assertFalse(nodepool.host_key(invalid))

    def test_rsa_wire_requires_positive_odd_exponent_and_bounded_modulus(self):
        def rsa(exponent):
            parts = [b"ssh-rsa", exponent, b"\x00\x80" + b"\x01" * 255]
            wire = b"".join(struct.pack(">I", len(part)) + part for part in parts)
            return "ssh-rsa " + base64.b64encode(wire).decode()
        self.assertTrue(nodepool.host_key(rsa(b"\x01\x00\x01")))
        self.assertFalse(nodepool.host_key(rsa(b"\x04")))
        self.assertFalse(nodepool.host_key(rsa(b"\x80")))

    def test_explicit_tenant_labels_are_anchored_and_preserved(self):
        supply, ci = plans()
        files = nodepool.render(supply, ci, "rke2")
        tenant = yaml.safe_load(files["zuul-tenants.yaml"])[0]["tenant"]
        self.assertEqual(tenant["allowed-labels"], ["^platform\\-linux$"])
        self.assertEqual(tenant["source"]["gerrit"]["config-projects"], [{"platform/ci-config": {"load-branch": "main"}}])

    def test_unrestricted_and_cross_tenant_labels_fail_closed(self):
        supply, ci = plans()
        del ci["tenants"][0]["allowed_labels"]
        with self.assertRaises(ValueError):
            nodepool.validate(supply, ci)
        supply, ci = plans()
        second = copy.deepcopy(ci["tenants"][0])
        second["name"] = "another-tenant"
        ci["tenants"].append(second)
        with self.assertRaises(ValueError):
            nodepool.validate(supply, ci)
        supply, ci = plans()
        supply["pools"][0]["tenant"] = "missing"
        with self.assertRaises(ValueError):
            nodepool.validate(supply, ci)

    def test_existing_zuul_contract_without_restrictions_is_unchanged(self):
        tenant = yaml.safe_load(zuul.tenant_configuration(fixture()))[0]["tenant"]
        self.assertNotIn("allowed-labels", tenant)

    def test_duplicate_worker_identity_and_single_domain_are_rejected(self):
        supply, ci = plans()
        supply["pools"][0]["nodes"][1]["host"] = supply["pools"][0]["nodes"][0]["host"]
        with self.assertRaises(ValueError):
            nodepool.validate(supply, ci)
        supply, ci = plans()
        supply["pools"][0]["nodes"][1]["failure_domain"] = "rack-0"
        with self.assertRaises(ValueError):
            nodepool.validate(supply, ci)
        supply, ci = plans()
        supply["pools"][0]["nodes"][0]["username"] = "root"
        with self.assertRaises(ValueError):
            nodepool.validate(supply, ci)

    def test_all_targets_report_configuration_only_not_allocation(self):
        supply, ci = plans()
        for runtime in nodepool.RUNTIMES:
            files = nodepool.render(supply, ci, runtime)
            status = json.loads(files["render-status.json"])
            self.assertFalse(status["registered"])
            self.assertFalse(status["workers_modified"])
            self.assertFalse(status["allocation_verified"])
            self.assertFalse(status["ha_verified"])

    def test_cli_outputs_new_private_files_without_echoing_hosts(self):
        supply, ci = plans()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker_plan, ci_plan = root / "workers.json", root / "ci.json"
            worker_plan.write_text(json.dumps(supply), encoding="utf-8")
            ci_plan.write_text(json.dumps(ci), encoding="utf-8")
            output = root / "private" / "new"
            args = ["--plan", str(worker_plan), "--zuul-plan", str(ci_plan), "--runtime", "docker", "--output-dir", str(output)]
            with mock.patch.object(nodepool, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(nodepool.main(args), 0)
            self.assertFalse(json.loads(stdout.getvalue())["registered"])
            self.assertNotIn("worker-0.example.com", stdout.getvalue())
            before = (output / "nodepool.yaml").read_bytes()
            with mock.patch.object(nodepool, "ROOT", root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(nodepool.main(args), 1)
            self.assertEqual((output / "nodepool.yaml").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
