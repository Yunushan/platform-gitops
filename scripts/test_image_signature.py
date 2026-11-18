#!/usr/bin/env python3
"""Signature verification policy boundaries; actual crypto is separately gated."""

import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import verify_image_signature as verifier


IMAGE = "registry.example.invalid/project/image@sha256:" + "a" * 64


def jwt(repository="project/image", actions=None, expiry=None):
    header = {"alg": "RS256"}
    claims = {"aud": "harbor-registry", "exp": int(time.time()) + 900 if expiry is None else expiry,
        "access": [{"type": "repository", "name": repository, "actions": ["pull"] if actions is None else actions}]}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return encode(header) + "." + encode(claims) + ".synthetic"


def payload(image=IMAGE):
    return [{"critical": {"identity": {"docker-reference": image},
        "image": {"docker-manifest-digest": image.rsplit("@", 1)[1]}, "type": verifier.PREDICATE}, "optional": {}}]


def fixture(root):
    values = {"cosign_binary": b"synthetic reviewed executable", "registry_ca": b"-----BEGIN CERTIFICATE-----\nsynthetic",
        "public_key": b"-----BEGIN PUBLIC KEY-----\nsynthetic\n-----END PUBLIC KEY-----\n",
        "trusted_root": json.dumps({"mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1",
            "tlogs": [{"baseUrl": "https://log.example.invalid", "publicKey": {"rawBytes": "synthetic"}}]}).encode(),
        "credential": json.dumps({"registry": "registry.example.invalid", "repository": "project/image", "token": jwt()}).encode()}
    plan = {"schema_version": 1, "image": IMAGE}
    for name, raw in values.items():
        path = root / name; path.write_bytes(raw)
        plan[name] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    return plan


