#!/usr/bin/env python3
"""Static publisher rejection contracts; native registry/worker coverage is opt-in."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import time
import unittest
from unittest import mock

import publish_harbor_static_image as publisher
from render_harbor_static_task import task_spec


def plan_fixture(root):
    credential = {"schema_version": 1, "operation_id": "a" * 32, "plan_sha256": "b" * 64,
        "origin": "https://registry.example.invalid", "project_id": 2, "robot_id": 3,
        "username": "robot$fixture+publisher", "secret": "synthetic-private-value-1234567890", "expires_at": int(time.time()) + 3600}
    ca = root / "ca.crt"; ca.write_bytes(b"synthetic-public-ca")
    path = root / "credential.json"; path.write_bytes(publisher.canonical(credential))
    return {"schema_version": 1, "origin": credential["origin"], "ca_file": str(ca), "credential_file": str(path),
        "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(), "credential_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "leaf_sha256": "c" * 64, "project_id": 2, "robot_id": 3, "repository": "fixture/ha", "base_digest": "sha256:" + "d" * 64,
        "revision": "e" * 40, "files": ["index.html"]}, credential


class StaticPublisherTests(unittest.TestCase):
    def test_plan_rejects_unbounded_origin_scope_revision_and_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, _ = plan_fixture(Path(temporary))
            publisher.validate(plan)
            for key, value in (("origin", "http://registry.example.invalid"), ("origin", "https://user:secret@registry.example.invalid"),
                    ("origin", "https://registry.example.invalid/path"), ("repository", "fixture/../other"), ("base_digest", "latest"),
                    ("revision", "main"), ("project_id", True), ("files", ["../index.html"]), ("files", ["index.html", "index.html"]),
                    ("files", [".env", "index.html"]), ("files", ["index.html", "sub/../../secret"])):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError): publisher.validate({**plan, key: value})

    def test_robot_handoff_refuses_admin_wrong_project_expiry_and_changed_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, credential = plan_fixture(Path(temporary))
            publisher.private_inputs(plan)
            for key, value in (("username", "admin"), ("username", "robot$other+publisher"), ("project_id", True),
                    ("project_id", 4), ("robot_id", 6), ("expires_at", int(time.time()) - 1), ("origin", "https://other.example.invalid")):
                changed = {**credential, key: value}; raw = publisher.canonical(changed); Path(plan["credential_file"]).write_bytes(raw)
                reviewed = {**plan, "credential_sha256": hashlib.sha256(raw).hexdigest()}
                with self.subTest(key=key), self.assertRaises(ValueError): publisher.private_inputs(reviewed)
            with self.assertRaises(ValueError): publisher.private_inputs(plan)

    @unittest.skipUnless(os.name == "posix", "Kubernetes AtomicWriter input links")
    def test_hash_pinned_secret_and_ca_allow_kubernetes_input_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan, _ = plan_fixture(root)
            for key in ("ca_file", "credential_file"):
                original = Path(plan[key]); target = original.with_name(original.name + ".data"); original.rename(target); original.symlink_to(target.name)
            publisher.private_inputs(plan)
            Path(plan["credential_file"]).write_bytes(b"substituted-private-input")
            with self.assertRaises(ValueError): publisher.private_inputs(plan)

    @unittest.skipUnless(os.name == "posix", "Linux no-follow snapshot")
    def test_snapshot_is_reproducible_and_rejects_symlinks_hardlinks_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); output = root / "output"; output.mkdir(); source = output / "index.html"; source.write_bytes(b"reviewed static output")
            first = publisher.output_layer(output, ["index.html"], "not-in-public-output")
            self.assertEqual(first, publisher.output_layer(output, ["index.html"], "not-in-public-output"))
            with tarfile.open(fileobj=io.BytesIO(first[0])) as archive:
                self.assertEqual(archive.getnames(), ["www", "www/index.html"])
                self.assertEqual(archive.extractfile("www/index.html").read(), source.read_bytes())
            with self.assertRaises(ValueError): publisher.output_layer(output, ["index.html"], "reviewed")
            os.link(source, output / "linked")
            with self.assertRaises(ValueError): publisher.output_layer(output, ["index.html"], "absent")
            (output / "linked").unlink(); source.unlink(); source.symlink_to(root / "private")
            with self.assertRaises(OSError): publisher.output_layer(output, ["index.html"], "absent")
            source.unlink(); (root / "private").mkdir(); (output / "sub").symlink_to(root / "private", target_is_directory=True)
            with self.assertRaises(OSError): publisher.output_layer(output, ["sub/file"], "absent")

    def test_base_config_and_layers_are_digest_bound_and_foreign_layers_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, _ = plan_fixture(Path(temporary)); raw = b"base-tar"; config = publisher.canonical({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": [publisher.digest(raw)]}})
            base = {"schemaVersion": 2, "mediaType": publisher.MEDIA, "config": publisher.descriptor(config, publisher.CONFIG), "layers": [publisher.descriptor(raw, publisher.LAYER)]}
            args = (plan, publisher.canonical(base), config, [raw], b"new-layer", "f" * 64)
            config_raw, manifest = publisher.assemble(*args)
            self.assertEqual(json.loads(config_raw)["config"]["User"], "1000:1000")
            self.assertEqual(json.loads(manifest)["layers"][-1]["digest"], publisher.digest(b"new-layer"))
            for change in ("foreign", "digest", "size"):
                changed = copy.deepcopy(base)
                if change == "foreign": changed["layers"][0]["urls"] = ["https://other.example.invalid/blob"]
                if change == "digest": changed["layers"][0]["digest"] = "sha256:" + "0" * 64
                if change == "size": changed["config"]["size"] += 1
                with self.subTest(change=change), self.assertRaises(ValueError): publisher.assemble(plan, publisher.canonical(changed), config, [raw], b"new", "f" * 64)

    def test_bearer_authentication_requires_exact_selected_robot_repository_grants(self):
        import base64
        registry = object.__new__(publisher.Registry)
        registry.plan = {"repository": "fixture/ha"}; registry.credential = {"username": "robot$fixture+publisher"}
        encode = lambda value: base64.urlsafe_b64encode(publisher.canonical(value)).rstrip(b"=").decode()
        claims = {"sub": registry.credential["username"], "aud": "harbor-registry", "exp": int(time.time()) + 60,
            "access": [{"type": "repository", "name": "fixture/ha", "actions": ["push", "pull"]}]}
        cases = [claims, {**claims, "sub": "admin"}, {**claims, "access": [{"type": "repository", "name": "fixture/ha", "actions": ["pull", "push", "delete"]}]},
            {**claims, "access": [{"type": "repository", "name": "other/ha", "actions": ["pull", "push"]}]}, {**claims, "exp": int(time.time()) - 1}]
        for index, value in enumerate(cases):
            token = encode({"alg": "RS256"}) + "." + encode(value) + ".synthetic-signature"
            with mock.patch.object(registry, "request", return_value=(200, {}, publisher.canonical({"token": token}))):
                if index == 0: registry.authenticate()
                else:
                    with self.subTest(index=index), self.assertRaises(ValueError): registry.authenticate()

    def test_upload_state_refuses_redirects_other_repositories_and_extra_queries(self):
        registry = object.__new__(publisher.Registry); registry.prefix = "/v2/fixture/ha"; registry.plan = {"origin": "https://registry.example.invalid"}
        path = registry.prefix + "/blobs/uploads/" + "a" * 8 + "-" + "b" * 27 + "?_state=original"
        self.assertEqual(registry.location(path), path)
        for value in ("https://attacker.example.invalid" + path, path.replace("fixture/ha", "other/ha"), path + "&digest=bad", path + "#fragment", path + "&_state=duplicate"):
            with self.subTest(value=value), self.assertRaises(ValueError): registry.location(value)

    def test_build_has_no_robot_program_input_or_service_account_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, _ = plan_fixture(Path(temporary)); plan.update(ca_file="/publisher-input/ca.crt", credential_file="/run/robot/credential.json")
            image = "registry.example.invalid/python@sha256:" + "f" * 64
            task = task_spec(plan, build_image=image, publisher_image=image, build_argv=["python3", "-c", "pass"], program_map="program", input_map="input", robot_secret="robot")
            self.assertEqual({m["name"] for m in task["steps"][0]["volumeMounts"]}, {"output", "build-tmp"})
            self.assertTrue(next(m for m in task["steps"][1]["volumeMounts"] if m["name"] == "output")["readOnly"])
            self.assertNotIn("hostPath", json.dumps(task)); self.assertNotIn("secret", json.dumps(task["steps"][0]))
            for step in task["steps"]: self.assertFalse(step["securityContext"]["allowPrivilegeEscalation"])

    @unittest.skipUnless(os.name == "posix", "Composed native runtime fixture")
    def test_worker_aggregate_keeps_details_in_hash_bound_private_receipt(self):
        from test_harbor_worker_fixture import aggregate_worker_report
        report = {"actual_ci_worker_registry_publication_verified": True, "full_stack_verified": False,
            "pod_uid": "private-detail", "memory": {"peak": 100}, "events": ["detail"]}
        raw = publisher.canonical(report); result = aggregate_worker_report(report, raw)
        self.assertTrue(result["valkey_gateway_harbor_actual_ci_worker_registry_publication_verified"])
        self.assertFalse(result["valkey_gateway_harbor_full_stack_verified"])
        self.assertEqual(result["valkey_gateway_harbor_worker_receipt_sha256"], {"receipt": hashlib.sha256(raw).hexdigest()})
        self.assertNotIn("private-detail", json.dumps(result))
        self.assertTrue(all(type(v) is bool or isinstance(v, dict) and all(publisher.hex64(h) for h in v.values()) for v in result.values()))
        with self.assertRaises(ValueError): aggregate_worker_report(report, publisher.canonical({**report, "full_stack_verified": True}))

    @unittest.skipUnless(os.name == "posix", "Composed native runtime fixture")
    def test_http_readiness_never_accepts_wrong_serving_content(self):
        from test_harbor_worker_fixture import BODY, http_ready
        response = mock.Mock(status=503)
        connection = mock.Mock(); connection.getresponse.return_value = response
        with mock.patch("test_harbor_worker_fixture.http.client.HTTPConnection", return_value=connection):
            self.assertFalse(http_ready(self, "192.0.2.3", 30080)); connection.close.assert_called_once()
            response.status = 200; response.read.return_value = b"wrong image content"
            with self.assertRaises(AssertionError): http_ready(self, "192.0.2.3", 30080)
            response.read.return_value = BODY; self.assertTrue(http_ready(self, "192.0.2.3", 30080))
            connection.request.side_effect = ConnectionRefusedError()
            with self.assertRaises(ConnectionRefusedError): http_ready(self, "192.0.2.3", 30080)

    def test_cli_failure_does_not_print_private_exception(self):
        with mock.patch.object(publisher, "read_bounded_bytes", side_effect=ValueError("secret-do-not-print")), mock.patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(publisher.main(["--plan", "/missing", "--output", "/missing"]), 1)
            self.assertNotIn("secret-do-not-print", error.getvalue())


if __name__ == "__main__": unittest.main()
