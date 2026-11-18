#!/usr/bin/env python3
"""Exercise exact review votes using explicitly simulated worker/source evidence."""

import copy
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
import unittest
from unittest import mock
from uuid import uuid4

import yaml

from bounded_subprocess import run_bounded
import render_tekton_gerrit_reporter as renderer
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter
from test_tekton_gerrit_bridge import Client as BaseClient, fixture as source_plan, package, Source, row


def label():
    return {"name": "Verified", "project": "team/application", "function": "NoBlock", "values": {"-1": "Failed", " 0": "No score", "+1": "Passed"}, "default_value": 0}


class Client(BaseClient):
    def call(self, args, **kwargs):
        if args[:2] == ["get", "--raw"]:
            return json.dumps({"apiVersion": "tekton.dev/v1", "kind": "PipelineRunList", "metadata": {"continue": ""},
                "items": [copy.deepcopy(v) for k, v in self.objects.items() if k.startswith("pipelinerun")]})
        return super().call(args, **kwargs)


class Gerrit:
    def __init__(self):
        self.account = {"_account_id": 9, "username": "ci-review"}; self.definition = label()
        self.row = {**row(), "labels": {"Verified": {"all": []}}, "messages": []}
        self.posts = []; self.fail = False; self.accept_then_fail = False; self.reads = 0; self.before_read = None

    def request(self, path, payload=None):
        if payload is not None:
            self.posts.append((path, copy.deepcopy(payload)))
            if self.fail: raise ValueError("Uncertain rejected write")
            self.row["labels"]["Verified"]["all"] = [{"_account_id": 9, "value": payload["labels"]["Verified"]}]
            self.row["messages"].append({"id": uuid4().hex, "author": {"_account_id": 9}, "_revision_number": 2, "tag": payload["tag"],
                "message": "Patch Set 2: Verified" + ("+1" if payload["labels"]["Verified"] == 1 else "-1") + "\n\n" + payload["message"]})
            if self.accept_then_fail: raise ValueError("Lost response after review persisted")
            return {"labels": payload["labels"]}
        self.reads += 1
        if self.before_read: self.before_read(self.reads)
        if path == "/a/accounts/self": return copy.deepcopy(self.account)
        if path.endswith("/labels/?inherited"): return [copy.deepcopy(self.definition)]
        return copy.deepcopy(self.row)


