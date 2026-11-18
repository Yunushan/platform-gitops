#!/usr/bin/env python3
"""Actual disposable API/RBAC/CRD writes; no Gerrit server, controller or worker."""

import copy
import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import yaml

from bounded_file import read_bounded_bytes
from kubernetes_management import Client
import render_tekton_forge_bridge as common_renderer
import tekton_gerrit_bridge as bridge
from test_argocd_identity_api_fixture import await_crd_conditions
from test_rook_api_fixture import exercise as api_fixture
from test_tekton_gerrit_bridge import fixture, package, Source


CRD_SHA = "1f7a7d78a0fa4019cdd262aaa6d6260752fd60f8bf10e526959d2989fa79ee35"


def exercise(test, *, reporter_check=False):
    repository = Path(__file__).resolve().parents[1]
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repository / "scripts").glob("*.py")}
    schema_path = Path(os.environ["TEKTON_GERRIT_TEST_CRD"])
    raw = read_bounded_bytes(schema_path, max_bytes=1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != CRD_SHA: raise ValueError("Use the reviewed upstream Tekton v1.4.0 PipelineRun schema")
    definition = yaml.safe_load(raw)
    # Use the original v1 structural/CEL schema. This fixture has no webhook
    # conversion server and never submits beta resources.
    definition["spec"]["versions"] = [v for v in definition["spec"]["versions"] if v["name"] == "v1"]
    definition["spec"]["conversion"] = {"strategy": "None"}
    definition["spec"]["versions"][0]["storage"] = True

    def action(admin, root, kubectl):
        original = kubectl(["create", "-f", "-", "-o", "json"], definition)
        await_crd_conditions(admin, original)
        admin.call(["wait", "--for=condition=Established", "crd/" + original["metadata"]["name"], "--timeout=30s"])
        plan = fixture()
        plan["management_target"] = {"kubeconfig_env": "DISPOSABLE_CI_CONFIG", "context": "storage", "cluster_uid": admin.get("ns/kube-system")["metadata"]["uid"]}
        for name in (plan["namespace"], plan["submitter_namespace"]):
            created = kubectl(["create", "-f", "-", "-o", "json"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
            if name == plan["namespace"]: plan["namespace_uid"] = created["metadata"]["uid"]
        for obj in common_renderer.rbac(plan): kubectl(["create", "-f", "-", "-o", "json"], obj)
        token = admin.call(["create", "token", plan["submitter_account"], "--namespace", plan["submitter_namespace"], "--duration=10m"]).strip()
        config = json.loads(admin.call(["config", "view", "--minify", "--flatten", "--raw", "-o", "json"]))
        config["users"][0]["user"] = {"token": token}
        config_file = root / "ci-kubeconfig.json"; config_file.write_text(json.dumps(config)); config_file.chmod(0o600)
        bundle = root / "gerrit-package"; bundle.mkdir(mode=0o700)
        contract, template, digest = package(bundle, plan)
        source = Source(); item = bridge.current(plan, source.row)
        expected, data = bridge.run_for(contract, template, digest, item)
        class LostAck:
            def __init__(self, client): self.client = client; self.writes = 0
            def get(self, *args, **kwargs): return self.client.get(*args, **kwargs)
            def call(self, args, **kwargs):
                value = self.client.call(args, **kwargs)
                if args[:1] == ["create"] and "--dry-run=server" not in args:
                    obj = json.loads(kwargs["payload"])
                    if obj["kind"] == "PipelineRun":
                        self.writes += 1
                        if self.writes == 1: raise ValueError("Lost acknowledgement after original API create")
                return value
        with mock.patch.dict(os.environ, {"DISPOSABLE_CI_CONFIG": str(config_file)}):
            with Client(plan["management_target"]).frozen() as scoped:
                client = LostAck(scoped)
                result = bridge.submit(client, source, contract, template, digest, item)
                test.assertTrue(result["accepted"])
                original_run = scoped.get("pipelinerun.tekton.dev/" + data["run"], plan["namespace"])
                test.assertTrue(bridge.identifier(original_run["metadata"]["uid"]))
                test.assertTrue(bridge.submit(client, source, contract, template, digest, item)["duplicate"])
                test.assertEqual(client.writes, 1)
                # Actual restricted submitter may neither inspect Secrets nor
                # delete runs or write arbitrary control-namespace objects.
                for args in (["get", "secrets", "--namespace", plan["namespace"], "-o", "json"],
                    ["delete", "pipelinerun.tekton.dev/" + data["run"], "--namespace", plan["namespace"]],
                    ["create", "configmap", "forbidden", "--from-literal=key=value", "--namespace", plan["submitter_namespace"]]):
                    with test.assertRaises(ValueError): scoped.call(args)
                if reporter_check:
                    import render_tekton_gerrit_reporter as report_renderer
                    import tekton_gerrit_reporter as report
                    from test_tekton_gerrit_reporter import Gerrit, label
                    report_plan = {"schema_version": 1, "source_contract_sha256": digest, "account": "gerrit-result-reporter", "secret": "gerrit-result-inputs",
                        "http_username": "ci-review", "account_id": 9, "label_definition_sha256": report.fingerprint(label()),
                        "helper_images": ["registry.example.com/entrypoint@sha256:" + "d" * 64], "poll_seconds": 10, "max_runs_per_poll": 5}
                    outputs = report_renderer.render(bundle, report_plan)
                    report_digest = json.loads(outputs["reporter-status.json"])["contract_sha256"]
                    for obj in yaml.safe_load_all(outputs["reporter-rbac.yaml"]): kubectl(["create", "-f", "-", "-o", "json"], obj)
                    report_token = admin.call(["create", "token", report_plan["account"], "--namespace", plan["submitter_namespace"], "--duration=10m"]).strip()
                    report_config = copy.deepcopy(config); report_config["users"][0]["user"] = {"token": report_token}
                    report_path = root / "reporter-kubeconfig.json"; report_path.write_text(json.dumps(report_config)); report_path.chmod(0o600)
                    with mock.patch.dict(os.environ, {"DISPOSABLE_CI_CONFIG": str(report_path)}):
                        with Client(plan["management_target"]).frozen() as observer:
                            for args, payload in ((["get", "secrets", "--namespace", plan["namespace"], "-o", "json"], None),
                                (["delete", "pipelinerun.tekton.dev/" + data["run"], "--namespace", plan["namespace"]], None),
                                (["create", "-f", "-", "-o", "json"], bridge.common.canonical({**expected, "metadata": {**expected["metadata"], "name": "forbidden"}}))):
                                with test.assertRaises(ValueError): observer.call(args, payload=payload)
                            # Deliberately simulated failed controller status.
                            # There is no successful TaskRun/Pod or worker here.
                            admin.call(["patch", "pipelinerun.tekton.dev/" + data["run"], "--namespace", plan["namespace"], "--subresource=status", "--type=merge", "-p",
                                bridge.common.canonical({"status": {"conditions": [{"type": "Succeeded", "status": "False"}], "completionTime": "2026-11-06T10:10:00Z"}}).decode()])
                            failed = observer.get("pipelinerun.tekton.dev/" + data["run"], plan["namespace"])
                            fake_api = Gerrit()
                            summary, _ = report.poll(observer, fake_api, contract, template, digest, report_plan, report_digest)
                            test.assertEqual(summary["reported"], 1)
                            test.assertEqual(report.report_run(observer, fake_api, contract, template, digest, report_plan, report_digest, failed), "already-reported")
                            test.assertEqual(len(fake_api.posts), 1); test.assertEqual(fake_api.posts[0][1]["labels"], {"Verified": -1})
                # Replace the run through the fixture admin; the production
                # submitter must reject the new UID rather than adopting it.
                admin.call(["delete", "pipelinerun.tekton.dev/" + data["run"], "--namespace", plan["namespace"]])
                replacement = kubectl(["create", "-f", "-", "-o", "json"], expected)
                test.assertNotEqual(replacement["metadata"]["uid"], original_run["metadata"]["uid"])
                with test.assertRaises(ValueError): bridge.submit(client, source, contract, template, digest, item)
                admin.call(["delete", "pipelinerun.tekton.dev/" + data["run"], "--namespace", plan["namespace"]])
                test.assertFalse(bridge.submit(client, source, contract, template, digest, item)["run_retained"])
                test.assertIsNone(scoped.get("pipelinerun.tekton.dev/" + data["run"], plan["namespace"], optional=True))
                test.assertEqual(client.writes, 1)
        return {"real_api_admission_verified": True, "lost_ack_original_uid_verified": True, "retained_receipt_deduplication_verified": True,
            "separate_reporter_rbac_verified": reporter_check, "simulated_failure_vote_surrogate_only": reporter_check,
            "scoped_rbac_verified": True, "replaced_run_refused": True, "garbage_collected_run_not_recreated": True, **{k: False for k in bridge.FALSE_FLAGS}}
    result = api_fixture(test, management_action=action)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repository / "scripts").glob("*.py")}
    if before != after: raise ValueError("Source set changed during the API acceptance")
    report = os.environ.get("TEKTON_GERRIT_TEST_REPORT")
    if report:
        path = Path(report).resolve(); path.relative_to((repository / "private").resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream: json.dump({**result, "source_files_sha256": before, "tekton_schema_sha256": CRD_SHA}, stream, sort_keys=True)
    return result
