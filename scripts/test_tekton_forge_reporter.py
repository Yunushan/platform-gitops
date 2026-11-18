#!/usr/bin/env python3
"""Verify result attribution, worker evidence, status receipts and isolation."""

import contextlib
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
import unittest
from unittest import mock

import yaml

from bounded_subprocess import run_bounded
import render_tekton_forge_bridge as renderer
import tekton_forge_bridge as bridge
import tekton_forge_artifact_promotion as promotion
import tekton_forge_reporter as reporter
from test_tekton_forge_bridge import FakeClient as SubmitClient, fixture as base_plan


def fixture(root, plan=None, runtime="rke2"):
    plan = copy.deepcopy(plan) if plan is not None else base_plan()
    plan["reporting"] = {"enabled": True, "account": "ci-result-reporter", "secret": "ci-result-inputs",
                         "context": "ci/tekton/application", "poll_seconds": 10, "max_runs_per_poll": 5,
                         "helper_images": ["registry.example.com/entrypoint@sha256:" + "d" * 64]}
    files = renderer.render(plan, runtime)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8", newline="\n")
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    contract, template = bridge.load_bundle(root, digest)
    client = FakeClient()
    with mock.patch.object(bridge, "verify_head"):
        bridge.submit(client, contract, template, digest, "a" * 40)
    run = next(obj for key, obj in client.objects.items() if key.startswith("pipelinerun"))
    run["status"] = {"conditions": [{"type": "Succeeded", "status": "True"}], "completionTime": "2026-11-06T10:10:00Z",
                     "childReferences": [{"kind": "TaskRun", "pipelineTaskName": "build", "name": "application-task"}]}
    task_spec = copy.deepcopy(run["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"])
    task_spec.pop("metadata"); task_spec.pop("spec")
    task_uid, pod_uid, node_uid = [str(i) * 8 + "-" + str(i) * 4 + "-4" + str(i) * 3 + "-8" + str(i) * 3 + "-" + str(i) * 12 for i in (3, 4, 5)]
    reviewed_steps = task_spec["steps"]
    task = {"metadata": {"uid": task_uid, "ownerReferences": [{"kind": "PipelineRun", "uid": run["metadata"]["uid"], "controller": True}]},
            "spec": {"serviceAccountName": plan["service_account"], "params": [{"name": "revision", "value": "a" * 40}], "taskSpec": task_spec},
            "status": {"conditions": [{"type": "Succeeded", "status": "True"}], "completionTime": "2026-11-06T10:09:59Z", "podName": "application-pod",
                       "steps": [{"name": step["name"], "terminated": {"exitCode": 0}, "imageID": step["image"]} for step in reviewed_steps]}}
    steps = reporter.substitute(reviewed_steps, "a" * 40)
    if plan["schema_version"] == 2:
        from gerrit_artifact_contract import resolve
        steps = resolve(steps, {"revision": "a" * 40}, pod=True)
    containers = []
    for index, step in enumerate(steps):
        container = copy.deepcopy(step)
        container["name"] = "step-" + step["name"]
        container["command"] = ["/tekton/bin/entrypoint"]
        order = ["-wait_file", f"/tekton/run/{index - 1}/out"] if index else []
        container["args"] = [*order, "-post_file", "/tekton/run/" + str(index) + "/out", "-entrypoint", step["command"][0], "--", *step["command"][1:], *step.get("args", [])]
        container["volumeMounts"].append({"name": "tekton-internal-bin", "mountPath": "/tekton/bin", "readOnly": True})
        containers.append(container)
    pod = {"metadata": {"uid": pod_uid, "ownerReferences": [{"kind": "TaskRun", "uid": task_uid, "controller": True}]},
           "spec": {"nodeName": "worker-1", "serviceAccountName": plan["service_account"],
                    "securityContext": copy.deepcopy(run["spec"]["taskRunTemplate"]["podTemplate"]["securityContext"]),
                    "volumes": copy.deepcopy(task_spec["volumes"]) + [{"name": "tekton-internal-bin", "emptyDir": {}}], "containers": containers,
                    "initContainers": [{"name": "prepare", "image": plan["reporting"]["helper_images"][0]}]},
           "status": {"phase": "Succeeded", "startTime": "2026-11-06T10:00:00Z", "containerStatuses": [
               {"name": step["name"], "state": {"terminated": {"exitCode": 0}}, "imageID": "docker-pullable://" + step["image"]} for step in containers],
               "initContainerStatuses": [{"name": "prepare", "state": {"terminated": {"exitCode": 0}}, "imageID": "sha256:" + "d" * 64}]}}
    client.objects["taskrun.tekton.dev/application-task"] = task
    client.objects["pod/application-pod"] = pod
    client.objects["node/worker-1"] = {"metadata": {"uid": node_uid, "creationTimestamp": "2026-11-01T10:00:00Z", "labels": plan["node_selector"].copy()}}
    return contract, template, digest, client, run