def fixture(root, runtime="rke2", *, source_configuration=None, source_client=None, source_item=None, report_overrides=None):
    bundle = root / "source"; bundle.mkdir()
    contract, template, digest = package(bundle, plan=source_configuration, runtime=runtime)
    plan = {"schema_version": 1, "source_contract_sha256": digest, "account": "gerrit-result-reporter", "secret": "gerrit-result-inputs",
        "http_username": "ci-review", "account_id": 9, "label_definition_sha256": reporter.fingerprint(label()),
        "helper_images": ["registry.example.com/entrypoint@sha256:" + "d" * 64], "poll_seconds": 10, "max_runs_per_poll": 5}
    if report_overrides: plan.update(report_overrides)
    files = renderer.render(bundle, plan); output = root / "reporter"; output.mkdir()
    for k, v in files.items(): (output / k).write_text(v, encoding="utf-8", newline="\n")
    report_digest = json.loads(files["reporter-status.json"])["contract_sha256"]
    reporter.load_bundle(output, report_digest)
    client = Client(contract["plan"]); source = source_client or Source()
    item = source_item or bridge.current(contract["plan"], source.row)
    bridge.submit(client, source, contract, template, digest, item)
    run = next(v for k, v in client.objects.items() if k.startswith("pipelinerun"))
    run["status"] = {"conditions": [{"type": "Succeeded", "status": "True"}], "completionTime": "2026-11-06T10:10:00Z",
        "childReferences": [{"kind": "TaskRun", "pipelineTaskName": "build", "name": "application-task"}]}
    task_spec = copy.deepcopy(run["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"])
    task_spec.pop("metadata"); task_spec.pop("spec")
    task_uid, pod_uid, node_uid = [str(uuid4()) for _ in range(3)]
    task = {"metadata": {"uid": task_uid, "ownerReferences": [{"kind": "PipelineRun", "uid": run["metadata"]["uid"], "controller": True}]},
        "spec": {"serviceAccountName": contract["plan"]["service_account"], "params": copy.deepcopy(run["spec"]["params"]), "taskSpec": task_spec},
        "status": {"conditions": [{"type": "Succeeded", "status": "True"}], "completionTime": "2026-11-06T10:09:59Z", "podName": "application-pod",
            "steps": [{"name": n, "terminated": {"exitCode": 0}} for n in ("checkout", "build")]}}
    steps = reporter.worker.substitute(task_spec["steps"], item["revision"], item["ref"]); containers = []
    for index, step in enumerate(steps):
        container = copy.deepcopy(step); container["name"] = "step-" + step["name"]; container["command"] = ["/tekton/bin/entrypoint"]
        wait = ["-wait_file", "/tekton/run/0/out"] if index else []
        container["args"] = [*wait, "-post_file", "/tekton/run/" + str(index) + "/out", "-entrypoint", step["command"][0], "--", *step["command"][1:], *step.get("args", [])]
        container["volumeMounts"].append({"name": "tekton-internal-bin", "mountPath": "/tekton/bin", "readOnly": True}); containers.append(container)
    pod = {"metadata": {"uid": pod_uid, "ownerReferences": [{"kind": "TaskRun", "uid": task_uid, "controller": True}]},
        "spec": {"nodeName": "worker-1", "serviceAccountName": contract["plan"]["service_account"],
            "securityContext": copy.deepcopy(run["spec"]["taskRunTemplate"]["podTemplate"]["securityContext"]),
            "volumes": copy.deepcopy(task_spec["volumes"]) + [{"name": "tekton-internal-bin", "emptyDir": {}}], "containers": containers,
            "initContainers": [{"name": "prepare", "image": plan["helper_images"][0]}]},
        "status": {"phase": "Succeeded", "startTime": "2026-11-06T10:00:00Z", "containerStatuses": [
            {"name": s["name"], "state": {"terminated": {"exitCode": 0}}, "imageID": "docker-pullable://" + s["image"]} for s in containers],
            "initContainerStatuses": [{"name": "prepare", "state": {"terminated": {"exitCode": 0}}, "imageID": "sha256:" + "d" * 64}]}}
    client.objects["taskrun.tekton.dev/application-task"] = task; client.objects["pod/application-pod"] = pod
    client.objects["node/worker-1"] = {"metadata": {"uid": node_uid, "creationTimestamp": "2026-11-01T10:00:00Z", "labels": contract["plan"]["node_selector"].copy()}}
    return contract, template, digest, plan, report_digest, client, run, Gerrit(), files


