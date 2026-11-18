#!/usr/bin/env python3
"""Actual current Gerrit patch set to isolated worker build, Harbor digest and serving."""

import copy
import hashlib
import json

from source_bound_static_image import checkout_program_sha256
import tekton_gerrit_bridge as bridge
from test_tekton_gerrit_bridge import fixture as source_fixture
from test_gerrit_review_runtime_fixture import prepare, server
from test_harbor_worker_fixture import (BODY, WorkerChecks, worker_workload, PoolHealthChecks, JobReplicaChecks,
    CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks)


BUILD = """from pathlib import Path
import os
assert os.getuid() == 1000
for path in ('/run/robot/credential.json', '/run/source/token', '/run/provenance/checkout.json', '/source-input/source.json', '/var/run/secrets/kubernetes.io/serviceaccount/token'):
    assert not Path(path).exists()
try:
    Path('/workspace/source/forbidden-write').write_text('must be read-only')
except OSError:
    pass
else:
    raise AssertionError('source workspace was writable')
Path('/workspace/output/index.html').write_bytes(BODY_LITERAL)
print('isolated-build-output-created')
""".replace("BODY_LITERAL", repr(BODY))


class GerritWorkerChecks(WorkerChecks):
    def worker_workload(self, test, context, native):
        print(json.dumps({"gerrit_publication_stage": "native-gerrit-start"}), flush=True)
        with server(worker_context=context, active_processors=2) as gerrit:
            prepared = prepare(gerrit); api = gerrit["api"]; number = prepared["change_number"]
            native["private_values"].extend(gerrit["users"].values())
            api.call("fixture-admin", "/a/changes/" + str(number) + "/edit/build.py", BUILD.encode(), method="PUT", content_type="text/plain")
            api.call("fixture-admin", "/a/changes/" + str(number) + "/edit:publish", {"notify": "NONE"})
            plan = source_fixture(); plan.update(management_target=context["target"])
            ca = (gerrit["tls"] / "ca.crt").read_bytes()
            plan["source"].update(api_url=gerrit["origin"], clone_url=gerrit["origin"] + "/a/" + prepared["project"] + ".git", project=prepared["project"], ca_sha256=hashlib.sha256(ca).hexdigest())
            inputs = gerrit["root"] / "source-reader-inputs"; inputs.mkdir(mode=0o700)
            for name, raw in (("api-token", gerrit["users"]["ci-read"].encode()), ("ca.crt", ca)):
                path = inputs / name; path.write_bytes(raw); path.chmod(0o600)
            items = bridge.discover(bridge.Gerrit(plan, inputs), plan); test.assertEqual(len(items), 1); item = items[0]
            test.assertEqual(item["change"], number)
            source = {"schema_version": 1, "clone_url": plan["source"]["clone_url"], "read_username": "ci-read", "revision": item["revision"],
                "patchset_ref": item["ref"], "ca_sha256": hashlib.sha256(ca).hexdigest(), "checkout_program_sha256": checkout_program_sha256()}
            print(json.dumps({"gerrit_publication_stage": "current-patchset-selected"}), flush=True)
            def negative(create, client, namespace, task, publication, worker_context):
                wrong = "1" * 40 if source["revision"] != "1" * 40 else "2" * 40
                changed = {**source, "revision": wrong}
                create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "wrong-source-input", "namespace": namespace}, "immutable": True, "data": {"source.json": json.dumps(changed)}})
                invalid = copy.deepcopy(task)
                next(v for v in invalid["volumes"] if v["name"] == "source-input")["configMap"]["name"] = "wrong-source-input"
                run = create({"apiVersion": "tekton.dev/v1", "kind": "TaskRun", "metadata": {"name": "wrong-source-revision", "namespace": namespace},
                    "spec": {"serviceAccountName": "static-builder", "timeout": "5m0s", "taskSpec": invalid,
                        "podTemplate": {"nodeSelector": {"platform.gitops/ci-worker": "true"}, "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}}}}})
                before = self.sql("SELECT count(*) FROM artifact;", "registry")
                def stopped():
                    actual = client.get("taskrun/wrong-source-revision", namespace); test.assertEqual(actual["metadata"]["uid"], run["metadata"]["uid"])
                    condition = next((c for c in actual.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
                    if condition.get("status") == "True": raise AssertionError("Wrong commit unexpectedly succeeded")
                    return actual if condition.get("status") == "False" else None
                actual = worker_context["wait"](stopped, seconds=120)
                pod = client.get("pod/" + actual["status"]["podName"], namespace)
                statuses = {v["name"]: v for v in pod["status"]["containerStatuses"]}
                test.assertNotEqual(statuses["step-checkout"]["state"]["terminated"]["exitCode"], 0)
                for name in ("step-build", "step-publish"):
                    step = next(v for v in actual["status"]["steps"] if v["name"] == name.removeprefix("step-"))
                    test.assertEqual(step["terminationReason"], "Skipped")
                test.assertFalse(actual["status"].get("results", actual["status"].get("taskResults", [])))
                test.assertEqual(self.sql("SELECT count(*) FROM artifact;", "registry"), before)
                print(json.dumps({"gerrit_publication_stage": "wrong-commit-refused-before-build-and-publish"}), flush=True)
            selected = {"source": source, "ca": ca, "token": gerrit["users"]["ci-read"], "negative": negative}
            result = worker_workload(test, context, self, native, source_context=selected)
            current = bridge.discover(bridge.Gerrit(plan, inputs), plan); test.assertEqual(current, items)
            result.update(current_gerrit_patchset_selection_verified=True, source_readonly_and_witness_isolation_verified=True,
                automatic_gerrit_pipeline_submission_verified=False, gerrit_review_feedback_verified=False)
            return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(GerritWorkerChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
