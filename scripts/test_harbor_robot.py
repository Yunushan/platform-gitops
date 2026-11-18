#!/usr/bin/env python3
"""Project-only Harbor robot provisioning, privacy and uncertain-write contracts."""

import copy
from datetime import datetime, timezone
import hashlib
import http.client
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import provision_harbor_robot as helper


def plan(root):
    ca = root / "ca.crt"; ca.write_bytes(b"synthetic CA; never used for TLS")
    return {"schema_version": 1, "server": {"origin": "https://harbor.example.invalid:8443",
        "harbor_version": "v2.15.1-335c05f2", "ca_file": str(ca.resolve()), "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(),
        "leaf_sha256": "a" * 64, "admin_username": "operator", "password_env": "HARBOR_OPERATOR_PASSWORD"},
        "project": {"name": "fixture", "id": 7}, "robot": {"name": "ci-publisher", "username": "robot$fixture+ci-publisher", "duration_days": 7}}


class Fake:
    def __init__(self, specification):
        self.plan = specification; self.row = None; self.created = 0; self.authenticated = 0; self.lose_response = False; self.auth_fails = False
    def ready(self): pass
    def lookup(self): return copy.deepcopy(self.row)
    def robot(self, identifier):
        if self.row is None or identifier != self.row["id"]: raise ValueError("removed")
        return copy.deepcopy(self.row)
    def create(self, operation):
        self.created += 1; created = datetime.now(timezone.utc).isoformat(); expires = int(time.time()) + self.plan["robot"]["duration_days"] * 86400
        self.row = {"id": 41, "name": self.plan["robot"]["username"], "description": helper.marker(operation, self.plan),
            "duration": self.plan["robot"]["duration_days"], "level": "project", "disable": False, "editable": True,
            "creation_time": created, "expires_at": expires, "permissions": copy.deepcopy(helper.permission(self.plan))}
        if self.lose_response: raise TimeoutError("uncertain synthetic creation")
        return {"id": 41, "name": self.row["name"], "creation_time": created, "expires_at": expires, "secret": "private-synthetic-robot-secret"}
    def authenticate(self, credential):
        self.authenticated += 1
        if self.auth_fails or credential["secret"] != "private-synthetic-robot-secret": raise ValueError("synthetic credential refused")


