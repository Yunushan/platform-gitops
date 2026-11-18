#!/usr/bin/env python3
"""Check real encryption plus scoped provider reads and uncertain escrow recovery."""

import base64
from contextlib import contextmanager
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from atomic_file import atomic_write_bytes
from bounded_subprocess import run_bounded
import ceph_key_material as material
import ceph_key_recovery as recovery


def uid(number):
    return "00000000-0000-0000-0000-" + str(number).zfill(12)


def fixture(directory):
    image = "quay.io/ceph/ceph@sha256:" + "a" * 64
    pod = {"containers": [{"name": "toolbox", "image": image}], "nodeName": "storage-a"}
    cluster = {"storage": {"config": {"encryptedDevice": "true"}, "useAllNodes": False, "useAllDevices": False,
                           "nodes": [{"name": "storage-" + letter} for letter in "abcd"]}}
    plan = {"schema_version": 1, "operation_id": uid(30), "runtime": "rke2",
            "source": {"target": {"kubeconfig_env": "CEPH_RECOVERY_KUBECONFIG", "context": "storage", "cluster_uid": uid(1)},
                       "namespace": "rook-ceph", "namespace_uid": uid(2), "name": "ceph", "cluster_uid": uid(3),
                       "fsid": uid(4), "spec_sha256": recovery.storage.fingerprint(cluster), "ceph_version": "20.2.5",
                       "pod": {"name": "toolbox-abc", "uid": uid(5), "node": "storage-a", "node_uid": uid(6), "container": "toolbox",
                               "spec_sha256": recovery.storage.fingerprint(pod), "image": image, "image_id": image,
                               "container_id": "containerd://" + "b" * 64, "started_at": "2026-11-16T12:00:00Z"},
                       "osds": [{"id": number, "uuid": uid(10 + number)} for number in range(4)]},
            "destination": {"server": {"url": "https://bao.example.com:8200", "cluster_id": uid(20), "version": "2.7.2",
                                       "raft": {"bao-a": "192.0.2.41:8201", "bao-b": "192.0.2.42:8201", "bao-c": "192.0.2.43:8201"},
                                       "ca_file": str(directory / "ca.crt"), "ca_sha256": "c" * 64, "token_env": "CEPH_RECOVERY_ESCROW_TOKEN"},
                            "mount": "recovery", "mount_uuid": uid(21), "prefix": "ceph/keys"},
            "encryption": {"binary": str(directory / "age"), "binary_sha256": "d" * 64,
                           "recipient": "age1" + "a" * 58},
            "limits": {"command_seconds": 10, "overall_seconds": 300}}
    return plan, cluster, pod


