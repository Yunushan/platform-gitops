#!/usr/bin/env python3
"""Synthetic source manifest and real worker HTTP checks for the VM CD case."""

import http.client
import json

from test_k3s_vm_fixture import PROBE_IMAGE, PROBE_MANIFEST
from tekton_forge_reporter import owned, uid


BODY = b"tested on the selected Git revision\n"
RKE2_PROBE_IMAGE = "public.ecr.aws/docker/library/busybox@sha256:" + PROBE_MANIFEST
KUSTOMIZATION = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["web.json"]}


def manifest(*, runtime="k3s"):
    if runtime not in {"k3s", "rke2"}: raise ValueError("Select the original K3s or RKE2 worker image")
    image = RKE2_PROBE_IMAGE if runtime == "rke2" else PROBE_IMAGE
    labels = {"app": "synthetic-web"}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "synthetic-application"}, "data": {"result": "tested"}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "synthetic-web-content"}, "data": {"index.html": BODY.decode()}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "synthetic-web"}, "spec": {
            "replicas": 2, "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
                "automountServiceAccountToken": False, "nodeSelector": {"platform.gitops/ci-worker": "true", "kubernetes.io/os": "linux"},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}]}},
                "containers": [{"name": "web", "image": image, "imagePullPolicy": "IfNotPresent", "command": ["/bin/httpd", "-f", "-p", "8080", "-h", "/www"],
                    "ports": [{"name": "http", "containerPort": 8080}], "readinessProbe": {"httpGet": {"path": "/", "port": "http"}, "periodSeconds": 2},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "32Mi"}},
                    "volumeMounts": [{"name": "content", "mountPath": "/www", "readOnly": True}]}],
                "volumes": [{"name": "content", "configMap": {"name": "synthetic-web-content", "defaultMode": 0o444}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "synthetic-web"}, "spec": {
            "type": "NodePort", "selector": labels, "ports": [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}]}}
    ]}


def application_test(*, runtime="k3s"):
    # This test is committed to Gerrit and executed by the real reviewed job.
    # It uses the build image's standard library, not the renderer's environment.
    return ('import json\nfrom pathlib import Path\nimport unittest\n'
        'class DeploymentInput(unittest.TestCase):\n'
        '    def test_reviewed_workload(self):\n'
        '        doc = json.loads(Path("deploy/staging/web.json").read_text())\n'
        '        self.assertEqual(doc, ' + repr(manifest(runtime=runtime)) + ')\n'
        + ('        build = json.loads(Path("deploy/staging/kustomization.yaml").read_text())\n'
           '        self.assertEqual(build, ' + repr(KUSTOMIZATION) + ')\n' if runtime == "rke2" else '')).encode()


