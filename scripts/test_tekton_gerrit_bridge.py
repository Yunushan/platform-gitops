#!/usr/bin/env python3
"""Check Gerrit review identity, fixed jobs, retained writes and actual TLS Git."""

import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from bounded_subprocess import run_bounded
import render_tekton_gerrit_bridge as renderer
import tekton_checkout as checkout
import tekton_gerrit_bridge as bridge
from test_tekton_forge_bridge import fixture as forge_fixture


def fixture():
    old = forge_fixture()
    plan = {k: v for k, v in old.items() if k not in {"provider", "publish_address"}}
    plan["name"] = "gerrit-review"
    plan.update(namespace_uid="33333333-3333-4333-8333-333333333333", poll_seconds=30, max_changes=100)
    plan["source"] = {"api_url": "https://review.example.com", "project": "team/application", "branch": "main",
        "clone_url": "https://review.example.com/a/team/application.git", "read_username": "ci-read", "ca_sha256": "c" * 64}
    return plan


def row(*, commit="a" * 40, change=123, patchset=2):
    return {"project": "team/application", "branch": "main", "status": "NEW", "_number": change,
        "current_revision": commit, "revisions": {commit: {"_number": patchset, "ref": bridge.reference(change, patchset)}}}


def event():
    return {"type": "patchset-created", "change": {"project": "team/application", "branch": "main", "number": "123"},
        "patchSet": {"number": "2", "revision": "a" * 40, "ref": "refs/changes/23/123/2"}}


def package(root, plan=None, runtime="rke2"):
    files = renderer.render(plan or fixture(), runtime)
    for k, v in files.items(): (root / k).write_text(v, encoding="utf-8", newline="\n")
    digest = json.loads(files["render-status.json"])["contract_sha256"]
    contract, template = bridge.load_bundle(root, digest)
    return contract, template, digest


class Source:
    def __init__(self): self.row = row(); self.calls = []

    def get(self, path):
        self.calls.append(path)
        return [copy.deepcopy(self.row)] if path.startswith("/a/changes/?") else copy.deepcopy(self.row)


