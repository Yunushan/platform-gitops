#!/usr/bin/env python3
"""Exercise authenticated push binding, retry receipts and credential isolation."""

import copy
import base64
from email.message import Message
import hashlib
import hmac
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml

from bounded_subprocess import run_bounded
import render_tekton_forge_bridge as renderer
import tekton_checkout as checkout
import tekton_forge_bridge as bridge
from test_tekton import declared_fields


def fixture():
    return {"schema_version": 1, "name": "forge-push", "provider": "forgejo",
        "source": {"repository_id": 123, "repository": "team/application", "branch": "main", "read_username": "ci-read",
                   "api_url": "https://forge.example.com/api/v1", "clone_url": "https://forge.example.com/team/application.git"},
        "management_target": {"kubeconfig_env": "PLATFORM_CI_KUBECONFIG", "context": "ci", "cluster_uid": "11111111-1111-4111-8111-111111111111"},
        "namespace": "platform-ci-jobs", "service_account": "application-build", "submitter_account": "forge-event-bridge",
        "submitter_namespace": "platform-ci-control", "node_selector": {"kubernetes.io/os": "linux", "platform.gitops/ci-worker": "true"},
        "controller_selector": {"kubernetes.io/os": "linux", "platform.gitops/control-service": "true"},
        "checkout_image": "registry.example.com/git-python@sha256:" + "a" * 64,
        "build_image": "registry.example.com/python@sha256:" + "b" * 64,
        "bridge_image": "registry.example.com/python-kubectl@sha256:" + "c" * 64,
        "build_argv": ["python3", "-m", "unittest", "discover"], "read_secret": "application-read", "timeout_minutes": 10,
        "publish_address": "127.0.0.1", "bridge_secret": "forge-event-inputs"}


def event(plan=None, **changes):
    plan = plan or fixture()
    body = bridge.canonical({"repository": {"id": plan["source"]["repository_id"], "full_name": plan["source"]["repository"]},
                            "ref": "refs/heads/main", "after": "a" * 40, **changes})
    headers = Message()
    prefix = "X-Forgejo-" if plan["provider"] == "forgejo" else "X-Gitea-"
    headers[prefix + "Event"] = "push"
    headers[prefix + "Signature"] = hmac.new(b"x" * 32, body, hashlib.sha256).hexdigest()
    return headers, body


def package(root, runtime="rke2"):
    files = renderer.render(fixture(), runtime)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8", newline="\n")
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    contract, template = bridge.load_bundle(root, digest)
    return contract, template, digest


class FakeClient:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_run = False
        self.accept_then_fail = False
        self.race_lock = False

    def get(self, resource, namespace, *, optional=False):
        self.calls.append(("get", resource))
        return copy.deepcopy(self.objects.get(resource))

    def call(self, args, *, payload=None, seconds=30):
        obj = json.loads(payload)
        kind = "pipelinerun.tekton.dev" if obj["kind"] == "PipelineRun" else "configmap"
        key = kind + "/" + obj["metadata"]["name"]
        self.calls.append(("create", key))
        if key in self.objects:
            raise ValueError("Already exists")
        if self.fail_run and kind.startswith("pipelinerun"):
            raise ValueError("Ambiguous or rejected create")
        obj["metadata"]["uid"] = "22222222-2222-4222-8222-222222222222"
        self.objects[key] = obj
        if self.accept_then_fail and kind.startswith("pipelinerun") or self.race_lock and key.endswith("-lock"):
            raise ValueError("Connection lost after server accepted request")
        return json.dumps(obj)


class Socket:
    def __init__(self, request):
        self.input, self.output = io.BytesIO(request), bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, value):
        self.output.extend(value)


class BridgeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("FORGE_BRIDGE_API_FIXTURE") == "1", "isolated Kubernetes API fixture not requested")
    def test_actual_api_deduplicates_after_lost_run_acknowledgement(self):
        import test_rook_api_fixture as api_fixture
        from test_argocd_identity_api_fixture import await_crd_conditions
        from test_tekton_gerrit_api_fixture import CRD_SHA
        from kubernetes_management import Client
        path = Path(os.environ["TEKTON_GERRIT_TEST_CRD"])
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CRD_SHA)
        definition = yaml.safe_load(raw)
        definition["spec"]["versions"] = [v for v in definition["spec"]["versions"] if v["name"] == "v1"]
        definition["spec"]["versions"][0]["storage"] = True
        definition["spec"]["conversion"] = {"strategy": "None"}
        def check(admin, root, kubectl):
            crd = kubectl(["create", "-f", "-", "-o", "json"], definition)
            await_crd_conditions(admin, crd)
            admin.call(["wait", "--for=condition=Established", "crd/" + crd["metadata"]["name"], "--timeout=30s"])
            plan = fixture()
            plan["management_target"] = {"kubeconfig_env": "DISPOSABLE_FORGE_CI", "context": "storage", "cluster_uid": admin.get("ns/kube-system")["metadata"]["uid"]}
            for name in (plan["namespace"], plan["submitter_namespace"]):
                kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}})
            for obj in renderer.rbac(plan): kubectl(["create", "-f", "-", "-o", "json"], obj)
            token = admin.call(["create", "token", plan["submitter_account"], "--namespace", plan["submitter_namespace"], "--duration=10m"]).strip()
            config = json.loads(admin.call(["config", "view", "--minify", "--flatten", "--raw", "-o", "json"]))
            config["users"][0]["user"] = {"token": token}
            config_path = root / "forge-ci-kubeconfig.json"; config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
            bundle = root / "forge-bridge-bundle"; bundle.mkdir(mode=0o700)
            files = renderer.render(plan, "k8s")
            for name, content in files.items(): (bundle / name).write_text(content, encoding="utf-8")
            digest = json.loads(files["render-status.json"])["contract_sha256"]
            contract, template = bridge.load_bundle(bundle, digest)
            class LostAck:
                def __init__(self, client): self.client = client; self.writes = 0
                def get(self, *args, **kwargs): return self.client.get(*args, **kwargs)
                def call(self, args, **kwargs):
                    result = self.client.call(args, **kwargs)
                    if args[:1] == ["create"] and json.loads(kwargs["payload"])["kind"] == "PipelineRun":
                        self.writes += 1
                        if self.writes == 1: raise ValueError("fixture lost acknowledgement")
                    return result
            with mock.patch.dict(os.environ, {"DISPOSABLE_FORGE_CI": str(config_path)}):
                with Client(plan["management_target"]).frozen() as scoped, mock.patch.object(bridge, "verify_head"):
                    client = LostAck(scoped)
                    self.assertTrue(bridge.submit(client, contract, template, digest, "a" * 40)["accepted"])
                    self.assertTrue(bridge.submit(client, contract, template, digest, "a" * 40)["duplicate"])
                    self.assertEqual(client.writes, 1)
                    with self.assertRaises(ValueError): scoped.call(["get", "secrets", "--namespace", plan["namespace"], "-o", "json"])
                    run, data = bridge.event_run(contract, template, digest, "a" * 40)
                    admin.call(["delete", "pipelinerun.tekton.dev/" + data["run"], "--namespace", plan["namespace"]])
                    self.assertFalse(bridge.submit(client, contract, template, digest, "a" * 40)["run_retained"])
                    self.assertEqual(client.writes, 1)
        api_fixture.exercise(self, management_action=check)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("FORGE_BRIDGE_API_FIXTURE") == "1", "isolated Kubernetes API fixture not requested")
    def test_receiver_pod_admitted_in_restricted_namespace(self):
        import test_rook_api_fixture as api_fixture
        objects = list(yaml.safe_load_all(renderer.render(fixture(), "k8s")["bridge-workload.yaml"]))
        deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
        template = copy.deepcopy(deployment["spec"]["template"])
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {**template["metadata"], "name": "fixture-receiver",
               "namespace": fixture()["submitter_namespace"]}, "spec": template["spec"]}
        def check(_client, _root, kubectl):
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": pod["metadata"]["namespace"], "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
            kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
                "name": fixture()["submitter_account"], "namespace": pod["metadata"]["namespace"]}})
            admitted = kubectl(["create", "--dry-run=server", "-f", "-", "-o", "json"], pod)
            self.assertEqual(admitted["spec"]["containers"][0]["readinessProbe"]["httpGet"],
                             {"scheme": "HTTPS", "path": "/healthz", "port": "https"})
            self.assertFalse(admitted["spec"]["automountServiceAccountToken"])
        api_fixture.exercise(self, management_action=check)

    def test_invalid_source_origin_dynamic_command_and_worker_placement_are_rejected(self):
        for field, value in (("build_argv", ["echo", "$(params.unreviewed)"]), ("namespace", "default"),
                             ("submitter_namespace", "kube-system"), ("checkout_image", "image:latest"),
                             ("node_selector", {"kubernetes.io/os": "linux", "invalid/prefix/key": "true"})):
            plan = fixture()
            plan[field] = value
            with self.assertRaises(ValueError):
                renderer.render(plan, "docker")
        plan = fixture()
        plan["source"]["clone_url"] = "https://other.example.com/team/application.git"
        with self.assertRaises(ValueError):
            renderer.render(plan, "raw")

    def test_exact_raw_signature_and_provider_headers_before_json(self):
        for provider in ("forgejo", "gitea"):
            plan = fixture()
            plan["provider"] = provider
            headers, body = event(plan)
            self.assertEqual(bridge.authenticate(plan, headers, body, b"x" * 32), "a" * 40)
            for changed in (body + b" ", b"not JSON"):
                with self.assertRaises(ValueError):
                    bridge.authenticate(plan, headers, changed, b"x" * 32)
            headers["X-" + provider.title() + "-Signature"] = headers["X-" + provider.title() + "-Signature"]
            with self.assertRaises(ValueError):
                bridge.authenticate(plan, headers, body, b"x" * 32)

    def test_signed_wrong_repositories_refs_deletions_and_ambiguous_json_fail(self):
        for changes in ({"repository": {"id": 124, "full_name": "team/application"}}, {"repository": {"id": 123, "full_name": "other/application"}},
                        {"ref": "refs/tags/main"}, {"ref": "refs/heads/other"}, {"after": "0" * 40}, {"after": "$(bad)"}):
            headers, body = event(**changes)
            with self.assertRaises(ValueError):
                bridge.authenticate(fixture(), headers, body, b"x" * 32)
        headers, _ = event()
        body = b'{"after":"a","after":"b"}'
        headers.replace_header("X-Forgejo-Signature", hmac.new(b"x" * 32, body, hashlib.sha256).hexdigest())
        with self.assertRaises(ValueError):
            bridge.authenticate(fixture(), headers, body, b"x" * 32)

    def test_real_source_identity_and_branch_head_must_match(self):
        plan = fixture()
        with mock.patch.object(bridge, "api_get", side_effect=[{"id": 123, "full_name": "team/application"}, {"commit": {"id": "a" * 40}}]) as api:
            bridge.verify_head(plan, "a" * 40)
        self.assertEqual(api.call_args_list[-1].args[1], "/repos/team/application/branches/main")
        with mock.patch.object(bridge, "api_get", side_effect=[{"id": 123, "full_name": "team/application"}, {"commit": {"id": "b" * 40}}]), self.assertRaises(ValueError):
            bridge.verify_head(plan, "a" * 40)

    def test_retry_and_run_gc_keep_durable_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest = package(Path(directory))
            client = FakeClient()
            with mock.patch.object(bridge, "verify_head") as head:
                first = bridge.submit(client, contract, template, digest, "a" * 40)
                second = bridge.submit(client, contract, template, digest, "a" * 40)
                key = next(key for key in client.objects if key.startswith("pipelinerun"))
                del client.objects[key]
                third = bridge.submit(client, contract, template, digest, "a" * 40)
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertTrue(third["duplicate"])
            self.assertFalse(third["run_retained"])
            self.assertEqual(head.call_count, 1)
            self.assertEqual(len([x for x in client.calls if x[0] == "create" and x[1].startswith("pipelinerun")]), 1)

    def test_ambiguous_create_and_concurrent_reservation_never_recreate_jobs(self):
        for failure in ("fail_run", "race_lock", "accept_then_fail"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                contract, template, digest = package(Path(directory))
                client = FakeClient()
                setattr(client, failure, True)
                with mock.patch.object(bridge, "verify_head"):
                    if failure == "accept_then_fail":
                        self.assertTrue(bridge.submit(client, contract, template, digest, "a" * 40)["accepted"])
                    else:
                        for _ in range(2):
                            with self.assertRaises(ValueError):
                                bridge.submit(client, contract, template, digest, "a" * 40)
                attempts = [key for op, key in client.calls if op == "create" and key.startswith("pipelinerun")]
                self.assertEqual(len(attempts), 0 if failure == "race_lock" else 1)

    def test_receipts_reject_replaced_runs_and_modified_embedded_steps(self):
        for mutation in ("uid", "command", "extra-step"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                contract, template, digest = package(Path(directory))
                client = FakeClient()
                with mock.patch.object(bridge, "verify_head"):
                    bridge.submit(client, contract, template, digest, "a" * 40)
                run = next(value for key, value in client.objects.items() if key.startswith("pipelinerun"))
                if mutation == "uid": run["metadata"]["uid"] = "replaced"
                if mutation == "command": run["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]["steps"][1]["command"] = ["other"]
                if mutation == "extra-step": run["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]["steps"].append({"name": "unexpected"})
                with self.assertRaises(ValueError):
                    bridge.submit(client, contract, template, digest, "a" * 40)

    def test_templates_inline_trusted_pipeline_and_mount_read_token_only_for_checkout(self):
        for runtime in renderer.RUNTIMES:
            files = renderer.render(fixture(), runtime)
            run = json.loads(files["run-template.json"])
            self.assertNotIn("pipelineRef", run["spec"])
            steps = run["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]["steps"]
            self.assertIn("source", {v["name"] for v in steps[0]["volumeMounts"]})
            self.assertNotIn("source", {v["name"] for v in steps[1]["volumeMounts"]})
            self.assertNotIn("checkout-tmp", {v["name"] for v in steps[1]["volumeMounts"]})
            self.assertEqual(steps[1]["command"], fixture()["build_argv"])
            for step in steps:
                self.assertEqual(next(v["mountPath"] for v in step["volumeMounts"] if v["name"] == "workspace"), "/workspace/repository")
            environment = {e["name"]: e["value"] for e in steps[1]["env"]}
            self.assertEqual((environment["GIT_CONFIG_KEY_0"], environment["GIT_CONFIG_VALUE_0"]), ("safe.directory", "/workspace/repository"))
            accounts = [x for x in yaml.safe_load_all(files["bridge-rbac.yaml"]) if x["kind"] == "ServiceAccount"]
            self.assertTrue(all(x["automountServiceAccountToken"] is False for x in accounts))
            status = json.loads(files["render-status.json"])
            self.assertFalse(status["deployed"])
            self.assertFalse(status["application_ci_verified"])

    def test_inline_task_and_duration_match_real_tekton_admission_serialization(self):
        for minutes, duration in ((5, "5m0s"), (10, "10m0s"), (59, "59m0s"), (60, "1h0m0s")):
            plan = fixture(); plan["timeout_minutes"] = minutes
            run = renderer.run_template(plan, "reviewed checkout program")
            self.assertEqual(run["spec"]["timeouts"], {"pipeline": duration})
            embedded = run["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]
            self.assertEqual(embedded["metadata"], {})
            self.assertIsNone(embedded["spec"])

    @unittest.skipUnless(os.name == "posix", "Scoped Linux checkout diagnostics")
    def test_checkout_failure_reports_phase_without_private_command_or_stderr(self):
        failure = subprocess.CalledProcessError(128, ["git", "private-command-credential"], stderr=b"private-server-response")
        failure.checkout_stage = "fetch"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(os, "geteuid", return_value=1000), mock.patch.object(sys, "argv", ["checkout", "https://example.invalid/repository.git", "a" * 40, temporary, "reader"]), mock.patch.object(checkout, "checkout", side_effect=failure), mock.patch.object(sys, "stderr", io.StringIO()) as output:
            self.assertEqual(checkout.main(), 1)
            self.assertIn("[fetch]", output.getvalue())
            self.assertNotIn("private-command-credential", output.getvalue())
            self.assertNotIn("private-server-response", output.getvalue())

    def test_checkout_uses_exact_sha_isolated_auth_no_redirects_or_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def git(args, **kwargs):
                calls.append((args, kwargs))
            workspace = Path(directory) / "repository"
            workspace.mkdir()
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_SSL_NO_VERIFY": "1", "PRIVATE_TOKEN": "must-not-inherit"}):
                checkout.checkout(fixture()["source"]["clone_url"], "a" * 40, workspace, "ci-read", runner=git)
            self.assertEqual(len(calls), 5)
            fetch = next(args for args, _ in calls if "fetch" in args)
            self.assertEqual(fetch[-1], "a" * 40)
            self.assertEqual(calls[-1][1]["input"], ("verify HEAD " + "a" * 40 + "\n").encode())
            (workspace / "existing").write_text("preserve")
            with self.assertRaises(ValueError):
                checkout.checkout(fixture()["source"]["clone_url"], "a" * 40, workspace, "ci-read", runner=git)
            for args, kwargs in calls:
                self.assertNotIn("shell", kwargs)
                self.assertEqual(kwargs["timeout"], 180)
                self.assertNotIn("PRIVATE_TOKEN", kwargs["env"])
                self.assertNotIn("GIT_SSL_NO_VERIFY", kwargs["env"])
                self.assertIn("http.followRedirects=false", args)
                self.assertIn("http.sslVerify=true", args)
                self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "https")
            with self.assertRaises(ValueError):
                checkout.checkout("http://example.com/repo", "a" * 40, workspace, "ci-read", runner=git)

    def test_handler_bounds_framing_and_authenticates_before_submit(self):
        plan = fixture()
        headers, body = event(plan)
        contract = {"plan": plan}
        handler = bridge.make_handler(FakeClient(), contract, {}, "d" * 64, b"x" * 32)
        prefix = b"POST /hooks/push HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
        valid = prefix + str(headers).replace("\n", "\r\n").strip().encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        for raw, code in ((valid, 202), (valid.replace(b"application/json", b"application/x-www-form-urlencoded"), 400),
                          (valid.replace(b"Content-Length:", b"Transfer-Encoding: chunked\r\nContent-Length:"), 400),
                          (valid.replace(b"X-Forgejo-Signature:", b"Other-Signature:"), 403)):
            request = Socket(raw)
            with mock.patch.object(bridge, "submit", return_value={}) as submit:
                handler(request, ("127.0.0.1", 1), types.SimpleNamespace())
            self.assertTrue(request.output.startswith(("HTTP/1.0 " + str(code)).encode()), request.output)
            self.assertEqual(submit.call_count, 1 if code == 202 else 0)
            self.assertNotIn(body, request.output)

    def test_readiness_endpoint_does_not_submit_jobs(self):
        handler = bridge.make_handler(FakeClient(), {"plan": fixture()}, {}, "d" * 64, b"x" * 32)
        for path, status in (("/healthz", 200), ("/other", 404)):
            request = Socket(("GET " + path + " HTTP/1.1\r\nHost: test\r\n\r\n").encode())
            with mock.patch.object(bridge, "submit") as submit:
                handler(request, ("127.0.0.1", 1), types.SimpleNamespace())
            self.assertTrue(request.output.startswith(("HTTP/1.0 " + str(status)).encode()))
            submit.assert_not_called()

    @unittest.skipUnless(os.name == "posix" and os.environ.get("FORGE_TLS_FIXTURE") == "1", "isolated TLS fixture not requested")
    def test_https_probe_and_busy_receiver(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("OpenSSL required for local TLS fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cert = root / "cert.pem"; key = root / "key.pem"
            run_bounded([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                         "-subj", "/CN=local-test", "-addext", "subjectAltName=IP:127.0.0.1",
                         "-keyout", str(key), "-out", str(cert)], timeout=30, output_max_bytes=65536, check=True)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert, key)
            with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": ""}):
                trusted = ssl.create_default_context(cafile=str(cert))
            handler = bridge.make_handler(FakeClient(), {"plan": fixture()}, {}, "d" * 64, b"x" * 32)
            entered = threading.Event(); release = threading.Event(); errors = []
            def submit(*_args):
                entered.set()
                if not release.wait(10): raise RuntimeError("fixture timed out")
            with bridge.make_server(("127.0.0.1", 0), handler, tls) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                base = "https://127.0.0.1:" + str(server.server_port)
                def post():
                    headers, body = event()
                    try:
                        with urlopen(Request(base + "/hooks/push", data=body,
                                             headers={**dict(headers.items()), "Content-Type": "application/json"}),
                                     context=trusted, timeout=10) as response:
                            if response.status != 202: errors.append("unexpected submission status")
                    except Exception as error: errors.append(str(error))
                try:
                    with urlopen(base + "/healthz", context=trusted, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                    with mock.patch.object(bridge, "submit", side_effect=submit):
                        worker = threading.Thread(target=post, daemon=True); worker.start()
                        self.assertTrue(entered.wait(5))
                        with self.assertRaises((TimeoutError, OSError)):
                            urlopen(base + "/healthz", context=trusted, timeout=0.5)
                        release.set(); worker.join(timeout=5)
                        self.assertFalse(worker.is_alive()); self.assertFalse(errors)
                    with urlopen(base + "/healthz", context=trusted, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    release.set(); server.shutdown(); thread.join(timeout=5)

    def test_modified_program_or_template_stops_before_client_or_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract, template, digest = package(root)
            with mock.patch.object(bridge, "Client") as client, mock.patch.object(bridge, "HTTPServer") as server:
                self.assertEqual(bridge.main(["--bundle-dir", str(root), "--contract-sha256", digest]), 0)
                client.assert_not_called()
                server.assert_not_called()
            (root / "run-template.json").write_text("{}")
            with self.assertRaises(ValueError):
                bridge.load_bundle(root, digest)

    def test_compose_config_with_real_cli_when_available(self):
        binary = shutil.which("docker")
        if not binary:
            self.skipTest("Docker Compose CLI is unavailable")
        version = run_bounded([binary, "compose", "version"], timeout=15, text=True, output_max_bytes=65536, check=False)
        if version.returncode:
            self.skipTest("Docker Compose CLI is not usable in this environment")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package(root, "docker")
            env = {**os.environ, "PLATFORM_TEKTON_BRIDGE_BUNDLE": root.as_posix(), "PLATFORM_TEKTON_BRIDGE_INPUTS": root.as_posix()}
            result = run_bounded([binary, "compose", "-f", str(root / "compose.yaml"), "config", "--quiet"], env=env,
                                 timeout=30, text=True, output_max_bytes=65536, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "Native systemd syntax check needs Linux")
    def test_native_unit_with_systemd_analyze_when_available(self):
        binary = shutil.which("systemd-analyze")
        if not binary:
            self.skipTest("systemd-analyze is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package(root, "raw")
            result = run_bounded([binary, "verify", "--man=no", str(root / "platform-tekton-bridge.service")],
                                 timeout=30, text=True, output_max_bytes=65536, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "Receiver startup requires Linux")
    def test_service_pins_a_private_kubeconfig_snapshot_until_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract, _, digest = package(root, "raw")
            inputs = root / "inputs"
            inputs.mkdir()
            for name, value in {"webhook-secret": "x" * 32, "api-token": "y" * 32, "tls.key": "fixture",
                                "ca.crt": "fixture", "kubeconfig": "original-target"}.items():
                path = inputs / name
                path.write_text(value)
                path.chmod(0o600)
            captured = []
            configuration = {"users": [{"user": {"token": "fixture-token"}}], "clusters": [{"cluster": {"server": "https://api.example.com"}}]}
            client = mock.Mock()
            client.call.return_value = json.dumps(configuration)
            def inspect(*args):
                frozen = Path(os.environ[contract["plan"]["management_target"]["kubeconfig_env"]])
                captured.append(frozen)
                self.assertNotEqual(frozen, inputs / "kubeconfig")
                self.assertEqual(frozen.stat().st_mode & 0o777, 0o600)
                (inputs / "kubeconfig").write_text("different-target")
                self.assertEqual(json.loads(frozen.read_text()), configuration)
            with mock.patch.object(bridge, "INPUTS", inputs), mock.patch.object(os, "geteuid", return_value=1000), \
                 mock.patch.object(bridge, "Client", return_value=client), mock.patch.object(bridge, "serve_target", side_effect=inspect), mock.patch.dict(os.environ):
                bridge.serve(root, digest)
            self.assertIn("--flatten", client.call.call_args.args[0])
            self.assertEqual(len(captured), 1)
            self.assertFalse(captured[0].exists())

    def test_target_snapshot_refuses_dynamic_authentication_and_unresolved_files(self):
        client = mock.Mock()
        base = {"users": [{"user": {"token": "fixture-token"}}], "clusters": [{"cluster": {"server": "https://api.example.com"}}]}
        for user in ({"exec": {"command": "arbitrary"}}, {"tokenFile": "/other/credential"}, {"auth-provider": {"name": "external"}}):
            config = copy.deepcopy(base)
            config["users"][0]["user"] = user
            client.call.return_value = json.dumps(config)
            with self.assertRaises(ValueError):
                bridge.normalized_target(client)
        client.call.return_value = json.dumps(base)
        self.assertEqual(json.loads(bridge.normalized_target(client)), base)

    def test_real_kubectl_flattens_relative_ca_without_contacting_a_server(self):
        binary = shutil.which("kubectl")
        if not binary:
            self.skipTest("kubectl is unavailable")
        version = run_bounded([binary, "version", "--client=true", "-o", "json"], timeout=15, text=True, output_max_bytes=65536, check=False)
        if version.returncode:
            self.skipTest("kubectl client is not usable in this environment")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ca.crt").write_text("local-fixture-ca")
            config = {"apiVersion": "v1", "kind": "Config", "current-context": "ci",
                "clusters": [{"name": "fixture", "cluster": {"server": "https://api.example.invalid", "certificate-authority": "ca.crt"}}],
                "contexts": [{"name": "ci", "context": {"cluster": "fixture", "user": "fixture"}}],
                "users": [{"name": "fixture", "user": {"token": "local-fixture-token"}}]}
            path = root / "kubeconfig"
            path.write_text(json.dumps(config))
            target = fixture()["management_target"]
            with mock.patch.dict(os.environ, {target["kubeconfig_env"]: str(path)}):
                normalized = json.loads(bridge.normalized_target(bridge.Client(target)))
            cluster = normalized["clusters"][0]["cluster"]
            self.assertNotIn("certificate-authority", cluster)
            self.assertEqual(base64.b64decode(cluster["certificate-authority-data"]), b"local-fixture-ca")

    @unittest.skipUnless(os.name == "posix", "Real checkout fixture needs Linux Git/askpass paths")
    def test_real_git_https_checkout_and_application_test_on_linux(self):
        git, openssl = shutil.which("git"), shutil.which("openssl")
        if not git or not openssl:
            self.skipTest("Git and OpenSSL are required for the local TLS fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, repos, credentials = root / "source", root / "repos", root / "credentials"
            source.mkdir()
            repos.mkdir()
            credentials.mkdir()
            def run(args, **kwargs):
                return run_bounded(args, timeout=30, text=True, output_max_bytes=1024 * 1024, check=True, **kwargs)
            run([git, "init", "-q", str(source)])
            run([git, "-C", str(source), "config", "user.name", "Fixture"])
            run([git, "-C", str(source), "config", "user.email", "fixture@example.invalid"])
            (source / "test_application.py").write_text('import unittest\nclass Application(unittest.TestCase):\n    def test_result(self):\n        self.assertEqual(2 + 2, 4)\n')
            run([git, "-C", str(source), "add", "."])
            run([git, "-C", str(source), "commit", "-qm", "Fixture"])
            revision = run([git, "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
            run([git, "clone", "--bare", "--quiet", str(source), str(repos / "repository.git")])
            run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=local-test",
                 "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(root / "tls.key"), "-out", str(credentials / "ca.crt")])
            (credentials / "token").write_text("local-fixture-token")
            expected_auth = "Basic " + base64.b64encode(b"ci-read:local-fixture-token").decode()
            authenticated = []
            class GitHTTP(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def handle_git(self):
                    if self.headers.get("Authorization") != expected_auth:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    authenticated.append(True)
                    url = urlsplit(self.path)
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 1024 * 1024:
                        raise ValueError("Fixture request too large")
                    env = {**os.environ, "GIT_PROJECT_ROOT": str(repos), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": url.path,
                           "QUERY_STRING": url.query, "REQUEST_METHOD": self.command, "REMOTE_USER": "ci-read",
                           "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(size)}
                    result = run_bounded([git, "http-backend"], input=self.rfile.read(size), env=env,
                                         timeout=30, output_max_bytes=4 * 1024 * 1024, check=True)
                    headers, body = result.stdout.split(b"\r\n\r\n", 1)
                    fields = [line.decode("ascii").split(":", 1) for line in headers.split(b"\r\n")]
                    status = next((int(value.strip().split()[0]) for key, value in fields if key.lower() == "status"), 200)
                    self.send_response(status)
                    for key, value in fields:
                        if key.lower() != "status": self.send_header(key, value.strip())
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                do_GET = handle_git
                do_POST = handle_git
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(credentials / "ca.crt", root / "tls.key", password="")
            with ThreadingHTTPServer(("127.0.0.1", 0), GitHTTP) as server:
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    workspace = root / "checkout"
                    workspace.mkdir()
                    checkout.checkout("https://127.0.0.1:" + str(server.server_port) + "/repository.git", revision, workspace, "ci-read", credentials=credentials)
                    self.assertTrue(authenticated)
                    self.assertEqual(run([git, "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip(), revision)
                    self.assertNotIn("local-fixture-token", (workspace / ".git" / "config").read_text())
                    run([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(workspace)])
                finally:
                    server.shutdown()
                    thread.join(timeout=5)

    def test_optional_release_declares_pipeline_fields(self):
        path = os.environ.get("TEKTON_TEST_EXPORT")
        if not path:
            self.skipTest("Set TEKTON_TEST_EXPORT to check release CRD field compatibility")
        with Path(path).open(encoding="utf-8") as source:
            objects = list(yaml.safe_load_all(source))
        crd = next(x for x in objects if x and x["kind"] == "CustomResourceDefinition" and x["metadata"]["name"] == "pipelineruns.tekton.dev")
        schema = next(v for v in crd["spec"]["versions"] if v["name"] == "v1")["schema"]["openAPIV3Schema"]
        declared_fields(json.loads(renderer.render(fixture(), "rke2")["run-template.json"]), schema)


if __name__ == "__main__":
    unittest.main()
