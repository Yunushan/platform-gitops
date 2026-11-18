import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import yaml

import provision_woodpecker_gitea_oauth as subject


def fixture():
    return {"schema_version": 1, "target": {"kubeconfig_env": "FIXTURE_KUBECONFIG", "context": "fixture", "cluster_uid": "11111111-1111-4111-8111-111111111111"},
            "namespace": "woodpecker", "namespace_uid": "22222222-2222-4222-8222-222222222222", "secret_name": "woodpecker-gitea-oauth"}


class FakeClient:
    def __init__(self): self.value = None; self.creates = 0; self.lost = False
    def get(self, name, namespace=None, optional=False):
        return {"metadata": {"uid": fixture()["namespace_uid"]}} if name.startswith("namespace/") else copy.deepcopy(self.value)
    def call(self, args, payload=None):
        self.creates += 1; self.value = json.loads(payload)
        if self.lost: raise ValueError("fixture lost acknowledgement")
        return "{}"


class SecretTests(unittest.TestCase):
    def test_shared_bootstrap_checks_gitea_before_writes_and_skips_forgejo_oauth(self):
        playbook = Path(__file__).resolve().parents[1] / "ansible/playbooks/configure-platform-app-secrets.yml"
        tasks = yaml.safe_load(playbook.read_text(encoding="utf-8"))[0]["tasks"]
        self.assertEqual(tasks[0]["name"], "Validate Woodpecker forge selection")
        self.assertEqual(tasks[1]["name"], "Require pre-provisioned Gitea OAuth Secret")
        self.assertTrue(tasks[1]["no_log"])
        self.assertEqual(tasks[1]["when"], "platform_woodpecker_forge_provider == 'gitea'")
        forgejo = next(task for task in tasks if task.get("name") == "Configure Forgejo OAuth application for Woodpecker")
        self.assertEqual(forgejo["when"], "platform_woodpecker_forge_provider == 'forgejo'")
        valkey = next(task for task in tasks if task.get("name") == "Generate or preserve shared platform Valkey auth secret")
        self.assertNotIn("when", valkey)

    def test_health_check_selects_gitea_or_forgejo_oauth_contract(self):
        path = Path(__file__).resolve().parents[1] / "ansible/playbooks/verify-platform-app-health.yml"
        text = path.read_text(encoding="utf-8")
        play = yaml.safe_load(text)[0]
        self.assertIn("WOODPECKER_GITEA_OAUTH_SECRET_NAME", play["vars"]["platform_woodpecker_gitea_oauth_secret_name_from_env"])
        self.assertIn("WOODPECKER_FORGE_PROVIDER", play["vars"]["platform_woodpecker_forge_provider_from_env"])
        self.assertIn("WOODPECKER_GITEA_CLIENT WOODPECKER_GITEA_SECRET", text)
        self.assertIn("WOODPECKER_FORGEJO_CLIENT WOODPECKER_FORGEJO_SECRET", text)

    @unittest.skipUnless(os.name == "posix", "private file permission check requires POSIX")
    def test_private_files_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            for name in subject.KEYS:
                path = root / name; path.write_bytes(b"synthetic-value\n"); path.chmod(0o600)
            self.assertEqual(set(subject.read_inputs(root)), set(subject.KEYS.values()))
            (root / "client-secret").chmod(0o644)
            with self.assertRaises(ValueError): subject.read_inputs(root)

    def test_create_idempotence_and_lost_ack(self):
        client = FakeClient(); client.lost = True
        data = {"WOODPECKER_GITEA_CLIENT": "YQ==", "WOODPECKER_GITEA_SECRET": "Yg=="}
        with mock.patch.object(subject, "verify_target"):
            self.assertFalse(subject.provision(client, fixture(), data)["secret_present_and_matching"])
            self.assertTrue(subject.provision(client, fixture(), data, apply=True)["secret_present_and_matching"])
            self.assertTrue(subject.provision(client, fixture(), data, apply=True)["secret_present_and_matching"])
        self.assertEqual(client.creates, 1)

    def test_existing_different_secret_is_never_overwritten(self):
        client = FakeClient(); client.value = {"metadata": {}, "type": "Opaque", "data": {"unrelated": "eA=="}}
        with mock.patch.object(subject, "verify_target"), self.assertRaises(ValueError):
            subject.provision(client, fixture(), {"WOODPECKER_GITEA_CLIENT": "YQ==", "WOODPECKER_GITEA_SECRET": "Yg=="}, apply=True)
        self.assertEqual(client.creates, 0)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("GITEA_OAUTH_API_FIXTURE") == "1", "isolated API fixture not requested")
    def test_real_api_create_readback_and_refuse_rotation(self):
        import test_rook_api_fixture as api_fixture
        def check(admin, _root, kubectl):
            plan = fixture(); plan["target"] = {"kubeconfig_env": "IGNORED_FOR_FIXTURE", "context": "storage", "cluster_uid": admin.get("ns/kube-system")["metadata"]["uid"]}
            namespace = kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": plan["namespace"]}})
            plan["namespace_uid"] = namespace["metadata"]["uid"]
            data = {"WOODPECKER_GITEA_CLIENT": "YQ==", "WOODPECKER_GITEA_SECRET": "Yg=="}
            self.assertTrue(subject.provision(admin, plan, data, apply=True)["secret_present_and_matching"])
            self.assertTrue(subject.provision(admin, plan, data, apply=True)["secret_present_and_matching"])
            with self.assertRaises(ValueError): subject.provision(admin, plan, {**data, "WOODPECKER_GITEA_SECRET": "Yw=="}, apply=True)
        api_fixture.exercise(self, management_action=check)


if __name__ == "__main__": unittest.main()