class Client:
    def __init__(self, plan):
        self.plan = plan; self.objects = {}; self.calls = []; self.fail = None; self.mutate = False

    def get(self, resource, namespace=None, *, optional=False):
        if resource == "namespace/kube-system": return {"metadata": {"uid": self.plan["management_target"]["cluster_uid"]}}
        if resource == "namespace/" + self.plan["namespace"]: return {"metadata": {"uid": self.plan["namespace_uid"], "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
        return copy.deepcopy(self.objects.get(resource))

    def call(self, args, *, payload=None, seconds=30):
        if args[0] == "config": return json.dumps({"current-context": "ci", "contexts": [{"name": "ci", "context": {"cluster": "ci"}}], "clusters": [{"name": "ci", "cluster": {"server": "https://127.0.0.1:6443"}}]})
        obj = json.loads(payload); name = obj["metadata"]["name"]
        kind = "pipelinerun.tekton.dev" if obj["kind"] == "PipelineRun" else "configmap"
        key = kind + "/" + name
        self.calls.append((args, copy.deepcopy(obj)))
        obj["metadata"]["uid"] = str(uuid4())
        if "--dry-run=server" in args:
            if self.mutate: obj["spec"]["pipelineRef"] = {"name": "injected"}
            return json.dumps(obj)
        if key in self.objects: raise ValueError("exists")
        stage = "lock" if name.endswith("-lock") else "run" if kind.startswith("pipelinerun") else "done"
        if self.fail == stage + "-before": raise ValueError("uncertain write")
        self.objects[key] = obj
        if self.fail == stage + "-after": raise ValueError("lost acknowledgement")
        return json.dumps(obj)


class GerritTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.contract, self.template, self.digest = package(self.root)
        self.plan = self.contract["plan"]; self.source = Source(); self.client = Client(self.plan)
        self.item = bridge.from_event(self.plan, event())

    def submit(self): return bridge.submit(self.client, self.source, self.contract, self.template, self.digest, self.item)

    def test_event_requires_exact_project_branch_patchset_and_nonzero_revision(self):
        self.assertEqual(self.item, bridge.current(self.plan, row()))
        for path, value in ((["type"], "ref-updated"), (["change", "project"], "other"), (["change", "branch"], "release"),
            (["change", "number"], True), (["patchSet", "number"], "02"), (["patchSet", "revision"], "0" * 40), (["patchSet", "ref"], "refs/changes/22/123/2")):
            bad = event(); cursor = bad
            for k in path[:-1]: cursor = cursor[k]
            cursor[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError): bridge.from_event(self.plan, bad)

    def test_closed_wip_wrong_project_and_stale_events_never_reserve(self):
        for key, value in (("status", "MERGED"), ("work_in_progress", True), ("project", "other"), ("current_revision", "b" * 40)):
            self.source.row = {**row(), key: value}
            with self.subTest(key=key), self.assertRaises(ValueError): self.submit()
            self.assertFalse(self.client.objects)

    def test_duplicate_delivery_and_garbage_collection_do_not_recreate_runs(self):
        self.assertFalse(self.submit()["duplicate"])
        self.assertTrue(self.submit()["duplicate"])
        key = next(k for k in self.client.objects if k.startswith("pipelinerun"))
        del self.client.objects[key]
        self.assertFalse(self.submit()["run_retained"])
        self.assertNotIn(key, self.client.objects)
        self.assertEqual(sum(o["kind"] == "PipelineRun" and "--dry-run=server" not in a for a, o in self.client.calls), 1)

    def test_controller_pipeline_label_is_predeclared_and_other_metadata_is_not_adopted(self):
        expected, _ = bridge.run_for(self.contract, self.template, self.digest, bridge.current(self.plan, self.source.row))
        self.assertEqual(expected["metadata"]["labels"]["tekton.dev/pipeline"], expected["metadata"]["name"])
        bridge.check_run(expected, copy.deepcopy(expected))
        for key, value in (("tekton.dev/pipeline", "another-pipeline"), ("unexpected", "label")):
            changed = copy.deepcopy(expected); changed["metadata"]["labels"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): bridge.check_run(expected, changed)

    def test_uncertain_reservation_or_rejected_run_is_never_replayed(self):
        for failure in ("lock-before", "lock-after", "run-before"):
            self.client = Client(self.plan); self.client.fail = failure
            with self.subTest(failure=failure), self.assertRaises(ValueError): self.submit()
            # A confirmed absence after an uncertain reservation create does
            # not yet have a durable reservation. Stop; do not auto-retry it.
            if failure == "lock-before": continue
            self.client.fail = None
            with self.assertRaises(ValueError): self.submit()
            self.assertFalse(any(k.startswith("pipelinerun") for k in self.client.objects))

    def test_lost_run_or_done_acknowledgement_observes_original_uid(self):
        for failure in ("run-after", "done-after"):
            self.client = Client(self.plan); self.client.fail = failure
            self.assertTrue(self.submit()["accepted"])
            self.client.fail = None
            self.assertTrue(self.submit()["duplicate"])
            self.assertEqual(sum(o["kind"] == "PipelineRun" and "--dry-run=server" not in a for a, o in self.client.calls), 1)

    def test_admission_injection_stops_before_reservation(self):
        self.client.mutate = True
        with self.assertRaises(ValueError): self.submit()
        self.assertFalse(self.client.objects)

    def test_replacement_between_acknowledgement_and_observation_is_not_adopted(self):
        original = self.client.call
        def replaced(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "create" and "--dry-run=server" not in args:
                obj = json.loads(result)
                key = ("pipelinerun.tekton.dev/" if obj["kind"] == "PipelineRun" else "configmap/") + obj["metadata"]["name"]
                if obj["kind"] == "PipelineRun": self.client.objects[key]["metadata"]["uid"] = str(uuid4())
            return result
        with mock.patch.object(self.client, "call", side_effect=replaced), self.assertRaises(ValueError): self.submit()
        self.assertFalse(any(k.endswith("-done") for k in self.client.objects))

    def test_run_uid_and_fixed_spec_and_receipt_tampering_are_rejected(self):
        self.submit(); original = copy.deepcopy(self.client.objects)
        run = next(k for k in original if k.startswith("pipelinerun"))
        done = next(k for k in original if k.endswith("-done"))
        lock = next(k for k in original if k.endswith("-lock"))
        for key, field in ((run, "uid"), (run, "spec"), (done, "data"), (lock, "uid")):
            self.client.objects = copy.deepcopy(original)
            if field == "uid": self.client.objects[key]["metadata"]["uid"] = str(uuid4())
            elif field == "spec": self.client.objects[key]["spec"]["params"][0]["value"] = "f" * 40
            else: self.client.objects[key]["data"]["revision"] = "b" * 40
            with self.subTest(key=key, field=field), self.assertRaises(ValueError): self.submit()

    def test_pagination_is_bounded_complete_and_checked_before_writes(self):
        items = [row(change=1), {**row(change=2), "_more_changes": True}]
        source = mock.Mock(); source.get.side_effect = [items, [row(change=3)]]
        self.assertEqual(len(bridge.discover(source, self.plan)), 3)
        self.assertIn("S=2", source.get.call_args.args[0])
        small = {**self.plan, "max_changes": 2}
        source.get.side_effect = [items, [row(change=3)]]
        with self.assertRaises(ValueError): bridge.discover(source, small)
        source.get.side_effect = [items, [row(change=2)]]
        with self.assertRaises(ValueError): bridge.discover(source, self.plan)
        self.assertFalse(self.client.objects)

    def test_plan_rejects_ambient_targets_unpinned_images_and_dynamic_commands(self):
        for key, value in (("namespace_uid", "unknown"), ("build_image", "python:latest"), ("build_argv", ["echo", "$(params.inject)"]),
            ("namespace", "default"), ("poll_seconds", True), ("max_changes", 0), ("node_selector", {"kubernetes.io/os": "linux", "node-role.kubernetes.io/control-plane": "true"})):
            with self.subTest(key=key), self.assertRaises(ValueError): bridge.validate({**fixture(), key: value})
        for key, value in (("api_url", "https://user:secret@example.com"), ("clone_url", "https://other.example.com/repo.git"), ("project", "team/../other"), ("ca_sha256", "unknown")):
            bad = fixture(); bad["source"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): bridge.validate(bad)

    def test_five_formats_keep_kubernetes_explicit_and_build_credentials_isolated(self):
        for runtime in bridge.RUNTIMES:
            files = renderer.render(self.plan, runtime); status = json.loads(files["render-status.json"])
            for key in bridge.FALSE_FLAGS: self.assertIs(status[key], False)
            template = json.loads(files["run-template.json"])
            steps = template["spec"]["pipelineSpec"]["tasks"][0]["taskSpec"]["steps"]
            self.assertEqual(steps[0]["args"][-1], "$(params.patchset-ref)")
            self.assertFalse(any(v["name"] == "source" for v in steps[1]["volumeMounts"]))
            self.assertFalse(template["spec"]["taskRunTemplate"]["podTemplate"].get("automountServiceAccountToken", False))
            if runtime in {"rke2", "k3s", "k8s"}:
                workload = list(yaml.safe_load_all(files["bridge-workload.yaml"]))[1]
                self.assertEqual(workload["spec"]["replicas"], 2)
                self.assertFalse(workload["spec"]["template"]["spec"]["automountServiceAccountToken"])
            elif runtime == "docker": self.assertNotIn("ports", yaml.safe_load(files["compose.yaml"])["services"][self.plan["name"]])
            else: self.assertIn("--watch", files["platform-gerrit-tekton.service"])

    def test_altered_program_or_template_refused_before_runtime(self):
        (self.root / "run-template.json").write_text("{}")
        with self.assertRaises(ValueError): bridge.load_bundle(self.root, self.digest)

    @unittest.skipUnless(os.environ.get("TEKTON_GERRIT_TEST_API") == "1", "Explicit isolated Kubernetes API and pinned Tekton schema")
    def test_actual_kubernetes_api_submission_and_replay(self):
        from test_tekton_gerrit_api_fixture import exercise
        exercise(self)

    def test_patchset_checkout_fetches_ref_and_verifies_its_exact_commit(self):
        calls = []
        checkout.checkout(self.plan["source"]["clone_url"], "a" * 40, self.root / "repo", "ci-read", patchset_ref=self.item["ref"], runner=lambda args, **kw: calls.append((args, kw)))
        fetch = next(a for a, _ in calls if "fetch" in a)
        self.assertEqual(fetch[-1], self.item["ref"] + ":refs/platform/patchset")
        self.assertEqual(calls[2][1]["input"], ("verify refs/platform/patchset " + "a" * 40 + "\n").encode())
        for invalid in ("refs/heads/main", "refs/changes/24/123/2", "refs/changes/23/123/02", "--upload-pack=evil"):
            with self.assertRaises(ValueError): checkout.checkout(self.plan["source"]["clone_url"], "a" * 40, self.root / "unused", "ci-read", patchset_ref=invalid)

    @unittest.skipUnless(os.name == "posix", "Actual HTTPS Git fixture requires Linux")
    def test_actual_https_review_api_and_patchset_checkout(self):
        git, openssl = shutil.which("git"), shutil.which("openssl")
        if not git or not openssl: self.skipTest("Require Git and OpenSSL")
        def run(args, **kwargs): return run_bounded(args, timeout=30, text=True, output_max_bytes=1024 * 1024, check=True, **kwargs)
        source, repositories, inputs = self.root / "source", self.root / "repos", self.root / "inputs"
        source.mkdir(); repositories.mkdir(); inputs.mkdir(mode=0o700)
        run([git, "init", "-q", str(source)]); run([git, "-C", str(source), "config", "user.name", "Fixture"])
        run([git, "-C", str(source), "config", "user.email", "fixture@example.invalid"])
        (source / "test_application.py").write_text("import unittest\nclass App(unittest.TestCase):\n def test_result(self): self.assertEqual(2+2,4)\n")
        run([git, "-C", str(source), "add", "."]); run([git, "-C", str(source), "commit", "-qm", "Fixture"])
        commit = run([git, "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
        (repositories / "team").mkdir()
        bare = repositories / "team/application.git"
        run([git, "clone", "--bare", "-q", str(source), str(bare)])
        ref = "refs/changes/23/123/2"
        run([git, "--git-dir=" + str(bare), "update-ref", ref, commit])
        run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=local-test", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(inputs / "tls.key"), "-out", str(inputs / "ca.crt")])
        token = uuid4().hex
        for name in ("token", "api-token"): (inputs / name).write_text(token); (inputs / name).chmod(0o600)
        (inputs / "ca.crt").chmod(0o600)
        expected = "Basic " + base64.b64encode(("ci-read:" + token).encode()).decode()
        observed = []; response_row = row(commit=commit)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def handle_request(self):
                if self.headers.get("Authorization") != expected:
                    self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="fixture"'); self.send_header("Content-Length", "0"); self.end_headers(); return
                observed.append(self.command)
                path = urlsplit(self.path)
                if path.path.startswith("/a/changes/"):
                    body = b")]}'\n" + json.dumps([response_row] if path.path == "/a/changes/" else response_row).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                size = int(self.headers.get("Content-Length", "0"))
                if size > 1024 * 1024: raise ValueError("Fixture input too large")
                env = {**os.environ, "GIT_PROJECT_ROOT": str(repositories), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path.path.removeprefix("/a"),
                    "QUERY_STRING": path.query, "REQUEST_METHOD": self.command, "REMOTE_USER": "ci-read", "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(size)}
                result = run_bounded([git, "http-backend"], input=self.rfile.read(size), env=env, timeout=30, output_max_bytes=4 * 1024 * 1024, check=True)
                headers, body = result.stdout.split(b"\r\n\r\n", 1)
                fields = [line.decode("ascii").split(":", 1) for line in headers.split(b"\r\n")]
                self.send_response(next((int(v.strip().split()[0]) for k, v in fields if k.lower() == "status"), 200))
                for key, value in fields:
                    if key.lower() != "status": self.send_header(key, value.strip())
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            do_GET = handle_request
            do_POST = handle_request
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(inputs / "ca.crt", inputs / "tls.key")
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                plan = fixture(); origin = "https://127.0.0.1:" + str(server.server_port)
                plan["source"].update(api_url=origin, clone_url=origin + "/a/team/application.git", ca_sha256=hashlib.sha256((inputs / "ca.crt").read_bytes()).hexdigest())
                api = bridge.Gerrit(plan, inputs)
                item = bridge.discover(api, plan)[0]; bridge.verify_current(api, plan, item)
                workspace = self.root / "checkout"
                checkout.checkout(plan["source"]["clone_url"], commit, workspace, "ci-read", patchset_ref=ref, credentials=inputs)
                self.assertEqual(run([git, "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip(), commit)
                run([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(workspace)])
                self.assertNotIn(token, (workspace / ".git/config").read_text())
                with self.assertRaises(Exception): checkout.checkout(plan["source"]["clone_url"], "b" * 40, self.root / "wrong", "ci-read", patchset_ref=ref, credentials=inputs)
                self.assertFalse((self.root / "wrong/test_application.py").exists())
                self.assertGreaterEqual(len(observed), 4)
                plan["source"]["ca_sha256"] = "0" * 64
                with self.assertRaises(ValueError): bridge.Gerrit(plan, inputs)
            finally: server.shutdown(); thread.join(timeout=5)


if __name__ == "__main__": unittest.main()