class FakeClient(SubmitClient):
    def __init__(self):
        super().__init__()
        self.cursor = ""
        self.mutate = False

    def get(self, resource, namespace=None, *, optional=False):
        if self.mutate and resource.startswith("pipelinerun") and not optional:
            self.objects[resource]["metadata"]["uid"] = "66666666-6666-4666-8666-666666666666"
        return super().get(resource, namespace, optional=optional)

    def call(self, args, *, payload=None, seconds=30):
        if args[:2] == ["get", "--raw"]:
            self.calls.append(("list", args[2]))
            return json.dumps({"apiVersion": "tekton.dev/v1", "kind": "PipelineRunList",
                "items": [obj for key, obj in self.objects.items() if key.startswith("pipelinerun")], "metadata": {"continue": self.cursor}})
        return super().call(args, payload=payload, seconds=seconds)


class Forge:
    def __init__(self):
        self.status = None
        self.posts = 0
        self.fail_post = False
        self.accept_then_fail = False
        self.wrong_repository = False

    def request(self, plan, suffix, payload=None):
        if payload is not None:
            self.posts += 1
            if not self.fail_post:
                self.status = copy.deepcopy(payload)
                self.status["status"] = self.status.pop("state")
            if self.fail_post or self.accept_then_fail:
                raise ValueError("Ambiguous API response")
            return copy.deepcopy(payload), {}
        if "/commits/" in suffix:
            return {"sha": "a" * 40, "statuses": [self.status] if self.status else []}, {}
        return {"id": 124 if self.wrong_repository else 123, "full_name": "team/application"}, {}


