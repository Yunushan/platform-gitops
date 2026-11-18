#!/usr/bin/env python3
"""Real restricted robot OCI publication through original Harbor failure survivors."""

import hashlib
from urllib.parse import urlencode

import provision_harbor_robot as helper
from strict_json import loads_strict_json
from test_harbor_core_replica_fixture import CoreReplicaChecks
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_jobservice_replica_fixture import JobReplicaChecks
from test_harbor_jwt_runtime_fixture import CoreRegistry, JWTChecks, original_location, token_parts
from test_harbor_pool_health_fixture import PoolHealthChecks
from test_harbor_registry_runtime_fixture import REPOSITORY, RegistryChecks, digest, publish, retrieve
from test_harbor_service_runtime_fixture import ServiceChecks


class RobotChecks:
    def jwt_identities_ready(self, context):
        core = self.service_clients["harbor-core"]
        status, raw = core.request("GET", "/api/v2.0/projects/fixture", authorization=helper.basic("admin", self.admin_password))
        context["test"].assertEqual(status, 200); project = loads_strict_json(raw)
        ca = context["root"] / "ca.crt"
        self.robot_plan = {"schema_version": 1, "server": {"origin": "https://192.0.2.91:8443",
            "harbor_version": "v2.15.1-poolhealth.local-335c05f2-poolhealth", "ca_file": str(ca),
            "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(), "leaf_sha256": core.leaf,
            "admin_username": "admin", "password_env": "HARBOR_SYNTHETIC_OPERATOR_PASSWORD"},
            "project": {"name": "fixture", "id": project["project_id"]},
            "robot": {"name": "ci-publisher", "username": "robot$fixture+ci-publisher", "duration_days": 7}}
        client = helper.Client(self.robot_plan, password=self.admin_password)
        self.robot_directory = context["root"] / "robot-private"
        before = helper.operate(self.robot_plan, self.robot_directory, mode="preview", client=client)
        context["test"].assertTrue(before["read_only"]); context["test"].assertFalse(self.robot_directory.exists())
        result = helper.operate(self.robot_plan, self.robot_directory, mode="apply", client=client)
        context["test"].assertTrue(result["permissions_verified"])
        self.robot_credential = helper.load(self.robot_directory / "credential.json")
        context["private_values"].append(self.robot_credential["secret"])
        self.robot_credential_sha = hashlib.sha256((self.robot_directory / "credential.json").read_bytes()).hexdigest()
        self.robot_id = self.robot_credential["robot_id"]; self.robot_witness = None; self.robot_phases = set()
        context["test"].assertEqual(self.sql("SELECT count(*) FROM robot WHERE id=" + str(self.robot_id) + ";", "registry"), "1")
        bad = {**self.robot_plan, "server": {**self.robot_plan["server"], "leaf_sha256": "0" * 64}}
        with context["test"].assertRaises(ValueError): helper.Client(bad, password=self.admin_password).ready()
        with context["test"].assertRaises(ValueError): helper.operate(self.robot_plan, self.robot_directory.parent / "robot-collision", mode="apply", client=client)
        context["test"].assertFalse((self.robot_directory.parent / "robot-collision").exists())

    def jwt_workload_completed(self, context, phase):
        test = context["test"]; core = self.service_clients["harbor-core"]
        client = helper.Client(self.robot_plan, password=self.admin_password)
        result = helper.operate(self.robot_plan, self.robot_directory, mode="resume", client=client)
        test.assertTrue(result["server_read_only"]); test.assertTrue(result["credential_authentication_verified"])
        test.assertEqual(hashlib.sha256((self.robot_directory / "credential.json").read_bytes()).hexdigest(), self.robot_credential_sha)
        test.assertEqual(self.sql("SELECT count(*) FROM robot WHERE id=" + str(self.robot_id) + ";", "registry"), "1")
        auth = helper.basic(self.robot_credential["username"], self.robot_credential["secret"])
        anonymous = CoreRegistry(core)
        query = urlencode({"service": "harbor-registry", "scope": "repository:fixture/ha:pull,push,delete"})
        status, _, raw = anonymous._request("GET", "/service/token?" + query, authorization=auth)
        test.assertEqual(status, 200); token = loads_strict_json(raw)["token"]; context["private_values"].append(token)
        _, claims = token_parts(token); test.assertEqual(claims["sub"], self.robot_credential["username"])
        test.assertEqual(len(claims["access"]), 1); test.assertEqual(claims["access"][0]["name"], "fixture/ha")
        test.assertEqual(set(claims["access"][0]["actions"]), {"pull", "push"})
        writer = CoreRegistry(core, "Bearer " + token)
        class UploadClient:
            def request(self, *args, **kwargs):
                status, headers, raw = writer.request(*args, **kwargs)
                if args[0] in {"POST", "PATCH"} and "location" in headers: headers["location"] = original_location(headers["location"])
                return status, headers, raw
        tag = "robot-" + phase; parts = publish(UploadClient(), UploadClient(), tag); retrieve(writer, tag, parts)
        test.assertIn(writer.request("DELETE", REPOSITORY + "/manifests/" + digest(parts[2]))[0], (401, 403))
        test.assertIn(core.request("POST", "/api/v2.0/projects", {"project_name": "robot-escalation", "public": False}, auth)[0], (401, 403))
        denied_query = urlencode({"service": "harbor-registry", "scope": "repository:other-fixture/ha:pull,push,delete"})
        status, _, raw = anonymous._request("GET", "/service/token?" + denied_query, authorization=auth)
        test.assertEqual(status, 200); denied = loads_strict_json(raw)["token"]; context["private_values"].append(denied)
        _, denied_claims = token_parts(denied)
        test.assertTrue(all(not row.get("actions") for row in denied_claims.get("access", [])))
        test.assertIn(CoreRegistry(core, "Bearer " + denied).request("GET", REPOSITORY + "/manifests/" + tag)[0], (401, 403))
        test.assertIn(anonymous._request("GET", "/v2/other-fixture/ha/manifests/" + tag, authorization="Bearer " + denied)[0], (401, 403))
        test.assertIn(anonymous._request("POST", "/v2/other-fixture/ha/blobs/uploads/", authorization="Bearer " + denied)[0], (401, 403))
        test.assertIn(anonymous._request("POST", "/v2/other-fixture/ha/blobs/uploads/", authorization="Bearer " + token)[0], (401, 403))
        if self.robot_witness:
            previous, previous_tag, previous_parts = self.robot_witness
            retrieve(CoreRegistry(core, "Bearer " + previous), previous_tag, previous_parts)
        else: self.robot_witness = (token, tag, parts)
        self.robot_phases.add(phase)

    def exercise(self, context):
        result = super().exercise(context)
        if self.robot_phases != self.expected_jwt_phases: raise ValueError("Require restricted robot work in all original failure phases")
        result.update(valkey_gateway_harbor_robot_create_once_resume_verified=True,
            valkey_gateway_harbor_robot_project_push_pull_verified=True,
            valkey_gateway_harbor_robot_delete_and_admin_refusal_verified=True,
            valkey_gateway_harbor_robot_cross_project_refusal_verified=True,
            valkey_gateway_harbor_robot_original_credential_after_promotions_verified=True,
            valkey_gateway_harbor_ci_worker_publication_verified=False)
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(RobotChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
