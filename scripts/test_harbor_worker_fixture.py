#!/usr/bin/env python3
"""Opt-in actual Tekton build, Harbor robot publication and private digest pull on KVM workers."""

import base64
import hashlib
import http.client
import json
import os
import re
import tarfile
from pathlib import Path

import publish_harbor_static_image as publisher
from bounded_file import read_bounded_bytes
from render_harbor_static_task import task_spec
from strict_json import loads_strict_json
from test_harbor_core_replica_fixture import CoreReplicaChecks
from test_harbor_database_promotion_fixture import DatabasePromotionChecks
from test_harbor_jobservice_replica_fixture import JobReplicaChecks
from test_harbor_jwt_runtime_fixture import JWTChecks
from test_harbor_pool_health_fixture import PoolHealthChecks
from test_harbor_registry_runtime_fixture import RegistryChecks
from test_harbor_robot_fixture import RobotChecks
from test_harbor_service_runtime_fixture import ServiceChecks
from test_k3s_vm_fixture import ARTIFACTS, PROBE_MANIFEST, fingerprint
from test_tekton_worker_runtime_fixture import PYTHON_IMAGE

BODY = b"built by an isolated Tekton worker and pulled by digest\n"
REVISION = hashlib.sha1(b"disposable-worker-fixture-source-v1").hexdigest()


def base_image():
    path = Path(os.environ["K3S_VM_TEST_ARTIFACTS"]) / "k3s-airgap-images-amd64.tar.zst"
    fingerprint(path, ARTIFACTS[path.name])
    selected = {}; wanted = {"blobs/sha256/" + PROBE_MANIFEST}
    for _ in range(2):
        with tarfile.open(path, "r|zst") as archive:
            for item in archive:
                if item.name not in wanted: continue
                if not item.isfile() or not 0 < item.size <= publisher.LIMIT or item.name in selected: raise ValueError("Base archive identity changed")
                raw = archive.extractfile(item).read(publisher.LIMIT + 1)
                if len(raw) != item.size or publisher.digest(raw)[7:] != item.name.rsplit("/", 1)[1]: raise ValueError("Base archive content changed")
                selected[item.name] = raw
        if len(selected) == 1:
            manifest = loads_strict_json(selected["blobs/sha256/" + PROBE_MANIFEST])
            wanted = {"blobs/sha256/" + d["digest"][7:] for d in [manifest["config"], *manifest["layers"]]}
        else: break
    if set(selected) != {"blobs/sha256/" + PROBE_MANIFEST, *wanted}: raise ValueError("Pinned base image is incomplete")
    return selected


def http_ready(test, host, port):
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", "/"); response = connection.getresponse()
        if response.status != 200: return False
        test.assertEqual(response.read(1024), BODY)
        return True
    finally: connection.close()