class ReporterTests(unittest.TestCase):
    def test_cli_failure_retains_categories_and_code_locations_without_private_values(self):
        private = "private-password-9384 https://private.example.invalid/token /private/config"
        errors = [reporter.KubernetesOperationError(1, "request timed out " + private), ValueError(private)]
        for error in errors:
            with self.subTest(error=type(error).__name__), mock.patch.object(reporter, "operate", side_effect=error):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    self.assertEqual(reporter.main(["--bundle-dir", "/private/bundle", "--contract-sha256", "a" * 64, "--watch"]), 1)
                text = output.getvalue(); record = json.loads(text.splitlines()[0])["gerrit_reporter_failure"]
                self.assertEqual(record["exception_type"], type(error).__name__)
                self.assertTrue(any(location.startswith("tekton_gerrit_reporter.py:") for location in record["code_locations"]))
                for value in ("private-password-9384", "private.example.invalid", "/private/config", "/private/bundle"):
                    self.assertNotIn(value, text)
                if isinstance(error, reporter.KubernetesOperationError):
                    self.assertTrue(record["kubernetes"]["signals"]["timeout"])
                else:
                    self.assertNotIn("kubernetes", record)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.contract, self.template, self.digest, self.plan, self.report_digest, self.client, self.run, self.api, self.files = fixture(self.root)

    def report(self): return reporter.report_run(self.client, self.api, self.contract, self.template, self.digest, self.plan, self.report_digest, self.run)

    def test_exact_evidenced_success_is_read_back_and_not_reposted_after_worker_gc(self):
        self.assertEqual(self.report(), "reported"); self.assertEqual(self.report(), "already-reported")
        del self.client.objects["taskrun.tekton.dev/application-task"]; del self.client.objects["pod/application-pod"]
        self.assertEqual(self.report(), "already-reported"); self.assertEqual(len(self.api.posts), 1)
        path, payload = self.api.posts[0]
        self.assertIn("/revisions/" + "a" * 40 + "/review", path)
        self.assertEqual(payload["labels"], {"Verified": 1}); self.assertEqual(payload["notify"], "NONE")
        self.assertEqual(payload["drafts"], "KEEP"); self.assertNotIn("on_behalf_of", payload)

    def test_pending_has_no_source_or_review_writes(self):
        self.run["status"] = {"conditions": [{"type": "Succeeded", "status": "Unknown"}]}
        self.assertEqual(self.report(), "pending"); self.assertFalse(self.api.posts); self.assertEqual(self.api.reads, 0)

    def test_terminal_failure_blocks_without_claiming_successful_worker_execution(self):
        self.run["status"]["conditions"][0]["status"] = "False"
        del self.client.objects["taskrun.tekton.dev/application-task"]; del self.client.objects["pod/application-pod"]
        self.assertEqual(self.report(), "reported"); self.assertEqual(self.api.posts[0][1]["labels"], {"Verified": -1})

    def test_only_known_terminal_cancellation_can_vote_negative(self):
        self.run["spec"]["status"] = "Cancelled"
        with self.assertRaises(ValueError): self.report()
        self.assertFalse(self.api.posts)
        self.run["status"]["conditions"][0]["status"] = "False"
        self.assertEqual(self.report(), "reported")
        self.assertEqual(self.api.posts[0][1]["labels"], {"Verified": -1})

    def test_claimed_success_without_actual_child_evidence_cannot_vote(self):
        mutations = {
            "missing-task": lambda: self.client.objects.pop("taskrun.tekton.dev/application-task"),
            "missing-pod": lambda: self.client.objects.pop("pod/application-pod"),
            "wrong-revision": lambda: self.client.objects["taskrun.tekton.dev/application-task"]["spec"]["params"][0].update(value="b" * 40),
            "wrong-ref": lambda: self.client.objects["taskrun.tekton.dev/application-task"]["spec"]["params"][1].update(value="refs/changes/23/123/3"),
            "bad-exit": lambda: self.client.objects["pod/application-pod"]["status"]["containerStatuses"][1]["state"]["terminated"].update(exitCode=1),
            "no-checkout-wait": lambda: self.client.objects["pod/application-pod"]["spec"]["containers"][1]["args"].__setitem__(1, "/elsewhere"),
            "read-token-in-build": lambda: self.client.objects["pod/application-pod"]["spec"]["containers"][1]["volumeMounts"].append({"name": "source", "mountPath": "/private"}),
            "bad-helper": lambda: self.client.objects["pod/application-pod"]["spec"]["initContainers"][0].update(image="helper:latest"),
            "control-plane-worker": lambda: self.client.objects["node/worker-1"]["metadata"]["labels"].update({"node-role.kubernetes.io/control-plane": "true"}),
            "wrong-owner": lambda: self.client.objects["taskrun.tekton.dev/application-task"]["metadata"]["ownerReferences"][0].update(uid=str(uuid4()))}
        original = copy.deepcopy(self.client.objects)
        for key, mutate in mutations.items():
            self.client.objects = copy.deepcopy(original); mutate()
            with self.subTest(key=key), self.assertRaises(ValueError): self.report()
            self.assertFalse(self.api.posts)

    def test_stale_closed_wrong_source_and_late_revision_change_cannot_vote(self):
        original = copy.deepcopy(self.api.row)
        for key, value in (("status", "MERGED"), ("work_in_progress", True), ("project", "other"), ("current_revision", "b" * 40)):
            self.api.row = {**copy.deepcopy(original), key: value}
            with self.subTest(key=key), self.assertRaises(ValueError): self.report()
            self.assertFalse(self.api.posts)
        self.api.row = original; self.api.reads = 0
        self.api.before_read = lambda count: self.api.row.update(current_revision="b" * 40) if count == 6 else None
        with self.assertRaises(ValueError): self.report()
        self.assertFalse(self.api.posts)

    def test_bot_identity_label_drift_copying_and_existing_vote_are_not_overwritten(self):
        account, definition, obj = copy.deepcopy(self.api.account), copy.deepcopy(self.api.definition), copy.deepcopy(self.api.row)
        for mode in ("account", "username", "copy", "post-submit", "default", "definition", "existing-vote"):
            self.api.account = copy.deepcopy(account); self.api.definition = copy.deepcopy(definition); self.api.row = copy.deepcopy(obj)
            if mode == "account": self.api.account["_account_id"] = 10
            if mode == "username": self.api.account["username"] = "someone-else"
            if mode == "copy":
                self.api.definition["copy_condition"] = "is:ANY"
                self.plan["label_definition_sha256"] = reporter.fingerprint(self.api.definition)
            elif mode == "post-submit":
                self.api.definition["allow_post_submit"] = True
                self.plan["label_definition_sha256"] = reporter.fingerprint(self.api.definition)
            elif mode == "default":
                self.api.definition["default_value"] = 1
                self.plan["label_definition_sha256"] = reporter.fingerprint(self.api.definition)
            elif mode == "definition": self.api.definition["function"] = "MaxWithBlock"
            else: self.plan["label_definition_sha256"] = reporter.fingerprint(definition)
            if mode == "existing-vote": self.api.row["labels"]["Verified"]["all"] = [{"_account_id": 9, "value": 1}]
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.report()
            self.assertFalse(self.api.posts)

    def test_uncertain_write_is_observed_without_repost(self):
        self.api.accept_then_fail = True
        self.assertEqual(self.report(), "reported"); self.assertEqual(self.report(), "already-reported"); self.assertEqual(len(self.api.posts), 1)

    def test_failed_write_keeps_reservation_and_is_not_retried(self):
        self.api.fail = True
        with self.assertRaises(ValueError): self.report()
        self.api.fail = False
        with self.assertRaises(ValueError): self.report()
        self.assertEqual(len(self.api.posts), 1)

    def test_uncertain_reservation_never_sends_a_review(self):
        self.client.fail = "lock-after"
        with self.assertRaises(ValueError): self.report()
        self.client.fail = None
        with self.assertRaises(ValueError): self.report()
        self.assertFalse(self.api.posts)

    def test_published_vote_message_and_reservation_drift_refuse(self):
        self.report(); original_objects = copy.deepcopy(self.client.objects); original_row = copy.deepcopy(self.api.row)
        vote_key = next(k for k in original_objects if k.endswith("-vote-lock"))
        for mode in ("vote", "author", "revision", "message", "tag", "reservation", "copied"):
            self.client.objects = copy.deepcopy(original_objects); self.api.row = copy.deepcopy(original_row)
            if mode == "vote": self.api.row["labels"]["Verified"]["all"][0]["value"] = -1
            if mode == "author": self.api.row["messages"][0]["author"]["_account_id"] = 10
            if mode == "revision": self.api.row["messages"][0]["_revision_number"] = 3
            if mode == "message": self.api.row["messages"][0]["message"] = "other run"
            if mode == "tag": self.api.row["messages"][0]["tag"] = "other"
            if mode == "reservation": self.client.objects[vote_key]["metadata"]["uid"] = str(uuid4())
            if mode == "copied": self.api.row["labels"]["Verified"]["all"][0]["copied"] = True
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.report()
        self.assertEqual(len(self.api.posts), 1)

    def test_corrupted_package_separate_identities_and_five_runtime_outputs(self):
        original = self.contract["plan"]
        for key, value in (("account", original["submitter_account"]), ("secret", original["bridge_secret"]), ("http_username", original["source"]["read_username"]), ("account_id", True), ("helper_images", ["helper:latest"])):
            with self.subTest(key=key), self.assertRaises(ValueError): reporter.validate({**self.plan, key: value}, self.contract)
        for runtime in bridge.RUNTIMES:
            with tempfile.TemporaryDirectory() as d:
                contract, template, digest, plan, rdigest, client, run, api, files = fixture(Path(d), runtime)
                status = json.loads(files["reporter-status.json"])
                self.assertEqual(status["source_contract_sha256"], digest)
                for key in bridge.FALSE_FLAGS: self.assertIs(status[key], False)
                roles = list(yaml.safe_load_all(files["reporter-rbac.yaml"]))
                role = next(v for v in roles if v["kind"] == "Role")
                runs = next(r for r in role["rules"] if r["resources"] == ["pipelineruns"])
                self.assertEqual(runs["verbs"], ["get", "list"])
                self.assertFalse(any("secrets" in r["resources"] for r in role["rules"]))
                if runtime in {"rke2", "k3s", "k8s"}:
                    pod = list(yaml.safe_load_all(files["reporter-workload.yaml"]))[1]["spec"]["template"]["spec"]
                    self.assertEqual(pod["serviceAccountName"], plan["account"])
                    self.assertEqual(next(v for v in pod["volumes"] if v["name"] == "inputs")["secret"]["secretName"], plan["secret"])
                    self.assertFalse(pod["automountServiceAccountToken"])
                elif runtime == "docker": self.assertNotIn("ports", next(iter(yaml.safe_load(files["compose.yaml"])["services"].values())))
                else: self.assertIn("User=platform-ci-reporter", files["platform-gerrit-tekton-reporter.service"])
        (self.root / "reporter/tekton_gerrit_reporter.py").write_text("changed")
        with self.assertRaises(ValueError): reporter.load_bundle(self.root / "reporter", self.report_digest)

    def test_bounded_poll_catches_stale_runs_without_publishing(self):
        self.api.row["status"] = "ABANDONED"
        summary, cursor = reporter.poll(self.client, self.api, self.contract, self.template, self.digest, self.plan, self.report_digest)
        self.assertEqual(summary["rejected"], 1); self.assertFalse(self.api.posts); self.assertEqual(cursor, "")

    @unittest.skipUnless(os.environ.get("TEKTON_GERRIT_TEST_API") == "1", "Explicit actual isolated API and separate reporter RBAC")
    def test_actual_kubernetes_reporter_rbac_and_failure_receipt(self):
        from test_tekton_gerrit_api_fixture import exercise
        exercise(self, reporter_check=True)

    @unittest.skipUnless(os.environ.get("GERRIT_REVIEW_TEST_RUNTIME") == "1", "Explicit isolated real Gerrit distribution and native API tokens")
    def test_real_gerrit_discovery_checkout_and_review_protocol(self):
        from test_gerrit_review_runtime_fixture import exercise
        exercise(self)

    @unittest.skipUnless(os.name == "posix", "Actual loopback HTTPS transport requires Linux OpenSSL")
    def test_real_https_review_transport_enforces_trust_auth_scope_and_readback(self):
        openssl = shutil.which("openssl")
        if not openssl: self.skipTest("OpenSSL is required")
        inputs = self.root / "inputs"; inputs.mkdir(mode=0o700)
        run_bounded([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(inputs / "tls.key"), "-out", str(inputs / "ca.crt")], timeout=30, check=True, output_max_bytes=1024 * 1024)
        token = uuid4().hex; (inputs / "api-token").write_text(token); (inputs / "api-token").chmod(0o600); (inputs / "ca.crt").chmod(0o600)
        import base64
        expected_auth = "Basic " + base64.b64encode((self.plan["http_username"] + ":" + token).encode()).decode()
        fake = self.api; seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle_request(self):
                if self.headers.get("Authorization") != expected_auth:
                    self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
                size = int(self.headers.get("Content-Length", "0"))
                if size > bridge.LIMIT: raise ValueError("Fixture request too large")
                payload = json.loads(self.rfile.read(size)) if size else None
                seen.append(self.command)
                body = b")]}'\n" + json.dumps(fake.request(self.path, payload)).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            do_GET = handle_request
            do_POST = handle_request
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(inputs / "ca.crt", inputs / "tls.key")
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                original = copy.deepcopy(self.contract["plan"]); original["source"]["api_url"] = "https://127.0.0.1:" + str(server.server_port)
                original["source"]["clone_url"] = original["source"]["api_url"] + "/a/team/application.git"
                original["source"]["ca_sha256"] = hashlib.sha256((inputs / "ca.crt").read_bytes()).hexdigest()
                api = reporter.GerritReview(original, self.plan, inputs)
                self.assertTrue(api.context.check_hostname)
                reporter.source_policy(api, original, self.plan)
                self.assertEqual(reporter.report_run(self.client, api, self.contract, self.template, self.digest, self.plan, self.report_digest, self.run), "reported")
                self.assertEqual(reporter.report_run(self.client, api, self.contract, self.template, self.digest, self.plan, self.report_digest, self.run), "already-reported")
                self.assertEqual(seen.count("POST"), 1)
                with self.assertRaises(ValueError): api.request("/a/projects/other/labels/", {"arbitrary": True})
                original["source"]["ca_sha256"] = "0" * 64
                with self.assertRaises(ValueError): reporter.GerritReview(original, self.plan, inputs)
            finally: server.shutdown(); thread.join(timeout=5)


if __name__ == "__main__": unittest.main()