class Kube:
    def __init__(self, plan, cluster, pod):
        self.plan = plan
        scope, selected = plan["source"], plan["source"]["pod"]
        owner = [{"apiVersion": "ceph.rook.io/v1", "kind": "CephCluster", "name": scope["name"], "uid": scope["cluster_uid"], "controller": True}]
        def owned(number):
            return {"uid": uid(number), "namespace": scope["namespace"], "ownerReferences": copy.deepcopy(owner)}
        self.objects = {"namespace/kube-system": {"metadata": {"uid": uid(1)}}, "namespace/rook-ceph": {"metadata": {"uid": uid(2)}},
                        "cephcluster.ceph.rook.io/ceph": {"metadata": {"uid": uid(3)}, "spec": cluster,
                                                       "status": {"phase": "Ready", "ceph": {"health": "HEALTH_OK", "fsid": uid(4)}}},
                        "pod/toolbox-abc": {"metadata": {"uid": uid(5)}, "spec": pod, "status": {"phase": "Running", "containerStatuses": [{
                            "name": "toolbox", "containerID": selected["container_id"], "imageID": selected["image_id"], "restartCount": 0, "ready": True,
                            "state": {"running": {"startedAt": selected["started_at"]}}}]}},
                        "node/storage-a": {"metadata": {"uid": uid(6)}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                        "secret/rook-ceph-mon": {"metadata": owned(7), "data": {key: base64.b64encode(value.encode()).decode() for key, value in {
                            "fsid": uid(4), "cluster-name": "ceph", "admin-secret": "fixture-admin-not-real", "mon-secret": "fixture-mon-not-real"}.items()}},
                        "configmap/rook-ceph-mon-endpoints": {"metadata": owned(8), "data": {"data": "a=192.0.2.1:6789", "mapping": "{}", "maxMonId": "2"}}}
        self.calls = []
        self.reads = 0
        self.change_key = False
        self.omit_lockbox = False

    @contextmanager
    def frozen(self):
        self.calls.append(("freeze",))
        yield self

    def get(self, name, namespace=None):
        self.calls.append(("get", name, namespace))
        return copy.deepcopy(self.objects.get(name))

    def call(self, arguments, *, seconds=30):
        self.calls.append(("call", tuple(arguments)))
        if arguments[0] == "config":
            return json.dumps({"current-context": "storage", "contexts": [{"name": "storage", "context": {"cluster": "storage"}}],
                               "clusters": [{"name": "storage", "cluster": {"server": "https://api.example.com:6443"}}]})
        command = arguments[arguments.index("/usr/bin/ceph") + 1:]
        if command == ["version"]:
            return "ceph version 20.2.5 (fixture) tentacle (stable)\n"
        if command == ["fsid"]:
            return uid(4) + "\n"
        if command == ["--format=json", "osd", "dump"]:
            self.reads += 1
            return json.dumps({"fsid": uid(4), "epoch": 5, "osds": [{"osd": row["id"], "uuid": row["uuid"], "up": 1, "in": 1} for row in self.plan["source"]["osds"]]})
        if command == ["auth", "export"]:
            entities = ["client.admin", *["osd." + str(n) for n in range(4)]]
            if not self.omit_lockbox:
                entities += ["client.osd-lockbox." + row["uuid"] for row in self.plan["source"]["osds"]]
            return "\n".join("[" + entity + "]\n\tkey = fixture-sensitive-key-canary" for entity in entities) + "\n"
        if command[:2] == ["config-key", "get"]:
            assert command[2] in {"dm-crypt/osd/" + row["uuid"] + "/luks" for row in self.plan["source"]["osds"]}
            return base64.b64encode((b"changed-" if self.change_key and self.reads > 1 else b"original") * 8).decode() + "\n"
        raise AssertionError("Unexpected provider command")


class Vault:
    def __init__(self, plan):
        self.plan = plan
        self.item = None
        self.posts = 0
        self.lost_ack = False
        self.accept = True
        self.current = 1
        self.root = False
        self.mount_uuid = plan["destination"]["mount_uuid"]
        self.engine_version = "2"
        self.calls = []

    def json(self, path, *, method="GET", data=None, allow_missing=False):
        self.calls.append((method, path, copy.deepcopy(data)))
        if path == "sys/health":
            return {"cluster_id": self.plan["destination"]["server"]["cluster_id"], "version": "2.7.2", "initialized": True, "sealed": False, "standby": False}
        if path == "sys/storage/raft/configuration":
            return {"data": {"config": {"servers": [{"node_id": name, "address": address, "voter": True, "leader": i == 0}
                                                     for i, (name, address) in enumerate(self.plan["destination"]["server"]["raft"].items())]}}}
        if path == "sys/mounts/" + self.plan["destination"]["mount"]:
            return {"data": {"uuid": self.mount_uuid, "type": "kv", "options": {"version": self.engine_version}}}
        if path == "sys/capabilities-self":
            result = {}
            for value in data["paths"]:
                if self.root:
                    result[value] = ["root"]
                elif value.endswith("/" + self.plan["operation_id"]):
                    result[value] = ["create", "read"] if "/data/" in value else ["read"] if "/metadata/" in value else ["deny"]
                else:
                    result[value] = ["read"] if value in {"sys/storage/raft/configuration", "sys/mounts/" + self.plan["destination"]["mount"]} else ["deny"]
            return result
        if "/metadata/" in path:
            return None if self.item is None else {"data": {"current_version": self.current, "versions": {"1": {"destroyed": False, "deletion_time": ""}}}}
        if "/data/" in path and method == "POST":
            self.posts += 1
            if data["options"] != {"cas": 0} or self.item is not None:
                raise ValueError("CAS refused")
            if self.accept:
                self.item = copy.deepcopy(data["data"])
            if self.lost_ack:
                raise TimeoutError("Simulated lost write acknowledgement")
            return {"data": {"version": 1}}
        if "/data/" in path and method == "GET":
            return {"data": {"data": copy.deepcopy(self.item), "metadata": {"version": 1, "destroyed": False, "deletion_time": ""}}}
        raise AssertionError("Unexpected escrow request")


class ModelCipher:
    """Model only; actual age encryption is exercised by NativeAgeTests."""
    def encrypt(self, value):
        return b"age-encryption.org/v1\nfixture-model\n" + base64.b64encode(value)

    def decrypt(self, value, identity):
        return base64.b64decode(value.split(b"\n", 2)[2], validate=True)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plan, cluster, pod = fixture(self.root)
        self.kube = Kube(self.plan, cluster, pod)
        self.vault = Vault(self.plan)
        self.source = material.Source(self.plan, material.Budget(300), client=self.kube)
        self.output = self.root / "capture"

    def operate(self, mode="capture", **kwargs):
        return recovery.operate(self.plan, mode=mode, output=self.output, private_root=self.root,
                                source=self.source, vault=self.vault, cipher=ModelCipher(), **kwargs)

    def test_local_validation_never_constructs_clients_or_reads_environment_credentials(self):
        with mock.patch.object(recovery, "Vault", side_effect=AssertionError("No network")), mock.patch.object(recovery, "Source", side_effect=AssertionError("No source")):
            self.assertTrue(recovery.operate(self.plan)["local_inputs_verified"])
        self.assertFalse(self.kube.calls)

    def test_complete_capture_has_exact_read_only_source_commands_and_no_plaintext_files_or_escrow(self):
        result = self.operate()
        self.assertTrue(result["captured"])
        self.assertEqual(self.vault.posts, 1)
        self.assertEqual(self.kube.reads, 2)
        for name in recovery.FALSE_FLAGS:
            self.assertIs(result[name], False)
        for call in self.kube.calls:
            if call[0] == "call" and call[1][0] != "config":
                self.assertEqual(call[1][0], "exec")
                self.assertIn("/usr/bin/timeout", call[1])
                self.assertNotIn("get-or-create", call[1])
        for path in self.output.iterdir():
            self.assertNotIn(b"fixture-sensitive-key-canary", path.read_bytes())
        self.assertNotIn("fixture-sensitive-key-canary", json.dumps(self.vault.item))
        self.assertTrue(self.operate("verify-archive", identity=self.root / "not-opened-by-model")["decryption_verified"])

    def test_lost_ack_resume_observes_original_version_without_a_second_write(self):
        self.vault.lost_ack = True
        with self.assertRaises(TimeoutError):
            self.operate()
        self.assertEqual(recovery.load(self.output, self.plan)["phase"], "escrow-create-requested")
        self.assertTrue(self.operate("resume")["escrow_verified"])
        self.assertEqual(self.vault.posts, 1)

    def test_missing_after_lost_ack_is_not_proof_of_failure_or_permission_to_retry(self):
        self.vault.lost_ack, self.vault.accept = True, False
        with self.assertRaises(TimeoutError):
            self.operate()
        with self.assertRaises(ValueError):
            self.operate("resume")
        self.assertEqual(self.vault.posts, 1)

    def test_occupied_entry_is_never_overwritten(self):
        self.vault.item = {"foreign": True}
        with self.assertRaises(ValueError):
            self.operate()
        self.assertEqual(self.vault.posts, 0)
        self.assertEqual(self.vault.item, {"foreign": True})

    def test_key_rotation_and_missing_lockbox_are_refused_before_any_escrow_write(self):
        for field in ("change_key", "omit_lockbox"):
            with self.subTest(field=field):
                setattr(self.kube, field, True)
                with self.assertRaises(ValueError):
                    self.source.collect()
                setattr(self.kube, field, False)
                self.kube.reads = 0
        self.assertEqual(self.vault.posts, 0)

    def test_replaced_container_and_foreign_monitor_secret_stop_capture(self):
        for name, field in (("pod/toolbox-abc", "uid"), ("secret/rook-ceph-mon", "ownerReferences")):
            original = copy.deepcopy(self.kube.objects[name])
            self.kube.objects[name]["metadata"][field] = uid(99) if field == "uid" else []
            with self.assertRaises(ValueError):
                self.source.collect()
            self.kube.objects[name] = original

    def test_recover_reads_ciphertext_without_contacting_source_and_refuses_overwrite(self):
        self.operate()
        original = (self.output / recovery.ARCHIVE).read_bytes()
        with self.assertRaises(FileExistsError):
            self.operate("recover")
        (self.output / recovery.ARCHIVE).unlink()
        with mock.patch.object(recovery, "Source", side_effect=AssertionError("Source unavailable")):
            self.assertTrue(self.operate("recover")["archive_recovered"])
        self.assertEqual((self.output / recovery.ARCHIVE).read_bytes(), original)
        self.assertEqual(self.vault.posts, 1)

    def test_corruption_and_wrong_escrow_version_are_not_accepted(self):
        self.operate()
        self.vault.current = 2
        with self.assertRaises(ValueError):
            self.operate("resume")
        self.vault.current = 1
        path = self.output / recovery.ARCHIVE
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaises(ValueError):
            self.operate("verify-archive", identity=self.root / "unused")

    def test_root_capabilities_stop_before_provider_key_reads(self):
        self.vault.root = True
        with self.assertRaises(ValueError):
            self.operate()
        self.assertFalse(self.kube.calls)

    def test_kv_v1_or_recreated_mount_is_rejected_before_reading_keys_or_writing(self):
        for field, value in (("engine_version", "1"), ("mount_uuid", uid(99))):
            original = getattr(self.vault, field)
            setattr(self.vault, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.operate()
            self.assertFalse(self.kube.calls)
            self.assertEqual(self.vault.posts, 0)
            setattr(self.vault, field, original)
            self.output = self.root / ("next-" + field)

    def test_scope_change_and_traversal_cannot_resume_an_original_capture(self):
        self.operate()
        self.plan["source"]["fsid"] = uid(99)
        with self.assertRaises(ValueError):
            self.operate("resume")
        self.plan["destination"]["prefix"] = "../other"
        with self.assertRaises(ValueError):
            recovery.validate(self.plan)

    def test_prewrite_resume_uses_original_ciphertext_and_submits_only_the_first_write(self):
        original_json = self.vault.json
        def stop_before_intent(path, **kwargs):
            if "/metadata/" in path:
                raise TimeoutError("Pre-write read failed")
            return original_json(path, **kwargs)
        with mock.patch.object(self.vault, "json", side_effect=stop_before_intent):
            with self.assertRaises(TimeoutError):
                self.operate()
        encrypted = (self.output / recovery.ARCHIVE).read_bytes()
        self.assertEqual(recovery.load(self.output, self.plan)["phase"], "encrypted")
        self.assertEqual(self.vault.posts, 0)
        with mock.patch.object(self.source, "collect", side_effect=AssertionError("Do not reread or replace the original source")):
            self.assertTrue(self.operate("resume")["escrow_verified"])
        self.assertEqual(self.vault.posts, 1)
        self.assertEqual((self.output / recovery.ARCHIVE).read_bytes(), encrypted)

    def test_original_journal_lock_blocks_concurrent_resume(self):
        self.operate()
        with recovery.lock(self.output), self.assertRaises(OSError):
            self.operate("resume")
        self.assertEqual(self.vault.posts, 1)

    def test_all_runtime_consumers_keep_the_explicit_provider_target(self):
        for runtime in recovery.storage.RUNTIMES:
            plan = copy.deepcopy(self.plan)
            plan["runtime"] = runtime
            recovery.validate(plan)
            self.assertEqual(plan["source"]["target"], self.plan["source"]["target"])

    def test_platform_image_manifest_and_cri_identity_are_pinned_separately(self):
        scope = self.plan["source"]["pod"]
        status = self.kube.objects["pod/toolbox-abc"]["status"]["containerStatuses"][0]
        scope["image_id"] = "sha256:" + "e" * 64
        scope["container_id"] = "cri-o://" + "f" * 64
        status.update(imageID=scope["image_id"], containerID=scope["container_id"])
        recovery.validate(self.plan)
        self.source.collect()
        status["imageID"] = "sha256:" + "1" * 64
        with self.assertRaises(ValueError):
            self.source.collect()

    def test_incomplete_decrypted_monitor_or_lockbox_material_is_rejected(self):
        record = {"operation": self.plan["operation_id"]}
        original = {"schema_version": 1, "operation": record["operation"], "plan_sha256": recovery.storage.fingerprint(self.plan),
                    "source": self.plan["source"], "material": self.source.collect()}
        for change in ("monitor", "lockbox", "boolean-osd"):
            value = copy.deepcopy(original)
            if change == "monitor":
                del value["material"]["monitor_secret"]["data"]["mon-secret"]
            elif change == "lockbox":
                value["material"]["auth_export_base64"] = base64.b64encode(b"[client.admin]\n key = fixture\n").decode()
            else:
                value["material"]["osds"][0]["id"] = False
            with self.subTest(change=change), self.assertRaises(ValueError):
                recovery.validate_plaintext(value, self.plan, record)

    def test_boolean_escrow_schema_and_false_completed_claims_are_rejected(self):
        self.operate()
        self.vault.item["schema_version"] = True
        with self.assertRaises(ValueError):
            self.operate("resume")
        record = recovery.load(self.output, self.plan)
        record["escrow_verified"] = False
        recovery.save(self.output, record)
        with self.assertRaises(ValueError):
            recovery.load(self.output, self.plan)

    def test_http_adapter_rejects_redirects_and_only_treats_explicit_read_404_as_missing(self):
        client = object.__new__(recovery.Vault)
        client.budget, client.seconds, client.bearer = material.Budget(60), 10, "fixture-not-a-real-token"
        response, connection = mock.Mock(status=302), mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(client, "connect", return_value=connection):
            with self.assertRaises(ValueError):
                client.json("recovery/data/allowed", method="POST", data={"options": {"cas": 0}})
            response.read.assert_not_called()
            response.read1.assert_not_called()
            self.assertEqual(connection.request.call_count, 1)
            response.status = 404
            self.assertIsNone(client.json("recovery/metadata/allowed", allow_missing=True))
            with self.assertRaises(ValueError):
                client.json("recovery/data/allowed", method="POST", data={}, allow_missing=True)
            response.status = 200
            response.read1.side_effect = io.BytesIO(b'{"data":{"version":1}}').read
            self.assertEqual(client.json("recovery/data/allowed", method="POST", data={"options": {"cas": 0}}), {"data": {"version": 1}})
        self.assertEqual(connection.close.call_count, 4)


@unittest.skipUnless(os.environ.get("CEPH_RECOVERY_TEST_AGE") and os.environ.get("CEPH_RECOVERY_TEST_KEYGEN"), "Requires separately pinned native age fixtures")
class NativeAgeTests(unittest.TestCase):
    def setUp(self):
        RecoveryTests.setUp(self)
        binary, keygen = (Path(os.environ[name]) for name in ("CEPH_RECOVERY_TEST_AGE", "CEPH_RECOVERY_TEST_KEYGEN"))
        for path, variable in ((binary, "CEPH_RECOVERY_TEST_AGE_SHA256"), (keygen, "CEPH_RECOVERY_TEST_KEYGEN_SHA256")):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), os.environ[variable])
        generated = run_bounded([str(keygen)], timeout=20, output_max_bytes=65536, check=False)
        self.assertEqual(generated.returncode, 0)
        self.identity = self.root / "recovery-identity"
        atomic_write_bytes(self.identity, generated.stdout, replace=False)
        recipient = re.search(rb"(?m)^# public key: (age1[0-9a-z]+)$", generated.stdout).group(1).decode()
        self.plan["encryption"] = {"binary": str(binary), "binary_sha256": os.environ["CEPH_RECOVERY_TEST_AGE_SHA256"], "recipient": recipient}

    def test_native_age_capture_and_offline_decryption_reject_wrong_identity_and_tampering(self):
        result = recovery.operate(self.plan, mode="capture", output=self.output, private_root=self.root,
                                  source=self.source, vault=self.vault)
        self.assertTrue(result["escrow_verified"])
        encrypted = (self.output / recovery.ARCHIVE).read_bytes()
        self.assertNotIn(b"fixture-sensitive-key-canary", encrypted)
        verified = recovery.operate(self.plan, mode="verify-archive", output=self.output, private_root=self.root, identity=self.identity)
        self.assertTrue(verified["decryption_verified"])
        for name in recovery.FALSE_FLAGS:
            self.assertIs(verified[name], False)
        wrong = run_bounded([os.environ["CEPH_RECOVERY_TEST_KEYGEN"]], timeout=20, output_max_bytes=65536, check=False)
        other_identity = self.root / "wrong-identity"
        atomic_write_bytes(other_identity, wrong.stdout, replace=False)
        with self.assertRaises(ValueError):
            recovery.operate(self.plan, mode="verify-archive", output=self.output, private_root=self.root, identity=other_identity)
        damaged = bytearray(encrypted)
        damaged[-1] ^= 1
        with self.assertRaises(ValueError):
            recovery.Cipher(self.plan, material.Budget(60)).decrypt(bytes(damaged), self.identity)


if __name__ == "__main__":
    unittest.main()