def serve_published_image(test, context, harbor, namespace, image):
    client = context["client"]; host = image.split("/", 1)[0]; node_names = {n["name"] for n in context["nodes"][1:]}
    if host != "192.0.2.91:8443" or "@" not in image: raise ValueError("Use only the original disposable registry")
    manifest_digest = image.rsplit("@", 1)[1]
    if not publisher.DIGEST.fullmatch(manifest_digest): raise ValueError("Serve only a reviewed image digest")
    def create(value): return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(value).encode()))
    # This application identity is project Guest, distinct from both the publisher robot and operator.
    auth = base64.b64encode(("fixture-reader:" + harbor.reader_password).encode()).decode()
    config = {"auths": {host: {"auth": auth}}}
    create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "application-pull", "namespace": namespace}, "type": "kubernetes.io/dockerconfigjson",
        "data": {".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()}})
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
    deployment = create({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "built-web", "namespace": namespace}, "spec": {"replicas": 2,
        "selector": {"matchLabels": {"app": "built-web"}}, "template": {"metadata": {"labels": {"app": "built-web"}}, "spec": {
            "automountServiceAccountToken": False, "nodeSelector": {"platform.gitops/ci-worker": "true"}, "imagePullSecrets": [{"name": "application-pull"}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000},
            "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": {"app": "built-web"}}, "topologyKey": "kubernetes.io/hostname"}]}},
            "containers": [{"name": "web", "image": image, "imagePullPolicy": "Always", "securityContext": security,
                "ports": [{"containerPort": 8080}], "readinessProbe": {"httpGet": {"path": "/", "port": 8080}},
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "64Mi"}}}]}}}})
    service = create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "built-web", "namespace": namespace}, "spec": {"type": "NodePort", "selector": {"app": "built-web"}, "ports": [{"port": 8080, "targetPort": 8080}]}})
    client.call(["rollout", "status", "deployment/built-web", "-n", namespace, "--timeout=120s"], seconds=135)
    pods = json.loads(client.call(["get", "pods", "-n", namespace, "-l", "app=built-web", "-o", "json"]))["items"]
    test.assertEqual(len(pods), 2); test.assertEqual({p["spec"]["nodeName"] for p in pods}, node_names)
    for serving in pods:
        test.assertEqual(serving["spec"]["containers"][0]["image"], image)
        test.assertFalse(serving["spec"].get("volumes")); test.assertFalse(serving["spec"]["automountServiceAccountToken"])
        status = serving["status"]["containerStatuses"][0]; test.assertTrue(status["ready"])
        test.assertTrue(status["imageID"].endswith("@" + manifest_digest))
        test.assertRegex(status["containerID"], r"^containerd://[0-9a-f]{64}$")
    pod_uids = {pod["metadata"]["uid"] for pod in pods}
    def endpoints_ready():
        test.assertEqual(client.get("service/built-web", namespace)["metadata"]["uid"], service["metadata"]["uid"])
        slices = json.loads(client.call(["get", "endpointslices", "-n", namespace, "-l", "kubernetes.io/service-name=built-web", "-o", "json"]))["items"]
        endpoints = {e.get("targetRef", {}).get("uid") for item in slices
            if any(o.get("kind") == "Service" and o.get("uid") == service["metadata"]["uid"] for o in item["metadata"].get("ownerReferences", []))
            for e in item.get("endpoints", []) if e.get("conditions", {}).get("ready") is True}
        test.assertFalse(endpoints - pod_uids)
        return endpoints == pod_uids
    context["wait"](endpoints_ready, seconds=30)
    for node in context["nodes"][1:]:
        context["wait"](lambda node=node: http_ready(test, node["address"], service["spec"]["ports"][0]["nodePort"]), seconds=30)
    return {"actual_private_digest_pull_and_serving_verified": True, "actual_serving_deployment_uid": deployment["metadata"]["uid"]}


