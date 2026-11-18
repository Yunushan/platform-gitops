#!/usr/bin/env python3
"""Actual Gerrit discovery, scoped Tekton submission, worker build and vote."""

import base64
import copy
import hashlib
import json
import os
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import render_gerrit_promotion_access as promotion_access
import render_tekton_gerrit_bridge as submission_renderer
import render_tekton_gerrit_reporter as result_renderer
import tekton_gerrit_bridge as bridge
import tekton_gerrit_reporter as reporter
from kubernetes_management import Client
from test_gerrit_review_runtime_fixture import prepare, server
from test_tekton_gerrit_bridge import fixture as source_plan
from test_tekton_worker_runtime_fixture import PYTHON_IMAGE

APPLICATION_TEST = '''import os
from pathlib import Path
import unittest

class WorkerIsolation(unittest.TestCase):
    def test_build_has_no_checkout_or_api_credentials(self):
        self.assertEqual(os.geteuid(), 1000)
        self.assertFalse(Path('/run/source/token').exists())
        self.assertFalse(Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists())
        status = Path('/proc/self/status').read_text()
        self.assertRegex(status, r'NoNewPrivs:\\s+1')
        self.assertRegex(status, r'Seccomp:\\s+2')
'''


def failure_tasks(manager,namespace):
    try:return json.loads(manager.call(['get','taskruns.tekton.dev','-n',namespace,'-o','json']))['items']
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print(json.dumps({'gerrit_worker_diagnostic_capture_failed':type(error).__name__}),flush=True)
        return []