class SignatureTests(unittest.TestCase):
    def test_mutable_images_broad_credentials_wrong_tool_and_unknown_controls_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = fixture(Path(directory))
            with mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]):
                verifier.inspect(plan)
                mutations = [lambda p: p.update(image="registry.example.invalid/project/image:latest"),
                    lambda p: p.update(image="https://" + IMAGE), lambda p: p.update(image=IMAGE + "?" + "token" + "=" + "forbidden"),
                    lambda p: p.update(skip_tlog=True), lambda p: p["cosign_binary"].update(sha256="0" * 64),
                    lambda p: p["credential"].update(path=p["public_key"]["path"])]
                for change in mutations:
                    item = copy.deepcopy(plan); change(item)
                    with self.assertRaises(ValueError): verifier.inspect(item)

    def test_pull_scope_lifetime_and_ambiguous_json_refuse(self):
        for token in (jwt(actions=["pull", "push"]), jwt(repository="another/image"), jwt(expiry=1)):
            raw = json.dumps({"registry": "registry.example.invalid", "repository": "project/image", "token": token})
            with self.assertRaises(ValueError): verifier.pull_token(raw, "registry.example.invalid", "project/image")
        with self.assertRaises(ValueError):
            verifier.pull_token('{"registry":"a","registry":"b"}', "registry.example.invalid", "project/image")

    def test_changed_tool_key_ca_or_credential_refuses_before_execution(self):
        for name in ("cosign_binary", "registry_ca", "public_key", "trusted_root", "credential"):
            with tempfile.TemporaryDirectory() as directory:
                plan = fixture(Path(directory))
                Path(plan[name]["path"]).write_bytes(b"changed")
                with mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]), mock.patch.object(verifier, "run_bounded") as runner:
                    with self.assertRaises(ValueError): verifier.inspect(plan)
                    runner.assert_not_called()

    def test_log_trust_must_be_explicit_https_and_nonempty(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = fixture(Path(directory))
            for logs in ([], [{"baseUrl": "http://log.example.invalid", "publicKey": {}}],
                    [{"baseUrl": "https://user:pass@log.example.invalid", "publicKey": {}}]):
                path = Path(plan["trusted_root"]["path"])
                path.write_text(json.dumps({"mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1", "tlogs": logs}))
                plan["trusted_root"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                with mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]):
                    with self.assertRaises(ValueError): verifier.inspect(plan)

    def test_output_must_bind_predicate_reference_and_manifest_digest(self):
        self.assertEqual(verifier.checked_output(json.dumps(payload()), IMAGE), payload())
        cases = [[], payload(IMAGE.replace("project/image", "another/image")), [{"critical": {}}]]
        changed = payload(); changed[0]["critical"]["image"]["docker-manifest-digest"] = "sha256:" + "b" * 64
        cases.append(changed)
        changed = payload(); changed[0]["critical"]["type"] = "another-attestation"; cases.append(changed)
        for rows in cases:
            with self.assertRaises(ValueError): verifier.checked_output(json.dumps(rows), IMAGE)
        with self.assertRaises(ValueError): verifier.checked_output('[{"critical":{},"critical":{}}]', IMAGE)

    def test_read_only_execution_freezes_inputs_and_strips_unreviewed_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = fixture(Path(directory))
            def run(args, **kwargs):
                self.assertIn("--insecure-ignore-tlog=false", args)
                self.assertIn("--insecure-ignore-sct=false", args)
                self.assertNotIn("--registry-password", args)
                self.assertEqual(args[-1], IMAGE)
                self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "TMPDIR", "DOCKER_CONFIG", "LANG", "LC_ALL"})
                frozen = Path(kwargs["env"]["DOCKER_CONFIG"])
                config = json.loads((frozen / "config.json").read_bytes())
                self.assertEqual(set(config["auths"]), {"registry.example.invalid"})
                self.assertEqual(set(config["auths"]["registry.example.invalid"]), {"registrytoken"})
                self.assertEqual((frozen / "public_key").read_bytes(), Path(plan["public_key"]["path"]).read_bytes())
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload()), stderr="")
            with mock.patch.object(verifier, "require_nonroot"):
                with mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]), mock.patch.object(verifier, "run_bounded", side_effect=run):
                    result = verifier.operate(plan)
            self.assertTrue(result["transparency_inclusion_verified"])
            for name in ("admission_verified", "ci_evidence_verified", "release_authorization_verified", "ha_verified", "full_stack_verified"):
                self.assertIs(result[name], False)

    def test_failure_is_never_retried_or_replaced_by_a_positive_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = fixture(Path(directory))
            with mock.patch.object(verifier, "require_nonroot"):
                with mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]), mock.patch.object(verifier, "run_bounded", return_value=SimpleNamespace(returncode=1, stdout="", stderr="private-response")) as runner:
                    with self.assertRaisesRegex(ValueError, "refused"): verifier.operate(plan)
                    runner.assert_called_once()

    def test_signed_annotations_require_explicit_version_and_unambiguous_values(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = fixture(Path(directory))
            annotation = {"platform.gitops/build-evidence-sha256": "a" * 64}
            plan.update(schema_version=2, annotations=annotation)
            with mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]): verifier.inspect(plan)
            for value in ({}, {"bad key": "value"}, {"key": "a,b=other"}, {"key": "line\nnext"},
                    {"key": "a=b"}, {"key": True}, {"key": "x" * 2049}):
                with self.subTest(value=value), self.assertRaises(ValueError): verifier.validate_annotations(value)
            plan["schema_version"] = 1
            with self.assertRaises(ValueError): verifier.validate(plan)

    def test_annotations_are_enforced_by_tool_and_verified_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = fixture(Path(directory)); annotations = {"platform.gitops/build-evidence-sha256": "a" * 64}
            plan.update(schema_version=2, annotations=annotations)
            rows = payload(); rows[0]["optional"] = {**annotations, "unrelated": "allowed"}
            def run(arguments, **kwargs):
                position = arguments.index("--annotations")
                self.assertEqual(arguments[position + 1], "platform.gitops/build-evidence-sha256=" + "a" * 64)
                return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
            with mock.patch.object(verifier, "require_nonroot"), mock.patch.object(verifier, "COSIGN_SHA256", plan["cosign_binary"]["sha256"]), mock.patch.object(verifier, "run_bounded", side_effect=run):
                proof = verifier.operate(plan)
                self.assertIs(proof["signed_annotations_verified"], True)
                self.assertEqual(proof["signed_annotations_sha256"], hashlib.sha256(verifier.canonical(annotations)).hexdigest())
                self.assertIs(proof["ci_evidence_verified"], False)
                rows[0]["optional"] = {}
                with self.assertRaisesRegex(ValueError, "annotations"): verifier.operate(plan)
            rows[0]["optional"] = {"platform.gitops/build-evidence-sha256": "b" * 64}
            with self.assertRaises(ValueError): verifier.checked_output(json.dumps(rows), IMAGE, annotations)

    def test_cli_diagnostics_never_print_private_paths_or_tool_output(self):
        with mock.patch.object(verifier, "read_bootstrap_bytes", side_effect=ValueError("private-input")), mock.patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(verifier.main(["--plan", "/unavailable"]), 1)
            self.assertNotIn("private-input", output.getvalue())


if __name__ == "__main__": unittest.main()