class RobotContracts(unittest.TestCase):
    def test_plan_refuses_system_wildcard_infinite_or_unpinned_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            value = plan(Path(directory)); helper.validate(value)
            changes = [("schema_version", True), ("server.origin", "http://harbor.example.invalid"),
                ("server.origin", "https://operator:secret@harbor.example.invalid"), ("server.origin", "https://harbor.example.invalid/path"),
                ("server.ca_sha256", "unreviewed"), ("server.harbor_version", "latest"), ("project.id", True),
                ("project.name", "*"), ("robot.duration_days", -1), ("robot.duration_days", 91), ("robot.username", "robot$system")]
            for field, replacement in changes:
                changed = copy.deepcopy(value); keys = field.split("."); target = changed
                for key in keys[:-1]: target = target[key]
                target[keys[-1]] = replacement
                with self.subTest(field=field), self.assertRaises(ValueError): helper.validate(changed)

    def test_preview_never_reserves_state_or_creates_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value = plan(root); client = Fake(value); state = root / "not-created"
            result = helper.operate(value, state, client=client)
            self.assertTrue(result["read_only"]); self.assertTrue(result["creation_available"])
            self.assertFalse(state.exists()); self.assertEqual(client.created, 0); self.assertEqual(client.authenticated, 0)

    def test_apply_and_resume_retain_original_private_credential_without_recreate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value = plan(root); client = Fake(value); state = root / "state"
            self.assertTrue(helper.operate(value, state, mode="apply", client=client)["permissions_verified"])
            original = (state / "credential.json").read_bytes()
            result = helper.operate(value, state, mode="resume", client=client)
            self.assertTrue(result["server_read_only"]); self.assertTrue(result["local_state_written"])
            self.assertEqual(client.created, 1); self.assertEqual((state / "credential.json").read_bytes(), original)
            with self.assertRaises(FileExistsError): helper.operate(value, state, mode="apply", client=Fake(value))

    def test_preexisting_account_is_never_adopted_or_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value = plan(root); client = Fake(value); client.create("a" * 32); state = root / "new"
            with self.assertRaisesRegex(ValueError, "existing robot"): helper.operate(value, state, mode="apply", client=client)
            self.assertFalse(state.exists()); self.assertEqual(client.created, 1)

    def test_lost_create_response_never_repeats_post_or_rotates_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value = plan(root); client = Fake(value); client.lose_response = True; state = root / "state"
            with self.assertRaises(TimeoutError): helper.operate(value, state, mode="apply", client=client)
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "one-time secret response"): helper.operate(value, state, mode="resume", client=client)
            self.assertEqual(client.created, 1); self.assertEqual(helper.load(state / "state.json")["phase"], "credential-unavailable")
            self.assertFalse((state / "credential.json").exists())

    def test_saved_one_time_response_survives_later_authentication_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value = plan(root); client = Fake(value); client.auth_fails = True; state = root / "state"
            with self.assertRaises(ValueError): helper.operate(value, state, mode="apply", client=client)
            self.assertTrue((state / "create-response.json").exists()); client.auth_fails = False
            self.assertTrue(helper.operate(value, state, mode="resume", client=client)["credential_authentication_verified"])
            self.assertEqual(client.created, 1)

    def test_drifted_permissions_identity_secret_plan_and_removed_robot_refuse(self):
        for change in ("permissions", "id", "secret", "plan", "removed", "completed-binding"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); value = plan(root); client = Fake(value); state = root / "state"
                helper.operate(value, state, mode="apply", client=client)
                if change == "permissions": client.row["permissions"][0]["access"].append({"resource": "repository", "action": "delete", "effect": "allow"})
                elif change == "id": client.row["id"] += 1
                elif change == "secret":
                    credential = helper.load(state / "credential.json"); credential["secret"] = "changed-private-synthetic-secret"; helper.save(state / "credential.json", credential)
                elif change == "plan": value = copy.deepcopy(value); value["robot"]["duration_days"] = 8
                elif change == "removed": client.row = None
                else:
                    document = helper.load(state / "state.json"); del document["credential_sha256"]; helper.save(state / "state.json", document)
                with self.subTest(change=change), self.assertRaises(ValueError): helper.operate(value, state, mode="resume", client=client)
                self.assertEqual(client.created, 1)

    def test_management_transport_refuses_other_destinations_updates_and_scope_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            value = plan(Path(directory))
            with mock.patch.object(helper.ssl, "create_default_context", return_value=mock.Mock()): client = helper.Client(value, password="<SYNTHETIC_ADMIN_PASSWORD>")
            with mock.patch.object(http.client.HTTPSConnection, "__init__", return_value=None) as connect:
                for method, path in (("GET", "https://other.example.invalid/api/v2.0/systeminfo"), ("GET", "//other.example.invalid/api/v2.0/robots"),
                        ("GET", "/api/v2.0/projects/8"), ("PUT", "/api/v2.0/robots/41"), ("DELETE", "/api/v2.0/robots/41")):
                    with self.subTest(method=method), self.assertRaises(ValueError): client.request(method, path)
                for field, replacement in (("level", "system"), ("secret", "caller-selected-secret"), ("duration", -1)):
                    body = helper.payload(value, "a" * 32); body[field] = replacement
                    with self.subTest(field=field), self.assertRaises(ValueError): client.request("POST", "/api/v2.0/robots", body)
                with self.assertRaises(ValueError): client.request("GET", "/api/v2.0/systeminfo", authorization="value\r\nInjected: header")
                connect.assert_not_called()

    def test_project_lookup_completes_pagination_without_plus_name_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            value = plan(Path(directory))
            with mock.patch.object(helper.ssl, "create_default_context", return_value=mock.Mock()): client = helper.Client(value, password="<SYNTHETIC_ADMIN_PASSWORD>")
            first = [{"id": i + 1, "name": "other-robot-" + str(i)} for i in range(100)]
            target = {"id": 101, "name": value["robot"]["username"]}
            client.request = mock.Mock(side_effect=[(200, {"x-total-count": "101"}, first), (200, {"x-total-count": "101"}, [target])])
            self.assertEqual(client.lookup(), target)
            self.assertEqual(client.request.call_args_list, [mock.call("GET", client.collections[0]), mock.call("GET", client.collections[1])])
            self.assertNotIn("Name", client.collection); self.assertIn("ProjectID", client.collection)
            for second in ((200, {"x-total-count": "102"}, [target]), (200, {"x-total-count": "101"}, [{"id": 100, "name": target["name"]}])):
                client.request = mock.Mock(side_effect=[(200, {"x-total-count": "101"}, first), second])
                with self.assertRaises(ValueError): client.lookup()

    def test_project_readback_and_incomplete_name_filter_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            value = plan(Path(directory))
            with mock.patch.object(helper.ssl, "create_default_context", return_value=mock.Mock()): client = helper.Client(value, password="<SYNTHETIC_ADMIN_PASSWORD>")
            for project in ({"project_id": 8, "name": "fixture", "metadata": {"public": "false"}},
                    {"project_id": 7, "name": "fixture", "metadata": {"public": "true"}}):
                client.request = mock.Mock(side_effect=[(200, {}, {"harbor_version": value["server"]["harbor_version"]}), (200, {}, project)])
                with self.assertRaises(ValueError): client.ready()
            for total, rows in ((None, []), ("2", [{"name": value["robot"]["username"]}] * 2), ("1", []), ("1", [{"name": "unreviewed"}])):
                client.request = mock.Mock(return_value=(200, {"x-total-count": total}, rows))
                with self.assertRaises(ValueError): client.lookup()


if __name__ == "__main__": unittest.main()