def verify_workload(test, context, *, controller="argocd"):
    if controller not in {"argocd", "flux"}:
        raise ValueError("Select the Argo CD or Flux workload owner")
    manager, namespace = context["client"], "synthetic-application"
    if context.get("runtime") == "rke2":
        from test_gerrit_review_runtime_fixture import worker_inventory
        worker_inventory(context)
        workers = context["application_workers"]
    else:
        workers = context["nodes"][1:]
        if [node["address"] for node in workers] != ["192.0.2.3", "192.0.2.4"]:
            raise ValueError("Original private worker addresses changed")
    worker_names = {node["name"] for node in workers}
    image = RKE2_PROBE_IMAGE if context.get("runtime") == "rke2" else PROBE_IMAGE
    test.assertEqual(len(worker_names), 2)
    deployment = manager.get("deployment/synthetic-web", namespace)
    deployment_uid = uid(deployment["metadata"]["uid"])
    test.assertEqual(deployment["status"].get("availableReplicas"), 2)
    test.assertEqual(deployment["status"].get("updatedReplicas"), 2)
    if controller == "argocd":
        test.assertIn("argocd.argoproj.io/tracking-id", deployment["metadata"].get("annotations", {}))
    else:
        for name, value in (("name", "application"), ("namespace", "flux-system")):
            test.assertEqual(deployment["metadata"].get("labels", {}).get("kustomize.toolkit.fluxcd.io/" + name), value)
    def rows(resource): return json.loads(manager.call(["get", resource, "-n", namespace, "-l", "app=synthetic-web", "-o", "json"]))["items"]
    replicas = rows("replicasets")
    originals = {uid(r["metadata"]["uid"]) for r in replicas if owned(r, "Deployment", deployment_uid)}
    test.assertEqual(len(originals), 1)
    pods = rows("pods"); test.assertEqual(len(pods), 2)
    nodes, pod_uids = set(), set()
    for pod in pods:
        test.assertTrue(any(owned(pod, "ReplicaSet", parent) for parent in originals))
        test.assertFalse(pod["metadata"].get("deletionTimestamp"))
        test.assertEqual(pod["status"].get("phase"), "Running")
        test.assertTrue(any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod["status"].get("conditions", [])))
        node = manager.get("node/" + pod["spec"]["nodeName"])
        test.assertIn(node["metadata"]["name"], worker_names)
        test.assertEqual(node["metadata"]["labels"].get("platform.gitops/ci-worker"), "true")
        test.assertFalse(any(k in node["metadata"]["labels"] for k in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")))
        nodes.add(uid(node["metadata"]["uid"])); pod_uids.add(uid(pod["metadata"]["uid"]))
        test.assertIs(pod["spec"]["automountServiceAccountToken"], False)
        test.assertFalse(any("secret" in volume or "projected" in volume for volume in pod["spec"].get("volumes", [])))
        statuses = pod["status"].get("containerStatuses", [])
        test.assertEqual(len(statuses), 1); status = statuses[0]
        test.assertTrue(status["ready"]); test.assertTrue(status["state"].get("running"))
        test.assertEqual(pod["spec"]["containers"][0]["image"], image)
        if context.get("runtime") == "rke2":
            test.assertTrue(status["imageID"].endswith("@sha256:" + PROBE_MANIFEST))
        else:
            test.assertIn(status["imageID"], {image, image.split("@", 1)[0] + "@sha256:" + PROBE_MANIFEST})
        test.assertRegex(status["containerID"], r"^containerd://[0-9a-f]{64}$")
        value = manager.call(["exec", "pod/" + pod["metadata"]["name"], "-n", namespace, "-c", "web", "--", "/bin/sh", "-ec",
            'test "$(id -u)" = 1000; test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token; test ! -e /run/source/token; printf worker-isolation-ok'])
        test.assertEqual(value, "worker-isolation-ok")
    test.assertEqual(len(nodes), 2)
    service = manager.get("service/synthetic-web", namespace)
    service_uid = uid(service["metadata"]["uid"])
    test.assertEqual(service["spec"]["selector"], {"app": "synthetic-web"})
    port = service["spec"]["ports"][0]["nodePort"]
    test.assertIs(type(port), int); test.assertTrue(30000 <= port <= 32767)
    def endpoints_ready():
        test.assertEqual(manager.get("service/synthetic-web", namespace)["metadata"]["uid"], service_uid)
        slices = json.loads(manager.call(["get", "endpointslices", "-n", namespace, "-l", "kubernetes.io/service-name=synthetic-web", "-o", "json"]))["items"]
        endpoints = {e.get("targetRef", {}).get("uid") for s in slices if owned(s, "Service", service_uid) for e in s.get("endpoints", []) if e.get("conditions", {}).get("ready") is True}
        test.assertFalse(endpoints - pod_uids)
        return endpoints == pod_uids
    context["wait"](endpoints_ready, seconds=30)
    for node in workers:
        def serving():
            connection = http.client.HTTPConnection(node["address"], port, timeout=5)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                if response.status != 200: return False
                test.assertEqual(response.read(1025), BODY)
                return True
            finally: connection.close()
        context["wait"](serving, seconds=30)
    test.assertEqual(manager.get("deployment/synthetic-web", namespace)["metadata"]["uid"], deployment_uid)
    test.assertEqual(manager.get("service/synthetic-web", namespace)["metadata"]["uid"], service_uid)
    return {"actual_serving_workload_http_verified": True, "application_pods_on_two_real_workers_verified": True,
        "deployed_application_credential_isolation_verified": True, "application_deployment_uid": deployment_uid,
        "application_service_uid": service_uid, "application_pod_uids": sorted(pod_uids), "application_node_uids": sorted(nodes),
        "built_artifact_registry_publication_verified": False}