def worker_workload(test, context, harbor, native, *, source_context=None):
    client = context["client"]; namespace = "harbor-worker-publication"; core = harbor.service_clients["harbor-core"]
    ca = (native["root"] / "ca.crt").read_bytes(); credential_raw = (harbor.robot_directory / "credential.json").read_bytes()
    plan = {"schema_version": 1, "origin": "https://192.0.2.91:8443", "ca_file": "/publisher-input/ca.crt", "ca_sha256": hashlib.sha256(ca).hexdigest(),
        "leaf_sha256": core.leaf, "credential_file": "/run/robot/credential.json", "credential_sha256": hashlib.sha256(credential_raw).hexdigest(),
        "project_id": harbor.robot_plan["project"]["id"], "robot_id": harbor.robot_id, "repository": "fixture/ha", "base_digest": "sha256:" + PROBE_MANIFEST,
        "revision": source_context["source"]["revision"] if source_context is not None else REVISION, "files": ["index.html"]}
    # Trust is installed into each disposable guest's live containerd certs directory.
    # No skip_verify, hosts-file substitution or plaintext endpoint is used.
    host = "192.0.2.91:8443"; directory = "/var/lib/fixture-k3s/agent/etc/containerd/certs.d/" + host
    for node in context["nodes"][1:]:
        context["ssh"](node, "sudo mkdir -p " + directory)
        context["ssh"](node, "sudo tee " + directory + "/ca.crt >/dev/null", payload=ca.decode())
        hosts = 'server = "https://' + host + '"\n[host."https://' + host + '"]\n  capabilities = ["pull", "resolve"]\n  ca = "' + directory + '/ca.crt"\n'
        context["ssh"](node, "sudo tee " + directory + "/hosts.toml >/dev/null", payload=hosts)
        generated = context["ssh"](node, "sudo cat /var/lib/fixture-k3s/agent/etc/containerd/config.toml").stdout
        test.assertIn("/var/lib/fixture-k3s/agent/etc/containerd/certs.d", generated)
        # Verify from the real guest before scheduling, with its native TLS stack.
        result = context["ssh"](node, "curl --noproxy '*' --cacert " + directory + "/ca.crt --max-time 10 -sS -o /dev/null -w '%{http_code}' https://" + host + "/v2/")
        test.assertEqual(result.stdout, "401")
    def create(value): return json.loads(client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(value).encode()))
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
    account = create({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "static-builder", "namespace": namespace}, "automountServiceAccountToken": False})
    programs = {name: (Path(__file__).parent / name).read_text(encoding="utf-8") for name in ("publish_harbor_static_image.py", "bounded_file.py", "strict_json.py")}
    if source_context is not None:
        from render_gerrit_harbor_static_task import programs as source_programs
        programs = source_programs()
        create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "source-input", "namespace": namespace}, "immutable": True,
            "data": {"source.json": json.dumps(source_context["source"])}})
        create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "source-reader", "namespace": namespace}, "immutable": True,
            "data": {"token": base64.b64encode(source_context["token"].encode()).decode(), "ca.crt": base64.b64encode(source_context["ca"]).decode()}})
    create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "publisher-program", "namespace": namespace}, "immutable": True, "data": programs})
    create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "publisher-input", "namespace": namespace}, "immutable": True,
        "data": {"plan.json": json.dumps(plan), "ca.crt": ca.decode()}})
    robot = create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "publisher-robot", "namespace": namespace}, "immutable": True,
        "data": {"credential.json": base64.b64encode(credential_raw).decode()}})
    build = "from pathlib import Path; import os; assert os.getuid()==1000; assert not Path('/run/robot/credential.json').exists(); assert not Path('/publisher-input').exists(); assert not Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists(); Path('index.html').write_bytes(" + repr(BODY) + "); print('isolated-build-output-created')"
    task = task_spec(plan, build_image=PYTHON_IMAGE, publisher_image=PYTHON_IMAGE, build_argv=["python3", "-I", "-B", "-c", build],
        program_map="publisher-program", input_map="publisher-input", robot_secret="publisher-robot")
    if source_context is not None:
        from render_gerrit_harbor_static_task import task_spec as source_task
        task = source_task(plan, source_context["source"], checkout_image=PYTHON_IMAGE, build_image=PYTHON_IMAGE, publisher_image=PYTHON_IMAGE,
            build_argv=["python3", "-I", "-B", "/workspace/source/build.py"], program_map="publisher-program", input_map="publisher-input", robot_secret="publisher-robot",
            source_map="source-input", source_secret="source-reader")
    run = create({"apiVersion": "tekton.dev/v1", "kind": "TaskRun", "metadata": {"name": "static-publication", "namespace": namespace},
        "spec": {"serviceAccountName": "static-builder", "timeout": "5m0s", "taskSpec": task,
            "podTemplate": {"nodeSelector": {"platform.gitops/ci-worker": "true"}, "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}}}}})
    def completed():
        value = client.get("taskrun/static-publication", namespace)
        test.assertEqual(value["metadata"]["uid"], run["metadata"]["uid"])
        condition = next((c for c in value.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
        if condition.get("status") == "False":
            pod = client.get("pod/" + value["status"]["podName"], namespace)
            statuses = pod.get("status", {}).get("containerStatuses", [])
            print(json.dumps({"publication_failed_step_exit_codes": {s["name"]: s.get("state", {}).get("terminated", {}).get("exitCode") for s in statuses}}), flush=True)
            failed = next((v["name"] for v in statuses if v.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0), "step-publish")
            try: log = client.call(["logs", "pod/" + pod["metadata"]["name"], "-n", namespace, "-c", failed])
            except ValueError: log = ""
            if log:
                report_directory = Path(os.environ["K3S_VM_TEST_REPORT"]).resolve().parent
                report_directory.relative_to((Path(__file__).resolve().parents[1] / "private").resolve())
                if not re.fullmatch(r"step-(?:checkout|build|publish)", failed) or not re.fullmatch(r"[0-9a-f-]{36}", pod["metadata"]["uid"]): raise ValueError("Invalid private step diagnostic identity")
                target = report_directory / ("failed-" + pod["metadata"]["uid"] + "-" + failed + ".log")
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream: stream.write(log)
                locations = re.findall(r'File "[^"\n]*/([^/"\n]+[.]py)", line ([0-9]+)', log)
                print(json.dumps({"private_failed_step_log_saved": True, "step": failed,
                    "python_locations": [name + ":" + number for name, number in locations[-6:] if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)]}), flush=True)
            for line in log.splitlines():
                try: source_failure = loads_strict_json(line).get("source_bound_failure", {})
                except (ValueError, AttributeError): source_failure = {}
                if source_failure in ({"stage": "checkout", "details_suppressed": True}, {"stage": "publish", "details_suppressed": True}):
                    print(json.dumps({"trusted_source_step_diagnostic": source_failure}), flush=True)
                try: diagnostic = loads_strict_json(line).get("publisher_failure", {})
                except (ValueError, AttributeError): continue
                if (isinstance(diagnostic, dict) and set(diagnostic) == {"exception_type", "code_locations"}
                        and diagnostic["exception_type"] in {"ValueError", "OSError", "TimeoutError", "SSLError", "HTTPException", "FileInputTooLarge", "FileInputNotRegular", "PublicationFailure"}
                        and isinstance(diagnostic["code_locations"], list) and len(diagnostic["code_locations"]) <= 6
                        and all(isinstance(v, str) and re.fullmatch(r"(?:publish_harbor_static_image|bounded_file|strict_json)[.]py:[0-9]{1,5}", v) for v in diagnostic["code_locations"])):
                    print(json.dumps({"trusted_publisher_diagnostic": diagnostic}), flush=True)
            raise AssertionError("Actual publication TaskRun failed")
        return value if condition.get("status") == "True" else None
    actual = context["wait"](completed, seconds=330)
    results = actual["status"].get("results", actual["status"].get("taskResults", []))
    receipt = loads_strict_json(next(r["value"] for r in results if r["name"] == "image-receipt"))
    test.assertTrue(receipt["artifact_publication_verified"]); test.assertEqual(receipt["source_revision"], plan["revision"])
    image = receipt["image"]; test.assertEqual(image, host + "/fixture/ha@" + receipt["manifest_digest"])
    pod = client.get("pod/" + actual["status"]["podName"], namespace); node_names = {n["name"] for n in context["nodes"][1:]}
    test.assertIn(pod["spec"]["nodeName"], node_names)
    containers = {c["name"]: c for c in pod["spec"]["containers"]}
    test.assertFalse(any(m.get("mountPath") == "/run/robot" for m in containers["step-build"].get("volumeMounts", [])))
    test.assertTrue(any(m.get("mountPath") == "/run/robot" and m.get("readOnly") is True for m in containers["step-publish"]["volumeMounts"]))
    test.assertFalse(any("projected" in v for v in pod["spec"]["volumes"]))
    test.assertIn("isolated-build-output-created", client.call(["logs", "pod/" + pod["metadata"]["name"], "-n", namespace, "-c", "step-build"]))
    if source_context is not None:
        from source_bound_static_image import witness_bytes
        test.assertIs(receipt["source_checkout_witness_bound"], True)
        test.assertEqual(receipt["checkout_witness_sha256"], hashlib.sha256(witness_bytes(source_context["source"])).hexdigest())
        test.assertEqual(receipt["source_plan_sha256"], hashlib.sha256(publisher.canonical(source_context["source"])).hexdigest())
        mounts = {name: {m["name"]: m for m in value.get("volumeMounts", [])} for name, value in containers.items()}
        test.assertTrue(mounts["step-build"]["source-tree"]["readOnly"])
        test.assertTrue({"witness", "source-credential", "source-input", "robot"}.isdisjoint(mounts["step-build"]))
        test.assertNotIn("robot", mounts["step-checkout"]); test.assertNotIn("source-credential", mounts["step-publish"])
        test.assertTrue(mounts["step-publish"]["witness"]["readOnly"])
        statuses = {v["name"]: v for v in pod["status"]["containerStatuses"]}
        for name in ("step-checkout", "step-build", "step-publish"): test.assertEqual(statuses[name]["state"]["terminated"]["exitCode"], 0)
        # A conflicting requested commit must fail before either user build or publication.
        source_context["negative"](create, client, namespace, task, plan, context)
    served = serve_published_image(test, context, harbor, namespace, image)
    test.assertEqual(client.get("secret/publisher-robot", namespace)["metadata"]["uid"], robot["metadata"]["uid"])
    test.assertEqual(client.get("serviceaccount/static-builder", namespace)["metadata"]["uid"], account["metadata"]["uid"])
    return {"actual_ci_worker_registry_publication_verified": True, "actual_private_digest_pull_and_serving_verified": True,
        "build_publish_robot_mount_isolation_verified": True, "actual_publisher_taskrun_uid": actual["metadata"]["uid"], "actual_publisher_pod_uid": pod["metadata"]["uid"],
        **served, "publication_manifest_digest": receipt["manifest_digest"],
        "source_git_checkout_verified": source_context is not None, "wrong_source_revision_publication_refused_verified": source_context is not None,
        "source_revision": plan["revision"], "registry_image_signature_verified": False, "ci_cd_application_handoff_verified": False, "full_stack_verified": False}


def aggregate_worker_report(report, raw):
    if not isinstance(report, dict) or loads_strict_json(raw) != report: raise ValueError("Original private worker receipt changed")
    # The outer cache harness permits only booleans and maps of SHA256 values.
    # UIDs, resource samples and image receipts remain in the bound private file.
    result = {"valkey_gateway_harbor_" + key: value for key, value in report.items() if type(value) is bool}
    result["valkey_gateway_harbor_worker_receipt_sha256"] = {"receipt": hashlib.sha256(raw).hexdigest()}
    return result


class WorkerChecks(RobotChecks):
    application_timeout_seconds = 1800
    admission_workers = False
    guest_runtime_seconds = 1100

    def jwt_identities_ready(self, context):
        super().jwt_identities_ready(context)
        # Only the fixture supervisor seeds the reviewed base, using the restricted robot too.
        ca = (context["root"] / "ca.crt").read_bytes(); core = self.service_clients["harbor-core"]
        plan = {"origin": "https://192.0.2.91:8443", "repository": "fixture/ha", "leaf_sha256": core.leaf}
        registry = publisher.Registry(plan, ca, self.robot_credential); registry.authenticate()
        selected = base_image(); base = selected.pop("blobs/sha256/" + PROBE_MANIFEST)
        for raw in selected.values(): registry.upload(raw)
        status, headers, _ = registry.request("PUT", registry.prefix + "/manifests/sha256:" + PROBE_MANIFEST, base, publisher.MEDIA)
        context["test"].assertEqual(status, 201); context["test"].assertEqual(headers["docker-content-digest"], "sha256:" + PROBE_MANIFEST)
        import test_tekton_worker_runtime_fixture as tekton
        test = context["test"]
        self.worker_report = self.run_workers(test, lambda actual: tekton.exercise(test, actual,
            workload=lambda worker: self.worker_workload(test, worker, context)), ca)

    def run_workers(self, test, workload, registry_ca):
        import test_k3s_vm_fixture as vm
        return vm.exercise(test, admission_workers=self.admission_workers, guest_runtime_seconds=self.guest_runtime_seconds,
                           workload=workload)

    def worker_workload(self, test, context, native):
        return worker_workload(test, context, self, native)

    def exercise(self, context):
        result = super().exercise(context)
        path = Path(os.environ["K3S_VM_TEST_REPORT"])
        result.update(aggregate_worker_report(self.worker_report, read_bounded_bytes(path, max_bytes=1024 * 1024)))
        result["valkey_gateway_harbor_ci_worker_publication_verified"] = self.worker_report["actual_ci_worker_registry_publication_verified"]
        return result


def exercise(test):
    import test_valkey_gateway_runtime_fixture as gateway
    import test_valkey_runtime_fixture as cache
    class Application(WorkerChecks, PoolHealthChecks, JobReplicaChecks, CoreReplicaChecks, JWTChecks, DatabasePromotionChecks, ServiceChecks, RegistryChecks, gateway.Application): pass
    return cache.exercise(test, application=Application())
