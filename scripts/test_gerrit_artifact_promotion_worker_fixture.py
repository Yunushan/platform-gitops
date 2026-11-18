#!/usr/bin/env python3
"""Actual Gerrit artifact CI, merged image promotion, Argo rollout and private serving."""

import base64
import hashlib
import json
import os
from pathlib import Path
from unittest import mock
from urllib.parse import quote
from uuid import uuid4

import gerrit_artifact_promotion as promotion
import render_gerrit_artifact_promotion as renderer
import render_gerrit_promotion_access as access
from tekton_forge_reporter import owned
from test_argocd_reconciliation_fixture import exercise as argocd
from test_gerrit_artifact_worker_fixture import ArtifactChecks
from test_harbor_worker_fixture import (
    CoreReplicaChecks,
    DatabasePromotionChecks,
    JobReplicaChecks,
    JWTChecks,
    PoolHealthChecks,
    RegistryChecks,
    ServiceChecks,
    http_ready,
)

NAMESPACE = "synthetic-application"
NAME = "artifact-web"
MANIFEST = "deploy/staging/workload.json"


def deployment(image, *, replicas=2):
    if type(replicas) is not int or replicas not in (2, 3): raise ValueError("Select the original two or explicit three-worker profile")
    labels = {"app": NAME}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": {"replicas": replicas, "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
        "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
            "automountServiceAccountToken": False, "nodeSelector": {"platform.gitops/ci-worker": "true"}, "imagePullSecrets": [{"name": "application-pull"}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}]}},
            "containers": [{"name": "web", "image": image, "imagePullPolicy": "Always", "command": ["/bin/httpd", "-f", "-p", "8080", "-h", "/www"],
                "ports": [{"name": "http", "containerPort": 8080}], "readinessProbe": {"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 2},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "64Mi"}}}]}}}}


def retain(name, document):
    directory = Path(os.environ["K3S_VM_TEST_REPORT"]).resolve().parent
    directory.relative_to((Path(__file__).resolve().parents[1] / "private").resolve())
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream: json.dump(document, stream, sort_keys=True); stream.write("\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PromotionChecks(ArtifactChecks):
    promotion = True
    native_cli = False
    signature_gate = False
    application_replicas = 2

    def source_files(self, registry):
        base = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + registry["base_digest"]
        expected = deployment(base, replicas=self.application_replicas)
        source_test = ('import json\nfrom pathlib import Path\nimport unittest\n'
            'class PromotionInput(unittest.TestCase):\n'
            '    def test_reviewed_manifest(self):\n'
            '        self.assertEqual(json.loads(Path("/workspace/source/' + MANIFEST + '").read_text()), ' + repr(expected) + ')\n')
        return {MANIFEST: json.dumps(expected).encode(), "test_promotion_input.py": source_test.encode()}

    def promote_artifact(self, test, context, gerrit, source, run, receipt, execution, registry):
        from test_k3s_vm_fixture import application_workers
        workers = application_workers(context)
        api = gerrit["api"]; root = gerrit["root"]; project = quote(source["source"]["project"], safe="")
        writer = api.bootstrap("cd-writer")
        group = api.call("fixture-admin", "/a/groups/artifact-deployment-writers", {}, method="PUT")["id"]
        api.call("fixture-admin", "/a/groups/" + quote(group, safe="") + "/members/" + str(writer["_account_id"]), {}, method="PUT")
        reader_group = api.call("fixture-admin", "/a/groups/artifact-source-readers", {}, method="PUT")["id"]
        reader_id = execution["prepared"]["users"]["ci-read"]["_account_id"]
        api.call("fixture-admin", "/a/groups/" + quote(reader_group, safe="") + "/members/" + str(reader_id), {}, method="PUT")
        api.call("fixture-admin", "/a/projects/All-Projects/access", {"add": {"refs/meta/config": {"permissions": {"read": {"rules": {reader_group: {"action": "ALLOW"}}}}}}})
        api.call("fixture-admin", "/a/projects/" + project + "/access", {"add": {
            "refs/heads/deploy/staging": {"permissions": {"push": {"rules": {group: {"action": "ALLOW", "force": False}}}}},
            "refs/for/refs/heads/deploy/staging": {"permissions": {"pushMerge": {"exclusive": True, "rules": {group: {"action": "ALLOW"}}}}}}})
        before = api.call("ci-read", "/a/projects/" + project + "/branches/main")["revision"]
        api.call("fixture-admin", "/a/projects/" + project + "/branches/deploy%2Fstaging", {"revision": before}, method="PUT")
        base = registry["origin"].removeprefix("https://") + "/" + registry["repository"] + "@" + registry["base_digest"]
        state = {}
        def prepare(manager, target):
            def create(obj): return json.loads(manager.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(obj).encode()))
            auth = base64.b64encode(("fixture-reader:" + self.reader_password).encode()).decode()
            config = {"auths": {registry["origin"].removeprefix("https://"): {"auth": auth}}}
            create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "application-pull", "namespace": NAMESPACE}, "immutable": True,
                "type": "kubernetes.io/dockerconfigjson", "data": {".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()}})
            original = create(deployment(base, replicas=self.application_replicas))
            service = create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": NAME, "namespace": NAMESPACE},
                "spec": {"type": "NodePort", "selector": {"app": NAME}, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}})
            endpoint = json.loads(manager.call(["config", "view", "--minify", "-o", "json"]))["clusters"][0]["cluster"]["server"]
            policy = {"manifest": MANIFEST, "namespace": NAMESPACE, "deployment": NAME, "deployment_uid": original["metadata"]["uid"],
                "container": "web", "expected_image": base, "expected_deployment_revision": before,
                "commit_name": "Artifact writer", "commit_email": writer["email"], "destination_server": endpoint}
            state.update(policy=policy, original=original, service=service)
            return state

        def promote(cd, application, target):
            plan = {"schema_version": 2, "runtime": "raw", "bridge_contract_sha256": execution["digest"], "reporter_contract_sha256": execution["report_digest"],
                "source_read_username": "ci-read", "source_read_account_id": reader_id, "git_username": "cd-writer", "deployment_branch": "deploy/staging",
                "allow_create_branch": False, "allow_prune": False, "timeout_seconds": 180, "artifact_policy": state["policy"],
                "cd": {"kind": "argocd", "management_target": target, "namespace": "argocd", "name": application["metadata"]["name"],
                    "uid": application["metadata"]["uid"], "spec_sha256": promotion.common.sha256(application["spec"]), "path": "deploy/staging"}}
            bundle = root / "artifact-promotion"; bundle.mkdir(mode=0o700)
            files = renderer.render(execution["reporter_bundle"], plan)
            for name, text in files.items():
                path = bundle / name; path.write_text(text, encoding="utf-8", newline="\n"); path.chmod(0o600)
            digest = json.loads(files["artifact-promotion-status.json"])["contract_sha256"]
            _, report, contract, template = promotion.load_bundle(bundle, digest)
            access_plan = {"schema_version": 1, "ci": {"account": "gerrit-promotion-ci-reader", "namespace": source["submitter_namespace"],
                "workers": [n["name"] for n in workers]}, "cd": {"account": "fixture-promoter-read", "namespace": "argocd"}}
            rendered_access = access.render(bundle, digest, access_plan)
            for filename in ("ci-reader-rbac.json", "cd-reader-rbac.json"):
                for expected in json.loads(rendered_access[filename])["items"]:
                    kind = expected["kind"].lower() + (".rbac.authorization.k8s.io" if expected["kind"] != "ServiceAccount" else "")
                    actual = context["client"].get(kind + "/" + expected["metadata"]["name"], expected["metadata"].get("namespace"))
                    for field in set(expected) - {"metadata"}: test.assertEqual(actual.get(field), expected[field])
                    for field, value in expected["metadata"].items(): test.assertEqual(actual["metadata"].get(field), value)
            for attributes in ({"verb": "patch", "group": "apps", "resource": "deployments", "name": NAME},
                    {"verb": "get", "group": "apps", "resource": "deployments", "name": "outside-reviewed-workload"},
                    {"verb": "list", "group": "apps", "resource": "deployments"}, {"verb": "get", "resource": "secrets", "name": "application-pull"}):
                review = json.loads(cd.call(["create", "--raw", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "-f", "-"], payload=json.dumps({
                    "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview", "spec": {"resourceAttributes": {"namespace": NAMESPACE, **attributes}}}).encode()))
                test.assertIs(review["status"]["allowed"], False)
                test.assertFalse(review["status"].get("evaluationError"))
            inputs = root / "artifact-promoter-inputs"; inputs.mkdir(mode=0o700)
            for name, content in (("api-token", gerrit["users"]["ci-read"].encode()), ("token", gerrit["users"]["cd-writer"].encode()), ("ca.crt", (gerrit["tls"] / "ca.crt").read_bytes())):
                path = inputs / name; path.write_bytes(content); path.chmod(0o600)
            reader = promotion.legacy.ReadAPI(source, plan, inputs)
            args = (plan, report, contract, template, execution["promoter_ci"], cd, reader, run["metadata"]["name"])
            native_results = []
            watches = execution.get("watch_pair")
            def inspect_watches():
                if watches is not None:
                    watches.inspect("bridge"); watches.inspect("reporter")
            def invoke(*, apply=False, expected_code=0, record=None):
                if self.native_cli:
                    inspect_watches()
                    from test_gerrit_native_promotion_fixture import execute as native
                    value = native(test, context, root, bundle, digest, inputs, contract, plan, execution["promoter_ci"], cd,
                        run["metadata"]["name"], apply=apply, expected_code=expected_code, excluded_uids=(65534, *(watches.uids() if watches is not None else ()),
                            *(self.signature_uids.values() if self.signature_gate and hasattr(self, "signature_uids") else ())), artifact=True,
                        signature_inputs=self.signature_native_inputs if self.signature_gate and plan["schema_version"] == 3 else None)
                    inspect_watches()
                    native_results.append(value)
                    return value["result"]
                return promotion.execute(*args, apply=apply, record=record)
            with mock.patch.object(promotion, "INPUTS", inputs):
                if self.native_cli:
                    invoke(apply=True, expected_code=1)
                    diagnostic = native_results[-1]["journal"]["failure_diagnostic"]
                    test.assertTrue(any(frame["file"] == "tekton_gerrit_bridge.py" and frame["function"] == "current" for frame in diagnostic["code_locations"]))
                else:
                    with test.assertRaisesRegex(ValueError, "Change is outside this reviewed"):
                        invoke(apply=True)
                test.assertEqual(api.call("ci-read", "/a/projects/" + project + "/branches/deploy%2Fstaging")["revision"], before)
                number = execution["prepared"]["change_number"]
                api.call("fixture-admin", "/a/changes/" + str(number) + "/revisions/" + receipt["source_revision"] + "/review", {"labels": {"Code-Review": 2}, "notify": "NONE"})
                api.call("fixture-admin", "/a/changes/" + str(number) + "/submit", {"wait_for_merge": True, "notify": "NONE"})
                test.assertEqual(api.call("ci-read", "/a/projects/" + project + "/branches/main")["revision"], receipt["source_revision"])
                if self.signature_gate:
                    plan = self.prepare_signed_promotion(test, plan, report, contract, template, args)
                    files = renderer.render(execution["reporter_bundle"], plan)
                    for name, text in files.items():
                        path = bundle / name; path.write_text(text, encoding="utf-8", newline="\n"); path.chmod(0o600)
                    digest = json.loads(files["artifact-promotion-status.json"])["contract_sha256"]
                    _, report, contract, template = promotion.load_bundle(bundle, digest)
                    args = (plan, report, contract, template, execution["promoter_ci"], cd, reader, run["metadata"]["name"])
                preview = invoke()
                test.assertTrue(preview["read_only"])
                test.assertEqual(api.call("ci-read", "/a/projects/" + project + "/branches/deploy%2Fstaging")["revision"], before)
                if self.native_cli:
                    result = invoke(apply=True)
                    journal = native_results[-1]["journal"]
                else:
                    directory = Path(os.environ["K3S_VM_TEST_REPORT"]).resolve().parent / ("promotion-journal-" + uuid4().hex)
                    record, journal = promotion.common.new_journal(directory, {"schema_version": 2, "plan_sha256": promotion.common.sha256(plan), "handoff_contract_sha256": digest,
                        "controller_verified": False, "full_stack_verified": False})
                    result = invoke(apply=True, record=record)
                test.assertTrue(result["controller_verified"]); test.assertTrue(result["deployment_rollout_verified"])
                test.assertEqual(result["promoted_revision"], preview["prepared_revision"])
                test.assertNotEqual(result["promoted_revision"], receipt["source_revision"])
                replay = invoke(apply=True)
                test.assertFalse(replay["branch_push_attempted"])
                if self.signature_gate:
                    test.assertTrue(all(v["merged_ci_signature_verified"] for v in (preview, result, replay)))
                    test.assertTrue(all(v["result"]["merged_ci_signature_verified"] for v in native_results[1:]))
                    self.signature_native_results = native_results
            native_proof = {}
            if self.native_cli:
                test.assertEqual(len(native_results), 4)
                test.assertEqual(len({v["original_process"]["invocation_id"] for v in native_results}), 4)
                test.assertTrue(all(v["original_process"]["original_process_and_cgroup_removed"] is True for v in native_results))
                native_proof = {"native_runs": retain("promotion-native-runs.json", {"invocations": native_results})}
            state.update(plan=plan, journal=journal, result=result, application_uid=application["metadata"]["uid"])
            state["proof_sha256"] = {"plan": retain("promotion-plan.json", plan), "journal": retain("promotion-journal.json", journal),
                "contract": retain("promotion-contract.json", json.loads(files[promotion.CONTRACT])), **native_proof}
            return {"ci_cd_application_handoff_verified": True, "actual_artifact_ci_to_argocd_rollout_verified": True,
                "merged_ci_signature_verified": result.get("merged_ci_signature_verified", False),
                "unmerged_artifact_promotion_refused": True, "deployment_branch_preflight_read_only_verified": True,
                "exact_artifact_promotion_replay_verified": True, "rendered_promotion_access_live_verified": True,
                "promoter_cd_workload_write_and_secret_reads_denied": True, "native_promoter_cli_verified": self.native_cli,
                "artifact_native_promoter_nonroot_verified": self.native_cli, "artifact_native_promoter_units_removed": self.native_cli,
                "artifact_native_four_cli_modes_verified": self.native_cli,
                "source_revision": receipt["source_revision"], "artifact_deployment_revision": result["promoted_revision"],
                "artifact_promotion_proof_sha256": state["proof_sha256"], "full_stack_verified": False}

        def verify(manager, actual, outcome):
            original_uid = actual["original"]["metadata"]["uid"]; service_uid = actual["service"]["metadata"]["uid"]
            live = manager.get("deployment/" + NAME, NAMESPACE)
            test.assertEqual(live["metadata"]["uid"], original_uid)
            test.assertIn("argocd.argoproj.io/tracking-id", live["metadata"].get("annotations", {}))
            test.assertEqual(live["spec"]["template"]["spec"]["containers"][0]["image"], receipt["image"])
            rows = lambda resource: json.loads(manager.call(["get", resource, "-n", NAMESPACE, "-l", "app=" + NAME, "-o", "json"]))["items"]
            replicas = rows("replicasets"); parents = {r["metadata"]["uid"] for r in replicas if owned(r, "Deployment", original_uid)}
            pods = rows("pods"); test.assertEqual(len(pods), self.application_replicas)
            test.assertEqual({p["spec"]["nodeName"] for p in pods}, {n["name"] for n in workers})
            for pod in pods:
                test.assertTrue(any(owned(pod, "ReplicaSet", parent) for parent in parents))
                test.assertFalse(pod["metadata"].get("deletionTimestamp")); test.assertEqual(pod["status"]["phase"], "Running")
                test.assertFalse(pod["spec"].get("volumes")); test.assertIs(pod["spec"]["automountServiceAccountToken"], False)
                test.assertEqual(pod["spec"]["containers"][0]["image"], receipt["image"])
                test.assertEqual(pod["spec"]["imagePullSecrets"], [{"name": "application-pull"}])
                status = pod["status"]["containerStatuses"][0]; test.assertIs(status["ready"], True)
                test.assertTrue(status["imageID"].endswith("@" + receipt["manifest_digest"]))
                test.assertRegex(status["containerID"], r"^containerd://[0-9a-f]{64}$")
            pod_uids = {p["metadata"]["uid"] for p in pods}
            def endpoints():
                slices = json.loads(manager.call(["get", "endpointslices", "-n", NAMESPACE, "-l", "kubernetes.io/service-name=" + NAME, "-o", "json"]))["items"]
                serving = {e.get("targetRef", {}).get("uid") for s in slices if owned(s, "Service", service_uid)
                    for e in s.get("endpoints", []) if e.get("conditions", {}).get("ready") is True}
                test.assertFalse(serving - pod_uids)
                return serving == pod_uids
            context["wait"](endpoints, seconds=30)
            port = actual["service"]["spec"]["ports"][0]["nodePort"]
            for node in workers: context["wait"](lambda node=node: http_ready(test, node["address"], port), seconds=30)
            app = manager.get("application.argoproj.io/application", "argocd")
            test.assertEqual(app["metadata"]["uid"], actual["application_uid"])
            test.assertEqual(app["status"]["sync"]["revision"], actual["result"]["promoted_revision"])
            test.assertEqual(manager.get("service/" + NAME, NAMESPACE)["metadata"]["uid"], service_uid)
            admission = self.verify_promoted_admission(test, context, actual, live, pods, app) if hasattr(self, "verify_promoted_admission") else {}
            proof = {"application": retain("promotion-application.json", app), "deployment": retain("promotion-deployment.json", live),
                "pods": retain("promotion-pods.json", {"items": pods}), "replicasets": retain("promotion-replicasets.json", {"items": replicas}),
                "service": retain("promotion-service.json", manager.get("service/" + NAME, NAMESPACE))}
            return {"actual_private_digest_pull_and_serving_verified": True, "actual_promoted_digest_serving_on_two_workers_verified": self.application_replicas == 2,
                "actual_promoted_digest_serving_on_three_workers_verified": self.application_replicas == 3, **admission,
                "original_promoted_deployment_and_service_preserved": True, "deployed_application_credential_isolation_verified": True,
                "actual_serving_deployment_uid": original_uid, "artifact_rollout_proof_sha256": proof,
                "application_service_gitops_managed": False, "registry_image_signature_verified": False, "full_stack_verified": False}
        return argocd(test, gerrit, source, promote, worker_context=context, artifact_context={"prepare": prepare, "verify": verify})


class NativePromotionChecks(PromotionChecks):
    native_cli = True


class WatchedNativePromotionChecks(NativePromotionChecks):
    native_watch = True


def exercise(test, *, native_cli=False, native_watch=False):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    if native_watch and not native_cli:
        raise ValueError("Native artifact watches require the explicit native promoter")
    selected = WatchedNativePromotionChecks if native_watch else NativePromotionChecks if native_cli else PromotionChecks
    class Application(selected, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