class ReporterTests(unittest.TestCase):
    def test_only_empty_embedded_task_wrapper_can_project_to_regular_task_execution(self):
        body = {"steps": [{"name": "build"}], "metadata": {}, "spec": None}
        self.assertEqual(reporter.inline_task_spec(body), {"steps": [{"name": "build"}]})
        for key, value in (("metadata", {"labels": {"changed": "true"}}), ("metadata", None), ("spec", {"arbitrary": "execution"})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): reporter.inline_task_spec({**body, key: value})

    def test_verified_success_is_published_read_back_and_not_reappended(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest, client, run = fixture(Path(directory))
            forge = Forge()
            with mock.patch.object(reporter, "forge_request", side_effect=forge.request):
                self.assertEqual(reporter.report_run(client, contract, template, digest, run), "reported")
                self.assertEqual(reporter.report_run(client, contract, template, digest, run), "already-reported")
                del client.objects["pod/application-pod"]
                del client.objects["taskrun.tekton.dev/application-task"]
                self.assertEqual(reporter.report_run(client, contract, template, digest, run), "already-reported")
            self.assertEqual(forge.posts, 1)
            self.assertEqual(forge.status["status"], "success")
            self.assertTrue(forge.status["context"].endswith(digest[:12]))
            self.assertIn(run["metadata"]["uid"], forge.status["description"])

    def test_no_success_from_changed_identity_steps_credentials_or_worker(self):
        for mutation in ("run-uid", "owner", "exit", "command", "image", "extra-step", "secret", "env-secret", "host", "node", "node-recreated", "working-dir", "skip-flag", "wait-file", "alternate-entrypoint", "unreviewed-helper", "writable-entrypoint"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                contract, template, digest, client, run = fixture(Path(directory))
                pod, task = client.objects["pod/application-pod"], client.objects["taskrun.tekton.dev/application-task"]
                if mutation == "run-uid": run["metadata"]["uid"] = "66666666-6666-4666-8666-666666666666"
                if mutation == "owner": task["metadata"]["ownerReferences"][0]["uid"] = "wrong"
                if mutation == "exit": pod["status"]["containerStatuses"][1]["state"]["terminated"]["exitCode"] = 1
                if mutation == "command": pod["spec"]["containers"][1]["args"][-1] = "different-test"
                if mutation == "image": pod["spec"]["containers"][1]["image"] = "image:latest"
                if mutation == "extra-step": run["status"]["childReferences"].append({"kind": "TaskRun", "name": "extra"})
                if mutation == "secret": pod["spec"]["containers"][1]["volumeMounts"].append({"name": "source", "mountPath": "/secret"})
                if mutation == "env-secret": pod["spec"]["containers"][1]["envFrom"] = [{"secretRef": {"name": "extra"}}]
                if mutation == "host": pod["spec"]["hostNetwork"] = True
                if mutation == "node": client.objects["node/worker-1"]["metadata"]["labels"]["node-role.kubernetes.io/control-plane"] = ""
                if mutation == "node-recreated": client.objects["node/worker-1"]["metadata"]["creationTimestamp"] = "2026-11-06T10:20:00Z"
                if mutation == "working-dir": pod["spec"]["containers"][1]["workingDir"] = "/other"
                if mutation == "skip-flag": pod["spec"]["containers"][1]["args"][:0] = ["--when_expressions", "false"]
                if mutation == "wait-file": pod["spec"]["containers"][1]["args"][1] = "/already-present"
                if mutation == "alternate-entrypoint": pod["spec"]["containers"][1]["args"][:0] = ["--entrypoint=/bin/true"]
                if mutation == "unreviewed-helper": pod["spec"]["initContainers"][0]["image"] = "unknown:latest"
                if mutation == "writable-entrypoint": pod["spec"]["containers"][1]["volumeMounts"][-1]["readOnly"] = False
                forge = Forge()
                with mock.patch.object(reporter, "forge_request", side_effect=forge.request), self.assertRaises((ValueError, KeyError)):
                    reporter.report_run(client, contract, template, digest, run)
                self.assertEqual(forge.posts, 0)

    def test_failure_before_worker_creation_is_reported_without_claiming_a_build_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest, client, run = fixture(Path(directory))
            run["status"]["conditions"][0]["status"] = "False"
            run["status"]["childReferences"] = []
            forge = Forge()
            with mock.patch.object(reporter, "forge_request", side_effect=forge.request):
                self.assertEqual(reporter.report_run(client, contract, template, digest, run), "reported")
            self.assertEqual(forge.status["status"], "failure")

    def test_pending_and_unreceipted_runs_never_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest, client, run = fixture(Path(directory))
            run["status"]["conditions"][0]["status"] = "Unknown"
            with mock.patch.object(reporter, "forge_request") as forge:
                self.assertEqual(reporter.report_run(client, contract, template, digest, run), "pending")
                forge.assert_not_called()
            run["status"]["conditions"][0]["status"] = "True"
            del client.objects["configmap/" + run["metadata"]["name"] + "-done"]
            with mock.patch.object(reporter, "forge_request") as forge, self.assertRaises(ValueError):
                reporter.report_run(client, contract, template, digest, run)
            forge.assert_not_called()

    def test_prior_report_drift_is_reported_without_overwriting_the_forge(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest, client, run = fixture(Path(directory))
            forge = Forge()
            with mock.patch.object(reporter, "forge_request", side_effect=forge.request):
                reporter.report_run(client, contract, template, digest, run)
                forge.status["status"] = "failure"
                with self.assertRaises(ValueError):
                    reporter.report_run(client, contract, template, digest, run)
            self.assertEqual(forge.posts, 1)

    def test_mutated_receipt_is_rejected_even_when_create_returns_success(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, _, digest, client, _ = fixture(Path(directory))
            original = client.call
            def mutate(args, **kwargs):
                result = original(args, **kwargs)
                if args[0] == "create":
                    obj = json.loads(kwargs["payload"])
                    client.objects["configmap/" + obj["metadata"]["name"]]["data"]["unexpected"] = "changed"
                return result
            with mock.patch.object(client, "call", side_effect=mutate), self.assertRaises(ValueError):
                bridge.receipt(client, contract["plan"]["namespace"], "fixture-receipt", digest, {"kind": "fixture"}, create=True)

    def test_status_pagination_uses_fixed_paths_and_fails_when_unobservable(self):
        plan = base_plan()
        rows = [{"context": "other-" + str(i), "status": "success"} for i in range(50)]
        responses = [({"sha": "a" * 40, "statuses": rows}, {"link": '<https://other.example.invalid/page>; rel="next"'}),
                     ({"sha": "a" * 40, "statuses": [{"context": "selected", "status": "success"}]}, {})]
        with mock.patch.object(reporter, "forge_request", side_effect=responses) as request:
            self.assertEqual(reporter.latest_status(plan, "a" * 40, "selected")["status"], "success")
        self.assertTrue(all(call.args[1].startswith("/repos/team/application/commits/") for call in request.call_args_list))
        with mock.patch.object(reporter, "forge_request", return_value=({"sha": "a" * 40, "statuses": rows}, {})), self.assertRaises(ValueError):
            reporter.latest_status(plan, "a" * 40, "selected")

    def test_lost_append_response_recovers_by_readback_but_no_blind_retry(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                contract, template, digest, client, run = fixture(Path(directory))
                forge = Forge()
                forge.accept_then_fail = accepted
                forge.fail_post = not accepted
                with mock.patch.object(reporter, "forge_request", side_effect=forge.request):
                    if accepted:
                        self.assertEqual(reporter.report_run(client, contract, template, digest, run), "reported")
                    else:
                        for _ in range(2):
                            with self.assertRaises(ValueError):
                                reporter.report_run(client, contract, template, digest, run)
                self.assertEqual(forge.posts, 1)

    def test_conflicting_forge_context_or_repository_and_midflight_replacement_block_writes(self):
        for fault in ("context", "repository", "replacement"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                contract, template, digest, client, run = fixture(Path(directory))
                forge = Forge()
                if fault == "context": forge.status = {"context": contract["plan"]["reporting"]["context"] + "/" + digest[:12], "status": "failure"}
                if fault == "repository": forge.wrong_repository = True
                if fault == "replacement": client.mutate = True
                with mock.patch.object(reporter, "forge_request", side_effect=forge.request), self.assertRaises(ValueError):
                    reporter.report_run(client, contract, template, digest, run)
                self.assertEqual(forge.posts, 0)

    def test_poll_is_bounded_and_selector_is_derived_from_reviewed_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest, client, run = fixture(Path(directory))
            forge = Forge()
            client.cursor = "opaque-token"
            with mock.patch.object(reporter, "forge_request", side_effect=forge.request):
                summary, cursor = reporter.poll(client, contract, template, digest)
            self.assertEqual(summary["reported"], 1)
            self.assertEqual(cursor, "opaque-token")
            query = next(path for action, path in client.calls if action == "list")
            self.assertIn("limit=5", query)
            self.assertIn(digest[:32], query)
            with mock.patch.object(client, "call", return_value='{"kind":"Status"}'), self.assertRaises(ValueError):
                reporter.poll(client, contract, template, digest)

    def test_default_cli_is_local_and_reporting_is_disabled_unless_declared(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, template, digest, client, run = fixture(Path(directory))
            with mock.patch.object(bridge, "Client") as factory, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reporter.main(["--bundle-dir", directory, "--contract-sha256", digest]), 0)
            factory.assert_not_called()
            self.assertFalse(bridge.reporting_settings(base_plan())["enabled"])

    def test_reporter_credentials_and_rbac_are_separate_in_all_runtime_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            contract, _, _, _, _ = fixture(Path(directory))
            plan = contract["plan"]
            for runtime in renderer.RUNTIMES:
                files = renderer.render(plan, runtime)
                roles = list(yaml.safe_load_all(files["reporter-rbac.yaml"]))
                role = next(obj for obj in roles if obj["kind"] == "Role")
                self.assertFalse(any("pipelineruns" in rule["resources"] and "create" in rule["verbs"] for rule in role["rules"]))
                self.assertFalse(any(set(rule["verbs"]) & {"patch", "update", "delete"} or "secrets" in rule["resources"] for rule in role["rules"]))
                if runtime in {"rke2", "k3s", "k8s"}:
                    objects = list(yaml.safe_load_all(files["reporter-workload.yaml"]))
                    deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
                    pod = deployment["spec"]["template"]["spec"]
                    self.assertEqual(pod["serviceAccountName"], plan["reporting"]["account"])
                    self.assertEqual(next(v for v in pod["volumes"] if v["name"] == "inputs")["secret"]["secretName"], plan["reporting"]["secret"])
                    receiver = next(obj for obj in yaml.safe_load_all(files["bridge-workload.yaml"]) if obj["kind"] == "Deployment")
                    receiver_refs = [v["secret"]["secretName"] for v in receiver["spec"]["template"]["spec"]["volumes"] if "secret" in v]
                    self.assertNotIn(plan["reporting"]["secret"], receiver_refs)
                if runtime == "docker":
                    services = yaml.safe_load(files["compose.yaml"])["services"]
                    self.assertNotIn("ports", services[plan["name"] + "-reporter"])
                    self.assertIn("PLATFORM_TEKTON_REPORTER_INPUTS", services[plan["name"] + "-reporter"]["volumes"][1]["source"])
                if runtime == "raw":
                    self.assertIn("User=platform-ci-reporter", files["platform-tekton-reporter.service"])

    def test_compose_and_native_reporter_configuration_with_available_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract, _, _, _, _ = fixture(root)
            docker = shutil.which("docker")
            if docker:
                version = run_bounded([docker, "compose", "version"], timeout=15, text=True, output_max_bytes=65536, check=False)
                if version.returncode == 0:
                    files = renderer.render(contract["plan"], "docker")
                    path = root / "compose.yaml"
                    path.write_text(files["compose.yaml"], encoding="utf-8")
                    env = {**os.environ, "PLATFORM_TEKTON_BRIDGE_BUNDLE": root.as_posix(), "PLATFORM_TEKTON_BRIDGE_INPUTS": root.as_posix(),
                           "PLATFORM_TEKTON_REPORTER_INPUTS": root.as_posix()}
                    result = run_bounded([docker, "compose", "-f", str(path), "config", "--quiet"], env=env,
                                         timeout=30, text=True, output_max_bytes=65536, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
            systemd = shutil.which("systemd-analyze") if os.name == "posix" else None
            if systemd:
                path = root / "platform-tekton-reporter.service"
                path.write_text(renderer.render(contract["plan"], "raw")[path.name], encoding="utf-8")
                result = run_bounded([systemd, "verify", "--man=no", str(path)], timeout=30, text=True, output_max_bytes=65536, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "Local trusted HTTPS API fixture uses Linux TLS tooling")
    def test_real_https_status_post_readback_and_retry_against_local_api(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("OpenSSL is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "inputs"
            credentials.mkdir()
            (credentials / "status-token").write_text("local-fixture-status-token")
            (credentials / "status-token").chmod(0o600)
            result = run_bounded([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=local-test",
                "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(root / "tls.key"), "-out", str(credentials / "ca.crt")],
                timeout=30, text=True, output_max_bytes=65536, check=False)
            self.assertEqual(result.returncode, 0)
            remote = Forge()
            class API(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def handle_api(self):
                    if self.headers.get("Authorization") != "token local-fixture-status-token":
                        self.send_error(403)
                        return
                    payload = None
                    if self.command == "POST":
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    data, _ = remote.request(contract["plan"], self.path[7:], payload)
                    content = json.dumps(data).encode()
                    self.send_response(201 if payload else 200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                do_GET = handle_api
                do_POST = handle_api
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(credentials / "ca.crt", root / "tls.key", password="")
            with ThreadingHTTPServer(("127.0.0.1", 0), API) as server:
                server.socket = context.wrap_socket(server.socket, server_side=True)
                plan = base_plan()
                plan["source"]["api_url"] = "https://127.0.0.1:" + str(server.server_port) + "/api/v1"
                plan["source"]["clone_url"] = "https://127.0.0.1:" + str(server.server_port) + "/team/application.git"
                contract, template, digest, client, run = fixture(root, plan)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with mock.patch.object(reporter, "INPUTS", credentials), mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(credentials / "ca.crt")}):
                        self.assertEqual(reporter.report_run(client, contract, template, digest, run), "reported")
                        self.assertEqual(reporter.report_run(client, contract, template, digest, run), "already-reported")
                    self.assertEqual(remote.posts, 1)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


class ArtifactReporterTests(unittest.TestCase):
    def setUp(self):
        import gerrit_artifact_contract as artifact
        from test_harbor_static_image import plan_fixture

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        registry, _ = plan_fixture(root)
        del registry["revision"]
        registry.update(ca_file="/publisher-input/ca.crt", credential_file="/run/robot/credential.json")
        plan = base_plan()
        plan["schema_version"] = 2
        plan["source"]["ca_sha256"] = "a" * 64
        plan["publication"] = {"publisher_image": plan["build_image"], "trust_secret": "publisher-trust",
                               "robot_secret": "publisher-robot", "registry": registry}
        self.contract, self.template, self.digest, self.client, self.run = fixture(root, plan)
        self.task = self.client.objects["taskrun.tekton.dev/application-task"]
        self.pod = self.client.objects["pod/application-pod"]
        self.source = artifact.forge_source_plan(plan, "a" * 40)
        image_digest = "sha256:" + "e" * 64
        self.receipt = {"schema_version": 1, "image": "registry.example.invalid/fixture/ha@" + image_digest,
            "manifest_digest": image_digest, "config_digest": image_digest, "output_layer_digest": image_digest,
            "output_tree_sha256": "a" * 64, "base_digest": registry["base_digest"], "source_revision": "a" * 40,
            "artifact_publication_verified": True, "source_checkout_witness_bound": True, "publisher_signature_verified": False,
            "deployment_verified": False, "ha_verified": False, "full_stack_verified": False,
            "checkout_witness_sha256": hashlib.sha256(artifact.source_binding.witness_bytes(self.source)).hexdigest(),
            "source_plan_sha256": promotion.common.sha256(self.source)}
        for result in (self.run, self.task):
            result["status"]["results"] = [{"name": "image-receipt", "value": json.dumps(self.receipt)}]

    def test_success_binds_both_results_to_the_exact_publisher(self):
        data, proof = reporter.evidence(self.client, self.contract, self.template, self.digest, self.run)
        self.assertEqual(proof["artifact"], self.receipt)
        self.assertEqual(proof["steps"][-1]["step"], "publish")
        self.assertEqual(proof["task_uid"], self.task["metadata"]["uid"])
        self.assertEqual(self.source["branch_ref"], "refs/heads/main")
        self.assertEqual(data["revision"], self.source["revision"])
        for replacement in ({**self.receipt, "checkout_witness_sha256": "0" * 64},
                            {**self.receipt, "image": "other.example.invalid/fixture/ha@" + self.receipt["manifest_digest"]}):
            for result in (self.task, self.run):
                result["status"]["results"][0]["value"] = json.dumps(replacement)
            with self.assertRaisesRegex(ValueError, "outside the reviewed"):
                reporter.evidence(self.client, self.contract, self.template, self.digest, self.run)

    def test_receipt_substitution_or_wrong_image_id_refuses_success(self):
        original = copy.deepcopy(self.task["status"]["results"])
        self.task["status"]["results"][0]["value"] = "{}"
        with self.assertRaisesRegex(ValueError, "results differ"):
            reporter.evidence(self.client, self.contract, self.template, self.digest, self.run)
        self.task["status"]["results"] = original
        self.pod["status"]["containerStatuses"][-1]["imageID"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "Publisher image ID"):
            reporter.evidence(self.client, self.contract, self.template, self.digest, self.run)

    def test_protection_loss_blocks_release_and_signature_binds_run_uids(self):
        data, evidence = reporter.evidence(self.client, self.contract, self.template, self.digest, self.run)
        plan = {"handoff": {"bridge_contract_sha256": self.digest, "deployment_branch": "deploy/staging"},
                "source_revision": data["revision"], "pipeline_run": data["run"]}
        with mock.patch.object(promotion.common, "verified_ci", return_value=(data, evidence)):
            blocked = lambda *_: ({"name": "deploy/staging", "protected": False}, {})
            with self.assertRaisesRegex(ValueError, "deployment-branch-is-unprotected"):
                promotion.verify(plan, self.contract, self.template, self.client, blocked)
            allowed = lambda *_: ({"name": "deploy/staging", "protected": True}, {})
            scoped, proof = promotion.verify(plan, self.contract, self.template, self.client, allowed)
        request = promotion.release_request(plan, self.contract, scoped, proof)
        self.assertEqual(request["annotations"]["platform.gitops/task-run-uid"], self.task["metadata"]["uid"])
        self.assertEqual(request["annotations"]["platform.gitops/pipeline-run-uid"], self.run["metadata"]["uid"])


if __name__ == "__main__":
    unittest.main()