def exercise(test, context, *, argocd_check=False, native_watch=False, publication_context=None):
    promotion = publication_context is not None and publication_context.get("promotion") is True
    if publication_context is not None and (argocd_check or native_watch and not (promotion and publication_context.get("native_promoter") is True)):
        raise ValueError("Artifact watches require the explicit merged artifact promotion case")
    manager, workspace = context["client"], context["root"]
    stage = "native-gerrit-bootstrap"
    print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
    report = {}
    with server(handoff=argocd_check or promotion, worker_context=context, active_processors=2 if publication_context is not None else None) as gerrit, ExitStack() as services:
        api, root = gerrit["api"], gerrit["root"]
        def write(directory, name, value):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / name
            path.write_bytes(value if isinstance(value, bytes) else value.encode())
            path.chmod(0o600)
            return path
        def create(obj): return json.loads(manager.call(["create", "-f", "-", "-o", "json"], payload=bridge.common.canonical(obj)))
        def apply(payload): manager.call(["apply", "--server-side", "--field-manager=fixture-review-identities", "-f", "-"], payload=payload.encode())
        prepared = prepare(gerrit, handoff=argocd_check, fast_forward_only=promotion)
        number = prepared["change_number"]
        api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/test_worker_isolation.py", APPLICATION_TEST.encode(), method="PUT", content_type="text/plain")
        if argocd_check:
            from urllib.parse import quote

            from test_argocd_worker_workload import application_test, manifest
            for name, content in (("deploy/staging/web.json", json.dumps(manifest()).encode()), ("test_deployment_input.py", application_test())):
                api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/" + quote(name, safe=""), content, method="PUT", content_type="text/plain")
        if publication_context is not None:
            from test_gerrit_harbor_worker_fixture import BUILD
            prefix = "import unittest\nresult=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('/workspace/source'))\nif not result.wasSuccessful(): raise SystemExit(1)\n"
            api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/build.py", (prefix + BUILD).encode(), method="PUT", content_type="text/plain")
        if promotion:
            from urllib.parse import quote
            for path, content in publication_context["source_files"].items():
                api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/" + quote(path, safe=""), content, method="PUT", content_type="text/plain")
        api.call("fixture-admin", "/a/changes/" + str(number) + "/edit:publish", {"notify": "NONE"})
        plan = source_plan()
        plan.update(management_target={**context["target"], "kubeconfig_env": "DISPOSABLE_GERRIT_CI_KUBECONFIG"},
            checkout_image=PYTHON_IMAGE, build_image=PYTHON_IMAGE, bridge_image=PYTHON_IMAGE,
            build_argv=["python3", "-I", "-B", "-m", "unittest", "discover"])
        plan["source"].update(api_url=gerrit["origin"], clone_url=gerrit["origin"] + "/a/" + prepared["project"] + ".git",
            project=prepared["project"], ca_sha256=hashlib.sha256((gerrit["tls"] / "ca.crt").read_bytes()).hexdigest())
        if publication_context is not None:
            plan.update(schema_version=2, publication=publication_context["publication"], build_argv=["python3", "-I", "-B", "/workspace/source/build.py"])
        for namespace in (plan["namespace"], plan["submitter_namespace"]):
            obj = create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
            if namespace == plan["namespace"]: plan["namespace_uid"] = obj["metadata"]["uid"]
        reader = root / "reader"
        for name, content in (("api-token", gerrit["users"]["ci-read"]), ("ca.crt", (gerrit["tls"] / "ca.crt").read_bytes())): write(reader, name, content)
        source = bridge.Gerrit(plan, reader)
        items = bridge.discover(source, plan); test.assertEqual(len(items), 1); item = items[0]
        test.assertEqual(item["change"], number)
        create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": plan["read_secret"], "namespace": plan["namespace"]}, "immutable": True,
            "data": {"token": base64.b64encode(gerrit["users"]["ci-read"].encode()).decode(), "ca.crt": base64.b64encode((gerrit["tls"] / "ca.crt").read_bytes()).decode()}})
        if publication_context is not None:
            for name, data in ((plan["publication"]["robot_secret"], {"credential.json": publication_context["credential"]}),
                    (plan["publication"]["trust_secret"], {"ca.crt": publication_context["ca"]})):
                create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": plan["namespace"]}, "immutable": True,
                    "data": {k: base64.b64encode(v).decode() for k,v in data.items()}})
        bundle = root / "submission"; bundle.mkdir(mode=0o700)
        # Native submission/reporting helpers use the explicit disposable K3s
        # management target; their container workload is not deployed here.
        files = submission_renderer.render(plan, "raw")
        for name, value in files.items(): write(bundle, name, value)
        digest = json.loads(files["render-status.json"])["contract_sha256"]
        contract, template = bridge.load_bundle(bundle, digest)
        apply(files["bridge-rbac.yaml"])
        labels = api.call("fixture-admin", "/a/projects/team%2Fapplication/labels/?inherited")
        definition = [label for label in labels if label.get("name") == "Verified"]
        test.assertEqual(len(definition), 1)
        report_plan = {"schema_version": 1, "source_contract_sha256": digest, "account": "gerrit-result-reporter", "secret": "gerrit-result-inputs",
            "http_username": "ci-review", "account_id": prepared["users"]["ci-review"]["_account_id"], "label_definition_sha256": reporter.fingerprint(definition[0]),
            "helper_images": context["helper_images"], "poll_seconds": 10, "max_runs_per_poll": 5}
        output = root / "reporter"; output.mkdir(mode=0o700)
        result_files = result_renderer.render(bundle, report_plan)
        for name, value in result_files.items(): write(output, name, value)
        report_digest = json.loads(result_files["reporter-status.json"])["contract_sha256"]
        reporter.load_bundle(output, report_digest)
        apply(result_files["reporter-rbac.yaml"])
        def scoped_client(account):
            from test_k3s_vm_fixture import guest_identity_lifetime
            token = manager.call(["create", "token", account, "-n", plan["submitter_namespace"], "--duration="+guest_identity_lifetime(context)]).strip()
            config = copy.deepcopy(json.loads((workspace / "kubeconfig.json").read_text()))
            config["users"] = [{"name": "fixture", "user": {"token": token}}]
            config["contexts"][0]["context"]["user"] = "fixture"
            path = write(root / "identities", account + ".json", json.dumps(config))
            with mock.patch.dict(os.environ, {plan["management_target"]["kubeconfig_env"]: str(path)}): return Client(plan["management_target"])
        submitter, reviewer = scoped_client(plan["submitter_account"]), scoped_client(report_plan["account"])
        promoter_ci = reviewer
        if argocd_check or promotion:
            from test_k3s_vm_fixture import application_workers
            reader_account = "gerrit-promotion-ci-reader"
            selected = {"account": reader_account, "namespace": plan["submitter_namespace"],
                        "workers": [n["name"] for n in application_workers(context)]}
            for obj in promotion_access.ci_objects(plan, selected, report_plan["account"]): create(obj)
            promoter_ci = scoped_client(reader_account)
            for attributes in ({"verb": "create", "resource": "configmaps"}, {"verb": "patch", "group": "tekton.dev", "resource": "pipelineruns"},
                    {"verb": "patch", "group": "tekton.dev", "resource": "pipelineruns", "subresource": "status"}, {"verb": "get", "resource": "secrets"},
                    {"verb": "list", "group": "tekton.dev", "resource": "pipelineruns"},
                    {"verb": "get", "resource": "configmaps", "name": "outside-job-scope", "namespace": plan["submitter_namespace"]},
                    {"verb": "get", "resource": "nodes", "name": "outside-reviewed-worker-inventory", "namespace": ""}):
                denied = json.loads(promoter_ci.call(["create", "--raw", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "-f", "-"],
                    payload=bridge.common.canonical({"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview", "spec": {"resourceAttributes": {"namespace": plan["namespace"], **attributes}}})))
                test.assertIs(denied["status"]["allowed"], False); test.assertFalse(denied["status"].get("evaluationError"))
            with test.assertRaises(ValueError):
                promoter_ci.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=bridge.common.canonical({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "promoter-write-denied", "namespace": plan["namespace"]}}))
            with test.assertRaises(ValueError): promoter_ci.call(["get", "secret/" + plan["read_secret"], "-n", plan["namespace"], "-o", "json"])
        for client in (submitter, reviewer):
            # This virtual authorization endpoint has no normal CRUD schema.
            # Avoid kubectl's generic CRD-discovery fallback; do not grant the
            # scoped identities cluster-wide CRD listing just to ask this check.
            access = json.loads(client.call(["create", "--raw", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "-f", "-"], payload=bridge.common.canonical({"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"namespace": plan["namespace"], "verb": "get", "resource": "secrets", "name": plan["read_secret"]}}})))
            test.assertIs(access["status"]["allowed"], False); test.assertFalse(access["status"].get("evaluationError"))
            with test.assertRaises(ValueError): client.call(["get", "secret/" + plan["read_secret"], "-n", plan["namespace"], "-o", "json"])
        stage = "scoped-real-pipeline-submission"
        print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
        expected, _ = bridge.run_for(contract, template, digest, item)
        admitted = json.loads(submitter.call(["create", "--dry-run=server", "-f", "-", "-o", "json"], payload=bridge.common.canonical(expected)))
        try: bridge.check_run(expected, admitted)
        except ValueError:
            paths = []
            def differences(left, right, path="spec"):
                if len(paths) >= 60: return
                if type(left) is not type(right): paths.append(path); return
                if isinstance(left, dict):
                    for key in sorted(set(left) | set(right)):
                        if key not in left or key not in right: paths.append(path + "." + key)
                        else: differences(left[key], right[key], path + "." + key)
                elif isinstance(left, list):
                    if len(left) != len(right): paths.append(path + ".length")
                    for index, (a, b) in enumerate(zip(left, right)): differences(a, b, path + "[" + str(index) + "]")
                elif left != right: paths.append(path)
            differences(expected["spec"], admitted.get("spec"))
            print(json.dumps({"actual_admission_changed_field_paths": paths[:60]}), flush=True)
            embedded = admitted.get("spec", {}).get("pipelineSpec", {}).get("tasks", [{}])[0].get("taskSpec", {})
            duration = admitted.get("spec", {}).get("timeouts", {}).get("pipeline", "")
            print(json.dumps({"actual_empty_task_metadata": embedded.get("metadata") == {},
                "actual_null_custom_task_spec": "spec" in embedded and embedded["spec"] is None,
                "actual_pipeline_duration": duration if isinstance(duration, str) and re.fullmatch(r"[0-9hms.]{1,32}", duration) else "unrecognized"}), flush=True)
            raise
        if native_watch:
            from test_gerrit_native_watch_fixture import WatchPair
            native_inputs = {}
            for kind, client, login in (("bridge", submitter, "ci-read"), ("reporter", reviewer, "ci-review")):
                path = root / (kind + "-native-inputs")
                for name, value in (("api-token", gerrit["users"][login]), ("ca.crt", (gerrit["tls"] / "ca.crt").read_bytes()), ("kubeconfig", Path(client.prefix[2]).read_bytes())):
                    write(path, name, value)
                native_inputs[kind] = path
            watches = services.enter_context(WatchPair(test, context, root, {"bridge": bundle, "reporter": output}, native_inputs,
                {"bridge": digest, "reporter": report_digest}, {"bridge": files["platform-gerrit-tekton.service"], "reporter": result_files["platform-gerrit-tekton-reporter.service"]}))
            def native_submitted():
                watches.inspect("bridge"); watches.inspect("reporter")
                current = submitter.get("pipelinerun.tekton.dev/" + expected["metadata"]["name"], plan["namespace"], optional=True)
                recorded = submitter.get("configmap/" + expected["metadata"]["name"] + "-done", plan["namespace"], optional=True)
                if current and recorded:
                    bridge.check_run(expected, current)
                    test.assertEqual(recorded["data"]["uid"], current["metadata"]["uid"])
                    return current
            run = context["wait"](native_submitted, seconds=60)
            original_uid = run["metadata"]["uid"]
        else:
            try: submitted = bridge.submit(submitter, source, contract, template, digest, item)
            except ValueError:
                stored = submitter.get("pipelinerun.tekton.dev/" + expected["metadata"]["name"], plan["namespace"], optional=True)
                if stored:
                    metadata = stored["metadata"]
                    print(json.dumps({"stored_run_label_keys": sorted(metadata.get("labels", {})), "stored_run_annotation_keys": sorted(metadata.get("annotations", {})),
                        "stored_controller_pipeline_label_matches": metadata.get("labels", {}).get("tekton.dev/pipeline") == expected["metadata"]["name"],
                        "stored_owner_references_present": bool(metadata.get("ownerReferences")), "stored_deletion_requested": bool(metadata.get("deletionTimestamp"))}), flush=True)
                raise
            test.assertFalse(submitted["duplicate"])
            run = submitter.get("pipelinerun.tekton.dev/" + expected["metadata"]["name"], plan["namespace"])
            original_uid = run["metadata"]["uid"]
            test.assertTrue(bridge.submit(submitter, source, contract, template, digest, item)["duplicate"])
        stage = "actual-patchset-checkout-and-build"
        print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
        def completed():
            actual = reviewer.get("pipelinerun.tekton.dev/" + run["metadata"]["name"], plan["namespace"])
            if actual["metadata"]["uid"] != original_uid: raise AssertionError("Original pipeline was replaced")
            state = reporter.worker.terminal(actual)
            if state == "failure": raise AssertionError("Actual Gerrit patch-set build failed")
            return actual if state == "success" else None
        try:
            run = context["wait"](completed, seconds=240)
            task = reviewer.get("taskrun.tekton.dev/" + run["status"]["childReferences"][0]["name"], plan["namespace"])
            pod_name = task["status"]["podName"]
            task_uid = task["metadata"]["uid"]
            pod_uid = reviewer.get("pod/" + pod_name, plan["namespace"])["metadata"]["uid"]
            def worker_finalized():
                current_task = reviewer.get("taskrun.tekton.dev/" + run["status"]["childReferences"][0]["name"], plan["namespace"])
                current_pod = reviewer.get("pod/" + pod_name, plan["namespace"])
                if current_task["metadata"]["uid"] != task_uid or current_pod["metadata"]["uid"] != pod_uid:
                    raise AssertionError("Original application TaskRun or worker was replaced")
                if current_pod.get("status", {}).get("phase") == "Failed": raise AssertionError("Original application worker failed")
                return current_pod if current_pod.get("status", {}).get("phase") == "Succeeded" else None
            context["wait"](worker_finalized, seconds=60)
            _, _, evidence = reporter.evidence(reviewer, contract, template, digest, run, report_plan)
            build_log = manager.call(["logs", "pod/" + pod_name, "-c", "step-build", "-n", plan["namespace"]])
            test.assertIn("Ran " + str(3 if argocd_check or promotion else 2) + " tests", build_log); test.assertIn("OK", build_log)
            for token in gerrit["users"].values(): test.assertNotIn(token, build_log)
            stage = "actual-evidenced-gerrit-vote"
            print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
            inputs = root / "review-inputs"
            write(inputs, "api-token", gerrit["users"]["ci-review"]); write(inputs, "ca.crt", (gerrit["tls"] / "ca.crt").read_bytes())
            destination = reporter.GerritReview(plan, report_plan, inputs)
            if native_watch:
                def native_vote(current_run):
                    watches.inspect("bridge"); watches.inspect("reporter")
                    recorded = reviewer.get("configmap/" + current_run["metadata"]["name"] + "-vote", plan["namespace"], optional=True)
                    if not recorded: return None
                    test.assertEqual(reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, current_run), "already-reported")
                    return recorded
                context["wait"](lambda: native_vote(run), seconds=60)
            else:
                test.assertEqual(reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, run), "reported")
            test.assertEqual(reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, run), "already-reported")
            obj = reporter.review(destination, plan, item)
            votes = [v for v in obj["labels"]["Verified"]["all"] if v.get("_account_id") == report_plan["account_id"]]
            test.assertEqual(len(votes), 1); test.assertEqual(votes[0]["value"], 1)
            if native_watch:
                watches.wait_report("bridge", "accepted"); watches.wait_report("reporter", "reported")
                watches.restart("bridge"); watches.restart("reporter")
                checkpoints = {kind: len(watches.reports(kind)) for kind in ("bridge", "reporter")}
                watches.wait_report("bridge", "duplicates", after=checkpoints["bridge"])
                watches.wait_report("reporter", "already-reported", after=checkpoints["reporter"])
                test.assertEqual(submitter.get("pipelinerun.tekton.dev/" + run["metadata"]["name"], plan["namespace"])["metadata"]["uid"], original_uid)
                inventory = json.loads(manager.call(["get", "pipelineruns.tekton.dev", "-n", plan["namespace"], "-o", "json"]))["items"]
                test.assertEqual(len(inventory), 1)
            # Restart/replay must be proven while the reviewed change is open.
            # The explicit artifact callback may then merge and promote it.
            if publication_context is not None:
                publication_context["execution"] = {"contract": contract, "template": template, "digest": digest,
                    "report_plan": report_plan, "report_digest": report_digest, "reporter_bundle": output,
                    "promoter_ci": promoter_ci, "prepared": prepared, "watch_pair": watches if native_watch else None}
                publication_context["result"] = publication_context["on_success"](plan, run, task, evidence, context, gerrit, item)
            if promotion:
                report = {"real_gerrit_patchset_worker_build_verified": True, "gerrit_review_feedback_verified": True,
                    "scoped_submitter_and_reporter_secret_reads_denied": True, "real_application_tests_completed": 3,
                    "build_checkout_and_api_credential_isolation_verified": True, "replayed_submission_not_duplicated": True,
                    "replayed_result_not_reposted": True, "modeled_ci_controller_worker_evidence": False,
                    "gerrit_worker_evidence": evidence, "native_bridge_workload_deployed": False,
                    "continuous_event_delivery_verified": False, "promoter_ci_write_denied": True,
                    "promoter_ci_secret_read_denied": True, "promoter_ci_out_of_scope_reads_denied": True}
            elif argocd_check:
                from test_gerrit_review_runtime_fixture import handoff_acceptance
                stage = "actual-worker-ci-merged-serving-handoff"
                print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
                cd_result = handoff_acceptance(test, gerrit, prepared, plan, contract, template, report_plan, report_digest,
                    promoter_ci, run, argocd_check=True, worker_context=context, reporter_bundle=output, native_cli=native_watch, watch_pair=watches if native_watch else None)
                report = {**cd_result, "real_gerrit_patchset_worker_build_verified": True, "gerrit_review_feedback_verified": True,
                    "scoped_submitter_and_reporter_secret_reads_denied": True, "real_application_tests_completed": 3,
                    "build_checkout_and_api_credential_isolation_verified": True, "replayed_submission_not_duplicated": True,
                    "replayed_result_not_reposted": True, "modeled_ci_controller_worker_evidence": False,
                    "gerrit_worker_evidence": evidence, "native_bridge_workload_deployed": False,
                    "continuous_event_delivery_verified": False, "real_worker_ci_to_argocd_serving_handoff_verified": True,
                    "promoter_ci_write_denied": True, "promoter_ci_secret_read_denied": True, "promoter_ci_out_of_scope_reads_denied": True}
            else:
                if not native_watch:
                    api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/untested.txt", b"A replacement patch set without CI.\n", method="PUT", content_type="text/plain")
                    api.call("fixture-admin", "/a/changes/" + str(number) + "/edit:publish", {"notify": "NONE"})
                    with test.assertRaises(ValueError): reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, run)
                    current = api.call("fixture-admin", "/a/changes/" + str(number) + "/detail?o=CURRENT_REVISION&o=DETAILED_LABELS")
                    test.assertFalse(any(v.get("_account_id") == report_plan["account_id"] and v.get("value") == 1 for v in current["labels"].get("Verified", {}).get("all", [])))
                stage = "actual-failing-patchset-build"
                print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
                api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/test_failure.py",
                    b"import unittest\nclass DeliberateFailure(unittest.TestCase):\n    def test_rejected_result(self):\n        self.fail('Deliberate fixture failure')\n",
                    method="PUT", content_type="text/plain")
                api.call("fixture-admin", "/a/changes/" + str(number) + "/edit:publish", {"notify": "NONE"})
                failed_items = bridge.discover(source, plan); test.assertEqual(len(failed_items), 1)
                failed_item = failed_items[0]
                test.assertNotEqual(failed_item["revision"], item["revision"])
                failed_expected, _ = bridge.run_for(contract, template, digest, failed_item)
                if native_watch:
                    with test.assertRaises(ValueError): reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, run)
                    current = api.call("fixture-admin", "/a/changes/" + str(number) + "/detail?o=CURRENT_REVISION&o=DETAILED_LABELS")
                    test.assertFalse(any(v.get("_account_id") == report_plan["account_id"] and v.get("value") == 1 for v in current["labels"].get("Verified", {}).get("all", [])))
                    failed_run = context["wait"](lambda: submitter.get("pipelinerun.tekton.dev/" + failed_expected["metadata"]["name"], plan["namespace"], optional=True), seconds=60)
                    bridge.check_run(failed_expected, failed_run)
                else:
                    test.assertFalse(bridge.submit(submitter, source, contract, template, digest, failed_item)["duplicate"])
                    failed_run = submitter.get("pipelinerun.tekton.dev/" + failed_expected["metadata"]["name"], plan["namespace"])
                failed_uid = failed_run["metadata"]["uid"]
                test.assertNotEqual(failed_uid, original_uid)
                def failed_completed():
                    actual = reviewer.get("pipelinerun.tekton.dev/" + failed_expected["metadata"]["name"], plan["namespace"])
                    if actual["metadata"]["uid"] != failed_uid: raise AssertionError("Original failed pipeline was replaced")
                    state = reporter.worker.terminal(actual)
                    if state == "success": raise AssertionError("Deliberately failing application unexpectedly succeeded")
                    return actual if state == "failure" else None
                failed_run = context["wait"](failed_completed, seconds=240)
                failed_task = reviewer.get("taskrun.tekton.dev/" + failed_run["status"]["childReferences"][0]["name"], plan["namespace"])
                test.assertEqual(reporter.worker.terminal(failed_task), "failure")
                test.assertTrue(reporter.worker.owned(failed_task, "PipelineRun", failed_uid))
                failed_task_uid = failed_task["metadata"]["uid"]
                failed_pod_name = failed_task["status"]["podName"]
                failed_pod_uid = reviewer.get("pod/" + failed_pod_name, plan["namespace"])["metadata"]["uid"]
                def failed_worker_finalized():
                    actual_task = reviewer.get("taskrun.tekton.dev/" + failed_task["metadata"]["name"], plan["namespace"])
                    actual_pod = reviewer.get("pod/" + failed_pod_name, plan["namespace"])
                    if actual_task["metadata"]["uid"] != failed_task_uid or actual_pod["metadata"]["uid"] != failed_pod_uid:
                        raise AssertionError("Original failed TaskRun or worker was replaced")
                    test.assertTrue(reporter.worker.owned(actual_pod, "TaskRun", failed_task_uid))
                    if actual_pod.get("status", {}).get("phase") == "Succeeded": raise AssertionError("Failed application worker unexpectedly succeeded")
                    return actual_pod if actual_pod.get("status", {}).get("phase") == "Failed" else None
                failed_pod = context["wait"](failed_worker_finalized, seconds=60)
                failed_steps = {s["name"]: s.get("terminated", {}).get("exitCode") for s in failed_task["status"]["steps"]}
                test.assertEqual(failed_steps, {"checkout": 0, "build": 1, **({"publish": 1} if publication_context is not None else {})})
                if publication_context is not None:
                    publication = next(v for v in failed_task["status"]["steps"] if v["name"] == "publish")
                    test.assertEqual(publication["terminationReason"], "Skipped")
                    test.assertFalse(failed_run["status"].get("results", failed_run["status"].get("pipelineResults", [])))
                failed_build_log = manager.call(["logs", "pod/" + failed_pod_name, "-c", "step-build", "-n", plan["namespace"]])
                test.assertIn("Ran 3 tests", failed_build_log); test.assertIn("FAILED (failures=1)", failed_build_log)
                for token in gerrit["users"].values(): test.assertNotIn(token, failed_build_log)
                _, _, failure_evidence = reporter.evidence(reviewer, contract, template, digest, failed_run, report_plan)
                test.assertEqual(failure_evidence["state"], "failure")
                test.assertNotIn("steps", failure_evidence)
                stage = "actual-failed-build-negative-vote"
                print(json.dumps({"gerrit_worker_stage": stage}), flush=True)
                if native_watch:
                    context["wait"](lambda: native_vote(failed_run), seconds=60)
                else:
                    test.assertEqual(reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, failed_run), "reported")
                test.assertEqual(reporter.report_run(reviewer, destination, contract, template, digest, report_plan, report_digest, failed_run), "already-reported")
                failed_review = reporter.review(destination, plan, failed_item)
                failed_votes = [v for v in failed_review["labels"]["Verified"]["all"] if v.get("_account_id") == report_plan["account_id"]]
                test.assertEqual(len(failed_votes), 1); test.assertEqual(failed_votes[0]["value"], -1)
                report = {"real_gerrit_patchset_worker_build_verified": True, "gerrit_review_feedback_verified": True,
                    "scoped_submitter_and_reporter_secret_reads_denied": True, "real_application_tests_completed": 2,
                    "build_checkout_and_api_credential_isolation_verified": True, "replayed_submission_not_duplicated": True,
                    "replayed_result_not_reposted": True, "stale_patchset_vote_refused": True, "untested_patchset_approval_not_copied": True,
                    "modeled_ci_controller_worker_evidence": False, "gerrit_worker_evidence": evidence,
                    "native_bridge_workload_deployed": False, "continuous_event_delivery_verified": False,
                    "real_failed_application_tests_completed": 3, "real_failed_build_negative_vote_verified": True,
                    "failed_build_report_replay_not_reposted": True, "failed_build_successful_worker_claim_absent": True,
                    "failed_build_run_uid": failed_uid, "failed_build_task_uid": failed_task_uid, "failed_build_pod_uid": failed_pod_uid,
                    "failed_build_node_uid": reviewer.get("node/" + failed_pod["spec"]["nodeName"])["metadata"]["uid"]}
            if publication_context is not None:
                report.update(publication_context["result"])
                report.update(automatic_gerrit_pipeline_submission_verified=True, failed_build_publication_skipped_verified=not promotion, source_git_checkout_verified=True)
            if native_watch:
                units = watches.audit(gerrit["users"].values())
                report.update(native_watch_units=units, native_watch_services_started=True, native_watch_restart_and_replay_verified=True,
                    distinct_native_watch_uids_verified=True, continuous_patchset_polling_verified=True, native_watch_reported_positive_and_negative=not (argocd_check or promotion),
                    native_bridge_workload_deployed=True, automatic_ci_then_explicit_native_promotion_verified=argocd_check or (promotion and publication_context.get("native_promoter") is True))
        except Exception as error:
            print(json.dumps({"gerrit_worker_failure_stage": stage, "exception_type": type(error).__name__, "safe_reason": re.sub(r"[A-Za-z0-9_+/=-]{32,}", "<redacted>", str(error))[:220]}), flush=True)
            tasks = failure_tasks(manager,plan["namespace"])
            print(json.dumps({"actual_task_conditions": [[{k: c.get(k) for k in ("type", "status", "reason")} for c in t.get("status", {}).get("conditions", [])] for t in tasks]}), flush=True)
            print(json.dumps({"actual_task_steps": [[{"name": s.get("name"), "exit_code": s.get("terminated", {}).get("exitCode"), "reason": s.get("terminated", {}).get("reason")} for s in t.get("status", {}).get("steps", [])] for t in tasks]}), flush=True)
            for t in tasks:
                pod_name = t.get("status", {}).get("podName")
                if not pod_name: continue
                for step in (("checkout", "build", "publish") if publication_context is not None else ("checkout", "build")):
                    try: value = manager.call(["logs", "pod/" + pod_name, "-c", "step-" + step, "-n", plan["namespace"], "--tail=80"])
                    except (ValueError,OSError,subprocess.SubprocessError): continue
                    print(json.dumps({"application_step": step, "safe_log_signals": {s: s in value for s in (
                        "workspace-or-credentials", "[init]", "[fetch]", "[update-ref]", "[cat-file]", "[checkout]",
                        "workspace_writable=False", "source_token_readable=False", "source_ca_readable=False", "git_binary_present=False",
                        "Ran 0 tests", "Ran 2 tests", "FAILED", "PermissionError", "NoNewPrivs", "Seccomp", "ModuleNotFoundError", "Read-only file system")}}), flush=True)
            raise
    test.assertFalse(root.exists())
    if native_watch:
        for handle in watches.handles.values():
            info = watches.show(handle["name"])
            test.assertTrue(info.get("LoadState") == "not-found" or info.get("ActiveState") == "inactive" and info.get("MainPID") == "0")
        report["temporary_native_watch_units_removed"] = True
    report["temporary_gerrit_server_and_scope_removed"] = True
    report["gerrit_fixture_request_deadline_seconds"] = api.request_timeout
    report["gerrit_fixture_token_lifetime"] = api.token_lifetime
    report["gerrit_fixture_request_observations"] = api.request_observations
    report["gerrit_fixture_jvm_profile"] = gerrit["jvm_profile"]
    report["gerrit_fixture_rest_readiness_verified"] = True
    report["gerrit_fixture_rest_readiness_seconds"] = gerrit["rest_readiness_seconds"]
    return report
